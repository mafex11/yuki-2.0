"""The ask-Yuki overlay: one input, and a short stack of reply cards above it.

Lifecycle of a request, all of it animated and none of it timed with sleeps:
submit collapses the input, a card grows in where the input used to be, and a
fresh (empty) input expands below it. The card fills in when the answer arrives
and grows to fit. :data:`MAX_CARDS` cards stay visible; older ones fade away.

Memory's nudges live in the same stack (tone ``nudge``: labelled
``Yuki, 14:32, nudge``, with their own Reply), each
placed by the time the user saw it, so the stack reads as one conversation.

The card stack sits in a scroll area whose height follows its content up to
:data:`MAX_CARDS_FRACTION` of the screen; past that it scrolls (wheel anywhere on
the panel, or the thin scrollbar) and the input below it stays on screen and
keeps the keyboard. A new card or a reply filling in scrolls the stack to the
bottom; scrolling up stops that following until the user is back at the bottom.

The overlay never decides anything about the request -- it hands the text to
:mod:`yuki.ui.runtime` and renders what comes back.

**Thread mode** (:meth:`Overlay.attach_thread`, phase 2): the card stack gives way
to the live chat thread (:class:`yuki.ui.chatview.ChatThreadView` over the tray's
:class:`~yuki.ui.chat_model.ChatModel`), the same thread the Yuki window shows: a
bubble per message, typed or spoken, Yuki's replies streaming in, and each
response's steps. The card calls keep working (they return :class:`ThreadCard`
stand-ins), so the controller in :mod:`yuki.ui.app` is unchanged.

**A run's foreground is never taken** (x12Overlay; live 10 Oct 21:09:45 and 21:10:00 the hotkey brought the bar up
mid-run, it took the keyboard, and the run's ctrl+t into Arc was refused twice: "Foreground window changed ... got
'Yuki'"). Every key Yuki's hands send must land in the window the run works in, and only one window has the keyboard:

* **the hotkey while a run holds the desk** (:func:`run_holds_desk`: a request working or about to act, Yuki's hands
  at work, or the "Yuki is acting" marker; not a run the user paused, nor one waiting on their typed answer) shows a
  small bar of its own, :class:`SpareBar`, *without* taking the keyboard ("Yuki is mid-task. Type or click here to
  ask."), and the ask bar itself stays closed -- so the tray sees no open ask bar: the run's answer and memory's nudges
  go where they go with nothing open. The user's first key that types (held back by :class:`yuki.ui.hotkey.KeyClaim`,
  so their words never land in the run's window, a chat box where Enter sends) or their click on it opens the ask bar
  as usual with the keyboard, and the keys held back are typed into it. Esc or the hotkey closes it; so do a click
  elsewhere, the run letting go of the desk (after :data:`SPARE_FREE_S`) and :data:`SPARE_LIFE_S` unclaimed;
* **a submit while a run holds the desk** gives the keyboard straight back to the window the run works in. Words that
  went to the run (an answer to its question, words for a paused run) close the bar; anything else (a question for
  the reading lane) leaves it on screen, unfolded, without the keyboard (*lent*), so its answer shows there; typing or
  a click takes the keyboard again, and it closes on Esc, the hotkey, a click elsewhere, or :data:`LENT_LINGER_S`
  after the run and the answer are done;
* **Yuki's own clicks pass through** a bar showing without the keyboard: it lets clicks through while Yuki's hands
  are at work (:data:`SPARE_GUARD_TAIL_S`), and a press that carries Yuki's input stamp is never taken for the user's
  (it lets that one through from then on, and the small bar steps aside);
* **closing while it holds the foreground** hands the keyboard straight back to the window that was in front before
  it, not 150 ms later when the fade ends (a run's first step, sent the moment the bar steps aside for it, no longer
  meets the fading bar in front).

Every other way the bar opens (a question, a click on the island, a card's Reply, the tray, the window) is the user's
or a question's, and takes the keyboard as before. Plumbing only: who holds the keyboard is measured on the run's own
state, input stamps and window handles, never on what anybody typed or said; the logs carry codes and counts only.
"""

from __future__ import annotations

import ctypes
import os
import time
from typing import Any, Callable, Literal

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from yuki.ui import tokens as _tokens
from yuki.ui.focus import FocusResult, set_no_activate
from yuki.ui.surface import (
    ANIM_MS,
    CARD_BG,
    CARD_BORDER,
    ERROR_BG,
    ERROR_BORDER,
    TEXT_DIM,
    TEXT_PRIMARY,
    SurfaceWindow,
    ui_font,
)

#: Width of the ask bar (the Lattice design's ``.askbar``: 780 px) and its padding (18 px in).
PANEL_WIDTH = 780
PAD = 18
#: The ask bar's top, from the top of the monitor in use (the design's ``top: 64px``).
TOP_PX = 64

#: How many cards (requests, replies and nudges together) stay on screen.
MAX_CARDS = 5

#: Tallest the card stack gets, as a share of the screen's available height; a
#: longer conversation scrolls inside it. The panel's top sits at 18% of the
#: screen, so stack + input end well above the taskbar at any screen size.
MAX_CARDS_FRACTION = 0.55

#: The stack's scrollbar: thin, only there when it can scroll (Lattice hairline greys, :mod:`yuki.ui.tokens`).
SCROLL_STYLE = (
    "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
    "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0 2px 0; }"
    f"QScrollBar::handle:vertical {{ background: {_tokens.COLOR['hair2']}; border-radius: 4px; min-height: 24px; }}"
    f"QScrollBar::handle:vertical:hover {{ background: {_tokens.COLOR['mark']}; }}"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
)

#: After the overlay is shown, how long losing activation does not count as the
#: user clicking away. Covers the transient activate/deactivate messages of the
#: foreground hand-over (:func:`yuki.ui.focus.force_foreground`). A UI timer, not
#: a wait: nothing blocks on it.
FOCUS_GRACE_MS = 200

#: A bar showing without the keyboard (the small bar, or the ask bar lent back): how often it reads again whether a
#: run still holds the desk and which window is in front (where the keyboard goes back to) (ms). A UI timer, not a
#: wait; it runs only while such a bar shows.
SPARE_TICK_MS = 500
#: ... and how often it looks at Yuki's own input (to let its clicks through) and at the user's clicks (ms): only
#: while such a bar shows; each look is two reads of in-process state.
SPARE_GUARD_MS = 30
#: Yuki's own input ran this recently: a bar showing without the keyboard lets every click through (s). Its click
#: comes after it names what is under the point, so the bar is out of the way by then.
SPARE_GUARD_TAIL_S = 1.0
#: The small bar closes by itself this long after it came up, unclaimed (s)...
SPARE_LIFE_S = 8.0
#: ... or this long after no run holds the desk any more (s).
SPARE_FREE_S = 2.0
#: The ask bar lent back after a question for the reading lane closes this long after the run and the answer are
#: done, untouched (s).
LENT_LINGER_S = 8.0
#: The user's claim (a key or a click) waits at most this long for Yuki's own input under way to finish before the
#: bar takes the keyboard, so a send in progress is never split between two windows (s).
CLAIM_WAIT_S = 1.0
#: A press's input stamp, read as it arrived (``nativeEvent``), still describes the press Qt delivers this soon (s).
PRESS_FRESH_S = 0.5
#: Yuki's own input ended this recently: its hands are still on the desk, between two sends of one step (s).
DESK_QUIET_S = 2.0
#: A response in the thread with no sign of life for this long is not a run any more, whatever its status says (a
#: lost end event): the "Yuki is acting" marker's own bound (:data:`yuki.memory.acting.MAX_AGE_S`).
DESK_STALE_S = 600.0
#: The line of a bar showing without the keyboard, and once no run holds the desk any more.
SPARE_LINE = "Yuki is mid-task. Type or click here to ask."
SPARE_FREE_LINE = "Type or click here to ask."
#: The small bar: its height, and where it sits (centred, at the ask bar's top).
SPARE_H = 48
#: The input's own line.
ASK_LINE = "Ask Yuki"
#: Step kinds (:class:`yuki.ui.chat_model.Step`) that are Yuki's hands, or a thinker job, at work while they run.
_HAND_STEPS = frozenset({"tool", "hand", "job"})

CardTone = Literal["reply", "question", "error", "context", "nudge"]


# -- who holds the desk -----------------------------------------------------------------------------------------------


def _last_life(item: Any) -> float:
    """The latest moment a thread response showed it was alive (epoch s): its bubble, its end, its steps."""
    moments = [getattr(item, "at", 0.0), getattr(item, "ended_at", None) or 0.0]
    for step in getattr(item, "steps", None) or []:
        moments.append(getattr(step, "started", 0.0) or 0.0)
        moments.append(getattr(step, "ended", None) or 0.0)
    return max(float(m) for m in moments if isinstance(m, (int, float)))


def own_input_recent(within: float = DESK_QUIET_S) -> bool:
    """One of Yuki's own input actions runs now, or ended under ``within`` seconds ago (any hand in this process: the
    worker's, the voice's, a routine card's; :mod:`yuki.actions.leases`)."""
    try:
        from yuki.actions import leases

        if leases.own_input_now():
            return True
        at = float(leases.own_input_at() or 0.0)
        return bool(at) and time.monotonic() - at <= within
    except Exception:  # noqa: BLE001 - no leases (an offline render): nobody's hands
        return False


def own_input_running() -> bool:
    """One of Yuki's own input actions is under way right now (started, not ended)."""
    try:
        from yuki.actions import leases

        start, end = leases.own_input_span()
        return bool(start) and end is None
    except Exception:  # noqa: BLE001
        return False


