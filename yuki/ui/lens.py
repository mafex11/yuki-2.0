"""Liquid Glass rendered like a static picture: only when something it depends on changes, then cached.

The user's rule for the glass (2026-09-26): little to no GPU, CPU or RAM. Nothing
here runs per frame, while idle, or while the mouse moves. A surface asks for its
glass when its size, place, theme, the wallpaper or a glass setting changes; the
result is an ordinary image the widget paints like any other.

Three settings (``%LOCALAPPDATA%\\Yuki\\ui.json``, :mod:`yuki.ui.ui_settings`):

* **Off** -- clean translucent panels with crisp edges, painted by Qt
  (:func:`yuki.ui.window.glass.paint_glass`). No refraction, no GPU device.
* **Light** -- real refraction on the large glass (the window's sheet, panels,
  cards, the overlay, the pill and the strip), on the CPU, no GPU device:
  the flat middle of a lens bends nothing, so it is painted by Qt exactly (the
  same tone range and tint as the shader); only the curved rim band is refracted,
  per pixel, with numpy, from a displacement map computed once per shape size and
  reused (:class:`_BandCache`). Controls (pills, rows, the selection) stay
  crisp painted glass. No dispersion, no vibrancy boost.
* **Full** -- the GPU shader (:mod:`yuki.ui.window.refract`: dispersion,
  droplet pills, every depth), offscreen on Direct3D 11, still rendered only on
  change and read back into an image. The device is created on first use and
  released with the window's process.

"Glass bending" scales how far the rim bends light (the lens height) and "Blur"
scales the frost, for both Light and Full.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Sequence

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

#: Kept for older callers. Since 2026-09-27 the window has a plain background (or DWM's blur of the desktop,
#: which no app can read), so page glass has nothing of its own to bend in Light: it is painted crisp, and
#: Light refracts only glass that floats over the app's own content (see :func:`refract_under`).
LIGHT_LEVELS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GlassPolicy:
    """What the glass does in this process (set from the UI settings file)."""

    mode: str = "full"          # off | light | full
    bend: float = 1.0           # 0..2: how far the rim bends light
    blur: float = 1.0           # 0..2: frost
    reduce_motion: bool = False
    see_through: bool = False   # the window shows DWM's blur of the desktop instead of its plain background

    def refracts(self, level: str | None) -> bool:
        """Whether page glass of ``level`` is part of its page's cached picture (it always is: drawn on a
        change, then only composited; Full refracts it on the GPU, Light and Off paint it flat, once)."""
        del level
        return True

    @property
    def gpu(self) -> bool:
        """Whether a page's glass picture is refracted on the GPU (Full on the plain background)."""
        return self.mode == "full" and not self.see_through

    @property
    def floats_refract(self) -> bool:
        """Whether glass floating over the app's content (hover card, toast, menus, tooltips) bends it."""
        return self.mode in ("light", "full")


_POLICY = GlassPolicy()
_LOCK = threading.Lock()


def policy() -> GlassPolicy:
    return _POLICY


def set_policy(p: GlassPolicy) -> None:
    global _POLICY
    with _LOCK:
        _POLICY = p


def scaled_optics(o: Any, p: GlassPolicy | None = None) -> Any:
    """``o`` (a :class:`~yuki.ui.window.theme.Optics`) with the bending and blur settings applied."""
    p = p or _POLICY
    if p.bend == 1.0 and p.blur == 1.0:
        return o
    return replace(o, height=o.height * max(0.0, p.bend), blur=o.blur * max(0.0, p.blur))


# -- geometry: the signed distance field of a rounded rectangle / squircle ----------------------------


def _sd(lx: np.ndarray, ly: np.ndarray, bx: float, by: float, r: float, n: float) -> np.ndarray:
    qx = np.abs(lx) - bx + r
    qy = np.abs(ly) - by + r
    mx = np.maximum(qx, 0.0)
    my = np.maximum(qy, 0.0)
    if n <= 2.001:
        c = np.hypot(mx, my)
    else:
        c = np.power(np.power(mx, n) + np.power(my, n), 1.0 / n)
    return np.minimum(np.maximum(qx, qy), 0.0) + c - r


