"""``yuki-ui``: the tray app that owns the overlay, the strip and the two lanes.

:class:`YukiUi` is the only place that knows how a stream of agent events turns
into a visible thing. It holds no opinion about what a request *means*: it shows a
card per request, and when the worker starts using tools it steps aside for the
status strip. Every decision it makes is about who is busy and what is on screen --
including when memory's nudge cards (:mod:`yuki.ui.nudges`) may show.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Sequence

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from yuki.config import MODEL_ALIASES, Settings
from yuki.ui import startup
from yuki.ui.calls import CallController
from yuki.ui.surface import ACCENT
from yuki.ui.hotkey import HotkeyListener, hotkey_bindings


def user_bindings(bindings: dict[str, str]) -> dict[str, str]:
    """``bindings`` with the user's own "Open Yuki anywhere" shortcut from the window's Settings (``ui.json``
    ``open_hotkey``), unless ``YUKI_HOTKEY`` overrides it for a try-out."""
    import os

    if os.environ.get("YUKI_HOTKEY"):
        return bindings
    try:
        from yuki.ui import ui_settings

        own = ui_settings.load().open_hotkey
    except Exception:
        own = ""
    return {**bindings, "toggle": own} if own else bindings
from yuki.ui.memory import (
    STATUS_PENDING,
    HealthWindow,
    KnowsWindow,
    MemoryControl,
    ReviewWindow,
    TodayWindow,
    describe_memory_status,
    minutes_until_tomorrow,
    quiet_until,
)
from yuki.ui.nudges import (
    BUSY_NOTIFICATION_STATES,
    IMPLICIT_REPLY_S,
    NUDGE_EVENT_NAME,
    OVERLAY_NUDGES,
    NoticeCard,
    NudgeController,
    nudge_answered,
    nudge_moment,
    nudge_reply_request,
    notification_state,
    overlay_label,
)
from yuki.ui.overlay import Overlay, ReplyCard
from yuki.ui.runtime import FRONT_DESK, WORKER, AgentRuntime
from yuki.ui.status import StatusStrip, describe_summary, describe_tool
from yuki.ui.uilog import UiLog
from yuki.ui.window_host import WindowHost
from yuki.voice.indicator import VoicePill
from yuki.voice.session import VoiceSession

#: The effort levels offered in the tray menu (all five stay reachable from the
#: CLI's ``/effort``); a level outside these simply shows nothing ticked.
TRAY_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

class _HealthBridge(QObject):
    """The health check's thread hands its result to the GUI thread through this."""

    done = Signal(object)


#: Name of the mutex that keeps one Yuki UI per session.
MUTEX_NAME = "Local\\YukiUiSingleInstance"

#: Where a typed request was written, told to the agent as a bracketed note after the request
#: (Yuki's own format, stripped for display by chat_model.clean_request).
WHERE_TYPED = {
    "ask_bar": "[Typed in the ask bar: a small box, so the reply is a few short sentences.]",
    "window": "[Typed in the Yuki app's chat page: there is room for a complete answer.]",
}

_ERROR_ALREADY_EXISTS = 183