def user_click_at() -> float:
    """When the user last pressed a mouse button (monotonic; 0.0: not known), as the input monitor saw it
    (:mod:`yuki.actions.userinput`, listening while a request works on the desktop). Yuki's own clicks carry its
    input stamp and never count there."""
    try:
        from yuki.actions import userinput

        monitor = userinput.running()
        return float(monitor.last("click") or 0.0) if monitor is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def game_in_front() -> bool:
    """A game is the window in front (:func:`yuki.memory.gamegate.game_in_front`): its keys are never held back."""
    try:
        from yuki.memory import gamegate

        return bool(gamegate.game_in_front())
    except Exception:  # noqa: BLE001 - unknown: nothing is held back
        return True


#: ``WM_[LRMX]BUTTONDOWN`` and their double clicks: a press on a window.
_PRESS_MESSAGES = frozenset({0x0201, 0x0203, 0x0204, 0x0206, 0x0207, 0x0209, 0x020B, 0x020D})


def native_press(message: Any) -> bool | None:
    """For a mouse press arriving on one of the bar's windows (the ``MSG`` Qt hands to ``nativeEvent``): whether it
    carries Yuki's input stamp (``GetMessageExtraInfo``: its hands' ``SendInput`` click). None for any other message."""
    try:
        from ctypes import wintypes

        from yuki.core.inputtag import is_yuki_input

        msg = wintypes.MSG.from_address(int(message))
        if int(msg.message) not in _PRESS_MESSAGES:
            return None
        return is_yuki_input(int(_user32().GetMessageExtraInfo() or 0))
    except Exception:  # noqa: BLE001 - not on Windows
        return None


def press_was_yuki(native: tuple[float, bool] | None, *, now: float | None = None) -> bool:
    """Whether the press being handled was Yuki's own click: its input stamp as it arrived (``native``: ``(when,
    stamped)``, fresh for :data:`PRESS_FRESH_S`), else -- no stamp known -- whether Yuki's own input was under way."""
    t = time.monotonic() if now is None else float(now)
    if native is not None and t - native[0] <= PRESS_FRESH_S:
        return bool(native[1])
    try:
        from yuki.actions import leases

        return bool(leases.own_input_now())
    except Exception:  # noqa: BLE001
        return False


def acting_marker() -> bool:
    """The "Yuki is acting" marker stands (:mod:`yuki.memory.acting`: a request with its hands on the desktop, from
    its first action to its end, in any of Yuki's processes; a dead process's or a stale one never counts)."""
    try:
        from yuki.memory import acting

        return acting.current(None) is not None
    except Exception:  # noqa: BLE001
        return False


def run_holds_desk(model: Any, *, waiting: bool = False, now: float | None = None,
                   own_input: Callable[[], bool] = own_input_recent,
                   marker: Callable[[], bool] = acting_marker) -> str | None:
    """Why a run holds the desk now (Yuki's hands are on the mouse and keyboard, or are about to be), as a short
    content-free code, or ``None`` when the desk is the user's.

    * ``hands``: a tool, a voice hand or a thinker job is running in the thread (``model``: the live
      :class:`~yuki.ui.chat_model.ChatModel`; ``None`` in card mode);
    * ``run``: a request is in hand (pending or running), between steps or still planning its first one (live
      21:09:45: the request had read memory and was planning; it acted 5 s later);
    * ``input``: Yuki's own input runs or just ended (:func:`own_input_recent`);
    * ``acting``: the "Yuki is acting" marker stands (:func:`acting_marker`);
    * ``question`` (``waiting`` only): a request waits on the user's typed answer.

    ``waiting`` False (the hotkey opening the bar): a request waiting on the user's typed answer is not holding the
    desk -- they open the bar to answer it, and it acts on nothing meanwhile -- and the acting marker, which such a
    request keeps standing, is then not counted either. ``waiting`` True (a submit): it counts, because the answer
    sets it acting again. A response with no sign of life for :data:`DESK_STALE_S` is not counted (a lost end event).
    Nothing here reads what a message says: statuses, step kinds and times only.
    """
    t = time.time() if now is None else float(now)
    question = False
    items = list(getattr(model, "items", None) or []) if model is not None else []
    for item in reversed(items):
        if getattr(item, "kind", "") != "assistant":
            continue
        try:
            if t - _last_life(item) > DESK_STALE_S:
                continue
        except (TypeError, ValueError):
            continue
        status = str(getattr(item, "status", "") or "")
        if status == "question":
            if waiting:
                return "question"
            question = True
            continue
        if any(getattr(s, "kind", "") in _HAND_STEPS and bool(getattr(s, "running", False))
               for s in getattr(item, "steps", None) or []):
            return "hands"
        if getattr(item, "jobs", None) and status in ("pending", "running"):
            return "run"
    if own_input():
        return "input"
    if not question and marker():
        return "acting"
    return None


def waiting_on_answer(model: Any, *, now: float | None = None) -> bool:
    """A request in the thread waits on the user's typed answer (status ``question``, alive within
    :data:`DESK_STALE_S`): what they submit now goes to it (the tray's ``_on_submitted``)."""
    t = time.time() if now is None else float(now)
    for item in list(getattr(model, "items", None) or []) if model is not None else []:
        if getattr(item, "kind", "") != "assistant" or str(getattr(item, "status", "") or "") != "question":
            continue
        try:
            if t - _last_life(item) <= DESK_STALE_S:
                return True
        except (TypeError, ValueError):
            continue
    return False


# -- windows (handles only; every call is one Win32 query or SetForegroundWindow) -------------------------------------

_U32: Any = None


def _user32() -> Any:
    """A private ``user32`` with its own prototypes (the process-wide ``windll.user32`` is shared; its ``restype``\\ s
    are not this module's to set)."""
    global _U32
    if _U32 is None:
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetForegroundWindow.restype = wintypes.BOOL
        for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
            getattr(u32, name).argtypes = [wintypes.HWND]
            getattr(u32, name).restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetMessageExtraInfo.argtypes = []
        u32.GetMessageExtraInfo.restype = wintypes.LPARAM
        _U32 = u32
    return _U32


def foreground_window() -> int:
    """The window in front now (0 when Windows can't say)."""
    try:
        return int(_user32().GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001 - not on Windows
        return 0


def window_usable(hwnd: int) -> bool:
    """``hwnd`` is a live, visible window that is not minimised: one the keyboard can be handed to."""
    try:
        u32 = _user32()
        return bool(hwnd) and bool(u32.IsWindow(hwnd)) and bool(u32.IsWindowVisible(hwnd)) \
            and not bool(u32.IsIconic(hwnd))
    except Exception:  # noqa: BLE001
        return False


def window_is_ours(hwnd: int) -> bool:
    """``hwnd`` belongs to this process (the bar, the island, a card): never a window to hand the keyboard back to."""
    try:
        from ctypes import wintypes

        pid = wintypes.DWORD(0)
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) == os.getpid()
    except Exception:  # noqa: BLE001
        return False


def give_foreground(hwnd: int) -> bool:
    """Hand the foreground to ``hwnd`` (allowed: the caller's process holds it); True when Windows says it is in
    front afterwards."""
    try:
        _user32().SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        return False
    return foreground_window() == int(hwnd)


def set_through(window: QWidget, on: bool) -> None:
    """Let every click through ``window`` (``WS_EX_TRANSPARENT``) or take them again; remembered on the window, so the
    style is written only when it changes."""
    if bool(getattr(window, "_yuki_through", False)) == bool(on):
        return
    window._yuki_through = bool(on)   # type: ignore[attr-defined]
    try:
        from yuki.ui.focus import set_click_through

        set_click_through(int(window.winId()), bool(on))
    except Exception:  # noqa: BLE001 - not on Windows
        pass


def make_key_claim() -> Any:
    """The hook that holds the user's keys for the ask bar while it shows without the keyboard
    (:class:`yuki.ui.hotkey.KeyClaim`)."""
    from yuki.ui.hotkey import KeyClaim

    return KeyClaim()


