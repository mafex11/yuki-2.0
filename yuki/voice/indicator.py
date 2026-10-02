"""The voice pill: a small status at the bottom centre of the screen, never focused (Horizon's wave pills).

It says what voice is doing with a wave and a word: ``Listening`` (a white sine: a conversation is open and
it is the user's turn), ``Hearing you`` (the same wave, livelier), ``Thinking`` and ``Working on it`` (a dotted
grey line: a task is running and the conversation waits to speak its result), ``Speaking`` (the accent
sine), or an error in words (no wave, in red). While only the wake word is on it stays hidden (a dim tray
tooltip says so instead): an always-visible pill would be noise all day.

An error or a note widens the pill to fit its words (up to :data:`NOTE_MAX_WIDTH`) and stays for
:data:`NOTE_MS` even if voice goes back to listening for the name meanwhile: a failure is never shown for a
blink. ``ended`` (v3.16) is the line after a conversation ended by itself ("Say 'Yuki' to talk again"), for
:data:`ENDED_MS`. The wave animates only while the pill is shown (:class:`yuki.ui.wave.Wave`: at most 30 frames a
second, stopped when hidden, still with "Reduce motion"). Purely presentational; it decides nothing.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel

from yuki.ui.surface import SurfaceWindow

PILL_WIDTH = 150
PILL_HEIGHT = 40
MARGIN = 24
#: How long an error or a "voice off" note stays before the pill fades.
NOTE_MS = 8000
#: How long the "Say 'Yuki' to talk again" line stays after a conversation ended by itself (v3.16, 04 F2).
ENDED_MS = 3000
#: An error or a note may widen the pill up to this, so the reason can be read.
NOTE_MAX_WIDTH = 520
#: States that show words of their own (the detail) for a while, then fade: how long each stays.
TIMED = {"error": NOTE_MS, "note": NOTE_MS, "ended": ENDED_MS}

#: state -> (label, ink role, wave kind or ""). The state is in its words; an error is said in red.
LOOK: dict[str, tuple[str, str, str]] = {
    "starting": ("Starting voice…", "text_2", "think"),
    "listening": ("Listening", "text", "listen"),
    "hearing": ("Hearing you", "text", "hear"),
    "thinking": ("Thinking", "text_2", "think"),
    "speaking": ("Speaking", "text", "speak"),
    "working": ("Working on it", "text_2", "think"),
    "error": ("Voice error", "danger", ""),
    "note": ("Voice", "text_2", ""),
    "ended": ("Say ‘Yuki’ to talk again", "text_2", ""),
}


class VoicePill(SurfaceWindow):
    """Bottom-centre voice status. ``show_state(state, detail)`` is the only entry point."""

    def __init__(self) -> None:
        from yuki.ui.wave import Wave
        from yuki.ui.window.theme import variable_font

        super().__init__(activates=False, radius=PILL_HEIGHT // 2)
        self.setObjectName("YukiVoicePill")
        self.setFixedSize(PILL_WIDTH + 2 * self.SHADOW, PILL_HEIGHT + 2 * self.SHADOW)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.SHADOW + 14, self.SHADOW, self.SHADOW + 18, self.SHADOW)
        row.setSpacing(11)
        self.wave = Wave("listen", theme=self.theme, size=(46, 16), parent=self)
        row.addWidget(self.wave, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel(self)
        self.label.setFont(variable_font(13.5, "Regular"))
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._ink = "text"   # the state's ink role (LOOK)
        self.restyle()
        from yuki.ui.window.widgets import attach_tips

        attach_tips(self, self.theme)   # its devices tooltip is a floating panel too
        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(self.fade_out)
        self._devices = ""
        self._width = PILL_WIDTH

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (settings changed)
        self.label.setStyleSheet(f"color: {self.ink_css(self._ink)};")
        self.wave.update()

    def set_devices(self, text: str) -> None:
        """The devices voice uses now (``Mic: …, Speaker: …``), shown as the pill's tooltip."""
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
        label, self._ink, wave = LOOK.get(state, (state.capitalize(), "text", "listen"))
        if state in ("error", "starting", "note", "ended") and detail and detail not in ("tray", "hotkey", "wake"):
            label = detail  # an error, or a note such as the voice or a device falling back
            self.setToolTip(detail + (f"\n{self._devices}" if self._devices else ""))
        elif self._devices:
            self.setToolTip(self._devices)
        self.wave.setVisible(bool(wave))
        if wave:
            self.wave.set_kind(wave)
        room = self.label.fontMetrics().horizontalAdvance(label) + 14 + 18 + (46 + 11 if wave else 0) + 2
        width = max(PILL_WIDTH, room) if state not in TIMED else min(NOTE_MAX_WIDTH, max(PILL_WIDTH, room))
        self._set_width(width)
        inner = width - 14 - 18 - (46 + 11 if wave else 0)
        self.label.setText(self.label.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, inner))
        self.restyle()
        self.update()
        if not self.isVisible() or self.closing:
            self.fade_in(self.anchor())
        else:
            self.move(self.anchor())
        if state in TIMED:
            self._fade.start(TIMED[state])
