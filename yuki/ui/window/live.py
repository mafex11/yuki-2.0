"""The live Yuki window: frameless Windows 11 chrome, a sidebar, one page at a time, flat shadcn dark.

Built by :mod:`yuki.ui.window.app` in the window's own process and destroyed
with it. The window is opaque ``#09090b``; the sidebar sits on the same colour
behind a 1 px border; pages are flat cards on it (docs/UI-window.md).

Chrome (:mod:`yuki.ui.window.chrome`): drag from the sidebar's head or any empty
part of the top band, resize from the edges, Aero Snap, Windows 11's snap
layouts on the maximise button, double-click the top to maximise.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.window.theme import ICON, RADIUS, SIDEBAR_W, SPACE, WINDOW_SIZE, Theme, font
from yuki.ui.window.widgets import Card, NavItem, Shimmer, Toast, attach_tips, scroll, text_label

#: (key, glyph, label) of the six pages, in sidebar order.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("today", "today", "Today"),
    ("me", "me", "Me"),
    ("yuki", "yuki", "Yuki"),
    ("status", "status", "Status"),
    ("chat", "chat", "Chat"),
    ("settings", "settings", "Settings"),
)

#: The top band that drags the window (logical px).
CAPTION_H = 44
#: Room above each page's content for the caption buttons that sit over it (logical px).
PAGE_TOP = 40

#: Segoe Fluent Icons for the caption buttons: minimise, maximise, restore, close.
_CAPTION_GLYPHS = {"min": "", "max": "", "restore": "", "close": ""}


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
        if self.hot or self.isDown():
            p.fillRect(self.rect(), QColor(196, 43, 28) if self.kind == "close" else self.t.muted)
        p.setPen(QColor(255, 255, 255) if self.kind == "close" and self.hot else self.t.text_2)
        p.setFont(font("icon_small"))
        kind = "restore" if self.kind == "max" and self.window().isMaximized() else self.kind
        p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, _CAPTION_GLYPHS[kind])
        p.end()


class _Mark(QWidget):
    """Yuki's mark in the sidebar's head: a light rounded square with the robot glyph (shadcn's team logo)."""

    def __init__(self, t: Theme, size: int = 32) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(size, size)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from yuki.ui.window.paint import paint_surface

        p = QPainter(self)
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["card"], self.t.level("primary"))
        p.setPen(self.t.primary_fg)
        p.setFont(font("icon"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, ICON["yuki"])
        p.end()


class LiveSidebar(QWidget):
    """The sidebar: Yuki's mark, the six pages, and a live footer (voice, memory, today's cost)."""

    navigate = Signal(str)

    def __init__(self, t: Theme, *, current: str = "today") -> None:
        super().__init__()
        self.t = t
        self.setFixedWidth(SIDEBAR_W)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(SPACE["m"], SPACE["m"], SPACE["m"] + 1, SPACE["m"])
        self.body.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(4, 4, 4, SPACE["l"])
        head.setSpacing(SPACE["s"] + 2)
        head.addWidget(_Mark(t))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(text_label(t, "Yuki", "body_strong"))
        col.addWidget(text_label(t, "Your PC assistant", "caption", "text_2"))
        head.addLayout(col, 1)
        self.head_box = QWidget()
        self.head_box.setLayout(head)
        self.body.addWidget(self.head_box)
        pages = text_label(t, "Pages", "micro", "text_2")
        pages.setContentsMargins(8, 0, 0, 4)
        self.body.addWidget(pages)
        self.items: dict[str, NavItem] = {}
        for key, glyph, label in PAGES:
            item = NavItem(t, glyph, label)
            item.setChecked(key == current)
            item.clicked.connect(lambda _c=False, k=key: self.navigate.emit(k))
            self.items[key] = item
            self.body.addWidget(item)
        self.body.addStretch(1)
        self.foot = Card(t, padding=SPACE["m"])
        self.foot.body.setSpacing(4)
        self.body.addWidget(self.foot)
        self._state: tuple | None = None
        self.set_footer(True, True, "")

    def paintEvent(self, _e: object) -> None:  # noqa: D102 - the border between the sidebar and the pages
        p = QPainter(self)
        p.fillRect(self.rect(), self.t.bg)
        p.fillRect(QRectF(self.width() - 1, 0, 1, self.height()), self.t.border)
        p.end()

    def set_current(self, key: str) -> None:
        for k, item in self.items.items():
            item.setChecked(k == key)

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
                it.widget().hide()   # gone at once: a label left until deleteLater would overlap the new one
                it.widget().deleteLater()
        # state in words: what is on reads plainly, what is off reads muted
        rows = [(listening, voice_detail or "Say “Yuki” to start", "Voice is off"),
                (remembering, "Remembering your day", "Memory paused")]
        if not connected:
            rows.insert(0, (False, "", "Yuki isn't running"))
        for on, text_on, text_off in rows:
            lay.addWidget(text_label(t, text_on if on else text_off, "caption_strong" if on else "caption",
                                     "text" if on else "text_2", wrap=True))
        if footnote:
            lay.addWidget(text_label(t, footnote, "caption", "text_2", wrap=True))


class _Loading(QWidget):
    """What a page shows while its data is read (on a background thread): its title and skeleton cards where
    its panels will be. Everything is a child of this widget, so nothing pops up."""

    def __init__(self, t: Theme, title: str) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE["xl"], PAGE_TOP, SPACE["xl"], SPACE["xl"])
        col.setSpacing(SPACE["l"])
        col.addWidget(text_label(t, title, "large_title", parent=self))
        col.addWidget(text_label(t, "Reading your memory…", "body", "text_2", parent=self))
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


