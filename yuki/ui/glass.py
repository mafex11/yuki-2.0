"""The glass window base for every surface yuki-ui shows: the overlay, the strip, nudge, question and
call cards, the call indicator and the voice pill.

Each is a frameless, translucent, always-on-top tool window with rounded
corners and a soft shadow that fades and slides when it appears.

The look is Liquid Glass (docs/UI-window.md). Since 2026-09-27 every surface
is **clear glass** over the real desktop: the window is per-pixel transparent,
the panel is a light smoke (:attr:`Theme.surface_fill`), the v3 catch-light
on its lit edge, a soft shadow, and no outline and no dark block. Nothing is
refracted here: Windows gives no app the desktop's pixels without a screen
capture, and the old stand-in (the wallpaper picture) showed the wallpaper
even over other windows. So there is no render, no image and no GPU device
for these surfaces in any glass setting; painting one costs a few fills.
(The glass setting still shapes the window's own glass; see
:mod:`yuki.ui.window.live`.)

Light and dark follow Windows' app mode (or the Appearance setting). Text
colours come from :meth:`GlassWindow.ink` so a surface reads on both.

The shadow is painted, not a ``QGraphicsDropShadowEffect``: graphics effects on a
translucent top-level window are unreliable. So the window keeps a transparent
margin (:data:`GlassWindow.SHADOW`) around a "panel" rectangle.

Focus is the other half of the difference between the surfaces. The overlay
must own the keyboard the instant it appears; the strip must never take it. A
``Qt.Tool`` window is shown with ``SW_SHOWNOACTIVATE`` and Qt's
``activateWindow()`` is a bare ``SetForegroundWindow``, which the foreground lock
refuses to a background tray app -- so an activating window goes through
:func:`yuki.ui.focus.force_foreground` (see :meth:`GlassWindow.take_focus`), and a
non-activating one is pinned ``WS_EX_NOACTIVATE`` so not even a click on it
activates it.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import weakref
from dataclasses import asdict, replace
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from yuki.ui.focus import FocusResult, force_foreground, set_no_activate

#: Duration of the show/hide transition, in milliseconds (0 with "Reduce motion").
ANIM_MS = 150

#: How far the window travels while fading, in logical pixels.
SLIDE_PX = 8

#: The v0 palette, kept for callers that still import it (dark surfaces).
GLASS_BG = QColor(16, 17, 20, 217)
GLASS_BORDER = QColor(255, 255, 255, 28)
TEXT_PRIMARY = QColor(238, 240, 245)
TEXT_DIM = QColor(238, 240, 245, 150)
CARD_BG = QColor(255, 255, 255, 14)
CARD_BORDER = QColor(255, 255, 255, 20)
ERROR_BG = QColor(224, 72, 72, 46)
ERROR_BORDER = QColor(255, 120, 120, 90)
ACCENT = QColor(126, 180, 255)

#: Surface glass smoke (alpha of :attr:`Theme.surface_fill`), (dark, light), per :attr:`GlassWindow.glass_density`.
SURFACE_DENSITY: dict[str, tuple[float, float]] = {"clear": (0.42, 0.58), "reading": (0.66, 0.84)}


def text_halo(label: QWidget, dark: bool) -> None:
    """A soft halo behind a label on clear glass, so one line reads over a busy screen without a dark block."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect

    halo = label.graphicsEffect()
    if not isinstance(halo, QGraphicsDropShadowEffect):
        halo = QGraphicsDropShadowEffect(label)
        halo.setBlurRadius(10)
        halo.setOffset(0, 0)
        label.setGraphicsEffect(halo)
    halo.setColor(QColor(0, 0, 0, 200) if dark else QColor(255, 255, 255, 230))


#: After a size or place change, how long to wait for it to settle before the glass is re-rendered.
SETTLE_MS = 140
#: With nothing rendered for this long, the wallpaper copy (and a GPU device) is let go.
IDLE_RELEASE_MS = 20_000