@dataclass
class _Band:
    """The rim band of one shape size: where it is, and how each of its pixels bends light."""

    iy: np.ndarray        # int32 rows (shape-local)
    ix: np.ndarray        # int32 cols
    d: np.ndarray         # depth inside the rim (px)
    nx: np.ndarray        # outward normal
    ny: np.ndarray
    bend: np.ndarray      # displacement per unit of lens height (toward the centre)
    sheen: np.ndarray     # the bevel's Fresnel term
    cov: np.ndarray       # anti-aliased coverage

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in (self.iy, self.ix, self.d, self.nx, self.ny, self.bend, self.sheen, self.cov))


class _BandCache:
    """Displacement maps per shape size, reused across renders (LRU under a byte budget)."""

    def __init__(self, budget: int = 8 << 20) -> None:
        self._items: OrderedDict[tuple, _Band] = OrderedDict()
        self._bytes = 0
        self.budget = budget
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, w: int, h: int, r: float, n: float, bezel: float, curvature: float) -> _Band:
        key = (w, h, round(r, 1), round(n, 2), round(bezel, 1), round(curvature, 2))
        with self._lock:
            band = self._items.get(key)
            if band is not None:
                self._items.move_to_end(key)
                self.hits += 1
                return band
        band = _compute_band(w, h, r, n, bezel, curvature)
        with self._lock:
            self.misses += 1
            self._items[key] = band
            self._bytes += band.nbytes
            while self._bytes > self.budget and len(self._items) > 1:
                _, old = self._items.popitem(last=False)
                self._bytes -= old.nbytes
        return band

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0


BANDS = _BandCache()


def _compute_band(w: int, h: int, r: float, n: float, bezel: float, curvature: float) -> _Band:
    """The same maths as ``shaders/glass.frag``, for the pixels within ``bezel`` of the rim."""
    bx, by = w / 2.0, h / 2.0
    r = min(r, bx, by)
    bezel = max(bezel, 1.0)
    # only a frame of the shape can be in the band: build the grid for it, not the whole rect
    frame = int(math.ceil(bezel)) + 2
    ys = np.arange(h, dtype=np.float32)
    xs = np.arange(w, dtype=np.float32)
    rows = np.nonzero((ys < frame) | (ys >= h - frame))[0]
    cols = np.nonzero((xs < frame) | (xs >= w - frame))[0]
    # pixels in the top/bottom strips (all columns) plus the left/right strips (middle rows)
    mid = np.setdiff1d(np.arange(h), rows, assume_unique=True)
    iy = np.concatenate([np.repeat(rows, w), np.repeat(mid, len(cols))]).astype(np.int32)
    ix = np.concatenate([np.tile(np.arange(w), len(rows)), np.tile(cols, len(mid))]).astype(np.int32)
    lx = ix.astype(np.float32) + 0.5 - bx
    ly = iy.astype(np.float32) + 0.5 - by
    sd = _sd(lx, ly, bx, by, r, n)
    keep = sd < 0.5                       # inside, or on the anti-aliased edge
    d_all = np.maximum(-sd, 0.0)
    keep &= d_all < bezel
    iy, ix, lx, ly, sd = iy[keep], ix[keep], lx[keep], ly[keep], sd[keep]
    e = 0.75
    gx = _sd(lx + e, ly, bx, by, r, n) - _sd(lx - e, ly, bx, by, r, n)
    gy = _sd(lx, ly + e, bx, by, r, n) - _sd(lx, ly - e, bx, by, r, n)
    gl = np.hypot(gx, gy)
    gl[gl < 1e-6] = 1.0
    nx, ny = gx / gl, gy / gl
    d = np.maximum(-sd, 0.0)
    x = np.clip(d / bezel, 0.0, 1.0)
    u = 1.0 - x
    u3 = u * u * u
    slope = u3 * np.power(np.maximum(1.0 - u3 * u, 1e-5), -0.75)
    tilt = np.arctan(slope * curvature)
    bend = np.tan(tilt - np.arcsin(np.sin(tilt) / 1.5))
    sheen = np.power(np.sin(tilt), 3.0) * u
    cov = np.clip(0.5 - sd, 0.0, 1.0)
    f = np.float32
    return _Band(iy, ix, d.astype(f), nx.astype(f), ny.astype(f), bend.astype(f), sheen.astype(f), cov.astype(f))


