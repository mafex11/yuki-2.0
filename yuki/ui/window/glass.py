"""Painting the Liquid Glass material with QPainter.

Everything here is pure painting: no window, no native call. The same code
draws the real window (over DWM's backdrop) and the offscreen mockups (over a
painted, blurred wallpaper that stands in for the desktop).

* :func:`paint_glass` -- one glass shape: soft shadow, translucent fill, a top
  sheen, the specular edge (bright top-left, faint rim bottom-right) and a
  hairline outline. Shadows are blurred once per size and cached.
* :func:`paint_backdrop` -- what the window lies on: DWM's blur (nothing to
  paint but the tint), a blurred image (mockups / painted fallback), or the
  opaque tinted gradient when neither is available.
* :func:`blur_image` -- a cheap, good-looking blur by repeated smooth down- and
  up-scaling (no per-pixel Python).
* :func:`wallpaper` -- the simulated desktop the mockups sit on.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from yuki.ui.window.theme import GlassLevel, Theme


def rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    r = min(radius, rect.height() / 2, rect.width() / 2)
    path.addRoundedRect(rect, r, r)
    return path


def blur_image(img: QImage, radius: float, passes: int = 3) -> QImage:
    """A soft blur of ``img`` (radius in px) by smooth down/up-scaling."""
    if radius <= 0.5:
        return img
    w, h = img.width(), img.height()
    factor = max(2.0, radius / 2.0)
    out = img
    for _ in range(passes):
        small = out.scaled(max(1, int(w / factor)), max(1, int(h / factor)),
                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return out


class _ShadowCache:
    """Blurred rounded-rect shadows, keyed by geometry and colour (LRU, small)."""

    def __init__(self, size: int = 96) -> None:
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._size = size

    def get(self, w: int, h: int, radius: float, blur: int, color: QColor, dpr: float) -> QImage:
        key = (w, h, round(radius), blur, color.rgba(), dpr)
        img = self._items.get(key)
        if img is not None:
            self._items.move_to_end(key)
            return img
        pad = blur * 2
        img = QImage(int((w + 2 * pad) * dpr), int((h + 2 * pad) * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.scale(dpr, dpr)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(rounded(QRectF(pad, pad, w, h), radius))
        p.end()
        img = blur_image(img, blur * dpr * 0.9)
        img.setDevicePixelRatio(dpr)
        self._items[key] = img
        if len(self._items) > self._size:
            self._items.popitem(last=False)
        return img

    def clear(self) -> None:
        self._items.clear()


SHADOWS = _ShadowCache()


def paint_shadow(p: QPainter, rect: QRectF, radius: float, level: GlassLevel) -> None:
    if level.shadow_blur <= 0 or level.shadow.alpha() == 0:
        return
    dpr = p.device().devicePixelRatioF() if p.device() else 1.0
    img = SHADOWS.get(int(rect.width()), int(rect.height()), radius, level.shadow_blur, level.shadow, dpr)
    pad = level.shadow_blur * 2
    p.drawImage(QPointF(rect.x() - pad, rect.y() - pad + level.shadow_y), img)


def paint_glass(
    p: QPainter, rect: QRectF, radius: float, level: GlassLevel, *,
    tint: QColor | None = None, shadow: bool = True, edge: bool = True, floating: bool = False,
    painted: bool = False,
) -> None:
    """One glass shape (see the module docstring). ``tint`` washes the fill with a colour.

    While :data:`yuki.ui.window.refract.COLLECTOR` is active the shape is recorded for the GPU (real
    refraction) and nothing is painted here. ``floating``: the shape lies over content painted before it
    (a popover), so it refracts that content. ``painted``: always painted here, never refracted (glass
    that changes size while it streams, like a chat bubble: re-rendering it would cost every frame).
    """
    from yuki.ui.lens import policy
    from yuki.ui.window.refract import COLLECTOR, LIVE

    if not painted and (COLLECTOR.active or LIVE.on) and policy().refracts(level.name):
        if COLLECTOR.active:
            if COLLECTOR.add(p, rect, radius, level, tint, shadow, floating):
                return
        elif not floating:
            return   # the live window's cached glass image already has it
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if shadow:
        paint_shadow(p, rect, radius, level)
    path = rounded(rect, radius)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(level.fill)
    p.drawPath(path)
    if tint is not None:
        p.setBrush(tint)
        p.drawPath(path)
    # top sheen: light falling on the upper part of the sheet
    if level.sheen.alpha():
        g = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + max(18.0, rect.height() * 0.5)))
        g.setColorAt(0.0, level.sheen)
        clear = QColor(level.sheen)
        clear.setAlpha(0)
        g.setColorAt(1.0, clear)
        p.setBrush(QBrush(g))
        p.drawPath(path)
    if edge:
        # hairline outline just outside the specular edge: definition on bright backdrops
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(level.outline, 1.0))
        p.drawPath(rounded(rect.adjusted(-0.5, -0.5, 0.5, 0.5), radius + 0.5))
        # specular edge: bright at the top left, clear across the middle, a faint rim bottom right
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, level.edge)
        mid = QColor(level.edge)
        mid.setAlpha(int(level.edge.alpha() * 0.18))
        g.setColorAt(0.38, mid)
        g.setColorAt(0.62, mid)
        g.setColorAt(1.0, level.rim)
        p.setPen(QPen(QBrush(g), 1.2))
        p.drawPath(rounded(rect.adjusted(0.6, 0.6, -0.6, -0.6), max(0.0, radius - 0.6)))
    p.restore()


def paint_backdrop(p: QPainter, rect: QRectF, theme: Theme, image: QImage | None, *,
                   offset: QPointF = QPointF(0, 0), dwm: bool = False) -> None:
    """The window's floor.

    ``dwm``: DWM paints the blurred backdrop behind a transparent client area;
    only the tint is laid on it. ``image``: a pre-blurred picture of what is
    behind (mockups, or a painted fallback from the wallpaper); ``offset`` is
    where the window sits in it. Neither: an opaque tinted gradient (the fallback
    when transparency effects are off).
    """
    p.save()
    if dwm:
        p.fillRect(rect, theme.window_tint)
    elif image is not None:
        dpr = image.devicePixelRatio() or 1.0
        src = QRectF(offset.x() * dpr, offset.y() * dpr, rect.width() * dpr, rect.height() * dpr)
        p.drawImage(rect, image, src)
        p.fillRect(rect, theme.window_tint)
        # acrylic's faint noise is replaced by a gentle luminosity lift at the top
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0.0, QColor(255, 255, 255, 26 if not theme.dark else 8))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(rect, QBrush(g))
    else:
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0.0, theme.fallback_top)
        g.setColorAt(1.0, theme.fallback_bottom)
        p.fillRect(rect, QBrush(g))
    p.restore()


def hatch(p: QPainter, rect: QRectF, color: QColor, spacing: float = 6.0, width: float = 1.0) -> None:
    """45-degree hatching inside ``rect`` (PC off / asleep)."""
    p.save()
    p.setClipRect(rect)
    p.setPen(QPen(color, width))
    x = rect.left() - rect.height()
    while x < rect.right():
        p.drawLine(QPointF(x, rect.bottom()), QPointF(x + rect.height(), rect.top()))
        x += spacing
    p.restore()


def wallpaper(theme: Theme, size: QSize, dpr: float = 1.0) -> QImage:
    """A soft, colourful stand-in for the desktop behind the mockups (Windows 11 bloom-like)."""
    img = QImage(int(size.width() * dpr), int(size.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w, h = size.width(), size.height()
    base = QLinearGradient(0, 0, w, h)
    base.setColorAt(0.0, theme.wallpaper_base[0])
    base.setColorAt(1.0, theme.wallpaper_base[1])
    p.fillRect(QRectF(0, 0, w, h), QBrush(base))
    for color, fx, fy, fr in theme.wallpaper:
        r = fr * max(w, h)
        g = QRadialGradient(QPointF(fx * w, fy * h), r)
        g.setColorAt(0.0, color)
        clear = QColor(color)
        clear.setAlpha(0)
        g.setColorAt(1.0, clear)
        p.fillRect(QRectF(0, 0, w, h), QBrush(g))
    # a few crisp shapes, so the blur behind the glass has something to blur
    p.setPen(Qt.PenStyle.NoPen)
    for i, (fx, fy, fs) in enumerate(((0.08, 0.12, 0.10), (0.9, 0.7, 0.14), (0.62, 0.08, 0.07), (0.3, 0.8, 0.09))):
        c = QColor(theme.wallpaper[i % len(theme.wallpaper)][0])
        c.setAlpha(150)
        p.setBrush(c)
        s = fs * w
        p.drawEllipse(QRectF(fx * w - s / 2, fy * h - s / 2, s, s))
    p.end()
    return img