def ui_font(size: int = 11, *, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The system UI font at a given point size (Segoe UI Variable on Windows 11)."""
    font = QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    font.setPointSize(size)
    font.setWeight(weight)
    return font


# -- appearance: theme and glass setting, shared by every surface in this process -----------------------


class _Appearance:
    settings: Any = None
    hooked = False


_APPEARANCE = _Appearance()


def ui_settings() -> Any:
    """The UI settings (``ui.json``), loaded once and applied as this process's glass policy."""
    if _APPEARANCE.settings is None:
        from yuki.ui import ui_settings as us

        try:
            _APPEARANCE.settings = us.apply_policy()
        except Exception:   # a broken file never stops a surface from showing
            _APPEARANCE.settings = us.UiSettings()
            us.apply_policy(_APPEARANCE.settings)
    return _APPEARANCE.settings


def system_dark() -> bool:
    try:
        from yuki.ui.window.backdrop import system_prefers_dark

        return system_prefers_dark()
    except Exception:
        return True


def current_theme(mode: str = "auto") -> Any:
    """The window design system's :class:`~yuki.ui.window.theme.Theme` for a surface."""
    from yuki.ui.window.theme import theme

    if mode in ("dark", "light"):
        return theme(mode)
    appearance = getattr(ui_settings(), "appearance", "system")
    if appearance in ("dark", "light"):
        return theme(appearance)
    return theme("dark" if system_dark() else "light")


def reload_appearance() -> None:
    """The settings file or Windows' app mode changed: every surface re-reads and repaints."""
    from yuki.ui import lens

    _APPEARANCE.settings = None
    ui_settings()
    if lens.policy().mode == "off":
        lens.release()
    GlassWindow.refresh_all()


def _css(c: QColor) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


# -- one background thread renders every surface's glass ---------------------------------------------


class _GlassWorker(QObject):
    """Renders panel glass off the GUI thread, one job at a time (the GPU device, if any, stays on it)."""

    done = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._jobs: queue.Queue[tuple[Callable[[], Any], Callable[[Any], None]] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.done.connect(lambda pair: pair[0](pair[1]))
        self.renders = 0
        self.last_ms = 0.0
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(IDLE_RELEASE_MS)
        self._idle.timeout.connect(self._release)

    def submit(self, work: Callable[[], Any], deliver: Callable[[Any], None], *, arm_idle: bool = True) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="yuki-glass", daemon=True)
            self._thread.start()
        self._idle.stop()
        self._jobs.put((work, self._delivered(deliver, arm_idle)))

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            work, deliver = job
            started = time.perf_counter()
            try:
                result = work()
            except Exception:   # a failed render leaves the painted glass in place
                result = None
            self.last_ms = (time.perf_counter() - started) * 1000
            self.renders += 1
            self.done.emit((deliver, result))

    def _delivered(self, deliver: Callable[[Any], None], arm_idle: bool) -> Callable[[Any], None]:
        def run(result: Any) -> None:
            try:
                deliver(result)
            finally:
                if arm_idle and self._jobs.empty():
                    self._idle.start()
        return run

    def _release(self) -> None:
        from yuki.ui import lens

        self.submit(lambda: lens.release(), lambda _r: None, arm_idle=False)


_WORKER: _GlassWorker | None = None


def glass_worker() -> _GlassWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = _GlassWorker()
    return _WORKER


# -- the window ---------------------------------------------------------------------------------------


class GlassWindow(QWidget):
    """A frameless translucent panel that fades and slides in and out.

    Args:
        activates: True if the window should take keyboard focus when shown (the
            overlay does; the status strip must not, or it would pull focus out
            of whatever the user is doing).
        radius: Corner radius of the panel.

    Attributes a surface may set:
        theme_mode: ``auto`` (Windows' app mode / the Appearance setting), ``dark`` or ``light``.
        glass_level: The design system's glass optics of the panel (``popover``: frosted).
        glass_tint: A wash of colour over the glass (a nudge card's kind), or ``None``.

    Signals:
        faded_out: Emitted once the hide animation has finished and the window is
            actually hidden.
        focus_path: After every :meth:`take_focus`, a dict describing how (and
            whether) the window got the foreground -- the ``focus_path`` UI event.
    """

    #: Transparent padding reserved for the painted shadow, in logical pixels.
    SHADOW = 16

    faded_out = Signal()
    focus_path = Signal(dict)

    _instances: "weakref.WeakSet[GlassWindow]" = weakref.WeakSet()
    #: Offscreen renders only: the screen the wallpaper fills (the offscreen platform's is 800x600).
    virtual_screen: Any = None

    def __init__(self, *, activates: bool = True, radius: int = 16) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool  # no taskbar button, no alt-tab entry
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        if not activates:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.radius = radius
        self._activates = activates
        self.theme_mode = "auto"
        #: Floating surfaces carry text over anything behind them: the design's frosted glass.
        self.glass_level = "popover"
        #: ``clear`` (a one-line status) or ``reading`` (paragraphs, a chat): how much smoke the glass has.
        self.glass_density = "reading"
        self.glass_tint: QColor | None = None
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
        #: The refracted panel: (key, image) of the last render; the key wanted now; its place.
        self._glass: tuple[tuple, QImage] | None = None
        self._glass_wanted: tuple | None = None
        self._glass_target: QPoint | None = None
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._request_glass)
        GlassWindow._instances.add(self)
        self._hook_system()

    # -- appearance ----------------------------------------------------------

    @staticmethod
    def _hook_system() -> None:
        if _APPEARANCE.hooked:
            return
        _APPEARANCE.hooked = True
        try:
            from PySide6.QtGui import QGuiApplication

            hints = QGuiApplication.styleHints()
            hints.colorSchemeChanged.connect(lambda *_: reload_appearance())
        except Exception:
            pass

    def theme(self) -> Any:
        return current_theme(self.theme_mode)

    def ink(self, role: str = "text") -> QColor:
        """A text colour for this surface: ``text`` | ``text_2`` | ``text_3`` | ``accent`` | a kind name."""
        t = self.theme()
        if role in ("text", "text_2", "text_3", "accent"):
            return QColor(getattr(t, role))
        return QColor(t.kind(role))

    def ink_css(self, role: str = "text") -> str:
        return _css(self.ink(role))

    def button_css(self, style: str = "quiet", accent: QColor | None = None) -> str:
        """A pill button's style sheet: ``quiet`` (outlined) or ``tinted`` (the accent's wash)."""
        t = self.theme()
        # clear glass pills: a faint wash, no outline (2026-09-27)
        if style == "tinted":
            a = QColor(accent or t.accent)
            ink = a.lighter(125) if t.dark else a.darker(125)
            return (f"QPushButton {{ color: {_css(ink)}; background: rgba({a.red()},{a.green()},{a.blue()},"
                    f"{48 if t.dark else 32}); border: none;"
                    " border-radius: 11px; padding: 3px 14px; min-height: 18px; }"
                    f"QPushButton:hover {{ background: rgba({a.red()},{a.green()},{a.blue()},{80 if t.dark else 56}); }}"
                    f"QPushButton:disabled {{ color: {_css(t.text_3)}; background: transparent; }}")
        wash = QColor(t.text)
        wash.setAlphaF(0.08 if t.dark else 0.06)
        hover = QColor(t.text)
        hover.setAlphaF(0.14 if t.dark else 0.10)
        return (f"QPushButton {{ color: {_css(t.text_2)}; background: {_css(wash)}; border: none;"
                " border-radius: 11px; padding: 3px 14px; min-height: 18px; }"
                f"QPushButton:hover {{ color: {_css(t.text)}; background: {_css(hover)}; }}"
                f"QPushButton:disabled {{ color: {_css(t.text_3)}; background: transparent; }}")

    def restyle(self) -> None:
        """The theme changed: a surface re-applies its colours here (the base repaints)."""

    @classmethod
    def refresh_all(cls) -> None:
        for w in list(cls._instances):
            try:
                w._glass = None
                w._shadow_pixmap = None
                w.restyle()
                w.update()
                if w.isVisible():
                    w._request_glass()
            except RuntimeError:   # the C++ side is gone
                pass

    # -- painting ----------------------------------------------------------

    def panel_rect(self) -> QRect:
        """The opaque part of the window: everything inside the shadow margin."""
        return self.rect().adjusted(self.SHADOW, self.SHADOW, -self.SHADOW, -self.SHADOW)

    def panel_radius(self) -> float:
        """The panel's corner radius; a short panel (the voice pill, the strip) is a pill: fully round."""
        h = self.panel_rect().height()
        return h / 2 if h <= 60 else float(self.radius)

    def _level(self) -> Any:
        """Clear surface glass: the theme's smoke, the raised level's catch-light, a soft shadow that fades
        out inside the window's margin (or its edge shows as a band), and no outline.

        How much smoke depends on :attr:`glass_density`: ``clear`` for a one-line status (pill, indicator,
        strip: see-through, its label carries a soft halo) or ``reading`` for paragraphs and a chat (the
        overlay, cards, tray panels), where whatever is behind must not fight the text. Nothing behind can be
        blurred (no app can read the desktop's pixels), so legibility over a busy screen comes from this."""
        t = self.theme()
        lvl = t.level("raised")
        fill = QColor(t.surface_fill)
        fill.setAlphaF(SURFACE_DENSITY.get(self.glass_density, SURFACE_DENSITY["reading"])[0 if t.dark else 1])
        return replace(lvl, fill=fill, sheen=QColor(255, 255, 255, 0),
                       outline=QColor(0, 0, 0, 0), shadow_blur=min(lvl.shadow_blur, self.SHADOW - 4),
                       shadow_y=min(lvl.shadow_y, 4), name="surface")

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        from yuki.ui.window.glass import paint_glass

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_glass(painter, QRectF(self.panel_rect()), self.panel_radius(), self._level(), tint=self.glass_tint,
                    painted=True)

    # -- the refracted panel (Light / Full) -------------------------------------

    def _glass_key(self) -> tuple:
        from yuki.ui import lens

        pos = self._glass_target if self._glass_target is not None else self.pos()
        panel = self.panel_rect()
        pol = lens.policy()
        tint = self.glass_tint.rgba() if self.glass_tint is not None else 0
        return (panel.width(), panel.height(), pos.x() + panel.x(), pos.y() + panel.y(),
                round(self.devicePixelRatioF(), 3), self.theme().name, self.panel_radius(), self.glass_level, tint,
                pol.mode, pol.bend, pol.blur, _wallpaper_stamp())

    def _want_glass(self) -> None:
        """Nothing to render since 2026-09-27 (clear glass is painted); kept for callers."""

    def _request_glass(self) -> None:
        """Kept for older callers and tests: surfaces are painted, so there is nothing to render."""
        self._glass = None

    def _shadow(self) -> QPixmap:   # kept for subclasses that paint their own frame
        ratio = self.devicePixelRatioF()
        if self._shadow_pixmap is not None and self._shadow_key == (self.size(), ratio):
            return self._shadow_pixmap
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        self._shadow_key = (self.size(), ratio)
        self._shadow_pixmap = pixmap
        return pixmap

    # -- show / hide -------------------------------------------------------

    @staticmethod
    def _motion_ms() -> int:
        from yuki.ui import lens

        ui_settings()
        return 0 if lens.policy().reduce_motion else ANIM_MS

    def fade_in(self, target: QPoint) -> None:
        """Show the window at ``target``, fading up and sliding into place.

        Args:
            target: Final top-left position, in global logical coordinates.
        """
        self._anim.stop()
        self._closing = False
        self._glass_target = QPoint(target)
        ms = self._motion_ms()
        for animation in (self._opacity, self._move):
            animation.setDuration(ms)
        start_opacity = self.windowOpacity() if self.isVisible() else 0.0
        if not self.isVisible():
            self.setWindowOpacity(0.0 if ms else 1.0)
            self.move(target + QPoint(0, SLIDE_PX if ms else 0))
            # winId() creates the native window if needed; pin the activation
            # style before it is ever shown.
            set_no_activate(int(self.winId()), not self._activates)
            self.show()
        if self._activates:
            # Now, at show time -- not when the fade ends -- because the user starts
            # typing the moment the hotkey is released. Also when already visible:
            # a re-open during the fade-out, or a question arriving while the user
            # is in another window, must take the keyboard back too.
            self.take_focus(reason="show")
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
        ms = self._motion_ms()
        for animation in (self._opacity, self._move):
            animation.setDuration(ms)
        self._opacity.setStartValue(self.windowOpacity())
        self._opacity.setEndValue(0.0)
        self._opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(self.pos() + QPoint(0, SLIDE_PX if ms else 0))
        self._move.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.start()

    def focus_target(self) -> QWidget | None:
        """The child that should hold the keyboard once the window is active."""
        return None

    def take_focus(self, *, reason: str, allow_unlock: bool = True) -> FocusResult:
        """Make this window the foreground window and focus :meth:`focus_target`.

        The native sequence first (:func:`~yuki.ui.focus.force_foreground`:
        attach input to the foreground thread, bring to top, set foreground,
        activate, focus, detach; one injected key-up if that was refused), then
        the Qt side so Qt's own idea of the active window and focus widget agrees:
        ``raise_()``, ``activateWindow()``, ``setFocus(ActiveWindowFocusReason)``.
        The result goes out on :attr:`focus_path`.

        Args:
            reason: Why focus was taken, for the log (``show``, ``grace_end``...).
            allow_unlock: Permit the injected key-up fallback.

        Returns:
            What the native sequence achieved.
        """
        result = force_foreground(int(self.winId()), allow_unlock=allow_unlock)
        self.raise_()
        self.activateWindow()
        target = self.focus_target()
        if target is not None:
            target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.focus_path.emit({"window": self.objectName(), "reason": reason, **asdict(result)})
        return result

    @property
    def closing(self) -> bool:
        """True while the hide animation is running."""
        return self._closing

    def _on_anim_finished(self) -> None:
        if self._closing:
            self._closing = False
            self.hide()
            self.faded_out.emit()


_STAMP: list[float] = [0.0, -10.0]   # value, when it was read


def _wallpaper_stamp() -> float:
    """When the desktop picture last changed (a stat, at most every 2 s): a new one re-renders the glass."""
    now = time.monotonic()
    if now - _STAMP[1] < 2.0:
        return _STAMP[0]
    try:
        from yuki.ui.window.refract import wallpaper_path

        path = wallpaper_path()
        _STAMP[0] = os.stat(path).st_mtime if path is not None else 0.0
    except OSError:
        _STAMP[0] = 0.0
    _STAMP[1] = now
    return _STAMP[0]