# -- QImage <-> numpy without copies ------------------------------------------------------------------


def _view(img: QImage) -> np.ndarray:
    """A writable (h, w, 4) uint8 view of an ARGB32 image (bytes are B, G, R, A)."""
    buf = img.bits()
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
    return arr[:, : img.width()]


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    r = min(radius, rect.height() / 2, rect.width() / 2)
    path.addRoundedRect(rect, r, r)
    return path


def _tone_fill(tone: tuple[float, float]) -> QColor | None:
    """The one fill that squeezes colours into ``tone`` (col' = lo + col * (hi - lo))."""
    lo, hi = float(tone[0]), float(tone[1])
    alpha = 1.0 - (hi - lo)
    if alpha <= 1e-3:
        return None
    gray = min(1.0, max(0.0, lo / alpha))
    c = QColor.fromRgbF(gray, gray, gray, 1.0)
    c.setAlphaF(alpha)
    return c


# -- the Light renderer ---------------------------------------------------------------------------------


def _shape_geometry(s: Any) -> tuple[float, float, float, float]:
    """(radius, corner exponent, bezel, height) in device px, as ``refract.pack`` computes them."""
    o, k, rect = s.optics, s.scale, s.rect
    half = min(rect.width(), rect.height()) / 2.0
    radius = min(s.radius, half)
    capsule = radius >= half - 0.75 * k
    corner = 2.0 if capsule else o.corner
    if corner > 2.0:
        radius = min(half, radius * (1.0 + 0.28 * (corner - 2.0)))
    bezel = min(o.bezel * k, half)
    height = min(o.height * k, half * 1.15)
    return radius, corner, bezel, height


def render_light(base: QImage, depths: Sequence[Sequence[Any]], *, shadows: bool = True) -> QImage:
    """``base`` with each depth of glass shapes laid on it (CPU; see the module docstring)."""
    out = base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(1.0)          # shapes are in device px
    for shapes in depths:
        if not shapes:
            continue
        src = _view(out).copy()           # what this depth refracts: everything below it
        opaque = bool(src[..., 3].min() == 255)
        sources = [_source(s, src, opaque) for s in shapes]
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if shadows:
            glass = QPainterPath()
            for s in shapes:
                glass.addPath(_rounded(s.rect, s.radius))
            outside = QPainterPath()
            outside.addRect(QRectF(-1e5, -1e5, 2e5, 2e5))
            outside = outside.subtracted(glass)      # a shadow never darkens glass of the same depth
            for s in shapes:
                _paint_shadow(p, s, outside)
        for s, source in zip(shapes, sources):
            _paint_interior(p, s, source)
        p.end()
        for s, source in zip(shapes, sources):
            _refract_band(out, s, source)
    return out


@dataclass
class _Source:
    """What one shape refracts: the pixels below it (blurred when its optics frost), and where they sit."""

    arr: np.ndarray       # (h, w, 4) uint8, B G R A
    x0: int
    y0: int
    image: QImage | None = None   # the blurred crop, for painting the flat middle
    opaque: bool = True           # every pixel below is opaque (the usual case: the plain window background)


