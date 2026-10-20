"""The window's building blocks in the Horizon x Field Unit look, all painted from
:mod:`yuki.ui.window.theme` tokens.

Horizon: flat panels on the umber field (:class:`Card`, :class:`Panel`), pill buttons (:class:`Button`: primary, outline,
ghost), a switch (:class:`Toggle`), a segmented choice (:class:`Segmented`), a select (:class:`Dropdown`),
inputs, a menu, a tooltip, a thin scrollbar. Field Unit: hardware keys with a status LED (:class:`Key`,
:class:`NavItem`), dot-matrix displays (:class:`DotDisplay`, :class:`TimeDisplay`), keycaps
(:class:`KeyCapture`). No gradients, glow, blur or translucency; the accent only where something is live,
current or selected.

Nothing here reads memory or knows a page: a widget gets a :class:`~yuki.ui.window.theme.Theme` and its
content, and paints. Clicks come out as Qt signals the page's controller connects to the memory calls.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
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

from yuki.ui.window.paint import (
    dots_width,
    paint_dots,
    paint_key,
    paint_led,
    paint_ring,
    paint_shadow,
    paint_surface,
    rounded,
)
from yuki.ui.window.theme import CONTROL_H, ICON, RADIUS, SPACE, Theme, css, font, variable_font

# -- text ------------------------------------------------------------------------------


def _ink(t: Theme, ink: str) -> QColor:
    if ink in ("text", "text_2", "text_3", "accent", "danger", "faint", "cool"):
        return getattr(t, ink)
    return t.kind(ink)


def text_label(t: Theme, text: str, role: str = "body", ink: str = "text", *, wrap: bool = False,
               selectable: bool = False, parent: QWidget | None = None) -> QLabel:
    """A label in a type role and an ink role (``text`` | ``text_2`` | ``text_3`` | ``danger`` | a kind name).

    A wrapping label never asks for more width than its container has (it wraps inside it instead)."""
    lab = QLabel(text, parent)
    lab.setFont(font(role))
    lab.setStyleSheet(f"color: {css(_ink(t, ink))}; background: transparent;")
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
    lab.setStyleSheet(f"color: {css(ink or t.text_3)}; background: transparent;")
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
    """A flat surface with a 1 px line. ``level``: ``card`` (a page module on the umber field) | ``muted`` (a
    "glass" inset: evidence, a composer, an empty state) | ``display`` | ``popover``."""

    def __init__(self, t: Theme, *, level: str = "card", radius: int | None = None, padding: int = SPACE["l"],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if radius is None:
            radius = RADIUS["panel"] if level == "card" else RADIUS["bubble"] if level == "muted" else RADIUS["display"]
        self.t, self.level, self.radius = t, level, radius
        self.flashing = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(SPACE["m"])

    def flash(self, ms: int = 1400) -> None:
        """Light the card's line in the accent for a moment: the section a dashboard card just led to."""
        from PySide6.QtCore import QTimer

        self.flashing = True
        self.update()
        QTimer.singleShot(ms, self._unflash)

    def _unflash(self) -> None:
        try:
            self.flashing = False
            self.update()
        except RuntimeError:
            pass

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_surface(p, QRectF(self.rect()), self.radius, self.t.level(self.level),
                      border=self.t.accent_alpha(0.75) if self.flashing else None)
        p.end()


class Panel(Card):
    """A page module: a Display-cut title, an optional muted description, an optional right-hand widget (a
    key chip such as "All 11"), then content. ``glyph`` is accepted for older callers: modules carry no icon."""

    def __init__(self, t: Theme, title: str = "", subtitle: str = "", *, right: QWidget | None = None,
                 glyph: str | None = None, level: str = "card", padding: int = 22,
                 parent: QWidget | None = None) -> None:
        super().__init__(t, level=level, padding=padding, parent=parent)
        del glyph
        self.body.setContentsMargins(padding, 20, padding, 20)
        self.body.setSpacing(SPACE["m"])
        if title:
            head = QHBoxLayout()
            head.setSpacing(SPACE["m"])
            col = QVBoxLayout()
            col.setSpacing(3)
            col.setContentsMargins(0, 0, 0, 0)
            col.addWidget(text_label(t, title, "headline", wrap=True))
            if subtitle:
                col.addWidget(text_label(t, subtitle, "caption", "text_3", wrap=True))
            col.addStretch(1)
            head.addLayout(col, 1)
            if right is not None:
                head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
            self.body.addLayout(head)
            self.body.addSpacing(2)


