"""The window's building blocks, all painted from :mod:`yuki.ui.window.theme` tokens.

Controls are pills; containers are glass. Nothing here reads memory or knows a
page: a widget gets a :class:`~yuki.ui.window.theme.Theme` and its content, and
paints. Clicks come out as Qt signals the page (phase 2: the controller)
connects to the memory calls.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.window.glass import paint_glass, rounded
from yuki.ui.window.refract import collecting
from yuki.ui.window.theme import CONTROL_H, ICON, RADIUS, SPACE, Theme, css, font

# -- text ------------------------------------------------------------------------------


def text_label(t: Theme, text: str, role: str = "body", ink: str = "text", *, wrap: bool = False,
               selectable: bool = False, parent: QWidget | None = None) -> QLabel:
    """A label in a type role and an ink role (``text`` | ``text_2`` | ``text_3`` | a kind name)."""
    lab = QLabel(text, parent)
    lab.setFont(font(role))
    color = getattr(t, ink, None) if ink in ("text", "text_2", "text_3", "accent") else t.kind(ink)
    lab.setStyleSheet(f"color: {css(color)}; background: transparent;")
    lab.setTextFormat(Qt.TextFormat.PlainText)
    lab.setWordWrap(wrap)
    if wrap:
        lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
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


class Glass(QWidget):
    """A glass container: ``level`` (inset | regular | raised | lens), a radius and padding."""

    def __init__(self, t: Theme, *, level: str = "regular", radius: int = RADIUS["panel"],
                 padding: int = SPACE["l"], tint: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.level, self.radius, self.tint = t, level, radius, tint
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._pad = padding
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(SPACE["m"])

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_glass(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), self.radius, self.t.level(self.level),
                    tint=self.tint, shadow=self.level not in ("inset",))
        p.end()


class Panel(Glass):
    """A titled glass panel: title, optional subtitle, optional right-hand widget, then content."""

    def __init__(self, t: Theme, title: str = "", subtitle: str = "", *, right: QWidget | None = None,
                 glyph: str | None = None, level: str = "regular", padding: int = SPACE["l"] + 2,
                 parent: QWidget | None = None) -> None:
        super().__init__(t, level=level, padding=padding, parent=parent)
        if title:
            head = QHBoxLayout()
            head.setSpacing(SPACE["s"])
            if glyph:
                head.addWidget(icon_label(t, glyph, t.text_2))
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(text_label(t, title, "headline"))
            if subtitle:
                col.addWidget(text_label(t, subtitle, "caption", "text_3", wrap=True))
            head.addLayout(col, 1)
            if right is not None:
                head.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
            self.body.addLayout(head)


def scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(
        "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
        "QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px; }"
        "QScrollBar::handle:vertical { background: rgba(128,128,140,90); border-radius: 3px; min-height: 30px; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }")
    area.viewport().setAutoFillBackground(False)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    return area


# -- controls --------------------------------------------------------------------------


class Pill(QAbstractButton):
    """A pill button. ``style``: glass | accent | quiet | tinted (``tint`` colour) | danger."""

    def __init__(self, t: Theme, text: str = "", *, glyph: str | None = None, style: str = "glass",
                 size: str = "regular", tint: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.style, self.size_name, self.tint, self.glyph = t, style, size, tint, glyph
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(font("caption_strong" if size == "small" else "callout"))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: D102
        h = CONTROL_H[self.size_name]
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) if self.text() else 0
        pad = 14 if self.size_name == "small" else 18
        icon = (16 + (6 if self.text() else 0)) if self.glyph else 0
        return QSize(max(h, w + icon + 2 * pad) if self.text() else h, h)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        t = self.t
        ink = t.text
        if collecting():   # real glass: every pill is a droplet; the style is its tint
            if self.style == "accent":
                paint_glass(p, r, r.height() / 2, t.level("accent"))
                ink = t.text_on_accent
            elif self.style in ("tinted", "danger"):
                c = QColor(t.status["critical"] if self.style == "danger" else (self.tint or t.accent))
                wash = QColor(c)
                wash.setAlphaF(0.16 if not t.dark else 0.22)
                paint_glass(p, r, r.height() / 2, t.level("control"), tint=wash)
                ink = c.darker(115) if not t.dark else c.lighter(125)
            else:
                paint_glass(p, r, r.height() / 2, t.level("control"), shadow=self.style != "quiet")
                ink = t.text if self.style == "glass" else t.text_2
        elif self.style == "accent":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.accent)
            p.drawPath(rounded(r, r.height() / 2))
            hi = QColor(255, 255, 255, 70)
            p.setPen(QPen(hi, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(rounded(r.adjusted(0.5, 0.5, -0.5, -0.5), r.height() / 2))
            ink = t.text_on_accent
        elif self.style == "quiet":
            p.setPen(QPen(t.baseline, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(rounded(r, r.height() / 2))
            ink = t.text_2
        elif self.style in ("tinted", "danger"):
            c = QColor(t.status["critical"] if self.style == "danger" else (self.tint or t.accent))
            fill = QColor(c)
            fill.setAlphaF(0.14 if not t.dark else 0.20)
            edge = QColor(c)
            edge.setAlphaF(0.35)
            p.setBrush(fill)
            p.setPen(QPen(edge, 1.0))
            p.drawPath(rounded(r, r.height() / 2))
            ink = c if not t.dark else c.lighter(115)
        else:
            paint_glass(p, r, r.height() / 2, t.level("raised"), shadow=False)
        p.setPen(ink)
        pad = 14 if self.size_name == "small" else 18
        x = r.left() + pad if self.text() else r.center().x() - 8
        if self.glyph:
            p.setFont(font("icon_small" if self.size_name == "small" else "icon"))
            gw = 16
            if not self.text():
                p.drawText(QRectF(r.center().x() - gw / 2, r.top(), gw, r.height()),
                           Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
            else:
                p.drawText(QRectF(x, r.top(), gw, r.height()), Qt.AlignmentFlag.AlignCenter,
                           ICON.get(self.glyph, self.glyph))
                x += gw + 6
        if self.text():
            p.setFont(self.font())
            p.drawText(QRectF(x, r.top(), r.right() - x - pad + 2, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


class Chip(QWidget):
    """A small tag: a dot and a word in a kind's colour (joke, unsure, you, story, unclear...)."""

    WORDS = {"joke": "Joke", "story": "Story", "you": "You said", "unsure": "Not sure", "unclear": "May be misheard",
             "call": "Call", "live": "Live"}

    def __init__(self, t: Theme, kind: str, text: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.kind = t, kind
        self.text = text or self.WORDS.get(kind, kind.capitalize())
        self.setFont(font("micro"))
        fm = QFontMetrics(self.font())
        self.setFixedSize(fm.horizontalAdvance(self.text) + 26, 20)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QColor(self.t.kind("unclear" if self.kind == "unsure" else self.kind))
        fill = QColor(c)
        fill.setAlphaF(0.13 if not self.t.dark else 0.20)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(rounded(r, r.height() / 2))
        p.setBrush(c)
        p.drawEllipse(QRectF(r.left() + 8, r.center().y() - 3, 6, 6))
        p.setPen(self.t.text_2 if not self.t.dark else self.t.text)
        p.setFont(self.font())
        p.drawText(QRectF(r.left() + 18, r.top(), r.width() - 20, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   self.text)
        p.end()


class Toggle(QAbstractButton):
    """An on/off switch: a glass track, the knob a bright glass bead; accent when on."""

    def __init__(self, t: Theme, on: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(46, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked() and collecting():
            paint_glass(p, r, r.height() / 2, self.t.level("accent"), shadow=False)
        elif self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.t.accent)
            p.drawPath(rounded(r, r.height() / 2))
        else:
            paint_glass(p, r, r.height() / 2, self.t.level("inset"), shadow=False)
            p.setPen(QPen(self.t.baseline, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(rounded(r, r.height() / 2))
        d = r.height() - 6
        x = r.right() - d - 3 if self.isChecked() else r.left() + 3
        knob = QRectF(x, r.top() + 3, d, d)
        if collecting():   # the knob is a glass bead on the track
            paint_glass(p, knob, d / 2, self.t.level("lens"))
            p.end()
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(knob.translated(0, 1.2))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(knob)
        p.end()


class Segmented(QWidget):
    """A pill of mutually exclusive options; the chosen one sits in a glass lens."""

    changed = Signal(str)

    def __init__(self, t: Theme, options: Sequence[str], current: str, *, small: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.options, self.current = t, list(options), current
        self.setFont(font("caption_strong" if small else "callout"))
        fm = QFontMetrics(self.font())
        self._widths = [fm.horizontalAdvance(o) + (22 if small else 28) for o in self.options]
        self.setFixedSize(sum(self._widths) + 8, CONTROL_H["small" if small else "regular"] + 4)

    def mousePressEvent(self, event: object) -> None:  # noqa: D102
        x = event.position().x() - 4
        for opt, w in zip(self.options, self._widths):
            if x < w:
                self.current = opt
                self.update()
                self.changed.emit(opt)
                return
            x -= w

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, r, r.height() / 2, self.t.level("inset"), shadow=False)
        x = r.left() + 3
        for opt, w in zip(self.options, self._widths):
            cell = QRectF(x, r.top() + 3, w, r.height() - 6)
            if opt == self.current:
                paint_glass(p, cell, cell.height() / 2, self.t.level("lens"), shadow=True)
            p.setPen(self.t.text if opt == self.current else self.t.text_2)
            p.setFont(self.font())
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, opt)
            x += w
        p.end()


class Dropdown(QAbstractButton):
    """A glass pill that shows the chosen value and a chevron (the list opens in a popover)."""

    def __init__(self, t: Theme, value: str, *, width: int = 300, glyph: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph = t, glyph
        self.setText(value)
        self.setFont(font("callout"))
        self.setFixedSize(width, CONTROL_H["regular"] + 4)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, r, r.height() / 2, self.t.level("control" if collecting() else "raised"), shadow=False)
        x = r.left() + 14
        if self.glyph:
            p.setFont(font("icon_small"))
            p.setPen(self.t.text_2)
            p.drawText(QRectF(x, r.top(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
            x += 24
        p.setFont(self.font())
        p.setPen(self.t.text)
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.right() - 34 - x))
        p.drawText(QRectF(x, r.top(), r.right() - 34 - x, r.height()), Qt.AlignmentFlag.AlignVCenter, text)
        p.setFont(font("icon_small"))
        p.setPen(self.t.text_3)
        p.drawText(QRectF(r.right() - 30, r.top(), 18, r.height()), Qt.AlignmentFlag.AlignCenter, ICON["down"])
        p.end()


class TextPill(QWidget):
    """A text field in an inset glass pill, with an optional send pill on the right."""

    submitted = Signal(str)

    def __init__(self, t: Theme, placeholder: str, *, action: str | None = None, action_glyph: str | None = None,
                 text: str = "", height: int = CONTROL_H["input"], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setFixedHeight(height)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 4, 5, 4)
        row.setSpacing(SPACE["s"])
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFont(font("body"))
        self.edit.setFrame(False)
        ph = QColor(t.text_3)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; color: {css(t.text)}; border: none;"
            f" selection-background-color: {css(t.accent_soft)}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, ph)
        self.edit.setPalette(pal)
        row.addWidget(self.edit, 1)
        self.button: Pill | None = None
        if action or action_glyph:
            self.button = Pill(t, action or "", glyph=action_glyph, style="accent",
                               size="regular" if height >= 40 else "small")
            row.addWidget(self.button)
            self.button.clicked.connect(lambda: self.submitted.emit(self.edit.text()))
        self.edit.returnPressed.connect(lambda: self.submitted.emit(self.edit.text()))

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        big = self.height() >= 40
        level = ("control" if big else "inset") if collecting() else ("raised" if big else "inset")
        paint_glass(p, r, r.height() / 2, self.t.level(level), shadow=False)
        p.end()


class Avatar(QWidget):
    """A round initial in a soft tint (people)."""

    def __init__(self, t: Theme, name: str, color: QColor, size: int = 36, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.name, self.color = t, name, color
        self.setFixedSize(size, size)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = QColor(self.color)
        fill.setAlphaF(0.22 if not self.t.dark else 0.30)
        p.setPen(QPen(QColor(255, 255, 255, 120 if not self.t.dark else 40), 1.0))
        p.setBrush(fill)
        p.drawEllipse(r)
        p.setPen(self.color.darker(135) if not self.t.dark else self.color.lighter(130))
        f = font("headline")
        f.setPixelSize(int(r.height() * 0.42))
        p.setFont(f)
        initial = next((ch for ch in self.name if ch.isalnum()), "?").upper()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, initial)
        p.end()


class Dot(QWidget):
    """A status dot with a soft halo (good / warning / serious / critical / accent / muted)."""

    def __init__(self, t: Theme, color: QColor, size: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self.setFixedSize(size + 8, size + 8)
        self._d = size

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QRectF(self.rect()).center()
        halo = QColor(self.color)
        halo.setAlphaF(0.22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, self._d / 2 + 4, self._d / 2 + 4)
        p.setBrush(self.color)
        p.drawEllipse(c, self._d / 2, self._d / 2)
        p.end()


class NavItem(QAbstractButton):
    """A sidebar entry: glyph and word; the current page sits in a glass lens."""

    def __init__(self, t: Theme, glyph: str, text: str, *, badge: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.badge = t, glyph, badge
        self.setText(text)
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.isChecked():
            paint_glass(p, r, r.height() / 2, self.t.level("lens"), shadow=True)
        ink = self.t.accent if self.isChecked() else self.t.text_2
        p.setPen(ink)
        p.setFont(font("icon"))
        p.drawText(QRectF(r.left() + 14, r.top(), 20, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
        p.setPen(self.t.text if self.isChecked() else self.t.text_2)
        p.setFont(font("body_strong" if self.isChecked() else "body"))
        p.drawText(QRectF(r.left() + 46, r.top(), r.width() - 60, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   self.text())
        if self.badge:
            p.setFont(font("micro"))
            fm = QFontMetrics(p.font())
            w = max(20, fm.horizontalAdvance(self.badge) + 12)
            b = QRectF(r.right() - w - 10, r.center().y() - 10, w, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.t.kind("question"))
            p.drawPath(rounded(b, 10))
            p.setPen(QColor(255, 255, 255))
            p.drawText(b, Qt.AlignmentFlag.AlignCenter, self.badge)
        p.end()


class StatTile(Glass):
    """A small figure: label, value, and an optional note under it."""

    def __init__(self, t: Theme, label: str, value: str, note: str = "", *, glyph: str | None = None,
                 dot: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(t, level="inset", radius=RADIUS["row"] + 4, padding=SPACE["m"] + 2, parent=parent)
        self.body.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        if dot is not None:
            head.addWidget(Dot(t, dot, 8))
        elif glyph:
            head.addWidget(icon_label(t, glyph, t.text_3, "icon_small"))
        head.addWidget(text_label(t, label, "caption", "text_2"))
        head.addStretch(1)
        self.body.addLayout(head)
        self.body.addWidget(text_label(t, value, "numeral"))
        if note:
            self.body.addWidget(text_label(t, note, "caption", "text_3", wrap=True))


class Divider(QWidget):
    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setFixedHeight(1)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.fillRect(self.rect(), self.t.grid)
        p.end()


class ItemRow(QWidget):
    """One thing Yuki believes, with where it came from and the two ways to act on it.

    Layout: an optional leading widget (avatar), the title and chips, the text, a
    source line ("Seen in Discord · yesterday"), then two quiet pills: **Why?**
    (the evidence folds open under it) and **Not right** (a composer folds open:
    the user says what is true in their own words; phase 2 sends it through
    ``MemoryClient.correct_portrait`` as their own correction).
    """

    why = Signal(str)
    correct = Signal(str, str)

    def __init__(self, t: Theme, ref: str, title: str, text: str, source: str, *, tags: Sequence[str] = (),
                 leading: QWidget | None = None, evidence: Sequence[tuple[str, str, str]] = (),
                 show_evidence: bool = False, composer: bool = False, placeholder: str = "",
                 actions: bool = True, title_role: str = "body_strong", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.ref = t, ref
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, SPACE["xs"], 0, SPACE["xs"])
        outer.setSpacing(SPACE["m"])
        if leading is not None:
            outer.addWidget(leading, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        if title:
            head.addWidget(text_label(t, title, title_role))
        for tag in tags:
            head.addWidget(Chip(t, tag))
        head.addStretch(1)
        col.addLayout(head)
        col.addWidget(text_label(t, text, "callout", "text", wrap=True))
        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.addWidget(icon_label(t, "history", t.text_3, "icon_small"))
        foot.addWidget(text_label(t, source, "caption", "text_3"))
        foot.addStretch(1)
        if actions:
            why = Pill(t, "Hide why" if show_evidence else "Why?", style="quiet", size="small")
            why.clicked.connect(lambda: self.why.emit(self.ref))
            wrong = Pill(t, "Not right", glyph="edit", style="tinted" if composer else "quiet", size="small",
                         tint=t.kind("question") if composer else None)
            foot.addWidget(why)
            foot.addWidget(wrong)
        col.addLayout(foot)
        if show_evidence and evidence:
            ev = Glass(t, level="inset", radius=RADIUS["row"], padding=SPACE["m"])
            ev.body.setSpacing(6)
            ev.body.addWidget(text_label(t, "Why Yuki thinks this", "caption_strong", "text_2"))
            for when, where, words in evidence:
                line = QHBoxLayout()
                line.setSpacing(SPACE["s"])
                stamp = text_label(t, f"{when} · {where}", "caption", "text_3")
                stamp.setFixedWidth(150)
                line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
                line.addWidget(text_label(t, words, "caption", "text_2", wrap=True), 1)
                ev.body.addLayout(line)
            col.addWidget(ev)
        if composer:
            box = Glass(t, level="inset", radius=RADIUS["row"] + 4, padding=SPACE["m"])
            box.body.setSpacing(SPACE["s"])
            box.body.addWidget(text_label(t, "Tell Yuki what's right, in your own words. It will fix this and "
                                             "remember it came from you.", "caption", "text_2", wrap=True))
            field_ = TextPill(t, placeholder or "e.g. “Elina is my friend”", action="Tell Yuki", height=38)
            field_.submitted.connect(lambda words: self.correct.emit(self.ref, words))
            box.body.addWidget(field_)
            row = QHBoxLayout()
            row.addWidget(Pill(t, "It's just wrong, forget it", style="quiet", size="small"))
            row.addStretch(1)
            row.addWidget(Pill(t, "Cancel", style="quiet", size="small"))
            box.body.addLayout(row)
            col.addWidget(box)
        outer.addLayout(col, 1)


class SettingRow(QWidget):
    """A settings line: glyph, a plain-language label with a hint under it, the control on the right."""

    def __init__(self, t: Theme, glyph: str, label: str, hint: str, control: QWidget | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(SPACE["m"])
        ic = icon_label(t, glyph, t.text_2)
        ic.setFixedWidth(22)
        row.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(text_label(t, label, "body"))
        if hint:
            col.addWidget(text_label(t, hint, "caption", "text_3", wrap=True))
        row.addLayout(col, 1)
        if control is not None:
            row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)


def section_title(t: Theme, text: str, note: str = "") -> QWidget:
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(4, SPACE["s"], 4, 0)
    row.addWidget(text_label(t, text, "title"))
    row.addStretch(1)
    if note:
        row.addWidget(text_label(t, note, "caption", "text_3"))
    return w


def page_header(t: Theme, title: str, subtitle: str, right: Sequence[QWidget] = ()) -> QWidget:
    w = QWidget()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    row = QHBoxLayout(w)
    row.setContentsMargins(4, 0, 0, 0)
    row.setSpacing(SPACE["s"])
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(text_label(t, title, "large_title"))
    col.addWidget(text_label(t, subtitle, "callout", "text_2", wrap=True))
    row.addLayout(col, 1)
    for r in right:
        row.addWidget(r, 0, Qt.AlignmentFlag.AlignVCenter)
    return w


Callback = Callable[..., object]