def _source(s: Any, src: np.ndarray, opaque: bool = True) -> _Source:
    from yuki.ui.window.glass import blur_image

    blur = s.optics.blur * s.scale
    if blur < 1.5:
        return _Source(src, 0, 0, None, opaque)
    H, W = src.shape[:2]
    _r, _c, _b, height = _shape_geometry(s)
    m = int(math.ceil(height + blur * 2)) + 2
    x0, y0 = max(0, int(s.rect.left()) - m), max(0, int(s.rect.top()) - m)
    x1, y1 = min(W, int(math.ceil(s.rect.right())) + m), min(H, int(math.ceil(s.rect.bottom())) + m)
    if x1 <= x0 or y1 <= y0:
        return _Source(src, 0, 0, None, opaque)
    crop = np.ascontiguousarray(src[y0:y1, x0:x1])
    img = QImage(crop.data, x1 - x0, y1 - y0, (x1 - x0) * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    img = blur_image(img, blur, passes=2).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    return _Source(_view(img).copy(), x0, y0, img, opaque)


def _paint_shadow(p: QPainter, s: Any, outside: QPainterPath) -> None:
    from PySide6.QtCore import QPointF

    from yuki.ui.window.glass import SHADOWS

    o = s.optics
    if not s.shadow or o.shadow <= 0:
        return
    blur = max(1, int(o.shadow_blur * s.scale * 0.5))
    color = QColor(0, 0, 0, int(255 * min(1.0, o.shadow)))
    img = SHADOWS.get(int(s.rect.width()), int(s.rect.height()), s.radius, blur, color, 1.0)
    pad = blur * 2
    p.save()
    if s.clip is not None:
        p.setClipRect(s.clip)
    p.setClipPath(outside, Qt.ClipOperation.IntersectClip)
    p.drawImage(QPointF(s.rect.x() - pad, s.rect.y() - pad + o.shadow_y * s.scale), img)
    p.restore()


def _paint_interior(p: QPainter, s: Any, source: _Source) -> None:
    """The flat middle, painted exactly: (blur,) tone range, tint. The rim band is overwritten after."""
    from yuki.ui.window.refract import _tint

    o = s.optics
    radius, _corner, bezel, _height = _shape_geometry(s)
    inner = s.rect.adjusted(bezel * 0.5, bezel * 0.5, -bezel * 0.5, -bezel * 0.5)
    if inner.width() <= 1 or inner.height() <= 1:
        return
    p.save()
    if s.clip is not None:
        p.setClipRect(s.clip)
    p.setClipPath(_rounded(inner, max(0.0, radius - bezel * 0.5)), Qt.ClipOperation.IntersectClip)
    if source.image is not None:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)   # the frost replaces what's there
        p.drawImage(source.x0, source.y0, source.image)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    fill = _tone_fill(o.tone)
    if fill is not None:
        p.fillRect(s.rect, fill)
    tr, tg, tb, ta = _tint(o.tint, s.tint)
    if ta > 0:
        p.fillRect(s.rect, QColor.fromRgbF(tr, tg, tb, ta))
    p.restore()


@dataclass
class _Sampler:
    """A band's refraction for one lens height and one light: where each pixel samples, and its light.

    Everything that depends only on the shape's size and optics is here, computed once and reused;
    a render only adds the shape's position and gathers pixels.
    """

    ix: np.ndarray        # int32 destination, shape-local
    iy: np.ndarray
    sx: np.ndarray        # int32 sample base (top-left of the 2x2), shape-local
    sy: np.ndarray
    fx: np.ndarray        # float32 bilinear weights
    fy: np.ndarray
    light: np.ndarray     # float32 added light (specular rim + sheen)
    keep: np.ndarray      # float32 1 - inner shadow
    cov: np.ndarray       # float32 coverage
    edge: np.ndarray      # int32 positions (into the arrays above) of the anti-aliased edge pixels


_SAMPLERS: OrderedDict[tuple, _Sampler] = OrderedDict()
_SAMPLER_LOCK = threading.Lock()
#: The samplers kept, by bytes (a window's panels and cards fit; the oldest go first).
_SAMPLER_BUDGET = 10 << 20
_SAMPLER_BYTES = [0]


def _sampler_bytes(s: "_Sampler") -> int:
    return sum(a.nbytes for a in (s.ix, s.iy, s.sx, s.sy, s.fx, s.fy, s.light, s.keep, s.cov, s.edge))


