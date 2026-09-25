"""The voice pill: a small glass status at the bottom centre of the screen, never focused.

It says what voice is doing: ``Listening`` (a conversation is open and it is
the user's turn), ``Hearing you``, ``Thinking``, ``Speaking``, ``Working on it``
(a task is running and the conversation is waiting to speak its result), or an
error in words. While only the wake word is on it stays hidden (a dim tray
tooltip says so instead): an always-visible pill would be noise all day.
Purely presentational; it decides nothing.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel

from yuki.ui.glass import ACCENT, GlassWindow, ui_font

PILL_WIDTH = 210
PILL_HEIGHT = 34
MARGIN = 24
#: How long an error or a "voice off" note stays before the pill fades.
NOTE_MS = 7000

#: state -> (label, dot colour).
LOOK: dict[str, tuple[str, QColor]] = {
    "starting": ("Starting voice…", QColor(170, 176, 190)),
    "listening": ("Listening", ACCENT),
    "hearing": ("Hearing you", QColor(120, 220, 160)),
    "thinking": ("Thinking", QColor(255, 200, 110)),
    "speaking": ("Speaking", QColor(190, 150, 255)),
    "working": ("Working on it", QColor(255, 200, 110)),
    "error": ("Voice error", QColor(255, 120, 120)),
}


class VoicePill(GlassWindow):
    """Bottom-centre voice status. ``show_state(state, detail)`` is the only entry point."""

    def __init__(self) -> None:
        super().__init__(activates=False, radius=12)
        self.setObjectName("YukiVoicePill")
        self.setFixedSize(PILL_WIDTH + 2 * self.SHADOW, PILL_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 28, self.SHADOW, self.SHADOW + 12, self.SHADOW)
        self.label = QLabel(self)
        self.label.setFont(ui_font(9))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setStyleSheet("color: rgba(238,240,245,230);")
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._dot = ACCENT
        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(self.fade_out)

    def anchor(self) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.center().x() - self.width() // 2
        y = area.bottom() - self.height() + self.SHADOW - MARGIN
        return QPoint(x, y)

    def show_state(self, state: str, detail: str = "") -> None:
        self._fade.stop()
        if state in ("off", "wake"):
            if self.isVisible():
                self.fade_out()
            return
        label, self._dot = LOOK.get(state, (state.capitalize(), ACCENT))
        if state in ("error", "starting") and detail and detail not in ("tray", "hotkey", "wake"):
            label = detail  # an error, or a note such as the voice falling back
            self.setToolTip(detail)
        self.label.setText(self.label.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, PILL_WIDTH - 44))
        self.update()
        if not self.isVisible() or self.closing:
            self.fade_in(self.anchor())
        if state == "error":
            self._fade.start(NOTE_MS)

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        panel = QRectF(self.panel_rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._dot)
        painter.drawEllipse(QRectF(panel.left() + 12, panel.center().y() - 4, 8, 8))
