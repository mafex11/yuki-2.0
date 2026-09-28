"""The window base for every floating surface yuki-ui shows: the overlay, the strip, nudge, question and call
cards, the call pill, the voice pill and the tray's panels.

Each is a frameless, always-on-top tool window: a Horizon floating dialog (the navy
panel ``#10141B`` with a 1 px ``#222A36`` line, 14 px corners, a soft shadow; pills are
fully round) that fades and slides a few pixels when it appears. Opaque: whatever is
behind it never shows through, so text reads the same over any screen. Dark only; the
accent only on live and recording states (docs/UI-window.md).

The shadow is painted, not a ``QGraphicsDropShadowEffect`` (graphics effects on a
translucent top-level window are unreliable), so the window keeps a transparent
margin (:data:`SurfaceWindow.SHADOW`) around a "panel" rectangle; the shadow is
drawn once per size and reused.

Focus is the other half of the difference between the surfaces. The overlay
must own the keyboard the instant it appears; the strip must never take it. A
``Qt.Tool`` window is shown with ``SW_SHOWNOACTIVATE`` and Qt's
``activateWindow()`` is a bare ``SetForegroundWindow``, which the foreground lock
refuses to a background tray app -- so an activating window goes through
:func:`yuki.ui.focus.force_foreground` (see :meth:`SurfaceWindow.take_focus`), and
a non-activating one is pinned ``WS_EX_NOACTIVATE`` so not even a click on it
activates it.
"""

from __future__ import annotations

import weakref
from dataclasses import asdict
from typing import Any

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
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from yuki.ui.focus import FocusResult, force_foreground, set_no_activate

#: Duration of the show/hide transition, in milliseconds (0 with "Reduce motion").
ANIM_MS = 150

#: How far the window travels while fading, in logical pixels.
SLIDE_PX = 6



def accent_color() -> QColor:
    """The user's accent (``ui.json`` ``accent``: olive green by default, or orange)."""
    from yuki.ui.window.theme import theme

    return QColor(theme().accent)


#: The tray icon's mark: the accent as the app started (the rest of the UI reads it live).
ACCENT = accent_color()
#: Flat tokens for callers that paint with fixed colours (the card-stack overlay, the tray panels' HTML).
TEXT_PRIMARY = QColor("#E6E9EE")
TEXT_DIM = QColor("#AEB6C2")
CARD_BG = QColor("#161C26")
CARD_BORDER = QColor("#222A36")
ERROR_BG = QColor("#2A1416")
ERROR_BORDER = QColor("#5A2A2E")


