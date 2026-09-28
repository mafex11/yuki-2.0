"""Painting helpers for the Horizon x Field Unit look: rounded paths, a flat surface (fill + 1 px border), a
hardware key (a 2 px bottom edge) with its status LED, dot-matrix text (Doto, with Segoe UI Variable Display's
light colon, as the mockups set it), the wave indicators' curves, and a soft shadow for floating dialogs.
Pure QPainter; no window, no native call, no caches beyond a few small shadow images.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
import math

from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from yuki.ui.window.theme import Surface, Theme, dot_font, variable_font


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


# -- Field Unit: keys, LEDs, dot-matrix text -----------------------------------------------------------


def paint_key(p: QPainter, rect: QRectF, t: Theme, *, on: bool = False, hover: bool = False, down: bool = False,
              radius: float | None = None, bottom: float = 2.0) -> None:
    """A hardware key: its face on a 1 px line, the bottom edge ``bottom`` px thick (pressed: the face drops
    onto it). ``on``: the lit face of the selected key. ``radius``: default scaled to the key's size
    (:func:`~yuki.ui.window.theme.corner`); a capsule passes half its height."""
    from yuki.ui.window.theme import corner

    if radius is None:
        radius = corner(rect.width(), rect.height())
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(t.border)
    p.drawPath(rounded(rect, radius))
    face = QColor(t.key_on if on else t.key)
    if hover and not on:
        face = face.lighter(112)
    drop = bottom - 1.0 if down else 0.0
    inner = rect.adjusted(1.0, 1.0 + drop, -1.0, -bottom + drop)
    p.setBrush(face)
    p.drawPath(rounded(inner, max(0.0, radius - 1.0)))
    p.restore()


def paint_led(p: QPainter, center: QPointF, color: QColor, d: float = 5.0) -> None:
    """A status LED: a small round dot (the accent when live, the line colour when off)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(center, d / 2.0, d / 2.0)
    p.restore()


#: Characters a dot-matrix display sets in Segoe UI Variable Display Light (the mockups' ``.c`` span).
DOT_SEPARATORS = ":."


def _dot_parts(text: str, px: float) -> list[tuple[str, QFont]]:
    dot, sep = dot_font(px), variable_font(px, "Light", display=True)
    parts: list[tuple[str, QFont]] = []
    for ch in text:
        f = sep if ch in DOT_SEPARATORS else dot
        if parts and parts[-1][1] is f:
            parts[-1] = (parts[-1][0] + ch, f)
        else:
            parts.append((ch, f))
    return parts


def dots_width(text: str, px: float) -> float:
    """How wide ``text`` is as dot-matrix text at ``px``."""
    return sum(QFontMetricsF(f).horizontalAdvance(s) + (px * 0.04 if f.family() != "Doto" else 0.0)
               for s, f in _dot_parts(text, px))


class _DotCache:
    """Rendered dot-matrix strings (text, size, colour, pixel ratio) -> pixmap: the displays and the skyline's
    time flag repaint on every hover frame, and a cached string is one blit instead of two fonts' shaping."""

    def __init__(self, size: int = 96) -> None:
        self._items: OrderedDict[tuple, tuple] = OrderedDict()
        self._size = size
        self.hits = 0
        self.misses = 0

    def get(self, text: str, px: float, color: QColor, dpr: float) -> tuple:
        from PySide6.QtGui import QPixmap

        key = (text, round(px, 2), color.rgba(), round(dpr, 3))
        got = self._items.get(key)
        if got is not None:
            self._items.move_to_end(key)
            self.hits += 1
            return got
        self.misses += 1
        width = dots_width(text, px)
        dot_fm = QFontMetricsF(dot_font(px))
        cap = dot_fm.capHeight() or px * 0.7
        h = px * 1.4
        pm = QPixmap(max(1, int((width + 4) * dpr)), max(1, int(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        q.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        base = h / 2 + cap / 2
        x = 1.0
        q.setPen(color)
        for part, f in _dot_parts(text, px):
            q.setFont(f)
            fm = QFontMetricsF(f)
            pad = px * 0.02 if f.family() != "Doto" else 0.0
            q.drawText(QPointF(x + pad, base), part)
            x += fm.horizontalAdvance(part) + 2 * pad
        q.end()
        got = (pm, width, h)
        self._items[key] = got
        if len(self._items) > self._size:
            self._items.popitem(last=False)
        return got


DOTS = _DotCache()


def paint_dots(p: QPainter, rect: QRectF, text: str, px: float, color: QColor, *,
               align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> float:
    """Draw ``text`` in Doto at ``px`` inside ``rect`` (vertically centred on the digits' height), the colon and
    point in Segoe UI Variable Display Light; from :data:`DOTS` after the first time. Returns the width."""
    dpr = p.device().devicePixelRatioF() if p.device() is not None else 1.0
    pm, width, h = DOTS.get(text, px, color, dpr)
    if align & Qt.AlignmentFlag.AlignRight:
        x = rect.right() - width
    elif align & Qt.AlignmentFlag.AlignHCenter:
        x = rect.center().x() - width / 2.0
    else:
        x = rect.left()
    p.drawPixmap(QPointF(x - 1.0, rect.center().y() - h / 2), pm)
    return width


# -- the wave indicators ---------------------------------------------------------------------------


def wave_path(rect: QRectF, phase: float, *, cycles: float = 1.6, amp: float = 1.0) -> QPainterPath:
    """A smooth, slightly irregular sine across ``rect`` (the listening / speaking / check-in waves): two sines
    under an envelope that keeps the ends near the middle line. ``phase`` in radians moves it along."""
    w, h = rect.width(), rect.height()
    mid = rect.center().y()
    a = (h / 2.0 - 1.0) * amp
    n = max(12, int(w / 2))
    path = QPainterPath()
    for i in range(n + 1):
        u = i / n
        env = 0.3 + 0.7 * math.sin(math.pi * u)
        y = 0.68 * math.sin(2 * math.pi * cycles * u + phase) + 0.32 * math.sin(2 * math.pi * cycles * 1.9 * u
                                                                                 - 1.6 * phase + 0.8)
        pt = QPointF(rect.left() + u * w, mid - a * env * y)
        if i == 0:
            path.moveTo(pt)
        else:
            path.lineTo(pt)
    return path


def paint_wave(p: QPainter, rect: QRectF, color: QColor, phase: float = 0.0, *, width: float = 1.5,
               cycles: float = 1.6, amp: float = 1.0) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(wave_path(rect, phase, cycles=cycles, amp=amp))
    p.restore()


def paint_dotted(p: QPainter, rect: QRectF, color: QColor, offset: float = 0.0, *, width: float = 1.5) -> None:
    """The thinking line: dots on the middle line (2 on, 4 off, as the mockup), drifting with ``offset``."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setDashPattern([2.0 / width, 4.0 / width])
    pen.setDashOffset(-offset / width)
    p.setPen(pen)
    y = rect.center().y()
    p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
    p.restore()