class SpareBar(SurfaceWindow):
    """The small bar the hotkey shows while a run holds the desk (see the module docstring): the face and one line,
    never activated (``WS_EX_NOACTIVATE``, shown without activating), at the ask bar's top, centred.

    Signals:
        pressed: ``(by_yuki)`` a mouse press on it: True when it was Yuki's own click (its input stamp; see
            :func:`press_was_yuki`), False when it was the user's.
    """

    SHADOW = 0
    pressed = Signal(bool)

    def __init__(self) -> None:
        super().__init__(activates=False, radius=SPARE_H // 2)
        self.setObjectName("YukiSpareBar")
        from yuki.ui import lattice as L
        from yuki.ui.dots import FaceWidget

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 22, 0)
        row.setSpacing(12)
        self.face = FaceWidget(4, self, blink=True)
        self.face.set_state("idle")
        row.addWidget(self.face, 0, Qt.AlignmentFlag.AlignVCenter)
        self.line = L.Para(SPARE_LINE, font=L.ui_font(15), color=L.TEXT2, line_height=1.2, parent=self, max_lines=1)
        row.addWidget(self.line, 1, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(SPARE_H)
        self._native: tuple[float, bool] | None = None
        self._fit()

    def set_line(self, text: str) -> None:
        if self.line.text() != text:
            self.line.set_text(text)
            self._fit()

    def _fit(self) -> None:
        width = 16 + self.face.width() + 12 + int(self.line.natural_width()) + 4 + 22
        self.line.setFixedSize(int(self.line.natural_width()) + 4, 20)
        self.setFixedWidth(min(PANEL_WIDTH, width))

    def nativeEvent(self, event_type: Any, message: Any) -> Any:  # noqa: N802, D102 - a press's input stamp
        stamped = native_press(message)
        if stamped is not None:
            self._native = (time.monotonic(), stamped)
        return False, 0

    def mousePressEvent(self, event: Any) -> None:  # noqa: D102 - Qt override
        self.pressed.emit(press_was_yuki(self._native))
        self._native = None
        event.accept()

    def close_now(self) -> None:
        """Gone at once (the ask bar comes up in its place)."""
        self._anim.stop()
        self._closing = False
        self.hide()

    def fade_out(self) -> None:  # noqa: D102 - nothing more is taken from a bar on its way out
        set_through(self, True)
        super().fade_out()

    def hideEvent(self, event: Any) -> None:  # noqa: D102
        super().hideEvent(event)
        set_through(self, False)

    def paintEvent(self, event: object) -> None:  # noqa: D102 - the ask bar's field and border, as a pill
        del event
        from PySide6.QtGui import QPainterPath

        from yuki.ui import lattice as L

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        painter.fillPath(path, L.BG)
        painter.setPen(QPen(L.ASKBAR_BORDER, 1.0))
        painter.drawPath(path)
        painter.end()


class AskInput(QPlainTextEdit):
    """The text field. Enter submits, Shift+Enter adds a line, Esc hides.

    It is a :class:`QPlainTextEdit` rather than a line edit only because of
    Shift+Enter; it renders as one line and grows to a few lines at most.

    Signals:
        submitted: Non-empty text, with surrounding whitespace removed.
        escaped: Esc was pressed.
        wants_height: The preferred height changed (text wrapped onto a new line).
    """

    submitted = Signal(str)
    escaped = Signal()
    wants_height = Signal(int)

    MAX_LINES = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(ui_font(13))
        self.setPlaceholderText("Ask Yuki…")
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        from yuki.ui.window.theme import css, theme

        self.setStyleSheet(
            "QPlainTextEdit {"
            "  background: transparent;"
            f"  color: {_tokens.COLOR['text']};"
            f"  selection-background-color: {css(theme().accent_soft)};"
            "  border: none; padding: 0px;"
            "}"
        )
        self.document().documentLayout().documentSizeChanged.connect(
            lambda *_: self.wants_height.emit(self.preferred_height())
        )

    def preferred_height(self) -> int:
        """Height that fits the current text, capped at :attr:`MAX_LINES`."""
        line = self.fontMetrics().lineSpacing()
        lines = max(1.0, self.document().documentLayout().documentSize().height())
        return int(line * min(lines, float(self.MAX_LINES))) + 6

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102 - Qt override
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
            return
        super().keyPressEvent(event)


class ReplyCard(QWidget):
    """One exchange: the request in dim text, and Yuki's answer below it.

    The card owns its reveal and growth animation. The height is animated and the
    window resizes to whatever the layout asks for on every frame, so a single
    animation grows both the card and the window.

    Args:
        prompt: What the user asked, shown dim above the body. Empty for a card
            that is only a message (a question, or an error with no request).
        body: Initial body text; empty means "still working".
        tone: ``context`` is something Yuki said first (a nudge being replied
            to); ``nudge`` is one of memory's nudges in the conversation, with
            a Reply of its own; ``error`` gets a red border.
        accent: Kept for callers; the look is monochrome.
        at: When the card's moment happened (epoch s; now by default): the
            stack is kept in this order.
        nudge: The nudge a ``nudge`` card shows.
        parent: Qt parent.

    Signals:
        reply_clicked: ``(nudge)`` -- Reply on a ``nudge`` card.
    """

    PENDING = "…"

    reply_clicked = Signal(object)
    #: The body was replaced (a reply arrived): the overlay brings it into view.
    content_changed = Signal()

    def __init__(
        self,
        prompt: str,
        body: str = "",
        *,
        tone: CardTone = "reply",
        accent: QColor | None = None,
        at: float | None = None,
        nudge: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tone: CardTone = tone
        self.accent = accent
        self.at: float = at if at is not None else time.time()
        self.nudge = nudge
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(4)

        self._prompt = QLabel(prompt, self)
        self._prompt.setFont(ui_font(10))
        self._prompt.setWordWrap(True)
        self._prompt.setStyleSheet(f"color: {TEXT_DIM.name()};")
        self._prompt.setVisible(bool(prompt))
        self.reply_button: QPushButton | None = None
        if tone == "nudge":
            from yuki.ui.surface import button_css

            self._prompt.setFont(ui_font(9))
            self._prompt.setTextFormat(Qt.TextFormat.PlainText)
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            header.addWidget(self._prompt, 1)
            self.reply_button = QPushButton("Reply", self)
            self.reply_button.setFont(ui_font(9, weight=QFont.Weight.Medium))
            self.reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A click must leave the keyboard in the input below.
            self.reply_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.reply_button.setStyleSheet(button_css("outline"))
            self.reply_button.clicked.connect(lambda: self.reply_clicked.emit(self.nudge))
            header.addWidget(self.reply_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(header)
        else:
            layout.addWidget(self._prompt)

        self._body = QLabel(body or self.PENDING, self)
        self._body.setFont(ui_font(12))
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Selecting text with the mouse must not pull the keyboard out of the input.
        self._body.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._body.setStyleSheet(f"color: {TEXT_PRIMARY.name()};")
        layout.addWidget(self._body)

        #: Dim time/steps/cost line under a finished reply; hidden until set.
        self._meta = QLabel("", self)
        self._meta.setFont(ui_font(9))
        self._meta.setTextFormat(Qt.TextFormat.PlainText)
        self._meta.setStyleSheet(f"color: {TEXT_DIM.name()};")
        self._meta.setVisible(False)
        layout.addWidget(self._meta)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(ANIM_MS)
        self._grow = QPropertyAnimation(self, b"maximumHeight", self)
        self._grow.setDuration(ANIM_MS)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setMaximumHeight(0)

    # -- content -----------------------------------------------------------

    def set_body(self, text: str, *, tone: CardTone | None = None) -> None:
        """Replace the body text and grow (or shrink) to fit.

        Args:
            text: New body text.
            tone: New colour treatment, if it changed.
        """
        if tone is not None:
            self.tone = tone
        self._body.setText(text)
        self.update()
        self.grow_to_fit()
        self.content_changed.emit()

    def set_meta(self, text: str) -> None:
        """Show a dim footer line (``52 s, 10 steps, 8¢``) and grow to fit.

        Args:
            text: The line; empty hides it.
        """
        self._meta.setText(text)
        self._meta.setVisible(bool(text))
        self.grow_to_fit()

    def set_reply_state(self, state: Literal["open", "replying", "replied"]) -> None:
        """What a ``nudge`` card's Reply says: ``Reply``, ``Replying…`` or ``Replied``."""
        if self.reply_button is None:
            return
        self.reply_button.setText({"open": "Reply", "replying": "Replying…", "replied": "Replied"}[state])
        self.reply_button.setEnabled(state == "open")

    def wanted_height(self) -> int:
        """Height the card needs for its current text at its current width."""
        width = self.width() or (PANEL_WIDTH - 2 * PAD)
        return self.layout().minimumHeightForWidth(width)

    def resizeEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().resizeEvent(event)  # type: ignore[arg-type]
        # A new width (the scrollbar came or went) rewraps the text: follow it, once the
        # grow animation is not the one setting the height.
        if self._grow.state() != QPropertyAnimation.State.Running and self.maximumHeight() > 0:
            target = self.wanted_height()
            if target != self.maximumHeight():
                self.setMaximumHeight(target)

    # -- animation ---------------------------------------------------------

    def reveal(self) -> None:
        """Fade and grow the card in from nothing."""
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.grow_to_fit()

    def grow_to_fit(self) -> None:
        """Animate the height to whatever the current text needs."""
        target = self.wanted_height()
        current = self.maximumHeight()
        self._grow.stop()
        self._grow.setStartValue(current if current < 16777215 else target)
        self._grow.setEndValue(target)
        self._grow.start()

    def dismiss(self) -> None:
        """Fade and shrink away, then delete the card."""
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        self._grow.stop()
        self._grow.setStartValue(self.maximumHeight())
        self._grow.setEndValue(0)
        self._grow.finished.connect(self.deleteLater)
        self._grow.start()

    @property
    def height_animation(self) -> QPropertyAnimation:
        """The height animation, so the window can follow it frame by frame."""
        return self._grow

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        error = self.tone == "error"
        painter.setBrush(ERROR_BG if error else CARD_BG)
        painter.setPen(QPen(ERROR_BORDER if error else CARD_BORDER, 1.0))
        painter.drawRoundedRect(rect, 8, 8)


class ResChip(QWidget):
    """A resolved chip (the design's ``.res``): ``who Sam`` / ``where WhatsApp``: a 30 px pill, a mono key in text-3,
    the value in 600. (No swap key: the agent acts at once, there is no plan to change yet.)"""

    def __init__(self, key: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from yuki.ui import lattice as L

        self.key, self.value = key, value
        fk, fv = L.mono_font(10.5), L.ui_font(13.5, 600)
        from PySide6.QtGui import QFontMetricsF

        w = 11 + QFontMetricsF(fk).horizontalAdvance(key) + 6 + QFontMetricsF(fv).horizontalAdvance(value) + 8 + 2
        self.setFixedSize(int(round(w)) + 1, 30)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtGui import QFontMetricsF, QPainterPath

        from yuki.ui import lattice as L

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, L.RAISE)
        p.setPen(QPen(L.HAIR2, 1.0))
        p.drawPath(path)
        fk, fv = L.mono_font(10.5), L.ui_font(13.5, 600)
        x = 12.0
        p.setFont(fk)
        p.setPen(L.TEXT3)
        kw = QFontMetricsF(fk).horizontalAdvance(self.key)
        p.drawText(QRectF(x, 0, kw + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.key)
        x += kw + 6
        p.setFont(fv)
        p.setPen(L.TEXT)
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignmentFlag.AlignVCenter, self.value)
        p.end()


class IntentCard(QWidget):
    """The Intent card inside the ask bar (the design's ``.intent``: a clearing framed by four crop marks).

    * **Unsent** (an offer put words in the ask bar): "In the ask bar · not sent", that nothing is sent until Enter,
      and Send / Clear, with where the words came from;
    * **Resolved** (a typed request whose task frame named a person): "Resolved from memory", the request, its
      chips (who, where) and **because** (where the frame found them, and how long ago).
    """

    send = Signal()
    clear = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from yuki.ui import lattice as L

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 16)
        col.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = L.Icon("key", 10, L.TEXT3, self)
        head.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self, max_lines=1)
        head.addWidget(self.label, 1)
        col.addLayout(head)
        self.title = L.Para("", font=L.ui_font(19, 600), line_height=1.45, parent=self)
        self.title_gap = QWidget(self)
        self.title_gap.setFixedHeight(10)
        col.addWidget(self.title_gap)
        col.addWidget(self.title)
        self.text = L.Para("", font=L.ui_font(14), color=L.TEXT2, line_height=1.45, parent=self)
        self.text_gap = QWidget(self)
        self.text_gap.setFixedHeight(8)
        col.addWidget(self.text_gap)
        col.addWidget(self.text)
        grid = QHBoxLayout()
        grid.setContentsMargins(0, 16, 0, 0)
        grid.setSpacing(14)
        self.dt = L.Para("Resolved", font=L.mono_font(11), color=L.TEXT3, line_height=24 / 11, parent=self)
        self.dt.setFixedWidth(78)
        grid.addWidget(self.dt, 0, Qt.AlignmentFlag.AlignTop)
        dd = QVBoxLayout()
        dd.setSpacing(8)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        self.chips_row.addStretch(1)
        dd.addLayout(self.chips_row)
        self.because = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.45, parent=self)
        dd.addWidget(self.because)
        grid.addLayout(dd, 1)
        self.grid_box = QWidget(self)
        self.grid_box.setLayout(grid)
        col.addWidget(self.grid_box)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 18, 0, 0)
        foot.setSpacing(8)
        self.k_send = L.Key("Send", variant="pri", icon="up", parent=self)
        self.k_clear = L.Key("Clear", variant="ghost", parent=self)
        self.k_send.clicked.connect(self.send.emit)
        self.k_clear.clicked.connect(self.clear.emit)
        foot.addWidget(self.k_send)
        foot.addWidget(self.k_clear)
        foot.addStretch(1)
        self.source = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self, max_lines=1)
        foot.addWidget(self.source, 0, Qt.AlignmentFlag.AlignVCenter)
        self.foot_box = QWidget(self)
        self.foot_box.setLayout(foot)
        col.addWidget(self.foot_box)
        self._chips: list[QWidget] = []
        self.mode = ""

    def show_unsent(self, source: str = "") -> None:
        self.mode = "unsent"
        self.icon.name = "key"
        self.label.set_text("In the ask bar · not sent")
        self.title.setVisible(False)
        self.title_gap.setVisible(False)
        self.text.set_text("I put it here for you. Nothing is sent until you press Enter, and you can change any "
                           "word first.")
        self.text.setVisible(True)
        self.text_gap.setVisible(True)
        self.grid_box.setVisible(False)
        self.foot_box.setVisible(True)
        self.source.set_text(source)
        self.source.setFixedWidth(int(self.source.natural_width()) + 2)
        self.show()

    def show_resolved(self, request: str, frame: dict[str, Any]) -> bool:
        """The frame's first person and their usual route as chips; ``False`` when the frame named nobody."""
        persons = [p for p in (frame or {}).get("persons") or [] if isinstance(p, dict) and p.get("name")]
        if not persons:
            return False
        person = persons[0]
        routes = [r for r in person.get("routes") or [] if isinstance(r, dict)]
        self.mode = "resolved"
        self.icon.name = "memory"
        self.label.set_text("Plan · resolved from memory")
        self.title.set_text(request)
        self.title.setVisible(bool(request))
        self.title_gap.setVisible(bool(request))
        self.text.setVisible(False)
        self.text_gap.setVisible(False)
        for c in self._chips:
            c.deleteLater()
        self._chips = [ResChip("who", str(person["name"]), self)]
        if routes and (routes[0].get("label") or routes[0].get("app")):
            self._chips.append(ResChip("where", str(routes[0].get("label") or routes[0].get("app")), self))
        for k, c in enumerate(self._chips):
            self.chips_row.insertWidget(k, c)
        mine = routes[0].get("mine") if routes else None
        because = []
        where = (frame or {}).get("where")
        if where:
            because.append(str(where))
        if isinstance(mine, (int, float)) and mine > 0:
            because.append(f"you sent {int(mine)} messages there")
        self.because.set_text(("because " + ", and ".join(because)) if because else "")
        if because:
            self.because.set_text("because " + ", and ".join(because),
                                  [__import__("yuki.ui.lattice", fromlist=["Span"]).Span(0, 7, color=None, weight=500)])
        self.because.setVisible(bool(because))
        self.grid_box.setVisible(True)
        self.foot_box.setVisible(False)
        self.show()
        return True

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return self.layout().totalHeightForWidth(w)

    def paintEvent(self, _e: object) -> None:  # noqa: D102 - a clearing: four crop marks, nothing else
        from yuki.ui import lattice as L

        p = QPainter(self)
        L.paint_crop_marks(p, QRectF(self.rect()))
        p.end()


