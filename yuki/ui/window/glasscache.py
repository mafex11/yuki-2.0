"""The live window's glass as cached pictures (the user's rule: render on change, then just composite).

Two kinds of picture, both made on the one glass thread (:func:`yuki.ui.glass.glass_worker`):

* **The floor** -- the window's sheet: the desktop wallpaper under the window,
  refracted through the window's rim. Made again only when the window is
  resized, comes to rest after a move, or the theme, the wallpaper or a glass
  setting changes. :class:`~yuki.ui.window.live.LiveWindow` paints it.
* **A page's glass** -- the panels, cards (and in Full, every pill and row) of
  one page, drawn over the floor. :class:`GlassPage` holds its page's widgets and
  paints this picture under them, so it scrolls with the page for free: nothing
  is rendered while scrolling. It is made again when the page's shapes change
  (the page was rebuilt with new data, resized, something folded open, a
  selection moved); a change that leaves every shape where it was costs nothing.

Where the shapes are: the page is painted once into a scratch image with
:data:`yuki.ui.window.refract.COLLECTOR` recording, so every widget that paints
glass becomes a lens without knowing it. With the Light setting only the large
glass is recorded (the rest paints itself as crisp flat glass); with Off nothing
is, and no picture is made at all.
"""

from __future__ import annotations

import time
import weakref
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

#: A change is rendered once it has been still this long (a resize, a fold opening).
SETTLE_MS = 120


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    r = min(radius, rect.height() / 2, rect.width() / 2)
    path.addRoundedRect(rect, r, r)
    return path


class Stats:
    """What the glass cost this run (for the measurements and the log)."""

    floor_renders = 0
    page_renders = 0
    skipped = 0
    last_ms = 0.0
    total_ms = 0.0


STATS = Stats()


def notify_glass(widget: QWidget) -> None:
    """A widget's glass changed without a resize (a toggle flipped, a selection moved): its page re-renders."""
    w: Any = widget
    while w is not None:
        if isinstance(w, GlassPage):
            w.schedule()
            return
        w = w.parentWidget()


