"""The voice pill: a small flat status at the bottom centre of the screen, never focused.

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

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel

from yuki.ui.surface import SurfaceWindow, ui_font

PILL_WIDTH = 210
PILL_HEIGHT = 34
MARGIN = 24
#: How long an error or a "voice off" note stays before the pill fades.
NOTE_MS = 8000
#: An error or a note may widen the pill up to this, so the reason can be read.
NOTE_MAX_WIDTH = 520

#: state -> (label, ink role). The state is in its words (no coloured dot); an error is said in red.
LOOK: dict[str, tuple[str, str]] = {
    "starting": ("Starting voice…", "text_2"),
    "listening": ("Listening", "text"),
    "hearing": ("Hearing you", "text"),
    "thinking": ("Thinking", "text"),
    "speaking": ("Speaking", "text"),
    "working": ("Working on it", "text"),
    "error": ("Voice error", "danger"),
    "note": ("Voice", "text_2"),
}


class VoicePill(SurfaceWindow):
    """Bottom-centre voice status. ``show_state(state, detail)`` is the only entry point."""

    def __init__(self) -> None:
        super().__init__(activates=False)
        self.setObjectName("YukiVoicePill")
        self.setFixedSize(PILL_WIDTH + 2 * self.SHADOW, PILL_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 16, self.SHADOW, self.SHADOW + 16, self.SHADOW)
        self.label = QLabel(self)
        self.label.setFont(ui_font(9, weight=QFont.Weight.Medium))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._ink = "text"   # the state's ink role (LOOK)
        self.restyle()
        from yuki.ui.window.widgets import attach_tips

        attach_tips(self, self.theme)   # its devices tooltip is a flat popover too
        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(self.fade_out)
        self._devices = ""
        self._width = PILL_WIDTH

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.label.setStyleSheet(f"color: {self.ink_css(self._ink)};")

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
        label, self._ink = LOOK.get(state, (state.capitalize(), "text"))
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
