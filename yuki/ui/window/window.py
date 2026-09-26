"""The Yuki window: a glass sidebar and one page at a time. Built on open, destroyed on close.

Lifecycle (the "costs nothing when closed" rule): the tray creates a
:class:`YukiWindow` when its icon is clicked (:func:`open_window`), with
``WA_DeleteOnClose``; closing it deletes every widget, page, cached pixmap and
view model. Nothing of it runs while it is closed: no timer, no watcher, no
thread. Page data is read when a page is shown (phase 2: on a worker thread,
see docs/UI-window.md).

Material: on Windows 11 the window asks DWM for Acrylic behind it
(:mod:`yuki.ui.window.backdrop`) and paints only a tint; otherwise, and in the
offscreen mockups, :func:`~yuki.ui.window.glass.paint_backdrop` paints the
stand-in. The sidebar and every panel float on it as glass.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from yuki.ui.window.glass import paint_backdrop, paint_glass
from yuki.ui.window.refract import collecting
from yuki.ui.window.theme import SIDEBAR_W, SPACE, WINDOW_SIZE, Theme, font
from yuki.ui.window.widgets import Glass, NavItem, text_label

#: (key, glyph, label) of the six pages, in sidebar order.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("today", "today", "Today"),
    ("me", "me", "Me"),
    ("yuki", "yuki", "Yuki"),
    ("status", "status", "Status"),
    ("chat", "chat", "Chat"),
    ("settings", "settings", "Settings"),
)


class _Orb(QWidget):
    """Yuki's mark: a small glass orb with a soft accent glow."""

    def __init__(self, t: Theme, size: int = 34) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(size, size)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        g = QRadialGradient(QPointF(r.center().x() - r.width() * 0.18, r.center().y() - r.height() * 0.22),
                            r.width() * 0.75)
        a = QColor(self.t.accent)
        g.setColorAt(0.0, a.lighter(160))
        g.setColorAt(0.55, a)
        g.setColorAt(1.0, QColor(self.t.kind("review")).darker(115))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(r)
        hi = QLinearGradient(r.topLeft(), QPointF(r.left(), r.center().y()))
        hi.setColorAt(0.0, QColor(255, 255, 255, 170))
        hi.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hi)
        p.drawEllipse(r.adjusted(r.width() * 0.18, 2, -r.width() * 0.18, -r.height() * 0.45))
        p.end()


class _CaptionButtons(QWidget):
    """Minimise / maximise / close, Windows 11 style, painted (the window has no system caption)."""

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(138, 32)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setFont(font("icon_small"))
        p.setPen(self.t.text_2)
        for i, glyph in enumerate(("", "", "")):
            p.drawText(QRectF(i * 46, 0, 46, 32), Qt.AlignmentFlag.AlignCenter, glyph)
        p.end()


class Sidebar(Glass):
    navigate = Signal(str)

    def __init__(self, t: Theme, *, current: str = "today", badges: dict[str, str] | None = None,
                 listening: bool = True, remembering: bool = True, footnote: str = "") -> None:
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
        self.body.addLayout(head)
        self.items: dict[str, NavItem] = {}
        badges = badges or {}
        for key, glyph, label in PAGES:
            item = NavItem(t, glyph, label, badge=badges.get(key, ""))
            item.setChecked(key == current)
            item.clicked.connect(lambda _c=False, k=key: self.navigate.emit(k))
            self.items[key] = item
            self.body.addWidget(item)
        self.body.addStretch(1)
        foot = Glass(t, level="inset", radius=16, padding=SPACE["s"] + 2)
        foot.body.setSpacing(4)
        for on, text_on, text_off in ((listening, "Listening for “Yuki”", "Voice is off"),
                                      (remembering, "Remembering your day", "Memory paused")):
            foot.body.addWidget(text_label(t, text_on if on else text_off, "caption_strong" if on else "caption",
                                           "text_2" if on else "text_3"))
        if footnote:
            foot.body.addWidget(text_label(t, footnote, "caption", "text_3", wrap=True))
        self.body.addWidget(foot)

    def set_current(self, key: str) -> None:
        for k, item in self.items.items():
            item.setChecked(k == key)


class YukiWindow(QWidget):
    """The window. ``pages`` maps a page key to a factory returning its widget (built when first shown)."""

    closed = Signal()

    def __init__(self, t: Theme, pages: dict[str, Callable[[], QWidget]], *, current: str = "today",
                 badges: dict[str, str] | None = None, backdrop: QImage | None = None,
                 backdrop_offset: QPointF = QPointF(0, 0), dwm: bool = False, listening: bool = True,
                 remembering: bool = True, footnote: str = "") -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.t = t
        self.setObjectName("YukiWindow")
        self.setWindowTitle("Yuki")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(*WINDOW_SIZE)
        self._backdrop, self._offset, self._dwm = backdrop, backdrop_offset, dwm
        self._factories = pages
        self._built: dict[str, QWidget] = {}

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE["m"], SPACE["m"], 0, SPACE["m"])
        row.setSpacing(0)
        self.sidebar = Sidebar(t, current=current, badges=badges, listening=listening, remembering=remembering,
                               footnote=footnote)
        self.sidebar.navigate.connect(self.show_page)
        row.addWidget(self.sidebar)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 4, 0)
        top.addStretch(1)
        top.addWidget(_CaptionButtons(t))
        right.addLayout(top)
        self.stack = QStackedWidget()
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        right.addWidget(self.stack, 1)
        row.addLayout(right, 1)
        self.show_page(current)

    def show_page(self, key: str) -> None:
        if key not in self._built:
            widget = self._factories[key]()
            self._built[key] = widget
            self.stack.addWidget(widget)
        self.stack.setCurrentWidget(self._built[key])
        self.sidebar.set_current(key)

    def page(self, key: str) -> QWidget:
        self.show_page(key)
        return self._built[key]

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if collecting():   # real glass: the window is a sheet the wallpaper is refracted through
            from yuki.ui.window.theme import RADIUS

            paint_glass(p, QRectF(self.rect()), RADIUS["window"], self.t.level("floor"))
        else:
            paint_backdrop(p, QRectF(self.rect()), self.t, self._backdrop, offset=self._offset, dwm=self._dwm)
        p.end()

    def closeEvent(self, event: object) -> None:  # noqa: D102
        from yuki.ui.window.glass import SHADOWS

        SHADOWS.clear()   # nothing of the window outlives it
        self.closed.emit()
        super().closeEvent(event)

