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
    from yuki.ui.lens import density, policy, scaled_level
    from yuki.ui.window.refract import COLLECTOR, LIVE

    if not painted and (COLLECTOR.active or LIVE.on) and policy().refracts(level.name):
        if COLLECTOR.active:
            if COLLECTOR.add(p, rect, radius, level, tint, shadow, floating):
                return
        elif not floating:
            return   # the live window's cached glass image already has it
    level = scaled_level(level)   # the Transparency setting: how much of the fill is laid down
    if tint is not None:
        m = density()
        if abs(m - 1.0) > 1e-3:
            tint = QColor(tint)
            tint.setAlphaF(min(0.97, tint.alphaF() * m))
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
        paint_catchlight(p, rect, radius, level)
    p.restore()


def paint_catchlight(p: QPainter, rect: QRectF, radius: float, level: GlassLevel) -> None:
    """The v3 edge: a faint catch-light on the lit (top-left) side that fades out before the middle, and a
    fainter shade on the far side for definition. No outline goes all the way round (2026-09-27)."""
    if level.outline.alpha():   # a level that still asks for a hairline (none of the current ones)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(level.outline, 1.0))
        p.drawPath(rounded(rect.adjusted(-0.5, -0.5, 0.5, 0.5), radius + 0.5))
    # the light falls from the top left: along the diagonal the edge is bright only near that corner
    span = max(rect.width(), rect.height())
    end = QPointF(rect.left() + min(rect.width(), span), rect.top() + min(rect.height(), span))
    g = QLinearGradient(rect.topLeft(), end)
    clear = QColor(level.edge)
    clear.setAlpha(0)
    g.setColorAt(0.0, level.edge)
    g.setColorAt(0.30, clear)
    shade = QColor(level.rim)
    far = QColor(shade)
    far.setAlpha(0)
    g.setColorAt(0.70, far)
    g.setColorAt(1.0, shade)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QBrush(g), 1.0))
    p.drawPath(rounded(rect.adjusted(0.5, 0.5, -0.5, -0.5), max(0.0, radius - 0.5)))


def paint_backdrop(p: QPainter, rect: QRectF, theme: Theme, image: QImage | None, *,
                   offset: QPointF = QPointF(0, 0), dwm: bool = False, solid: bool = False) -> None:
    """The window's floor.

    ``solid`` (the live window's default since 2026-09-27): the plain background, black in dark and white in
    light. ``dwm``: DWM paints the blurred desktop behind a transparent client area ("See-through"); only a
    faint wash of the background is laid on it, for text. ``image``: a pre-blurred picture of what is behind
    (the phase-1 mockups); ``offset`` is where the window sits in it. None of them: an opaque tinted gradient
    (the fallback when transparency effects are off).
    """
    p.save()
    if solid:
        p.fillRect(rect, theme.bg)
    elif dwm:
        wash = QColor(theme.bg)
        wash.setAlphaF(0.30)
        p.fillRect(rect, wash)
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


# -- the soft backdrop the glass bends (2026-09-27) ------------------------------------------------------

#: Vertical period of the backdrop's pools of colour (logical px): a long page keeps finding some.
BLOOM_PERIOD = 1000.0
#: Width of the fade where the pages' backdrop meets the sidebar's (logical px): both are zero at the seam.
BLOOM_FADE = 110.0
#: The backdrop is painted at this fraction of the size and scaled up when shown: it is nothing but soft blur.
BLOOM_SCALE = 0.25


def _blooms(t: Theme, strength: float, width: float, height: float, split: float):
    """The pools of colour, in window coordinates at scroll 0: ``(colour, cx, cy, radius, side)``; ``side`` is
    ``"right"`` (under the pages, fading out toward ``split``) or ``"left"`` (under the sidebar). Tiles down a
    long page every :data:`BLOOM_PERIOD`."""
    dark = t.dark

    def col(c: QColor, a: float) -> QColor:
        out = QColor(c)
        out.setAlphaF(min(1.0, a * strength))
        return out

    accent = col(t.accent, 0.26 if dark else 0.20)
    violet = col(t.kind("review"), 0.22 if dark else 0.17)
    teal = col(t.kind("call"), 0.16 if dark else 0.14)
    rose = col(t.kind("question"), 0.12 if dark else 0.12)
    span = max(200.0, width - split)
    out = [(col(t.accent, 0.20 if dark else 0.16), split * 0.25, min(height, 900.0) * 0.72, 260.0, "left")]
    k, y0 = 0, 0.0
    while y0 < height + BLOOM_PERIOD:
        out += [(accent, split + span * (0.80 if k % 2 == 0 else 0.30), y0 + 0.16 * BLOOM_PERIOD, 0.50 * BLOOM_PERIOD,
                 "right"),
                (violet, split + span * (0.28 if k % 2 == 0 else 0.76), y0 + 0.60 * BLOOM_PERIOD, 0.46 * BLOOM_PERIOD,
                 "right"),
                (teal if k % 2 == 0 else rose, split + span * 0.97, y0 + 0.92 * BLOOM_PERIOD, 0.34 * BLOOM_PERIOD,
                 "right")]
        k += 1
        y0 += BLOOM_PERIOD
    return out


