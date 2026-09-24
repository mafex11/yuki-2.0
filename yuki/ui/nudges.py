"""Nudge cards: memory's praise, nudges and reminders, bottom-right, never taking focus.

The memory service writes a nudge and sets the named, auto-reset Windows event
:data:`NUDGE_EVENT_NAME`. :class:`NudgeWatcher` waits on that event on its own
thread (and looks anyway every :data:`FALLBACK_POLL_S`, in case a signal was
missed or the service predates the event), fetches ``pending_nudges()`` and
hands the rows to the GUI thread.

:class:`NudgeController` keeps them in a queue and shows one at a time as a
:class:`NudgeCard`, but only when the screen is free. The UI says what "busy"
means (a request in flight in the open overlay, the task strip up, a reply to
a nudge being written), and Windows says whether the user is in something
full-screen, presenting, or away (``SHQueryUserNotificationState``). Anything
not shown stays queued until the screen is free.

Every outcome is acknowledged to memory exactly once: ``shown`` (the card
faded after :func:`linger_for` its text, or it was delivered into the open
overlay), ``dismissed``, ``replied`` or ``snoozed``. Memory decides what a
nudge says and when; nothing here reads its text.

While the overlay is open, nudges go into its conversation instead of the
corner (the ``in_overlay`` signal), and when it opens the recent ones
(:data:`OVERLAY_RECENT_HOURS`, via ``recent_nudges()``) come back as cards, so
a card that faded unread can be read again. Every delivered nudge is also
remembered per agent lane, so the next request to that lane carries a short
:func:`coach_note` of what Yuki's coach said (at most
:data:`COACH_NOTE_NUDGES`, each attached once per lane).
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QEnterEvent, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from yuki.agent.memory import TRAY_TIMEOUT_S, MemoryAccess, MemoryUnavailable
from yuki.ui.glass import ACCENT, TEXT_DIM, GlassWindow, ui_font
from yuki.ui.memory import local_moment
from yuki.ui.status import STRIP_MARGIN, STRIP_WIDTH
from yuki.ui.uilog import UiLog

#: The named event the memory service sets when it writes a nudge.
NUDGE_EVENT_NAME = "Local\\YukiNudgeReady"

#: Slow fallback look at ``pending_nudges()`` when the event stays quiet.
FALLBACK_POLL_S = 60.0

#: How long a card stays up untouched before it fades (counts as ``shown``):
#: a base, plus reading time per character of its text, capped (:func:`linger_for`).
CARD_LINGER_BASE_MS = 12_000
CARD_LINGER_PER_CHAR_MS = 60
CARD_LINGER_MAX_MS = 40_000

#: While something is queued and the screen is busy, look again this often.
#: (A full-screen app ending raises nothing Yuki can wait on.)
RETRY_MS = 15_000

#: Card size (without the shadow margin): the strip's width, a bit taller.
CARD_WIDTH = STRIP_WIDTH
CARD_MIN_HEIGHT = 96

#: When the overlay opens, nudges shown this long ago or less come back as cards...
OVERLAY_RECENT_HOURS = 12.0
#: ...the newest this many of them.
OVERLAY_NUDGES = 3

#: Typing in the overlay this soon after the newest unanswered nudge in it was
#: seen sends that nudge along as the context of the request (as Reply does).
IMPLICIT_REPLY_S = 30 * 60

#: Most nudges one request's coach note carries.
COACH_NOTE_NUDGES = 3
#: Guards on one note line (a nudge is one or two short sentences already).
COACH_NOTE_TEXT_CHARS = 400
COACH_NOTE_REASON_CHARS = 200

#: Reactions after which a nudge counts as answered (no implicit reply to it).
ANSWERED_REACTIONS: frozenset[str] = frozenset({"replied", "dismissed", "snoozed"})

#: Accent colour and heading per nudge kind.
KIND_ACCENTS: dict[str, QColor] = {
    "praise": QColor(118, 214, 158),
    "nudge": QColor(255, 190, 110),
    "reminder": ACCENT,
    "review": QColor(196, 160, 255),
}
KIND_TITLES: dict[str, str] = {
    "praise": "Praise", "nudge": "Nudge", "reminder": "Reminder", "review": "Weekly review",
}

#: ``QUERY_USER_NOTIFICATION_STATE`` values, and the ones that mean "not now".
NOTIFICATION_STATES: dict[int, str] = {
    1: "not_present",  # screen saver, locked, or fast user switching
    2: "busy",  # a full-screen app
    3: "d3d_full_screen",  # a full-screen Direct3D game
    4: "presentation_mode",
    5: "accepts_notifications",
    6: "quiet_time",
    7: "app",
}
BUSY_NOTIFICATION_STATES: frozenset[int] = frozenset({1, 2, 3, 4})

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF


def kind_accent(kind: Any) -> QColor:
    """The accent colour for a nudge kind (an unknown kind gets the default accent)."""
    return KIND_ACCENTS.get(str(kind or ""), ACCENT)


def kind_title(kind: Any) -> str:
    """The small heading for a nudge kind."""
    return KIND_TITLES.get(str(kind or ""), "Yuki")


def notification_state() -> tuple[int, str]:
    """``SHQueryUserNotificationState``: ``(value, name)``; ``(0, "error ...")`` when it fails."""
    try:
        shell32 = ctypes.windll.shell32
        query = shell32.SHQueryUserNotificationState
        query.argtypes = [ctypes.POINTER(ctypes.c_int)]
        query.restype = ctypes.c_long
        state = ctypes.c_int(0)
        result = int(query(ctypes.byref(state)))
    except (AttributeError, OSError) as exc:
        return 0, f"error {type(exc).__name__}"
    if result != 0:
        return 0, f"error 0x{result & 0xFFFFFFFF:08x}"
    return state.value, NOTIFICATION_STATES.get(state.value, "unknown")


def when_label(value: Any) -> str:
    """``14:05`` for today, ``Tue 14:05`` otherwise; ``""`` when it cannot be read."""
    moment = local_moment(value)
    if moment is None:
        return ""
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%a %H:%M")


def nudge_reply_request(nudge: dict[str, Any], reply: str, *, certain: bool = True) -> str:
    """The request a reply to a nudge becomes: the nudge as context, then the user's words.

    Args:
        nudge: The ``pending_nudges()`` row the user replied to.
        reply: What they typed.
        certain: ``True`` when the user pressed Reply on it. ``False`` when they
            only typed soon after it: then the note says they *may* be answering
            it, and the model judges whether the words are about the nudge.
    """
    kind = str(nudge.get("kind") or "nudge")
    sent = when_label(nudge.get("at"))
    text = " ".join(str(nudge.get("text") or "").split())
    lead = "Replying to your nudge" if certain else "The user may be answering your recent nudge, or may be asking something else"
    head = f'[{lead} (kind: {kind}){f", sent {sent}" if sent else ""}: "{text}"'
    reason = " ".join(str(nudge.get("reason") or "").split())
    if reason:
        head += f" -- why you sent it: {reason}"
    return f"{head}]\n{reply}"


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def linger_for(text: Any) -> int:
    """How long a card with ``text`` stays up untouched, in ms.

    :data:`CARD_LINGER_BASE_MS` plus :data:`CARD_LINGER_PER_CHAR_MS` per
    character (whitespace squashed), at most :data:`CARD_LINGER_MAX_MS`.
    """
    chars = len(_squash(text))
    return min(CARD_LINGER_MAX_MS, CARD_LINGER_BASE_MS + CARD_LINGER_PER_CHAR_MS * chars)


def _epoch(value: Any) -> float | None:
    moment = local_moment(value)
    return moment.timestamp() if moment is not None else None


def nudge_moment(nudge: dict[str, Any]) -> float | None:
    """When the user saw ``nudge`` (epoch s): delivered this run, else memory's ``shown_at``, else written."""
    for key in ("_seen_at", "shown_at", "at"):
        at = _epoch(nudge.get(key))
        if at is not None:
            return at
    return None