def ui_font(size: int = 11, *, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The UI font at a point size (the window's family and weights, :func:`yuki.ui.window.theme.variable_font`)."""
    from yuki.ui.window.theme import variable_font

    word = ("Semibold" if weight >= QFont.Weight.DemiBold else "Medium" if weight >= QFont.Weight.Medium else
            "Light" if weight <= QFont.Weight.Light else "Regular")
    return variable_font(size * 4 / 3, word)


def current_theme(mode: str = "dark") -> Any:
    """The design system's :class:`~yuki.ui.window.theme.Theme` (dark only)."""
    from yuki.ui.window.theme import theme

    del mode
    return theme()


def reload_appearance() -> None:
    """The settings file changed (the window said so over the pipe): every surface re-reads and repaints."""
    from yuki.ui import ui_settings

    ui_settings.reload()
    SurfaceWindow.refresh_all()


def _css(c: QColor) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def button_css(style: str = "outline") -> str:
    """A Horizon pill button as a style sheet: ``default`` (primary: light with dark ink) | ``outline`` |
    ``ghost`` (words only) | ``destructive``; the older words ``tinted`` (primary) and ``quiet`` (outline)
    still work."""
    from yuki.ui.window.widgets import button_colors

    t = current_theme()
    v = {"tinted": "default", "quiet": "outline", "glass": "secondary", "danger": "destructive"}.get(style, style)
    fill, border, ink = button_colors(t, v)
    hfill, hborder, hink = button_colors(t, v, hover=True)
    _f, _b, dink = button_colors(t, v, enabled=False)
    dfill = QColor(fill)
    dfill.setAlphaF(fill.alphaF() * 0.5)
    weight = 600 if v == "default" else 400
    return (f"QPushButton {{ color: {_css(ink)}; background: {_css(fill)}; border: 1px solid {_css(border)};"
            f" border-radius: 15px; padding: 0px 14px; min-height: 28px; font-weight: {weight}; }}"
            f"QPushButton:hover {{ color: {_css(hink)}; background: {_css(hfill)}; border-color: {_css(hborder)}; }}"
            f"QPushButton:pressed {{ background: {_css(hfill)}; }}"
            f"QPushButton:disabled {{ color: {_css(dink)}; background: {_css(dfill)}; }}")


# -- the window ---------------------------------------------------------------------------------------


class SurfaceWindow(QWidget):
    """A frameless floating dialog (Horizon) that fades and slides in and out.

    Args:
        activates: True if the window should take keyboard focus when shown (the
            overlay does; the status strip must not, or it would pull focus out
            of whatever the user is doing).
        radius: Corner radius of the panel (Horizon: 14; a pill passes half its height).

    Signals:
        faded_out: Emitted once the hide animation has finished and the window is
            actually hidden.
        focus_path: After every :meth:`take_focus`, a dict describing how (and
            whether) the window got the foreground -- the ``focus_path`` UI event.
    """

    #: Transparent padding reserved for the painted shadow, in logical pixels.
    SHADOW = 20

    faded_out = Signal()
    focus_path = Signal(dict)

    _instances: "weakref.WeakSet[SurfaceWindow]" = weakref.WeakSet()

    def __init__(self, *, activates: bool = True, radius: int = 14) -> None:
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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)   # the rounded corners and shadow
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
        SurfaceWindow._instances.add(self)
        self.warmed = False
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self.warm)   # on the first idle turn: the first show is not a cold build

    def warm(self) -> None:
        """Make the native window, polish the style and lay out now (hidden), so the first show of this
        surface paints within a frame of its hotkey or event."""
        if self.warmed:
            return
        try:
            self.winId()
            self.ensurePolished()
            for child in self.findChildren(QWidget):
                child.ensurePolished()
            if self.layout() is not None:
                self.layout().activate()
            # one render offscreen primes the glyph caches and the shadow for this size (the image is dropped)
            from PySide6.QtGui import QImage

            img = QImage(max(1, self.width()), max(1, self.height()), QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            self.render(img)
            self.warmed = True
        except RuntimeError:   # gone before its first idle turn
            pass

    # -- appearance ----------------------------------------------------------

    def theme(self) -> Any:
        return current_theme()

    def ink(self, role: str = "text") -> QColor:
        """A text colour: ``text`` | ``text_2`` | ``text_3`` | ``accent`` | ``danger`` | a kind name."""
        t = self.theme()
        if role in ("text", "text_2", "text_3", "accent", "danger"):
            return QColor(getattr(t, role))
        return QColor(t.kind(role))

    def ink_css(self, role: str = "text") -> str:
        return _css(self.ink(role))

    def button_css(self, style: str = "outline", accent: QColor | None = None) -> str:
        """A button's style sheet (see :func:`button_css`); ``accent`` is accepted for older callers."""
        del accent
        return button_css(style)

    def restyle(self) -> None:
        """The settings changed: a surface re-applies its colours here (the base repaints)."""

    @classmethod
    def refresh_all(cls) -> None:
        for w in list(cls._instances):
            try:
                w.restyle()
                w.update()
            except RuntimeError:   # the C++ side is gone
                pass

    # -- painting ----------------------------------------------------------

    def panel_rect(self) -> QRect:
        """The opaque part of the window: everything inside the shadow margin."""
        return self.rect().adjusted(self.SHADOW, self.SHADOW, -self.SHADOW, -self.SHADOW)

    def panel_radius(self) -> float:
        return float(self.radius)

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        from yuki.ui.window.paint import paint_shadow, paint_surface

        painter = QPainter(self)
        panel = QRectF(self.panel_rect())
        paint_shadow(painter, panel, self.panel_radius(), blur=10, dy=6, color=QColor(0, 0, 0, 125))
        paint_surface(painter, panel, self.panel_radius(), self.theme().level("popover"))
        painter.end()

    # -- show / hide -------------------------------------------------------

    @staticmethod
    def _motion_ms() -> int:
        from yuki.ui import ui_settings

        return 0 if ui_settings.current().reduce_motion else ANIM_MS

    def fade_in(self, target: QPoint) -> None:
        """Show the window at ``target``, fading up and sliding into place.

        Args:
            target: Final top-left position, in global logical coordinates.
        """
        self._anim.stop()
        self._closing = False
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