class GlassPage(QWidget):
    """Holds one page's widgets and paints its cached glass under them.

    Args:
        content: The page's widget.
        host: The live window (it gives the theme, the floor picture and the page's place in it).
    """

    def __init__(self, content: QWidget, host: Any) -> None:
        super().__init__()
        from PySide6.QtWidgets import QVBoxLayout

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.content = content
        self._host = weakref.ref(host)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(content)
        self._image: QImage | None = None
        self._shadows: list[Any] = []
        self._sig: tuple | None = None
        self._pending: tuple | None = None
        self._collecting = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._render)

    # -- when -----------------------------------------------------------------------------------------

    def schedule(self) -> None:
        self._settle.start()

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self.schedule()

    def event(self, e: QEvent) -> bool:  # noqa: D102
        if e.type() == QEvent.Type.LayoutRequest:
            self.schedule()
        return super().event(e)

    def hideEvent(self, e: Any) -> None:  # noqa: D102 - a page not shown keeps no picture (RAM)
        super().hideEvent(e)
        self._settle.stop()
        self._image, self._shadows, self._sig, self._pending = None, [], None, None

    def showEvent(self, e: Any) -> None:  # noqa: D102
        super().showEvent(e)
        self.schedule()

    def forget(self) -> None:
        """Drop the picture (the setting went Off, or the theme changed and the page is rebuilt)."""
        self._image, self._shadows, self._sig, self._pending = None, [], None, None
        self.update()

    # -- what ------------------------------------------------------------------------------------------

    def collect(self, dpr: float) -> list[Any]:
        """The page's glass shapes (device px, page-relative), recorded from one scratch paint."""
        from yuki.ui.window.refract import COLLECTOR

        host = self._host()
        if host is None or self.width() < 2 or self.height() < 2:
            return []
        scratch = QImage(int(self.width() * dpr), int(self.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        scratch.setDevicePixelRatio(dpr)
        scratch.fill(Qt.GlobalColor.transparent)
        self._collecting = True
        COLLECTOR.begin(host.t, "below")
        try:
            self.content.render(scratch)
        finally:
            shapes = COLLECTOR.end()
            self._collecting = False
        return [s for s in shapes if not s.floating]

    def _render(self) -> None:
        from yuki.ui import lens
        from yuki.ui.glass import glass_worker

        host = self._host()
        if host is None or not self.isVisible():
            return   # rendered when it is shown
        pol = lens.policy()
        if pol.mode == "off":
            if self._image is not None:
                self.forget()
            return
        floor = host.floor_image()
        if floor is None:
            host.need_floor(self)
            return
        dpr = self.devicePixelRatioF()
        started = time.perf_counter()
        shapes = self.collect(dpr)
        offset = host.page_offset(self)
        sig = (tuple((round(s.rect.x()), round(s.rect.y()), round(s.rect.width()), round(s.rect.height()),
                      round(s.radius), s.depth, id(s.optics), s.tint.rgba() if s.tint is not None else 0,
                      (round(s.clip.x()), round(s.clip.y()), round(s.clip.width()), round(s.clip.height()))
                      if s.clip is not None else None) for s in shapes),
               self.width(), self.height(), offset, host.floor_key(), pol)
        if sig == self._sig or sig == self._pending:
            STATS.skipped += 1
            return
        self._pending = sig
        if not shapes:
            self._image, self._shadows, self._sig, self._pending = None, [], sig, None
            self.update()
            return
        base = page_base(floor, QSize(int(self.width() * dpr), int(self.height() * dpr)),
                         QPointF(offset[0] * dpr, offset[1] * dpr))
        top = [s for s in shapes if s.depth == 0]
        depths = sorted({s.depth for s in shapes})
        layers = [("glass", [s for s in shapes if s.depth == d]) for d in depths]
        mode = pol.mode
        ref = weakref.ref(self)
        collect_ms = (time.perf_counter() - started) * 1000

        def work() -> QImage:
            t0 = time.perf_counter()
            if mode == "light":
                img = lens.render_light(base, [p for _k, p in layers], shadows=False)
            else:
                img = lens.render(base, layers, "full")
            out = mask(img, top)
            STATS.last_ms = (time.perf_counter() - t0) * 1000 + collect_ms
            STATS.total_ms += STATS.last_ms
            STATS.page_renders += 1
            return out

        def deliver(img: QImage) -> None:
            page = ref()
            if page is None or img is None:
                return
            try:
                if page._pending != sig:
                    return   # superseded while it rendered
                img.setDevicePixelRatio(dpr)
                page._image, page._shadows, page._sig, page._pending = img, top, sig, None
                page.update()
            except RuntimeError:
                pass

        glass_worker().submit(work, deliver)

    # -- painting ----------------------------------------------------------------------------------

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        if self._collecting or self._image is None:
            return
        from yuki.ui.window.glass import SHADOWS

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        dpr = self._image.devicePixelRatio() or 1.0
        for s in self._shadows:
            o = s.optics
            if not s.shadow or o.shadow <= 0:
                continue
            blur = max(1, int(o.shadow_blur * 0.5))
            rect = QRectF(s.rect.x() / dpr, s.rect.y() / dpr, s.rect.width() / dpr, s.rect.height() / dpr)
            img = SHADOWS.get(int(rect.width()), int(rect.height()), s.radius / dpr, blur,
                              QColor(0, 0, 0, int(255 * min(1.0, o.shadow))), dpr)
            p.save()
            outside = QPainterPath()
            outside.addRect(QRectF(self.rect()).adjusted(-100, -100, 100, 100))
            p.setClipPath(outside.subtracted(_rounded(rect, s.radius / dpr)))
            p.drawImage(QPointF(rect.x() - blur * 2, rect.y() - blur * 2 + o.shadow_y), img)
            p.restore()
        p.drawImage(QPointF(0, 0), self._image)
        p.end()


def page_base(floor: QImage, size: QSize, offset: QPointF) -> QImage:
    """What a page's glass refracts: the floor under the page (as if unscrolled), mirrored on downward.

    A page is taller than the window; below the window's bottom the floor's last
    stretch is reflected, which reads as the same frosted sheet continuing.
    """
    out = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(1.0)
    out.fill(QColor(0, 0, 0))
    src = QImage(floor)
    src.setDevicePixelRatio(1.0)
    x0, y0 = int(offset.x()), int(offset.y())
    avail = max(1, src.height() - y0)
    p = QPainter(out)
    y, flip = 0, False
    while y < size.height():
        h = min(avail, size.height() - y)
        piece = src.copy(x0, y0 if not flip else src.height() - h, size.width(), h)
        if flip:
            piece = piece.mirrored(False, True)
        p.drawImage(0, y, piece)
        y += h
        flip = not flip
    p.end()
    return out


def mask(img: QImage, top: list[Any]) -> QImage:
    """Only the glass: ``img`` inside the outermost shapes (each within its clip), transparent elsewhere."""
    out = QImage(img.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    path = QPainterPath()
    for s in top:
        shape = _rounded(s.rect, s.radius)
        if s.clip is not None:
            clip = QPainterPath()
            clip.addRect(s.clip)
            shape = shape.intersected(clip)
        path.addPath(shape)
    src = QImage(img)
    src.setDevicePixelRatio(1.0)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setClipPath(path)
    p.drawImage(0, 0, src)
    p.end()
    return out


def render_floor(base: QImage, t: Any, radius: float, dpr: float) -> QImage:
    """The window's sheet over ``base`` (the wallpaper under the window), in the policy's mode."""
    from yuki.ui import lens
    from yuki.ui.window.refract import Shape

    rect = QRectF(0, 0, base.width(), base.height())
    shape = Shape(rect, radius * dpr, lens.scaled_optics(t.optics["floor"]), dpr, None, None, False)
    mode = lens.policy().mode
    if mode == "light":
        return lens.render_light(base, [[shape]], shadows=False)
    return lens.render(base, [("glass", [shape])], "full")