_BLOOM_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()


def bloom_field(t: Theme, strength: float, area: QRectF, window_w: float, window_h: float, split: float) -> QImage | None:
    """The soft backdrop under ``area`` (window coordinates at scroll 0), at :data:`BLOOM_SCALE` of its size, or
    None when ``strength`` is 0 (the plain background). The window's own background, each page and the sidebar
    ask for the rectangle they cover; the pools under the pages fade to nothing at ``split`` (the sidebar's
    edge) and the sidebar's own pool fades out before it, so no seam shows where they meet, scrolled or not.
    Cached (a few small images)."""
    if strength <= 0.0 or area.width() < 2 or area.height() < 2:
        return None
    key = (t.name, round(strength, 3), round(area.x()), round(area.y()), round(area.width()), round(area.height()),
           round(window_w), round(window_h), round(split))
    img = _BLOOM_CACHE.get(key)
    if img is not None:
        _BLOOM_CACHE.move_to_end(key)
        return img
    k = BLOOM_SCALE
    w, h = max(1, int(area.width() * k + 0.5)), max(1, int(area.height() * k + 0.5))
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(t.bg)
    for side in ("left", "right"):
        layer = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        p = QPainter(layer)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.scale(k, k)
        p.translate(-area.x(), -area.y())
        any_drawn = False
        for color, cx, cy, r, where in _blooms(t, strength, window_w, max(window_h, area.bottom()), split):
            if where != side or cy + r < area.top() or cy - r > area.bottom():
                continue
            g = QRadialGradient(QPointF(cx, cy), r)
            g.setColorAt(0.0, color)
            mid = QColor(color)
            mid.setAlphaF(color.alphaF() * 0.45)
            g.setColorAt(0.45, mid)
            clear = QColor(color)
            clear.setAlpha(0)
            g.setColorAt(1.0, clear)
            p.fillRect(QRectF(cx - r, cy - r, 2 * r, 2 * r), QBrush(g))
            any_drawn = True
        if any_drawn:   # zero at the seam between the sidebar's backdrop and the pages'
            fade = QLinearGradient(QPointF(split - BLOOM_FADE if side == "left" else split, 0),
                                   QPointF(split if side == "left" else split + BLOOM_FADE, 0))
            fade.setColorAt(0.0, QColor(0, 0, 0, 255 if side == "left" else 0))
            fade.setColorAt(1.0, QColor(0, 0, 0, 0 if side == "left" else 255))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            p.fillRect(QRectF(area.x() - 1, area.y() - 1, area.width() + 2, area.height() + 2), QBrush(fade))
        p.end()
        if any_drawn:
            q = QPainter(img)
            q.drawImage(0, 0, layer)
            q.end()
    _BLOOM_CACHE[key] = img
    while len(_BLOOM_CACHE) > 12:
        _BLOOM_CACHE.popitem(last=False)
    return img


def paint_bloom(p: QPainter, target: QRectF, field: QImage, area: QRectF, exposed: QRectF | None = None) -> None:
    """Paint ``field`` (made by :func:`bloom_field` for ``area``) scaled to ``target``; only ``exposed`` (in
    ``target``'s coordinates) when given, so a scroll step paints just the strip it uncovers."""
    k = BLOOM_SCALE
    if exposed is None:
        exposed = target
    part = exposed.intersected(target)
    if part.isEmpty():
        return
    src = QRectF((part.x() - target.x()) * k, (part.y() - target.y()) * k, part.width() * k, part.height() * k)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.drawImage(part, field, src)
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
