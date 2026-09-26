"""Real Liquid Glass: GPU refraction through rounded-rect and squircle lenses (Qt RHI, Direct3D 11).

How the picture is made
-----------------------
Every glass shape is a slab of glass lying on what is behind it. Its rim is a
bevel with a squircle height profile, ``h(x) = (1 - (1-x)^4)^(1/4)``, where
``x`` runs from 0 at the rim to 1 where the top goes flat (``bezel`` px in; a
bezel of at least half the short side makes a droplet). One fragment shader
(``shaders/glass.frag``) does the physics per pixel:

* The shape's signed distance field gives the distance to the rim and the
  outward normal (analytic, no displacement-map image).
* The profile's slope is the surface tilt; Snell's law (air to glass, n = 1.5)
  turns the tilt into how far the ray through the pixel is bent toward the
  centre, times the glass ``height``. The background is sampled from there:
  the rim magnifies and bends what is behind it, the flat middle stays clear.
  Droplets can also ``magnify`` as a whole.
* Red, green and blue bend by ``1 -/+ chroma`` of that (dispersion): colour
  fringes where the bend is strong.
* A little ``blur`` (mip-mapped taps), a ``saturation`` boost, a ``tone`` range
  (Apple adapts the glass for legibility: light glass never gets darker than
  ``tone[0]``, dark glass never brighter than ``tone[1]``), then the ``tint``.
* Light: a thin specular rim, brightest on the edge facing ``light_angle`` and
  fainter on the opposite edge, a Fresnel sheen on the steep part of the bevel,
  an inner shadow on the far side for depth, and a soft drop shadow outside,
  all from the same distance field.

Layers
------
Glass refracts whatever is already composited under it. :class:`Compositor`
builds the picture bottom-up: the base (the desktop wallpaper), then each depth
of glass (the window's floor sheet, panels on it, pills on those), then the
widgets' content (text, icons, charts) painted by Qt with the glass left out,
then *floating* glass (popovers such as the timeline's hover card) that
refracts the content under it, then the floating glass's own content.

:data:`COLLECTOR` is how the shapes are found: while it is active,
:func:`yuki.ui.window.glass.paint_glass` records each shape (its device rect,
clip, level and depth) instead of painting it, so every widget that already
paints glass becomes a lens without knowing about the GPU.

Costs
-----
GPU only: nothing per pixel runs in Python. The wallpaper is read once from the
registry path (no screen capture, so no capture border ever appears). The live
surface (:class:`GlassView`, a QRhiWidget) renders only when ``update()`` is
called: idle, it costs nothing, and closing the window destroys it and every
GPU resource. Offscreen (mockups, tests) the same passes run in a standalone
Direct3D 11 QRhi and are read back to a QImage (ARCHITECTURE.md rule 7: nothing
reaches the desktop).
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QRhi,
    QRhiBuffer,
    QRhiColorAttachment,
    QRhiD3D11InitParams,
    QRhiD3D12InitParams,
    QRhiDepthStencilClearValue,
    QRhiGraphicsPipeline,
    QRhiReadbackDescription,
    QRhiReadbackResult,
    QRhiSampler,
    QRhiShaderResourceBinding,
    QRhiShaderStage,
    QRhiTexture,
    QRhiTextureRenderTargetDescription,
    QRhiVertexInputAttribute,
    QRhiVertexInputBinding,
    QRhiVertexInputLayout,
    QRhiViewport,
    QShader,
)
from PySide6.QtWidgets import QRhiWidget

from yuki.ui.window.theme import GlassLevel, Optics, Theme

SHADER_DIR = Path(__file__).with_name("shaders")
_FLOATS = 32                      # floats per shape instance (8 vec4 attributes)
_STRIDE = _FLOATS * 4


# -- shaders ------------------------------------------------------------------------------------


def _qsb() -> Path:
    import PySide6

    return Path(PySide6.__file__).with_name("qsb.exe")


def load_shader(name: str) -> QShader:
    """``shaders/<name>`` as a QShader, compiled by qsb when the .qsb is missing or older than the source."""
    src = SHADER_DIR / name
    out = SHADER_DIR / f"{name}.qsb"
    if src.exists() and (not out.exists() or out.stat().st_mtime < src.stat().st_mtime):
        subprocess.run([str(_qsb()), "--glsl", "100es,120,150", "--hlsl", "50", "--msl", "12", "-o", str(out),
                        str(src)], check=True, capture_output=True)
    shader = QShader.fromSerialized(QByteArray(out.read_bytes()))
    if not shader.isValid():
        raise RuntimeError(f"invalid shader {out}")
    return shader


# -- shapes -------------------------------------------------------------------------------------


@dataclass
class Shape:
    """One lens on the canvas. ``rect``, ``radius`` and ``clip`` are device px; ``optics`` are logical px
    and are multiplied by ``scale`` (device px per logical px)."""

    rect: QRectF
    radius: float
    optics: Optics
    scale: float = 1.0
    tint: QColor | None = None     # an extra wash (a kind colour) mixed into the optics' tint
    clip: QRectF | None = None
    shadow: bool = True
    depth: int = 0
    floating: bool = False
    level: GlassLevel | None = None   # the painted look, for the flat (Light / Off) cached picture

    def moved(self, dx: float, dy: float) -> "Shape":
        from dataclasses import replace

        return replace(self, rect=self.rect.translated(dx, dy),
                       clip=self.clip.translated(dx, dy) if self.clip is not None else None)


def _tint(base: QColor, extra: QColor | None) -> tuple[float, float, float, float]:
    r, g, b, a = base.redF(), base.greenF(), base.blueF(), base.alphaF()
    if extra is None or extra.alpha() == 0:
        return r, g, b, a
    ea = min(1.0, extra.alphaF() * 2.2)            # a 14% kind wash over glass reads like ~30% of the tint
    return (r + (extra.redF() - r) * ea, g + (extra.greenF() - g) * ea, b + (extra.blueF() - b) * ea,
            a + (1.0 - a) * extra.alphaF())


def pack(shapes: Sequence[Shape]) -> bytes:
    """Per-instance attributes for ``shaders/glass.vert`` (8 x vec4 per shape)."""
    a = np.zeros((len(shapes), _FLOATS), np.float32)
    for i, s in enumerate(shapes):
        o, k, r = s.optics, s.scale, s.rect
        half = min(r.width(), r.height()) / 2.0
        radius = min(s.radius, half)
        capsule = radius >= half - 0.75 * k
        corner = 2.0 if capsule else o.corner
        if corner > 2.0:   # a squircle corner looks smaller than a circle of the same radius
            radius = min(half, radius * (1.0 + 0.28 * (corner - 2.0)))
        bezel = min(o.bezel * k, half)
        height = min(o.height * k, half * 1.15)     # small droplets bend hard but stay lenses
        tr, tg, tb, ta = _tint(o.tint, s.tint)
        c = s.clip if s.clip is not None else QRectF(-1e6, -1e6, 2e6, 2e6)
        a[i] = (r.x(), r.y(), r.width(), r.height(),
                radius, bezel, height, o.blur * k,
                o.chroma, o.specular, math.radians(o.light_angle), o.shadow if s.shadow else 0.0,
                tr, tg, tb, ta,
                o.tone[0], o.tone[1], o.saturation, o.magnify,
                o.shadow_blur * k, o.shadow_y * k, o.inner_shadow, o.rim_width * k,
                c.left(), c.top(), c.right(), c.bottom(),
                corner, o.curvature, o.rim_floor, o.lift)
    return a.tobytes()


# A layer is ("glass", [Shape, ...]) or ("image", QImage): premultiplied, canvas-sized.
Layer = tuple[str, object]


_BEGIN_OFFSCREEN = None


def _begin_offscreen(rhi: QRhi):
    """``QRhi::beginOffscreenFrame(QRhiCommandBuffer **)``, called directly.

    PySide6 6.11's binding drops the out-parameter (it returns only the result), so the command buffer
    would be unreachable. The exported C++ member is called through ctypes and its pointer wrapped
    by shiboken; the command buffer stays owned by the QRhi, as in C++.
    """
    global _BEGIN_OFFSCREEN
    import ctypes

    import PySide6
    import shiboken6
    from PySide6.QtGui import QRhiCommandBuffer

    if _BEGIN_OFFSCREEN is None:
        dll = ctypes.WinDLL(str(Path(PySide6.__file__).with_name("Qt6Gui.dll")))
        fn = getattr(dll, "?beginOffscreenFrame@QRhi@@QEAA?AW4FrameOpResult@1@PEAPEAVQRhiCommandBuffer@@"
                          "V?$QFlags@W4BeginFrameFlag@QRhi@@@@@Z")
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        _BEGIN_OFFSCREEN = fn
    ptr = ctypes.c_void_p()
    result = _BEGIN_OFFSCREEN(shiboken6.getCppPointer(rhi)[0], ctypes.byref(ptr), 0)
    if result != 0 or not ptr.value:
        raise RuntimeError(f"beginOffscreenFrame failed ({result})")
    return shiboken6.wrapInstance(ptr.value, QRhiCommandBuffer)


def create_rhi(timestamps: bool = False) -> QRhi | None:
    """A standalone QRhi: Direct3D 11 (Windows' default for Qt), else Direct3D 12."""
    flags = QRhi.Flag.EnableTimestamps if timestamps else QRhi.Flag(0)
    for impl, params in ((QRhi.Implementation.D3D11, QRhiD3D11InitParams()),
                         (QRhi.Implementation.D3D12, QRhiD3D12InitParams())):
        rhi = QRhi.create(impl, params, flags)
        if rhi is not None:
            return rhi
    return None


# -- the compositor -----------------------------------------------------------------------------


class Compositor:
    """Records the layer passes on a QRhi: into its own textures (read back) or into a widget's target."""

    def __init__(self, rhi: QRhi | None = None, *, timestamps: bool = True) -> None:
        self._own = rhi is None
        self.rhi = rhi if rhi is not None else create_rhi(timestamps)
        if self.rhi is None:
            raise RuntimeError("no Direct3D device for the glass")
        rhi = self.rhi
        self.bgra = rhi.isTextureFormatSupported(QRhiTexture.Format.BGRA8)
        self.fmt = QRhiTexture.Format.BGRA8 if self.bgra else QRhiTexture.Format.RGBA8
        self.qfmt = QImage.Format.Format_ARGB32_Premultiplied if self.bgra else QImage.Format.Format_RGBA8888_Premultiplied
        self._sh = {n: load_shader(n) for n in ("glass.vert", "glass.frag", "blit.vert", "blit.frag")}
        self._res: list = []                  # long-lived resources, destroyed in release()
        self._sampler = self._keep(rhi.newSampler(QRhiSampler.Filter.Linear, QRhiSampler.Filter.Linear,
                                                  QRhiSampler.Filter.Linear, QRhiSampler.AddressMode.ClampToEdge,
                                                  QRhiSampler.AddressMode.ClampToEdge))
        self._sampler.create()
        self._ubuf_rt = self._ubuf()          # sampling a render target
        self._ubuf_img = self._ubuf()         # sampling an uploaded image
        self._dummy = self._keep(rhi.newTexture(self.fmt, QSize(1, 1), 1, QRhiTexture.Flag(0)))
        self._dummy.create()
        self._layout = self._srb(self._dummy, self._ubuf_img)
        self._inst: QRhiBuffer | None = None
        self._inst_cap = 0
        self._size: QSize | None = None
        self._tex: list = []
        self._rt: list = []
        self._rp = None
        self._pipes: dict = {}
        self._srbs: dict = {}
        self._images: dict[int, tuple] = {}   # QImage.cacheKey() -> (texture, srb)
        self.last_gpu_ms = 0.0

    # resources

    def _keep(self, res):
        self._res.append(res)
        return res

    def _ubuf(self) -> QRhiBuffer:
        b = self._keep(self.rhi.newBuffer(QRhiBuffer.Type.Dynamic, QRhiBuffer.UsageFlag.UniformBuffer, 32))
        b.create()
        return b

    def _srb(self, tex, ubuf):
        srb = self.rhi.newShaderResourceBindings()
        stages = QRhiShaderResourceBinding.StageFlag.VertexStage | QRhiShaderResourceBinding.StageFlag.FragmentStage
        srb.setBindings([
            QRhiShaderResourceBinding.uniformBuffer(0, stages, ubuf),
            QRhiShaderResourceBinding.sampledTexture(1, QRhiShaderResourceBinding.StageFlag.FragmentStage, tex,
                                                     self._sampler),
        ])
        srb.create()
        return self._keep(srb)

    def _pipeline(self, kind: str, rp):
        key = (kind, id(rp))
        ps = self._pipes.get(key)
        if ps is not None:
            return ps
        ps = self.rhi.newGraphicsPipeline()
        blend = QRhiGraphicsPipeline.TargetBlend()
        blend.enable = True
        blend.srcColor = QRhiGraphicsPipeline.BlendFactor.One
        blend.dstColor = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha
        blend.srcAlpha = QRhiGraphicsPipeline.BlendFactor.One
        blend.dstAlpha = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha
        ps.setTargetBlends([blend])
        layout = QRhiVertexInputLayout()
        if kind == "glass":
            ps.setTopology(QRhiGraphicsPipeline.Topology.TriangleStrip)
            ps.setShaderStages([QRhiShaderStage(QRhiShaderStage.Type.Vertex, self._sh["glass.vert"]),
                                QRhiShaderStage(QRhiShaderStage.Type.Fragment, self._sh["glass.frag"])])
            layout.setBindings([QRhiVertexInputBinding(_STRIDE, QRhiVertexInputBinding.Classification.PerInstance)])
            layout.setAttributes([QRhiVertexInputAttribute(0, i, QRhiVertexInputAttribute.Format.Float4, i * 16)
                                  for i in range(8)])
        else:
            ps.setTopology(QRhiGraphicsPipeline.Topology.Triangles)
            ps.setShaderStages([QRhiShaderStage(QRhiShaderStage.Type.Vertex, self._sh["blit.vert"]),
                                QRhiShaderStage(QRhiShaderStage.Type.Fragment, self._sh["blit.frag"])])
        ps.setVertexInputLayout(layout)
        ps.setShaderResourceBindings(self._layout)
        ps.setRenderPassDescriptor(rp)
        if not ps.create():
            raise RuntimeError(f"pipeline {kind} failed")
        self._pipes[key] = self._keep(ps)
        return ps

    def _ensure_size(self, size: QSize) -> None:
        if self._size == size:
            return
        for r in self._tex + self._rt:
            r.destroy()
        self._tex, self._rt = [], []
        flags = (QRhiTexture.Flag.RenderTarget | QRhiTexture.Flag.MipMapped | QRhiTexture.Flag.UsedWithGenerateMips
                 | QRhiTexture.Flag.UsedAsTransferSource)
        for _ in range(2):
            tex = self.rhi.newTexture(self.fmt, size, 1, flags)
            tex.create()
            rt = self.rhi.newTextureRenderTarget(QRhiTextureRenderTargetDescription(QRhiColorAttachment(tex)))
            if self._rp is None:
                self._rp = self._keep(rt.newCompatibleRenderPassDescriptor())
            rt.setRenderPassDescriptor(self._rp)
            rt.create()
            self._tex.append(tex)
            self._rt.append(rt)
        self._srbs = {i: self._srb(t, self._ubuf_rt) for i, t in enumerate(self._tex)}
        self._size = QSize(size)

    def _image(self, img: QImage, rub) -> tuple:
        key = img.cacheKey()
        hit = self._images.get(key)
        if hit is not None:
            return hit
        img = img if img.format() == self.qfmt else img.convertToFormat(self.qfmt)
        tex = self.rhi.newTexture(self.fmt, img.size(), 1, QRhiTexture.Flag(0))
        tex.create()
        rub.uploadTexture(tex, img)
        hit = (tex, self._srb(tex, self._ubuf_img), img)
        self._images[key] = hit
        return hit

    def _drop_images(self, keep: set[int]) -> None:
        for key in [k for k in self._images if k not in keep]:
            tex, srb, _img = self._images.pop(key)
            srb.destroy()
            tex.destroy()
            if srb in self._res:
                self._res.remove(srb)

    # recording

    def record(self, cb, base: QImage, layers: Sequence[Layer], target=None) -> None:
        """Record every pass into ``cb``: the base, then each layer; the result lands in ``target`` (a render
        target, e.g. a QRhiWidget's) or stays in the compositor's texture for :meth:`render` to read back."""
        rhi = self.rhi
        size = base.size()
        self._ensure_size(size)
        w, h = float(size.width()), float(size.height())
        ndc = -1.0 if rhi.isYUpInNDC() else 1.0
        flip = 1.0 if rhi.isYUpInFramebuffer() else 0.0
        rub = rhi.nextResourceUpdateBatch()
        rub.updateDynamicBuffer(self._ubuf_rt, 0, 32, struct.pack("8f", w, h, ndc, flip, 0, 0, 0, 0))
        rub.updateDynamicBuffer(self._ubuf_img, 0, 32, struct.pack("8f", w, h, ndc, 0.0, 0, 0, 0, 0))
        # every glass layer's instances in one buffer, one update per frame
        blobs, offsets, off = [], [], 0
        for kind, payload in layers:
            if kind == "glass" and payload:
                blob = pack(payload)
                blobs.append(blob)
                offsets.append(off)
                off += len(blob)
            else:
                offsets.append(-1)
        if off:
            if off > self._inst_cap:
                if self._inst is not None:
                    self._res.remove(self._inst)
                    self._inst.destroy()
                self._inst_cap = max(off, 64 * _STRIDE)
                self._inst = self._keep(rhi.newBuffer(QRhiBuffer.Type.Dynamic, QRhiBuffer.UsageFlag.VertexBuffer,
                                                      self._inst_cap))
                self._inst.create()
            rub.updateDynamicBuffer(self._inst, 0, off, b"".join(blobs))
        used: set[int] = {base.cacheKey()}
        base_tex = self._image(base, rub)
        images = {}
        for kind, payload in layers:
            if kind == "image":
                images[id(payload)] = self._image(payload, rub)
                used.add(payload.cacheKey())
        blit = self._pipeline("blit", self._rp)
        glass = self._pipeline("glass", self._rp)
        vp = QRhiViewport(0, 0, w, h)
        clear = QColor(0, 0, 0, 0)
        ds = QRhiDepthStencilClearValue(1.0, 0)

        cur = 0
        cb.beginPass(self._rt[cur], clear, ds, rub)
        cb.setGraphicsPipeline(blit)
        cb.setViewport(vp)
        cb.setShaderResources(base_tex[1])
        cb.draw(3)
        cb.endPass()
        for (kind, payload), inst_off in zip(layers, offsets):
            if kind == "glass" and not payload:
                continue
            dst = 1 - cur
            rub = rhi.nextResourceUpdateBatch()
            if kind == "glass":
                rub.generateMips(self._tex[cur])
            cb.beginPass(self._rt[dst], clear, ds, rub)
            cb.setGraphicsPipeline(blit)
            cb.setViewport(vp)
            cb.setShaderResources(self._srbs[cur])
            cb.draw(3)
            if kind == "glass":
                cb.setGraphicsPipeline(glass)
                cb.setViewport(vp)
                cb.setShaderResources(self._srbs[cur])
                cb.setVertexInput(0, [(self._inst, inst_off)])
                cb.draw(4, len(payload))
            else:
                cb.setGraphicsPipeline(blit)
                cb.setShaderResources(images[id(payload)][1])
                cb.draw(3)
            cb.endPass()
            cur = dst
        self._cur = cur
        if target is not None:
            tps = self._pipeline("blit", target.renderPassDescriptor())
            px = target.pixelSize()
            cb.beginPass(target, clear, ds)
            cb.setGraphicsPipeline(tps)
            cb.setViewport(QRhiViewport(0, 0, px.width(), px.height()))
            cb.setShaderResources(self._srbs[cur])
            cb.draw(3)
            cb.endPass()
        self._drop_images(used)

    def render(self, base: QImage, layers: Sequence[Layer]) -> QImage:
        """Offscreen: the composited picture as a QImage (premultiplied ARGB32), base-sized."""
        rhi = self.rhi
        cb = _begin_offscreen(rhi)
        self.record(cb, base, layers)
        rb = QRhiReadbackResult()
        rub = rhi.nextResourceUpdateBatch()
        rub.readBackTexture(QRhiReadbackDescription(self._tex[self._cur]), rb)
        cb.resourceUpdate(rub)
        rhi.endOffscreenFrame()
        self.last_gpu_ms = cb.lastCompletedGpuTime() * 1000.0
        size = base.size()
        data = bytes(rb.data)
        img = QImage(data, size.width(), size.height(), size.width() * 4, self.qfmt).copy()
        if rhi.isYUpInFramebuffer():
            img = img.mirrored(False, True)
        if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
            img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(base.devicePixelRatio())
        return img

    def draw_only(self, base: QImage, layers: Sequence[Layer]) -> float:
        """One offscreen frame without the readback (what a live frame costs the GPU); its GPU ms, if timed."""
        cb = _begin_offscreen(self.rhi)
        self.record(cb, base, layers)
        self.rhi.endOffscreenFrame()
        self.last_gpu_ms = cb.lastCompletedGpuTime() * 1000.0
        return self.last_gpu_ms

    def trim(self) -> None:
        """Free the canvas-sized textures and uploaded images (kept: the device, shaders and pipelines)."""
        self._drop_images(set())
        for r in self._tex + self._rt:
            r.destroy()
        self._tex, self._rt, self._srbs = [], [], {}
        self._size = None

    def release(self) -> None:
        """Destroy every GPU resource (and the QRhi when it is ours)."""
        self._drop_images(set())
        for r in self._tex + self._rt:
            r.destroy()
        for r in reversed(self._res):
            try:
                r.destroy()
            except RuntimeError:
                pass
        self._tex, self._rt, self._res, self._pipes, self._srbs = [], [], [], {}, {}
        self._inst = None
        if self._own and self.rhi is not None:
            self.rhi = None      # the QRhi (owned by Python) goes with its last reference
        self._size = None


# -- the live surface ---------------------------------------------------------------------------


class GlassView(QRhiWidget):
    """The window's bottom layer on a real desktop: the wallpaper and every glass shape, refracted on the GPU.

    It renders only when :meth:`set_scene` or ``update()`` asks (a resize, a page switch, a hover animation
    frame); otherwise it is idle. Its GPU resources go when the widget does.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setApi(QRhiWidget.Api.Direct3D11)
        self._comp: Compositor | None = None
        self._base: QImage | None = None
        self._layers: list[Layer] = []
        self.frames = 0

    def set_scene(self, base: QImage, layers: Sequence[Layer]) -> None:
        self._base, self._layers = base, list(layers)
        self.update()

    def initialize(self, cb) -> None:  # noqa: D102
        if self._comp is None:
            self._comp = Compositor(self.rhi())

    def render(self, cb) -> None:  # noqa: D102
        if self._comp is not None and self._base is not None:
            self._comp.record(cb, self._base, self._layers, target=self.renderTarget())
            self.frames += 1

    def releaseResources(self) -> None:  # noqa: D102
        if self._comp is not None:
            self._comp.release()
            self._comp = None


# -- the desktop wallpaper (one read, no capture) -----------------------------------------------


def wallpaper_path() -> Path | None:
    """The desktop picture: ``HKCU\\Control Panel\\Desktop\\WallPaper``, else Windows' transcoded copy."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            value = str(winreg.QueryValueEx(key, "WallPaper")[0] or "")
        if value and Path(value).is_file():
            return Path(value)
    except OSError:
        pass
    cached = Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Themes\TranscodedWallpaper"))
    return cached if cached.is_file() else None


def load_wallpaper(size: QSize, dpr: float = 1.0, path: Path | None = None) -> QImage | None:
    """The wallpaper decoded once and cropped to fill ``size`` logical px (Windows' "Fill"), or None."""
    path = path or wallpaper_path()
    if path is None:
        return None
    from PySide6.QtGui import QImageReader

    w, h = int(size.width() * dpr), int(size.height() * dpr)
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    src = reader.size()
    if src.isValid() and src.width() > 0 and src.height() > 0:
        k = max(w / src.width(), h / src.height())
        if k < 1.0:   # decode straight to the size needed: no full-resolution copy in memory
            reader.setScaledSize(QSize(max(w, round(src.width() * k)), max(h, round(src.height() * k))))
    img = reader.read()
    if img.isNull():
        return None
    scaled = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    x, y = (scaled.width() - w) // 2, (scaled.height() - h) // 2
    out = scaled.copy(x, y, w, h).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(dpr)
    return out


# -- finding the shapes: paint_glass records instead of painting ---------------------------------


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    r = min(radius, rect.height() / 2, rect.width() / 2)
    path.addRoundedRect(rect, r, r)
    return path


class _Collector:
    """While active, :func:`yuki.ui.window.glass.paint_glass` hands its shapes here.

    ``mode`` "below": record every shape; the content painted after a *floating* shape, inside it, is left
    out (it goes on top of that shape's glass). ``mode`` "above": record nothing; a floating shape clears
    what is under it, so the image holds only what sits on floating glass.
    """

    def __init__(self) -> None:
        self.active = False
        self.theme: Theme | None = None
        self.mode = "below"
        self.shapes: list[Shape] = []

    def begin(self, theme: Theme, mode: str = "below") -> None:
        self.active, self.theme, self.mode = True, theme, mode
        self.shapes = []

    def end(self) -> list[Shape]:
        self.active = False
        return self.shapes

    def add(self, p: QPainter, rect: QRectF, radius: float, level: GlassLevel, tint: QColor | None, shadow: bool,
            floating: bool) -> bool:
        if not self.active or self.theme is None:
            return False
        if self.mode == "above":
            if floating:
                p.save()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                p.fillPath(_rounded(rect, radius), QColor(0, 0, 0))
                p.restore()
            return True
        tr = p.deviceTransform()
        dev = tr.mapRect(rect)
        scale = math.hypot(tr.m11(), tr.m12()) or 1.0
        clip = self._clip(p, tr)
        if clip is not None:
            if not clip.intersects(dev):
                return True
        optics = self.theme.optics.get(level.name) or self.theme.optics["regular"]
        if floating:   # over busy content: frosted more, so what sits on it stays readable
            optics = self.theme.optics.get("popover", optics)
        from yuki.ui.lens import scaled_optics

        optics = scaled_optics(optics)   # the user's "Glass bending" and "Blur"
        if clip is not None and shadow and optics.shadow > 0:
            # a clip that hugs the shape is the widget's own bounds: let the shadow out past it
            m = (optics.shadow_blur * 2 + abs(optics.shadow_y) + 2) * scale
            hug = 3.0 * scale
            clip = QRectF(clip)
            if abs(clip.left() - dev.left()) <= hug:
                clip.setLeft(clip.left() - m)
            if abs(clip.top() - dev.top()) <= hug:
                clip.setTop(clip.top() - m)
            if abs(clip.right() - dev.right()) <= hug:
                clip.setRight(clip.right() + m)
            if abs(clip.bottom() - dev.bottom()) <= hug:
                clip.setBottom(clip.bottom() + m)
        depth = 0
        if not floating:
            for s in self.shapes:
                if not s.floating and s.rect.adjusted(-1.5, -1.5, 1.5, 1.5).contains(dev):
                    depth = max(depth, s.depth + 1)
        self.shapes.append(Shape(dev, radius * scale, optics, scale, tint, clip, shadow, depth, floating, level))
        if floating:   # what this painter draws next, inside the shape, belongs on top of its glass
            outside = QPainterPath()
            outside.addRect(QRectF(-1e5, -1e5, 2e5, 2e5))
            p.setClipPath(outside.subtracted(_rounded(rect, radius)), Qt.ClipOperation.IntersectClip)
        return True

    @staticmethod
    def _clip(p: QPainter, tr) -> QRectF | None:
        clip = None
        engine = p.paintEngine()
        if engine is not None:
            region = engine.systemClip()
            if not region.isEmpty():
                clip = QRectF(region.boundingRect())
        if p.hasClipping():
            c = tr.mapRect(p.clipBoundingRect())
            clip = c if clip is None else clip.intersected(c)
        return clip


COLLECTOR = _Collector()


class _Live:
    """The live window (phase 2): its refracted glass is a cached image painted under the widgets.

    While ``on``, :func:`yuki.ui.window.glass.paint_glass` paints nothing for a level the glass
    setting refracts (it is already in the cached image) and paints the rest as crisp flat glass.
    """

    on = False


LIVE = _Live()


def collecting() -> bool:
    """True while glass controls are real glass (recording shapes, or the live window with the Full
    setting): widgets then draw droplets, not flat stand-ins, for their controls."""
    from yuki.ui.lens import policy

    return (COLLECTOR.active or LIVE.on) and policy().mode == "full"


# -- composing widgets over a base ----------------------------------------------------------------


@dataclass
class Placed:
    """A widget on the canvas: where (logical px), and an optional rounded clip for its content."""

    widget: object
    pos: QPointF
    clip_radius: float | None = None
    shapes: list[Shape] = field(default_factory=list)
    below: QImage | None = None
    above: QImage | None = None


def compose_layers(items: Iterable[Placed], theme: Theme, canvas: QSize, dpr: float,
                   render: Callable[[object], QImage]) -> list[Layer]:
    """Render each widget twice (content below and above floating glass), collect its shapes, and stack
    everything into layers for :class:`Compositor` over a ``canvas``-sized (logical px) base."""
    items = list(items)
    for it in items:
        COLLECTOR.begin(theme, "below")
        try:
            it.below = render(it.widget)
        finally:
            shapes = COLLECTOR.end()
        dx, dy = it.pos.x() * dpr, it.pos.y() * dpr
        it.shapes = [s.moved(dx, dy) for s in shapes]
        if any(s.floating for s in shapes):
            COLLECTOR.begin(theme, "above")
            try:
                it.above = render(it.widget)
            finally:
                COLLECTOR.end()

    def canvas_image(which: str) -> QImage | None:
        img = QImage(int(canvas.width() * dpr), int(canvas.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        drawn = False
        for it in items:
            src = it.below if which == "below" else it.above
            if src is None:
                continue
            p.save()
            if it.clip_radius is not None:
                p.setClipPath(_rounded(QRectF(it.pos, QSize(src.width() / dpr, src.height() / dpr)), it.clip_radius))
            if which == "above":   # only what sits on floating glass
                mask = QPainterPath()
                for s in it.shapes:
                    if s.floating:
                        mask.addPath(_rounded(QRectF(s.rect.x() / dpr, s.rect.y() / dpr, s.rect.width() / dpr,
                                                     s.rect.height() / dpr), s.radius / dpr))
                p.setClipPath(mask, Qt.ClipOperation.IntersectClip)
            p.drawImage(it.pos, src)
            p.restore()
            drawn = True
        p.end()
        return img if drawn else None

    shapes = [s for it in items for s in it.shapes]
    layers: list[Layer] = []
    for depth in sorted({s.depth for s in shapes if not s.floating}):
        layers.append(("glass", [s for s in shapes if not s.floating and s.depth == depth]))
    below = canvas_image("below")
    if below is not None:
        layers.append(("image", below))
    floats = [s for s in shapes if s.floating]
    if floats:
        layers.append(("glass", floats))
        above = canvas_image("above")
        if above is not None:
            layers.append(("image", above))
    return layers


_SHARED: Compositor | None = None


def shared() -> Compositor:
    """One offscreen compositor per process (mockups, tests)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Compositor()
    return _SHARED


def release_shared() -> None:
    global _SHARED
    if _SHARED is not None:
        _SHARED.release()
        _SHARED = None