def scrollbar_css(t: Theme) -> str:
    """A thin bar: a quiet handle that brightens under the pointer, no arrows."""
    return ("QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; }"
            f"QScrollBar::handle:vertical {{ background: {css(t.muted)}; border-radius: 4px; min-height: 32px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {css(t.faint)}; }}"
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

#: The older style words, as this system's variants.
VARIANTS = {"glass": "outline", "quiet": "outline", "accent": "default", "tinted": "default",
            "danger": "destructive", "primary": "default", "secondary": "outline"}


def button_colors(t: Theme, variant: str, *, hover: bool = False, down: bool = False,
                  enabled: bool = True) -> tuple[QColor, QColor, QColor]:
    """``(fill, border, ink)`` of a pill button: ``default`` (primary: light, dark ink) | ``outline`` (a line,
    secondary ink) | ``ghost`` (words only) | ``destructive`` (an error's red, in words) | ``key`` (a hardware
    key face). Hover and pressed shift a step; disabled is at half opacity."""
    v = VARIANTS.get(variant, variant)
    clear = QColor(0, 0, 0, 0)
    hot = hover or down
    if v == "default":
        fill = QColor(t.primary)
        if hot:
            fill = fill.darker(112)
        border, ink = clear, QColor(t.primary_fg)
    elif v == "destructive":
        fill, border, ink = QColor(t.destructive if hot else clear), QColor(t.destructive.lighter(160)), QColor(t.danger)
    elif v == "ghost":
        fill, border, ink = (QColor(t.subtle) if hot else clear), clear, QColor(t.text if hot else t.text_2)
    elif v == "key":
        fill, border, ink = QColor(t.muted if hot else t.subtle), QColor(t.border), QColor(t.text_2)
    else:   # outline
        fill, border, ink = (QColor(t.subtle) if hot else clear), QColor(t.border), QColor(t.text if hot else t.text_2)
    if not enabled:
        for c in (fill, border, ink):
            c.setAlphaF(c.alphaF() * 0.5)
    return fill, border, ink


def paint_button(p: QPainter, r: QRectF, t: Theme, style: str = "outline", tint: QColor | None = None, *,
                 hover: bool = False, down: bool = False, enabled: bool = True) -> QColor:
    """A button's body; returns the ink for its label. ``tint`` is accepted for older callers and ignored."""
    del tint
    v = VARIANTS.get(style, style)
    fill, border, ink = button_colors(t, v, hover=hover, down=down, enabled=enabled)
    if v == "key":
        paint_key(p, r, t, hover=hover, down=down, radius=r.height() / 2)   # a capsule key
    else:
        paint_surface(p, r, r.height() / 2, t.level("card"), fill=fill, border=border)
    return ink


class Button(QAbstractButton):
    """A pill button. ``style``: ``default`` (primary) | ``outline`` | ``ghost`` | ``destructive`` | ``key`` (a
    small hardware key, the modules' "All 11" chip) or an older word (see :data:`VARIANTS`); ``size``:
    ``small`` (30 px) | ``regular`` (32) | ``large`` (36) | ``mini`` (a 24 px key chip); a glyph alone makes a
    round icon button."""

    def __init__(self, t: Theme, text: str = "", *, glyph: str | None = None, style: str = "outline",
                 size: str = "regular", tint: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.style, self.size_name, self.glyph = t, style, size, glyph
        del tint
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        primary = VARIANTS.get(style, style) == "default"
        self.setFont(variable_font(12 if size == "mini" else 13, "Semibold" if primary else "Regular"))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _pad(self) -> int:
        return 10 if self.size_name == "mini" else 14

    def _h(self) -> int:
        return 26 if self.size_name == "mini" else CONTROL_H.get(self.size_name, CONTROL_H["regular"])

    def sizeHint(self) -> QSize:  # noqa: D102
        h = self._h()
        if not self.text():
            return QSize(h, h)
        fm = QFontMetrics(self.font())
        icon = (11 + 6) if self.glyph else 0
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
            paint_ring(p, r.adjusted(1, 1, -1, -1), r.height() / 2, self.t.ring, 2.0)
        p.setPen(ink)
        pad = self._pad()
        glyph = ICON.get(self.glyph, self.glyph) if self.glyph else ""
        if glyph and not self.text():
            p.setFont(font("icon_small"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, glyph)
            p.end()
            return
        fm = QFontMetrics(self.font())
        content = fm.horizontalAdvance(self.text()) + ((11 + 6) if glyph else 0)
        x = max(r.left() + pad, r.center().x() - content / 2)
        if glyph:
            f = font("icon_small")
            f.setPixelSize(11)
            p.setFont(f)
            p.drawText(QRectF(x, r.top(), 11, r.height()), Qt.AlignmentFlag.AlignCenter, glyph)
            x += 11 + 6
        p.setFont(self.font())
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.right() - x - pad + 2))
        p.drawText(QRectF(x, r.top(), r.right() - x, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        p.end()


class TextAction(QAbstractButton):
    """A quiet word with a small glyph ("Why?", "Not right", "Copy", "Reply"): muted, brighter under the
    pointer. The mockups' ``.acts``."""

    def __init__(self, t: Theme, text: str, glyph: str | None = None, *, ink: QColor | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.ink = t, glyph, ink
        self.setText(text)
        self.setFont(variable_font(12.5, "Regular"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.style = "ghost"
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: D102
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self.text()) + (17 if self.glyph else 0) + 2, 22)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect())
        on = self.style != "ghost"
        ink = QColor(self.ink or (self.t.text_2 if (self.underMouse() or on) else self.t.text_3))
        if not self.isEnabled():
            ink.setAlphaF(0.6)
        p.setPen(ink)
        x = r.left()
        if self.glyph:
            f = font("icon_small")
            f.setPixelSize(11)
            p.setFont(f)
            p.drawText(QRectF(x, r.top() + 1, 12, r.height()), Qt.AlignmentFlag.AlignCenter,
                       ICON.get(self.glyph, self.glyph))
            x += 17
        p.setFont(self.font())
        p.drawText(QRectF(x, r.top(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


#: What each old tag says, as plain secondary text after an item's source ("" = nothing to add: the
#: section or the source line already says it). No badges (the user reads them as generic AI UI).
TAG_WORDS = {"joke": "Said as a joke", "story": "", "you": "", "unsure": "Yuki isn't sure",
             "unclear": "May be misheard", "call": "", "live": "", "updating": "Updating"}


def tag_words(tags: Sequence[str]) -> str:
    """The plain words for an item's tags, as sentences (no chips, no dots)."""
    return ". ".join(w for w in (TAG_WORDS.get(tag, "") for tag in tags) if w)


def join_source(*parts: str) -> str:
    """A source line and what its tags add, as short sentences: "Seen in Discord, 25 Sep. Said as a joke"."""
    return ". ".join(p for p in parts if p)


# -- form controls ---------------------------------------------------------------------


class Toggle(QAbstractButton):
    """The switch: a 40 x 22 track; on, a light track and a dark thumb; off, a key-face track and a muted
    thumb."""

    def __init__(self, t: Theme, on: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        on = self.isChecked()
        t = self.t
        paint_surface(p, r, r.height() / 2, t.level("card"), fill=QColor(t.primary if on else t.subtle),
                      border=QColor(t.primary if on else t.border))
        thumb = QColor(t.bg if on else t.text_3)
        if not self.isEnabled():
            thumb.setAlphaF(0.5)
        d = 14.0
        x = r.right() - d - 4 if on else r.left() + 4
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(thumb)
        p.drawEllipse(QRectF(x, r.top() + (r.height() - d) / 2, d, d))
        p.end()


class Segmented(QWidget):
    """Mutually exclusive options on a glass track; the chosen one is a light pill."""

    changed = Signal(str)

    def __init__(self, t: Theme, options: Sequence[str], current: str, *, small: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.options, self.current = t, list(options), current
        self.setFont(variable_font(12.5 if small else 13, "Regular"))
        fm = QFontMetrics(self.font())
        self._widths = [fm.horizontalAdvance(o) + 24 for o in self.options]
        self.setFixedSize(sum(self._widths) + 4, 28 if small else 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: object) -> None:  # noqa: D102
        x = event.position().x() - 2
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
        paint_surface(p, r, r.height() / 2, t.level("display"))
        x = r.left() + 2
        for opt, w in zip(self.options, self._widths):
            cell = QRectF(x, r.top() + 2, w, r.height() - 4)
            on = opt == self.current
            if on:
                paint_surface(p, cell, cell.height() / 2, t.level("primary"))
            f = variable_font(self.font().pixelSize(), "Semibold" if on else "Regular")
            p.setFont(f)
            p.setPen(t.primary_fg if on else t.text_3)
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, opt)
            x += w
        p.end()


class Dropdown(QAbstractButton):
    """A select: the chosen value and a chevron in a glass box; a click opens the choices.

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
        self.setFont(variable_font(13.5, "Regular"))
        self.setFixedSize(width, CONTROL_H["input"])
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
        paint_surface(p, r, RADIUS["field"], t.level("input"),
                      border=QColor(t.faint) if self.underMouse() else None)
        x = r.left() + 12
        if self.glyph:
            p.setFont(font("icon_small"))
            p.setPen(t.text_3)
            p.drawText(QRectF(x, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
            x += 24
        p.setFont(self.font())
        p.setPen(t.text_2)
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.right() - 32 - x))
        p.drawText(QRectF(x, r.top(), r.right() - 32 - x, r.height()), Qt.AlignmentFlag.AlignVCenter, text)
        f = font("icon_small")
        f.setPixelSize(9)
        p.setFont(f)
        p.setPen(t.text_3)
        p.drawText(QRectF(r.right() - 28, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, ICON["down"])
        p.end()


class _Field(QWidget):
    """The box around a line edit (a glass pill, or a rounded box), brighter while it holds the keyboard."""

    def __init__(self, t: Theme, edit: QWidget, parent: QWidget | None = None, *, pill: bool = True) -> None:
        super().__init__(parent)
        self.t, self.edit, self.pill = t, edit, pill
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(14 if pill else 12, 0, 12, 0)
        row.addWidget(edit)
        edit.installEventFilter(self)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return False

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        t = self.t
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        focused = self.edit.hasFocus()
        paint_surface(p, r, RADIUS["field"], t.level("input"), border=QColor(t.text_3) if focused else None)
        p.end()


class Input(QWidget):
    """A text field (a glass pill with a line, brighter when focused), with an optional button beside it."""

    submitted = Signal(str)

    def __init__(self, t: Theme, placeholder: str, *, action: str | None = None, action_glyph: str | None = None,
                 text: str = "", height: int = CONTROL_H["input"], pill: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(height + 2)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["s"])
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFont(variable_font(13.5, "Regular"))
        self.edit.setFrame(False)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; color: {css(t.text)}; border: none;"
            f" selection-background-color: {css(t.accent_soft)}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, t.faint)
        self.edit.setPalette(pal)
        self.field = _Field(t, self.edit, self, pill=pill)
        row.addWidget(self.field, 1)
        self.button: Button | None = None
        if action or action_glyph:
            self.button = Button(t, action or "", glyph=action_glyph, style="default",
                                 size="regular" if height >= 34 else "small")
            wrap = QVBoxLayout()
            wrap.setContentsMargins(0, 1, 0, 1)
            wrap.addWidget(self.button)
            row.addLayout(wrap)
            self.button.clicked.connect(lambda: self.submitted.emit(self.edit.text()))
        self.edit.returnPressed.connect(lambda: self.submitted.emit(self.edit.text()))


class Avatar(QWidget):
    """A person's initial on a small key face (a rounded square, the mockup's ``.av``)."""

    def __init__(self, t: Theme, name: str, size: int = 30, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.name = t, name
        self.setFixedSize(size, size + 2)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 2, self.width(), self.width())
        from yuki.ui.window.theme import corner

        paint_surface(p, r, corner(r.width(), r.height()), self.t.level("key"))
        p.setPen(self.t.text_2)
        p.setFont(variable_font(r.height() * 0.43, "Regular", display=True))
        initial = next((ch for ch in self.name if ch.isalnum()), "?").upper()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, initial)
        p.end()


# -- Field Unit: keys, LEDs, displays -------------------------------------------------------------


class Led(QWidget):
    """A status LED. ``state``: ``on`` (the accent: live) | ``ok`` (secondary ink: fine) | ``off``."""

    def __init__(self, t: Theme, state: str = "off", size: int = 7, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.state = t, state
        self.setFixedSize(size + 4, max(size + 4, 14))

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        c = {"on": self.t.accent, "ok": self.t.text_2}.get(self.state, self.t.led_off)
        paint_led(p, QPointF(self.width() / 2, self.height() / 2), c, self.width() - 4)
        p.end()


class Key(QAbstractButton):
    """A hardware key (Field Unit): a glyph over a short word on a key face with a 2 px bottom edge, and a
    status LED in its top-left corner. ``checked`` lights the face and the LED (the selected page); ``led``
    overrides the LED: ``on`` (the accent), ``ok`` (secondary ink), ``off``, or ``None`` for no LED."""

    def __init__(self, t: Theme, glyph: str, text: str, *, size: tuple[int, int] = (60, 60),
                 led: str | None = "off", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.led = t, glyph, led
        self.setText(text)
        self.setFixedSize(*size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def set_led(self, led: str | None) -> None:
        if led != self.led:
            self.led = led
            self.update()

    def led_color(self) -> QColor | None:
        if self.led is None:
            return None
        if self.isChecked() or self.led == "on":
            return self.t.accent
        return self.t.text_2 if self.led == "ok" else self.t.led_off

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        on = self.isChecked()
        paint_key(p, r, t, on=on, hover=self.underMouse() and self.isEnabled(), down=self.isDown())
        led = self.led_color()
        if led is not None:
            paint_led(p, QPointF(r.left() + 8.5, r.top() + 8.5), led, 5.0)
        ink = QColor(t.text if on else t.text_3)
        if self.underMouse() and not on and self.isEnabled():
            ink = QColor(t.text_2)
        if not self.isEnabled():
            ink = QColor(t.faint)
        drop = 1.0 if self.isDown() else 0.0
        p.setPen(ink)
        if self.text():
            p.setFont(font("icon"))
            p.drawText(QRectF(r.left(), r.top() + drop + r.height() * 0.14, r.width(), 20),
                       Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
            p.setFont(variable_font(11, "Regular"))
            p.drawText(QRectF(r.left(), r.top() + drop + r.height() * 0.14 + 22, r.width(), 16),
                       Qt.AlignmentFlag.AlignCenter, self.text())
        else:
            p.setFont(font("icon"))
            p.drawText(r.adjusted(0, drop, 0, drop - 1), Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            paint_ring(p, r.adjusted(1, 1, -1, -1), 8, t.ring, 1.5)
        p.end()


class NavItem(Key):
    """A sidebar key: the page's glyph and word; the current page lit, its LED in the accent. ``badge``:
    something waits on that page (the LED shows it in secondary ink; the tooltip says what)."""

    def __init__(self, t: Theme, glyph: str, text: str, *, badge: str = "", parent: QWidget | None = None) -> None:
        super().__init__(t, glyph, text, size=(60, 60), led="off", parent=parent)
        self.setCheckable(True)
        self.badge = ""
        self.set_badge(badge)

    def set_badge(self, badge: str) -> None:
        self.badge = badge
        self.led = "ok" if badge else "off"
        self.update()


class DotDisplay(QWidget):
    """A dot-matrix display (Field Unit): a glass inset with a Doto value and its unit in small words in the
    bottom-right corner. ``ink``: ``accent`` | ``text`` | ``faint`` (a zero). ``top``: the value's offset
    from the top (the big display sits higher, the mockup's ``margin-top``)."""

    def __init__(self, t: Theme, value: str, unit: str = "", *, px: int = 44, ink: str = "text",
                 height: int = 112, top: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.value, self.unit, self.px, self.ink = t, value, unit, px, ink
        self.top = top
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(height)
        self.setMinimumWidth(min(150, int(dots_width(value, px)) + 36))   # narrower: the digits shrink to fit
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if unit:
            self.setAccessibleName(f"{value} {unit}")

    def set_value(self, value: str) -> None:
        if value != self.value:
            self.value = value
            self.update()

    def set_link(self, on_click: Callable[[], object], tip: str = "") -> None:
        """Make the display a link: it lifts under the pointer, drops when pressed, and ``on_click`` runs."""
        self._link = on_click
        self._hot = self._down = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            self.setToolTip(tip)

    def enterEvent(self, e: object) -> None:  # noqa: D102
        if getattr(self, "_link", None):
            self._hot = True
            self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        if getattr(self, "_link", None):
            self._hot = self._down = False
            self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e: object) -> None:  # noqa: D102
        if getattr(self, "_link", None) and e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e: object) -> None:  # noqa: D102
        link = getattr(self, "_link", None)
        if link and self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                link()

    def color(self) -> QColor:
        return {"accent": self.t.accent, "faint": self.t.faint, "text_2": self.t.text_2}.get(self.ink, self.t.text)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        hot, down = getattr(self, "_hot", False), getattr(self, "_down", False)
        if down:
            r = r.adjusted(0, 1, 0, 0)
        paint_surface(p, r, RADIUS["display"], self.t.level("display"),
                      fill=QColor(self.t.panel) if hot and not down else None,
                      border=QColor(self.t.faint) if hot else None)
        if getattr(self, "_link", None):   # a link: a small arrow in the corner, brighter under the pointer
            f = font("icon_small")
            f.setPixelSize(10)
            p.setFont(f)
            p.setPen(self.t.text_2 if hot else self.t.faint)
            p.drawText(QRectF(r.right() - 30, r.top() + 10, 18, 16), Qt.AlignmentFlag.AlignCenter, ICON["next"])
        top = self.top if self.top is not None else (18 if self.px >= 60 else 26 if self.height() >= 108 else 16)
        px = float(self.px)
        need = dots_width(self.value, px)
        if need > r.width() - 32:
            px = max(12.0, px * (r.width() - 32) / need)
        paint_dots(p, QRectF(r.left() + 16, r.top() + top, r.width() - 32, px), self.value, px, self.color())
        if self.unit:
            p.setFont(variable_font(12.5, "Regular"))
            p.setPen(self.t.text_3)
            p.drawText(r.adjusted(14, 0, -14, -9), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                       self.unit)
        p.end()


class TimeDisplay(QAbstractButton):
    """A time on a small dot-matrix display with up/down arrows (quiet hours). The arrows step by
    ``step_min``; a click on the digits opens the full list. ``changed(hh:mm)``."""

    changed = Signal(str)

    def __init__(self, t: Theme, value: str, *, times: Sequence[str], step_min: int = 30,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.value, self.times, self.step = t, value, list(times), step_min
        self.setFixedSize(108, 42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to pick a time, or use the arrows")

    def _shift(self, n: int) -> None:
        if self.value in self.times:
            i = (self.times.index(self.value) + n) % len(self.times)
        else:
            i = 0
        self.set_value(self.times[i], emit=True)

    def set_value(self, value: str, *, emit: bool = False) -> None:
        self.value = value
        self.update()
        if emit:
            self.changed.emit(value)

    def text(self) -> str:  # noqa: D102 - the value, like a Dropdown's text
        return self.value

    def mouseReleaseEvent(self, e: object) -> None:  # noqa: D102
        x, y = e.position().x(), e.position().y()
        if x >= self.width() - 26:
            self._shift(-1 if y < self.height() / 2 else 1)
            return
        popup = ListPopup(self.t, [(v, v) for v in self.times], self.value, width=self.width())
        popup.picked.connect(lambda label, _v: self.set_value(str(label), emit=True))
        popup.open_below(self)
        self._popup = popup

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        from yuki.ui.window.theme import corner

        paint_surface(p, r, corner(r.width(), r.height(), RADIUS["display"]), self.t.level("display"))
        paint_dots(p, QRectF(r.left() + 14, r.top(), r.width() - 40, r.height()), self.value, 22, self.t.accent)
        f = font("icon_small")
        f.setPixelSize(8)
        p.setFont(f)
        p.setPen(self.t.text_3)
        p.drawText(QRectF(r.right() - 24, r.center().y() - 12, 14, 11), Qt.AlignmentFlag.AlignCenter, ICON["up"])
        p.drawText(QRectF(r.right() - 24, r.center().y() + 1, 14, 11), Qt.AlignmentFlag.AlignCenter, ICON["down"])
        p.end()


class StatTile(DotDisplay):
    """Kept for older callers: a figure as a dot-matrix display (label as its unit)."""

    def __init__(self, t: Theme, label: str, value: str, note: str = "", *, glyph: str | None = None,
                 parent: QWidget | None = None) -> None:
        del note, glyph
        super().__init__(t, value, label, px=44, parent=parent)


class Divider(QWidget):
    """A 1 px rule in the quieter line colour."""

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setFixedHeight(1)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.fillRect(self.rect(), self.t.line2)
        p.end()


class ItemRow(QWidget):
    """One thing Yuki believes, with where it came from and the two ways to act on it.

    Layout: an optional leading widget (avatar), the title, the text, a source line
    ("Seen in Discord, yesterday"), then two quiet words: **Why?** (the evidence folds
    open under it) and **Not right** (a composer folds open: the user says what is
    true in their own words, or "It's just wrong, forget it"). The folds are built
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
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(SPACE["m"])
        if leading is not None:
            outer.addWidget(leading, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        outer.addLayout(col, 1)
        self._col = col
        if title:
            col.addWidget(text_label(t, title, title_role, wrap=True))
        self._tag_words = tag_words(tags)
        self.text_label = text_label(t, text, "body", "text_2", wrap=True, selectable=True)
        col.addWidget(self.text_label)
        self.was_label: QLabel | None = None
        self.source_label = text_label(t, join_source(source, self._tag_words), "micro", "text_3", wrap=True)
        col.addSpacing(2)
        col.addWidget(self.source_label)
        foot = QHBoxLayout()
        foot.setSpacing(16)
        foot.setContentsMargins(0, 4, 0, 0)
        self.why_pill: TextAction | None = None
        self.wrong_pill: TextAction | None = None
        if actions:
            self.why_pill = TextAction(t, "Why?", "info")
            self.why_pill.clicked.connect(self.toggle_evidence)
            foot.addWidget(self.why_pill)
            if not evidence:
                self.why_pill.hide()
            self.wrong_pill = TextAction(t, "Not right", "edit")
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
            self.text_label.setStyleSheet(f"color: {css(t.text)}; background: transparent;")
            if self.was_label is None:
                self.was_label = text_label(t, "", "caption", "text_3", wrap=True)
                self._col.insertWidget(self._col.indexOf(self.text_label) + 1, self.was_label)
            self.was_label.setText(f"Yuki had: {self._text}")
            note = "Yuki is putting this right" if not late else "Yuki hasn't applied this yet, it will"
            self.source_label.setText(f"{state}. You corrected this at {when}. {note}")
        elif kind == "forget":
            self.text_label.setStyleSheet(f"color: {css(t.text_3)}; background: transparent;")
            self.source_label.setText(f"{state}. You said this is wrong at {when}")
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
            ev = Card(t, level="muted", padding=SPACE["m"], parent=self)
            ev.body.setContentsMargins(14, 12, 14, 12)
            ev.body.setSpacing(6)
            ev.body.addWidget(text_label(t, "Why Yuki thinks this", "caption_strong", "text_2"))
            for when, where, words in self._evidence_rows:   # each entry: when and where, then its words
                line = QHBoxLayout()
                line.setSpacing(SPACE["s"])
                stamp = text_label(t, f"{when}, {where}", "micro", "text_3", wrap=True)
                stamp.setFixedWidth(118)
                line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
                line.addWidget(text_label(t, words, "micro", "text_2", wrap=True, selectable=True), 1)
                ev.body.addLayout(line)
            self._ev_box = ev
            self._col.insertWidget(self._col.count() if self._composer is None else self._col.count() - 1, ev)
            ev.setVisible(False)
        show = not self._ev_box.isVisible()
        self._ev_box.setVisible(show)
        if self.why_pill is not None:
            self.why_pill.setText("Hide why" if show else "Why?")
            self.why_pill.style = "on" if show else "ghost"
            self.why_pill.updateGeometry()
            self.why_pill.update()
        if show:
            self.why.emit(self.ref)

    def toggle_composer(self) -> None:
        t = self.t
        if self._composer is None:
            box = Card(t, level="muted", padding=14, parent=self)
            box.body.setSpacing(SPACE["s"] + 2)
            box.body.addWidget(text_label(t, "Tell Yuki what's right, in your own words. It fixes this and "
                                             "remembers it came from you.", "caption", "text_2", wrap=True))
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
            self.wrong_pill.style = "on" if show else "ghost"
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
    """A settings line: a small muted glyph, a label with a hint under it, the control on the right; a
    quiet rule above every row but the first of its module."""

    def __init__(self, t: Theme, glyph: str, label: str, hint: str, control: QWidget | None,
                 parent: QWidget | None = None, *, rule: bool | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.rule = rule
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.grid = QVBoxLayout(self)
        self.grid.setContentsMargins(0, 12, 0, 12)
        self.grid.setSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        self.grid.addLayout(row)
        ic = icon_label(t, glyph, t.text_3, "icon_small")
        f = ic.font()
        f.setPixelSize(14)
        ic.setFont(f)
        ic.setFixedSize(24, 20)
        ic.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(ic)
        left.addStretch(1)
        row.addLayout(left)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(text_label(t, label, "body_strong", wrap=True))
        if hint:
            col.addWidget(text_label(t, hint, "micro", "text_3", wrap=True))
        col.addStretch(1)
        row.addLayout(col, 1)
        if control is not None:
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignTop)

    def add_below(self, widget: QWidget) -> None:
        """A second line under the label (the quiet-hours displays)."""
        wrap = QHBoxLayout()
        wrap.setContentsMargins(36, 0, 0, 0)
        wrap.addWidget(widget, 1)
        self.grid.addLayout(wrap)

    def _first(self) -> bool:
        if self.rule is not None:
            return not self.rule
        par = self.parentWidget()
        lay = par.layout() if par is not None else None
        if lay is None:
            return True
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, SettingRow) and not w.isHidden():
                return w is self
        return True

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        if not self._first():
            p = QPainter(self)
            p.fillRect(QRectF(0, 0, self.width(), 1), self.t.line2)
            p.end()


def section_title(t: Theme, text: str, note: str = "") -> QWidget:
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, SPACE["s"], 0, 0)
    row.addWidget(text_label(t, text, "title"))
    row.addStretch(1)
    if note:
        row.addWidget(text_label(t, note, "caption", "text_3"))
    return w


def page_header(t: Theme, title: str, subtitle: str, right: Sequence[QWidget] = (), *, muted: str = "",
                right_top: int = 14) -> QWidget:
    """A page's title (36 px Segoe UI Variable Display Light; ``muted``: a second part in muted ink, "Sunday
    27 September") and its lede (15 px, secondary ink, wrapping within 720 px)."""
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACE["xl"])
    col = QVBoxLayout()
    col.setSpacing(8)
    head = QLabel(w)
    head.setFont(font("large_title"))
    head.setTextFormat(Qt.TextFormat.RichText if muted else Qt.TextFormat.PlainText)
    if muted:
        import html as _html

        head.setText(f"{_html.escape(title)} <span style='color:{t.text_3.name()}'>{_html.escape(muted)}</span>")
    else:
        head.setText(title)
    head.setStyleSheet(f"color: {css(t.text)}; background: transparent;")
    w.title_label = head  # type: ignore[attr-defined]
    col.addWidget(head)
    lede = text_label(t, subtitle, "lede", "text_2", wrap=True)
    lede.setMaximumWidth(720)
    lrow = QHBoxLayout()   # the lede keeps to the left, at most 720 px wide
    lrow.setContentsMargins(0, 0, 0, 0)
    lrow.addWidget(lede, 1)
    lrow.addStretch(0)
    col.addLayout(lrow)
    row.addLayout(col, 1)
    if right:
        side = QHBoxLayout()
        side.setSpacing(10)
        side.setContentsMargins(0, right_top, 0, 0)
        for r in right:
            side.addWidget(r, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(side)
    return w


# -- floating: toast, menu, tooltip --------------------------------------------------------------------


class Toast(QWidget):
    """A short note at the bottom of the window ("Told Yuki. It will fix this."): a floating panel."""

    #: Room around the card for its shadow (logical px).
    MARGIN = 12
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
        inner = min(self.MAX_W, par.width() - 120) - 36
        br = fm.boundingRect(QRect(0, 0, max(120, inner), 400), int(Qt.TextFlag.TextWordWrap), text)
        m = self.MARGIN
        w, h = br.width() + 36 + 2 * m, max(44, br.height() + 24) + 2 * m
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
        paint_shadow(p, r, RADIUS["floating"], blur=9, dy=4, color=QColor(0, 0, 0, 130))
        paint_surface(p, r, RADIUS["floating"], self.t.level("popover"))
        p.setPen(self.t.danger if self._tone in ("error",) else self.t.text)
        p.setFont(font("body"))
        p.drawText(r.adjusted(18, 12, -18, -12), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                   self._text)
        p.end()


def menu_css(t: Theme) -> str:
    """A :class:`Menu`'s items (its surface is painted): Horizon's tray menu spacing and hover."""
    return (f"QMenu {{ background: transparent; color: {css(t.menu_ink)}; border: none; padding: 8px; }}"
            "QMenu::item { padding: 7px 24px 7px 34px; border-radius: 10px; background: transparent; }"
            f"QMenu::item:selected {{ background: {css(t.bubble)}; color: {css(t.text)}; }}"
            f"QMenu::item:disabled {{ color: {css(t.text_3)}; }}"
            f"QMenu::separator {{ height: 1px; background: {css(t.float_sep)}; margin: 5px 6px; }}"
            "QMenu::indicator { width: 14px; height: 14px; left: 12px; }"
            "QMenu::right-arrow { width: 10px; height: 10px; right: 10px; }")


def _paint_menu(widget: QWidget, t: Theme) -> None:
    p = QPainter(widget)
    paint_surface(p, QRectF(widget.rect()), RADIUS["floating"], t.level("popover"))
    menu = widget if isinstance(widget, QMenu) else None
    led = getattr(widget, "header_led", None)
    if menu is not None and led is not None:   # the status line at the top: its LED (lit while live)
        acts = [a for a in menu.actions() if a.isVisible()]
        if acts and not acts[0].isEnabled() and not acts[0].isSeparator():
            geo = menu.actionGeometry(acts[0])
            live = bool(led()) if callable(led) else bool(led)
            paint_led(p, QPointF(geo.left() + 19, geo.center().y() + 0.5), t.accent if live else t.led_off, 7.0)
    p.end()


class Menu(QMenu):
    """A floating menu (Horizon's tray menu): the panel colour, 14 px corners, the hovered item on a subtle
    fill. ``header_led``: a callable (or bool) that lights an LED beside a disabled first line (the tray's
    "Remembering today" status)."""

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.header_led = None
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFont(variable_font(13.5, "Regular"))
        self.setStyleSheet(menu_css(t))

    def paintEvent(self, e: object) -> None:  # noqa: D102
        _paint_menu(self, self.t)
        super().paintEvent(e)


class _MenuPainter(QObject):
    """Paints the floating surface under a plain QMenu's items (submenus made with ``addMenu``)."""

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
        menu.setFont(variable_font(13.5, "Regular"))
        menu.setProperty("yuki_menu", True)
    menu.setStyleSheet(menu_css(theme()))


class Tip(QWidget):
    """The tooltip: a small floating panel (never focused), wrapping at 320 px."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowTransparentForInput)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._text = ""

    def show_tip(self, text: str, gpos: object, host: QWidget | None = None) -> None:
        from PySide6.QtGui import QGuiApplication

        del host
        self._text = text
        fm = QFontMetrics(font("micro"))
        br = fm.boundingRect(QRect(0, 0, 320, 400), int(Qt.TextFlag.TextWordWrap), text)
        self.resize(br.width() + 26, br.height() + 16)
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
        p.setPen(t.text_2)
        p.setFont(font("micro"))
        p.drawText(r.adjusted(13, 8, -13, -8), int(Qt.TextFlag.TextWordWrap), self._text)
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
    """A skeleton module: a field-coloured panel with quiet blocks that pulse gently while a section loads. Its timer
    runs only while it is shown; it is deleted when the real content takes its place."""

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
        paint_surface(p, r, RADIUS["panel"], t.level("card"))
        block = QColor(t.muted)
        phase = (self._clock.elapsed() % 2000) / 2000.0
        block.setAlphaF(0.7 + 0.3 * math.cos(phase * 2 * math.pi))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(block)
        p.drawPath(rounded(QRectF(r.left() + 22, r.top() + 22, r.width() * 0.34, 14), 4))
        y = r.top() + 54
        for i in range(self.rows):
            if y + 12 > r.bottom() - 16:
                break
            p.drawPath(rounded(QRectF(r.left() + 22, y, (r.width() - 44) * (0.92 - 0.17 * (i % 3)), 10), 4))
            y += 24
        p.end()


Callback = Callable[..., object]


# -- a compact scrolling picker (long lists: the quiet-hour times) ----------------------------------------


class _ListRow(QAbstractButton):
    """One choice in a :class:`ListPopup`: its words, a subtle fill under the pointer and on the current one."""

    def __init__(self, t: Theme, text: str, current: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.current = t, current
        self.setText(text)
        self.setFont(variable_font(13.5, "Semibold" if current else "Regular"))
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
            paint_surface(p, r, 10, self.t.level("card"), fill=QColor(self.t.bubble), border=QColor(0, 0, 0, 0))
        p.setPen(self.t.text if self.current or self.underMouse() else self.t.menu_ink)
        p.setFont(self.font())
        p.drawText(r.adjusted(12, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())
        if self.current:
            p.setFont(font("icon_small"))
            p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       ICON["check"])
        p.end()


class ListPopup(QWidget):
    """A small floating panel with a scrolling list, opened under a control and scrolled to its current value.
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
        outer.setContentsMargins(6, 6, 6, 6)
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
        self.resize(max(width, 110), shown * self.ROW_H + 12)

    def open_below(self, anchor: QWidget) -> None:
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
        paint_surface(p, QRectF(self.rect()), RADIUS["card"], self.t.level("popover"))
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
    """Keycaps showing a shortcut; click them and press the keys you want. A key with modifiers
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
        self.setFixedSize(width, 36)
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

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(self.width(), 36)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect())
        if self.listening:
            live = [combo_words(m) for m in _MOD_ORDER if m in self._mods]
            if not live:
                box = r.adjusted(1, 3, -1, -3)
                paint_surface(p, box, RADIUS["field"], t.level("input"), border=QColor(t.text_3))
                p.setPen(t.text_2)
                p.setFont(variable_font(13, "Regular"))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, "Press your shortcut…")
                p.end()
                return
            paint_keycaps(p, r, t, live + ["…"], align=Qt.AlignmentFlag.AlignRight)
        else:
            words = combo_words(self.combo)
            paint_keycaps(p, r, t, words.split(" + ") if words else ["Not set"], align=Qt.AlignmentFlag.AlignRight,
                          hover=self.underMouse())
        p.end()


def _keycap_font():
    return variable_font(12.5, "Regular")


def keycaps_width(keys: Sequence[str], *, gap: int = 6) -> int:
    """How wide keys are as keycaps joined by "+"."""
    fm = QFontMetrics(_keycap_font())
    plus = fm.horizontalAdvance("+") + 2 * gap
    return sum(fm.horizontalAdvance(k) + 24 for k in keys) + plus * max(0, len(keys) - 1)


def paint_keycaps(p: QPainter, rect: QRectF, t: Theme, keys: Sequence[str], *,
                  align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft, hover: bool = False, gap: int = 6,
                  height: float = 32.0, plus: bool = True) -> float:
    """Keycaps (Field Unit): each key a small face with a 3 px bottom edge, "+" between them."""
    f = _keycap_font()
    fm = QFontMetrics(f)
    total = keycaps_width(keys, gap=gap) if plus else sum(fm.horizontalAdvance(k) + 24 for k in keys) + 4 * (len(keys) - 1)
    x = rect.right() - total if align & Qt.AlignmentFlag.AlignRight else rect.left()
    y = rect.center().y() - height / 2
    for i, k in enumerate(keys):
        if i:
            if plus:
                p.setPen(t.text_3)
                p.setFont(f)
                w = fm.horizontalAdvance("+") + 2 * gap
                p.drawText(QRectF(x, y, w, height - 3), Qt.AlignmentFlag.AlignCenter, "+")
                x += w
            else:
                x += 4
        w = fm.horizontalAdvance(k) + 24
        paint_key(p, QRectF(x, y, w, height), t, hover=hover, radius=height / 2, bottom=3.0)   # a capsule keycap
        p.setPen(t.text)
        p.setFont(f)
        p.drawText(QRectF(x, y, w, height - 3), Qt.AlignmentFlag.AlignCenter, k)
        x += w
    return total


class Keycaps(QWidget):
    """Keycaps for a fixed shortcut ("Ctrl + Alt + Y"), display only."""

    def __init__(self, t: Theme, keys: Sequence[str], *, height: int = 32, plus: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.keys, self._h, self.plus = t, list(keys), height, plus
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        fm = QFontMetrics(_keycap_font())
        w = keycaps_width(self.keys) if plus else sum(fm.horizontalAdvance(k) + 24 for k in self.keys) + 4 * (len(self.keys) - 1)
        self.setFixedSize(int(w) + 2, height + 2)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_keycaps(p, QRectF(self.rect()), self.t, self.keys, height=self._h, plus=self.plus)
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
                 min_col: int = 360, weights: Sequence[float] | None = None) -> None:
        super().__init__(parent)
        self._items: list = []
        self._pref: dict[int, int] = {}
        self.columns, self.min_col = columns, min_col
        self.weights = list(weights or [1.0] * columns)
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
        room = max(1, w - (n - 1) * sp)
        weights = self.weights[:n] if n > 1 else [1.0]
        widths = [max(1, int(room * wt / sum(weights))) for wt in weights]
        lefts = [sum(widths[:i]) + i * sp for i in range(n)]
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
            col_w = widths[c]
            if w is not None:
                h = w.heightForWidth(col_w) if w.hasHeightForWidth() else w.sizeHint().height()
            else:
                h = item.heightForWidth(col_w) if item.hasHeightForWidth() else item.sizeHint().height()
            if move:
                geo = QRect(rect.x() + lefts[c], rect.y() + heights[c], col_w, h)
                if w is not None:
                    if w.geometry() != geo:
                        w.setGeometry(geo)
                else:
                    item.setGeometry(geo)
            heights[c] += h + sp
        return max(0, max(heights) - sp)


# -- round 2: dashboard cards and rows that fold open ------------------------------------------------------


class NavCard(QAbstractButton):
    """A dashboard card that leads somewhere (Today's summaries of Me, Yuki and Status): a quiet title, a
    dot-matrix figure, one line, optional initials, and an arrow; it lifts under the pointer and drops when
    pressed. Painted in one pass (no child widgets), so a row of them costs almost nothing."""

    def __init__(self, t: Theme, title: str, value: str, line: str = "", *, initials: Sequence[str] = (),
                 lit: bool = False, height: int = 96, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.title, self.value, self.line = t, title, value, line
        self.initials, self.lit = list(initials)[:5], lit
        self.setFixedHeight(height)
        self.setMinimumWidth(128)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{title}: {value}. {line}")

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(210, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return QSize(128, self.height())

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
        hot, down = self.underMouse(), self.isDown()
        r = QRectF(self.rect()).adjusted(0, 1 if down else 0, 0, 0)
        fill = QColor(t.key if hot and not down else t.panel if not down else t.glass)
        paint_surface(p, r, RADIUS["panel"], t.level("card"), fill=fill,
                      border=QColor(t.faint) if hot else None)
        if self.hasFocus():
            paint_ring(p, r.adjusted(1, 1, -1, -1), RADIUS["panel"], t.ring, 1.5)
        x, w = r.left() + 18, r.width() - 36
        p.setFont(variable_font(12.5, "Regular"))
        p.setPen(t.text_3)
        p.drawText(QRectF(x, r.top() + 14, w - 20, 18), Qt.AlignmentFlag.AlignVCenter, self.title)
        f = font("icon_small")
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(t.text_2 if hot else t.faint)
        p.drawText(QRectF(r.right() - 32, r.top() + 14, 16, 18), Qt.AlignmentFlag.AlignCenter, ICON["next"])
        vw = paint_dots(p, QRectF(x, r.top() + 36, w, 30), self.value, 28, t.accent if self.lit else t.text)
        cx = x + vw + 16
        for i, ini in enumerate(self.initials):   # people: their initials, overlapping keys
            kr = QRectF(cx + i * 20, r.top() + 38, 26, 26)
            if kr.right() > r.right() - 16:
                break
            paint_surface(p, kr, 9, t.level("key"), fill=QColor(t.key_on), border=QColor(t.bg))
            p.setFont(variable_font(11.5, "Regular", display=True))
            p.setPen(t.text_2)
            p.drawText(kr, Qt.AlignmentFlag.AlignCenter, ini)
        if self.line:
            lf = variable_font(12.5, "Regular")
            p.setFont(lf)
            p.setPen(t.text_2)
            fm = QFontMetrics(lf)
            p.drawText(QRectF(x, r.bottom() - 28, w, 18), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(self.line, Qt.TextElideMode.ElideRight, int(w)))
        p.end()


class _FoldHead(QAbstractButton):
    """A fold row's head: optional initial or LED, the short title, one quiet line, the date on the right and a
    chevron; the row lights under the pointer and drops when pressed. Painted (no labels)."""

    H_ONE, H_TWO = 38, 52

    def __init__(self, t: Theme, title: str, line: str, meta: str, *, initial: str = "", led: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.title, self.line, self.meta = t, title, line, meta
        self.initial, self.led = initial, led
        self.open = False
        #: True when the last paint had to shorten the title or the line to fit (the row then opens to them in
        #: full: FoldRow's ``full``).
        self.cut = False
        self.setCheckable(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.H_TWO if (title and line) else self.H_ONE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(200)
        self.setToolTip(line if line and len(line) > 60 else "")

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(320, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return QSize(200, self.height())

    def cut_changed(self) -> None:
        """``cut`` changed (the row sets this hook)."""

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
        r = QRectF(self.rect())
        hot, down = self.underMouse(), self.isDown()
        if hot or down or self.open:
            paint_surface(p, r.adjusted(-8, 0, 8, 0), 12, t.level("card"),
                          fill=QColor(t.glass if down else t.key if hot else t.panel), border=QColor(0, 0, 0, 0))
        x = r.left()
        if self.initial:
            kr = QRectF(x, r.center().y() - 15, 30, 30)
            paint_surface(p, kr, 10, t.level("key"))
            p.setFont(variable_font(13, "Regular", display=True))
            p.setPen(t.text_2)
            p.drawText(kr, Qt.AlignmentFlag.AlignCenter, self.initial)
            x += 42
        elif self.led is not None:
            paint_led(p, QPointF(x + 4, r.center().y() - (7 if self.line and self.title else 0)),
                      {"on": t.accent, "ok": t.text_2}.get(self.led, t.led_off), 7)
            x += 18
        meta_w = 0
        mf = variable_font(12, "Regular")
        if self.meta:
            meta_w = QFontMetrics(mf).horizontalAdvance(self.meta) + 14
        chev = 22
        w = r.right() - chev - meta_w - x
        tf = variable_font(14, "Semibold" if self.line else "Regular")
        lf = variable_font(13, "Regular")
        cut = False
        if self.title and self.line:
            p.setFont(tf)
            p.setPen(t.text)
            shown = QFontMetrics(tf).elidedText(self.title, Qt.TextElideMode.ElideRight, int(w))
            cut |= shown != self.title
            p.drawText(QRectF(x, r.top() + 6, w, 20), Qt.AlignmentFlag.AlignVCenter, shown)
            p.setFont(lf)
            p.setPen(t.text_2)
            shown = QFontMetrics(lf).elidedText(self.line, Qt.TextElideMode.ElideRight, int(w))
            cut |= shown != self.line
            p.drawText(QRectF(x, r.top() + 26, w, 20), Qt.AlignmentFlag.AlignVCenter, shown)
        else:
            words = self.title or self.line
            f = tf if self.title else lf
            p.setFont(f)
            p.setPen(t.text if self.title else t.text_2)
            shown = QFontMetrics(f).elidedText(words, Qt.TextElideMode.ElideRight, int(w))
            cut |= shown != words
            p.drawText(QRectF(x, r.top(), w, r.height()), Qt.AlignmentFlag.AlignVCenter, shown)
        if cut != self.cut:
            self.cut = cut
            self.cut_changed()
        if self.meta:
            p.setFont(mf)
            p.setPen(t.text_3)
            p.drawText(QRectF(r.right() - chev - meta_w, r.top(), meta_w - 8, 26 if self.line and self.title else
                              r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.meta)
        f = font("icon_small")
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(t.text_2 if hot else t.faint)
        p.drawText(QRectF(r.right() - chev, r.top(), chev, r.height()), Qt.AlignmentFlag.AlignCenter,
                   ICON["down" if self.open else "next"])
        p.end()


class FoldRow(QWidget):
    """A row that folds open: a one- or two-line head (the short title and one quiet line), and under it, built
    the first time it opens, the full body (``body()``: the text, where it came from, Why? / Not right, the
    to-do's buttons). Keeps long text out of the page until the user asks for it.

    ``trailing``: a small widget kept on the right of the head (a to-do's Done)."""

    toggled = Signal(bool)

    def __init__(self, t: Theme, title: str, line: str = "", meta: str = "", *, body: Callable[[], QWidget] | None
                 = None, initial: str = "", led: str | None = None, trailing: QWidget | None = None,
                 expanded: bool = False, full: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self._make_body = body
        #: The head's words in full: when the head has to shorten them to fit, the row opens to them (on top
        #: of its body, if it has one), so nothing a user would read is only ever seen with "…".
        self.full = full
        self.body_widget: QWidget | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(0)
        line_row = QHBoxLayout()
        line_row.setContentsMargins(0, 0, 0, 0)
        line_row.setSpacing(8)
        self.head = _FoldHead(t, title, line, meta, initial=initial, led=led, parent=self)
        self.head.cut_changed = self._cut_changed  # type: ignore[method-assign]
        self.head.clicked.connect(self.toggle)
        line_row.addWidget(self.head, 1)
        if trailing is not None:
            trailing.setParent(self)
            line_row.addWidget(trailing, 0, Qt.AlignmentFlag.AlignVCenter)
        self.col.addLayout(line_row)
        if body is None:
            self.head.setCursor(Qt.CursorShape.ArrowCursor)
        if expanded:
            self.toggle()

    @property
    def is_open(self) -> bool:
        return self.body_widget is not None and not self.body_widget.isHidden()

    def _cut_changed(self) -> None:
        if self._make_body is None and self.body_widget is None:
            self.head.setCursor(Qt.CursorShape.PointingHandCursor if self.head.cut and self.full
                                else Qt.CursorShape.ArrowCursor)

    def ensure_body(self) -> QWidget | None:
        spell = bool(self.full) and self.head.cut
        if self.body_widget is None and (self._make_body is not None or spell):
            if spell:   # the words the head had to shorten, in full, above the body
                w = QWidget()
                w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                inner = QVBoxLayout(w)
                inner.setContentsMargins(0, 0, 0, 0)
                inner.setSpacing(8)
                inner.addWidget(text_label(self.t, self.full, "body", "text", wrap=True, selectable=True))
                if self._make_body is not None:
                    inner.addWidget(self._make_body())
            else:
                w = self._make_body()
            w.setParent(self)
            wrap = QWidget(self)
            wrap.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            lay = QVBoxLayout(wrap)
            lay.setContentsMargins(42 if self.head.initial else 18 if self.head.led is not None else 0, 4, 0, 10)
            lay.addWidget(w)
            self.body_widget = wrap
            self.body_widget.inner = w  # type: ignore[attr-defined]
            wrap.hide()
            self.col.addWidget(wrap)
        return self.body_widget

    def toggle(self) -> None:
        body = self.ensure_body()
        if body is None:
            return
        show = body.isHidden()
        body.setVisible(show)
        self.head.open = show
        self.head.update()
        self.toggled.emit(show)
