"""The voice pill: a small glass status at the bottom centre of the screen, never focused.

It says what voice is doing: ``Listening`` (a conversation is open and it is
the user's turn), ``Hearing you``, ``Thinking``, ``Speaking``, ``Working on it``
(a task is running and the conversation is waiting to speak its result), or an
error in words. While only the wake word is on it stays hidden (a dim tray
tooltip says so instead): an always-visible pill would be noise all day.
An error or a note widens the pill to fit its words (up to :data:`NOTE_MAX_WIDTH`)
and stays for :data:`NOTE_MS` even if voice goes back to listening for the name
meanwhile: a failure is never shown for a blink. Purely presentational; it decides nothing.
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
NOTE_MS = 8000
#: An error or a note may widen the pill up to this, so the reason can be read.
NOTE_MAX_WIDTH = 520

#: state -> (label, the colour of the faint wash on the pill's clear glass). No coloured dot (2026-09-27):
#: the state is in its words; the wash only hints at it.
LOOK: dict[str, tuple[str, QColor]] = {
    "starting": ("Starting voice…", QColor(170, 176, 190)),
    "listening": ("Listening", ACCENT),
    "hearing": ("Hearing you", QColor(120, 220, 160)),
    "thinking": ("Thinking", QColor(255, 200, 110)),
    "speaking": ("Speaking", QColor(190, 150, 255)),
    "working": ("Working on it", QColor(255, 200, 110)),
    "error": ("Voice error", QColor(255, 120, 120)),
    "note": ("Voice", QColor(170, 176, 190)),
}
#: How strong that wash is (alpha of the state colour over the glass).
WASH_ALPHA = 0.10


class VoicePill(GlassWindow):
    """Bottom-centre voice status. ``show_state(state, detail)`` is the only entry point."""

    def __init__(self) -> None:
        super().__init__(activates=False, radius=12)
        self.setObjectName("YukiVoicePill")
        self.setFixedSize(PILL_WIDTH + 2 * self.SHADOW, PILL_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 16, self.SHADOW, self.SHADOW + 16, self.SHADOW)
        self.label = QLabel(self)
        self.label.setFont(ui_font(9))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._dot = ACCENT   # the state's colour: a faint wash of the glass (the name is kept for callers)
        self.restyle()
        from yuki.ui.window.widgets import attach_tips

        attach_tips(self, self.theme)   # its devices tooltip is clear glass too
        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(self.fade_out)
        self._devices = ""
        self._width = PILL_WIDTH

    def restyle(self) -> None:  # noqa: D102 - GlassWindow hook (theme changed)
        from yuki.ui.glass import text_halo

        self.glass_density = "clear"
        self.label.setStyleSheet(f"color: {self.ink_css('text')};")
        text_halo(self.label, self.theme().dark)
        wash = QColor(self._dot)
        wash.setAlphaF(WASH_ALPHA)
        self.glass_tint = wash

    def set_devices(self, text: str) -> None:
        """The devices voice uses now (``Mic: … · Speaker: …``), shown as the pill's tooltip."""
        self._devices = text or ""
        self.setToolTip(self._devices)

    def anchor(self) -> QPoint:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        x = area.center().x() - self.width() // 2
        y = area.bottom() - self.height() + self.SHADOW - MARGIN
        return QPoint(x, y)

    def _set_width(self, width: int) -> None:
        if width != self._width:
            self._width = width
            self.setFixedSize(width + 2 * self.SHADOW, PILL_HEIGHT + 2 * self.SHADOW)

    def show_state(self, state: str, detail: str = "") -> None:
        if state in ("off", "wake"):
            if self._fade.isActive():
                return  # an error or a note is up: it fades when its time is over
            if self.isVisible():
                self.fade_out()
            return
        self._fade.stop()
        label, self._dot = LOOK.get(state, (state.capitalize(), ACCENT))
        width = PILL_WIDTH
        if state in ("error", "starting", "note") and detail and detail not in ("tray", "hotkey", "wake"):
            label = detail  # an error, or a note such as the voice or a device falling back
            self.setToolTip(detail + (f"\n{self._devices}" if self._devices else ""))
            if state in ("error", "note"):
                width = min(NOTE_MAX_WIDTH, max(PILL_WIDTH, self.label.fontMetrics().horizontalAdvance(label) + 48))
        elif self._devices:
            self.setToolTip(self._devices)
        self._set_width(width)
        self.label.setText(self.label.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, width - 36))
        self.restyle()
        self.update()
        if not self.isVisible() or self.closing:
            self.fade_in(self.anchor())
        else:
            self.move(self.anchor())
        if state in ("error", "note"):
            self._fade.start(NOTE_MS)