def single_instance_handle(name: str = MUTEX_NAME) -> int | None:
    """Claim the single-instance mutex.

    Args:
        name: Kernel object name.

    Returns:
        The handle to hold for the process lifetime, or None if another instance
        already owns it.
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def tray_icon() -> QIcon:
    """A small snowflake glyph, drawn rather than shipped as a file."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(16, 17, 20, 235))
    painter.setPen(QPen(QColor(255, 255, 255, 40), 2.0))
    painter.drawRoundedRect(3, 3, 58, 58, 16, 16)
    pen = QPen(ACCENT, 5.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    centre, arm = 32, 17
    painter.drawLine(centre, centre - arm, centre, centre + arm)
    for dx, dy in ((arm, arm), (arm, -arm)):
        painter.drawLine(
            int(centre - dx * 0.87), int(centre - dy * 0.5), int(centre + dx * 0.87), int(centre + dy * 0.5)
        )
    painter.end()
    return QIcon(pixmap)


class YukiUi(QObject):
    """Wires the hotkeys, the overlay, the strip and the runtime together.

    Args:
        settings: Shared settings for both agents.
        ui_log: Where UI events go.
        runtime: The agent runtime. One is built from ``settings`` if omitted.
        memory_control: The tray's memory controls. Built on the runtime's
            memory if omitted.
        nudge_event: The named event memory sets when it writes a nudge.
    """

    def __init__(
        self,
        settings: Settings,
        ui_log: UiLog,
        runtime: AgentRuntime | None = None,
        memory_control: MemoryControl | None = None,
        nudge_event: str = NUDGE_EVENT_NAME,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.ui_log = ui_log
        self.overlay = Overlay()
        self.strip = StatusStrip()
        self.runtime = runtime or AgentRuntime(settings, ui_log)
        self.memory = memory_control or MemoryControl(self.runtime.memory, ui_log)
        #: The "what Yuki knows" panel, built the first time it is asked for.
        self.knows: KnowsWindow | None = None
        self.memory.knows_ready.connect(self._on_knows_ready)
        #: The "Today's list" panel, built the first time it is asked for.
        self.today: TodayWindow | None = None
        self.memory.today_ready.connect(self._on_today_ready)
        #: The "This week's review" panel, built the first time it is asked for.
        self.review: ReviewWindow | None = None
        self.memory.review_ready.connect(self._on_review_ready)
        #: Memory's nudge cards, shown only while nothing else needs the screen.
        self.nudges = NudgeController(
            self.runtime.memory, ui_log, blocked=self._nudge_blocker, inline=self._nudge_inline,
            event_name=nudge_event,
        )
        self.nudges.reply_requested.connect(self._on_nudge_reply)
        self.nudges.in_overlay.connect(self._on_nudge_in_overlay)
        self.nudges.recent_ready.connect(self._on_recent_nudges)
        #: Calls, just the user's side: the consent card, the indicator and Stop.
        self.calls = CallController(
            self.runtime.memory, ui_log, covered=lambda: self.nudges.showing is not None,
            strip_up=self.strip.isVisible,
        )
        # The consent card has the corner: a nudge card up makes way (it goes back in the queue).
        self.calls.changed.connect(lambda _status: self.nudges.make_way() if self.calls.card_up else None)
        self.calls.card.faded_out.connect(self.nudges.try_show)
        self.nudges.card.faded_out.connect(self.calls.place_indicator)
        #: The nudge the user is writing a reply to (its card is in the overlay),
        #: and where Reply was pressed: ``card`` (the corner) or ``overlay``.
        self._nudge_reply: dict | None = None
        self._nudge_reply_via: str = "card"

        #: request id -> the card showing it.
        self.cards: dict[int, ReplyCard] = {}
        #: Worker requests that have already taken over the screen with the strip.
        self._acting: set[int] = set()
        #: Which lane is waiting for an answer, and for which request.
        self._question: tuple[str, int] | None = None
        #: Where each request's closing message landed (a card, or ``None`` for
        #: the strip), until its time/steps/cost suffix arrives.
        self._settled: dict[int, ReplyCard | None] = {}

        self.overlay.submitted.connect(self._on_submitted)
        self.overlay.dismissed.connect(self._on_overlay_dismissed)
        self.overlay.opened.connect(self._on_overlay_opened)
        # How each activation got (or failed to get) the keyboard: which step of
        # the foreground hand-over worked, and what Windows reported.
        self.overlay.focus_path.connect(self._on_focus_path)
        #: The window that was in front when the overlay last took the keyboard:
        #: where the user was before they started typing to Yuki.
        self._origin_hwnd: int | None = None

        runtime_signals = self.runtime
        runtime_signals.started.connect(self._on_started)
        runtime_signals.tool_called.connect(self._on_tool_called)
        runtime_signals.asked.connect(self._on_asked)
        runtime_signals.finished.connect(self._on_finished)
        runtime_signals.failed.connect(self._on_failed)
        runtime_signals.queued.connect(self._on_queued)
        runtime_signals.summarized.connect(self._on_summarized)
        # Moments the screen may have come free for a queued nudge.
        runtime_signals.lane_done.connect(self._on_lane_free)
        self.strip.faded_out.connect(self.nudges.try_show)
        self.overlay.faded_out.connect(self.nudges.try_show)

        self._settings_for_keys = settings
        self.bindings = user_bindings(hotkey_bindings(settings))
        self.hotkeys = self._listen(self.bindings)
        #: Voice (docs/VOICE.md): nothing runs until the tray, the hotkey or the
        #: wake word (if the user turned it on) starts it.
        self.voice = VoiceSession(settings, self.runtime, self.runtime.memory, ui_log)
        self.voice_pill = VoicePill()
        self.voice.state_changed.connect(self._on_voice_state)
        self.voice.devices_changed.connect(self.voice_pill.set_devices)
        self.calls.changed.connect(self.voice.on_call_status)
        runtime_signals.said.connect(self._on_said)
        # The Yuki window (its own process) and the live chat thread the overlay shows (docs/UI-window.md).
        self.window_host = WindowHost(self)
        self.overlay.attach_thread(self.window_host.model)
        #: One-off notices from the runtime (the daily cost alert) as a card, when the screen is free.
        self.notice_card: NoticeCard | None = None
        self._notices: list[tuple[str, str]] = []
        self._notice_retry = QTimer(self)
        self._notice_retry.setInterval(60_000)
        self._notice_retry.timeout.connect(self._show_next_notice)
        notice = getattr(runtime_signals, "notice", None)
        if notice is not None:
            notice.connect(self.show_notice)
        #: The tray's "Health check" panel (yuki doctor), built the first time it is asked for.
        self.health: HealthWindow | None = None
        self._health_bridge = _HealthBridge()
        self._health_bridge.done.connect(self._on_health_ready)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the lanes and the hotkey listener, and the memory service if it is down."""
        self.runtime.start()
        self.hotkeys.start()
        self.memory.ensure_service()
        # Watchdog: the memory service runs windowless, so if it ever dies the
        # user would never see it. Check every two minutes and restart it.
        self._memory_watchdog = QTimer(self)
        self._memory_watchdog.setInterval(120_000)
        self._memory_watchdog.timeout.connect(lambda: self.memory.ensure_service(quiet=True))
        self._memory_watchdog.start()
        self.nudges.start()
        self.calls.start()
        self.voice.start()
        self.window_host.start()
        self.ui_log.event(
            "start",
            hotkeys=self.bindings,
            model=self.settings.model,
            memory_session=getattr(self.runtime.memory, "session_id", None),
        )

    def stop(self) -> None:
        """Shut everything down in the right order.

        The memory service is left running: it is its own process, with its own
        lifetime, and keeps remembering while Yuki is closed.
        """
        self.ui_log.event("stop")
        self.window_host.stop()
        self.voice.shutdown()
        self.voice_pill.hide()
        self.nudges.stop()
        self.calls.stop()
        self.hotkeys.stop()
        self.runtime.stop()
        self.overlay.hide()
        self.strip.hide()
        self._notice_retry.stop()
        for panel in (self.knows, self.today, self.review, self.health, self.notice_card):
            if panel is not None:
                panel.hide()

    # -- input -------------------------------------------------------------

    def _listen(self, bindings: dict[str, str]) -> HotkeyListener:
        listener = HotkeyListener(bindings)
        listener.pressed.connect(self._on_hotkey)
        listener.failed.connect(
            lambda action, reason: self.ui_log.event("hotkey_failed", action=action, reason=reason)
        )
        return listener

    def reload_hotkeys(self) -> None:
        """The user set a new "Open Yuki anywhere" shortcut in the window (``ui.json``): listen for it now."""
        bindings = user_bindings(hotkey_bindings(self._settings_for_keys))
        if bindings == self.bindings:
            return
        self.hotkeys.stop()
        self.bindings = bindings
        self.hotkeys = self._listen(bindings)
        self.hotkeys.start()
        self.ui_log.event("hotkeys_reloaded", toggle=bindings.get("toggle", ""))

    def _on_hotkey(self, action: str) -> None:
        """React to a global hotkey."""
        self.ui_log.event("hotkey", action=action)
        if action == "cancel":
            self.runtime.cancel_worker()
            self.voice.interrupt()
            return
        if action == "voice":
            self.voice.toggle_conversation("hotkey")
            return
        self.overlay.toggle()
        if self.overlay.isVisible() and not self.overlay.closing:
            self.ui_log.event("overlay", state="shown", via="hotkey")

    def _on_focus_path(self, record: dict) -> None:
        """Log how the overlay got the keyboard, and remember where the user was.

        ``foreground_before`` is the window in front just before the overlay took
        focus. When it is the overlay itself (focus re-taken while already open),
        the earlier origin still stands.
        """
        self.ui_log.event("focus_path", **record)
        before = record.get("foreground_before")
        try:
            own = int(self.overlay.winId())
        except Exception:  # noqa: BLE001 - window not created yet
            own = None
        if before and before != own:
            self._origin_hwnd = int(before)

    def show_overlay(self) -> None:
        """Open the overlay (tray menu, or another instance asking for it)."""
        self.overlay.open()
        self.ui_log.event("overlay", state="shown", via="menu")

    def _on_submitted(self, text: str, via: str = "ask_bar") -> None:
        """The user pressed Enter: either an answer to a question, or a request.

        ``via``: ``ask_bar`` (the small overlay) or ``window`` (the app's chat page). The agent is
        told which, so a reply fits where it will be read (docs: prompt.py, reply length).
        """
        if self._question is not None:
            lane_name, request_id = self._question
            self._question = None
            self.runtime.answer(lane_name, text)
            self.window_host.user_answer(request_id, text)
            card = self.overlay.add_card(text)
            self.cards[request_id] = card
            self.overlay.expand_input()
            return
        # A nudge this answers (Reply pressed, or typed soon after the newest
        # one in the overlay) goes in front as the reply context; what the coach
        # said that this lane's agent has not heard yet goes after as a note.
        nudge, how = self._reply_context()
        request = (
            nudge_reply_request(nudge, text, certain=how != "implicit") if nudge is not None else text
        )
        nudge_id = nudge.get("id") if nudge is not None else None
        lane_guess = FRONT_DESK if self.runtime.worker_busy else WORKER  # as runtime.submit picks
        note, note_ids = self.nudges.coach_note_for(lane_guess, exclude=nudge_id)
        if note:
            request = f"{request}\n\n{note}"
        request = f"{request}\n\n{WHERE_TYPED.get(via, WHERE_TYPED['ask_bar'])}"
        lane_name, request_id = self.runtime.submit(request, origin_hwnd=self._origin_hwnd)
        self.window_host.user_message(request_id, text)
        self.cards[request_id] = self.overlay.add_card(text)
        self.overlay.expand_input()
        self.ui_log.event(
            "submit", id=request_id, lane=lane_name, text=text,
            **({"nudge_id": nudge_id} if nudge is not None else {}),
        )
        if nudge is not None:
            self.nudges.mark_attached(lane_name, [nudge_id])
            self.ui_log.event(
                "nudge_context_attached", mode="reply_prefix", how=how, id=nudge_id,
                kind=nudge.get("kind"), request_id=request_id, lane=lane_name,
            )
            if how != "implicit":
                # Only a pressed Reply is known to answer the nudge; a message
                # typed soon after it is context, not a recorded reply.
                card = self.overlay.nudge_card(nudge_id)
                if card is not None:
                    card.set_reply_state("replied")
                self.nudges.reply_sent(nudge, text, request_id, lane_name)
        if note:
            self.nudges.mark_attached(lane_name, note_ids)
            self.ui_log.event(
                "nudge_context_attached", mode="note", ids=note_ids, request_id=request_id,
                lane=lane_name, note=note,
            )

    def _reply_context(self) -> tuple[dict | None, str | None]:
        """The nudge the request being sent answers, and how that was decided.

        ``reply_button`` / ``card_reply``: the user pressed Reply on it (in the
        overlay / on the corner card), however old it is. ``implicit``: nothing
        was pressed, but the newest nudge in the overlay is unanswered and was
        seen at most :data:`IMPLICIT_REPLY_S` ago. Otherwise ``(None, None)``.
        """
        if self._nudge_reply is not None:
            nudge, self._nudge_reply = self._nudge_reply, None
            return nudge, "reply_button" if self._nudge_reply_via == "overlay" else "card_reply"
        shown = [card.nudge for card in self.overlay.nudge_cards()]
        if not shown:
            return None, None
        newest = max(shown, key=lambda n: nudge_moment(n) or 0.0)
        seen = nudge_moment(newest)
        # An implicit link is offered once, with the first message after the
        # nudge; after that the conversation has moved on and it is noise.
        used: set = self.__dict__.setdefault("_implicit_nudge_ids", set())
        if newest.get("id") in used:
            return None, None
        if nudge_answered(newest) or seen is None or time.time() - seen > IMPLICIT_REPLY_S:
            return None, None
        used.add(newest.get("id"))
        return newest, "implicit"

    # -- runtime events ----------------------------------------------------

    def _on_started(self, lane_name: str, request_id: int, request: str) -> None:
        card = self.cards.get(request_id)
        if card is None:
            self.cards[request_id] = self.overlay.add_card(request)

    def _on_tool_called(self, lane_name: str, request_id: int, name: str, tool_input: dict) -> None:
        """Show the step. For the worker, the first one takes the screen over."""
        line = describe_tool(name, tool_input)
        if lane_name == WORKER:
            if request_id not in self._acting:
                self._acting.add(request_id)
                if self.overlay.isVisible():
                    self.overlay.dismiss()
                self.ui_log.event("task_mode", id=request_id, tool=name)
            self.nudges.make_way()
            self.strip.show_step(line)
            return
        card = self.cards.get(request_id)
        if card is not None:
            card.set_body(line)

    def _on_asked(self, lane_name: str, request_id: int, question: str) -> None:
        """A lane needs an answer: bring the overlay back with the question."""
        self.ui_log.event("ask_user", id=request_id, lane=lane_name, question=question)
        if self.voice.owns(request_id):
            # Asked by voice: the voice says it and hears the answer. The strip
            # shows it without taking focus, and an answer typed later still works.
            self._question = (lane_name, request_id)
            self.strip.show_step(question)
            return
        self.strip.dismiss()
        self._question = (lane_name, request_id)
        card = self.cards.get(request_id)
        if card is None:
            self.cards[request_id] = self.overlay.add_card("", question, tone="question")
        else:
            card.set_body(question, tone="question")
        self.overlay.open()

    def _on_said(self, lane_name: str, request_id: int, text: str) -> None:
        """A progress line from the brain's ``say``: the strip shows it while the worker has the screen."""
        if lane_name == WORKER and request_id in self._acting:
            self.strip.show_step(text)

    def _on_voice_state(self, state: str, detail: str) -> None:
        """Voice changed state (listening, speaking, ...): the pill shows it."""
        self.voice_pill.show_state(state, detail)
        if state in ("error", "off", "starting", "note"):
            self.ui_log.event("voice", state=state, detail=detail)

    def _on_finished(self, lane_name: str, request_id: int, text: str) -> None:
        self._settle(lane_name, request_id, text, tone="reply")

    def _on_failed(self, lane_name: str, request_id: int, text: str) -> None:
        self._settle(lane_name, request_id, text, tone="error")

    def _settle(self, lane_name: str, request_id: int, text: str, *, tone: str) -> None:
        """Put a closing message where the user is actually looking."""
        self._acting.discard(request_id)
        if self._question is not None and self._question[1] == request_id:
            self._question = None
        card = self.cards.pop(request_id, None)
        if card is not None:
            card.set_body(text, tone="error" if tone == "error" else "reply")
        if self.overlay.isVisible() and not self.overlay.closing:
            if card is None:
                card = self.overlay.add_card("", text, tone="error" if tone == "error" else "reply")
            self.overlay.expand_input()
            self._settled[request_id] = card
        else:
            self.nudges.make_way()
            self.strip.show_final(text, tone=tone)
            self._settled[request_id] = None
        self.ui_log.event("settle", id=request_id, lane=lane_name, tone=tone, text=text)
        self.nudges.try_show()

    def _on_summarized(self, lane_name: str, request_id: int, summary: dict) -> None:
        """Put the dim ``52 s · 10 steps · 8¢`` suffix under the closing message."""
        if request_id not in self._settled:
            return
        target = self._settled.pop(request_id)
        text = describe_summary(summary)
        self.ui_log.event(
            "request_cost", id=request_id, lane=lane_name, suffix=text,
            cost_usd=summary.get("cost_usd"), wall_s=summary.get("wall_s"),
        )
        if not text:
            return
        if target is None:
            self.strip.set_meta(text)
            return
        try:
            target.set_meta(text)
        except RuntimeError:  # the card was trimmed away and deleted meanwhile
            pass

    def _on_queued(self, request_id: int, request: str, waiting: int) -> None:
        """A front-desk request turned out to need hands and went to the worker."""
        card = self.overlay.add_card(
            request, f"Queued — {waiting} ahead of it" if waiting > 1 else "Queued — up next"
        )
        self.cards[request_id] = card

    # -- nudges ------------------------------------------------------------

    def _nudge_blocker(self) -> str | None:
        """Why a nudge card may not show right now, or ``None`` when the screen is free.

        Busy means: the task strip is up, the worker has a request in hand, the
        user is writing a reply to a nudge, or the overlay is open with a
        request still waiting for its answer.
        """
        if self.strip.isVisible():
            return "status_strip"
        if self.calls.card_up:
            return "call_consent"
        if self._nudge_reply is not None:
            return "replying_to_nudge"
        if self.runtime.worker_busy:
            return "worker_busy"
        if self.overlay.isVisible() and not self.overlay.closing and (
            self.cards or self._question is not None
        ):
            return "overlay_request"
        return None

    def _on_lane_free(self, *_: object) -> None:
        """A lane finished (emitted from its thread; a bound slot, so it runs on the GUI thread)."""
        self.nudges.try_show()

    def _nudge_inline(self) -> bool:
        """True when a nudge should go into the overlay: it is open and not waiting on a question."""
        return self.overlay.isVisible() and not self.overlay.closing and self._question is None

    def _on_overlay_opened(self) -> None:
        """The overlay came up: queued nudges and a corner card move in; recent ones are fetched."""
        self.nudges.try_show()
        self.nudges.fetch_recent()

    def _on_recent_nudges(self, rows: object) -> None:
        """Memory's recent nudges arrived: the newest few go into the (still open) overlay."""
        if not (self.overlay.isVisible() and not self.overlay.closing):
            return
        rows = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
        for row in rows[-OVERLAY_NUDGES:]:
            self._add_nudge_card(row, "recent", seen_at=nudge_moment(row))

    def _on_nudge_in_overlay(self, nudge: dict, via: str) -> None:
        """A nudge delivered straight into the open overlay (it arrived, or the corner card moved in)."""
        self._add_nudge_card(nudge, via)

    def _add_nudge_card(self, nudge: dict, via: str, *, seen_at: float | None = None) -> ReplyCard | None:
        """Put ``nudge`` in the overlay's stack by the time it was seen; logs ``nudge_in_overlay``."""
        card = self.overlay.nudge_card(nudge.get("id"))
        if card is not None:
            return card
        at = seen_at or nudge_moment(nudge) or time.time()
        card = self.overlay.add_nudge_card(
            nudge, overlay_label(nudge), at=at
        )
        self.ui_log.event(
            "nudge_in_overlay", id=nudge.get("id"), kind=nudge.get("kind"), via=via,
            placed=card is not None, reaction=nudge.get("reaction"),
        )
        if card is None:  # older than a full stack of newer cards
            return None
        # From here the overlay's card and the controller share one dict, so
        # reactions recorded later show on it; it also counts as delivered.
        card.nudge = self.nudges.record_delivery(nudge, "overlay" if via != "recent" else "recent", seen_at=at)
        if card.nudge.get("reaction") == "replied":
            card.set_reply_state("replied")
        elif self._nudge_reply is not None and str(self._nudge_reply.get("id")) == str(nudge.get("id")):
            card.set_reply_state("replying")
        card.reply_clicked.connect(self._on_overlay_nudge_reply)
        return card

    def _on_overlay_nudge_reply(self, nudge: object) -> None:
        """Reply on a nudge card inside the overlay: the next thing typed answers it."""
        if not isinstance(nudge, dict):
            return
        previous = self._nudge_reply
        if previous is not None and str(previous.get("id")) != str(nudge.get("id")):
            self._release_nudge_reply(previous)
        self._nudge_reply, self._nudge_reply_via = nudge, "overlay"
        card = self.overlay.nudge_card(nudge.get("id"))
        if card is not None:
            card.set_reply_state("replying")
        self.overlay.input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.ui_log.event("nudge_reply_opened", id=nudge.get("id"), via="overlay")

    def _release_nudge_reply(self, nudge: dict) -> None:
        """Stop replying to ``nudge`` without sending. Its card's Reply opens again.

        One replied to from the corner card was never acknowledged at all, so it
        is acknowledged ``shown`` now (it was read, not dismissed).
        """
        card = self.overlay.nudge_card(nudge.get("id"))
        if card is not None:
            card.set_reply_state("open")
        if self._nudge_reply_via == "card":
            self.nudges.ack(nudge, "shown")
        self._nudge_reply = None

    def _on_nudge_reply(self, nudge: dict) -> None:
        """Reply on a nudge card: the overlay opens with the nudge in its conversation, being replied to."""
        self._nudge_reply, self._nudge_reply_via = nudge, "card"
        self._add_nudge_card(nudge, "reply")
        self.overlay.open()
        self.ui_log.event("overlay", state="shown", via="nudge_reply", nudge_id=nudge.get("id"))

    def _on_overlay_dismissed(self) -> None:
        """The overlay closed; a corner-card reply that was never sent counts as dismissed."""
        self.ui_log.event("overlay", state="hidden")
        if self._nudge_reply is not None:
            nudge, self._nudge_reply = self._nudge_reply, None
            card = self.overlay.nudge_card(nudge.get("id"))
            if card is not None:
                card.set_reply_state("open")
            if self._nudge_reply_via == "card":
                self.nudges.reply_abandoned(nudge)

    def snooze_nudges(self, minutes: int) -> None:
        """Quiet nudges for ``minutes`` (``0``: back on), then re-read the tray's status line."""
        self.nudges.snooze(minutes, on_done=self.memory.refresh_status)

    # -- tray --------------------------------------------------------------

    def set_model(self, alias_or_id: str) -> str:
        """Switch both lanes to a model from the next request (logs ``model_switch``).

        Returns:
            The model id now in force.
        """
        return self.runtime.set_model(alias_or_id)

    def set_effort(self, level: str) -> str:
        """Switch both lanes to an effort level (logs ``effort_switch`` per lane).

        Returns:
            The level now in force.
        """
        return self.runtime.set_effort(level)

    def show_knows(self) -> None:
        """Open the "what Yuki knows" panel and fill it from memory."""
        if self.knows is None:
            self.knows = KnowsWindow()
        self.knows.show_loading()
        self.knows.open()
        self.ui_log.event("knows", state="shown")
        self.memory.fetch_knows()

    def _on_knows_ready(self, portrait: object, knowhow: object, meta: str) -> None:
        if self.knows is not None:
            self.knows.show_knows(
                portrait if isinstance(portrait, str) else None,
                list(knowhow) if isinstance(knowhow, list) else [],
                meta,
            )

    def show_today(self) -> None:
        """Open the read-only "Today's list" panel and fill it from memory."""
        if self.today is None:
            self.today = TodayWindow()
        self.today.show_loading()
        self.today.open()
        self.ui_log.event("today", state="shown")
        self.memory.fetch_today()

    def _on_today_ready(self, rows: object, meta: str) -> None:
        if self.today is not None:
            self.today.waiting_note = self.calls.waiting
            self.today.show_today(list(rows) if isinstance(rows, list) else None, meta)

    def show_review(self) -> None:
        """Open the read-only "This week's review" panel and fill it from memory."""
        if self.review is None:
            self.review = ReviewWindow()
        self.review.show_loading()
        self.review.open()
        self.ui_log.event("review", state="shown")
        self.memory.fetch_review()

    def _on_review_ready(self, review: object, meta: str) -> None:
        if self.review is not None:
            self.review.show_review(review if isinstance(review, dict) else None, meta)

    # -- runtime notices and the health check ------------------------------

    def show_notice(self, kind: str, text: str) -> None:
        """``AgentRuntime.notice``: tell the user once, in a card bottom-right. It waits while a
        game or anything full screen is up, or while another card, the strip or the overlay has the screen."""
        self._notices.append((str(kind or ""), str(text or "")))
        self.ui_log.event("notice", kind=kind)
        self._show_next_notice()

    def _screen_free(self) -> bool:
        if self.strip.isVisible() or (self.overlay.isVisible() and not self.overlay.closing):
            return False
        if self.nudges.showing is not None or self.calls.card_up:
            return False
        value, _name = notification_state()
        return value not in BUSY_NOTIFICATION_STATES

    def _show_next_notice(self) -> None:
        if not self._notices:
            self._notice_retry.stop()
            return
        if not self._screen_free() or (self.notice_card is not None and self.notice_card.isVisible()):
            self._notice_retry.start()
            return
        kind, text = self._notices.pop(0)
        if self.notice_card is None:
            self.notice_card = NoticeCard()
            self.notice_card.faded_out.connect(self._show_next_notice)
        self.notice_card.show_notice(kind, text)
        self.ui_log.event("notice_shown", kind=kind)
        if not self._notices:
            self._notice_retry.stop()

    def show_health(self) -> None:
        """The tray's "Health check": ``yuki doctor`` off the GUI thread, then its checklist in a panel."""
        import threading

        if self.health is None:
            self.health = HealthWindow()
        self.health.show_loading()
        self.health.open()
        self.ui_log.event("health_check", state="started")
        settings = self.settings

        def run() -> None:
            started = time.monotonic()
            try:
                from yuki.core.doctor import format_report, run_checks

                results = run_checks(settings)
                report = format_report(results)
            except Exception as exc:   # the check itself failing is shown, never raised into the tray
                results, report = [], f"The health check couldn't run: {type(exc).__name__}: {exc}"
            self._health_bridge.done.emit((results, report, time.monotonic() - started))

        threading.Thread(target=run, name="yuki-health-check", daemon=True).start()

    def _on_health_ready(self, payload: object) -> None:
        results, report, took = payload if isinstance(payload, tuple) else ([], str(payload), 0.0)
        if self.health is not None:
            from datetime import datetime as _dt

            self.health.show_checks(list(results), report, f"Checked at {_dt.now():%H:%M} · took {took:.1f} s")
        self.ui_log.event("health_check", state="done", checks=len(results),
                          problems=sum(getattr(c, "status", "") == "fail" for c in results))

    def open_logs(self) -> None:
        """Open the session log folder in the file manager."""
        self.ui_log.event("open_logs", path=str(self.settings.sessions_dir))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.sessions_dir)))


