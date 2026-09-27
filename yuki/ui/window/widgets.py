"""The window's building blocks: shadcn/ui components in Qt Widgets, all painted from
:mod:`yuki.ui.window.theme` tokens.

Card, Button (default / secondary / outline / ghost / destructive), Input, Switch
(:class:`Toggle`), Tabs (:class:`Segmented`), Select (:class:`Dropdown`), a menu,
a tooltip, a thin scrollbar, a separator (:class:`Divider`) and a skeleton
(:class:`Shimmer`). Flat and opaque: no gradients, glow, blur or translucency.

Nothing here reads memory or knows a page: a widget gets a
:class:`~yuki.ui.window.theme.Theme` and its content, and paints. Clicks come out
as Qt signals the page's controller connects to the memory calls.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.window.paint import paint_ring, paint_shadow, paint_surface, rounded
from yuki.ui.window.theme import CONTROL_H, ICON, RADIUS, SPACE, Theme, css, font, variable_font

# -- text ------------------------------------------------------------------------------


def text_label(t: Theme, text: str, role: str = "body", ink: str = "text", *, wrap: bool = False,
               selectable: bool = False, parent: QWidget | None = None) -> QLabel:
    """A label in a type role and an ink role (``text`` | ``text_2`` | ``text_3`` | ``danger`` | a kind name).

    A wrapping label never asks for more width than its container has (it wraps inside it instead)."""
    lab = QLabel(text, parent)
    lab.setFont(font(role))
    color = getattr(t, ink, None) if ink in ("text", "text_2", "text_3", "accent", "danger") else t.kind(ink)
    lab.setStyleSheet(f"color: {css(color)}; background: transparent;")
    lab.setTextFormat(Qt.TextFormat.PlainText)
    lab.setWordWrap(wrap)
    if wrap:
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)   # a new policy drops QLabel's own height-for-width flag
        lab.setSizePolicy(policy)
        # never narrower than its longest word (so no word is cut), never wider than it must be
        fm = QFontMetrics(lab.font())
        longest = max((fm.horizontalAdvance(w) for w in text.split()), default=0)
        lab.setMinimumWidth(max(24, min(180, longest + 2)))
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def icon_label(t: Theme, glyph: str, ink: QColor | None = None, role: str = "icon") -> QLabel:
    lab = QLabel(ICON.get(glyph, glyph))
    lab.setFont(font(role))
    lab.setStyleSheet(f"color: {css(ink or t.text_2)}; background: transparent;")
    return lab


def hbox(*items: QWidget | int | None, spacing: int = SPACE["s"], margins: tuple[int, int, int, int] = (0, 0, 0, 0),
         parent: QWidget | None = None) -> QHBoxLayout:
    """A row; an int is a stretch (0 = one stretch), ``None`` is skipped."""
    row = QHBoxLayout(parent) if parent else QHBoxLayout()
    row.setSpacing(spacing)
    row.setContentsMargins(*margins)
    for it in items:
        if it is None:
            continue
        if isinstance(it, int):
            row.addStretch(it or 1)
        else:
            row.addWidget(it)
    return row


def vbox(*items: QWidget | int | None, spacing: int = SPACE["s"], margins: tuple[int, int, int, int] = (0, 0, 0, 0),
         parent: QWidget | None = None) -> QVBoxLayout:
    col = QVBoxLayout(parent) if parent else QVBoxLayout()
    col.setSpacing(spacing)
    col.setContentsMargins(*margins)
    for it in items:
        if it is None:
            continue
        if isinstance(it, int):
            col.addStretch(it or 1)
        else:
            col.addWidget(it)
    return col


# -- containers ------------------------------------------------------------------------


class Card(QWidget):
    """shadcn's Card: a flat surface with a 1 px border. ``level``: ``card`` (default) | ``muted`` (a quieter
    inset: evidence, a composer) | ``popover``."""

    def __init__(self, t: Theme, *, level: str = "card", radius: int = RADIUS["card"], padding: int = SPACE["l"],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.level, self.radius = t, level, radius
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(SPACE["m"])

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_surface(p, QRectF(self.rect()), self.radius, self.t.level(self.level))
        p.end()


class Panel(Card):
    """A titled card: title, optional description, optional right-hand widget, then content (CardHeader +
    CardContent)."""

    def __init__(self, t: Theme, title: str = "", subtitle: str = "", *, right: QWidget | None = None,
                 glyph: str | None = None, level: str = "card", padding: int = SPACE["xl"],
                 parent: QWidget | None = None) -> None:
        super().__init__(t, level=level, padding=padding, parent=parent)
        self.body.setSpacing(SPACE["l"])
        if title:
            # the icon sits on the title's line, top left; a control on the right lines up with that line
            head = QHBoxLayout()
            head.setSpacing(SPACE["s"] + 2)
            line_h = QFontMetrics(font("headline")).height()
            lift = max(0, ((right.sizeHint().height() if right is not None else 0) - line_h) // 2)
            if glyph:
                ic = icon_label(t, glyph, t.text_2)
                ic.setFixedSize(18, line_h)
                ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
                wrap = QVBoxLayout()
                wrap.setContentsMargins(0, lift, 0, 0)
                wrap.addWidget(ic)
                wrap.addStretch(1)
                head.addLayout(wrap)
            col = QVBoxLayout()
            col.setSpacing(4)
            col.setContentsMargins(0, lift, 0, 0)
            col.addWidget(text_label(t, title, "headline", wrap=True))
            if subtitle:
                col.addWidget(text_label(t, subtitle, "caption", "text_2", wrap=True))
            col.addStretch(1)
            head.addLayout(col, 1)
            if right is not None:
                head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
            self.body.addLayout(head)


def scrollbar_css(t: Theme) -> str:
    """shadcn's ScrollArea bar: thin, a subtle handle that brightens under the pointer, no arrows."""
    return ("QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; }"
            f"QScrollBar::handle:vertical {{ background: {css(t.muted)}; border-radius: 4px; min-height: 32px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {css(t.baseline)}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
            "QScrollBar:horizontal { height: 0; }")


def opaque(widget: QWidget, color: QColor) -> None:
    """Paint ``widget``'s background with ``color`` from its palette (a style-sheet background is never treated
    as opaque, so Qt could not scroll by moving pixels)."""
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.Window, color)
    widget.setPalette(pal)
    widget.setAutoFillBackground(True)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)


def scroll(widget: QWidget, t: Theme | None = None) -> QScrollArea:
    """A scroll area with the thin bar and smooth scrolling. With ``t`` the page is opaque on the theme's
    background, so a scroll step moves the pixels already drawn and paints only the strip that comes in."""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    if t is not None:
        opaque(area.viewport(), t.bg)
        opaque(widget, t.bg)
        area.verticalScrollBar().setStyleSheet(scrollbar_css(t))
    else:
        area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        area.viewport().setAutoFillBackground(False)
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    smooth(area)
    return area


