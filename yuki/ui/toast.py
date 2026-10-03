"""Floating notifications in the Lattice style (the design's ``.toast``): one line with a live check, bottom centre of the
monitor in use, never focused. In 220 ms (fade and 8 px rise, the design's ease), stays its time (2.2 s by default,
longer for longer words), then leaves in 420 ms. A new toast replaces the one up."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPropertyAnimation, QRectF, Qt, QTimer, QParallelAnimationGroup
from PySide6.QtGui import QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from yuki.ui import lattice as L

#: The design's toast: 2.2 s on screen, in 220 ms, out 420 ms, 74 px above the screen's bottom.
TOAST_MS = int(L.DUR["toast"])
IN_MS = 220
OUT_MS = int(L.DUR["leave"])
BOTTOM_PX = 74
#: Longer words stay longer (a note about a device change is read, not glanced at), up to this.
MAX_MS = 8000


def toast_ms(text: str) -> int:
    """How long a toast stays: the design's 2.2 s, plus reading time past ~40 characters, at most 8 s."""
    return min(MAX_MS, max(TOAST_MS, 1200 + 45 * len(" ".join(str(text or "").split()))))


class Toast(QWidget):
    """One toast window (shared: :func:`show_toast`)."""

    _shared: "Toast | None" = None

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setObjectName("YukiToast")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.text = ""
        self.icon = "check"
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self.leave)
        self._group = QParallelAnimationGroup(self)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._move = QPropertyAnimation(self, b"pos", self)
        self._group.addAnimation(self._fade)
        self._group.addAnimation(self._move)
        self._group.finished.connect(self._done)
        self._leaving = False

    @classmethod
    def shared(cls) -> "Toast":
        if cls._shared is None:
            cls._shared = Toast()
        return cls._shared

    def _fit(self) -> None:
        fm = QFontMetricsF(L.ui_font(13.5))
        w = 14 + 12 + 10 + fm.horizontalAdvance(self.text) + 14 + 2
        self.setFixedSize(int(round(w)) + 1, int(round(13.5 * 1.45 + 20 + 2)))

    def target(self) -> QPoint:
        from yuki.ui.island import active_screen

        screen = active_screen()
        geo = screen.geometry() if screen is not None else self.geometry()
        return QPoint(geo.center().x() - self.width() // 2, geo.bottom() + 1 - BOTTOM_PX - self.height())

    def show_text(self, text: str, *, icon: str = "check", ms: int | None = None) -> None:
        self.text, self.icon = " ".join(str(text or "").split()), icon
        self._fit()
        self._group.stop()
        self._leaving = False
        target = self.target()
        reduced = L.reduced()
        if not self.isVisible():
            self.setWindowOpacity(1.0 if reduced else 0.0)
            self.move(target + QPoint(0, 0 if reduced else 8))
            self.show()
        self.update()
        if not reduced:
            for anim, a, b in ((self._fade, self.windowOpacity(), 1.0), (self._move, self.pos(), target)):
                anim.setDuration(IN_MS)
                anim.setEasingCurve(L.ease_curve())
                anim.setStartValue(a)
                anim.setEndValue(b)
            self._group.start()
        else:
            self.move(target)
        self._hold.start(ms if ms is not None else toast_ms(self.text))

    def leave(self) -> None:
        if not self.isVisible():
            return
        if L.reduced():
            self.hide()
            return
        self._leaving = True
        for anim, a, b in ((self._fade, self.windowOpacity(), 0.0), (self._move, self.pos(), self.pos() + QPoint(0, 8))):
            anim.setDuration(OUT_MS)
            anim.setEasingCurve(L.ease_curve())
            anim.setStartValue(a)
            anim.setEndValue(b)
        self._group.start()

    def _done(self) -> None:
        if self._leaving:
            self._leaving = False
            self.hide()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 11.5, 11.5)
        p.fillPath(path, L.BG)
        p.setPen(QPen(L.PANEL_BORDER, 1.0))
        p.drawPath(path)
        L.paint_icon(p, QRectF(15, (self.height() - 12) / 2, 12, 12), self.icon, L.live())
        p.setPen(L.TEXT)
        p.setFont(L.ui_font(13.5))
        p.drawText(QRectF(15 + 12 + 10, 0, self.width() - 37 - 14, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)
        p.end()


def show_toast(text: str, *, icon: str = "check", ms: int | None = None) -> Toast:
    """Show ``text`` as the Lattice toast (replacing one that is up)."""
    t = Toast.shared()
    t.show_text(text, icon=icon, ms=ms)
    return t


__all__ = ["Toast", "show_toast", "toast_ms"]
