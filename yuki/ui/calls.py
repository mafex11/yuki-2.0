"""Calls, both sides: the consent card, the "what is being kept" indicator, and Stop.

Since 3 Oct 2026 (handbook rule 9, report 60) a call is recorded on both sides: the user's microphone (headphones
only) and the call app's own sound; a game's own voice chat is a session of its own (``kind`` "game") with its own
track. The memory service (:mod:`yuki.memory.calls`) detects a call or a game's voice chat and writes a ``calls``
row; it sets the named auto-reset event :data:`CALL_EVENT_NAME` whenever their state changes. :class:`CallWatcher`
waits on it (and looks every :data:`FALLBACK_POLL_S` anyway) and fetches ``call_status()``.

:class:`CallController` shows, never taking focus:

* :class:`CallConsentCard` while memory is asking (:func:`yuki.ui.call_words.card_target`): a call's card ("You're
  on a Discord call." - your mic and Discord's sound, turned into text later on this PC), a game's ("Remember
  VALORANT voice chat?" - your mic and the game's voice chat as their own track), never for a game still waiting
  to be seen (``ask_later``), and the card of a game session that ended before it could be shown
  (``pending_card``). Without headphones it says the mic waits for them while the app's sound is kept either way.
  Record / Not this call and Always for <app> (Record = yes or always, Not = no or never); every press goes to
  ``answer_call`` exactly once; a card left alone goes away after its ``ask_timeout_s`` and is answered ``none``
  (nothing recorded).
* The indicator (the island now, :mod:`yuki.ui.island`; :class:`CallIndicator` is the older pill) while anything
  is kept or a row waits for headphones: "Recording · Discord + you", "Recording · VALORANT voice + you",
  "Recording · Discord (your mic waits for headphones)" (:mod:`yuki.ui.call_words`, from ``mic`` and ``tracks``).

The tray's way to stop (Pause listening for this call, the window's Stop remembering) calls
:meth:`CallController.stop_call`; :attr:`CallController.recording` follows the status's top-level ``recording``, so
Stop is there whenever anything is kept (an app's own sound alone too). While call audio waits for the deferred
transcriber the tray shows "N calls waiting to be remembered" (also on the Today panel) and offers "Erase waiting
call audio" (:meth:`CallController.erase_waiting`). Nothing here reads, sends or shows anything that was said.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from yuki.agent.memory import TRAY_TIMEOUT_S, MemoryAccess, MemoryUnavailable
from yuki.ui.call_words import (RECORDING_STATUSES, call_rec, card_keys, card_target, consent_text, head_label,
                                indicator_text, is_game, live_view)
from yuki.ui.surface import SurfaceWindow
from yuki.ui.lattice_cards import LatticeCard
from yuki.ui.nudges import BUSY_NOTIFICATION_STATES, NudgeWatcher, notification_state
from yuki.ui.status import STRIP_HEIGHT, STRIP_MARGIN, STRIP_WIDTH
from yuki.ui.uilog import UiLog

#: The named event the memory service sets when a call's state changes.
CALL_EVENT_NAME = "Local\\YukiCallReady"
#: Fallback look at ``call_status()`` when the event stays quiet.
FALLBACK_POLL_S = 20.0
#: While a call is on, look this often too (the indicator follows the status).
ACTIVE_POLL_MS = 10_000

CARD_WIDTH = STRIP_WIDTH
INDICATOR_WIDTH = 236
INDICATOR_HEIGHT = 40
_GAP = 8


def waiting_text(waiting: Any) -> str:
    """``"2 calls waiting to be remembered"`` for ``call_status()["waiting"]``; ``""`` when none."""
    n = int((waiting or {}).get("calls") or 0) if isinstance(waiting, dict) else 0
    if n <= 0:
        return ""
    return f"{n} call{'' if n == 1 else 's'} waiting to be remembered"


#: What the call pill adds while Yuki is not listening for its name (v3.16, 04 F6; the voice session's own words).
NOT_LISTENING_TEXT = "Yuki isn't listening"


def _waiting_only(call: dict[str, Any]) -> bool:
    """Nothing is kept this moment and the row waits for headphones: the pill has no rec dot and no timer."""
    return call.get("status") == "waiting_headphones" and not call_rec(call)


class CallWatcher(NudgeWatcher):
    """:class:`yuki.ui.nudges.NudgeWatcher` on :data:`CALL_EVENT_NAME`, fetching ``call_status()``."""

    def __init__(self, memory: MemoryAccess, *, event_name: str = CALL_EVENT_NAME,
                 poll_s: float = FALLBACK_POLL_S) -> None:
        super().__init__(memory, event_name=event_name, poll_s=poll_s)

    def _fetch(self, why: str) -> None:
        try:
            status = self.memory.call_method("call_status", timeout_s=TRAY_TIMEOUT_S)
        except MemoryUnavailable as exc:
            if str(exc) != self._last_error:
                self._last_error = str(exc)
                self.problem.emit("call_status", str(exc))
            return
        except Exception as exc:  # the watcher must outlive anything memory does
            error = f"{type(exc).__name__}: {exc}"
            if error != self._last_error:
                self._last_error = error
                self.problem.emit("call_status", error)
            return
        self._last_error = None
        self.arrived.emit(status if isinstance(status, dict) else {}, why)


class CallConsentCard(LatticeCard):
    """The consent card in the Lattice design (top right, the design's ``.card.top``): "Call · Discord · 21:06", the
    question in 17 px, what is recorded (your mic and Discord's sound, :func:`yuki.ui.call_words.consent_text`),
    **Record this call** and **Not this call**, and a check "Always for Discord" (checked: Record answers
    ``always``, Not this call answers ``never``). A game's voice chat: "Game voice · VALORANT · 21:06", "Remember
    VALORANT voice chat?", **Record it** / **Not this time**; a game that already ended (its card could not be shown
    over it): **Yes** / **No**, Always holding for the next one. Never takes focus.

    Signals:
        answered: ``(call, answer)`` -- ``yes`` | ``no`` | ``always`` | ``never``, or ``none``
            when it timed out untouched.
    """

    answered = Signal(object, str)
    corner = "top"

    def __init__(self) -> None:
        super().__init__(icon="call")
        self.setObjectName("YukiCallConsentCard")
        self.call: dict[str, Any] | None = None
        from yuki.ui import lattice as L

        self.title = L.Label("A call started", parent=self)   # the words of the header ("Discord started a call")
        self.title.hide()
        self.when = L.Label("", parent=self)
        self.when.hide()
        self.question = L.Para("", font=L.ui_font(17, 600), line_height=1.45, parent=self)
        self.content.addWidget(self.question)
        self.content.addSpacing(6)
        self.detail = L.Para("", font=L.ui_font(14), color=L.TEXT2, line_height=1.45, parent=self)
        self.content.addWidget(self.detail)
        self.note_gap = QWidget(self)
        self.note_gap.setFixedHeight(6)
        self.content.addWidget(self.note_gap)
        self.note = L.Para("", font=L.ui_font(13), line_height=1.45, parent=self)   # said plainly (no warning colour)
        self.content.addWidget(self.note)
        acts = QHBoxLayout()
        acts.setContentsMargins(0, 14, 0, 0)
        acts.setSpacing(6)
        self.yes_button = L.Key("Record this call", variant="pri", size="sm", icon="rec", parent=self)
        self.no_button = L.Key("Not this call", size="sm", parent=self)
        self.always_button = L.Check("Always for this app", self)
        #: Kept for older callers: "never" is Not this call with the check on.
        self.never_button = L.Key("Never", variant="ghost", size="xs", parent=self)
        self.never_button.hide()
        acts.addWidget(self.yes_button)
        acts.addWidget(self.no_button)
        acts.addStretch(1)
        acts.addWidget(self.always_button)
        self.content.addLayout(acts)
        self.yes_button.clicked.connect(lambda: self._answer("always" if self.always_button.isChecked() else "yes"))
        self.no_button.clicked.connect(lambda: self._answer("never" if self.always_button.isChecked() else "no"))
        self.never_button.clicked.connect(lambda: self._answer("never"))
        self.x_button.clicked.connect(lambda: self._answer("no"))
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._answer("none"))

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.update()

    def set_call(self, call: dict[str, Any]) -> None:
        """Fill the card for one row (a call, a game's voice chat, or a game that already ended) and size it (no
        showing)."""
        self.call = call
        question, detail, note = consent_text(call)
        from yuki.ui.memory import local_moment

        app_name = str(call.get("app") or "")
        game = is_game(call)
        if game:
            self.title.setText(f"{app_name} voice chat" if app_name else "Game voice chat")
        else:
            self.title.setText(f"{app_name} started a call" if app_name else "A call started")
        moment = local_moment(call.get("detected_at"))
        self.when.setText(moment.strftime("%H:%M") if moment else "")
        ended = local_moment(call.get("ended_at")) if call.get("ended") else None
        when = f"{self.when.text()}–{ended:%H:%M}" if ended is not None and self.when.text() else self.when.text()
        self.set_head("call", " · ".join(x for x in (head_label(call), when) if x))
        yes, no = card_keys(call)
        self.yes_button.set_parts(yes)
        self.no_button.set_parts(no)
        self.question.set_text(question)
        self.detail.set_text(detail)
        self.note.set_text(note)
        self.note.setVisible(bool(note))
        self.note_gap.setVisible(bool(note))
        app = str(call.get("app") or "this app")
        self.always_button.setText(f"Always for {app}")
        self.always_button.setChecked(False)
        self.never_button.setText(f"Never for {app}")
        self.always_button.setVisible(bool(call.get("can_remember_choice", True)))
        self.fit_height()
        self.update()

    def anchor(self, *, above: int = 0) -> QPoint:  # noqa: D102 - top right of the monitor in use
        del above
        return LatticeCard.anchor(self)

    def show_call(self, call: dict[str, Any], *, above: int = 0) -> None:
        self.set_call(call)
        self.fade_in(self.anchor(above=above))
        try:
            timeout_ms = int(float(call.get("ask_timeout_s") or 120) * 1000)
        except (TypeError, ValueError):
            timeout_ms = 120_000
        self._timeout.start(timeout_ms)

    def withdraw(self) -> None:
        """Hide without an answer (memory stopped asking: the call ended, or it was answered elsewhere)."""
        self.call = None
        self._timeout.stop()
        self.fade_out()

    def _answer(self, answer: str) -> None:
        call, self.call = self.call, None
        self._timeout.stop()
        self.fade_out()
        if call is not None:
            self.answered.emit(call, answer)


class CallIndicator(SurfaceWindow):
    """The call pill (Horizon x Field Unit; the island stands in for it in the app): the accent recording dot, a
    small grey wave, the call's running time on a dot-matrix timer (37:12) and what is kept ("Recording · Discord +
    you"). Nothing kept while it waits for headphones: no dot, no timer, the words say it. Never takes focus.

    The timer ticks once a second and the wave moves only while the pill is shown (a full-screen game hides
    it, and then nothing runs). It counts from the call **session**'s start (``session_started_at``: the first
    row of the same microphone hold), so a row the service had to cut and reopen keeps one running time."""

    def __init__(self) -> None:
        from yuki.ui.wave import Wave
        from yuki.ui.window.theme import variable_font

        super().__init__(activates=False, radius=INDICATOR_HEIGHT // 2)
        self.setObjectName("YukiCallIndicator")
        self.setFixedSize(INDICATOR_WIDTH + 2 * self.SHADOW, INDICATOR_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 14, self.SHADOW, self.SHADOW + 18, self.SHADOW)
        row.setSpacing(11)
        self.dot = _RecDot(self)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.wave = Wave("call", theme=self.theme, size=(30, 14), parent=self)
        row.addWidget(self.wave, 0, Qt.AlignmentFlag.AlignVCenter)
        self.timer_label = _DotTimer(self)
        row.addWidget(self.timer_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel(self)
        self.label.setFont(variable_font(13.5, "Regular"))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.call: dict[str, Any] | None = None
        self._since: float | None = None
        #: Whether Yuki listens for its name now (v3.16, 04 F6): when not, the pill says so after its own words.
        self._listening = True
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_time)
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.label.setStyleSheet(f"color: {self.ink_css('text')};")
        self.dot.update()
        self.timer_label.update()

    def elapsed(self, now: float | None = None) -> str:
        """The call's running time: ``37:12`` (``1:02:05`` past an hour); ``""`` without a start."""
        import time as _time

        if self._since is None:
            return ""
        secs = max(0, int((now if now is not None else _time.time()) - self._since))
        h, rem = divmod(secs, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _update_time(self) -> None:
        self.timer_label.set_text(self.elapsed())
        self._fit()

    def _fit(self) -> None:
        fm = self.label.fontMetrics()
        waiting = _waiting_only(self.call or {})
        width = 14 + (0 if waiting else 8 + 11) + 30 + 11 + (0 if waiting else self.timer_label.width() + 11)
        width += fm.horizontalAdvance(self.label.text()) + 22
        width = max(INDICATOR_WIDTH, width)
        if width + 2 * self.SHADOW != self.width():
            self.setFixedSize(width + 2 * self.SHADOW, INDICATOR_HEIGHT + 2 * self.SHADOW)

    def set_listening(self, listening: bool) -> None:
        """Voice's listening state (v3.16): "Yuki isn't listening" after the pill's words while the name is not heard."""
        if bool(listening) == self._listening:
            return
        self._listening = bool(listening)
        if self.call is not None:
            self.label.setText(self._text(self.call))
            self._fit()
            self.update()

    def _text(self, call: dict[str, Any]) -> str:
        text = indicator_text(call)
        return text if self._listening else f"{text} · {NOT_LISTENING_TEXT}"

    def set_call(self, call: dict[str, Any]) -> None:
        from yuki.ui.memory import local_moment

        self.call = call
        self.label.setText(self._text(call))
        waiting = _waiting_only(call)
        # The session's start (05 §7.4): a call row the service had to cut and reopen keeps one running time.
        start = local_moment(call.get("session_started_at") or call.get("detected_at") or call.get("recording_since"))
        self._since = start.timestamp() if start is not None else None
        self.dot.setVisible(not waiting)
        self.timer_label.setVisible(not waiting and self._since is not None)
        self.timer_label.set_text(self.elapsed())
        self._fit()
        self.restyle()
        self.update()

    def showEvent(self, e: object) -> None:  # noqa: D102 - the timer runs only while shown
        super().showEvent(e)
        self._tick.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._tick.stop()

    def anchor(self, *, above: int = 0) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN - above
        return QPoint(x, y)


class _RecDot(QWidget):
    """The recording dot: the accent, 8 px."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(8, 8)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainter

        from yuki.ui.window.paint import paint_led

        p = QPainter(self)
        paint_led(p, QPointF(4, 4), self.window().theme().accent, 8)
        p.end()


class _DotTimer(QWidget):
    """The running time in Doto (the colon in Segoe UI Variable Display Light)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.text = ""
        self.setFixedHeight(20)
        self.set_text("00:00")

    def set_text(self, text: str) -> None:
        from yuki.ui.window.paint import dots_width

        if text != self.text:
            self.text = text
            self.setFixedWidth(int(dots_width(text or "00:00", 16)) + 2)
            self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        from yuki.ui.window.paint import paint_dots

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_dots(p, QRectF(self.rect()), self.text, 16, self.window().theme().text)
        p.end()


class CallController(QObject):
    """The call's card, indicator and Stop, driven by ``call_status()``.

    Args:
        memory: The shared memory access (``call_status``, ``answer_call``, ``stop_call``).
        ui_log: Where the ``call_*`` UI events go.
        covered: True while another corner surface is up (a nudge card): the
            indicator hides meanwhile.
        strip_up: True while the status strip is up: card and indicator sit above it.
        screen_state: ``(value, name)`` of the user's notification state.

    Signals:
        changed: ``(status dict)`` after every fetch (the tray enables Stop from it).
    """

    changed = Signal(object)
    _logged = Signal(str, object)

    def __init__(
        self, memory: MemoryAccess, ui_log: UiLog, *, covered: Callable[[], bool] = lambda: False,
        strip_up: Callable[[], bool] = lambda: False,
        screen_state: Callable[[], tuple[int, str]] = notification_state,
        event_name: str = CALL_EVENT_NAME, card: CallConsentCard | None = None,
        indicator: CallIndicator | None = None,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.ui_log = ui_log
        self._covered = covered
        self._strip_up = strip_up
        self._screen_state = screen_state
        self.card = card or CallConsentCard()
        self.indicator = indicator or CallIndicator()
        self.watcher = CallWatcher(memory, event_name=event_name)
        self.status: dict[str, Any] = {}
        #: Call ids answered (or timed out) here, so a late fetch never shows the card again.
        self._answered: set[Any] = set()
        self._threads: list[threading.Thread] = []
        self._logged.connect(lambda name, fields: self.ui_log.event(name, **(fields or {})))
        self.watcher.arrived.connect(self.offer)
        self.watcher.problem.connect(lambda what, error: self.ui_log.event("call_watch_error", what=what, error=error))
        self.card.answered.connect(self._on_answered)
        self._tick = QTimer(self)
        self._tick.setInterval(ACTIVE_POLL_MS)
        self._tick.timeout.connect(self.refresh)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if not self.memory.installed:
            return False
        started = self.watcher.start()
        self.ui_log.event("call_watch", state="started" if started else "failed", event_name=self.watcher.event_name)
        return started

    def stop(self) -> None:
        self._tick.stop()
        self.watcher.stop()
        self.card.hide()
        self.indicator.hide()

    def refresh(self) -> None:
        """Fetch now (also re-places the indicator)."""
        self.watcher.poke()

    @property
    def card_up(self) -> bool:
        return self.card.isVisible() and self.card.call is not None

    @property
    def recording(self) -> bool:
        """Stop must be reachable: anything is kept (the status's top-level ``recording``: the mic, a call app's own
        sound or a game's voice chat, even while the call row itself asks or waits), or the row may record (waiting
        for headphones)."""
        call = self.status.get("call") or {}
        return bool(self.status.get("recording")) or call.get("status") in RECORDING_STATUSES

    @property
    def view(self) -> dict[str, Any] | None:
        """What the indicators show (:func:`yuki.ui.call_words.live_view`), or None."""
        return live_view(self.status)

    # -- state -------------------------------------------------------------

    def _above(self) -> int:
        return (STRIP_HEIGHT + _GAP) if self._strip_up() else 0

    def offer(self, status: object, why: str = "") -> None:
        """A fresh ``call_status()``: show, update or take down the card and the indicator. The card is the live
        call's while it asks, else the card of a game session that ended before it could be shown (``pending_card``);
        a game still waiting to be seen (``ask_later``) never has one. Each is answered once, through
        ``answer_call``."""
        status = status if isinstance(status, dict) else {}
        view_before = live_view(self.status) or {}
        before = ((self.status.get("call") or {}).get("status"), view_before.get("line"))
        self.status = status
        call = status.get("call") or None
        view = live_view(status) or {}
        if ((call or {}).get("status"), view.get("line")) != before:
            self.ui_log.event("call_status", via=why, call_id=(call or {}).get("id"), app=(call or {}).get("app"),
                              status=(call or {}).get("status"), kind=(call or {}).get("kind"),
                              recording=bool(status.get("recording")), kept=list(view.get("kinds") or ()),
                              pending_card=(status.get("pending_card") or {}).get("id"))
        target = card_target(status)
        if target is not None and target.get("id") in self._answered:
            target = None
        shown = (self.card.call or {}).get("id") if self.card_up else None
        if target is not None and (not self.card_up or shown != target.get("id")):
            self.card.show_call(target, above=self._above())
            self.ui_log.event("call_consent_shown", call_id=target.get("id"), app=target.get("app"),
                              headphones=target.get("headphones"), kind=target.get("kind") or "call",
                              ended=bool(target.get("ended")), keeps_sound=bool(target.get("keeps_sound")))
        elif target is None and self.card_up:
            self.card.withdraw()
        self.place_indicator()
        if call or target is not None or status.get("recording"):
            self._tick.start()
        else:
            self._tick.stop()
        self.changed.emit(status)

    def place_indicator(self) -> None:
        call = self.view
        on = call is not None
        value, _name = self._screen_state()
        hidden = self.card_up or self._covered() or value in BUSY_NOTIFICATION_STATES
        if call is not None and on and not hidden:
            self.indicator.set_call(call)
            target = self.indicator.anchor(above=self._above())
            if not self.indicator.isVisible() or self.indicator.closing:
                self.indicator.fade_in(target)
            else:
                self.indicator.move(target)
        elif self.indicator.isVisible() and not self.indicator.closing:
            self.indicator.fade_out()

    # -- actions -------------------------------------------------------------

    def _background(self, name: str, work: Callable[[], Any]) -> threading.Thread:
        def run() -> None:
            fields: dict[str, Any] = {}
            try:
                fields["result"] = work()
                fields["ok"] = True
            except MemoryUnavailable as exc:
                fields.update(ok=False, error=str(exc))
            except Exception as exc:  # never take the UI down
                fields.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            self._logged.emit(name, fields)
            self.watcher.poke()

        thread = threading.Thread(target=run, name=f"yuki-ui-{name}", daemon=True)
        thread.start()
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        return thread

    def _on_answered(self, call: dict[str, Any], answer: str) -> None:
        """Every card's answer (a call's, a game's, a game's that already ended) goes the one way: ``answer_call``."""
        call_id = call.get("id")
        self._answered.add(call_id)
        self.ui_log.event("call_consent_answer", call_id=call_id, app=call.get("app"), answer=answer,
                          kind=call.get("kind") or "call", ended=bool(call.get("ended")))
        self._background("call_answer", lambda: self.memory.call_method(
            "answer_call", call_id, answer, timeout_s=TRAY_TIMEOUT_S))

    def stop_call(self) -> threading.Thread:
        """Stop remembering what records now (the tray's Pause for this call, the window's Stop): memory's
        ``stop_call`` stops every row recording, the microphone and every app's own sound."""
        self.ui_log.event("call_stop_requested", call_id=(self.status.get("call") or {}).get("id"),
                          recording=bool(self.status.get("recording")))
        return self._background("call_stop", lambda: self.memory.call_method("stop_call", timeout_s=TRAY_TIMEOUT_S))

    @property
    def waiting(self) -> str:
        """"N calls waiting to be remembered", or ``""``."""
        return waiting_text(self.status.get("waiting"))

    def erase_waiting(self) -> threading.Thread:
        """The tray's "Erase waiting call audio": the spool's audio is deleted, its words never made."""
        self.ui_log.event("call_audio_erase_requested", waiting=self.status.get("waiting"))
        return self._background("call_audio_erase",
                                lambda: self.memory.call_method("erase_call_audio", timeout_s=TRAY_TIMEOUT_S))

    def wait(self, timeout_s: float = 5.0) -> None:
        for thread in list(self._threads):
            thread.join(timeout_s)


__all__ = ["CallController", "CallConsentCard", "CallIndicator", "CallWatcher", "CALL_EVENT_NAME",
           "card_target", "consent_text", "indicator_text", "live_view", "waiting_text"]