# -- buttons ---------------------------------------------------------------------------

#: The older style words, as shadcn variants.
VARIANTS = {"glass": "secondary", "quiet": "outline", "accent": "default", "tinted": "secondary",
            "danger": "destructive", "primary": "default"}


def button_colors(t: Theme, variant: str, *, hover: bool = False, down: bool = False,
                  enabled: bool = True) -> tuple[QColor, QColor, QColor]:
    """``(fill, border, ink)`` of a shadcn button variant: ``default`` (primary) | ``secondary`` | ``outline`` |
    ``ghost`` | ``destructive``. Hover and pressed shift the fill a step; disabled is at half opacity."""
    v = VARIANTS.get(variant, variant)
    clear = QColor(0, 0, 0, 0)
    hot = hover or down
    if v == "default":
        fill, border, ink = QColor("#e4e4e7" if hot else "#fafafa"), clear, QColor(t.primary_fg)
    elif v == "destructive":
        fill, border, ink = QColor("#991b1b" if hot else t.destructive.name()), clear, QColor(t.text)
    elif v == "outline":
        fill, border, ink = QColor(t.muted if hot else t.bg), QColor(t.border), QColor(t.text)
    elif v == "ghost":
        fill, border, ink = (QColor(t.muted) if hot else clear), clear, QColor(t.text if hot else t.text_2)
    else:   # secondary
        fill, border, ink = QColor("#323236" if hot else t.muted.name()), clear, QColor(t.text)
    if not enabled:
        for c in (fill, border, ink):
            c.setAlphaF(c.alphaF() * 0.5)
    return fill, border, ink


def paint_button(p: QPainter, r: QRectF, t: Theme, style: str = "secondary", tint: QColor | None = None, *,
                 hover: bool = False, down: bool = False, enabled: bool = True) -> QColor:
    """A button's body; returns the ink for its label. ``tint`` is accepted for older callers and ignored
    (colour is not decoration here)."""
    del tint
    fill, border, ink = button_colors(t, style, hover=hover, down=down, enabled=enabled)
    paint_surface(p, r, RADIUS["control"], t.level("card"), fill=fill, border=border)
    return ink


