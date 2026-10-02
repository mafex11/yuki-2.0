"""Calls, "just my side": the consent card, the "remembering your side" indicator, and Stop.

The memory service (:mod:`yuki.memory.calls`) detects a call and writes a
``calls`` row; it sets the named auto-reset event
:data:`CALL_EVENT_NAME` whenever the call's state changes. :class:`CallWatcher`
waits on it (and looks every :data:`FALLBACK_POLL_S` anyway) and fetches
``call_status()``.

:class:`CallController` shows, bottom-right and never taking focus:

* :class:`CallConsentCard` while memory is asking - "Remember what you say in
  this call?" with Yes, No, Always for <app>, Never for <app>; when the output
  is not headphones it says recording only happens with headphones. Every
  press goes to ``answer_call`` exactly once; a card left alone goes away after
  the call's ``ask_timeout_s`` and is answered ``none`` (nothing recorded).
* :class:`CallIndicator`, a small pill, while memory records the user's side
  (or waits for headphones), out of the way of cards and full-screen apps.

The tray's "Stop remembering this call" calls :meth:`CallController.stop_call`; while
call audio waits for the deferred transcriber the tray shows "N calls waiting
to be remembered" (also on the Today panel) and offers "Erase waiting call
audio" (:meth:`CallController.erase_waiting`).
Nothing here reads, sends or shows anything that was said.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from yuki.agent.memory import TRAY_TIMEOUT_S, MemoryAccess, MemoryUnavailable
from yuki.ui.surface import SurfaceWindow
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

HEADPHONES_NOTE = "Only with headphones, so other people's voices aren't picked up."


def consent_text(call: dict[str, Any]) -> tuple[str, str, str]:
    """``(question, detail, headphones note or "")`` for a ``call_status()["call"]``."""
    app = str(call.get("app") or "this app")
    detail = ("Only your microphone, kept encrypted and turned into text on this PC later, "
              "when it's free. Nobody else is recorded.")
    note = ""
    if call.get("headphones") is False:
        output = call.get("form_factor") or "not headphones"
        note = f"{HEADPHONES_NOTE} Your sound is on {output} now, so I'd wait for headphones."
    return "Remember what you say in this call?", detail, note


def waiting_text(waiting: Any) -> str:
    """``"2 calls waiting to be remembered"`` for ``call_status()["waiting"]``; ``""`` when none."""
    n = int((waiting or {}).get("calls") or 0) if isinstance(waiting, dict) else 0
    if n <= 0:
        return ""
    return f"{n} call{'' if n == 1 else 's'} waiting to be remembered"


def indicator_text(call: dict[str, Any]) -> str:
    app = str(call.get("app") or "")
    if call.get("status") == "waiting_headphones":
        return f"Waiting for headphones, {app}" if app else "Waiting for headphones"
    return f"Remembering your side, {app}" if app else "Remembering your side"


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


class CallConsentCard(SurfaceWindow):
    """The consent card (Horizon): a live LED and "Discord started a call", the question, what is and is not
    recorded, No and Yes, and under a rule "Never for Discord" and "Always for Discord". Never takes focus.

    Signals:
        answered: ``(call, answer)`` -- ``yes`` | ``no`` | ``always`` | ``never``, or ``none``
            when it timed out untouched.
    """

    answered = Signal(object, str)

    def __init__(self) -> None:
        super().__init__(activates=False)
        self.setObjectName("YukiCallConsentCard")
        self.call: dict[str, Any] | None = None
        self.setFixedWidth(CARD_WIDTH + 2 * self.SHADOW)
        from yuki.ui.window.theme import variable_font
        from yuki.ui.window.widgets import Led

        column = QVBoxLayout(self)
        column.setContentsMargins(self.SHADOW + 20, self.SHADOW + 16, self.SHADOW + 20, self.SHADOW + 12)
        column.setSpacing(6)
        who = QHBoxLayout()
        who.setSpacing(6)
        self.led = Led(self.theme(), "on", 7, parent=self)
        who.addWidget(self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = QLabel("A call started", self)
        self.title.setFont(variable_font(12, "Regular"))
        who.addWidget(self.title, 1)
        self.when = QLabel("", self)
        self.when.setFont(variable_font(12, "Regular"))
        who.addWidget(self.when)
        column.addLayout(who)
        column.addSpacing(4)
        self.question = QLabel(self)
        self.question.setFont(variable_font(19, "Regular", display=True))
        self.question.setWordWrap(True)
        column.addWidget(self.question)
        self.detail = QLabel(self)
        self.detail.setFont(variable_font(13.5, "Regular"))
        self.detail.setWordWrap(True)
        column.addWidget(self.detail)
        self.note = QLabel(self)
        self.note.setFont(variable_font(13, "Regular"))
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        for label in (self.title, self.when, self.question, self.detail, self.note):
            label.setTextFormat(Qt.TextFormat.PlainText)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addStretch(1)
        self.no_button = self._button("No", primary=False)
        self.yes_button = self._button("Yes", primary=True)
        row1.addWidget(self.no_button)
        row1.addWidget(self.yes_button)
        column.addSpacing(8)
        column.addLayout(row1)
        column.addSpacing(8)
        self._rule_y = 0
        self.rule = QWidget(self)
        self.rule.setFixedHeight(1)
        column.addWidget(self.rule)
        row2 = QHBoxLayout()
        row2.setSpacing(18)
        self.never_button = self._button("Never", primary=False, small=True)
        self.always_button = self._button("Always", primary=False, small=True)
        row2.addWidget(self.never_button)
        row2.addWidget(self.always_button)
        row2.addStretch(1)
        column.addLayout(row2)
        for button, answer in ((self.yes_button, "yes"), (self.no_button, "no"),
                               (self.always_button, "always"), (self.never_button, "never")):
            button.clicked.connect(lambda _=False, a=answer: self._answer(a))

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._answer("none"))
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        t = self.theme()
        self.led.t = t
        self.led.update()
        self.title.setStyleSheet(f"color: {self.ink_css('text_3')};")
        self.when.setStyleSheet(f"color: {self.ink_css('text_3')};")
        self.question.setStyleSheet(f"color: {self.ink_css('text')};")
        self.detail.setStyleSheet(f"color: {self.ink_css('text_2')};")
        self.note.setStyleSheet(f"color: {self.ink_css('text')};")   # said plainly (no warning colour)
        self.rule.setStyleSheet(f"background: {self.ink_css('text_3')};")
        self.rule.setStyleSheet(f"background: rgba({t.float_sep.red()},{t.float_sep.green()},{t.float_sep.blue()},255);")
        self.yes_button.setStyleSheet(self.button_css("default"))
        self.no_button.setStyleSheet(self.button_css("ghost"))
        quiet = self.button_css("ghost").replace("padding: 0px 14px", "padding: 0px 0px")
        for button in (self.never_button, self.always_button):
            button.setStyleSheet(quiet)

    def _button(self, text: str, *, primary: bool, small: bool = False) -> QPushButton:
        from yuki.ui.window.theme import variable_font

        button = QPushButton(text, self)
        button.setFont(variable_font(13, "Semibold" if primary else "Regular"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # the card never takes the keyboard
        return button   # styled as a pill in restyle()

    def set_call(self, call: dict[str, Any]) -> None:
        """Fill the card for one call and size it (no showing)."""
        self.call = call
        question, detail, note = consent_text(call)
        from yuki.ui.memory import local_moment

        app_name = str(call.get("app") or "")
        self.title.setText(f"{app_name} started a call" if app_name else "A call started")
        moment = local_moment(call.get("detected_at"))
        self.when.setText(moment.strftime("%H:%M") if moment else "")
        self.question.setText(question)
        self.detail.setText(detail)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        app = str(call.get("app") or "this app")
        self.always_button.setText(f"Always for {app}")
        self.never_button.setText(f"Never for {app}")
        remember = bool(call.get("can_remember_choice", True))
        self.always_button.setVisible(remember)
        self.never_button.setVisible(remember)
        self.rule.setVisible(remember)
        self.layout().activate()
        width = CARD_WIDTH + 2 * self.SHADOW
        lay = self.layout()
        wanted = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.sizeHint().height()
        self.setFixedHeight(max(120 + 2 * self.SHADOW, wanted))
        self.update()

    def anchor(self, *, above: int = 0) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN - above
        return QPoint(x, y)

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
    """The call pill (Horizon x Field Unit): the accent recording dot, a small grey wave, the call's running
    time on a dot-matrix timer (37:12) and "Remembering your side, Discord". Waiting for headphones: no dot,
    no timer, the words say it. Never takes focus.

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
        waiting = (self.call or {}).get("status") == "waiting_headphones"
        width = 14 + (0 if waiting else 8 + 11) + 30 + 11 + (0 if waiting else self.timer_label.width() + 11)
        width += fm.horizontalAdvance(self.label.text()) + 22
        width = max(INDICATOR_WIDTH, width)
        if width + 2 * self.SHADOW != self.width():
            self.setFixedSize(width + 2 * self.SHADOW, INDICATOR_HEIGHT + 2 * self.SHADOW)

    def set_call(self, call: dict[str, Any]) -> None:
        from yuki.ui.memory import local_moment

        self.call = call
        self.label.setText(indicator_text(call))
        waiting = call.get("status") == "waiting_headphones"
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
        call = self.status.get("call") or {}
        return call.get("status") in ("recording", "waiting_headphones")

    # -- state -------------------------------------------------------------

    def _above(self) -> int:
        return (STRIP_HEIGHT + _GAP) if self._strip_up() else 0

    def offer(self, status: object, why: str = "") -> None:
        """A fresh ``call_status()``: show, update or take down the card and the indicator."""
        status = status if isinstance(status, dict) else {}
        before = (self.status.get("call") or {}).get("status")
        self.status = status
        call = status.get("call") or None
        if (call or {}).get("status") != before:
            self.ui_log.event("call_status", via=why, call_id=(call or {}).get("id"), app=(call or {}).get("app"),
                              status=(call or {}).get("status"))
        asking = bool(call and call.get("asking") and call.get("id") not in self._answered)
        if asking and not self.card_up:
            self.card.show_call(call, above=self._above())
            self.ui_log.event("call_consent_shown", call_id=call.get("id"), app=call.get("app"),
                              headphones=call.get("headphones"))
        elif not asking and self.card_up:
            self.card.withdraw()
        self.place_indicator()
        if call:
            self._tick.start()
        else:
            self._tick.stop()
        self.changed.emit(status)

    def place_indicator(self) -> None:
        call = self.status.get("call") or {}
        on = call.get("status") in ("recording", "waiting_headphones")
        value, _name = self._screen_state()
        hidden = self.card_up or self._covered() or value in BUSY_NOTIFICATION_STATES
        if on and not hidden:
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
        call_id = call.get("id")
        self._answered.add(call_id)
        self.ui_log.event("call_consent_answer", call_id=call_id, app=call.get("app"), answer=answer)
        self._background("call_answer", lambda: self.memory.call_method(
            "answer_call", call_id, answer, timeout_s=TRAY_TIMEOUT_S))

    def stop_call(self) -> threading.Thread:
        """The tray's "Stop remembering this call"."""
        self.ui_log.event("call_stop_requested", call_id=(self.status.get("call") or {}).get("id"))
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
           "consent_text", "indicator_text", "waiting_text"]