def _sampler(band: _Band, bkey: tuple, height: float, o: Any, scale: float, bezel: float) -> _Sampler:
    key = (bkey, round(height, 2), round(o.light_angle, 1), round(o.rim_width * scale, 3), round(o.rim_floor, 3),
           round(o.specular, 3), round(o.inner_shadow, 3), round(bezel, 1))
    with _SAMPLER_LOCK:
        hit = _SAMPLERS.get(key)
        if hit is not None:
            _SAMPLERS.move_to_end(key)
            return hit
    x = band.ix.astype(np.float32) - band.nx * band.bend * height
    y = band.iy.astype(np.float32) - band.ny * band.bend * height
    sx = np.floor(x).astype(np.int32)
    sy = np.floor(y).astype(np.int32)
    ang = math.radians(o.light_angle)
    lx, ly = math.cos(ang), -math.sin(ang)
    facing = band.nx * lx + band.ny * ly
    rim = np.exp(-band.d / max(o.rim_width * scale, 0.35))
    glint = np.square(np.maximum(facing, 0.0)) + 0.55 * np.square(np.maximum(-facing, 0.0))
    light = o.specular * (rim * (o.rim_floor + (1.0 - o.rim_floor) * glint) + 0.07 * band.sheen * glint)
    inner = o.inner_shadow * np.exp(-band.d / (max(bezel, 1.0) * 0.55)) * (0.5 - 0.5 * facing)
    # a pixel that bends less than a quarter pixel and catches no light is the flat middle: Qt paints it
    shift = band.bend * height
    m = (shift >= 0.25) | (light >= 0.002) | (inner >= 0.002) | (band.cov < 0.999)
    f = np.float32
    cov = band.cov[m]
    edge = np.nonzero(cov < 0.999)[0].astype(np.int32)
    smp = _Sampler(band.ix[m], band.iy[m], sx[m], sy[m], (x - sx)[m].astype(f), (y - sy)[m].astype(f),
                   light[m].astype(f), (1.0 - inner[m]).astype(f), cov, edge)
    with _SAMPLER_LOCK:
        _SAMPLERS[key] = smp
        _SAMPLER_BYTES[0] += _sampler_bytes(smp)
        while _SAMPLER_BYTES[0] > _SAMPLER_BUDGET and len(_SAMPLERS) > 1:
            _, old = _SAMPLERS.popitem(last=False)
            _SAMPLER_BYTES[0] -= _sampler_bytes(old)
    return smp


def _unpack(px: np.ndarray) -> np.ndarray:
    """(n,) uint32 ARGB -> (n, 3) float32 B, G, R in 0..255."""
    out = np.empty((px.shape[0], 3), np.float32)
    out[:, 0] = px & 0xFF
    out[:, 1] = (px >> 8) & 0xFF
    out[:, 2] = (px >> 16) & 0xFF
    return out


