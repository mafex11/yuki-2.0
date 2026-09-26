"""The live Yuki window: frameless Windows 11 chrome, the sidebar, one page at a time, clear glass.

Built by :mod:`yuki.ui.window.app` in the window's own process and destroyed
with it. Since 2026-09-27 the window's background is plain: black in dark mode,
white in light (no wallpaper picture; Windows gives no app the desktop's pixels
without a screen capture, so nothing behind the window can be refracted). The
Appearance setting "See-through" instead asks DWM for Acrylic, DWM's own blur of
the real desktop, composited for free.

Everything on it is clear glass. Page glass (panels, cards, pills) is painted
by Qt; with the Full setting each page's glass is one cached GPU picture
(:class:`~yuki.ui.window.glasscache.GlassPage`) that refracts the layers under
it. Glass that floats over the app's own content -- the timeline's hover card,
the toast, menus, tooltips -- bends that content, rendered once when it comes to
rest (:class:`~yuki.ui.window.widgets.FloatLens`).

Chrome (:mod:`yuki.ui.window.chrome`): drag from the sidebar's head or any empty
part of the top band, resize from the edges, Aero Snap, Windows 11's snap
layouts on the maximise button, double-click the top to maximise.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.window.glass import paint_backdrop, paint_glass, rounded
from yuki.ui.window.glasscache import GlassPage
from yuki.ui.window.theme import ICON, RADIUS, SIDEBAR_W, SPACE, WINDOW_SIZE, Theme, font
from yuki.ui.window.widgets import Glass, NavItem, Shimmer, Toast, attach_tips, scroll, text_label
from yuki.ui.window.window import PAGES, _Orb

#: The top band that drags the window (logical px).
CAPTION_H = 44
#: After a move, how long the window must be still before its floor is re-rendered for the new place.
MOVE_SETTLE_MS = 250


class CaptionButton(QAbstractButton):
    """Minimise / maximise / close, Windows 11 style (close turns red under the pointer)."""

    def __init__(self, t: Theme, kind: str) -> None:
        super().__init__()
        self.t, self.kind = t, kind
        self.setFixedSize(46, 32)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip({"min": "Minimise", "max": "Maximise", "close": "Close"}[kind])
        self.hot = False

    def enterEvent(self, e: Any) -> None:  # noqa: D102
        self.hot = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:  # noqa: D102
        self.hot = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.hot or self.isDown():
            bg = QColor(196, 43, 28) if self.kind == "close" else QColor(self.t.grid)
            if self.kind != "close":
                bg.setAlphaF(min(1.0, bg.alphaF() * 2.2))
            p.fillRect(self.rect(), bg)
        ink = QColor(255, 255, 255) if self.kind == "close" and self.hot else self.t.text_2
        p.setPen(ink)
        p.setFont(font("icon_small"))
        maxed = self.window().isMaximized()
        glyph = {"min": "", "max": "" if maxed else "", "close": ""}[self.kind]
        p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, glyph)
        p.end()


class LiveSidebar(Glass):
    """The sidebar: Yuki's mark, the six pages, and a live footer (voice, memory, today's cost)."""

    navigate = Signal(str)

    def __init__(self, t: Theme, *, current: str = "today") -> None:
        super().__init__(t, level="regular", radius=22, padding=SPACE["m"])
        self.setFixedWidth(SIDEBAR_W - SPACE["m"])
        self.body.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(6, 6, 6, 10)
        head.setSpacing(SPACE["s"] + 2)
        head.addWidget(_Orb(t))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(text_label(t, "Yuki", "title"))
        col.addWidget(text_label(t, "your PC assistant", "caption", "text_3"))
        head.addLayout(col, 1)
        self.head_box = QWidget()
        self.head_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.head_box.setLayout(head)
        self.body.addWidget(self.head_box)
        self.items: dict[str, NavItem] = {}
        for key, glyph, label in PAGES:
            item = NavItem(t, glyph, label)
            item.setChecked(key == current)
            item.clicked.connect(lambda _c=False, k=key: self.navigate.emit(k))
            self.items[key] = item
            self.body.addWidget(item)
        self.body.addStretch(1)
        self.foot = Glass(t, level="inset", radius=16, padding=SPACE["s"] + 2)
        self.foot.body.setSpacing(4)
        self.body.addWidget(self.foot)
        self._state: tuple | None = None
        self.set_footer(True, True, "")

    def set_current(self, key: str) -> None:
        for k, item in self.items.items():
            item.setChecked(k == key)
        from yuki.ui.window.glasscache import notify_glass

        notify_glass(self)   # the selection lens moved (glass in Full)

    def set_badges(self, badges: dict[str, str]) -> None:
        for k, item in self.items.items():
            if item.badge != badges.get(k, ""):
                item.badge = badges.get(k, "")
                item.update()

    def set_footer(self, listening: bool, remembering: bool, footnote: str, *, voice_detail: str = "",
                   connected: bool = True) -> None:
        state = (listening, remembering, footnote, voice_detail, connected)
        if state == self._state:
            return
        self._state = state
        t = self.t
        lay = self.foot.body
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
            elif it.layout() is not None:
                sub = it.layout()
                while sub.count():
                    w = sub.takeAt(0).widget()
                    if w is not None:
                        w.deleteLater()
        # state in words (no coloured dots): what is on reads plainly, what is off reads dimmer, and the
        # footer's glass takes a faint warm wash while something is off
        rows = [(listening, voice_detail or "Listening for “Yuki”", "Voice is off"),
                (remembering, "Remembering your day", "Memory paused")]
        if not connected:
            rows.insert(0, (False, "", "Yuki isn't running"))
        for on, text_on, text_off in rows:
            lay.addWidget(text_label(t, text_on if on else text_off, "caption_strong" if on else "caption",
                                     "text_2" if on else "text_3"))
        off = not all(on for on, _a, _b in rows)
        wash = QColor(t.kind("nudge"))
        wash.setAlphaF(0.08 if not t.dark else 0.10)
        self.foot.tint = wash if off else None
        self.foot.update()
        if footnote:
            lay.addWidget(text_label(t, footnote, "caption", "text_3", wrap=True))


class _Loading(QWidget):
    """What a page shows while its data is read (on a background thread): its title and shimmering glass
    placeholders where its panels will be. Everything is a child of this widget, so nothing pops up."""

    def __init__(self, t: Theme, title: str) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
        col.setSpacing(SPACE["l"])
        col.addWidget(text_label(t, title, "large_title", parent=self))
        col.addWidget(text_label(t, "Reading your memory…", "callout", "text_3", parent=self))
        grid = QHBoxLayout()
        grid.setSpacing(SPACE["l"])
        col.addLayout(grid)
        for rows in ((4, 3), (3, 5)):
            column = QVBoxLayout()
            column.setSpacing(SPACE["l"])
            grid.addLayout(column, 1)
            for n in rows:
                column.addWidget(Shimmer(t, rows=n, height=70 + 24 * n, parent=self))
        col.addStretch(1)


class LiveWindow(QWidget):
    """The window. The controller fills its pages (:meth:`set_page`) and does what their buttons ask.

    Signals:
        navigated: ``(page)`` the user picked a page.
        closing: the window is closing (the controller saves its place and quits).
    """

    navigated = Signal(str)
    closing = Signal()

    def __init__(self, t: Theme, *, current: str = "today") -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.t = t
        self.setObjectName("YukiWindow")
        self.setWindowTitle("Yuki")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(900, 600)
        self.resize(*WINDOW_SIZE)
        self.current = current
        self._native = False
        self._dwm = False
        self._deferred_gen: dict[str, int] = {}
        self.wallpaper_base: Callable[[], QImage | None] | None = None   # unused since 2026-09-27 (kept for callers)

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE["m"], SPACE["m"], 0, SPACE["m"])
        row.setSpacing(0)
        self.sidebar = LiveSidebar(t, current=current)
        self.sidebar.navigate.connect(self._on_nav)
        self.sidebar_glass = GlassPage(self.sidebar, self)
        self.sidebar_glass.setFixedWidth(SIDEBAR_W - SPACE["m"])
        row.addWidget(self.sidebar_glass)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        top.addStretch(1)
        self.min_button = CaptionButton(t, "min")
        self.max_button = CaptionButton(t, "max")
        self.close_button = CaptionButton(t, "close")
        self.min_button.clicked.connect(self.showMinimized)
        self.max_button.clicked.connect(self.toggle_max)
        self.close_button.clicked.connect(self.close)
        for b in (self.min_button, self.max_button, self.close_button):
            top.addWidget(b)
        right.addLayout(top)
        self.stack = QStackedWidget()
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        right.addWidget(self.stack, 1)
        row.addLayout(right, 1)
        self.slots: dict[str, QWidget] = {}
        self.glass_pages: dict[str, GlassPage] = {}
        for key, _glyph, label in PAGES:
            holder = QWidget()
            holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            lay = QVBoxLayout(holder)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(_Loading(t, label))
            self.slots[key] = holder
            self.stack.addWidget(holder)
        self.toast = Toast(t, self)
        self.show_page(current)
        attach_tips(self, lambda: self.t)

    # -- pages ------------------------------------------------------------------------------------

    def _on_nav(self, key: str) -> None:
        self.show_page(key)
        self.navigated.emit(key)

    def show_page(self, key: str) -> None:
        self.current = key
        self.stack.setCurrentWidget(self.slots[key])
        self.sidebar.set_current(key)
        page = self.glass_pages.get(key)
        if page is not None:
            page.schedule()

    def set_page(self, key: str, widget: QWidget, *, scrolls: bool = True) -> GlassPage:
        """Put a built page in its slot (replacing what was there, keeping the scroll position)."""
        holder = self.slots[key]
        lay = holder.layout()
        old_value = None
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is None:
                continue
            if isinstance(w, QScrollArea):
                old_value = w.verticalScrollBar().value()
            w.deleteLater()
        page = GlassPage(widget, self)
        self.glass_pages[key] = page
        if scrolls:
            area = scroll(page)
            lay.addWidget(area)
            self._opaque(area)
            if old_value is not None:
                QTimer.singleShot(0, lambda: area.verticalScrollBar().setValue(old_value))
        else:
            lay.addWidget(page)
        page.schedule()
        attach_tips(page, lambda: self.t)
        self._run_deferred(key, widget)
        return page

    def _opaque(self, area: QScrollArea) -> None:
        """On the plain background a scrolled page paints that background itself (opaque), so Qt scrolls it
        by moving the pixels already drawn and paints only the strip that comes into view: most of a
        smooth-scroll frame costs nothing. See-through keeps it translucent (the desktop shows through)."""
        from yuki.ui.window.widgets import scroll_css

        page = area.widget()
        opaque = not self.see_through()
        bar = area.verticalScrollBar()
        if opaque:
            # the background comes from the palette, not a style sheet: a style-sheet background is never
            # treated as opaque (no pixel-moving scroll), and a style sheet on the area resets its children's
            # palettes; so the style sheet sits on the scrollbar alone
            area.setStyleSheet("")
            bar.setStyleSheet(scroll_css(True).split("}", 1)[1])
        else:
            bar.setStyleSheet("")
            area.setStyleSheet(scroll_css(True))
        # the page (it always covers the whole viewport: the area resizes it to fit) paints the background
        # itself and says so (WA_OpaquePaintEvent), which is what lets Qt move pixels when it scrolls
        area.viewport().setAutoFillBackground(False)
        area.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, not opaque)
        page.setAutoFillBackground(False)
        page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, not opaque)
        page.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, opaque)
        if isinstance(page, GlassPage):
            page.opaque_bg = QColor(self.t.bg) if opaque else None
            page.update()

    def _run_deferred(self, key: str, widget: QWidget) -> None:
        """A page that loads progressively lists its later sections in ``widget.deferred``: each is built on
        its own turn of the event loop, into a placeholder already in the page (no window ever pops up), so
        the first sections show at once and the rest fill in without freezing the window."""
        steps = getattr(widget, "deferred", None)   # the page's own list: it is empty once all are built
        if not steps:
            return
        gen = self._deferred_gen.get(key, 0) + 1
        self._deferred_gen[key] = gen

        def step() -> None:
            if self._deferred_gen.get(key) != gen or not steps:
                return
            try:
                steps.pop(0)()
            except RuntimeError:   # the page was replaced meanwhile
                return
            if steps:
                QTimer.singleShot(0, step)
            else:
                page = self.glass_pages.get(key)
                if page is not None:
                    attach_tips(page, lambda: self.t)
                    page.schedule()

        QTimer.singleShot(0, step)

    def page_widget(self, key: str) -> QWidget | None:
        gp = self.glass_pages.get(key)
        return gp.content if gp is not None else None

    def page_offset(self, page: GlassPage) -> tuple[int, int]:
        """Where ``page``'s top-left sits in the window when unscrolled (logical px)."""
        area = page.parentWidget()
        while area is not None and not isinstance(area, QScrollArea) and area is not self:
            area = area.parentWidget()
        origin = page.mapTo(self, QPoint(0, 0))
        if isinstance(area, QScrollArea):
            origin += QPoint(0, area.verticalScrollBar().value())
        return origin.x(), origin.y()

    def see_through(self) -> bool:
        from yuki.ui import lens

        return lens.policy().see_through and self._dwm

    def forget_glass(self) -> None:
        """A glass or background setting changed: every cached picture is made again."""
        for page in list(self.glass_pages.values()) + [self.sidebar_glass]:
            page.forget()
            page.schedule()
        self.update()

    # -- window events ------------------------------------------------------------------------------

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect())
        if self.see_through():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(r, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            paint_backdrop(p, r, self.t, None, dwm=True)
        else:
            paint_backdrop(p, r, self.t, None, solid=True)
        p.end()

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.hide()

    def changeEvent(self, e: QEvent) -> None:  # noqa: D102
        if e.type() == QEvent.Type.WindowStateChange:
            self.max_button.update()
        super().changeEvent(e)

    def toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def closeEvent(self, e: Any) -> None:  # noqa: D102
        self.closing.emit()
        super().closeEvent(e)

    # -- native chrome (Windows) ----------------------------------------------------------------------

    def make_native(self, *, dwm_backdrop: bool) -> None:
        """Called once the native window exists (after show). Rule 7: never offscreen.

        ``dwm_backdrop``: the "See-through" background (DWM Acrylic of the desktop behind the window).
        """
        from yuki.ui.window import chrome

        hwnd = int(self.winId())
        self._native = chrome.make_native_frame(hwnd)
        if dwm_backdrop:
            from yuki.ui.window.backdrop import apply

            self._dwm = apply(hwnd, "acrylic", dark=self.t.dark)
        self.update()

    def set_backdrop(self, on: bool) -> None:
        """Acrylic behind the window ("See-through") or none (the plain black / white background)."""
        if not self._native:
            return
        import ctypes
        from ctypes import wintypes

        from yuki.ui.window.backdrop import apply

        hwnd = int(self.winId())
        if on:
            self._dwm = apply(hwnd, "acrylic", dark=self.t.dark)
        else:
            try:
                v = ctypes.c_int(1)   # DWMSBT_NONE
                ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 38, ctypes.byref(v),
                                                           ctypes.sizeof(v))
            except Exception:
                pass
            self._dwm = False
        for holder in self.slots.values():   # the pages go opaque (plain) or translucent (see-through)
            area = holder.findChild(QScrollArea, options=Qt.FindChildOption.FindDirectChildrenOnly)
            if area is not None:
                self._opaque(area)
        self.update()

    def _caption_at(self, x: float, y: float) -> bool:
        """An empty spot of the top band or the sidebar's head: dragging there moves the window."""
        in_top = y < CAPTION_H
        head = self.sidebar.head_box
        in_head = head.rect().contains(head.mapFrom(self, QPoint(int(x), int(y))))
        if not (in_top or in_head):
            return False
        child = self.childAt(QPoint(int(x), int(y)))
        while child is not None and child is not self:
            if isinstance(child, (QAbstractButton,)) or child.focusPolicy() != Qt.FocusPolicy.NoFocus and \
                    child.inherits("QLineEdit"):
                return False
            child = child.parentWidget()
        return True

    def _max_at(self, x: float, y: float) -> bool:
        b = self.max_button
        return b.rect().contains(b.mapFrom(self, QPoint(int(x), int(y))))

    def nativeEvent(self, event_type: Any, message: Any) -> tuple[bool, int]:  # noqa: D102
        if not self._native:
            return False, 0
        from yuki.ui.window import chrome

        try:
            hwnd, msg, wparam, lparam = chrome.message(int(message))
        except Exception:
            return False, 0
        if msg == chrome.WM_NCCALCSIZE:
            r = chrome.on_nccalcsize(hwnd, wparam, lparam)
            return (True, r) if r is not None else (False, 0)
        if msg == chrome.WM_NCHITTEST:
            sx, sy = chrome.lparam_point(lparam)
            cx, cy = chrome.screen_to_client(hwnd, sx, sy)
            dpr = self.devicePixelRatioF() or 1.0
            x, y = cx / dpr, cy / dpr
            return True, chrome.hit_test(x, y, self.width(), self.height(), maximized=self.isMaximized(),
                                         caption=self._caption_at, max_button=self._max_at)
        if msg in (chrome.WM_NCLBUTTONDOWN, chrome.WM_NCLBUTTONDBLCLK) and wparam == chrome.HTMAXBUTTON:
            self.max_button.setDown(True)
            return True, 0
        if msg == chrome.WM_NCLBUTTONUP and wparam == chrome.HTMAXBUTTON:
            self.max_button.setDown(False)
            self.toggle_max()
            return True, 0
        if msg == chrome.WM_NCMOUSEMOVE:
            hot = wparam == chrome.HTMAXBUTTON
            if hot != self.max_button.hot:
                self.max_button.hot = hot
                self.max_button.update()
        elif msg == chrome.WM_NCMOUSELEAVE and self.max_button.hot:
            self.max_button.hot = False
            self.max_button.update()
        return False, 0
