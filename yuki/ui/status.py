"""The status strip: what Yuki is doing, bottom-right, while it has the hands.

While a task runs the overlay gets out of the way and this strip takes over: a
spinner and one line of text. The line is the tool's own one-line ``label`` from
the registry (:func:`yuki.agent.tools.tool_label`) plus its most telling argument,
so the phrase a user reads is written next to the tool it describes rather than
re-derived here from its identifier. Every tool in the registry has a label
(``tests/test_no_pattern.py``); a name the registry does not know reads "Working".
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from yuki.agent.tools import tool_label
from yuki.ui.glass import ACCENT, TEXT_DIM, TEXT_PRIMARY, GlassWindow, ui_font

#: Size of the panel (excluding the shadow margin), in logical pixels.
STRIP_WIDTH = 360
STRIP_HEIGHT = 56

#: Gap between the panel and the screen edges.
STRIP_MARGIN = 24

#: How long a finished message stays up before it fades.
FINAL_LINGER_MS = 4000


def describe_tool(name: str, tool_input: dict[str, Any] | None = None, *, limit: int = 44) -> str:
    """One line of present-tense status text for a tool call.

    The phrase is the tool's own ``label`` (``hotkey`` -> ``Pressing a shortcut``),
    with its most descriptive argument appended when there is one
    (``Opening an app — spotify``). A name the registry has never heard of reads
    "Working" (every registered tool has a label). Purely presentational: nothing
    here changes what runs.

    Args:
        name: Tool name exactly as the model called it.
        tool_input: The tool's arguments.
        limit: Maximum length of the appended argument.

    Returns:
        A single line, no trailing punctuation except an ellipsis.
    """
    phrase = tool_label(name) or "Working"
    detail = _detail(tool_input or {}, limit=limit)
    return f"{phrase} — {detail}" if detail else f"{phrase}…"


def _detail(tool_input: dict[str, Any], *, limit: int) -> str:
    """The most human-readable argument of a tool call, shortened.

    Prefers text over numbers because text is what the user recognises; falls back
    to ``key=value`` so a purely numeric call still says something.
    """
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return _shorten(" ".join(value.split()), limit)
        if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
            return _shorten("+".join(value), limit)
    numbers = [
        f"{key} {value}"
        for key, value in tool_input.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return _shorten(", ".join(numbers[:3]), limit) if numbers else ""


def describe_summary(summary: dict[str, Any]) -> str:
    """The short dim suffix for a finished request: ``52 s · 10 steps · 8¢``.

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
    return " · ".join(parts)


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
        if hasattr(host, "theme"):   # a glass surface: the theme's colours
            t = host.theme()
            track = QColor(t.baseline)
            accent = QColor(t.accent)
        painter.setPen(QPen(track, 2.0))
        painter.drawEllipse(rect)
        pen = QPen(accent, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self._angle * 16), int(-110 * 16))


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


class StatusStrip(GlassWindow):
    """Bottom-right glass strip showing the current step, then the final word.

    It never takes focus: it appears while the user is doing something else, and
    stealing the keyboard mid-game would be unforgivable.
    """

    def __init__(self) -> None:
        super().__init__(activates=False, radius=STRIP_HEIGHT // 2)
        self.setObjectName("YukiStatusStrip")
        self.setFixedSize(STRIP_WIDTH + 2 * self.SHADOW, STRIP_HEIGHT + 2 * self.SHADOW)

        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 20, self.SHADOW, self.SHADOW + 22, self.SHADOW)
        row.setSpacing(12)

        self.spinner = Spinner(self)
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self.label = ElidedLabel(self)
        self.label.setFont(ui_font(11))
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)

        #: Dim time/steps/cost suffix, shown only with a final message.
        self.meta = QLabel(self)
        self.meta.setFont(ui_font(9))
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setVisible(False)
        row.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.fade_out)
        self._tone = "final"
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - GlassWindow hook (theme changed)
        from yuki.ui.glass import text_halo

        self.glass_density = "clear"
        text_halo(self.label, self.theme().dark)
        self.label.setStyleSheet(f"color: {self.ink_css('error' if self._tone == 'error' else 'text')};")
        self.meta.setStyleSheet(f"color: {self.ink_css('text_3')};")
        wash = None
        if self._tone == "error":   # an error is said in words, with a faint red wash on the clear glass
            wash = QColor(self.ink("error"))
            wash.setAlphaF(0.08 if not self.theme().dark else 0.12)
        self.glass_tint = wash
        self.update()

    def anchor(self) -> QPoint:
        """Top-left to sit at: bottom-right of the primary screen's work area."""
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.right() - self.width() + self.SHADOW - STRIP_MARGIN
        y = area.bottom() - self.height() + self.SHADOW - STRIP_MARGIN
        return QPoint(x, y)

    # -- states ------------------------------------------------------------

    def show_step(self, text: str) -> None:
        """Show (or update) a line of progress with the spinner running.

        Args:
            text: The line to display.
        """
        self._linger.stop()
        self.meta.setVisible(False)
        self._tone = "final"
        self.restyle()
        self.label.setText(text)
        self.spinner.start()
        self.spinner.setVisible(True)
        self.fade_in(self.anchor())

    def show_final(self, text: str, *, tone: str = "final") -> None:
        """Show the closing message, stop the spinner, and fade after a few seconds.

        Args:
            text: The message.
            tone: ``final`` or ``error``; errors are tinted red.
        """
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
            text: e.g. ``52 s · 10 steps · 8¢``; empty hides it.
        """
        self.meta.setText(text)
        # The message label gives up the width; its resizeEvent re-elides it.
        self.meta.setVisible(bool(text))

    def dismiss(self) -> None:
        """Hide the strip now."""
        self._linger.stop()
        self.spinner.stop()
        self.fade_out()
