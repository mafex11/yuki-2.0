"""Flat painting helpers for the shadcn look: rounded paths, a surface (fill + 1 px border), a soft shadow for
floating popovers, and the timeline's hatching. Pure QPainter; no window, no native call, no caches beyond a
few small shadow images.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from yuki.ui.window.theme import Surface


def rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    r = max(0.0, min(radius, rect.height() / 2, rect.width() / 2))
    path.addRoundedRect(rect, r, r)
    return path


def paint_surface(p: QPainter, rect: QRectF, radius: float, level: Surface, *, fill: QColor | None = None,
                  border: QColor | None = None) -> None:
    """One flat surface: its fill, then its 1 px border drawn on the pixel grid (crisp at 100% and 150%).
    ``fill`` / ``border`` override the level's."""
    fill = level.fill if fill is None else fill
    border = level.border if border is None else border
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if fill.alpha():
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(rounded(rect, radius))
    if border.alpha():
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(border, 1.0))
        p.drawPath(rounded(rect.adjusted(0.5, 0.5, -0.5, -0.5), max(0.0, radius - 0.5)))
    p.restore()


def paint_ring(p: QPainter, rect: QRectF, radius: float, color: QColor, width: float = 2.0) -> None:
    """The focus ring: ``width`` px just outside ``rect`` (shadcn's ``ring-2 ring-ring/50``)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(color, width))
    half = width / 2
    p.drawPath(rounded(rect.adjusted(-half, -half, half, half), radius + half))
    p.restore()


def _blur(img: QImage, radius: float, passes: int = 3) -> QImage:
    """A soft blur by smooth down/up-scaling (no per-pixel Python)."""
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
    """Blurred rounded-rect shadows (shadcn's ``shadow-md`` under popovers), keyed by size (LRU, small)."""

    def __init__(self, size: int = 24) -> None:
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._size = size

    def get(self, w: int, h: int, radius: float, blur: int, color: QColor, dpr: float) -> QImage:
        key = (w, h, round(radius), blur, color.rgba(), round(dpr, 3))
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
        img = _blur(img, blur * dpr * 0.9)
        img.setDevicePixelRatio(dpr)
        self._items[key] = img
        if len(self._items) > self._size:
            self._items.popitem(last=False)
        return img

    def clear(self) -> None:
        self._items.clear()


SHADOWS = _ShadowCache()


def paint_shadow(p: QPainter, rect: QRectF, radius: float, *, blur: int = 10, dy: int = 4,
                 color: QColor | None = None) -> None:
    """A soft shadow under a floating surface (drawn once per size, then reused)."""
    color = color or QColor(0, 0, 0, 110)
    dpr = p.device().devicePixelRatioF() if p.device() else 1.0
    img = SHADOWS.get(int(rect.width()), int(rect.height()), radius, blur, color, dpr)
    pad = blur * 2
    p.drawImage(QPointF(rect.x() - pad, rect.y() - pad + dy), img)


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