class Button(QAbstractButton):
    """shadcn's Button. ``style``: ``default`` | ``secondary`` | ``outline`` | ``ghost`` | ``destructive`` (or an
    older word, see :data:`VARIANTS`); ``size``: ``small`` (32 px) | ``regular`` (36) | ``large`` (40); a glyph
    alone makes a square icon button."""

    def __init__(self, t: Theme, text: str = "", *, glyph: str | None = None, style: str = "outline",
                 size: str = "regular", tint: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.style, self.size_name, self.glyph = t, style, size, glyph
        del tint
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(variable_font(13 if size == "small" else 14, "Medium"))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _pad(self) -> int:
        return 12 if self.size_name == "small" else 16

    def sizeHint(self) -> QSize:  # noqa: D102
        h = CONTROL_H[self.size_name]
        if not self.text():
            return QSize(h, h)
        fm = QFontMetrics(self.font())
        icon = (16 + 8) if self.glyph else 0
        return QSize(fm.horizontalAdvance(self.text()) + icon + 2 * self._pad(), h)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        ink = paint_button(p, r, self.t, self.style, hover=self.underMouse(), down=self.isDown(),
                           enabled=self.isEnabled())
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus and self.isEnabled():
            ring = QColor(self.t.ring)
            ring.setAlphaF(0.5)
            paint_ring(p, r.adjusted(1, 1, -1, -1), RADIUS["control"], ring, 2.0)
        p.setPen(ink)
        pad = self._pad()
        glyph = ICON.get(self.glyph, self.glyph) if self.glyph else ""
        if glyph and not self.text():
            p.setFont(font("icon_small" if self.size_name == "small" else "icon"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, glyph)
            p.end()
            return
        fm = QFontMetrics(self.font())
        content = fm.horizontalAdvance(self.text()) + ((16 + 8) if glyph else 0)
        x = max(r.left() + pad, r.center().x() - content / 2)
        if glyph:
            p.setFont(font("icon_small"))
            p.drawText(QRectF(x, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, glyph)
            x += 16 + 8
        p.setFont(self.font())
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.right() - x - pad + 2))
        p.drawText(QRectF(x, r.top(), r.right() - x, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        p.end()


#: What each old tag says, as plain secondary text beside an item's source ("" = nothing to add: the
#: section or the source line already says it). No badges (the user reads them as generic AI UI).
TAG_WORDS = {"joke": "Said as a joke", "story": "", "you": "", "unsure": "Yuki isn't sure",
             "unclear": "May be misheard", "call": "", "live": "", "updating": "Updating"}


def tag_words(tags: Sequence[str]) -> str:
    """The plain words for an item's tags, joined (no chips)."""
    return " · ".join(w for w in (TAG_WORDS.get(tag, "") for tag in tags) if w)


# -- form controls ---------------------------------------------------------------------


class Toggle(QAbstractButton):
    """shadcn's Switch: a 44 x 24 track, the thumb a 20 px circle. On: a light track and a dark thumb."""

    def __init__(self, t: Theme, on: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        on = self.isChecked()
        track = QColor(self.t.primary if on else self.t.muted)
        thumb = QColor(self.t.primary_fg if on else self.t.text)
        if not self.isEnabled():
            track.setAlphaF(0.5)
            thumb.setAlphaF(0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawPath(rounded(r, r.height() / 2))
        d = r.height() - 4
        x = r.right() - d - 2 if on else r.left() + 2
        p.setBrush(thumb)
        p.drawEllipse(QRectF(x, r.top() + 2, d, d))
        p.end()


class Segmented(QWidget):
    """shadcn's Tabs list: mutually exclusive options on a muted track; the chosen one is a raised tab."""

    changed = Signal(str)

    def __init__(self, t: Theme, options: Sequence[str], current: str, *, small: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.options, self.current = t, list(options), current
        self.setFont(variable_font(13 if small else 14, "Medium"))
        fm = QFontMetrics(self.font())
        self._widths = [fm.horizontalAdvance(o) + (20 if small else 24) for o in self.options]
        self.setFixedSize(sum(self._widths) + 6, CONTROL_H["small" if small else "regular"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: object) -> None:  # noqa: D102
        x = event.position().x() - 3
        for opt, w in zip(self.options, self._widths):
            if x < w:
                if opt != self.current:
                    self.current = opt
                    self.update()
                    self.changed.emit(opt)
                return
            x -= w

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["card"], t.level("secondary"))
        x = r.left() + 3
        for opt, w in zip(self.options, self._widths):
            cell = QRectF(x, r.top() + 3, w, r.height() - 6)
            if opt == self.current:
                paint_surface(p, cell, RADIUS["control"], t.level("card"), border=QColor(0, 0, 0, 0))
            p.setPen(t.text if opt == self.current else t.text_2)
            p.setFont(self.font())
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, opt)
            x += w
        p.end()


class Dropdown(QAbstractButton):
    """shadcn's Select trigger: the chosen value and a chevron in a bordered box; a click opens the choices.

    ``options``: ``[(label, value), ...]`` or a callable returning them (read when opened, e.g. the
    devices plugged in now). ``changed(value)`` when the user picks one.
    """

    changed = Signal(object)

    def __init__(self, t: Theme, value: str, *, width: int = 300, glyph: str | None = None,
                 options: Sequence[tuple[str, object]] | Callable[[], Sequence[tuple[str, object]]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.options = t, glyph, options
        self.setText(value)
        self.setFont(font("body"))
        self.setFixedSize(width, CONTROL_H["regular"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open)

    def _open(self) -> None:
        opts = self.options() if callable(self.options) else self.options
        if not opts:
            return
        if len(opts) > ListPopup.ROWS:   # a long list (the quiet-hour times): a compact picker that scrolls
            popup = ListPopup(self.t, [(label, value) for label, value in opts], self.text(), width=self.width())
            popup.picked.connect(self._pick)
            popup.open_below(self)
            self._popup = popup
            return
        menu = Menu(self.t, self)
        menu.setMinimumWidth(self.width())
        for label, value in opts:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(label == self.text())
            act.triggered.connect(lambda _c=False, lab=label, v=value: self._pick(lab, v))
        menu.popup(self.mapToGlobal(self.rect().bottomLeft() + QPoint(0, 4)))

    def _pick(self, label: str, value: object) -> None:
        self.setText(label)
        self.update()
        self.changed.emit(value)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["control"], t.level("input"),
                      fill=QColor(t.subtle) if self.underMouse() else None)
        x = r.left() + 12
        if self.glyph:
            p.setFont(font("icon_small"))
            p.setPen(t.text_2)
            p.drawText(QRectF(x, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
            x += 24
        p.setFont(self.font())
        p.setPen(t.text)
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.right() - 32 - x))
        p.drawText(QRectF(x, r.top(), r.right() - 32 - x, r.height()), Qt.AlignmentFlag.AlignVCenter, text)
        p.setFont(font("icon_small"))
        p.setPen(t.text_2)
        p.drawText(QRectF(r.right() - 30, r.top(), 18, r.height()), Qt.AlignmentFlag.AlignCenter, ICON["down"])
        p.end()


class _Field(QWidget):
    """The bordered box around a line edit, with the focus ring when it holds the keyboard."""

    def __init__(self, t: Theme, edit: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.edit = t, edit
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.addWidget(edit)
        edit.installEventFilter(self)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return False

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        t = self.t
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)   # room for the ring
        focused = self.edit.hasFocus()
        paint_surface(p, r, RADIUS["control"], t.level("input"),
                      border=QColor("#52525b") if focused else None)
        if focused:
            ring = QColor(t.ring)
            ring.setAlphaF(0.35)
            paint_ring(p, r, RADIUS["control"], ring, 2.0)
        p.end()


class Input(QWidget):
    """shadcn's Input (a bordered box, a subtle focus ring), with an optional button beside it."""

    submitted = Signal(str)

    def __init__(self, t: Theme, placeholder: str, *, action: str | None = None, action_glyph: str | None = None,
                 text: str = "", height: int = CONTROL_H["input"], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(height + 4)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["s"] - 2)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFont(font("body"))
        self.edit.setFrame(False)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; color: {css(t.text)}; border: none;"
            f" selection-background-color: {css(t.accent_soft)}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, t.text_3)
        self.edit.setPalette(pal)
        self.field = _Field(t, self.edit, self)
        row.addWidget(self.field, 1)
        self.button: Button | None = None
        if action or action_glyph:
            self.button = Button(t, action or "", glyph=action_glyph, style="default",
                                 size="regular" if height >= 36 else "small")
            wrap = QVBoxLayout()
            wrap.setContentsMargins(0, 2, 0, 2)
            wrap.addWidget(self.button)
            row.addLayout(wrap)
            self.button.clicked.connect(lambda: self.submitted.emit(self.edit.text()))
        self.edit.returnPressed.connect(lambda: self.submitted.emit(self.edit.text()))


class Avatar(QWidget):
    """shadcn's Avatar fallback: a round initial on the muted surface (people)."""

    def __init__(self, t: Theme, name: str, size: int = 36, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.name = t, name
        self.setFixedSize(size, size)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.t.muted)
        p.drawEllipse(r)
        p.setPen(self.t.text)
        f = variable_font(r.height() * 0.40, "Medium")
        p.setFont(f)
        initial = next((ch for ch in self.name if ch.isalnum()), "?").upper()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, initial)
        p.end()


class NavItem(QAbstractButton):
    """A sidebar entry (shadcn's sidebar menu button): glyph and word; the current page on the muted fill."""

    def __init__(self, t: Theme, glyph: str, text: str, *, badge: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.badge = t, glyph, badge
        self.setText(text)
        self.setCheckable(True)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        if self.isChecked():
            paint_surface(p, r, RADIUS["control"], t.level("selected"))
        elif self.underMouse():
            paint_surface(p, r, RADIUS["control"], t.level("muted"))
        on = self.isChecked() or self.underMouse()
        p.setPen(t.text if on else t.text_2)
        p.setFont(font("icon"))
        p.drawText(QRectF(r.left() + 10, r.top(), 18, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
        p.setFont(variable_font(14, "Medium" if self.isChecked() else "Regular"))
        p.drawText(QRectF(r.left() + 38, r.top(), r.width() - 80, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   self.text())
        if self.badge:   # a count in plain secondary text
            p.setFont(font("caption"))
            p.setPen(t.text_2)
            p.drawText(QRectF(r.right() - 44, r.top(), 34, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.badge)
        p.end()


class StatTile(Card):
    """A small figure card: label, value, and an optional note under it."""

    def __init__(self, t: Theme, label: str, value: str, note: str = "", *, glyph: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(t, padding=SPACE["l"] + 4, parent=parent)
        self.body.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(text_label(t, label, "caption_strong", "text_2"))
        head.addStretch(1)
        if glyph:
            head.addWidget(icon_label(t, glyph, t.text_2, "icon_small"))
        self.body.addLayout(head)
        self.body.addWidget(text_label(t, value, "numeral"))
        if note:
            self.body.addWidget(text_label(t, note, "caption", "text_2", wrap=True))
        self.body.addStretch(1)   # top-aligned when a neighbour's note wraps onto two lines


class Divider(QWidget):
    """shadcn's Separator: 1 px of the border colour."""

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setFixedHeight(1)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.fillRect(self.rect(), self.t.border)
        p.end()


class ItemRow(QWidget):
    """One thing Yuki believes, with where it came from and the two ways to act on it.

    Layout: an optional leading widget (avatar), the title, the text, a source line
    ("Seen in Discord · yesterday"), then two ghost buttons: **Why?** (the evidence
    folds open under it) and **Not right** (a composer folds open: the user says what
    is true in their own words, or "It's just wrong, forget it"). The folds are built
    the first time they open.

    Signals:
        why: ``(ref)`` the evidence was opened.
        correct: ``(ref, words)`` the user's own correction.
        forget: ``(ref)`` "It's just wrong, forget it".
    """

    why = Signal(str)
    correct = Signal(str, str)
    forget = Signal(str)

    def __init__(self, t: Theme, ref: str, title: str, text: str, source: str, *, tags: Sequence[str] = (),
                 leading: QWidget | None = None, evidence: Sequence[tuple[str, str, str]] = (),
                 show_evidence: bool = False, composer: bool = False, placeholder: str = "",
                 actions: bool = True, title_role: str = "body_strong", pending: dict | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.ref = t, ref
        self._evidence_rows = list(evidence)
        self._placeholder = placeholder
        self._text, self._source = text, source
        self.leading_avatar = leading is not None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # every child is parented to this row as it is made (a parentless widget that is shown becomes a
        # top-level window for a moment: the "tiny windows" the Me page used to flash while it loaded)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, SPACE["xs"], 0, SPACE["xs"])
        outer.setSpacing(SPACE["m"])
        if leading is not None:
            outer.addWidget(leading, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        outer.addLayout(col, 1)
        self._col = col
        if title:
            col.addWidget(text_label(t, title, title_role, wrap=True))
        self._tag_words = tag_words(tags)
        self.text_label = text_label(t, text, "callout", "text", wrap=True, selectable=True)
        col.addWidget(self.text_label)
        self.was_label: QLabel | None = None
        self.source_label = text_label(t, " · ".join(x for x in (source, self._tag_words) if x), "caption",
                                       "text_2", wrap=True)
        col.addWidget(self.source_label)
        foot = QHBoxLayout()
        foot.setSpacing(4)
        foot.setContentsMargins(0, 2, 0, 0)
        self.why_pill: Button | None = None
        self.wrong_pill: Button | None = None
        if actions:
            self.why_pill = Button(t, "Why?", style="ghost", size="small")
            self.why_pill.clicked.connect(self.toggle_evidence)
            foot.addWidget(self.why_pill)
            if not evidence:
                self.why_pill.hide()
            self.wrong_pill = Button(t, "Not right", glyph="edit", style="ghost", size="small")
            self.wrong_pill.clicked.connect(self.toggle_composer)
            foot.addWidget(self.wrong_pill)
            foot.addStretch(1)
            col.addLayout(foot)
        self._ev_box: QWidget | None = None
        self._composer: QWidget | None = None
        self.field: Input | None = None
        self.done_note: QLabel | None = None
        if pending:
            self.set_pending(pending)
        elif show_evidence and evidence:
            self.toggle_evidence()
        if composer and not pending:
            self.toggle_composer()

    def set_pending(self, pending: dict) -> None:
        """Show a correction the user sent that memory has not applied yet, in the user's own words.

        ``pending``: ``{"kind": "correct" | "forget" | "confirm", "words", "at", "late"}``. Never the old
        text as if nothing happened: the user's words lead, the old text sits under them, dimmed.
        """
        from datetime import datetime

        t = self.t
        kind = pending.get("kind") or "correct"
        words = " ".join(str(pending.get("words") or "").split())
        when = datetime.fromtimestamp(float(pending.get("at") or 0) or time.time()).strftime("%H:%M")
        late = bool(pending.get("late"))
        state = {"correct": "Updating", "forget": "Forgetting", "confirm": "Confirmed"}.get(kind, "Updating")
        if late and kind != "confirm":
            state = "Still updating"
        if kind == "correct" and words:
            self.text_label.setText(f"You said: “{words}”")
            if self.was_label is None:
                self.was_label = text_label(t, "", "caption", "text_3", wrap=True)
                self._col.insertWidget(self._col.indexOf(self.text_label) + 1, self.was_label)
            self.was_label.setText(f"Yuki had: {self._text}")
            note = "Yuki is putting this right" if not late else "Yuki hasn't applied this yet, it will"
            self.source_label.setText(f"{state} · You corrected this at {when} · {note}")
        elif kind == "forget":
            self.text_label.setStyleSheet(f"color: {css(t.text_3)}; background: transparent;")
            self.source_label.setText(f"{state} · You said this is wrong at {when}")
        elif kind == "confirm":
            self.source_label.setText(f"You confirmed this at {when}")
        if self.wrong_pill is not None:
            self.wrong_pill.setText("Sent to Yuki")
            self.wrong_pill.style = "ghost"
            self.wrong_pill.setEnabled(False)
            self.wrong_pill.updateGeometry()
        if self._composer is not None:
            self._composer.setVisible(False)

    def toggle_evidence(self) -> None:
        t = self.t
        if self._ev_box is None:
            ev = Card(t, level="muted", radius=RADIUS["card"], padding=SPACE["m"], parent=self)
            ev.body.setSpacing(2)
            ev.body.addWidget(text_label(t, "Why Yuki thinks this", "caption_strong", "text"))
            for when, where, words in self._evidence_rows:   # each entry: where and when, then its words
                ev.body.addSpacing(6)
                ev.body.addWidget(text_label(t, f"{when} · {where}", "micro", "text_2", wrap=True))
                ev.body.addWidget(text_label(t, words, "caption", "text", wrap=True, selectable=True))
            self._ev_box = ev
            self._col.insertWidget(self._col.count() if self._composer is None else self._col.count() - 1, ev)
            ev.setVisible(False)
        show = not self._ev_box.isVisible()
        self._ev_box.setVisible(show)
        if self.why_pill is not None:
            self.why_pill.setText("Hide why" if show else "Why?")
            self.why_pill.updateGeometry()
        if show:
            self.why.emit(self.ref)

    def toggle_composer(self) -> None:
        t = self.t
        if self._composer is None:
            box = Card(t, level="muted", radius=RADIUS["card"], padding=SPACE["m"], parent=self)
            box.body.setSpacing(SPACE["s"] + 2)
            box.body.addWidget(text_label(t, "Tell Yuki what's right, in your own words. It will fix this and "
                                             "remember it came from you.", "caption", "text_2", wrap=True))
            self.field = Input(t, self._placeholder or "e.g. “Elina is my friend”", action="Tell Yuki",
                               height=CONTROL_H["input"])
            self.field.submitted.connect(self._send)
            box.body.addWidget(self.field)
            row = QHBoxLayout()
            row.setSpacing(SPACE["s"])
            forget = Button(t, "It's just wrong, forget it", style="outline", size="small")
            forget.clicked.connect(lambda: self._sent(self.forget.emit, self.ref))
            row.addWidget(forget)
            row.addStretch(1)
            cancel = Button(t, "Cancel", style="ghost", size="small")
            cancel.clicked.connect(self.toggle_composer)
            row.addWidget(cancel)
            box.body.addLayout(row)
            self._composer = box
            self._col.addWidget(box)
            box.setVisible(False)
        show = not self._composer.isVisible()
        self._composer.setVisible(show)
        if self.wrong_pill is not None:
            self.wrong_pill.style = "secondary" if show else "ghost"
            self.wrong_pill.update()
        if show and self.field is not None:
            self.field.edit.setFocus()

    def _send(self, words: str) -> None:
        words = " ".join((words or "").split())
        if words:
            self._sent(lambda ref: self.correct.emit(ref, words), self.ref, "correct", words)

    def _sent(self, emit: Callable[[str], object], ref: str, kind: str = "forget", words: str = "") -> None:
        """Hand the correction on, and show it at once as sent (the old text never stays as if nothing happened)."""
        emit(ref)
        if self._composer is not None:
            self._composer.setVisible(False)
        self.set_pending({"kind": kind, "words": words, "at": time.time()})


class SettingRow(QWidget):
    """A settings line: glyph, a plain-language label with a hint under it, the control on the right.

    The glyph sits on the label's first line; when the control is taller than that line, the glyph and the
    label move down so the label's line and the control share one centre."""

    def __init__(self, t: Theme, glyph: str, label: str, hint: str, control: QWidget | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(SPACE["m"])
        line_h = QFontMetrics(font("body")).height()
        lift = max(0, ((control.sizeHint().height() if control is not None else 0) - line_h) // 2)
        ic = icon_label(t, glyph, t.text_2)
        ic.setFixedSize(20, line_h)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left = QVBoxLayout()
        left.setContentsMargins(0, lift, 0, 0)
        left.addWidget(ic)
        left.addStretch(1)
        row.addLayout(left)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, lift, 0, 0)
        col.addWidget(text_label(t, label, "body_strong", wrap=True))
        if hint:
            col.addWidget(text_label(t, hint, "caption", "text_2", wrap=True))
        col.addStretch(1)
        row.addLayout(col, 1)
        if control is not None:
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignTop)


def section_title(t: Theme, text: str, note: str = "") -> QWidget:
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, SPACE["s"], 0, 0)
    row.addWidget(text_label(t, text, "title"))
    row.addStretch(1)
    if note:
        row.addWidget(text_label(t, note, "caption", "text_2"))
    return w


def page_header(t: Theme, title: str, subtitle: str, right: Sequence[QWidget] = ()) -> QWidget:
    """A page's title (24 px, 600) and its description (14 px, muted), wrapping inside the page."""
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACE["l"])
    col = QVBoxLayout()
    col.setSpacing(6)
    col.addWidget(text_label(t, title, "large_title"))
    col.addWidget(text_label(t, subtitle, "body", "text_2", wrap=True))
    row.addLayout(col, 1)
    for r in right:
        row.addWidget(r, 0, Qt.AlignmentFlag.AlignTop)
    return w


# -- floating: toast, menu, tooltip --------------------------------------------------------------------


class Toast(QWidget):
    """A short note at the bottom of the window ("Told Yuki. It will fix this."): a popover card."""

    #: Room around the card for its shadow (logical px).
    MARGIN = 10
    MAX_W = 520

    def __init__(self, t: Theme, parent: QWidget) -> None:
        super().__init__(parent)
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._text = ""
        self._tone = ""
        self._timer = None
        self.hide()

    def show_text(self, text: str, *, tone: str = "", ms: int = 3200) -> None:
        from PySide6.QtCore import QTimer

        self._text, self._tone = text, tone
        par = self.parentWidget()
        fm = QFontMetrics(font("body"))
        inner = min(self.MAX_W, par.width() - 120) - 32
        br = fm.boundingRect(QRect(0, 0, max(120, inner), 400), int(Qt.TextFlag.TextWordWrap), text)
        m = self.MARGIN
        w, h = br.width() + 32 + 2 * m, max(44, br.height() + 24) + 2 * m
        self.resize(w, h)
        offset = getattr(par, "content_left", lambda: 0)()
        self.move(offset + (par.width() - offset - w) // 2, par.height() - h - 16)
        self.raise_()
        self.show()
        self.update()
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.hide)
        self._timer.start(ms)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        paint_shadow(p, r, RADIUS["card"], blur=8, dy=3)
        paint_surface(p, r, RADIUS["card"], self.t.level("popover"))
        p.setPen(self.t.danger if self._tone in ("error",) else self.t.text)
        p.setFont(font("body"))
        p.drawText(r.adjusted(16, 12, -16, -12), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                   self._text)
        p.end()


def menu_css(t: Theme) -> str:
    """A :class:`Menu`'s items (its surface is painted): shadcn's DropdownMenu item spacing and hover."""
    return (f"QMenu {{ background: transparent; color: {css(t.text)}; border: none; padding: 5px; }}"
            "QMenu::item { padding: 6px 24px 6px 28px; border-radius: 4px; background: transparent; }"
            f"QMenu::item:selected {{ background: {css(t.muted)}; }}"
            f"QMenu::item:disabled {{ color: {css(t.text_2)}; }}"
            f"QMenu::separator {{ height: 1px; background: {css(t.border)}; margin: 4px -5px; }}"
            "QMenu::indicator { width: 14px; height: 14px; left: 8px; }"
            "QMenu::right-arrow { width: 10px; height: 10px; right: 8px; }")


def _paint_menu(widget: QWidget, t: Theme) -> None:
    p = QPainter(widget)
    paint_surface(p, QRectF(widget.rect()), RADIUS["control"], t.level("popover"))
    p.end()


class Menu(QMenu):
    """shadcn's DropdownMenu: a popover with a 1 px border, 6 px corners, a muted item under the pointer."""

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFont(font("body"))
        self.setStyleSheet(menu_css(t))

    def paintEvent(self, e: object) -> None:  # noqa: D102
        _paint_menu(self, self.t)
        super().paintEvent(e)


class _MenuPainter(QObject):
    """Paints the popover surface under a plain QMenu's items (submenus made with ``addMenu``)."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__()
        self.theme = theme

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() == QEvent.Type.Paint and isinstance(obj, QWidget):
            _paint_menu(obj, self.theme())
        return False


_MENU_PAINTER: list[_MenuPainter] = []


def style_menu(menu: QMenu, theme: Callable[[], Theme]) -> None:
    """Make an existing plain QMenu (a submenu) look like :class:`Menu` (once; restyled after)."""
    if not menu.property("yuki_menu"):
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if not _MENU_PAINTER:
            _MENU_PAINTER.append(_MenuPainter(theme))
        _MENU_PAINTER[0].theme = theme
        menu.installEventFilter(_MENU_PAINTER[0])
        menu.setFont(font("body"))
        menu.setProperty("yuki_menu", True)
    menu.setStyleSheet(menu_css(theme()))


class Tip(QWidget):
    """The tooltip: a small popover (never focused), wrapping at 320 px."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowTransparentForInput)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._text = ""

    def show_tip(self, text: str, gpos: object, host: QWidget | None = None) -> None:
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QGuiApplication

        del host
        self._text = text
        fm = QFontMetrics(font("micro"))
        br = fm.boundingRect(QRect(0, 0, 320, 400), int(Qt.TextFlag.TextWordWrap), text)
        self.resize(br.width() + 24, br.height() + 14)
        pos = QPoint(gpos) + QPoint(12, 20)
        screen = QGuiApplication.screenAt(QPoint(gpos)) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            pos.setX(max(area.left(), min(pos.x(), area.right() - self.width())))
            if pos.y() + self.height() > area.bottom():
                pos.setY(QPoint(gpos).y() - self.height() - 8)
        self.move(pos)
        self.show()
        self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        t = self.theme()
        p = QPainter(self)
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["tooltip"], t.level("popover"))
        p.setPen(t.text)
        p.setFont(font("micro"))
        p.drawText(r.adjusted(12, 7, -12, -7), int(Qt.TextFlag.TextWordWrap), self._text)
        p.end()


class _TipFilter(QObject):
    """Shows :class:`Tip` instead of the system tooltip for the widgets it watches."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__()
        from PySide6.QtCore import QTimer

        self.tip = Tip(theme)
        self.owner: QWidget | None = None
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self.tip.hide)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        kind = ev.type()
        if kind == QEvent.Type.ToolTip and isinstance(obj, QWidget):
            text = obj.toolTip()
            if not text:
                return False
            self.owner = obj
            self.tip.show_tip(text, ev.globalPos(), obj.window())
            self._hide.start(max(4000, 60 * len(text)))
            return True
        if obj is self.owner and kind in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
                                          QEvent.Type.Hide, QEvent.Type.KeyPress):
            self.tip.hide()
            self.owner = None
        return False


_TIPS: dict[str, _TipFilter] = {}


def attach_tips(root: QWidget, theme: Callable[[], Theme]) -> None:
    """Flat tooltips for ``root`` and every widget under it that has one (one filter per process; a filter on
    each widget with a tooltip, not on the whole application, so other events cost nothing)."""
    f = _TIPS.get("tips")
    if f is None:
        f = _TIPS["tips"] = _TipFilter(theme)
    f.tip.theme = theme
    if not root.property("yuki_tip"):   # the root always (its tooltip may be set later, e.g. the voice pill)
        root.installEventFilter(f)
        root.setProperty("yuki_tip", True)
    for w in root.findChildren(QWidget):
        if w.toolTip() and not w.property("yuki_tip"):
            w.installEventFilter(f)
            w.setProperty("yuki_tip", True)


# -- smooth scrolling ---------------------------------------------------------------------------------


class SmoothScroller(QObject):
    """Pixel-smooth, eased, kinetic scrolling for one scroll area, for the mouse wheel and the touchpad.

    * A wheel notch moves a target ~100 px (Windows' lines per notch); the view eases to it (an exponential
      ease, ~220 ms), and quick notches add up.
    * A touchpad's small deltas move the view by exactly that many pixels, and when the fingers lift fast
      the view glides on and slows down (friction), unless Windows keeps sending its own glide.
    * One timer at the screen's refresh rate runs only while something moves; idle, nothing runs.
    """

    #: Time constant of the ease toward a wheel target (s): ~3 of these to settle.
    EASE_S = 0.075
    #: Glide friction time constant after a touchpad flick (s).
    GLIDE_S = 0.33
    #: Below this speed (px/s) a glide stops; above it at lift-off one starts.
    GLIDE_MIN = 60.0
    #: Wheel events closer than this (s) are one gesture.
    GAP_S = 0.06

    def __init__(self, area: QScrollArea) -> None:
        super().__init__(area)
        from PySide6.QtCore import QElapsedTimer, QTimer

        self.area = area
        self.bar = area.verticalScrollBar()
        self.pos = float(self.bar.value())
        self.target = self.pos
        self.vel = 0.0
        self.gliding = False
        self._setting = False
        self._last_event = 0.0
        self._touch = False
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_tick = 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.frames = 0
        area.viewport().installEventFilter(self)
        self.bar.valueChanged.connect(self._on_value)
        self.bar.rangeChanged.connect(lambda *_: self._clamp())

    def _now(self) -> float:
        return self._clock.nsecsElapsed() / 1e9

    def _on_value(self, value: int) -> None:
        if not self._setting:   # moved from outside (the scrollbar dragged, "follow the newest"): take it
            self.pos = self.target = float(value)
            self.vel = 0.0
            self.gliding = False
            self.timer.stop()

    def _clamp(self) -> None:
        lo, hi = float(self.bar.minimum()), float(self.bar.maximum())
        self.target = max(lo, min(hi, self.target))

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() == QEvent.Type.Wheel:
            self.wheel(ev)
            return True
        return False

    def wheel(self, ev: object) -> None:
        """Take one wheel event (also forwarded by a surface that scrolls from its margins)."""
        from PySide6.QtWidgets import QApplication

        if ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        now = self._now()
        pd = ev.pixelDelta().y()
        ad = ev.angleDelta().y()
        if not pd and not ad:
            return
        touch = bool(pd) or (ad % 120 != 0) or ev.phase() != Qt.ScrollPhase.NoScrollPhase
        per_notch = max(1, QApplication.wheelScrollLines()) * 33.0
        dy = -(pd if pd else ad / 120.0 * per_notch)
        self.gliding = False
        dt = max(1e-3, now - self._last_event)
        if touch:
            # exact pixels, no easing lag; the speed is kept for a glide when the fingers lift
            inst = dy / dt if dt < 0.1 else 0.0
            self.vel = inst if now - self._last_event > self.GAP_S * 2 else 0.6 * self.vel + 0.4 * inst
            self.target = self.pos + dy
            self._clamp()
            self.pos = self.target
            self._apply()
        else:
            if now - self._last_event < 0.2 and (dy > 0) == (self.target > self.pos) and self.target != self.pos:
                dy *= 1.35   # quick notches in one direction add up
            self.target += dy
            self._clamp()
            self.vel = 0.0
        self._touch = touch
        self._last_event = now
        self._start()

    def _start(self) -> None:
        if not self.timer.isActive():
            screen = self.area.screen()
            rate = screen.refreshRate() if screen is not None else 60.0
            self.timer.setInterval(max(4, int(1000.0 / max(30.0, min(240.0, rate or 60.0)))))
            self._last_tick = self._now()
            self.timer.start()

    def _apply(self) -> None:
        value = int(round(self.pos))
        if value != self.bar.value():
            self._setting = True
            try:
                self.bar.setValue(value)
            finally:
                self._setting = False

    def _tick(self) -> None:
        import math

        now = self._now()
        dt = min(0.05, max(1e-4, now - self._last_tick))
        self._last_tick = now
        self.frames += 1
        lo, hi = float(self.bar.minimum()), float(self.bar.maximum())
        if self._touch:
            if not self.gliding:
                if now - self._last_event > self.GAP_S and abs(self.vel) > self.GLIDE_MIN:
                    self.gliding = True   # the fingers lifted mid-flick: glide on
                elif now - self._last_event > self.GAP_S:
                    self.timer.stop()
                    return
                else:
                    return
            self.pos += self.vel * dt
            self.vel *= math.exp(-dt / self.GLIDE_S)
            if self.pos <= lo or self.pos >= hi:
                self.pos = max(lo, min(hi, self.pos))
                self.vel = 0.0
            self.target = self.pos
            self._apply()
            if abs(self.vel) < self.GLIDE_MIN * 0.5:
                self.gliding = False
                self.timer.stop()
            return
        gap = self.target - self.pos
        if abs(gap) < 0.5:
            self.pos = self.target
            self._apply()
            self.timer.stop()
            return
        self.pos += gap * (1.0 - math.exp(-dt / self.EASE_S))
        self._apply()


def smooth(area: QScrollArea) -> SmoothScroller:
    """Give ``area`` smooth scrolling (once)."""
    existing = getattr(area, "_smooth", None)
    if existing is not None:
        return existing
    s = SmoothScroller(area)
    area._smooth = s  # type: ignore[attr-defined]
    return s


# -- loading placeholder --------------------------------------------------------------------------------


class Shimmer(QWidget):
    """shadcn's Skeleton: a card of muted blocks that pulse gently while a section loads. Its timer runs only
    while it is shown; it is deleted when the real content takes its place."""

    def __init__(self, t: Theme, *, rows: int = 3, height: int = 150, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QElapsedTimer, QTimer

        self.t, self.rows = t, rows
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(66)
        self._timer.timeout.connect(self.update)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(300, self.minimumHeight())

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        from yuki.ui import ui_settings

        if not ui_settings.current().reduce_motion:
            self._timer.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        import math

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["card"], t.level("card"))
        block = QColor(t.muted)
        phase = (self._clock.elapsed() % 2000) / 2000.0
        block.setAlphaF(0.75 + 0.25 * math.cos(phase * 2 * math.pi))   # animate-pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(block)
        p.drawPath(rounded(QRectF(r.left() + 24, r.top() + 24, r.width() * 0.34, 14), 4))
        y = r.top() + 54
        for i in range(self.rows):
            if y + 12 > r.bottom() - 16:
                break
            p.drawPath(rounded(QRectF(r.left() + 24, y, (r.width() - 48) * (0.92 - 0.17 * (i % 3)), 11), 4))
            y += 24
        p.end()


Callback = Callable[..., object]


# -- a compact scrolling picker (long lists: the quiet-hour times) ----------------------------------------


class _ListRow(QAbstractButton):
    """One choice in a :class:`ListPopup`: its words, the muted fill under the pointer and on the current one."""

    def __init__(self, t: Theme, text: str, current: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.current = t, current
        self.setText(text)
        self.setFont(variable_font(14, "Medium" if current else "Regular"))
        self.setFixedHeight(ListPopup.ROW_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        if self.current or self.underMouse():
            paint_surface(p, r, 4, self.t.level("selected"))
        p.setPen(self.t.text)
        p.setFont(self.font())
        p.drawText(r.adjusted(10, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())
        if self.current:
            p.setFont(font("icon_small"))
            p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       ICON["check"])
        p.end()


class ListPopup(QWidget):
    """A small popover with a scrolling list, opened under a control and scrolled to its current value.
    Replaces a menu for a long list (48 quiet-hour times made a screen-tall menu). ``picked(label, value)``."""

    picked = Signal(str, object)
    #: Rows shown at once; a longer list scrolls.
    ROWS = 8
    ROW_H = 32

    def __init__(self, t: Theme, options: Sequence[tuple[str, object]], current: str, *, width: int = 140) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.area = QScrollArea(self)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.setWidgetResizable(True)
        self.area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent;"
                                " border: none; }" + scrollbar_css(t))
        self.area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self._current_row: QWidget | None = None
        for label, value in options:
            row = _ListRow(t, label, label == current, inner)
            row.clicked.connect(lambda _c=False, lab=label, v=value: self._choose(lab, v))
            col.addWidget(row)
            if label == current:
                self._current_row = row
        self.area.setWidget(inner)
        smooth(self.area)
        outer.addWidget(self.area)
        shown = min(self.ROWS, max(1, len(options)))
        self.resize(max(width, 110), shown * self.ROW_H + 8)

    def open_below(self, anchor: QWidget) -> None:
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QGuiApplication

        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if pos.y() + self.height() > area.bottom():
                pos.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
        self.move(pos)
        self.show()
        if self._current_row is not None:   # the current value in the middle of the list
            bar = self.area.verticalScrollBar()
            bar.setValue(max(0, self._current_row.y() - (self.area.viewport().height() - self.ROW_H) // 2))

    def _choose(self, label: str, value: object) -> None:
        self.picked.emit(label, value)
        self.close()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_surface(p, QRectF(self.rect()), RADIUS["control"], self.t.level("popover"))
        p.end()


# -- a shortcut you set by pressing it -------------------------------------------------------------------


#: Qt's key codes for the modifiers, and the word each one is written as in a combo ("alt+shift").
_MOD_KEYS = {Qt.Key.Key_Control: "ctrl", Qt.Key.Key_Alt: "alt", Qt.Key.Key_Shift: "shift", Qt.Key.Key_Meta: "win",
             Qt.Key.Key_AltGr: "alt"}
#: Keys without a printable character, as :mod:`yuki.ui.hotkey` names them.
_KEY_WORDS = {Qt.Key.Key_Space: "space", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
              Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home",
              Qt.Key.Key_End: "end", Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown",
              Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
              Qt.Key.Key_Pause: "pause", Qt.Key.Key_Print: "printscreen", Qt.Key.Key_Backspace: "backspace"}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")


def combo_words(combo: str) -> str:
    """``"alt+shift"`` as a person writes it: ``"Alt + Shift"``."""
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "space": "Space", "enter": "Enter",
             "pageup": "Page Up", "pagedown": "Page Down", "printscreen": "Print Screen", "backspace": "Backspace"}
    parts = [x for x in combo.lower().split("+") if x]
    return " + ".join(names.get(x, x.upper() if len(x) <= 3 else x.capitalize()) for x in parts)


class KeyCapture(QAbstractButton):
    """An outlined field showing a shortcut; click it and press the keys you want. A key with modifiers
    ("ctrl+alt+k") or two or more modifiers alone, released together ("alt+shift"), is taken; Esc cancels.
    ``captured(combo)`` with the combo in :mod:`yuki.ui.hotkey`'s spelling."""

    captured = Signal(str)

    def __init__(self, t: Theme, combo: str, *, width: int = 200, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.combo = t, combo
        self.listening = False
        self._mods: set[str] = set()
        self._peak: set[str] = set()
        self.setFont(variable_font(14, "Medium"))
        self.setFixedSize(width, CONTROL_H["regular"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.clicked.connect(self._start)

    def _start(self) -> None:
        self.listening = True
        self._mods, self._peak = set(), set()
        self.setFocus()
        self.grabKeyboard()
        self.update()

    def _stop(self) -> None:
        self.listening = False
        self.releaseKeyboard()
        self.update()

    def focusOutEvent(self, e: object) -> None:  # noqa: D102
        if self.listening:
            self._stop()
        super().focusOutEvent(e)

    def keyPressEvent(self, e: object) -> None:  # noqa: D102
        if not self.listening:
            super().keyPressEvent(e)
            return
        key = e.key()
        if key == Qt.Key.Key_Escape and not self._mods:
            self._stop()
            return
        if key in _MOD_KEYS:
            self._mods.add(_MOD_KEYS[key])
            self._peak |= self._mods
            self.update()
            return
        word = _KEY_WORDS.get(key)
        if word is None and Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            word = f"f{int(key) - int(Qt.Key.Key_F1) + 1}"
        if word is None:
            text = (e.text() or "").strip().lower()
            if not text and 0x20 < int(key) < 0x7F:
                text = chr(int(key)).lower()
            word = text if len(text) == 1 else None
        if word is None or not self._mods:   # a shortcut needs a modifier: a plain key would fire while typing
            return
        self._finish("+".join([m for m in _MOD_ORDER if m in self._mods] + [word]))

    def keyReleaseEvent(self, e: object) -> None:  # noqa: D102
        if not self.listening:
            super().keyReleaseEvent(e)
            return
        key = e.key()
        if key in _MOD_KEYS:
            self._mods.discard(_MOD_KEYS[key])
            if not self._mods and len(self._peak) >= 2:   # modifiers alone, released together: a chord
                self._finish("+".join(m for m in _MOD_ORDER if m in self._peak))
            elif not self._mods:
                self._peak = set()
            self.update()

    def _finish(self, combo: str) -> None:
        self.combo = combo
        self._stop()
        self.captured.emit(combo)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        paint_button(p, r, t, "outline", hover=self.underMouse() and not self.listening)
        if self.listening:
            ring = QColor(t.ring)
            ring.setAlphaF(0.5)
            paint_ring(p, r, RADIUS["control"], ring, 2.0)
            live = " + ".join(combo_words(m) for m in _MOD_ORDER if m in self._mods)
            text, ink = (live + " + …" if live else "Press your shortcut…"), t.text_2 if not live else t.text
        else:
            text, ink = combo_words(self.combo) or "Not set", t.text
        p.setPen(ink)
        p.setFont(self.font())
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


# -- masonry: cards of their own height, in balanced columns ----------------------------------------------


class Masonry(QLayout):
    """Cards in columns, each exactly as tall as its content, each placed in the column that is shortest so
    far (so the columns end close together). ``columns`` at most; fewer when a column would be narrower than
    ``min_col``, down to one column in a narrow window (nothing is ever clipped). Height-for-width, so a scroll
    area sizes the page right.

    :meth:`add` puts a card in a chosen column instead (a page whose columns have a meaning); in one column
    the first column's cards come first, then the second's."""

    def __init__(self, parent: QWidget | None = None, *, columns: int = 2, spacing: int = SPACE["l"],
                 min_col: int = 360) -> None:
        super().__init__(parent)
        self._items: list = []
        self._pref: dict[int, int] = {}
        self.columns, self.min_col = columns, min_col
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def add(self, widget: QWidget, column: int) -> None:
        """Add ``widget`` to ``column`` (0-based)."""
        self.addWidget(widget)
        self._pref[id(self._items[-1])] = column

    def addItem(self, item) -> None:  # noqa: D102, N802
        w = item.widget()
        if w is not None and w.layout() is not None:
            # a card's own layout would pin its minimum to the height of its text wrapped at the narrowest
            # width; here each card gets exactly its height for the column's width
            w.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._items.append(item)

    def count(self) -> int:  # noqa: D102
        return len(self._items)

    def itemAt(self, i: int):  # noqa: D102, N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: D102, N802
        if not 0 <= i < len(self._items):
            return None
        item = self._items.pop(i)
        self._pref.pop(id(item), None)
        return item

    def expandingDirections(self):  # noqa: D102, N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: D102, N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102, N802
        return self._place(QRect(0, 0, w, 0), move=False)

    def setGeometry(self, rect) -> None:  # noqa: D102, N802
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self) -> QSize:  # noqa: D102, N802
        return QSize(self.min_col * self.columns, self.heightForWidth(self.min_col * self.columns))

    def minimumSize(self) -> QSize:  # noqa: D102, N802
        return QSize(self.min_col, 0)

    def _place(self, rect, *, move: bool) -> int:
        sp = self.spacing()
        w = rect.width()
        n = max(1, min(self.columns, (w + sp) // (self.min_col + sp)))
        col_w = max(1, (w - (n - 1) * sp) // n)
        heights = [0] * n
        order = self._items
        if self._pref and n == 1:
            order = sorted(self._items, key=lambda it: self._pref.get(id(it), 0))   # stable: column, then order
        for item in order:
            if item.isEmpty():
                continue
            pref = self._pref.get(id(item))
            c = min(pref, n - 1) if pref is not None and n > 1 else heights.index(min(heights))
            w = item.widget()
            if w is not None:
                h = w.heightForWidth(col_w) if w.hasHeightForWidth() else w.sizeHint().height()
            else:
                h = item.heightForWidth(col_w) if item.hasHeightForWidth() else item.sizeHint().height()
            if move:
                geo = QRect(rect.x() + c * (col_w + sp), rect.y() + heights[c], col_w, h)
                if w is not None:
                    if w.geometry() != geo:
                        w.setGeometry(geo)
                else:
                    item.setGeometry(geo)
            heights[c] += h + sp
        return max(0, max(heights) - sp)