def _refract_band(img: QImage, s: Any, source: _Source) -> None:
    from yuki.ui.window.refract import _tint

    W, H = img.width(), img.height()
    stride = img.bytesPerLine() // 4
    o = s.optics
    radius, corner, bezel, height = _shape_geometry(s)
    x0, y0 = int(round(s.rect.left())), int(round(s.rect.top()))
    w, h = int(round(s.rect.width())), int(round(s.rect.height()))
    if w < 2 or h < 2:
        return
    band = BANDS.get(w, h, radius, corner, bezel, o.curvature)
    bkey = (w, h, round(radius, 1), round(corner, 2), round(bezel, 1), round(o.curvature, 2))
    smp = _sampler(band, bkey, height, o, s.scale, bezel)
    gx = smp.ix + x0
    gy = smp.iy + y0
    sx, sy, fx, fy, light, keep, cov, edge = smp.sx, smp.sy, smp.fx, smp.fy, smp.light, smp.keep, smp.cov, smp.edge
    x_lo, y_lo, x_hi, y_hi = 0, 0, W, H
    if s.clip is not None:
        c = s.clip
        x_lo, y_lo = max(0, math.ceil(c.left())), max(0, math.ceil(c.top()))
        x_hi, y_hi = min(W, math.ceil(c.right())), min(H, math.ceil(c.bottom()))
    if x0 < x_lo or y0 < y_lo or x0 + w > x_hi or y0 + h > y_hi:
        ok = (gx >= x_lo) & (gy >= y_lo) & (gx < x_hi) & (gy < y_hi)
        if not ok.any():
            return
        sel = np.nonzero(ok)[0]
        gx, gy, sx, sy, fx, fy = gx[sel], gy[sel], sx[sel], sy[sel], fx[sel], fy[sel]
        light, keep, cov = light[sel], keep[sel], cov[sel]
        edge = np.nonzero(cov < 0.999)[0]
    src = source.arr
    sh, sw = src.shape[:2]
    flat = np.ascontiguousarray(src).view(np.uint32).reshape(-1)
    ax = np.clip(sx + (x0 - source.x0), 0, sw - 2)
    ay = np.clip(sy + (y0 - source.y0), 0, sh - 2)
    i00 = ay * sw + ax
    fx2 = fx[:, None]
    a = _unpack(flat[i00])
    b = _unpack(flat[i00 + 1])
    top = a + (b - a) * fx2
    a = _unpack(flat[i00 + sw])
    b = _unpack(flat[i00 + sw + 1])
    col = top + ((a + (b - a) * fx2) - top) * fy[:, None]          # 0..255, B G R
    lo, hi = o.tone
    tr, tg, tb, ta = _tint(o.tint, s.tint)
    # tone range, tint and lift folded into one multiply-add
    col *= (hi - lo) / 255.0 * (1.0 - ta)
    col += np.array([lo * (1.0 - ta) + tb * ta, lo * (1.0 - ta) + tg * ta, lo * (1.0 - ta) + tr * ta],
                    np.float32) + o.lift
    col += light[:, None]
    col *= keep[:, None]
    np.clip(col, 0.0, 1.0, out=col)
    col *= 255.0
    col += 0.5
    out32 = np.frombuffer(img.bits(), np.uint32)
    di = gy * stride + gx
    if source.opaque:
        if len(edge):
            c2 = cov[edge][:, None]
            col[edge] = col[edge] * c2 + _unpack(out32[di[edge]]) * (1.0 - c2)
        c = col.astype(np.uint32)
        out32[di] = np.uint32(0xFF000000) | (c[:, 2] << 16) | (c[:, 1] << 8) | c[:, 0]
        return
    # a source with transparent parts (the See-through window): the same light, premultiplied, so the rim
    # stays as clear as what it bends instead of turning into an opaque ring
    a = flat[i00] >> 24
    sa = (a.astype(np.float32) / 255.0)
    alpha = (hi - lo) * (1.0 - ta) * sa + (1.0 - (hi - lo) * (1.0 - ta))
    alpha = np.minimum(1.0, alpha + light)
    np.clip(col, 0.0, alpha[:, None] * 255.0, out=col)
    alpha *= 255.0
    if len(edge):
        c2 = cov[edge]
        prev = out32[di[edge]]
        col[edge] = col[edge] * c2[:, None] + _unpack(prev) * (1.0 - c2[:, None])
        alpha[edge] = alpha[edge] * c2 + (prev >> 24).astype(np.float32) * (1.0 - c2)
    c = col.astype(np.uint32)
    out32[di] = (np.clip(alpha + 0.5, 0, 255).astype(np.uint32) << 24) | (c[:, 2] << 16) | (c[:, 1] << 8) | c[:, 0]


# -- one entry point for both modes ----------------------------------------------------------------------


def split_layers(layers: Sequence[tuple[str, Any]]) -> list[list[Any]]:
    return [list(payload) for kind, payload in layers if kind == "glass" and payload]


def render(base: QImage, layers: Sequence[tuple[str, Any]], mode: str | None = None) -> QImage:
    """``base`` with ``layers`` (``refract.compose_layers`` form) composited in ``mode`` (default: the policy's).

    Light draws each glass layer on the CPU and composites the ``image`` layers with Qt;
    Full is the GPU compositor. Off returns ``base`` with the image layers only.
    """
    mode = mode or _POLICY.mode
    if mode == "full":
        from yuki.ui.window.refract import shared

        comp = shared()
        try:
            return comp.render(base, layers)
        finally:
            comp.trim()   # the device stays; its page-sized textures go until the next render
    dpr = base.devicePixelRatio() or 1.0
    out = base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(1.0)
    for kind, payload in layers:
        if kind == "glass" and payload and mode == "light":
            out = render_light(out, [payload])
        elif kind == "image" and payload is not None:
            p = QPainter(out)
            img = QImage(payload)
            img.setDevicePixelRatio(1.0)
            p.drawImage(0, 0, img)
            p.end()
    out.setDevicePixelRatio(dpr)
    return out


