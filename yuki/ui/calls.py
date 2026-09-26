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

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from yuki.agent.memory import TRAY_TIMEOUT_S, MemoryAccess, MemoryUnavailable
from yuki.ui.glass import ACCENT, GlassWindow, ui_font
from yuki.ui.nudges import BUSY_NOTIFICATION_STATES, NudgeWatcher, notification_state
from yuki.ui.status import STRIP_HEIGHT, STRIP_MARGIN, STRIP_WIDTH
from yuki.ui.uilog import UiLog

#: The named event the memory service sets when a call's state changes.
CALL_EVENT_NAME = "Local\\YukiCallReady"
#: Fallback look at ``call_status()`` when the event stays quiet.
FALLBACK_POLL_S = 20.0
#: While a call is on, look this often too (the indicator follows the status).
ACTIVE_POLL_MS = 10_000

CALL_ACCENT = QColor(255, 122, 122)
WAIT_ACCENT = QColor(255, 190, 110)
CARD_WIDTH = STRIP_WIDTH
INDICATOR_WIDTH = 236
INDICATOR_HEIGHT = 34
_GAP = 8

HEADPHONES_NOTE = "Only with headphones, so other people's voices aren't picked up."


def consent_text(call: dict[str, Any]) -> tuple[str, str, str]:
    """``(question, detail, headphones note or "")`` for a ``call_status()["call"]``."""
    app = str(call.get("app") or "this app")
    detail = (f"{app} · only your microphone, kept encrypted and turned into text on this PC later, "
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
        return f"Waiting for headphones · {app}" if app else "Waiting for headphones"
    return f"Remembering your side · {app}" if app else "Remembering your side"


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


class CallConsentCard(GlassWindow):
    """The consent card: the question, what is and is not recorded, four answers. Never takes focus.

    Signals:
        answered: ``(call, answer)`` -- ``yes`` | ``no`` | ``always`` | ``never``, or ``none``
            when it timed out untouched.
    """

    answered = Signal(object, str)

    def __init__(self) -> None:
        super().__init__(activates=False, radius=20)
        self.setObjectName("YukiCallConsentCard")
        self.call: dict[str, Any] | None = None
        self.setFixedWidth(CARD_WIDTH + 2 * self.SHADOW)
        column = QVBoxLayout(self)
        column.setContentsMargins(self.SHADOW + 18, self.SHADOW + 12, self.SHADOW + 14, self.SHADOW + 12)
        column.setSpacing(5)

        self.title = QLabel("Yuki · call", self)
        self.title.setFont(ui_font(9, weight=QFont.Weight.DemiBold))
        column.addWidget(self.title)
        self.question = QLabel(self)
        self.question.setFont(ui_font(11, weight=QFont.Weight.DemiBold))
        self.question.setWordWrap(True)
        column.addWidget(self.question)
        self.detail = QLabel(self)
        self.detail.setFont(ui_font(9))
        self.detail.setWordWrap(True)
        column.addWidget(self.detail)
        self.note = QLabel(self)
        self.note.setFont(ui_font(9))
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        for label in (self.title, self.question, self.detail, self.note):
            label.setTextFormat(Qt.TextFormat.PlainText)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addStretch(1)
        self.no_button = self._button("No", primary=False)
        self.yes_button = self._button("Yes", primary=True)
        row1.addWidget(self.no_button)
        row1.addWidget(self.yes_button)
        column.addSpacing(2)
        column.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addStretch(1)
        self.never_button = self._button("Never", primary=False, small=True)
        self.always_button = self._button("Always", primary=False, small=True)
        row2.addWidget(self.never_button)
        row2.addWidget(self.always_button)
        column.addLayout(row2)
        for button, answer in ((self.yes_button, "yes"), (self.no_button, "no"),
                               (self.always_button, "always"), (self.never_button, "never")):
            button.clicked.connect(lambda _=False, a=answer: self._answer(a))

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._answer("none"))
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - GlassWindow hook (theme changed)
        call = self.ink("call")
        self.title.setStyleSheet(f"color: rgb({call.red()},{call.green()},{call.blue()});")
        self.question.setStyleSheet(f"color: {self.ink_css('text')};")
        self.detail.setStyleSheet(f"color: {self.ink_css('text_2')};")
        self.note.setStyleSheet(f"color: {self.ink_css('nudge')};")
        self.yes_button.setStyleSheet(self.button_css("tinted", call))
        for button in (self.no_button, self.never_button, self.always_button):
            button.setStyleSheet(self.button_css("quiet"))
        wash = QColor(call)
        wash.setAlphaF(0.07 if not self.theme().dark else 0.10)
        self.glass_tint = wash

    def _button(self, text: str, *, primary: bool, small: bool = False) -> QPushButton:
        button = QPushButton(text, self)
        button.setFont(ui_font(9 if small else 10, weight=QFont.Weight.DemiBold))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # the card never takes the keyboard
        return button   # styled as a pill in restyle()

    def set_call(self, call: dict[str, Any]) -> None:
        """Fill the card for one call and size it (no showing)."""
        self.call = call
        question, detail, note = consent_text(call)
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
        self.layout().activate()
        width = CARD_WIDTH + 2 * self.SHADOW
        lay = self.layout()
        wanted = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.sizeHint().height()
        self.setFixedHeight(max(120 + 2 * self.SHADOW, wanted))
        self._shadow_pixmap = None
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

class CallIndicator(GlassWindow):
    """The small "Remembering your side · Zoom" pill (or "Waiting for headphones"). Never takes focus.

    Clear glass like every surface (2026-09-27): the state is in its words, with a faint wash of the call's
    colour (or the waiting colour) on the glass; no coloured dot, no dark block, no border.
    """

    def __init__(self) -> None:
        super().__init__(activates=False, radius=INDICATOR_HEIGHT // 2)
        self.setObjectName("YukiCallIndicator")
        self.setFixedSize(INDICATOR_WIDTH + 2 * self.SHADOW, INDICATOR_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 16, self.SHADOW, self.SHADOW + 14, self.SHADOW)
        self.label = QLabel(self)
        self.label.setFont(ui_font(9))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.call: dict[str, Any] | None = None
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - GlassWindow hook (theme changed)
        from yuki.ui.glass import text_halo

        self.glass_density = "clear"
        self.label.setStyleSheet(f"color: {self.ink_css('text')};")
        text_halo(self.label, self.theme().dark)
        waiting = (self.call or {}).get("status") == "waiting_headphones"
        wash = QColor(self.ink("nudge" if waiting else "call"))
        wash.setAlphaF(0.08 if not self.theme().dark else 0.12)
        self.glass_tint = wash

    def set_call(self, call: dict[str, Any]) -> None:
        self.call = call
        self.label.setText(indicator_text(call))
        self.restyle()
        self.update()

    def anchor(self, *, above: int = 0) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN - above
        return QPoint(x, y)


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