def nudge_answered(nudge: dict[str, Any]) -> bool:
    """True once the user replied to, dismissed or snoozed ``nudge``."""
    return str(nudge.get("reaction") or "") in ANSWERED_REACTIONS


def overlay_label(nudge: dict[str, Any]) -> str:
    """The small label over a nudge in the overlay: ``Yuki · 14:32 · nudge``."""
    parts = ["Yuki", when_label(nudge_moment(nudge)), str(nudge.get("kind") or "nudge")]
    return " · ".join(part for part in parts if part)


def coach_note(nudges: list[dict[str, Any]]) -> str:
    """The attached-context note telling an agent what its coach said, one line per nudge.

    ``[Yuki's coach said at 14:32 (nudge): "..." -- why: ...]``. The words are
    the nudge's own; this only frames them as something Yuki already said.
    """
    lines = []
    for nudge in nudges:
        text = _clip(_squash(nudge.get("text")), COACH_NOTE_TEXT_CHARS)
        if not text:
            continue
        sent = when_label(nudge.get("at"))
        head = f"[Yuki's coach said{f' at {sent}' if sent else ''} ({nudge.get('kind') or 'nudge'}): \"{text}\""
        reason = _clip(_squash(nudge.get("reason")), COACH_NOTE_REASON_CHARS)
        if reason:
            head += f" -- why: {reason}"
        lines.append(head + "]")
    return "\n".join(lines)


