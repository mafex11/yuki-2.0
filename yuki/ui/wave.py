"""The wave indicators (Horizon): one small painted widget for every place Yuki shows a voice or live state.

* ``listen``  -- a white sine: Yuki is listening (or hearing you).
* ``think``   -- a dotted grey line: thinking or working.
* ``speak``   -- the accent sine: Yuki is speaking.
* ``call``    -- a small grey sine beside the call pill's recording dot and timer.
* ``checkin`` -- the check-in card's small cool-grey wave.

Lightweight by construction: every wave in the process shares one timer (:data:`TICKER`, at most
:data:`MAX_FPS` a second; a slow kind skips frames), and a wave is on it only while it is visible. Each tick
repaints only the waves whose frame is due, and only their own small rectangle. Hidden (or with its window
hidden) a wave leaves the ticker, and with no wave shown the timer stops: nothing runs. With "Reduce
motion" on a wave draws one still frame and never joins. The curve is a few dozen points.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

#: The fastest any wave repaints (frames a second).
MAX_FPS = 30

#: kind -> (frames a second, radians of phase a second, cycles across, stroke width).
KINDS: dict[str, tuple[int, float, float, float]] = {
    "listen": (30, 3.2, 1.6, 1.5),
    "hear": (30, 5.0, 1.8, 1.6),
    "think": (20, 14.0, 0.0, 1.5),     # the dots drift (px a second)
    "speak": (30, 4.4, 1.3, 1.6),
    "call": (12, 2.0, 1.4, 1.3),
    "checkin": (15, 1.2, 2.2, 1.3),
}


def reduce_motion() -> bool:
    try:
        from yuki.ui import ui_settings

        return bool(ui_settings.current().reduce_motion)
    except Exception:
        return False


class _Ticker:
    """The one timer every shown wave rides on (created with the first wave, on the GUI thread)."""

    def __init__(self) -> None:
        self.waves: set[Wave] = set()
        self.timer: QTimer | None = None
        self.ticks = 0

    def _ensure(self) -> QTimer:
        if self.timer is None:
            self.timer = QTimer()
            self.timer.setTimerType(Qt.TimerType.CoarseTimer)
            self.timer.setInterval(max(1, int(1000 / MAX_FPS)))
            self.timer.timeout.connect(self._tick)
        return self.timer

    def add(self, wave: "Wave") -> None:
        self.waves.add(wave)
        timer = self._ensure()
        if not timer.isActive():
            timer.start()

    def remove(self, wave: "Wave") -> None:
        self.waves.discard(wave)
        if not self.waves and self.timer is not None:
            self.timer.stop()

    @property
    def running(self) -> bool:
        return self.timer is not None and self.timer.isActive()

    def _tick(self) -> None:
        self.ticks += 1
        if reduce_motion():
            for w in list(self.waves):
                self.remove(w)
                w.update()
            return
        for w in list(self.waves):
            try:
                w._frame()
            except RuntimeError:   # its C++ side is gone
                self.waves.discard(w)
        if not self.waves and self.timer is not None:
            self.timer.stop()


#: The shared ticker.
TICKER = _Ticker()


class Wave(QWidget):
    """A wave of one ``kind`` (see the module docstring). ``theme``: a callable returning the current
    :class:`~yuki.ui.window.theme.Theme` (the accent is read at paint time, so an accent change shows at the
    next frame)."""

    def __init__(self, kind: str = "listen", *, theme=None, size: QSize | tuple[int, int] = (46, 16),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.kind = kind if kind in KINDS else "listen"
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        w, h = (size.width(), size.height()) if isinstance(size, QSize) else size
        self.setFixedSize(w, h)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = -1e9
        self.frames = 0
        self.destroyed.connect(lambda *_a, w=self: TICKER.waves.discard(w))

    # -- state ------------------------------------------------------------------------------------

    def theme(self):
        if self._theme is not None:
            return self._theme()
        from yuki.ui.window.theme import theme

        return theme()

    def set_kind(self, kind: str) -> None:
        kind = kind if kind in KINDS else "listen"
        if kind != self.kind:
            self.kind = kind
            self.update()

    @property
    def animating(self) -> bool:
        """On the shared ticker (shown, motion allowed)."""
        return self in TICKER.waves and TICKER.running

    def color(self) -> QColor:
        t = self.theme()
        return {"listen": t.text, "hear": t.text, "think": t.text_3, "speak": t.accent, "call": t.text_2,
                "checkin": t.cool}[self.kind]

    # -- the timer runs only while shown -------------------------------------------------------------

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        if not reduce_motion():
            TICKER.add(self)

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        TICKER.remove(self)

    def _frame(self) -> None:
        """A tick of the shared timer: repaint when this kind's next frame is due."""
        if not self.isVisible():
            TICKER.remove(self)
            return
        now = self._clock.elapsed()
        if now - self._last_ms < 1000.0 / min(MAX_FPS, KINDS[self.kind][0]) - 2:
            return
        self._last_ms = now
        self.frames += 1
        self.update()

    # -- painting -------------------------------------------------------------------------------------

    def phase(self) -> float:
        if reduce_motion():
            return 0.9
        return self._clock.elapsed() / 1000.0 * KINDS[self.kind][1]

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from yuki.ui.window.paint import paint_dotted, paint_wave

        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        _fps, _speed, cycles, width = KINDS[self.kind]
        if self.kind == "think":
            paint_dotted(p, r, self.color(), self.phase() % 6.0, width=width)
        else:
            amp = 1.0 if self.kind != "call" else 0.9
            if self.kind == "hear":
                amp = 0.8 + 0.2 * math.sin(self.phase() * 0.7)
            paint_wave(p, r, self.color(), self.phase(), width=width, cycles=cycles, amp=amp)
        p.end()
