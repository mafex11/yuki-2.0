"""The status strip: what Yuki is doing, bottom-right, while it has the hands.

While a task runs the overlay gets out of the way and this strip takes over: the
thinking line (Horizon's dotted grey wave, drifting while it works) and one line of text. The line is the tool's own one-line ``label`` from
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
from yuki.ui.surface import ACCENT, SurfaceWindow
from yuki.ui.wave import Wave

#: Size of the panel (excluding the shadow margin), in logical pixels.
STRIP_WIDTH = 360
STRIP_HEIGHT = 48

#: Gap between the panel and the screen edges.
STRIP_MARGIN = 24

#: How long a finished message stays up before it fades.
FINAL_LINGER_MS = 4000


def describe_tool(name: str, tool_input: dict[str, Any] | None = None, *, limit: int = 44) -> str:
    """One line of present-tense status text for a tool call, in plain words.

    The phrase is the tool's own ``label`` (``hotkey`` -> ``Pressing a shortcut``),
    with its most descriptive argument appended when there is one
    (``Opening an app — spotify``). Dates and date-times in the arguments (ISO, the
    tools' fixed format) read as a person says them (``Checking your calls on 26 Sep``,
    ``… since yesterday``); Yuki's own record ids (``call:23``) mean nothing to the
    user and are left out (the result names the thing, :func:`describe_result`). A
    name the registry has never heard of reads "Working". Purely presentational:
    nothing here changes what runs.

    Args:
        name: Tool name exactly as the model called it.
        tool_input: The tool's arguments.
        limit: Maximum length of the appended argument.

    Returns:
        A single line, no trailing punctuation except an ellipsis.
    """
    phrase = tool_label(name) or "Working"
    args = tool_input or {}
    dates = [(k, d) for k, v in args.items() if (d := _iso(v)) is not None]
    rest = {k: v for k, v in args.items() if _iso(v) is None and not _record_id(v)}
    detail = _detail(rest, limit=limit)
    line = f"{phrase} — {detail}" if detail else phrase
    when = _when_words(dates)
    if when:
        line = f"{line} {when}"
    return line if (detail or when) else f"{phrase}…"


def describe_result(name: str, tool_input: dict[str, Any] | None, payload: Any) -> str:
    """A better line for a finished step when its result names what it worked on, else ``""``.

    A call read from memory (the result's ``call``: its app and start) reads ``Reading yesterday's 22:09
    Discord call``. Built from the result's structured fields only."""
    del name, tool_input
    call = payload.get("call") if isinstance(payload, dict) else None
    if not isinstance(call, dict):
        return ""
    app = str(call.get("app") or "").strip()
    start = _iso(call.get("start"))
    if not app or start is None:
        return ""
    from datetime import date

    days = (date.today() - start.date()).days
    hm = start.strftime("%H:%M")
    if days == 0:
        return f"Reading today's {hm} {app} call"
    if days == 1:
        return f"Reading yesterday's {hm} {app} call"
    if 1 < days < 7:
        return f"Reading {start:%A}'s {hm} {app} call"
    return f"Reading the {app} call of {start.day} {start:%b}, {hm}"


def _iso(value: Any) -> Any:
    """A date or date-time from an ISO string (the tools' fixed format), else None."""
    from datetime import date, datetime

    if not isinstance(value, str) or not value[:4].isdigit() or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            d = date.fromisoformat(value.strip()[:10])
            return datetime(d.year, d.month, d.day)
        except ValueError:
            return None


def _record_id(value: Any) -> bool:
    """Yuki's own record ids, ``<kind>:<number>`` (``call:23``, ``fact:7``): a fixed internal format."""
    if not isinstance(value, str) or ":" not in value:
        return False
    kind, _sep, number = value.strip().partition(":")
    return kind.isalpha() and number.isdigit()


def _day_words(d: Any, *, time_too: bool = True) -> str:
    from datetime import date

    days = (date.today() - d.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{d.day} {d:%b}"
    return f"{day} {d:%H:%M}" if time_too and (d.hour, d.minute) != (0, 0) else day


def _when_words(dates: list[tuple[str, Any]]) -> str:
    """``on 26 Sep``, ``since yesterday``, ``from 25 Sep to today``, from the ISO dates of a call's arguments."""
    if not dates:
        return ""
    if len(dates) == 1:
        key, d = dates[0]
        words = _day_words(d)
        if key in ("since", "until", "before", "after"):
            return f"{key} {words}"
        return words if words.startswith(("today", "yesterday")) else f"on {words}"
    (_ka, a), (_kb, b) = sorted(dates[:2], key=lambda kd: kd[1])
    if a.date() == b.date():
        words = _day_words(a, time_too=False)
        return words if words in ("today", "yesterday") else f"on {words}"
    return f"from {_day_words(a)} to {_day_words(b)}"


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
    """Bottom-right strip showing the current step, then the final word.

    It never takes focus: it appears while the user is doing something else, and
    stealing the keyboard mid-game would be unforgivable.
    """

    def __init__(self) -> None:
        super().__init__(activates=False)
        self.setObjectName("YukiStatusStrip")
        self.setFixedSize(STRIP_WIDTH + 2 * self.SHADOW, STRIP_HEIGHT + 2 * self.SHADOW)

        from yuki.ui.window.theme import variable_font

        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 18, self.SHADOW, self.SHADOW + 18, self.SHADOW)
        row.setSpacing(12)

        self.spinner = ThinkingLine(self)
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self.label = ElidedLabel(self)
        self.label.setFont(variable_font(14, "Regular"))
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)

        #: Dim time/steps/cost suffix, shown only with a final message.
        self.meta = QLabel(self)
        self.meta.setFont(variable_font(12, "Regular"))
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setVisible(False)
        row.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.fade_out)
        self._tone = "final"
        self.restyle()

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.label.setStyleSheet(f"color: {self.ink_css('danger' if self._tone == 'error' else 'text')};")
        self.meta.setStyleSheet(f"color: {self.ink_css('text_3')};")
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
            tone: ``final`` or ``error``; an error is said in red.
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
            text: e.g. ``52 s, 10 steps, 8¢``; empty hides it.
        """
        self.meta.setText(text)
        # The message label gives up the width; its resizeEvent re-elides it.
        self.meta.setVisible(bool(text))

    def dismiss(self) -> None:
        """Hide the strip now."""
        self._linger.stop()
        self.spinner.stop()
        self.fade_out()
