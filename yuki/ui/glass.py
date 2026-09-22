"""The dark-glass window base.

Both surfaces Yuki shows -- the overlay and the status strip -- are the same
object: a frameless, translucent, always-on-top tool window with rounded corners,
a hairline border and a soft shadow, that fades and slides when it appears.

The shadow is painted, not a ``QGraphicsDropShadowEffect``: graphics effects on a
translucent top-level window are unreliable and force the whole widget tree
through an offscreen pixmap. So the window keeps a transparent margin
(:data:`GlassWindow.SHADOW`) around a "panel" rectangle, and ``paintEvent`` draws
the falloff into that margin itself.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

#: Duration of the show/hide transition, in milliseconds.
ANIM_MS = 150

#: How far the window travels while fading, in logical pixels.
SLIDE_PX = 8

#: Palette. Near-black glass at ~85% opacity, with light text.
GLASS_BG = QColor(16, 17, 20, 217)
GLASS_BORDER = QColor(255, 255, 255, 28)
TEXT_PRIMARY = QColor(238, 240, 245)
TEXT_DIM = QColor(238, 240, 245, 150)
CARD_BG = QColor(255, 255, 255, 14)
CARD_BORDER = QColor(255, 255, 255, 20)
ERROR_BG = QColor(224, 72, 72, 46)
ERROR_BORDER = QColor(255, 120, 120, 90)
ACCENT = QColor(126, 180, 255)


def ui_font(size: int = 11, *, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The system UI font at a given point size.

    Args:
        size: Point size.
        weight: Font weight.

    Returns:
        A copy of the platform's general UI font (Segoe UI Variable on Windows 11).
    """
    font = QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    font.setPointSize(size)
    font.setWeight(weight)
    return font


class GlassWindow(QWidget):
    """A frameless translucent panel that fades and slides in and out.

    Args:
        activates: True if the window should take keyboard focus when shown (the
            overlay does; the status strip must not, or it would pull focus out
            of whatever the user is doing).
        radius: Corner radius of the panel.

    Signals:
        faded_out: Emitted once the hide animation has finished and the window is
            actually hidden.
    """

    #: Transparent padding reserved for the painted shadow, in logical pixels.
    SHADOW = 16

    faded_out = Signal()

    def __init__(self, *, activates: bool = True, radius: int = 16) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool  # no taskbar button, no alt-tab entry
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.radius = radius
        self._activates = activates
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if not activates:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFont(ui_font())

        self._anim = QParallelAnimationGroup(self)
        self._opacity = QPropertyAnimation(self, b"windowOpacity", self)
        self._move = QPropertyAnimation(self, b"pos", self)
        for animation in (self._opacity, self._move):
            animation.setDuration(ANIM_MS)
            self._anim.addAnimation(animation)
        self._anim.finished.connect(self._on_anim_finished)
        self._closing = False
        self._shadow_pixmap: QPixmap | None = None
        self._shadow_key: object = None

    # -- painting ----------------------------------------------------------

    def panel_rect(self) -> QRect:
        """The opaque part of the window: everything inside the shadow margin."""
        return self.rect().adjusted(self.SHADOW, self.SHADOW, -self.SHADOW, -self.SHADOW)

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawPixmap(0, 0, self._shadow())

        panel = QRectF(self.panel_rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(GLASS_BG)
        painter.drawRoundedRect(panel, self.radius, self.radius)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(GLASS_BORDER, 1.0))
        painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), self.radius, self.radius)

    def _shadow(self) -> QPixmap:
        """The painted drop shadow for the current size, rendered once and cached.

        Concentric rounded strokes with a quadratic alpha falloff, drawn into the
        transparent margin. Cached because a keystroke in the overlay repaints the
        whole window and the falloff is dozens of paths.
        """
        ratio = self.devicePixelRatioF()
        if self._shadow_pixmap is not None and self._shadow_key == (self.size(), ratio):
            return self._shadow_pixmap
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        panel = QRectF(self.panel_rect())
        steps = self.SHADOW
        for step in range(steps, 0, -1):
            alpha = int(58 * (1 - step / steps) ** 2.2) + 2
            painter.setPen(QPen(QColor(0, 0, 0, alpha), 1.0))
            painter.drawRoundedRect(
                panel.adjusted(-step, -step + 1, step, step + 2),
                self.radius + step,
                self.radius + step,
            )
        painter.end()
        self._shadow_key = (self.size(), ratio)
        self._shadow_pixmap = pixmap
        return pixmap

    # -- show / hide -------------------------------------------------------

    def fade_in(self, target: QPoint) -> None:
        """Show the window at ``target``, fading up and sliding into place.

        Args:
            target: Final top-left position, in global logical coordinates.
        """
        self._anim.stop()
        self._closing = False
        start_opacity = self.windowOpacity() if self.isVisible() else 0.0
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.move(target + QPoint(0, SLIDE_PX))
            self.show()
            if self._activates:
                self.raise_()
                self.activateWindow()
        self._opacity.setStartValue(start_opacity)
        self._opacity.setEndValue(1.0)
        self._opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(target)
        self._move.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

    def fade_out(self) -> None:
        """Fade and slide away, then hide. Emits :attr:`faded_out` when done."""
        if not self.isVisible():
            return
        self._anim.stop()
        self._closing = True
        self._opacity.setStartValue(self.windowOpacity())
        self._opacity.setEndValue(0.0)
        self._opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(self.pos() + QPoint(0, SLIDE_PX))
        self._move.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.start()

    @property
    def closing(self) -> bool:
        """True while the hide animation is running."""
        return self._closing

    def _on_anim_finished(self) -> None:
        if self._closing:
            self._closing = False
            self.hide()
            self.faded_out.emit()
