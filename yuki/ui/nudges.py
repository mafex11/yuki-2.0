"""Nudge cards: memory's praise, nudges, reminders, reviews and "welcome back" messages, bottom-right, never taking focus.

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

A card that carries an offer (``offer``: a **typed** offer of
``yuki.memory.offers`` on an observation card or the welcome back) shows what it
will do (**Will:** its steps; **Won't:** a fixed line per kind), a button named
for the action, **Not now** and **Less of these** (also on a welcome back). The
button never starts the agent (2026-10-03; the user: "that is my work, I'll do
it"): memory checks the offer again and runs it by code (``offer_act``: add to
the list, show what Yuki holds, open again a page the user had, a draft to copy,
or a suggestion put unsent in the ask bar, ``ask_requested``), and the card shows
the result in place, with **Undo** where it applies. A second press, an expired
offer (30 minutes) or a ref that no longer resolves runs nothing. Not now is
``dismissed``; Less of these is ``less`` (feedback on that kind of card: fewer of
them, and that kind of offer counts down).

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

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QEnterEvent, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from yuki.agent.memory import TRAY_TIMEOUT_S, MemoryAccess, MemoryUnavailable
from yuki.ui.surface import SurfaceWindow
from yuki.ui.memory import local_moment
from yuki.ui.status import STRIP_MARGIN, STRIP_WIDTH
from yuki.ui.uilog import UiLog
# What a card says and offers, as plain data (no Qt): shared with the HTML window's process (re-exported here).
from yuki.ui.cards import (  # noqa: F401 - re-exported: chatview, window_host and tests import them from here
    KIND_TITLES, NOTICE_TITLES, OFFER_DOS, OFFER_FAILED, card_offer, card_title, kind_title, offer_expired,
    offer_label, offer_lines, offer_open,
)

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
ANSWERED_REACTIONS: frozenset[str] = frozenset({"replied", "dismissed", "snoozed", "accepted", "less"})

#: A card with an offer: not now, fewer of this kind (observation cards and the welcome back). The action
#: button is named for the action (the offer's own label), never "Do it".
OFFER_BUTTONS: tuple[str, str] = ("Not now", "Less of these")
#: Card kinds with "Less of these" (feedback on that kind; 06 §4.5 gave the welcome back one too).
LESS_KINDS: frozenset[str] = frozenset({"notice", "resume"})
#: A card with an offer stays up this much longer (there is something to decide).
OFFER_LINGER_EXTRA_MS = 6_000
#: After its offer ran, a card shows the result (and Undo) this long, then fades.
RESULT_LINGER_MS = 8_000
#: A card that expanded something to read (a draft, what Yuki holds) stays this long untouched.
EXPANDED_LINGER_MS = 45_000
#: The opening of a request sent from a card's suggestion unchanged: the words are Yuki's, the user sent them
#: (memory never stores them as the user's words: ``yuki.memory.conversations.YUKI_WORDS_MARKS``).
SUGGESTION_MARK = "[Yuki's own suggestion"
#: A question card's buttons: answer it (opens the overlay), not now, don't ask about this.
QUESTION_BUTTONS: tuple[str, str, str] = ("Answer", "Skip", "Don't ask")

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


def suggestion_request(nudge: dict[str, Any], text: str) -> str:
    """The request a card's suggestion becomes when the user sends it from the ask bar unchanged: marked as
    Yuki's words (the user read and sent them), so memory never stores them as the user's own."""
    return f"{SUGGESTION_MARK} from its card N{nudge.get('id')}, sent unchanged by the user]\n{text}"


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
    ref = " ".join(str(nudge.get("ref") or "").split())
    ident = f", id {ref}" if ref and kind == "question" else ""
    head = f'[{lead} (kind: {kind}{ident}){f", sent {sent}" if sent else ""}: "{text}"'
    reason = " ".join(str(nudge.get("reason") or "").split())
    if reason:
        head += f" -- why you sent it: {reason}"
    return f"{head}]\n{reply}"


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_question(text: str) -> tuple[str, str]:
    """A question card's text as (the question, what it rests on): the words up to the first question mark
    lead, bold and bright; the rest reads under it, small and dim. Presentation only: the whole text shows
    either way, and a text with no question mark before its end stays one piece."""
    text = " ".join((text or "").split())
    i = text.find("?")
    if 0 < i < len(text) - 1:
        return text[: i + 1], text[i + 1:].strip()
    return text, ""


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
    """The small label over a nudge in the overlay: ``Yuki, 14:32, nudge``."""
    parts = ["Yuki", when_label(nudge_moment(nudge)), str(nudge.get("kind") or "nudge")]
    return ", ".join(part for part in parts if part)


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


def _foreground_hwnd() -> int | None:
    """The window in front now (what a ``bring_back`` Undo brings back); None when it cannot be read."""
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(user32.GetForegroundWindow() or 0) or None
    except Exception:
        return None


def _open_page(url: str) -> bool:
    """Open a web page the user already had, with their default browser (http/https only; memory resolved it)."""
    from urllib.parse import urlsplit

    try:
        if urlsplit(url).scheme not in ("http", "https"):
            return False
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception:
        return False


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


class NudgeCard(SurfaceWindow):
    """One nudge, bottom-right (Horizon's check-in card): a small grey wave and the time, the text, then
    Dismiss and Reply as pills. Never takes focus. A kind other than a plain check-in says its kind in quiet
    words beside the wave ("Weekly review"); a question leads with the question, bold.

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
        never_ask: ``(nudge)`` -- "Don't ask" on a question card (its Dismiss reads "Skip").
        accepted: ``(nudge)`` -- the offer's button (named for the action) on a card with a typed offer; the card
            stays up and shows the result (:meth:`show_result`).
        less: ``(nudge)`` -- "Less of these" on an observation card or a welcome back.
        undo: ``(nudge)`` -- Undo after the offer ran (adding to the list, opening a page again).
    """

    replied = Signal(object)
    dismissed = Signal(object)
    expired = Signal(object)
    never_ask = Signal(object)
    accepted = Signal(object)
    less = Signal(object)
    undo = Signal(object)

    def __init__(self, *, linger_ms: int | None = None) -> None:
        super().__init__(activates=False)
        self.setObjectName("YukiNudgeCard")
        self.nudge: dict[str, Any] | None = None
        self._fixed_linger = linger_ms
        #: The linger of the nudge now up (set by :meth:`set_nudge`).
        self.linger_ms = linger_ms if linger_ms is not None else CARD_LINGER_BASE_MS
        self.setFixedWidth(CARD_WIDTH + 2 * self.SHADOW)

        from yuki.ui.wave import Wave
        from yuki.ui.window.theme import variable_font

        column = QVBoxLayout(self)
        column.setContentsMargins(self.SHADOW + 20, self.SHADOW + 16, self.SHADOW + 20, self.SHADOW + 16)
        column.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.wave = Wave("checkin", theme=self.theme, size=(120, 16), parent=self)
        header.addWidget(self.wave, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = QLabel(self)
        self.title.setFont(variable_font(12, "Regular"))
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.title)   # the kind, in quiet words (none for a plain check-in)
        header.addStretch(1)
        self.when = QLabel(self)
        self.when.setFont(variable_font(12, "Regular"))
        self.when.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.when)
        column.addLayout(header)

        self.body = QLabel(self)
        self.body.setFont(variable_font(15, "Regular"))
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.body)
        #: A question card: what the question rests on, small and dim under the bold question.
        self.detail = QLabel(self)
        self.detail.setFont(variable_font(13, "Regular"))
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setVisible(False)
        column.addWidget(self.detail)
        #: What the offer did, in place: "Added to your list.", the draft, what Yuki holds, or why nothing ran.
        self.result = QLabel(self)
        self.result.setFont(variable_font(13.5, "Regular"))
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setVisible(False)
        column.addWidget(self.result)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.setContentsMargins(0, 4, 0, 0)
        buttons.addStretch(1)
        self.never_button = self._button(QUESTION_BUTTONS[2])
        self.never_button.setVisible(False)   # question cards only
        self.less_button = self._button(OFFER_BUTTONS[1])
        self.less_button.setVisible(False)    # observation cards and the welcome back
        self.dismiss_button = self._button("Dismiss")
        self.reply_button = self._button("Reply")
        self.do_button = self._button("")
        self.do_button.setVisible(False)      # cards with a typed offer only: its label names the action
        self.undo_button = self._button("Undo")
        self.copy_button = self._button("Copy")
        self.close_button = self._button("Close")
        for after in (self.undo_button, self.copy_button, self.close_button):
            after.setVisible(False)           # after the offer ran
        self.never_button.clicked.connect(self._on_never)
        self.less_button.clicked.connect(self._on_less)
        self.dismiss_button.clicked.connect(self._on_dismiss)
        self.reply_button.clicked.connect(self._on_reply)
        self.do_button.clicked.connect(self._on_do)
        self.undo_button.clicked.connect(self._on_undo)
        self.copy_button.clicked.connect(self._on_copy)
        self.close_button.clicked.connect(self._on_close)
        for button in (self.never_button, self.less_button, self.dismiss_button, self.reply_button, self.do_button,
                       self.undo_button, self.copy_button, self.close_button):
            buttons.addWidget(button)
        column.addLayout(buttons)
        #: The offer ran (or was refused) on this card: what it says now is the result.
        self._acted = False
        #: A draft the card shows (for Copy).
        self._draft = ""

        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self._on_expire)
        #: True while the pointer is on the card and its clock is stopped.
        self._paused = False
        self.restyle()

    def _button(self, text: str) -> QPushButton:
        from yuki.ui.window.theme import variable_font

        button = QPushButton(text, self)
        button.setFont(variable_font(13, "Regular"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # The card never takes the keyboard; nor may its buttons.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _style_buttons(self) -> None:
        self.reply_button.setStyleSheet(self.button_css("default"))
        self.do_button.setStyleSheet(self.button_css("default"))
        self.copy_button.setStyleSheet(self.button_css("default"))
        quiet = self.button_css("ghost")
        for button in (self.dismiss_button, self.never_button, self.less_button, self.undo_button, self.close_button):
            button.setStyleSheet(quiet)
        self.title.setStyleSheet(f"color: {self.ink_css('text_3')};")

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (theme changed)
        self.when.setStyleSheet(f"color: {self.ink_css('text_3')};")
        self.body.setStyleSheet(f"color: {self.ink_css('text')};")
        self.detail.setStyleSheet(f"color: {self.ink_css('text_2')};")
        self.result.setStyleSheet(f"color: {self.ink_css('text')};")
        self._style_buttons()

    # -- content -----------------------------------------------------------

    def set_nudge(self, nudge: dict[str, Any]) -> None:
        """Fill the card for one nudge and size it to its text (no showing)."""
        self.nudge = nudge
        kind = str(nudge.get("kind") or "nudge")
        self.title.setText("" if kind == "nudge" else card_title(nudge))   # a plain check-in: the wave says it
        question = str(nudge.get("kind") or "") == "question"
        offer = offer_open(nudge) and not question
        self._acted, self._draft = False, ""
        self.reply_button.setText(QUESTION_BUTTONS[0] if question else "Reply")
        self.reply_button.setVisible(not offer)
        self.do_button.setText(offer_label(nudge) if offer else "")
        self.do_button.setEnabled(True)
        self.do_button.setVisible(offer)
        self.dismiss_button.setText(QUESTION_BUTTONS[1] if question else OFFER_BUTTONS[0] if offer else "Dismiss")
        self.dismiss_button.setVisible(True)
        self.never_button.setVisible(question)
        self.less_button.setVisible(kind in LESS_KINDS)
        for after in (self.undo_button, self.copy_button, self.close_button):
            after.setVisible(False)
        self.result.setVisible(False)
        self.result.setText("")
        self.when.setText(when_label(nudge.get("at")))
        text = _squash(nudge.get("text"))
        from yuki.ui.window.theme import variable_font

        if question:   # the question leads, bold and bright; what it rests on sits under it, small and dim
            asked, rest = split_question(text)
            self.body.setFont(variable_font(15, "Semibold"))
            self.body.setText(asked)
            self.detail.setText(rest)
            self.detail.setVisible(bool(rest))
        else:
            self.body.setFont(variable_font(15, "Regular"))
            self.body.setText(text)  # all of it: word-wrapped, the card grows to fit
            # what the button will and won't do, always in sight before the press
            self.detail.setText(offer_lines(nudge) if offer else "")
            self.detail.setVisible(offer)
        self.linger_ms = self._fixed_linger if self._fixed_linger is not None else (
            linger_for(text) + (OFFER_LINGER_EXTRA_MS if offer else 0))
        self.restyle()
        self._fit_height()

    def _fit_height(self) -> None:
        width = CARD_WIDTH + 2 * self.SHADOW
        self.layout().activate()
        wanted = self.layout().totalHeightForWidth(width) if self.layout().hasHeightForWidth() else (
            self.layout().sizeHint().height()
        )
        height = max(CARD_MIN_HEIGHT + 2 * self.SHADOW, wanted)
        if height != self.height():
            bottom = self.geometry().bottom()
            self.setFixedHeight(height)
            if self.isVisible():   # grow upwards: the card stays on its corner
                self.move(self.x(), bottom - height + 1)
        self.update()

    # -- the offer's result, in place ---------------------------------------

    def show_running(self) -> None:
        """The offer's button was pressed: it says so until memory answers (typed offers answer at once)."""
        self._acted = True
        self._linger.stop()
        self.do_button.setText("Running…")
        self.do_button.setEnabled(False)
        for button in (self.dismiss_button, self.less_button, self.reply_button, self.never_button):
            button.setVisible(False)

    def show_result(self, result: dict[str, Any]) -> None:
        """What the offer did (``offer_act``'s answer): a line and Undo (added to the list, a page opened again),
        the draft with Copy, what Yuki holds, or why nothing ran; then the card fades after a while."""
        self._acted = True
        self.do_button.setVisible(False)
        for button in (self.dismiss_button, self.less_button, self.reply_button, self.never_button):
            button.setVisible(False)
        do = str(result.get("do") or (card_offer(self.nudge) or {}).get("do") or "")
        linger = RESULT_LINGER_MS
        if not result.get("ok"):
            why = str(result.get("why") or "error")
            text = OFFER_FAILED.get(why, OFFER_FAILED["error"])
            if why == "already" and result.get("state") == "undone":
                text = "Already undone."
            self.result.setText(text)
            self.close_button.setVisible(True)
        elif do == "draft":
            self._draft = str(result.get("text") or "")
            self.result.setText(self._draft)
            self.copy_button.setVisible(True)
            self.close_button.setVisible(True)
            linger = EXPANDED_LINGER_MS
        elif do == "show_memory":
            items = result.get("items") or []
            self.result.setText("\n\n".join(f"{i.get('title')}: {i.get('text')}" for i in items if isinstance(i, dict)))
            self.close_button.setVisible(True)
            linger = EXPANDED_LINGER_MS
        else:
            self.result.setText(str(result.get("message") or "Done."))
            self.undo_button.setVisible(bool(result.get("undo")))
        self.result.setVisible(True)
        self.detail.setVisible(False)
        self.linger_ms = self._fixed_linger if self._fixed_linger is not None else linger
        self._fit_height()
        if self.underMouse():
            self._paused = True    # being read: the clock starts when the pointer leaves
        elif not self._paused:
            self._linger.start(self.linger_ms)

    def show_undone(self, result: dict[str, Any]) -> None:
        """Undo's answer: "Undone." (or why not)."""
        self.undo_button.setVisible(False)
        self.result.setText("Undone." if result.get("ok") else "Couldn't undo that.")
        self.close_button.setVisible(True)
        self._fit_height()
        if not self._paused:
            self._linger.start(self._fixed_linger if self._fixed_linger is not None else RESULT_LINGER_MS)

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
        self._attention(True)
        self._linger.start(self.linger_ms)

    def _attention(self, shown: bool) -> None:
        """Tell the shared attention state (``yuki.core.attention``: the voice narrator waits for a card the
        user is reading) that a card is up, or went away. Never fails the card."""
        try:
            from yuki.core.attention import shared

            if shown:
                self._attention_id = str((self.nudge or {}).get("id"))
                shared().card_shown(self._attention_id)
            elif getattr(self, "_attention_id", None) is not None:
                shared().card_closed(self._attention_id)
                self._attention_id = None
        except Exception:
            pass

    def hideEvent(self, event: object) -> None:  # noqa: D102 - faded out, withdrawn or closed: the card is gone
        self._attention(False)
        super().hideEvent(event)

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
        acted = self._acted
        nudge = self.close_card()
        if nudge is not None and not acted:   # after its offer ran the card's outcome is recorded already
            self.expired.emit(nudge)

    def _on_dismiss(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.dismissed.emit(nudge)

    def _on_reply(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.replied.emit(nudge)

    def _on_never(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.never_ask.emit(nudge)

    def _on_do(self) -> None:
        nudge = self.nudge
        if nudge is None or self._acted or not offer_open(nudge):
            return
        self.show_running()   # the card stays: the result shows here (typed offers answer at once)
        self.accepted.emit(nudge)

    def _on_undo(self) -> None:
        if self.nudge is not None:
            self.undo_button.setEnabled(False)
            self.undo.emit(self.nudge)

    def _on_copy(self) -> None:
        QGuiApplication.clipboard().setText(self._draft)
        self.copy_button.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy"))

    def _on_close(self) -> None:
        self.close_card()

    def _on_less(self) -> None:
        nudge = self.close_card()
        if nudge is not None:
            self.less.emit(nudge)


class NoticeCard(NudgeCard):
    """A one-off notice from the runtime (``AgentRuntime.notice``: the daily cost alert), as the same card as a
    nudge: its heading, the text, one "OK". Nothing is acknowledged to memory; it is not a
    nudge. Shown by :meth:`yuki.ui.app.YukiUi.show_notice` when the screen is free."""

    TITLES = {"cost": "Today's spend", "doctor": "Health check"}

    def show_notice(self, kind: str, text: str) -> None:
        self.set_nudge({"id": f"notice:{kind}", "kind": kind, "text": text, "at": None})
        self.title.setText(self.TITLES.get(kind, "Yuki"))
        self.reply_button.hide()
        self.never_button.hide()
        self.do_button.hide()
        self.less_button.hide()
        self.undo_button.hide()
        self.copy_button.hide()
        self.close_button.hide()
        self.dismiss_button.setText("OK")
        self._paused = False
        self.fade_in(self.anchor())
        self._attention(True)
        self._linger.start(self.linger_ms)


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
        offer_state: ``(nudge, state)`` -- a card's offer is ``running``, ``done``, ``undone``, ``prefilled``
            (an ``ask`` suggestion went into the ask bar) or could not run (``expired``, ``gone``, ``off``,
            ``already``, ``error``): the chat thread's button says so honestly.
        ask_requested: ``(nudge, text)`` -- an ``ask`` offer: put ``text`` in the ask bar, unsent.
    """

    reply_requested = Signal(object)
    in_overlay = Signal(object, str)
    recent_ready = Signal(object)
    offer_state = Signal(object, str)
    ask_requested = Signal(object, str)
    #: ``(event name, fields)`` from worker threads, written on the GUI thread.
    _logged = Signal(str, object)
    #: ``(nudge, result, via)`` / ``(nudge, result)``: memory's answer to a press / an Undo, back on the GUI thread.
    _acted = Signal(object, object, str)
    _undone = Signal(object, object)

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
        self.card.never_ask.connect(self._on_never_ask)
        self.card.accepted.connect(self.accept_offer)
        self.card.less.connect(self._on_less)
        self.card.undo.connect(self.undo_offer)
        self._acted.connect(self._on_acted)
        self._undone.connect(self._on_undone)
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
        if self.card._acted:   # its offer ran (the card shows the result): it is done, never queued again
            self.card.close_card()
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
        if card_offer(nudge) is not None:   # Not now on an offer: its button is gone in the chat thread too
            self.offer_state.emit(nudge, "dismissed")
        if str(nudge.get("kind") or "") == "question":
            self.question_feedback(nudge, never=False)

    def accept_offer(self, nudge: dict[str, Any], via: str = "card") -> threading.Thread | None:
        """The offer's button on a card (the corner card, or a card in the chat thread): acknowledged ``accepted``,
        then memory checks the offer again and runs it **by code** (``offer_act``; never the agent, never a model
        call), off the GUI thread; the answer comes back to :meth:`_on_acted`. Once per card: a second press, an
        expired offer or a card already answered runs nothing."""
        offer = card_offer(nudge)
        if offer is None:
            return None
        stored = self.record_delivery(nudge, via)   # the one dict per card, shared by the corner card and the thread
        if not offer_open(stored) or not offer_open(nudge):
            self.ui_log.event("nudge_offer_refused", id=nudge.get("id"), via=via, state=stored.get("_offer_state"),
                              outcome=stored.get("outcome"), expired=offer_expired(stored))
            if stored.get("_offer_state") is None and offer_expired(stored):
                self._show_result(stored, {"ok": False, "why": "expired", "do": offer.get("do")})
            return None
        stored["_offer_state"] = "running"
        self.ui_log.event("nudge_accepted", id=nudge.get("id"), kind=nudge.get("kind"), via=via,
                          trigger=nudge.get("trigger"), do=offer.get("do"), label=offer.get("label"))
        self.ack(stored, "accepted")
        self.offer_state.emit(stored, "running")
        nudge_id = stored.get("id")

        def work() -> None:
            t0 = time.perf_counter()
            try:
                result = self.memory.call_method("offer_act", nudge_id, timeout_s=TRAY_TIMEOUT_S)
            except MemoryUnavailable as exc:
                result = {"ok": False, "why": "error", "error": str(exc)}
            except Exception as exc:  # never take the UI down
                result = {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}
            result = dict(result) if isinstance(result, dict) else {"ok": False, "why": "error"}
            result["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            self._acted.emit(stored, result, via)

        thread = threading.Thread(target=work, name="yuki-ui-offer", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread

    def _on_acted(self, stored: dict[str, Any], result: dict[str, Any], via: str) -> None:
        """Memory's answer to an offer press (GUI thread): open the page again (``bring_back``, remembering what
        was in front for Undo), hand an ``ask`` suggestion to the ask bar, and show the result on the card."""
        offer = card_offer(stored) or {}
        do = str(result.get("do") or offer.get("do") or "")
        ok = bool(result.get("ok"))
        state = str(result.get("state") or "done") if ok else str(result.get("why") or "error")
        if not ok and result.get("why") == "already" and result.get("state"):
            state = str(result["state"])
        stored["_offer_state"] = state
        if ok:
            stored["outcome"] = state
            if do == "bring_back":
                stored["_prev_hwnd"] = _foreground_hwnd()
                opened = _open_page(str(result.get("url") or ""))
                if not opened:
                    result = {**result, "message": "Couldn't open that page.", "undo": False}
        # what the press did, kept on the card's one dict: the Yuki window's offer op answers with it (E3)
        stored["_offer_result"] = {k: result.get(k) for k in ("ok", "do", "state", "why", "message", "undo", "text",
                                                               "items", "title") if result.get(k) is not None}
        stored["_offer_result"]["do"] = do
        if ok:
            stored["_outcome_at"] = time.time()
        self.ui_log.event("nudge_offer_result", id=stored.get("id"), via=via, do=do, ok=ok, state=state,
                          why=result.get("why"), ms=result.get("ms"), error=result.get("error"))
        if ok and do == "ask":
            self.ask_requested.emit(stored, str(result.get("text") or ""))
            if self.card.isVisible() and self.card.nudge is not None and _nudge_id(self.card.nudge) == _nudge_id(stored):
                self.card.close_card()   # the ask bar has it now
        else:
            self._show_result(stored, result)
        self.offer_state.emit(stored, state)

    def _show_result(self, stored: dict[str, Any], result: dict[str, Any]) -> None:
        if self.card.isVisible() and self.card.nudge is not None and _nudge_id(self.card.nudge) == _nudge_id(stored):
            self.card.show_result(result)

    def undo_offer(self, nudge: dict[str, Any]) -> threading.Thread | None:
        """Undo on a card whose offer ran: ``bring_back`` brings back the window that was in front; memory takes back
        what it did (``offer_undo``: a to-do added ends as a wrong capture) and records it (the learning counts it)."""
        stored = self.record_delivery(nudge, str(nudge.get("_via") or "card"))
        offer = card_offer(stored) or {}
        if stored.get("_offer_state") != "done" or offer.get("do") not in ("todo_add", "bring_back"):
            return None
        stored["_offer_state"] = "undoing"
        prev = stored.get("_prev_hwnd") if offer.get("do") == "bring_back" else None
        nudge_id = stored.get("id")

        def work() -> None:
            if prev:
                try:
                    from yuki.actions.launch import focus_window

                    focus_window(int(prev))
                except Exception:
                    pass
            try:
                result = self.memory.call_method("offer_undo", nudge_id, timeout_s=TRAY_TIMEOUT_S)
            except Exception as exc:  # never take the UI down
                result = {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}
            self._undone.emit(stored, result if isinstance(result, dict) else {"ok": False})

        thread = threading.Thread(target=work, name="yuki-ui-offer-undo", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread

    def _on_undone(self, stored: dict[str, Any], result: dict[str, Any]) -> None:
        state = "undone" if result.get("ok") else "done"
        stored["_offer_state"] = state
        stored["_undo_result"] = dict(result)
        if result.get("ok"):
            stored["outcome"] = "undone"
        self.ui_log.event("nudge_offer_undo", id=stored.get("id"), ok=bool(result.get("ok")), why=result.get("why"))
        if self.card.isVisible() and self.card.nudge is not None and _nudge_id(self.card.nudge) == _nudge_id(stored):
            self.card.show_undone(result)
        self.offer_state.emit(stored, state)

    def offer_outcome(self, nudge: dict[str, Any], outcome: str) -> threading.Thread:
        """What became of an ``ask`` suggestion (``done`` / ``stopped`` / ``abandoned``), for the learning."""
        nudge_id = nudge.get("id")
        stored = self.delivered.get(_nudge_id(nudge))
        if stored is not None:
            stored["_offer_state"] = outcome
            stored["outcome"] = outcome

        def work() -> None:
            fields: dict[str, Any] = {"id": nudge_id, "outcome": outcome}
            try:
                fields["ok"] = bool(self.memory.call_method("offer_outcome", nudge_id, outcome,
                                                            timeout_s=TRAY_TIMEOUT_S))
            except Exception as exc:  # never take the UI down
                fields.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            self._logged.emit("nudge_offer_outcome", fields)

        thread = threading.Thread(target=work, name="yuki-ui-offer-outcome", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        if stored is not None:
            self.offer_state.emit(stored, outcome)
        return thread

    def _on_less(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_less", id=nudge.get("id"), kind=nudge.get("kind"), trigger=nudge.get("trigger"))
        self.ack(nudge, "less")
        if card_offer(nudge) is not None:
            self.offer_state.emit(nudge, "less")

    def _on_never_ask(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_dismissed", id=nudge.get("id"), via="card_never")
        self.ack(nudge, "dismissed")
        self.question_feedback(nudge, never=True)

    def question_feedback(self, nudge: dict[str, Any], *, never: bool) -> threading.Thread:
        """A question card's Skip / "Don't ask": ``skip_question(ref, never)`` off the GUI thread; logs ``question_skip``."""
        ref = nudge.get("ref")

        def work() -> None:
            fields: dict[str, Any] = {"id": nudge.get("id"), "ref": ref, "never": never}
            try:
                result = self.memory.call_method("skip_question", ref, never, timeout_s=TRAY_TIMEOUT_S)
                fields.update(ok=True, saved=bool((result or {}).get("saved")) if isinstance(result, dict) else None)
            except MemoryUnavailable as exc:
                fields.update(ok=False, error=str(exc))
            except Exception as exc:  # never take the UI down
                fields.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            self._logged.emit("question_skip", fields)

        thread = threading.Thread(target=work, name="yuki-ui-question-skip", daemon=True)
        thread.start()
        self._ack_threads = [t for t in self._ack_threads if t.is_alive()] + [thread]
        return thread

    def _on_replied(self, nudge: dict[str, Any]) -> None:
        self.ui_log.event("nudge_reply_opened", id=nudge.get("id"))
        self.reply_requested.emit(nudge)

    def reply_sent(self, nudge: dict[str, Any], text: str, request_id: int, lane: str) -> None:
        """The user sent their reply to ``nudge`` (it went to ``runtime.submit``)."""
        self.ui_log.event(
            "nudge_replied", id=nudge.get("id"), request_id=request_id, lane=lane, text=text
        )
        self.ack(nudge, "replied", reply=text)   # the coach reads the reply itself, not a count

    def reply_abandoned(self, nudge: dict[str, Any]) -> None:
        """The overlay closed before the user sent a reply to ``nudge``."""
        self.ui_log.event("nudge_dismissed", id=nudge.get("id"), via="overlay")
        self.ack(nudge, "dismissed")
        self.try_show()

    def ack(self, nudge: dict[str, Any], reaction: str, *, reply: str | None = None) -> threading.Thread:
        """``ack_nudge(id, reaction)`` off the GUI thread; logs ``nudge_ack``. ``reply``: what the user wrote back."""
        nudge_id = nudge.get("id")
        self._set_reaction(nudge_id, reaction)

        def work() -> None:
            try:
                if reply:
                    self.memory.ack_nudge(nudge_id, reaction, reply=reply)
                else:
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