def build_tray(ui: YukiUi, app: QApplication, icon: QIcon | None = None) -> QSystemTrayIcon:
    """Create the tray icon and its menu.

    Args:
        ui: The controller the menu acts on.
        app: The application, so Quit can end it.

    Returns:
        The tray icon, which the caller must keep a reference to.
    """
    tray = QSystemTrayIcon(icon or tray_icon(), app)
    from yuki.ui.surface import current_theme
    from yuki.ui.window.widgets import Menu, menu_css

    menu = Menu(current_theme())   # a flat popover like every surface

    def _menu_theme() -> None:   # restyled each time it opens (submenus are made later)
        from yuki.ui.window.widgets import style_menu

        menu.t = current_theme()
        menu.setStyleSheet(menu_css(menu.t))
        for sub in menu.findChildren(QMenu):   # the Microphone / Speaker / model submenus too
            if sub is not menu:
                style_menu(sub, current_theme)

    menu.aboutToShow.connect(_menu_theme)

    # Memory first: a status line (read afresh each time the menu opens), then
    # its three controls. Every memory call runs off the GUI thread and comes
    # back through MemoryControl's signals.
    memory_status = QAction(STATUS_PENDING, menu)
    memory_status.setEnabled(False)
    menu.addAction(memory_status)
    pause = QAction("Pause memory", menu, checkable=True)
    pause.triggered.connect(lambda checked: ui.memory.set_paused(bool(checked)))
    menu.addAction(pause)
    refresh_portrait = QAction("Refresh portrait now", menu)
    refresh_portrait.triggered.connect(lambda _checked=False: ui.memory.refresh_portrait())
    menu.addAction(refresh_portrait)
    knows = QAction("Show what Yuki knows", menu)
    knows.triggered.connect(lambda _checked=False: ui.show_knows())
    menu.addAction(knows)
    today = QAction("Today's list", menu)
    today.triggered.connect(lambda _checked=False: ui.show_today())
    menu.addAction(today)
    week_review = QAction("This week's review", menu)
    week_review.triggered.connect(lambda _checked=False: ui.show_review())
    menu.addAction(week_review)
    health = QAction("Health check", menu)
    health.triggered.connect(lambda _checked=False: ui.show_health())
    menu.addAction(health)
    quiet_hour = QAction("Quiet for 1 hour", menu)
    quiet_hour.triggered.connect(lambda _checked=False: ui.snooze_nudges(60))
    menu.addAction(quiet_hour)
    quiet_tomorrow = QAction("Quiet until tomorrow", menu)
    quiet_tomorrow.triggered.connect(lambda _checked=False: ui.snooze_nudges(minutes_until_tomorrow()))
    menu.addAction(quiet_tomorrow)
    nudges_on = QAction("Nudges on", menu)
    nudges_on.triggered.connect(lambda _checked=False: ui.snooze_nudges(0))
    menu.addAction(nudges_on)
    # Calls: only while memory is remembering the user's side of one (or waiting for headphones).
    stop_call = QAction("Stop remembering this call", menu)
    stop_call.triggered.connect(lambda _checked=False: ui.calls.stop_call())
    stop_call.setVisible(False)
    menu.addAction(stop_call)
    # Calls waiting for the deferred transcriber: a count, and a way to throw the audio away.
    call_waiting = QAction("", menu)
    call_waiting.setEnabled(False)
    call_waiting.setVisible(False)
    menu.addAction(call_waiting)
    erase_calls = QAction("Erase waiting call audio", menu)
    erase_calls.triggered.connect(lambda _checked=False: ui.calls.erase_waiting())
    erase_calls.setVisible(False)
    menu.addAction(erase_calls)

    def show_calls(_status: object = None) -> None:
        stop_call.setVisible(ui.calls.recording)
        text = ui.calls.waiting
        call_waiting.setText(text)
        call_waiting.setVisible(bool(text))
        erase_calls.setVisible(bool(text))

    ui.calls.changed.connect(show_calls)
    menu.aboutToShow.connect(show_calls)
    menu.aboutToShow.connect(ui.calls.refresh)

    def show_memory_status(status: object, error: object) -> None:
        del error
        current = status if isinstance(status, dict) else None
        memory_status.setText(
            describe_memory_status(current, installed=ui.memory.memory.installed)
        )
        running = bool(current and current.get("service_running"))
        pause.blockSignals(True)
        pause.setChecked(bool(current and current.get("paused")))
        pause.blockSignals(False)
        pause.setEnabled(running)
        refresh_portrait.setEnabled(current is not None)
        installed = ui.memory.memory.installed
        for item in (knows, today, week_review, quiet_hour, quiet_tomorrow):
            item.setEnabled(installed)
        nudges_on.setEnabled(installed and quiet_until(current) is not None)

    ui.memory.status_changed.connect(show_memory_status)
    menu.aboutToShow.connect(lambda: ui.memory.refresh_status())
    menu.addSeparator()

    show = QAction("Show", menu)
    show.triggered.connect(ui.show_overlay)
    menu.addAction(show)
    open_window = QAction("Open Yuki window", menu)
    open_window.triggered.connect(lambda _checked=False: ui.window_host.open_window())
    menu.addAction(open_window)

    # Voice: a conversation now (headphones or speakers, docs/VOICE.md §11), and the wake word (remembered).
    voice_item = QAction(f"Voice ({ui.bindings.get('voice') or 'no hotkey'})", menu, checkable=True)
    voice_item.triggered.connect(
        lambda checked: ui.voice.start_conversation("tray") if checked else ui.voice.stop_conversation("tray")
    )

    def show_voice(on: bool) -> None:
        voice_item.blockSignals(True)
        voice_item.setChecked(bool(on))
        voice_item.blockSignals(False)

    ui.voice.conversation_changed.connect(show_voice)
    menu.addAction(voice_item)
    wake_item = QAction("Listen for \u2018Yuki\u2019", menu, checkable=True)
    wake_item.setChecked(ui.voice.wake_enabled)
    wake_item.triggered.connect(lambda checked: ui.voice.set_wake(bool(checked)))
    menu.addAction(wake_item)

    def choose_voice() -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            None, "Yuki's voice",
            "ElevenLabs voice id (from the voice's page in ElevenLabs).\nLeave empty for the default voice.",
            text=ui.voice.tts_voice,
        )
        if ok:
            ui.voice.set_tts_voice(text)

    voice_id_item = QAction("Voice id…", menu)
    voice_id_item.triggered.connect(choose_voice)
    menu.addAction(voice_id_item)

    # Which microphone and which output voice uses: the active Core Audio devices, listed
    # afresh each time the menu opens, "System default" first; remembered in voice.json.
    # The first line of each says what voice has open right now (the speaker choice moves a
    # running conversation's output at once; the mic is never changed with it).
    device_menus = {kind: menu.addMenu(label) for kind, label in (("capture", "Microphone"), ("render", "Speaker"))}

    def fill_devices() -> None:
        from yuki.voice.audio import audio_devices

        found = audio_devices()
        for kind, submenu in device_menus.items():
            submenu.clear()
            group = QActionGroup(submenu)
            group.setExclusive(True)
            chosen = ui.voice.device_choice(kind)
            default = found.get(f"default_{kind}")
            in_use = ui.voice.device_in_use(kind)
            info = QAction(f"In use now: {in_use}" if in_use else "Not open right now", submenu)
            info.setEnabled(False)
            submenu.addAction(info)
            submenu.addSeparator()
            names: list[str | None] = [None, *found.get(kind, [])]
            if chosen and chosen not in names:
                names.append(chosen)  # chosen but not connected now: shown, so it can be un-chosen
            for name in names:
                if name is None:
                    label = "System default" + (f" ({default})" if default else "")
                else:
                    label = name + ("" if name in found.get(kind, []) else " (not connected)")
                item = QAction(label, submenu, checkable=True)
                item.setChecked((name or None) == (chosen or None))
                item.triggered.connect(lambda _checked=False, k=kind, n=name: ui.voice.set_device(k, n))
                group.addAction(item)
                submenu.addAction(item)

    menu.aboutToShow.connect(fill_devices)

    # Model and effort: one checkable item per choice, the current one ticked.
    # Both go through the runtime, so both lanes switch and each agent logs it.
    menu.addSeparator()
    model_group = QActionGroup(menu)
    model_group.setExclusive(True)
    model_items: dict[str, QAction] = {}
    for alias, model_id in MODEL_ALIASES.items():
        item = QAction(f"Model: {alias.capitalize()}", menu, checkable=True)
        item.triggered.connect(lambda _checked=False, a=alias: switch_model(a))
        model_group.addAction(item)
        menu.addAction(item)
        model_items[model_id] = item

    menu.addSeparator()
    effort_group = QActionGroup(menu)
    effort_group.setExclusive(True)
    effort_items: dict[str, QAction] = {}
    for level in TRAY_EFFORTS:
        item = QAction(f"Effort: {level.capitalize()}", menu, checkable=True)
        item.triggered.connect(lambda _checked=False, lv=level: switch_effort(lv))
        effort_group.addAction(item)
        menu.addAction(item)
        effort_items[level] = item

    def refresh_ticks() -> None:
        # Exclusive groups cannot be emptied by unchecking one item, so drop
        # exclusivity while syncing (a model or effort outside the menu, e.g.
        # xhigh from settings, shows nothing ticked).
        for group, items, current in (
            (model_group, model_items, ui.settings.model),
            (effort_group, effort_items, ui.settings.effort),
        ):
            group.setExclusive(False)
            for key, item in items.items():
                item.setChecked(key == current)
            group.setExclusive(True)
        tray.setToolTip(f"Yuki — {ui.settings.model} · effort {ui.settings.effort}\n{ui.voice.devices_text()}")

    def switch_model(alias: str) -> None:
        ui.set_model(alias)
        refresh_ticks()

    def switch_effort(level: str) -> None:
        ui.set_effort(level)
        refresh_ticks()

    menu.aboutToShow.connect(refresh_ticks)
    menu.addSeparator()

    logs = QAction("Open logs folder", menu)
    logs.triggered.connect(ui.open_logs)
    menu.addAction(logs)

    # Start with Windows: the per-user Run value (yuki.ui.startup), read afresh each time the menu opens.
    start_with_windows = QAction("Start with Windows", menu, checkable=True)

    def show_startup() -> None:
        start_with_windows.blockSignals(True)
        start_with_windows.setChecked(startup.is_enabled())
        start_with_windows.blockSignals(False)

    def toggle_startup(checked: bool) -> None:
        try:
            now_on = startup.set_enabled(bool(checked))
            ui.ui_log.event("startup_toggle", enabled=now_on, command=startup.read_value())
        except OSError as exc:
            ui.ui_log.event("startup_toggle", enabled=startup.is_enabled(), error=f"{type(exc).__name__}: {exc}")
        show_startup()

    start_with_windows.triggered.connect(toggle_startup)
    menu.aboutToShow.connect(show_startup)
    show_startup()
    menu.addAction(start_with_windows)

    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    refresh_ticks()
    tray.activated.connect(   # a left click opens the Yuki window (its own process)
        lambda reason: ui.window_host.open_window()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()
    return tray


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Yuki desktop shell.

    Args:
        argv: Unused; present for the console-script entry point.

    Returns:
        Process exit code. ``1`` if another instance is already running.
    """
    del argv
    handle = single_instance_handle()
    if handle is None:
        print("yuki-ui is already running (press the hotkey to show it)", file=sys.stderr)
        return 1

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName("Yuki")
    app.setQuitOnLastWindowClosed(False)
    from yuki.ui.window.theme import app_font

    app.setFont(app_font())   # Segoe UI Variable with its real weights, for the tray menu and plain widgets

    # Load the heavy native modules and draw the tray icon on this thread before any worker
    # thread starts. On 2026-09-25 the agent's prewarm thread was loading PIL's DLL (inside the
    # Windows loader lock) while this thread painted the icon, and Qt's raster engine waited on a
    # helper thread that could not start without that lock: the tray never came up and the
    # memory service was never started.
    import yuki.perception  # noqa: F401  (PIL, win32ui, comtypes: loaded here, not in a thread)

    icon = tray_icon()
    settings = Settings()
    ui_log = UiLog(sessions_dir=settings.sessions_dir, session_id=None)
    # Start with Windows is on by default: the first run turns it on once and records that it
    # did, so a later "off" from the tray is kept (yuki.ui.startup.apply_default).
    ui_log.event("startup_default", **startup.apply_default())
    ui = YukiUi(settings, ui_log)
    tray = build_tray(ui, app, icon)
    ui.start()

    try:
        code = app.exec()
    finally:
        ui.stop()
        tray.hide()
        ui_log.close()
    return int(code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