class Overlay(SurfaceWindow):
    """The ask bar in the Lattice design (phase E9): 780 px, 64 px from the top of the monitor in use, the field colour
    with a ``#2E3135`` border and 22 px corners. On top the input row (the face at 4 px, your words at 19 px, Speak and
    the shortcut), then the Intent card when there is one (words an offer put here unsent; a request's resolved chips),
    the thread (the live chat, in the design's turns), and a foot of key hints with the call's timer.

    The hotkey while a run holds the desk shows the small :class:`SpareBar` instead, without the keyboard, and this bar
    stays closed until the user types or clicks (:meth:`toggle`, :meth:`desk_held`, :attr:`passive`); a submit during a
    run gives the keyboard back (:attr:`lent`). See the module docstring.

    Signals:
        submitted: Text the user entered. The controller decides whether that is a
            new request or the answer to a pending question -- the overlay does not
            interpret it.
        dismissed: The overlay was closed by Esc, by a click elsewhere, or by the
            hotkey.
        opened: The overlay came up (from hidden or fading out; not a refocus
            of an overlay already open).
        do_nudge: ``(nudge)`` -- Do it on a card with an offer in the thread.
        speak: Speak (a voice conversation).
    """

    SHADOW = 0
    submitted = Signal(str)
    dismissed = Signal()
    opened = Signal()
    do_nudge = Signal(object)
    speak = Signal()

    def __init__(self) -> None:
        super().__init__(activates=True, radius=22)
        self.setObjectName("YukiOverlay")
        from yuki.ui import lattice as L
        from yuki.ui.dots import FaceWidget

        #: Thread mode (attach_thread): the live chat thread, and the stand-ins handed out for cards.
        self.thread: Any = None
        self._thread_model: Any = None
        self._thread_emit: Any = lambda ev: None
        self._proxies: list[ThreadCard] = []
        #: The call's ``(timer, app)`` for the foot, or None (the tray gives it: the island's clock).
        self.call_line: Any = lambda: None
        #: Whether a run the user paused (their takeover) is waiting: it holds nothing, so the hotkey opens the ask bar
        #: with the keyboard for their words. The tray gives it (``runtime.paused_id() is not None``).
        self.desk_paused: Callable[[], bool] = lambda: False
        #: Makes the hook that holds the user's keys for the bar while it shows without the keyboard.
        self.key_claim_factory: Callable[[], Any] = make_key_claim
        #: The small bar the hotkey shows while a run holds the desk (made on first use), and why it shows (a
        #: :func:`run_holds_desk` code), or None.
        self.spare_bar: SpareBar | None = None
        self._spare: str | None = None
        #: Why the ask bar is lent back (on screen without the keyboard after a submit during a run), or None.
        self._lent: str | None = None
        #: While either shows: since when (monotonic), since when no run holds the desk (None: one does), the user's
        #: last click already looked at, the key claim, and a claim under way (``key`` / ``click``) with its start.
        self._passive_t0 = 0.0
        self._free_since: float | None = None
        self._click_seen = 0.0
        self._key_claim: Any = None
        self._claiming: str | None = None
        self._claim_t0 = 0.0
        #: A press's input stamp as it arrived on the ask bar's window (lent): ``(when, stamped)``.
        self._native_press: tuple[float, bool] | None = None
        #: The window that was in front before the bar (where a run works, or where the user was): the keyboard goes
        #: back there when the bar closes while it holds the foreground. 0: none known.
        self._yield_to = 0
        self._presses_watched = False
        self._dismissing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        # the input row (the design's .ab-in): 64 px, 18 px in, the face, the words, Speak, the shortcut
        self.top_row = QWidget(self)
        self.top_row.setMinimumHeight(64 - 1)
        row = QHBoxLayout(self.top_row)
        row.setContentsMargins(18, 0, 12, 0)
        row.setSpacing(14)
        self.face = FaceWidget(4, self.top_row, blink=True)
        self.face.set_state("listening")
        row.addWidget(self.face, 0, Qt.AlignmentFlag.AlignVCenter)
        self.input = AskInput(self.top_row)
        self.input.setFont(L.ui_font(19))
        self.input.setPlaceholderText(ASK_LINE)
        self.input.submitted.connect(self._on_submit)
        self.input.escaped.connect(self.dismiss)
        self.input.wants_height.connect(self._on_input_height)
        self.input.textChanged.connect(self._on_text)
        row.addWidget(self.input, 1, Qt.AlignmentFlag.AlignVCenter)
        self.speak_key = L.Key("Speak", size="sm", icon="mic", parent=self.top_row)
        self.speak_key.clicked.connect(self.speak.emit)
        row.addWidget(self.speak_key, 0, Qt.AlignmentFlag.AlignVCenter)
        #: The shortcut that opens the ask bar, as a key hint ("Alt Shift").
        self.hint = L.Kbd("Alt Shift", self.top_row)
        row.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.top_row)
        # the Intent card (the design's .intent: 16 px under the input, 18 px in)
        self.intent_box = QWidget(self)
        ib = QVBoxLayout(self.intent_box)
        ib.setContentsMargins(18, 16, 18, 0)
        self.intent = IntentCard(self.intent_box)
        self.intent.send.connect(lambda: self._on_submit(self.input.toPlainText().strip())
                                 if self.input.toPlainText().strip() else None)
        self.intent.clear.connect(self._clear_unsent)
        ib.addWidget(self.intent)
        self.intent_box.setVisible(False)
        outer.addWidget(self.intent_box)
        # the thread (the design's .ab-thread: a label, then the turns)
        self.thread_head = L.Para("Earlier in this thread · today", font=L.mono_font(11), color=L.TEXT3,
                                  line_height=1.2, parent=self, max_lines=1)
        self.thread_head.setContentsMargins(0, 0, 0, 0)
        head_box = QWidget(self)
        hb = QVBoxLayout(head_box)
        hb.setContentsMargins(18, 18, 18, 4)
        hb.addWidget(self.thread_head)
        self.head_box = head_box
        head_box.setVisible(False)
        outer.addWidget(head_box)
        self.thread_box = QWidget(self)
        self.thread_lay = QVBoxLayout(self.thread_box)
        self.thread_lay.setContentsMargins(18, 0, 18, 10)
        self.thread_lay.setSpacing(0)
        outer.addWidget(self.thread_box)
        # the card stack (the older non-thread mode) scrolls in the same place
        self._content = QWidget()
        self._content.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(0, 0, 0, 0)
        self._cards.setSpacing(8)
        self._scroll = QScrollArea(self)
        self._scroll.setWidget(self._content)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setStyleSheet(SCROLL_STYLE)
        from yuki.ui.window.widgets import smooth

        smooth(self._scroll)
        self._scroll.setFixedHeight(0)
        self._scroll.setVisible(False)
        self.thread_lay.addWidget(self._scroll)
        self._follow = True
        self._forwarding_wheel = False
        bar = self._scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_scroll_range)
        bar.valueChanged.connect(self._on_scroll_value)
        # the foot (the design's .ab-foot): key hints, the call's timer on the right
        self.foot = QWidget(self)
        self.foot.setFixedHeight(10 + 13 + 10)
        fr = QHBoxLayout(self.foot)
        fr.setContentsMargins(18, 0, 18, 0)
        fr.setSpacing(18)
        self.foot_keys: list[tuple[Any, Any]] = []
        for key, words in (("Enter", "go"), ("Tab", "swap a chip"), ("Esc", "close")):
            pair = QHBoxLayout()
            pair.setSpacing(6)
            k = L.Kbd(key, self.foot)
            w = L.Para(words, font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self.foot, max_lines=1)
            w.setFixedSize(int(w.natural_width()) + 2, 13)
            pair.addWidget(k, 0, Qt.AlignmentFlag.AlignVCenter)
            pair.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
            fr.addLayout(pair)
            self.foot_keys.append((k, w))
        fr.addStretch(1)
        self.call_icon = L.Icon("rec", 11, L.TEXT3, self.foot)
        fr.addWidget(self.call_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.call_text = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self.foot,
                                max_lines=1)
        fr.addWidget(self.call_text, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.foot)
        self._call_tick = QTimer(self)
        self._call_tick.setInterval(1000)
        self._call_tick.timeout.connect(self._update_call)
        self._unsent_source = ""

        self._input_effect = None
        self.input.setMaximumHeight(self.input.preferred_height())
        self.resize(PANEL_WIDTH + 2 * self.SHADOW, self.layout().sizeHint().height())

        #: True for :data:`FOCUS_GRACE_MS` after :meth:`open`: a deactivation then
        #: is the focus hand-over settling, not the user leaving.
        self._focus_grace = False
        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.setInterval(FOCUS_GRACE_MS)
        self._grace_timer.timeout.connect(self._end_focus_grace)
        self._spare_timer = QTimer(self)
        self._spare_timer.setInterval(SPARE_TICK_MS)
        self._spare_timer.timeout.connect(self._spare_tick)
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(SPARE_GUARD_MS)
        self._guard_timer.timeout.connect(self._guard_tick)
        self.restyle()

    # -- look (the Lattice ask bar) -------------------------------------------------------------------------

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (theme changed)
        if not hasattr(self, "input"):
            return
        from yuki.ui import lattice as L
        from yuki.ui import ui_settings
        from yuki.ui.window.widgets import combo_words

        combo = combo_words(ui_settings.current().open_hotkey or "alt+shift")
        self.hint.text = combo.replace(" + ", " ")
        self.hint.setFixedSize(L.kbd_size(self.hint.text))
        self.hint.update()
        live = L.live()
        self.input.setStyleSheet(
            "QPlainTextEdit { background: transparent;"
            f" color: {L.css(L.TEXT)};"
            f" selection-background-color: {L.css(live)}; selection-color: {L.css(L.live_ink())};"
            " border: none; padding: 0px; }")
        pal = self.input.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, L.TEXT3)
        self.input.setPalette(pal)
        if self.thread is not None:
            self.thread.restyle()

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        from PySide6.QtGui import QPainterPath

        from yuki.ui import lattice as L

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 21.5, 21.5)
        painter.fillPath(path, L.BG)
        painter.setPen(QPen(L.ASKBAR_BORDER, 1.0))
        painter.drawPath(path)
        y = self.top_row.geometry().bottom() + 1
        painter.fillRect(QRectF(1, y, self.width() - 2, 1), L.HAIR)    # under the input row
        fy = self.foot.geometry().top()
        painter.fillRect(QRectF(1, fy, self.width() - 2, 1), L.HAIR)   # over the foot
        painter.end()

    def _on_text(self) -> None:
        """Words an offer put here stay "not sent" until Enter; the face listens while the field is empty."""
        empty = not self.input.toPlainText().strip()
        # the design's face listens in the ask bar, and rests while an offer's words wait unsent
        self.face.set_state("idle" if (self.intent.mode == "unsent" and not empty) else "listening")
        if empty and self.intent.mode == "unsent":
            self.intent.hide()
            self.intent.mode = ""
            self.intent_box.setVisible(False)
            self.fit()

    def show_unsent(self, source: str = "") -> None:
        """An offer's words are in the input, unsent: the Intent card says so (Send / Clear)."""
        self.intent.show_unsent(source)
        self.face.set_state("idle")
        self.intent_box.setVisible(True)
        self.foot_keys[0][1].set_text("send")
        self.fit()

    def show_resolved(self, request: str, frame: dict[str, Any]) -> None:
        """A request's task frame named a person: its chips (who, where, because) in the Intent card."""
        if self.intent.show_resolved(request, frame):
            self.intent_box.setVisible(True)
            self.fit()

    def clear_intent(self) -> None:
        self.intent.mode = ""
        self.intent.hide()
        self.intent_box.setVisible(False)
        self.foot_keys[0][1].set_text("go")
        self.fit()

    def _clear_unsent(self) -> None:
        self.input.clear()
        self.clear_intent()
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _update_call(self) -> None:
        line = None
        try:
            line = self.call_line()
        except Exception:
            line = None
        on = bool(line)
        self.call_icon.setVisible(on)
        self.call_text.setVisible(on)
        if on:
            timer, app = line
            self.call_text.set_text(f"{timer} on {app}" if app else str(timer))
            self.call_text.setFixedSize(int(self.call_text.natural_width()) + 4, 13)
            self.foot.layout().activate()

    def showEvent(self, e: object) -> None:  # noqa: D102 - the call's clock runs only while the ask bar is up
        super().showEvent(e)
        self._update_call()
        self._call_tick.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._call_tick.stop()
        self._drop_claim(self._end_lent("hidden"))   # the next open decides afresh
        set_through(self, False)

    # -- thread mode (the live chat thread) ------------------------------------

    def attach_thread(self, model: Any, emit: Any = None) -> None:
        """Show the live chat thread instead of the card stack (see the module docstring). ``emit``: how this
        overlay adds its own events to the thread (``WindowHost.emit``: applied here and sent to the open Yuki
        window, so its replica keeps the cards too; 42-window-live-refresh.md R2); ``model.apply`` by default."""
        if self.thread is not None:
            return
        from yuki.ui.chatview import ChatThreadView

        self._thread_model = model
        self._thread_emit = emit if callable(emit) else model.apply
        self.thread = ChatThreadView(model, self.theme, max_bubble=PANEL_WIDTH - 2 * PAD - 92,
                                     margins=(0, 0, 2, 0), look="lattice", parent=self.thread_box)
        self.thread.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.thread.content_resized.connect(self.fit)
        self.thread.reply_nudge.connect(self._on_thread_reply)
        self.thread.do_nudge.connect(self.do_nudge.emit)
        self._scroll.setVisible(False)
        self.thread_lay.insertWidget(0, self.thread)
        self.fit()

    def _on_thread_reply(self, nudge: object) -> None:
        card = self.nudge_card(nudge.get("id")) if isinstance(nudge, dict) else None
        if card is None and isinstance(nudge, dict):
            card = self._proxy("nudge", nudge=nudge)
        if card is not None:
            card.reply_clicked.emit(card.nudge)

    def _proxy(self, tone: str, *, nudge: dict[str, Any] | None = None, at: float | None = None) -> "ThreadCard":
        model = self._thread_model

        def reply_state(state: str) -> None:
            if nudge is not None and model is not None:
                self._thread_emit({"type": "nudge_state", "id": nudge.get("id"), "state": state})

        card = ThreadCard(tone, at=at, nudge=nudge, on_reply_state=reply_state)
        self._proxies.append(card)
        del self._proxies[:-100]
        return card

    # -- geometry ----------------------------------------------------------

    def anchor(self) -> QPoint:
        """Top-left to sit at: centred, 64 px from the top of the monitor in use (the design's ``.askbar``)."""
        from yuki.ui.island import active_screen

        screen = active_screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        width = PANEL_WIDTH + 2 * self.SHADOW
        x = area.left() + (area.width() - width) // 2
        y = area.top() + TOP_PX - self.SHADOW
        return QPoint(x, y)

    def max_cards_height(self) -> int:
        """Tallest the thread may be on the overlay's screen (:data:`MAX_CARDS_FRACTION`)."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        return max(120, int(area.height() * MAX_CARDS_FRACTION))

    def fit(self) -> None:
        """Size the thread to its content (capped), then the window to the layout. Keeps the top-left fixed."""
        if self.thread is not None:
            content = self.thread.content_height() if self._thread_model.items else 0
            stack = min(content, self.max_cards_height())
            if self.thread.height() != stack or self.thread.isVisible() != (stack > 0):
                self.thread.setFixedHeight(stack)
                self.thread.setVisible(stack > 0)
        else:
            content = self._cards.sizeHint().height() if self.cards() else 0
            stack = min(content, self.max_cards_height())
            if self._scroll.height() != stack or self._scroll.isVisible() != (stack > 0):
                self._scroll.setFixedHeight(stack)
                self._scroll.setVisible(stack > 0)
        self.head_box.setVisible(stack > 0)
        self.thread_box.setVisible(stack > 0)
        width = PANEL_WIDTH + 2 * self.SHADOW
        self.layout().activate()
        height = self.layout().totalHeightForWidth(width) if self.layout().hasHeightForWidth() else \
            self.layout().sizeHint().height()
        if (self.width(), self.height()) != (width, height):
            self.resize(width, height)
        self.update()

    # -- scrolling ---------------------------------------------------------

    def scroll_to_latest(self) -> None:
        """Follow the newest card again and bring the bottom of the stack into view."""
        if self.thread is not None:
            self.thread.scroll_to_latest()
        self._follow = True
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scroll_range(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self._scroll.verticalScrollBar().setValue(maximum)

    def _on_scroll_value(self, value: int) -> None:
        self._follow = value >= self._scroll.verticalScrollBar().maximum() - 2

    def scroll_range(self) -> tuple[int, int]:
        """``(value, maximum)`` of the stack's scrollbar (0, 0 when it fits)."""
        bar = self._scroll.verticalScrollBar()
        return bar.value(), bar.maximum()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: D102 - Qt override
        area = self.thread.area if self.thread is not None else self._scroll
        shown = self.thread.isVisible() if self.thread is not None else self._scroll.isVisible()
        if shown and not self._forwarding_wheel:
            self._forwarding_wheel = True
            try:
                from yuki.ui.window.widgets import smooth

                smooth(area).wheel(event)
            finally:
                self._forwarding_wheel = False
            event.accept()
            return
        super().wheelEvent(event)

    def _on_input_height(self, height: int) -> None:
        """Follow the input's own growth as the user types onto a new line."""
        if self.input.maximumHeight() > 0:
            self.input.setMaximumHeight(height)
            self.top_row.setMinimumHeight(max(63, height + 22))
            self.fit()

    # -- show / hide -------------------------------------------------------

    def focus_target(self) -> QWidget:  # noqa: D102 - SurfaceWindow hook
        return self.input

    def desk_held(self, *, waiting: bool = False) -> str | None:
        """Why a run holds the desk now (:func:`run_holds_desk` over this bar's thread), or None. A run the user paused
        (:attr:`desk_paused`) holds nothing when opening (their words resume it); a submit (``waiting``) counts it, as
        their words set it acting again. A check that fails says None: the bar then behaves as it always did."""
        try:
            if not waiting and self.desk_paused():
                return None
            return run_holds_desk(self._thread_model, waiting=waiting)
        except Exception:  # noqa: BLE001
            return None

    @property
    def passive(self) -> bool:
        """The small bar shows in place of this one, without the keyboard (this window itself is hidden then, so
        ``isVisible()`` already says no ask bar is open)."""
        return self._spare is not None

    @property
    def lent(self) -> bool:
        """This bar is open, unfolded, but has given the keyboard back to the window a run works in."""
        return self._lent is not None

    def open(self, *, spare_run: bool = False) -> None:
        """Show the overlay (or just refocus it) with the input ready to type in (the grace period is armed before
        anything is shown, so the hand-over's activation messages never read as "clicked away").

        ``spare_run`` (the hotkey, :meth:`toggle`): if a run holds the desk as the bar comes up (:meth:`desk_held`),
        the small :class:`SpareBar` shows instead, without the keyboard, and this bar stays closed (no ``opened``). Every
        other caller (a question, a click on the island or a card, the tray, the window) takes the keyboard as before;
        the small bar then goes, and keys the user typed at it are typed into this one."""
        coming_up = not self.isVisible() or self.closing
        if spare_run and coming_up and self._spare is None:
            why = self.desk_held()
            if why:
                self._show_spare(why)
                return
        claim = self._close_spare("opened", keep=True) if self._spare is not None else self._end_lent("opened")
        set_through(self, False)
        self._focus_grace = True
        self._grace_timer.start()
        self.expand_input()
        self.fit()
        self.fade_in(self.anchor())
        self._watch_presses()
        if claim is not None:
            self._finish_claim(claim)
        if coming_up:
            self.opened.emit()

    def dismiss(self, why: str = "") -> None:
        """Hide the overlay and tell the controller it is gone. Holding the foreground, it first hands it straight
        back to the window that was in front before it (:meth:`_give_back`; first, so "Reduce motion", which hides at
        once, gives it back too). ``why``: for the log lines only."""
        if not self.isVisible() or self.closing or self._dismissing:
            return
        self._dismissing = True   # the give-back's own deactivation must not dismiss a second time
        try:
            self._drop_claim(self._end_lent(why or "close"))
            self._give_back(why or "close")
            self.fade_out()
        finally:
            self._dismissing = False
        self.dismissed.emit()

    def toggle(self) -> None:
        """The hotkey: close the small bar or this one if either shows, else open (sparing a run that holds the desk:
        :meth:`open`)."""
        if self._spare is not None:
            self._close_spare("hotkey")
        elif self.isVisible() and not self.closing:
            self.dismiss("hotkey")
        else:
            self.open(spare_run=True)

    def changeEvent(self, event: QEvent) -> None:  # noqa: D102 - Qt override
        super().changeEvent(event)
        if event.type() != QEvent.Type.ActivationChange or not self.isVisible():
            return
        if self._lent is not None:
            # lent back, pinned no-activate: the user's click or typing takes the keyboard through _claim, before any
            # activation. Becoming active here is somebody else's doing (Windows picking the next window as the run
            # closes one): the keyboard goes straight back; with nowhere to give it, the bar is the ask bar as usual.
            # Losing an activation it never had is nothing.
            if self.isActiveWindow() and self._claiming is None and self._give_back("not_clicked") == "nowhere":
                self._drop_claim(self._end_lent("nowhere"))
            return
        if not self.isActiveWindow() and not self._focus_grace:
            self.dismiss()

    def _watch_presses(self) -> None:
        """Watch the bar's native window for mouse presses (once): every press anywhere on the bar reaches the window
        before the child under it, so the bar lent back can take the keyboard again on the user's click."""
        handle = self.windowHandle()
        if handle is not None and not self._presses_watched:
            handle.installEventFilter(self)
            self._presses_watched = True

    def nativeEvent(self, event_type: Any, message: Any) -> Any:  # noqa: N802, D102 - a press's input stamp (lent)
        if self._lent is not None:
            stamped = native_press(message)
            if stamped is not None:
                self._native_press = (time.monotonic(), stamped)
        return False, 0

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: D102 - Qt override
        if (
            self._lent is not None
            and event.type() == QEvent.Type.MouseButtonPress
            and watched is self.windowHandle()
            and not self.closing
        ):
            by_yuki, self._native_press = press_was_yuki(self._native_press), None
            if by_yuki:
                self._yuki_press()
            else:
                self._claim("click")
        return super().eventFilter(watched, event)   # the press goes on to the child (a caret, Speak) as usual

    def _end_focus_grace(self) -> None:
        """Close the grace window; if the hand-over did not stick, try once more (without the injected key-up)."""
        self._focus_grace = False
        if self.isVisible() and not self.closing and self._lent is None and not self.isActiveWindow():
            self.take_focus(reason="grace_end", allow_unlock=False)

    def take_focus(self, *, reason: str, allow_unlock: bool = True) -> FocusResult:
        """:meth:`SurfaceWindow.take_focus`, except while the bar is lent back to a run (then nothing is taken, and
        the ``focus_path`` record says so: ``path`` ``spare_run``). Remembers the window that was in front before
        (:attr:`_yield_to`)."""
        if self._lent is not None:
            own, front = self._hwnd(), foreground_window()
            self.focus_path.emit({"window": self.objectName(), "reason": reason, "path": "spare_run",
                                  "why": self._lent, "ok": False, "hwnd": own})
            return FocusResult(ok=False, path="spare_run", hwnd=own, foreground_before=front, foreground_after=front,
                               attached=False, attach_error=0, set_foreground_returned=[], injected=0,
                               elapsed_ms=0.0)
        result = super().take_focus(reason=reason, allow_unlock=allow_unlock)
        before = int(result.foreground_before or 0)
        if before and not self._is_own(before):
            self._yield_to = before
        return result

    # -- sparing a run (a bar without the keyboard) -----------------------------------------------------------------

    def _hwnd(self) -> int:
        try:
            return int(self.winId())
        except Exception:  # noqa: BLE001 - gone
            return 0

    def _is_own(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        spare = self.spare_bar
        try:
            if spare is not None and int(hwnd) == int(spare.winId()):
                return True
        except Exception:  # noqa: BLE001
            pass
        return int(hwnd) == self._hwnd() or window_is_ours(int(hwnd))

    def _passive_window(self) -> QWidget | None:
        if self._spare is not None:
            return self.spare_bar
        return self if self._lent is not None else None

    def _show_spare(self, why: str) -> None:
        """The hotkey while a run holds the desk: the small bar, no foreground hand-over, the window in front kept as
        the one the keyboard goes back to, and the claim on the user's keys armed (not over a game)."""
        if self.spare_bar is None:
            self.spare_bar = SpareBar()
            self.spare_bar.pressed.connect(self._on_spare_press)
        bar = self.spare_bar
        front = foreground_window()
        if front and not self._is_own(front):
            self._yield_to = front
        self._spare = why
        bar.set_line(SPARE_LINE)
        bar.face.set_state("idle")
        set_through(bar, False)
        anchor = self.anchor()
        bar.fade_in(QPoint(anchor.x() + (PANEL_WIDTH + 2 * self.SHADOW - bar.width()) // 2, anchor.y()))
        claim = self._begin_passive()
        self.focus_path.emit({"window": self.objectName(), "reason": "hotkey", "path": "spare_run", "why": why,
                              "ok": False, "hwnd": self._hwnd(), "foreground_before": front,
                              "foreground_after": front, "claim": claim})

    def _close_spare(self, why: str, *, keep: bool = False) -> Any:
        """The small bar goes (``opened``/``claimed``: at once, the ask bar comes up in its place; otherwise it fades,
        letting clicks through on its way out). ``keep``: the key claim is handed back to the caller (to type its keys
        into the ask bar), else dropped."""
        if self._spare is None:
            return None
        self._spare = None
        claim = self._end_passive()
        bar = self.spare_bar
        if bar is not None:
            if why in ("opened", "claimed"):
                bar.close_now()
            else:
                bar.fade_out()
        self.focus_path.emit({"window": self.objectName(), "reason": "spare_close", "why": why, "path": "spare_run",
                              "ok": True})
        if keep:
            return claim
        self._drop_claim(claim)
        return None

    def _lend(self, why: str) -> bool:
        """After a submit during a run (not words for the run itself): the keyboard goes straight back to the window
        the run works in, and the bar stays on screen, unfolded, pinned no-activate, so its answer shows there. False
        when there is no window to give it to (the bar then stays the ask bar as usual)."""
        if self._lent is not None:
            return True
        self._lent = why   # first: the give-back's own deactivation must not dismiss it
        self._focus_grace = False
        self._grace_timer.stop()
        if self._give_back("submit_during_run") == "nowhere":
            self._lent = None
            return False
        own = self._hwnd()
        if own:
            set_no_activate(own, True)
        self._watch_presses()
        self.face.set_state("idle")
        self.input.setPlaceholderText(SPARE_LINE)
        claim = self._begin_passive()
        self.focus_path.emit({"window": self.objectName(), "reason": "lend", "path": "spare_run", "why": why,
                              "ok": True, "claim": claim})
        return True

    def _end_lent(self, why: str) -> Any:
        """The bar lent back is the ask bar again (it takes the keyboard, or it is going): the pin comes off, clicks
        are taken again. The key claim goes back to the caller (None when there was none)."""
        if self._lent is None:
            return None
        self._lent = None
        claim = self._end_passive()
        own = self._hwnd()
        if own:
            set_no_activate(own, False)
        set_through(self, False)
        self.face.set_state("listening")
        self.input.setPlaceholderText(ASK_LINE)
        self.focus_path.emit({"window": self.objectName(), "reason": "lend_end", "why": why, "path": "spare_run",
                              "ok": True})
        return claim

    def _begin_passive(self) -> str:
        """Start watching while a bar shows without the keyboard: its line and life (:meth:`_spare_tick`), Yuki's
        clicks and the user's (:meth:`_guard_tick`), and the claim on the user's keys. Says how keys are claimed:
        ``hook``, ``game`` (a game in front: its keys are never held back) or ``none`` (no hook: a click still works)."""
        self._passive_t0 = time.monotonic()
        self._free_since = None
        self._claiming = None
        self._click_seen = user_click_at()
        self._spare_timer.start()
        self._guard_timer.start()
        if self._key_claim is not None:
            return "hook"
        if game_in_front():
            return "game"
        try:
            claim = self.key_claim_factory()
            claim.setParent(self)   # Qt keeps the thread object until it has finished, then deletes it
            claim.finished.connect(claim.deleteLater)
            # bound methods of this GUI-thread object: Qt queues the hook thread's signals across to it
            claim.claimed.connect(self._on_claim_key)
            claim.escaped.connect(self._on_claim_escape)
            claim.failed.connect(self._on_claim_failed)
            claim.start()
            self._key_claim = claim
            return "hook"
        except Exception:  # noqa: BLE001 - not on Windows: a click still claims it
            return "none"

    def _end_passive(self) -> Any:
        """Stop watching (the bar showing without the keyboard is gone or takes it); the key claim is handed back."""
        self._spare_timer.stop()
        self._guard_timer.stop()
        self._claiming = None
        claim, self._key_claim = self._key_claim, None
        return claim

    def _drop_claim(self, claim: Any) -> None:
        """End a key claim without typing what it held back (the bar went without becoming the ask bar)."""
        if claim is None:
            return
        try:
            claim.release(replay=False)
            claim.stop()
        except RuntimeError:   # its thread already ended (no hook) and Qt deleted it
            pass

    def _finish_claim(self, claim: Any, tries: int = 0) -> None:
        """The ask bar came up after a claim: once it holds the keyboard, the keys held back are typed into it, with
        the Shift and Ctrl they had; if it doesn't, once more after the grace's own retry, then they are dropped --
        never sent on to the window they were not for."""
        own = self._hwnd()
        if own and foreground_window() == own:
            try:
                sent = claim.release(replay=True)
                claim.stop()
            except RuntimeError:   # its thread already ended (no hook) and Qt deleted it
                return
            if sent:
                self.focus_path.emit({"window": self.objectName(), "reason": "typed", "path": "replay", "ok": True,
                                      "keys": int(sent)})
            return
        if tries == 0 and self.isVisible() and not self.closing:
            QTimer.singleShot(FOCUS_GRACE_MS + 40, lambda: self._finish_claim(claim, 1))
            return
        self._drop_claim(claim)
        self.focus_path.emit({"window": self.objectName(), "reason": "typed", "path": "replay", "ok": False,
                              "keys": 0})

    def _live_claim_sent(self) -> bool:
        """The signal being handled came from the key claim of the bar showing now (not one already ended)."""
        return self._key_claim is not None and self.sender() is self._key_claim

    def _on_claim_key(self) -> None:
        if self._live_claim_sent():
            self._claim("key")

    def _on_claim_failed(self, _reason: str) -> None:
        self.focus_path.emit({"window": self.objectName(), "reason": "claim_hook", "path": "failed", "ok": False})

    def _on_claim_escape(self) -> None:
        if not self._live_claim_sent():
            return
        if self._spare is not None:
            self._close_spare("esc")
        elif self._lent is not None:
            self.dismiss("esc")

    def _on_spare_press(self, by_yuki: bool) -> None:
        if self._spare is None:
            return
        if by_yuki:
            self._yuki_press()
        else:
            self._claim("click")

    def _yuki_press(self) -> None:
        """Yuki's own click landed on a bar showing without the keyboard (it came before the guard saw its input):
        never the user's. From now that bar lets clicks through (the guard keeps it so while Yuki's hands work); the
        small bar steps out of the run's way."""
        window = self._passive_window()
        if window is not None:
            set_through(window, True)
        self.focus_path.emit({"window": self.objectName(), "reason": "yuki_click", "path": "spare_run", "ok": True})
        if self._spare is not None:
            self._close_spare("yuki_click")

    def _claim(self, how: str) -> None:
        """The user typed at, or clicked, a bar showing without the keyboard: it becomes the ask bar with the keyboard.
        Yuki's own input under way finishes first (at most :data:`CLAIM_WAIT_S`), so a send is never split between
        two windows; the user's keys meanwhile are held back by the claim."""
        if self._claiming is not None or (self._spare is None and self._lent is None):
            return
        self._claiming = how
        self._claim_t0 = time.monotonic()
        self._claim_step()

    def _claim_step(self) -> None:
        how = self._claiming
        if how is None or (self._spare is None and self._lent is None):
            return   # it closed meanwhile
        if own_input_running() and time.monotonic() - self._claim_t0 < CLAIM_WAIT_S:
            QTimer.singleShot(20, self._claim_step)
            return
        was = self._spare or self._lent
        waited = int((time.monotonic() - self._claim_t0) * 1000)
        self.focus_path.emit({"window": self.objectName(), "reason": "clicked" if how == "click" else "typed",
                              "path": "user_click" if how == "click" else "user_keys", "ok": True, "was": was,
                              "waited_ms": waited})
        if self._spare is not None:
            claim = self._close_spare("claimed", keep=True)
            self.open()
        else:
            claim = self._end_lent("claimed")
            self._focus_grace = True
            self._grace_timer.start()
            self.take_focus(reason="clicked")
        if claim is not None:
            self._finish_claim(claim)

    def _spare_tick(self) -> None:
        """While a bar shows without the keyboard: its line follows whether a run still holds the desk, the window in
        front is the one the keyboard goes back to, and it closes by itself (the small bar :data:`SPARE_LIFE_S` after
        it came up, or :data:`SPARE_FREE_S` after the run let go; the bar lent back :data:`LENT_LINGER_S` after the
        run and the answer are done)."""
        if self._passive_window() is None:
            self._spare_timer.stop()
            return
        now = time.monotonic()
        if self._claiming is not None and now - self._claim_t0 > CLAIM_WAIT_S + 2.0:
            self._claiming = None   # a claim that never finished (it raised): the bar's own life rules apply again
        held = self.desk_held()
        front = foreground_window()
        if front and not self._is_own(front):
            self._yield_to = front
        if held:
            self._free_since = None
        elif self._free_since is None:
            self._free_since = now
        if self._spare is not None:
            if held:
                self._spare = held
            if self.spare_bar is not None:
                self.spare_bar.set_line(SPARE_LINE if held else SPARE_FREE_LINE)
            if self._claiming is not None:
                return
            if now - self._passive_t0 >= SPARE_LIFE_S:
                self._close_spare("timeout")
            elif self._free_since is not None and now - self._free_since >= SPARE_FREE_S:
                self._close_spare("free")
            return
        if held:
            self._lent = held
        line = SPARE_LINE if held else SPARE_FREE_LINE
        if self.input.placeholderText() != line:
            self.input.setPlaceholderText(line)
        if self._claiming is None and self._free_since is not None and now - self._free_since >= LENT_LINGER_S:
            self.dismiss("lent_done")

    def _guard_tick(self) -> None:
        """While a bar shows without the keyboard: it lets every click through while Yuki's own input runs or just
        ran (:data:`SPARE_GUARD_TAIL_S`), so Yuki's clicks reach what they aim at; and the user's click elsewhere (the
        input monitor's, never Yuki's own) means they moved on: the bar closes, as the ask bar does when clicked away
        from."""
        window = self._passive_window()
        if window is None:
            self._guard_timer.stop()
            return
        set_through(window, own_input_recent(SPARE_GUARD_TAIL_S))
        at = user_click_at()
        if at <= self._click_seen:
            return
        self._click_seen = at
        if at < self._passive_t0 or self._claiming is not None:
            return
        try:
            from PySide6.QtGui import QCursor

            if window.frameGeometry().contains(QCursor.pos()):
                return   # a press on the bar itself: its own handler takes it
        except Exception:  # noqa: BLE001
            return
        if self._spare is not None:
            self._close_spare("clicked_away")
        else:
            self.dismiss("clicked_away")

    def _give_back(self, why: str) -> str:
        """The bar holds the foreground and must not (it is closing, or it spares a run): the window that was in
        front before it gets the keyboard now (a run's next key lands where it works), not when the fade ends.

        Returns ``not_holding`` (the bar doesn't hold the foreground: the user clicked away, a run focused its
        window, or it never had it -- nothing to do), ``given``, or ``nowhere`` (that window is gone, minimised or
        Yuki's own, or Windows refused)."""
        own = self._hwnd()
        if not own or foreground_window() != own:
            return "not_holding"
        target = int(self._yield_to or 0)
        if not target or self._is_own(target) or not window_usable(target):
            self.focus_path.emit({"window": self.objectName(), "reason": "give_back", "why": why,
                                  "path": "nowhere", "ok": False})
            return "nowhere"
        ok = give_foreground(target)
        self.focus_path.emit({"window": self.objectName(), "reason": "give_back", "why": why,
                              "path": "set_foreground", "ok": ok, "hwnd": target})
        return "given" if ok else "nowhere"

    # -- cards -------------------------------------------------------------

    def cards(self) -> list[ReplyCard]:
        """The cards currently in the stack, oldest first."""
        if self.thread is not None:
            return list(self._proxies)  # type: ignore[arg-type]
        widgets = (
            self._cards.itemAt(index).widget() for index in range(self._cards.count())
        )
        return [widget for widget in widgets if isinstance(widget, ReplyCard)]

    def add_card(
        self,
        prompt: str,
        body: str = "",
        *,
        tone: CardTone = "reply",
        accent: QColor | None = None,
    ) -> ReplyCard:
        """Add a card at the bottom of the stack (thread mode: a stand-in; the thread shows the exchange)."""
        if self.thread is not None:   # the thread shows the exchange from the chat model
            self.scroll_to_latest()
            return self._proxy(tone)  # type: ignore[return-value]
        card = ReplyCard(prompt, body, tone=tone, accent=accent, parent=self._content)
        self._cards.addWidget(card)
        self._watch(card)
        card.show()
        card.reveal()
        self._trim()
        self.scroll_to_latest()
        return card

    def _watch(self, card: ReplyCard) -> None:
        """Resize with the card's growth, and bring a reply into view when it lands."""
        card.height_animation.valueChanged.connect(lambda *_: self.fit())
        card.content_changed.connect(self.scroll_to_latest)

    def add_nudge_card(
        self, nudge: dict[str, Any], label: str, *, accent: QColor | None = None, at: float
    ) -> ReplyCard | None:
        """Put one of memory's nudges in the stack, in time order (``None`` when the stack is full of newer cards)."""
        existing = self.nudge_card(nudge.get("id"))
        if existing is not None:
            return existing
        if self.thread is not None:
            self._thread_emit({"type": "nudge", "nudge": nudge, "at": at})
            self.scroll_to_latest()
            return self._proxy("nudge", nudge=nudge, at=at)  # type: ignore[return-value]
        cards = self.cards()
        index = next((i for i, c in enumerate(cards) if c.at > at), len(cards))
        if index == 0 and len(cards) >= MAX_CARDS:
            return None
        card = ReplyCard(
            label, " ".join(str(nudge.get("text") or "").split()), tone="nudge", accent=accent,
            at=at, nudge=nudge, parent=self._content,
        )
        self._cards.insertWidget(index, card)
        self._watch(card)
        card.show()
        card.reveal()
        self._trim()
        return card

    def nudge_cards(self) -> list[ReplyCard]:
        """The nudge cards in the stack, oldest first."""
        return [card for card in self.cards() if card.tone == "nudge" and card.nudge is not None]

    def nudge_card(self, nudge_id: Any) -> ReplyCard | None:
        """The card showing nudge ``nudge_id``, if it is in the stack."""
        if nudge_id is None:
            return None
        return next(
            (c for c in self.nudge_cards() if str(c.nudge.get("id")) == str(nudge_id)), None
        )

    def _trim(self) -> None:
        """Fade out the oldest cards until at most :data:`MAX_CARDS` remain."""
        cards = self.cards()
        for card in cards[: max(0, len(cards) - MAX_CARDS)]:
            self._cards.removeWidget(card)
            card.setParent(self._content)
            card.dismiss()

    def clear_cards(self) -> None:
        """Remove every card immediately."""
        if self.thread is not None:
            self._proxies.clear()
            return
        for card in self.cards():
            self._cards.removeWidget(card)
            card.deleteLater()
        self.fit()

    # -- input -------------------------------------------------------------

    def collapse_input(self) -> None:
        """After a submit: the field empties at once (the words are in the thread now); it stays in place."""
        self.input.clear()

    def expand_input(self) -> None:
        """A cleared, focused input (an offer's words put back with :meth:`show_unsent` after this)."""
        self.input.clear()
        self.clear_intent()
        self.input.setMaximumHeight(self.input.preferred_height())
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_submit(self, text: str) -> None:
        """Hand the text to the controller; the field empties. If a run held the desk before this submit (counting
        one waiting on this very answer, and one the user paused), the keyboard goes straight back to the window the
        run works in, so its next keys don't meet the bar in front: words that went to the run (an answer to its
        question, words for a paused run) close the bar; anything else (a question for the reading lane) leaves it on
        screen without the keyboard (:meth:`_lend`), so the answer shows in it. The request this submit starts itself
        is not that run (it is judged before the submit)."""
        held = self.desk_held(waiting=True)
        to_run = False
        if held:
            try:
                to_run = waiting_on_answer(self._thread_model) or bool(self.desk_paused())
            except Exception:  # noqa: BLE001 - unknown: the bar stays on screen, without the keyboard
                to_run = False
        self.collapse_input()
        self.clear_intent()
        self.submitted.emit(text)
        if held and self.isVisible() and not self.closing:
            if to_run:
                self.dismiss("submit_during_run")
            else:
                self._lend(held)


class ThreadCard(QObject):
    """What the card calls return in thread mode: the exchange itself is drawn from the chat model.

    It keeps the card interface the controller uses (``set_body``, ``set_meta``,
    ``set_reply_state``, ``reply_clicked``, ``nudge``, ``tone``, ``at``).
    """

    reply_clicked = Signal(object)
    content_changed = Signal()

    def __init__(self, tone: str, *, at: float | None = None, nudge: dict[str, Any] | None = None,
                 on_reply_state: Any = None) -> None:
        super().__init__()
        self.tone = tone
        self.at = at if at is not None else time.time()
        self.nudge = nudge
        self._on_reply_state = on_reply_state

    def set_body(self, text: str, *, tone: str | None = None) -> None:
        if tone is not None:
            self.tone = tone

    def set_meta(self, text: str) -> None:
        del text

    def set_reply_state(self, state: str) -> None:
        if self._on_reply_state is not None:
            self._on_reply_state(state)

    def dismiss(self) -> None:
        pass