class Page(QWidget):
    """Holds one page's widgets below the room the caption buttons take."""

    def __init__(self, content: QWidget) -> None:
        super().__init__()
        self.content = content
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, PAGE_TOP, 0, 0)
        lay.addWidget(content)


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
        self.setMinimumSize(900, 600)
        self.resize(*WINDOW_SIZE)
        self.current = current
        self._native = False
        self._deferred_gen: dict[str, int] = {}

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar = LiveSidebar(t, current=current)
        self.sidebar.navigate.connect(self._on_nav)
        row.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        row.addWidget(self.stack, 1)
        self.caption = QWidget(self)
        top = QHBoxLayout(self.caption)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        self.min_button = CaptionButton(t, "min")
        self.max_button = CaptionButton(t, "max")
        self.close_button = CaptionButton(t, "close")
        self.min_button.clicked.connect(self.showMinimized)
        self.max_button.clicked.connect(self.toggle_max)
        self.close_button.clicked.connect(self.close)
        for b in (self.min_button, self.max_button, self.close_button):
            top.addWidget(b)
        self.caption.setFixedSize(46 * 3, 32)
        self.slots: dict[str, QWidget] = {}
        self.pages: dict[str, Page] = {}
        for key, _glyph, label in PAGES:
            holder = QWidget()
            lay = QVBoxLayout(holder)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(_Loading(t, label))
            self.slots[key] = holder
            self.stack.addWidget(holder)
        self.toast = Toast(t, self)
        self.caption.raise_()
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

    def set_page(self, key: str, widget: QWidget, *, scrolls: bool = True) -> Page:
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
        page = Page(widget)
        self.pages[key] = page
        if scrolls:
            area = scroll(page, self.t)   # opaque: a scroll step moves pixels, painting only what comes in
            lay.addWidget(area)
            if old_value is not None:
                QTimer.singleShot(0, lambda: area.verticalScrollBar().setValue(old_value))
        else:
            lay.addWidget(page)
        attach_tips(page, lambda: self.t)
        self._run_deferred(key, widget)
        return page

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
                page = self.pages.get(key)
                if page is not None:
                    attach_tips(page, lambda: self.t)

        QTimer.singleShot(0, step)

    def page_widget(self, key: str) -> QWidget | None:
        page = self.pages.get(key)
        return page.content if page is not None else None

    def content_left(self) -> int:
        """Where the pages begin (window x): toasts centre over the pages, not the sidebar."""
        return self.sidebar.width()

    # -- window events ------------------------------------------------------------------------------

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.fillRect(self.rect(), self.t.bg)
        p.end()

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self.caption.move(self.width() - self.caption.width(), 0)
        self.caption.raise_()
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

    def make_native(self) -> None:
        """Called once the native window exists (after show). Rule 7: never offscreen."""
        from yuki.ui.window import chrome

        self._native = chrome.make_native_frame(int(self.winId()), border=self.t.border)
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