def _nudge_id(nudge: dict[str, Any]) -> str:
    return str(nudge.get("id"))


class NudgeWatcher(QObject):
    """Waits for the memory service's signal and fetches ``pending_nudges()``.

    Args:
        memory: The shared memory access.
        event_name: The named event to wait on.
        poll_s: The fallback look interval.

    Signals:
        arrived: ``(rows, why)`` -- ``why`` is ``start``, ``event``, ``poke`` or
            ``poll``. Emitted from the watcher thread (queued to the GUI thread).
        problem: ``(what, error)`` for the log, only when the error changes.
    """

    arrived = Signal(object, str)
    problem = Signal(str, str)

    def __init__(
        self,
        memory: MemoryAccess,
        *,
        event_name: str = NUDGE_EVENT_NAME,
        poll_s: float = FALLBACK_POLL_S,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.event_name = event_name
        self.poll_s = poll_s
        self._thread: threading.Thread | None = None
        self._kernel32: Any = None
        self._stop: int = 0
        self._wake: int = 0
        self._ready: int = 0
        self._last_error: str | None = None

    def _bind(self) -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32

    @property
    def running(self) -> bool:
        """True while the watcher thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Open the events and start the thread. ``False`` (and a ``problem``) when it cannot."""
        if self.running:
            return True
        try:
            self._kernel32 = self._bind()
            self._stop = int(self._kernel32.CreateEventW(None, True, False, None) or 0)
            self._wake = int(self._kernel32.CreateEventW(None, False, False, None) or 0)
            # Create-or-open: whichever of Yuki and the memory service comes first
            # creates it; auto-reset, so one SetEvent wakes this one waiter once.
            self._ready = int(self._kernel32.CreateEventW(None, False, False, self.event_name) or 0)
        except (AttributeError, OSError) as exc:
            self.problem.emit("events", f"{type(exc).__name__}: {exc}")
            return False
        if not (self._stop and self._wake):
            self.problem.emit("events", f"CreateEventW failed ({ctypes.get_last_error()})")
            return False
        if not self._ready:
            # Still useful: the fallback poll alone keeps nudges arriving.
            self.problem.emit(
                "event", f"could not open {self.event_name} ({ctypes.get_last_error()}); polling only"
            )
        self._thread = threading.Thread(target=self._run, name="yuki-ui-nudges", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 2.0) -> None:
        """End the thread (bounded) and close the handles."""
        if self._kernel32 is None:
            return
        if self._stop:
            self._kernel32.SetEvent(self._stop)
        if self._thread is not None:
            self._thread.join(timeout_s)
        if not self.running:
            for handle in (self._ready, self._wake, self._stop):
                if handle:
                    self._kernel32.CloseHandle(handle)
            self._ready = self._wake = self._stop = 0
            self._thread = None

    def poke(self) -> None:
        """Fetch now (e.g. after nudges are switched back on)."""
        if self._kernel32 is not None and self._wake:
            self._kernel32.SetEvent(self._wake)

    def _run(self) -> None:
        kernel32 = self._kernel32
        handles = [self._stop, self._wake] + ([self._ready] if self._ready else [])
        array = (wintypes.HANDLE * len(handles))(*handles)
        timeout_ms = int(self.poll_s * 1000)
        why = "start"
        while True:
            self._fetch(why)
            result = int(kernel32.WaitForMultipleObjects(len(handles), array, False, timeout_ms))
            if result == _WAIT_OBJECT_0:
                return
            if result == _WAIT_OBJECT_0 + 1:
                why = "poke"
            elif result == _WAIT_OBJECT_0 + 2:
                why = "event"
            elif result == _WAIT_TIMEOUT:
                why = "poll"
            else:
                self.problem.emit("wait", f"WaitForMultipleObjects returned {result:#x} ({ctypes.get_last_error()})")
                # Never spin: fall back to waiting on the stop event alone.
                if int(kernel32.WaitForSingleObject(self._stop, timeout_ms)) == _WAIT_OBJECT_0:
                    return
                why = "poll"

    def _fetch(self, why: str) -> None:
        try:
            rows = self.memory.pending_nudges()
        except MemoryUnavailable as exc:
            error = str(exc)
            if error != self._last_error:
                self._last_error = error
                self.problem.emit("pending_nudges", error)
            return
        except Exception as exc:  # the watcher must outlive anything memory does
            error = f"{type(exc).__name__}: {exc}"
            if error != self._last_error:
                self._last_error = error
                self.problem.emit("pending_nudges", error)
            return
        self._last_error = None
        self.arrived.emit(rows, why)


class NudgeCard(GlassWindow):
    """One nudge, bottom-right: heading, text, Reply and Dismiss. Never takes focus.

    The whole text always shows (wrapped, never cut). It stays up for
    :func:`linger_for` its text; the pointer on it stops the clock, and
    leaving starts the full linger again.

    Args:
        linger_ms: A fixed linger for every nudge (offline checks); ``None``
            (default) sizes it to each nudge's text.

    Signals:
        replied: ``(nudge)`` -- the user pressed Reply.
        dismissed: ``(nudge)`` -- the user pressed Dismiss.
        expired: ``(nudge)`` -- it faded on its own after :attr:`linger_ms`.
    """

    replied = Signal(object)
    dismissed = Signal(object)
    expired = Signal(object)

    def __init__(self, *, linger_ms: int | None = None) -> None:
        super().__init__(activates=False, radius=14)
        self.setObjectName("YukiNudgeCard")
        self.nudge: dict[str, Any] | None = None
        self._accent = ACCENT
        self._fixed_linger = linger_ms
        #: The linger of the nudge now up (set by :meth:`set_nudge`).
        self.linger_ms = linger_ms if linger_ms is not None else CARD_LINGER_BASE_MS
        self.setFixedWidth(CARD_WIDTH + 2 * self.SHADOW)

        column = QVBoxLayout(self)
        column.setContentsMargins(self.SHADOW + 18, self.SHADOW + 12, self.SHADOW + 14, self.SHADOW + 10)
        column.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = QLabel(self)
        self.title.setFont(ui_font(9, weight=QFont.Weight.DemiBold))
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.title)
        self.when = QLabel(self)
        self.when.setFont(ui_font(9))
        self.when.setTextFormat(Qt.TextFormat.PlainText)
        self.when.setStyleSheet(f"color: rgba(238,240,245,{TEXT_DIM.alpha()});")
        header.addWidget(self.when)
        header.addStretch(1)
        column.addLayout(header)

        self.body = QLabel(self)
        self.body.setFont(ui_font(11))
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setStyleSheet("color: rgba(238,240,245,240);")
        column.addWidget(self.body)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self.dismiss_button = self._button("Dismiss")
        self.reply_button = self._button("Reply")
        self.dismiss_button.clicked.connect(self._on_dismiss)
        self.reply_button.clicked.connect(self._on_reply)
        buttons.addWidget(self.dismiss_button)
        buttons.addWidget(self.reply_button)
        column.addLayout(buttons)

        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self._on_expire)
        #: True while the pointer is on the card and its clock is stopped.
        self._paused = False

    def _button(self, text: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setFont(ui_font(9))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # The card never takes the keyboard; nor may its buttons.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _style_buttons(self) -> None:
        a = self._accent
        self.reply_button.setStyleSheet(
            f"QPushButton {{ color: rgb({a.red()},{a.green()},{a.blue()});"
            f" background: rgba({a.red()},{a.green()},{a.blue()},34);"
            f" border: 1px solid rgba({a.red()},{a.green()},{a.blue()},90);"
            " border-radius: 8px; padding: 3px 12px; }"
            f"QPushButton:hover {{ background: rgba({a.red()},{a.green()},{a.blue()},64); }}"
        )
        self.dismiss_button.setStyleSheet(
            "QPushButton { color: rgba(238,240,245,170); background: transparent;"
            " border: 1px solid rgba(255,255,255,26); border-radius: 8px; padding: 3px 12px; }"
            "QPushButton:hover { color: rgba(238,240,245,240); background: rgba(255,255,255,18); }"
        )
        self.title.setStyleSheet(f"color: rgb({a.red()},{a.green()},{a.blue()});")

    # -- content -----------------------------------------------------------

    def set_nudge(self, nudge: dict[str, Any]) -> None:
        """Fill the card for one nudge and size it to its text (no showing)."""
        self.nudge = nudge
        self._accent = kind_accent(nudge.get("kind"))
        self.title.setText(kind_title(nudge.get("kind")))
        self.when.setText(when_label(nudge.get("at")))
        text = _squash(nudge.get("text"))
        self.body.setText(text)  # all of it: word-wrapped, the card grows to fit
        self.linger_ms = self._fixed_linger if self._fixed_linger is not None else linger_for(text)
        self._style_buttons()
        width = CARD_WIDTH + 2 * self.SHADOW
        wanted = self.layout().totalHeightForWidth(width) if self.layout().hasHeightForWidth() else (
            self.layout().sizeHint().height()
        )
        self.setFixedHeight(max(CARD_MIN_HEIGHT + 2 * self.SHADOW, wanted))
        self._shadow_pixmap = None
        self.update()

    def anchor(self) -> QPoint:
        """Top-left to sit at: bottom-right of the primary screen's work area, like the strip."""
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN
        return QPoint(x, y)

    def show_nudge(self, nudge: dict[str, Any]) -> None:
        """Show one nudge, without activating, and start its linger timer."""
        self.set_nudge(nudge)
        self._paused = False
        self.fade_in(self.anchor())
        self._linger.start(self.linger_ms)

    def withdraw(self) -> dict[str, Any] | None:
        """Hide at once without an outcome (to make way for the strip). Returns the nudge."""
        nudge, self.nudge = self.nudge, None
        self._linger.stop()
        self._anim.stop()
        self._closing = False
        self.hide()
        return nudge

    def close_card(self) -> dict[str, Any] | None:
        """Fade away without an outcome of its own (the caller records one). Returns the nudge."""
        nudge, self.nudge = self.nudge, None
        self._linger.stop()
        self.fade_out()
        return nudge

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        panel = QRectF(self.panel_rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._accent)
        painter.drawRoundedRect(QRectF(panel.left() + 7, panel.top() + 12, 3, panel.height() - 24), 1.5, 1.5)

    # -- pointer: reading it pauses the fade, leaving starts it over --------

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: D102 - Qt override
        super().enterEvent(event)
        if self._linger.isActive():
            self._paused = True
            self._linger.stop()

    def leaveEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().leaveEvent(event)
        if self._paused and self.nudge is not None:
            self._linger.start(self.linger_ms)
        self._paused = False

    # -- outcomes ----------------------------------------------------------

    def _on_expire(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.expired.emit(nudge)

    def _on_dismiss(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.dismissed.emit(nudge)

    def _on_reply(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.replied.emit(nudge)


class NudgeController(QObject):
    """The nudge queue: what arrives, when it may show, and every acknowledgement.

    Args:
        memory: The shared memory access (``pending_nudges``, ``ack_nudge``,
            ``snooze_nudges``).
        ui_log: Where the ``nudge_*`` and ``snooze`` events go.
        blocked: Returns why the UI is busy right now (a short reason), or
            ``None`` when a card may show.
        inline: True while the overlay is open and can take a nudge into its
            conversation (``in_overlay``) instead of the corner card.
        screen_state: ``(value, name)`` of the user's notification state;
            :func:`notification_state` by default.
        event_name: The named event the watcher waits on.
        card: The card window (built here when omitted).

    Signals:
        reply_requested: ``(nudge)`` -- open the overlay with it as context.
        in_overlay: ``(nudge, via)`` -- put this nudge in the open overlay
            (already acknowledged ``shown``); ``via`` is ``arrived`` or
            ``card_moved``.
        recent_ready: ``(rows)`` -- :meth:`fetch_recent`'s answer, oldest first.
    """

    reply_requested = Signal(object)
    in_overlay = Signal(object, str)
    recent_ready = Signal(object)
    #: ``(event name, fields)`` from worker threads, written on the GUI thread.
    _logged = Signal(str, object)

    def __init__(
        self,
        memory: MemoryAccess,
        ui_log: UiLog,
        *,
        blocked: Callable[[], str | None] = lambda: None,
        inline: Callable[[], bool] = lambda: False,
        screen_state: Callable[[], tuple[int, str]] = notification_state,
        event_name: str = NUDGE_EVENT_NAME,
        poll_s: float = FALLBACK_POLL_S,
        card: NudgeCard | None = None,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.ui_log = ui_log
        self._blocked = blocked
        self._inline = inline
        self._screen_state = screen_state
        self.card = card or NudgeCard()
        self.watcher = NudgeWatcher(memory, event_name=event_name, poll_s=poll_s)
        #: Waiting to be shown, oldest first.
        self.queue: list[dict[str, Any]] = []
        #: Every nudge id seen this run (queued, shown or acknowledged).
        self.known: set[str] = set()
        #: Nudges the user has been shown (card or overlay), by id, in delivery
        #: order. The dicts are shared with the overlay's cards, and carry
        #: ``_seen_at`` (epoch) and the latest ``reaction``.
        self.delivered: dict[str, dict[str, Any]] = {}
        #: lane -> ids already told to that lane's agent (coach note or reply prefix).
        self._attached: dict[str, set[str]] = {}
        #: The last deferral reason logged, so a busy screen is logged once.
        self._deferred: str | None = None
        self._ack_threads: list[threading.Thread] = []

        self._logged.connect(self._write_log)
        self.watcher.arrived.connect(self.offer)
        self.watcher.problem.connect(self._on_watch_problem)
        self.card.expired.connect(self._on_expired)
        self.card.dismissed.connect(self._on_dismissed)
        self.card.replied.connect(self._on_replied)
        self.card.faded_out.connect(self.try_show)
        self._retry = QTimer(self)
        self._retry.setInterval(RETRY_MS)
        self._retry.timeout.connect(self.try_show)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Start watching (nothing to watch when memory is not installed)."""
        if not self.memory.installed:
            self.ui_log.event("nudge_watch", state="memory_not_installed")
            return False
        started = self.watcher.start()
        self.ui_log.event(
            "nudge_watch", state="started" if started else "failed",
            event_name=self.watcher.event_name, poll_s=self.watcher.poll_s,
        )
        return started

    def stop(self) -> None:
        """Stop the watcher and hide the card (a card up at exit gets no outcome)."""
        self._retry.stop()
        self.watcher.stop()
        self.card.withdraw()

    def _write_log(self, name: str, fields: object) -> None:
        self.ui_log.event(name, **(fields if isinstance(fields, dict) else {}))

    def _on_watch_problem(self, what: str, error: str) -> None:
        self.ui_log.event("nudge_watch_error", what=what, error=error)

    # -- arrivals and showing ----------------------------------------------

    def offer(self, rows: object, why: str = "") -> None:
        """Queue the nudges not seen before, then show one if the screen is free."""
        fresh = [
            row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("id") is not None and _nudge_id(row) not in self.known
        ]
        for row in fresh:
            self.known.add(_nudge_id(row))
            self.queue.append(row)
            self.ui_log.event(
                "nudge_received", id=row.get("id"), kind=row.get("kind"), via=why,
                queued=len(self.queue), text=row.get("text"), reason=row.get("reason"),
            )
        if self.queue:
            self.try_show()

    @property
    def showing(self) -> dict[str, Any] | None:
        """The nudge on screen, if any."""
        return self.card.nudge if self.card.isVisible() else None

    def busy_reason(self) -> str | None:
        """Why a card may not show now: the UI's own reason, else a busy screen, else ``None``."""
        reason = self._blocked()
        if reason:
            return reason
        value, name = self._screen_state()
        if value in BUSY_NOTIFICATION_STATES:
            return f"screen_{name}"
        return None

    def try_show(self) -> None:
        """Show the next queued nudge if nothing is up and the screen is free.

        While the overlay is open (``inline``), everything queued goes into it
        instead, and a corner card still up moves there too.
        """
        if self._inline():
            self.bring_into_overlay()
        if not self.queue:
            self._retry.stop()
            self._deferred = None
            return
        if self.card.isVisible():
            return
        reason = self.busy_reason()
        if reason:
            if reason != self._deferred:
                self._deferred = reason
                self.ui_log.event("nudge_deferred", reason=reason, queued=len(self.queue))
            if not self._retry.isActive():
                self._retry.start()
            return
        self._deferred = None
        nudge = self.record_delivery(self.queue.pop(0), "card")
        self.card.show_nudge(nudge)
        self.ui_log.event(
            "nudge_shown", id=nudge.get("id"), kind=nudge.get("kind"), text=nudge.get("text"),
            queued=len(self.queue), linger_ms=self.card.linger_ms,
        )
        if not self.queue:
            self._retry.stop()

    def bring_into_overlay(self) -> list[dict[str, Any]]:
        """Hand the corner card (if up) and everything queued to the open overlay.

        Each goes out on ``in_overlay`` and is acknowledged ``shown`` (it is on
        screen now). Returns them, oldest first.
        """
        moved: list[tuple[dict[str, Any], str]] = []
        if self.card.isVisible() and self.card.nudge is not None:
            up = self.card.close_card()
            if up is not None:
                moved.append((up, "card_moved"))
        moved.extend((nudge, "arrived") for nudge in self.queue)
        self.queue = []
        self._retry.stop()
        self._deferred = None
        out = []
        for nudge, via in moved:
            stored = self.record_delivery(nudge, "overlay")
            self.ack(stored, "shown")
            self.in_overlay.emit(stored, via)
            out.append(stored)
        return out

    # -- what the user has seen, and what each agent has been told ---------

    def record_delivery(
        self, nudge: dict[str, Any], via: str, *, seen_at: float | None = None
    ) -> dict[str, Any]:
        """Remember that the user saw ``nudge``; returns the shared dict for it.

        Args:
            nudge: The row (``pending_nudges()`` or ``recent_nudges()``).
            via: ``card``, ``overlay`` or ``recent`` (for the dict only).
            seen_at: When it was seen (epoch); now by default. Kept from the
                first delivery.
        """
        key = _nudge_id(nudge)
        stored = self.delivered.get(key)
        if stored is None:
            stored = dict(nudge)
            stored["_seen_at"] = seen_at if seen_at is not None else time.time()
            stored["_via"] = via
            self.delivered[key] = stored
        else:
            for field in ("text", "reason", "kind", "at", "shown_at"):
                if nudge.get(field) is not None and stored.get(field) is None:
                    stored[field] = nudge[field]
            # memory's answer may be newer than ours (another reaction elsewhere)
            if nudge.get("reaction") and not nudge_answered(stored):
                stored["reaction"] = nudge["reaction"]
        self.known.add(key)
        return stored

    def _set_reaction(self, nudge_id: Any, reaction: str) -> None:
        """Mirror memory's rule locally: ``shown`` never replaces a real reaction."""
        stored = self.delivered.get(str(nudge_id))
        if stored is None:
            return
        if reaction != "shown" or not stored.get("reaction"):
            stored["reaction"] = reaction

    def coach_note_for(
        self, lane: str, *, exclude: Any = None, limit: int = COACH_NOTE_NUDGES
    ) -> tuple[str, list[Any]]:
        """The coach note for the next request to ``lane``, and the nudge ids in it.

        The newest ``limit`` delivered nudges that lane's agent has not been
        told about yet (``exclude``: one the request already carries as its
        reply context). Nothing is marked; call :meth:`mark_attached` once the
        request went out.
        """
        told = self._attached.get(lane, set())
        fresh = [
            n for key, n in self.delivered.items()
            if key not in told and key != (None if exclude is None else str(exclude))
        ]
        fresh.sort(key=lambda n: nudge_moment(n) or 0.0)
        chosen = fresh[-limit:] if limit > 0 else []
        return coach_note(chosen), [n.get("id") for n in chosen]

    def mark_attached(self, lane: str, ids: list[Any]) -> None:
        """``lane``'s agent has now been told about these nudges."""
        self._attached.setdefault(lane, set()).update(str(i) for i in ids)

    def fetch_recent(self, hours: float = OVERLAY_RECENT_HOURS) -> threading.Thread:
        """Fetch the nudges shown in the last ``hours`` off the GUI thread; answers on ``recent_ready``.

        Memory's ``recent_nudges()`` merged with what this run delivered (so a
        memory without that method, or one that is down, still gives this
        run's). Oldest first. Logs ``nudge_recent``.
        """
        cutoff = time.time() - hours * 3600.0
        local = [dict(n) for n in self.delivered.values() if (nudge_moment(n) or 0.0) >= cutoff]

        def work() -> None:
            rows: list[dict[str, Any]] = []
            error = None
            try:
                value = self.memory.call_method("recent_nudges", hours=hours, timeout_s=TRAY_TIMEOUT_S)
                rows = [dict(r) for r in (value or []) if isinstance(r, dict) and r.get("id") is not None]
            except MemoryUnavailable as exc:
                error = str(exc)
            except Exception as exc:  # never take the UI down
                error = f"{type(exc).__name__}: {exc}"
            merged: dict[str, dict[str, Any]] = {_nudge_id(r): r for r in rows}
            for n in local:
                row = merged.setdefault(_nudge_id(n), {})
                row.update({k: v for k, v in n.items() if v is not None})
            out = sorted(merged.values(), key=lambda n: nudge_moment(n) or 0.0)
            self._logged.emit(
                "nudge_recent",
                {"hours": hours, "from_memory": len(rows), "from_run": len(local), "total": len(out),
                 **({"error": error} if error else {})},
            )
            self.recent_ready.emit(out)

        thread = threading.Thread(target=work, name="yuki-ui-nudge-recent", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread

    def make_way(self) -> None:
        """The strip is about to take the corner: put a card that is up back at the head of the queue."""
        if not self.card.isVisible():
            return
        nudge = self.card.withdraw()
        if nudge is None:
            return
        self.queue.insert(0, nudge)
        self.ui_log.event("nudge_withdrawn", id=nudge.get("id"), reason="status_strip")
        if not self._retry.isActive():
            self._retry.start()

    # -- outcomes ----------------------------------------------------------

    def _on_expired(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_expired", id=nudge.get("id"), after_ms=self.card.linger_ms)
        self.ack(nudge, "shown")

    def _on_dismissed(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_dismissed", id=nudge.get("id"), via="card")
        self.ack(nudge, "dismissed")

    def _on_replied(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_reply_opened", id=nudge.get("id"))
        self.reply_requested.emit(nudge)

    def reply_sent(self, nudge: dict[str, Any], text: str, request_id: int, lane: str) -> None:
        """The user sent their reply to ``nudge`` (it went to ``runtime.submit``)."""
        self.ui_log.event(
            "nudge_replied", id=nudge.get("id"), request_id=request_id, lane=lane, text=text
        )
        self.ack(nudge, "replied")

    def reply_abandoned(self, nudge: dict[str, Any]) -> None:
        """The overlay closed before the user sent a reply to ``nudge``."""
        self.ui_log.event("nudge_dismissed", id=nudge.get("id"), via="overlay")
        self.ack(nudge, "dismissed")
        self.try_show()

    def ack(self, nudge: dict[str, Any], reaction: str) -> threading.Thread:
        """``ack_nudge(id, reaction)`` off the GUI thread; logs ``nudge_ack``."""
        nudge_id = nudge.get("id")
        self._set_reaction(nudge_id, reaction)

        def work() -> None:
            try:
                self.memory.ack_nudge(nudge_id, reaction)
                self._logged.emit("nudge_ack", {"id": nudge_id, "reaction": reaction, "ok": True})
            except (MemoryUnavailable, ValueError) as exc:
                self._logged.emit(
                    "nudge_ack", {"id": nudge_id, "reaction": reaction, "ok": False, "error": str(exc)}
                )
            except Exception as exc:  # an acknowledgement must never take the UI down
                self._logged.emit(
                    "nudge_ack",
                    {"id": nudge_id, "reaction": reaction, "ok": False, "error": f"{type(exc).__name__}: {exc}"},
                )

        thread = threading.Thread(target=work, name="yuki-ui-nudge-ack", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread

    def wait_acks(self, timeout_s: float = 5.0) -> None:
        """Wait (bounded) for acknowledgements in flight; for offline checks and exit."""
        for thread in list(self._ack_threads):
            thread.join(timeout_s)

    # -- quiet -------------------------------------------------------------

    def snooze(self, minutes: int, *, on_done: Callable[[], Any] | None = None) -> threading.Thread:
        """Quiet nudges for ``minutes`` (``0``: back on), in memory and on screen.

        A card that is up and everything queued are acknowledged ``snoozed``
        and dropped. Logs ``snooze``.
        """
        minutes = max(0, int(minutes))
        dropped: list[dict[str, Any]] = []
        if minutes > 0:
            up = self.card.close_card() if self.card.isVisible() else None
            dropped = ([up] if up else []) + self.queue
            self.queue = []
            self._retry.stop()
            for nudge in dropped:
                self.ack(nudge, "snoozed")

        def work() -> None:
            fields: dict[str, Any] = {
                "minutes": minutes,
                "dropped": [n.get("id") for n in dropped],
            }
            try:
                self.memory.snooze_nudges(minutes)
                fields["ok"] = True
            except MemoryUnavailable as exc:
                fields.update(ok=False, error=str(exc))
            except Exception as exc:  # never take the UI down
                fields.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            self._logged.emit("snooze", fields)
            if minutes == 0:
                self.watcher.poke()
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

        thread = threading.Thread(target=work, name="yuki-ui-nudge-snooze", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread
