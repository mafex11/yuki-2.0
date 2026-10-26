"""The status strip: what Yuki is doing, bottom-right, from the moment a request starts.

Every agent run (typed, spoken, anything) shows here **from its first second**
(2026-10-03, ``the design notes`` §4.4: the one real
"Do it" ran 9.6 s before anything appeared, and nothing on screen could stop it): the
thinking line (Horizon's dotted grey wave, drifting while it works), the current step,
the time it has taken and the step count, and a **Stop** pill that never takes focus and
cancels that request. The first strip of a session also says the stop shortcut once.
The step line comes from the one label function of every run surface
(:func:`yuki.ui.step_words.describe_step`): the brain's own plain words for the step, else
words built from the control and app the agent named, never a command, typed text, address,
key or id (revamp 38 §8). When the run ends the strip says how: the answer (Done), Stopped,
or the error.
"""

from __future__ import annotations

from typing import Any

import time

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from yuki.ui.step_words import _iso, app_name, clock, describe_step
from yuki.ui.surface import ACCENT, SurfaceWindow
from yuki.ui.wave import Wave

#: Size of the panel (excluding the shadow margin), in logical pixels. The corner cards share the width.
STRIP_WIDTH = 360
STRIP_HEIGHT = 48
#: The strip's own width: the step line, the time and step count, and the Stop pill side by side.
STRIP_RUN_WIDTH = 440

#: Gap between the panel and the screen edges.
STRIP_MARGIN = 24

#: How long a finished message stays up before it fades.
FINAL_LINGER_MS = 4000


def describe_tool(name: str, tool_input: dict[str, Any] | None = None, *, limit: int = 60) -> str:
    """One line of present-tense status text for a tool call, in plain words (``Closing Slack``, ``Clicking Done``).

    The one label function of every run surface (:func:`yuki.ui.step_words.describe_step`): the call's own ``what``,
    else words built from what the agent knows about the target (``_about``: the control's name, the app) and the
    call's fixed machine fields. Never the command, typed text, address, keys, ids or numbers (revamp 38 §8). A
    memory read's dates read as a person says them (``Checking your calls yesterday``). Purely presentational.

    Args:
        name: Tool name exactly as the model called it.
        tool_input: The tool's arguments as the ``tool_call`` event carries them.
        limit: The longest line.

    Returns:
        A single line, no trailing punctuation except an ellipsis.
    """
    return describe_step(name, tool_input, limit=limit)


def describe_result(name: str, tool_input: dict[str, Any] | None, payload: Any) -> str:
    """A better line for a finished step when its result names what it worked on, else ``""``.

    A call read from memory (the result's ``call``: its app and start) reads ``Reading yesterday's Discord call from
    10:09 PM``. Built from the result's structured fields only."""
    del name, tool_input
    call = payload.get("call") if isinstance(payload, dict) else None
    if not isinstance(call, dict):
        return ""
    app = app_name(call.get("app"))
    start = _iso(call.get("start"))
    if not app or start is None:
        return ""
    from datetime import date

    days = (date.today() - start.date()).days
    at = clock(start)
    if days == 0:
        return f"Reading today's {app} call from {at}"
    if days == 1:
        return f"Reading yesterday's {app} call from {at}"
    if 1 < days < 7:
        return f"Reading {start:%A}'s {app} call from {at}"
    return f"Reading the {app} call of {start.day} {start:%B}, {at}"


def describe_summary(summary: dict[str, Any]) -> str:
    """The short dim suffix for a finished request: ``52 s, 10 steps, 8¢``.

    Steps are model round trips. Cost is the estimate from ``Settings.pricing``,
    left out when the model has no price. Purely presentational.

    Args:
        summary: A ``request_summary`` record (:meth:`yuki.agent.loop.Agent._summarize`).

    Returns:
        One short line, or ``""`` when there is nothing worth showing.
    """
    parts: list[str] = []
    wall = summary.get("wall_s")
    if isinstance(wall, (int, float)):
        seconds = round(float(wall))
        parts.append(f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s")
    steps = summary.get("model_calls")
    if isinstance(steps, int) and steps > 0:
        parts.append(f"{steps} step{'' if steps == 1 else 's'}")
    cost = summary.get("cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(_money(float(cost)))
    return ", ".join(parts)


def elapsed_words(seconds: float) -> str:
    """A run's time so far, short: ``8 s``, ``1:05``, ``1:02:05``."""
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs} s"
    h, rem = divmod(secs, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def run_meta(seconds: float, steps: int) -> str:
    """The live suffix of a run: ``12 s`` before its first step, then ``12 s · step 3``."""
    return elapsed_words(seconds) + (f" · step {steps}" if steps > 0 else "")


def _money(usd: float) -> str:
    """``$1.23`` from a dollar up, whole cents below, one decimal under a cent."""
    if usd >= 1.0:
        return f"${usd:.2f}"
    cents = usd * 100
    if cents >= 0.95:
        return f"{cents:.0f}¢"
    return f"{cents:.1f}¢"


def _shorten(text: str, limit: int) -> str:
    """Truncate with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Spinner(QWidget):
    """A thin rotating arc. Driven by a Qt animation, so it costs no thread."""

    def __init__(self, parent: QWidget | None = None, *, diameter: int = 18) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self._angle = 0.0
        self.setFixedSize(QSize(diameter, diameter))
        self._spin = QPropertyAnimation(self, b"angle", self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1100)
        self._spin.setLoopCount(-1)
        self._spin.setEasingCurve(QEasingCurve.Type.Linear)

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = value
        self.update()

    #: Rotation in degrees; animated.
    angle = Property(float, _get_angle, _set_angle)

    def start(self) -> None:
        """Begin spinning."""
        if self._spin.state() != QPropertyAnimation.State.Running:
            self._spin.start()

    def stop(self) -> None:
        """Stop spinning and leave the arc where it is."""
        self._spin.stop()

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0)
        host = self.window()
        track, accent = QColor(255, 255, 255, 34), ACCENT
        if hasattr(host, "theme"):   # a surface: the theme's colours
            t = host.theme()
            track = QColor(t.baseline)
            accent = QColor(t.accent)
        painter.setPen(QPen(track, 2.0))
        painter.drawEllipse(rect)
        pen = QPen(accent, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self._angle * 16), int(-110 * 16))


class ThinkingLine(Wave):
    """The strip's "working" mark: the dotted thinking line; ``start`` / ``stop`` keep Spinner's calls (it
    animates only while shown)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        host = parent
        super().__init__("think", theme=(lambda: host.theme()) if hasattr(host, "theme") else None, size=(34, 10),
                         parent=parent)

    def start(self) -> None:
        self.setVisible(True)

    def stop(self) -> None:
        pass


class ElidedLabel(QLabel):
    """A label that shortens its text with an ellipsis instead of overflowing."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, text: str) -> None:  # noqa: D102 - Qt override
        self._full = text
        super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, self.width()))

    def resizeEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().resizeEvent(event)
        super().setText(
            self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        )


class StatusStrip(SurfaceWindow):
    """Bottom-right strip showing a run from its start: the step, its time and count, and Stop; then the final word.

    It never takes focus: it appears while the user is doing something else, and
    stealing the keyboard mid-game would be unforgivable. Its Stop pill is clickable
    without taking focus, like the cards' buttons.

    Signals:
        stop_clicked: ``(request id)`` -- Stop on the run the strip shows.
    """

    stop_clicked = Signal(int)

    def __init__(self) -> None:
        super().__init__(activates=False)
        self.setObjectName("YukiStatusStrip")
        self.setFixedSize(STRIP_RUN_WIDTH + 2 * self.SHADOW, STRIP_HEIGHT + 2 * self.SHADOW)

        from yuki.ui.window.theme import variable_font

        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 18, self.SHADOW, self.SHADOW + 18, self.SHADOW)
        row.setSpacing(12)

        self.spinner = ThinkingLine(self)
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self.label = ElidedLabel(self)
        self.label.setFont(variable_font(14, "Regular"))
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)

        #: Dim suffix: while a run goes on its time and step count ("12 s · step 3"); with a final message the
        #: request's time, steps and cost.
        self.meta = QLabel(self)
        self.meta.setFont(variable_font(12, "Regular"))
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setVisible(False)
        row.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

        #: Stop: cancels the run the strip shows (never takes focus).
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setFont(variable_font(12.5, "Regular"))
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stop_button.setToolTip("Stop this request")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._on_stop)
        row.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.fade_out)
        #: The run shown: its request id, when it started (monotonic), its steps so far; a 1 s clock while shown.
        self.run_id: int | None = None
        self._run_started = 0.0
        self._run_steps = 0
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._tone = "final"
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.label.setStyleSheet(f"color: {self.ink_css('danger' if self._tone == 'error' else 'text')};")
        self.meta.setStyleSheet(f"color: {self.ink_css('text_3')};")
        self.stop_button.setStyleSheet(self.button_css("outline"))
        self.update()

    def anchor(self) -> QPoint:
        """Top-left to sit at: bottom-right of the primary screen's work area."""
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN
        return QPoint(x, y)

    # -- states ------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the strip shows a run that has not ended."""
        return self.run_id is not None

    def start_run(self, request_id: int, *, started: float | None = None, steps: int = 0, line: str = "",
                  hint: str = "") -> None:
        """Show a run from its start: "Thinking…" (or its current line), its time, its step count and Stop.

        Args:
            request_id: The request (Stop cancels it).
            started: When it started (``time.monotonic()``); now by default.
            steps: Steps taken so far (a run shown again after another one, or after a question).
            line: Its current line; "Thinking…" when empty.
            hint: Said once, before the first step (the stop shortcut on a session's first strip).
        """
        self.run_id = int(request_id)
        self._run_started = time.monotonic() if started is None else float(started)
        self._run_steps = int(steps)
        self._linger.stop()
        self._tone = "final"
        self.restyle()
        text = line or "Thinking…"
        self.label.setText(f"{text} ({hint})" if hint and not line else text)
        self.spinner.start()
        self.spinner.setVisible(True)
        self.stop_button.setText("Stop")
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self.meta.setVisible(True)
        self._tick()
        self._clock.start()
        self.fade_in(self.anchor())

    def run_step(self, request_id: int, text: str, steps: int | None = None) -> None:
        """A step of the run the strip shows: its line, and the count when given; shown again if it was hidden."""
        if self.run_id != int(request_id):
            return
        if steps is not None:
            self._run_steps = int(steps)
        self._linger.stop()
        self.label.setText(text)
        self._tick()
        if not self.isVisible() or self.closing:
            self.fade_in(self.anchor())

    def stopping(self) -> None:
        """Stop was pressed: it says so until the run ends (a tool in flight finishes first)."""
        self.stop_button.setText("Stopping…")
        self.stop_button.setEnabled(False)

    def end_run(self, request_id: int | None = None) -> bool:
        """The run ended: its clock and Stop go (True when it was the one shown). The caller shows the final word
        (:meth:`show_final`) or dismisses the strip."""
        if self.run_id is None or (request_id is not None and self.run_id != int(request_id)):
            return False
        self.run_id = None
        self._clock.stop()
        self.stop_button.setVisible(False)
        return True

    def _tick(self) -> None:
        if self.run_id is not None:
            self.meta.setText(run_meta(time.monotonic() - self._run_started, self._run_steps))

    def _on_stop(self) -> None:
        if self.run_id is not None:
            self.stopping()
            self.stop_clicked.emit(self.run_id)

    def show_step(self, text: str) -> None:
        """Show (or update) a line of progress with the spinner running (outside a run: a question asked by
        voice; inside one, prefer :meth:`run_step`).

        Args:
            text: The line to display.
        """
        self._linger.stop()
        self.meta.setVisible(self.run_id is not None)
        self._tone = "final"
        self.restyle()
        self.label.setText(text)
        self.spinner.start()
        self.spinner.setVisible(True)
        self.fade_in(self.anchor())

    def show_final(self, text: str, *, tone: str = "final", outcome: str | None = None) -> None:
        """Show the closing message, stop the spinner, and fade after a few seconds.

        Args:
            text: The message.
            tone: ``final`` or ``error``; an error is said in red.
        """
        self.end_run()
        self.meta.setVisible(False)
        self.label.setText(text)
        self.spinner.stop()
        self.spinner.setVisible(False)
        self._tone = tone
        self.restyle()
        self.fade_in(self.anchor())
        self._linger.start(FINAL_LINGER_MS)

    def set_meta(self, text: str) -> None:
        """Add the dim suffix to the final message on show (no timer restart).

        Args:
            text: e.g. ``52 s, 10 steps, 8¢``; empty hides it.
        """
        self.meta.setText(text)
        # The message label gives up the width; its resizeEvent re-elides it.
        self.meta.setVisible(bool(text))

    def dismiss(self) -> None:
        """Hide the strip now (a run it shows keeps its clock: it comes back on the run's next step)."""
        self._linger.stop()
        self.spinner.stop()
        self.fade_out()

    def hideEvent(self, event: object) -> None:  # noqa: D102 - no clock while nothing is on screen
        self._clock.stop()
        super().hideEvent(event)

    def showEvent(self, event: object) -> None:  # noqa: D102
        super().showEvent(event)
        if self.run_id is not None:
            self._tick()
            self._clock.start()