def render_flat(base: QImage, layers: Sequence[tuple[str, Any]]) -> QImage:
    """``base`` with every glass shape of ``layers`` painted flat (fill, tint, catch-light; no bending), in
    depth order: the Light / Off page picture, made once per change on the glass thread. Shadows are left to
    :class:`~yuki.ui.window.glasscache.GlassPage` (it paints them outside the glass)."""
    from yuki.ui.window.glass import paint_glass

    out = base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(1.0)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    for kind, shapes in layers:
        if kind != "glass":
            continue
        for s in shapes:
            if s.level is None:
                continue
            p.save()
            if s.clip is not None:
                p.setClipRect(s.clip)
            paint_glass(p, s.rect, s.radius, s.level, tint=s.tint, shadow=False, painted=True)
            p.restore()
    p.end()
    return out


def float_lens(content: QImage, rect: QRectF, radius: float, optics: Any, *, tint: QColor | None = None,
               mode: str | None = None) -> QImage | None:
    """One floating lens over the app's own content, cropped to the lens (device px).

    ``content``: what lies under the glass (a grab of the app's widgets with a margin round the lens, so the
    rim has something to bend inwards), device px with its ``devicePixelRatio`` set. ``rect`` and ``radius``:
    the lens in ``content``'s device px. Light draws it on the CPU, Full on the GPU; called on the glass
    thread, once per place and size (the caller caches the result).
    """
    from yuki.ui.window.refract import Shape

    mode = mode or _POLICY.mode
    if mode == "off" or content.isNull() or rect.width() < 4 or rect.height() < 4:
        return None
    dpr = content.devicePixelRatio() or 1.0
    base = content.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    base.setDevicePixelRatio(1.0)
    shape = Shape(QRectF(rect), radius, scaled_optics(optics), dpr, tint, None, False)
    out = render(base, [("glass", [shape])], mode)
    out.setDevicePixelRatio(1.0)
    crop = out.copy(rect.toAlignedRect())
    crop.setDevicePixelRatio(dpr)
    return crop


# -- the desktop wallpaper behind a surface (decoded once per screen size) ---------------------------


class _Wallpaper:
    def __init__(self) -> None:
        self._img: QImage | None = None
        self._key: tuple | None = None
        self._lock = threading.Lock()

    def screen_image(self, size: QSize, dpr: float) -> QImage | None:
        """The wallpaper filling a screen of ``size`` logical px at ``dpr`` (None if it can't be read)."""
        key = (size.width(), size.height(), round(dpr, 3))
        with self._lock:
            if self._key == key and self._img is not None:
                return self._img
        from yuki.ui.window.refract import load_wallpaper

        img = load_wallpaper(size, dpr)
        with self._lock:
            self._img, self._key = img, key
        return img

    def crop(self, screen_rect: Any, rect: QRectF, dpr: float) -> QImage | None:
        """The piece of the wallpaper under ``rect`` (global logical px) on the screen ``screen_rect``."""
        img = self.screen_image(QSize(screen_rect.width(), screen_rect.height()), dpr)
        if img is None:
            return None
        x = (rect.x() - screen_rect.x()) * dpr
        y = (rect.y() - screen_rect.y()) * dpr
        w, h = max(1, int(round(rect.width() * dpr))), max(1, int(round(rect.height() * dpr)))
        out = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(QColor(0, 0, 0))
        p = QPainter(out)
        p.drawImage(QRectF(0, 0, w, h), img, QRectF(x, y, w, h))
        p.end()
        out.setDevicePixelRatio(dpr)
        return out

    def forget(self) -> None:
        with self._lock:
            self._img, self._key = None, None


WALLPAPER = _Wallpaper()


def release() -> None:
    """Drop every cache (a setting changed to Off, or the window is closing)."""
    BANDS.clear()
    with _SAMPLER_LOCK:
        _SAMPLERS.clear()
        _SAMPLER_BYTES[0] = 0
    WALLPAPER.forget()
    try:
        from yuki.ui.window.refract import release_shared

        release_shared()
    except Exception:
        pass
