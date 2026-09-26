"""The window's building blocks, all painted from :mod:`yuki.ui.window.theme` tokens.

Controls are pills; containers are glass. Nothing here reads memory or knows a
page: a widget gets a :class:`~yuki.ui.window.theme.Theme` and its content, and
paints. Clicks come out as Qt signals the page (phase 2: the controller)
connects to the memory calls.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.window.glass import paint_glass, rounded
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


def scroll_css(transparent: bool, bg: QColor | None = None) -> str:
    """A scroll area's style: a thin scrollbar; ``transparent``: the viewport and page show what is behind,
    else they paint ``bg`` themselves (opaque, so Qt can scroll by moving pixels)."""
    base = ("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
            if transparent else
            "QScrollArea { background: transparent; border: none; }"
            f"QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{ background: {css(bg or QColor(0, 0, 0))};"
            " border: none; }")
    return (base + "QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px; }"
            "QScrollBar::handle:vertical { background: rgba(128,128,140,90); border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }")


def scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(scroll_css(True))
    area.viewport().setAutoFillBackground(False)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    smooth(area)
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

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        t = self.t
        # every button is clear glass (2026-09-27): the style is only its tint, never a solid block
        if self.style == "accent":
            paint_glass(p, r, r.height() / 2, t.level("accent"))
            ink = QColor(t.accent_ink)
        elif self.style in ("tinted", "danger"):
            c = QColor(t.kind("error") if self.style == "danger" else (self.tint or t.accent))
            wash = QColor(c)
            wash.setAlphaF(0.12 if not t.dark else 0.18)
            paint_glass(p, r, r.height() / 2, t.level("control"), tint=wash, shadow=False)
            ink = c.darker(125) if not t.dark else c.lighter(125)
        elif self.style == "quiet":
            paint_glass(p, r, r.height() / 2, t.level("inset"), shadow=False)
            ink = t.text_2
        else:
            paint_glass(p, r, r.height() / 2, t.level("control"))
            ink = t.text
        if self.underMouse() and self.isEnabled():   # hover: a breath more light, painted (never re-rendered)
            wash = QColor(t.text)
            wash.setAlphaF(0.05 if not t.dark else 0.07)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wash)
            p.drawPath(rounded(r, r.height() / 2))
        if not self.isEnabled():
            ink = QColor(t.text_3)
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
    """A small tag: a word on a faint glass wash of a kind's colour (joke, unsure, you, story, unclear...).

    No coloured dot (2026-09-27: dots read as generic AI UI); the word and the wash carry it.
    """

    WORDS = {"joke": "Joke", "story": "Story", "you": "You said", "unsure": "Not sure", "unclear": "May be misheard",
             "call": "Call", "live": "Live", "updating": "Updating…"}

    def __init__(self, t: Theme, kind: str, text: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.kind = t, kind
        self.text = text or self.WORDS.get(kind, kind.capitalize())
        self.setFont(font("micro"))
        fm = QFontMetrics(self.font())
        self.setFixedSize(fm.horizontalAdvance(self.text) + 18, 20)

    def color(self) -> QColor:
        k = {"unsure": "unclear", "updating": "you", "live": "you"}.get(self.kind, self.kind)
        return QColor(self.t.kind(k))

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = self.color()
        fill = QColor(c)
        fill.setAlphaF(0.12 if not self.t.dark else 0.18)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(rounded(r, r.height() / 2))
        p.setPen(c.darker(135) if not self.t.dark else c.lighter(135))
        p.setFont(self.font())
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text)
        p.end()


def _glass_changed(widget: QWidget) -> None:
    from yuki.ui.window.glasscache import notify_glass

    notify_glass(widget)


class Toggle(QAbstractButton):
    """An on/off switch: a glass track, the knob a bright glass bead; accent when on."""

    def __init__(self, t: Theme, on: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(46, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(lambda _on: _glass_changed(self))

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        # the track is clear glass (a blue-hued wash when on), the knob a clear glass bead on it
        paint_glass(p, r, r.height() / 2, self.t.level("accent" if self.isChecked() else "inset"), shadow=False)
        d = r.height() - 6
        x = r.right() - d - 3 if self.isChecked() else r.left() + 3
        knob = QRectF(x, r.top() + 3, d, d)
        paint_glass(p, knob, d / 2, self.t.level("bead"))
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
                if opt != self.current:
                    self.current = opt
                    self.update()
                    _glass_changed(self)
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
    """A glass pill that shows the chosen value and a chevron; a click opens the choices.

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
        self.setFont(font("callout"))
        self.setFixedSize(width, CONTROL_H["regular"] + 4)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open)

    def _open(self) -> None:
        from PySide6.QtWidgets import QMenu

        opts = self.options() if callable(self.options) else self.options
        if not opts:
            return
        menu = GlassMenu(self.t, self)
        menu.setMinimumWidth(self.width())
        for label, value in opts:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(label == self.text())
            act.triggered.connect(lambda _c=False, lab=label, v=value: self._pick(lab, v))
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def _pick(self, label: str, value: object) -> None:
        self.setText(label)
        self.update()
        self.changed.emit(value)

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, r, r.height() / 2, self.t.level("control"), shadow=False)
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
        paint_glass(p, r, r.height() / 2, self.t.level("control" if self.height() >= 40 else "inset"), shadow=False)
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
        fill.setAlphaF(0.16 if not self.t.dark else 0.22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(r)
        p.setPen(self.color.darker(135) if not self.t.dark else self.color.lighter(130))
        f = font("headline")
        f.setPixelSize(int(r.height() * 0.42))
        p.setFont(f)
        initial = next((ch for ch in self.name if ch.isalnum()), "?").upper()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, initial)
        p.end()


class NavItem(QAbstractButton):
    """A sidebar entry: glyph and word; the current page sits in a faint blue-hued glass lens."""

    def __init__(self, t: Theme, glyph: str, text: str, *, badge: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.badge = t, glyph, badge
        self.setText(text)
        self.setCheckable(True)
        self.setFixedHeight(40)
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
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.isChecked():
            paint_glass(p, r, r.height() / 2, self.t.level("lens"), shadow=False)
        elif self.underMouse():
            wash = QColor(self.t.text)
            wash.setAlphaF(0.04 if not self.t.dark else 0.06)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wash)
            p.drawPath(rounded(r, r.height() / 2))
        ink = self.t.accent if self.isChecked() else self.t.text_2
        p.setPen(ink)
        p.setFont(font("icon"))
        p.drawText(QRectF(r.left() + 14, r.top(), 20, r.height()), Qt.AlignmentFlag.AlignCenter, ICON[self.glyph])
        p.setPen(self.t.text if self.isChecked() else self.t.text_2)
        p.setFont(font("body_strong" if self.isChecked() else "body"))
        p.drawText(QRectF(r.left() + 46, r.top(), r.width() - 60, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   self.text())
        if self.badge:   # a count on a faint glass wash, never a solid block
            p.setFont(font("micro"))
            fm = QFontMetrics(p.font())
            w = max(20, fm.horizontalAdvance(self.badge) + 12)
            b = QRectF(r.right() - w - 10, r.center().y() - 10, w, 20)
            c = QColor(self.t.kind("question"))
            wash = QColor(c)
            wash.setAlphaF(0.14 if not self.t.dark else 0.22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wash)
            p.drawPath(rounded(b, 10))
            p.setPen(c.darker(130) if not self.t.dark else c.lighter(125))
            p.drawText(b, Qt.AlignmentFlag.AlignCenter, self.badge)
        p.end()


class StatTile(Glass):
    """A small figure: label, value, and an optional note under it."""

    def __init__(self, t: Theme, label: str, value: str, note: str = "", *, glyph: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(t, level="inset", radius=RADIUS["row"] + 4, padding=SPACE["m"] + 2, parent=parent)
        self.body.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        if glyph:
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
    the user says what is true in their own words, or "It's just wrong, forget
    it"). The folds are built the first time they open.

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
        head = QHBoxLayout()
        head.setSpacing(6)
        col.addLayout(head)
        self._head = head
        if title:
            head.addWidget(text_label(t, title, title_role))
        for tag in tags:
            head.addWidget(Chip(t, tag))
        head.addStretch(1)
        self.text_label = text_label(t, text, "callout", "text", wrap=True, selectable=True)
        col.addWidget(self.text_label)
        self.was_label: QLabel | None = None
        foot = QHBoxLayout()
        foot.setSpacing(6)
        col.addLayout(foot)
        foot.addWidget(icon_label(t, "history", t.text_3, "icon_small"))
        self.source_label = text_label(t, source, "caption", "text_3")
        foot.addWidget(self.source_label)
        foot.addStretch(1)
        self.why_pill: Pill | None = None
        self.wrong_pill: Pill | None = None
        if actions:
            self.why_pill = Pill(t, "Why?", style="quiet", size="small")
            self.why_pill.clicked.connect(self.toggle_evidence)
            foot.addWidget(self.why_pill)
            if not evidence:
                self.why_pill.hide()
            self.wrong_pill = Pill(t, "Not right", glyph="edit", style="quiet", size="small")
            self.wrong_pill.clicked.connect(self.toggle_composer)
            foot.addWidget(self.wrong_pill)
        self._ev_box: QWidget | None = None
        self._composer: QWidget | None = None
        self.field: TextPill | None = None
        self.done_note: QLabel | None = None
        self._state_chip: Chip | None = None
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
        chip_text = {"correct": "Updating…", "forget": "Forgetting…", "confirm": "Confirmed"}.get(kind, "Updating…")
        if late and kind != "confirm":
            chip_text = "Still updating"
        if self._state_chip is None:
            self._state_chip = Chip(t, "updating", chip_text)
            self._head.insertWidget(max(0, self._head.count() - 1), self._state_chip)
        if kind == "correct" and words:
            self.text_label.setText(f"You said: “{words}”")
            if self.was_label is None:
                self.was_label = text_label(t, "", "caption", "text_3", wrap=True)
                self._col.insertWidget(self._col.indexOf(self.text_label) + 1, self.was_label)
            self.was_label.setText(f"Yuki had: {self._text}")
            note = "Yuki is putting this right" if not late else "Yuki hasn't applied this yet; it will"
            self.source_label.setText(f"You corrected this at {when} · {note}")
        elif kind == "forget":
            self.text_label.setStyleSheet(f"color: {css(t.text_3)}; background: transparent;")
            self.source_label.setText(f"You said this is wrong at {when} · Yuki is forgetting it")
        elif kind == "confirm":
            self.source_label.setText(f"You confirmed this at {when}")
        if self.wrong_pill is not None:
            self.wrong_pill.setText("Sent to Yuki")
            self.wrong_pill.style = "quiet"
            self.wrong_pill.setEnabled(False)
            self.wrong_pill.updateGeometry()
        if self._composer is not None:
            self._composer.setVisible(False)

    def toggle_evidence(self) -> None:
        t = self.t
        if self._ev_box is None:
            ev = Glass(t, level="inset", radius=RADIUS["row"], padding=SPACE["m"])
            ev.body.setSpacing(6)
            ev.body.addWidget(text_label(t, "Why Yuki thinks this", "caption_strong", "text_2"))
            for when, where, words in self._evidence_rows:
                line = QHBoxLayout()
                line.setSpacing(SPACE["s"])
                stamp = text_label(t, f"{when} · {where}", "caption", "text_3")
                stamp.setFixedWidth(150)
                line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
                line.addWidget(text_label(t, words, "caption", "text_2", wrap=True, selectable=True), 1)
                ev.body.addLayout(line)
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
            box = Glass(t, level="inset", radius=RADIUS["row"] + 4, padding=SPACE["m"])
            box.body.setSpacing(SPACE["s"])
            box.body.addWidget(text_label(t, "Tell Yuki what's right, in your own words. It will fix this and "
                                             "remember it came from you.", "caption", "text_2", wrap=True))
            self.field = TextPill(t, self._placeholder or "e.g. “Elina is my friend”", action="Tell Yuki", height=38)
            self.field.submitted.connect(self._send)
            box.body.addWidget(self.field)
            row = QHBoxLayout()
            forget = Pill(t, "It's just wrong, forget it", style="quiet", size="small")
            forget.clicked.connect(lambda: self._sent(self.forget.emit, self.ref))
            row.addWidget(forget)
            row.addStretch(1)
            cancel = Pill(t, "Cancel", style="quiet", size="small")
            cancel.clicked.connect(self.toggle_composer)
            row.addWidget(cancel)
            box.body.addLayout(row)
            self._composer = box
            self._col.addWidget(box)
            box.setVisible(False)
        show = not self._composer.isVisible()
        self._composer.setVisible(show)
        if self.wrong_pill is not None:
            self.wrong_pill.style = "tinted" if show else "quiet"
            self.wrong_pill.tint = t.kind("question") if show else None
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


class GlassSlider(QWidget):
    """A glass track with a bead; ``value`` 0..2 (1 = as designed). ``committed(value)`` when let go.

    Nothing is re-rendered while it is dragged: the glass follows once the bead is released.
    """

    committed = Signal(float)

    def __init__(self, t: Theme, value: float = 1.0, *, width: int = 200, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.value = max(0.0, min(2.0, float(value)))
        self.setFixedSize(width, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drag = False

    def _set_from(self, x: float) -> None:
        span = max(1.0, self.width() - 24)
        self.value = max(0.0, min(2.0, (x - 12) / span * 2.0))
        self.update()

    def mousePressEvent(self, e: object) -> None:  # noqa: D102
        self._drag = True
        self._set_from(e.position().x())

    def mouseMoveEvent(self, e: object) -> None:  # noqa: D102
        if self._drag:
            self._set_from(e.position().x())

    def mouseReleaseEvent(self, e: object) -> None:  # noqa: D102
        if self._drag:
            self._drag = False
            self._set_from(e.position().x())
            self.committed.emit(round(self.value, 2))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        track = QRectF(12, self.height() / 2 - 3, self.width() - 24, 6)
        paint_glass(p, track, 3, t.level("inset"), shadow=False, painted=True)
        filled = QRectF(track.left(), track.top(), track.width() * self.value / 2.0, track.height())
        paint_glass(p, filled, 3, t.level("accent"), shadow=False, painted=True)
        mid = track.left() + track.width() / 2
        p.setPen(QPen(t.text_3, 1.0))
        p.drawLine(int(mid), int(track.top() - 4), int(mid), int(track.bottom() + 4))
        x = track.left() + track.width() * self.value / 2.0
        knob = QRectF(x - 10, self.height() / 2 - 10, 20, 20)
        paint_glass(p, knob, 10, t.level("bead"), painted=True)
        p.end()


class Toast(QWidget):
    """A short glass note at the bottom of the window ("Told Yuki. It will fix this.")."""

    def __init__(self, t: Theme, parent: QWidget) -> None:
        super().__init__(parent)
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text = ""
        self._tone = ""
        self._timer = None
        self._grabbing = False
        self._lens = FloatLens(self, lambda: self.t, settle_ms=0)
        self.hide()

    def _tint(self) -> QColor | None:
        if not self._tone:
            return None
        tint = QColor(self.t.kind(self._tone) if self._tone in self.t.kinds else self.t.status.get(self._tone,
                                                                                                 self.t.accent))
        tint.setAlphaF(0.12)
        return tint

    def show_text(self, text: str, *, tone: str = "", ms: int = 3200) -> None:
        from PySide6.QtCore import QTimer

        self._text, self._tone = text, tone
        fm = QFontMetrics(font("callout"))
        w = min(self.parentWidget().width() - 80, fm.horizontalAdvance(text) + 56)
        self.resize(w, 40)
        par = self.parentWidget()
        self.move((par.width() - w) // 2 + 100, par.height() - 64)
        self.raise_()
        self.show()
        self.update()
        # the toast floats over the page: it bends what is under it, rendered once for this place and text
        self._lens.request((self.geometry().getRect(), text, tone), self._grab, self.height() / 2 - 1,
                           tint=self._tint())
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.hide)
        self._timer.start(ms)

    def _grab(self):
        par = self.parentWidget()
        if par is None:
            return None
        self._grabbing = True
        try:
            return grab_under(par, self.geometry())
        finally:
            self._grabbing = False

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        if self._grabbing:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_floating(p, r, r.height() / 2, self.t,
                       self._lens.image_for((self.geometry().getRect(), self._text, self._tone)), tint=self._tint())
        p.setPen(self.t.text)
        p.setFont(font("callout"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()


# -- floating glass over the app's own content ----------------------------------------------------------


def grab_under(host: QWidget, rect: "QRect", margin: int = 24):
    """What ``host`` shows under ``rect`` (host coordinates), with ``margin`` around it for the rim to bend:
    ``(image, where the glass sits in it)``, or None. A grab of the app's own widgets, never of the screen."""
    from PySide6.QtCore import QRect

    area = QRect(rect).adjusted(-margin, -margin, margin, margin).intersected(host.rect())
    if area.width() < 4 or area.height() < 4:
        return None
    img = host.grab(area).toImage()
    return img, QRectF(QRect(rect).translated(-area.topLeft()))


class FloatLens:
    """Refraction for one glass shape floating over the app's own content (a hover card, a toast, a menu,
    a tooltip): rendered once for a place and size on the glass thread, then kept and just painted.

    ``request(key, grab, radius)``: ``grab()`` (GUI thread) returns ``(content, rect)`` as
    :func:`grab_under` does. Nothing renders while the key stays the same; a new key waits ``settle_ms``
    (the pointer coming to rest) and renders once. Off, or when nothing is under it, the caller paints the
    same glass flat (:func:`paint_floating`).
    """

    renders = 0

    def __init__(self, owner: QWidget, theme: Callable[[], Theme], *, settle_ms: int = 90) -> None:
        from PySide6.QtCore import QTimer

        self.owner, self.theme = owner, theme
        self.key: object = None
        self.image = None
        self._wanted: object = None
        self._job: tuple | None = None
        self._timer = QTimer(owner)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, settle_ms))
        self._timer.timeout.connect(self._run)

    def image_for(self, key: object):
        return self.image if self.key == key and key is not None else None

    def forget(self) -> None:
        self.key = self.image = self._wanted = None
        self._timer.stop()

    def request(self, key: object, grab: Callable[[], object], radius: float, *, level: str = "popover",
                tint: QColor | None = None) -> None:
        from yuki.ui import lens

        if not lens.policy().floats_refract or key == self.key or key == self._wanted:
            return
        self._wanted = key
        self._job = (grab, radius, level, tint)
        self._timer.start()

    def _run(self) -> None:
        import weakref

        from yuki.ui import lens
        from yuki.ui.glass import glass_worker

        key, job = self._wanted, self._job
        if key is None or job is None:
            return
        grab, radius, level, tint = job
        try:
            got = grab()
        except RuntimeError:
            got = None
        if not got:
            self._wanted = None
            return
        content, rect = got
        dpr = content.devicePixelRatio() or 1.0
        t = self.theme()
        optics = t.optics.get(level) or t.optics["popover"]
        r_dev = QRectF(rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr)
        ref = weakref.ref(self)
        mode = lens.policy().mode

        def work():
            return lens.float_lens(content, r_dev, radius * dpr, optics, tint=tint, mode=mode)

        def deliver(img) -> None:
            me = ref()
            if me is None or me._wanted != key:
                return
            me.key, me.image, me._wanted = key, img, None
            FloatLens.renders += 1
            try:
                me.owner.update()
            except RuntimeError:
                pass

        glass_worker().submit(work, deliver)


def paint_floating(p: QPainter, rect: QRectF, radius: float, t: Theme, image=None, *,
                   tint: QColor | None = None, shadow: bool = True) -> None:
    """Floating glass: the refracted content under it when rendered (``image``), else the same glass flat."""
    level = t.level("popover")
    if image is None or image.isNull():
        paint_glass(p, rect, radius, level, tint=tint, shadow=shadow, painted=True)
        return
    from yuki.ui.window.glass import paint_catchlight, paint_shadow

    p.save()
    if shadow:
        paint_shadow(p, rect, radius, level)
    p.setClipPath(rounded(rect, radius))
    p.drawImage(rect, image)
    p.restore()
    p.save()
    paint_catchlight(p, rect, radius, level)
    p.restore()


class GlassMenu(QMenu):
    """A popup menu of clear glass (dropdowns, the day picker, the tray): rounded, translucent, no border;
    over the app it bends the window's content under it, rendered once when it opens."""

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFont(font("callout"))
        self.setStyleSheet(menu_css(t))
        self._lens = FloatLens(self, lambda: self.t, settle_ms=0)

    def _host(self) -> QWidget | None:
        par = self.parentWidget()
        return par.window() if par is not None else None

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        host = self._host()
        if host is None:
            return
        from PySide6.QtCore import QRect

        local = QRect(host.mapFromGlobal(self.geometry().topLeft()), self.size())
        self._lens.request(local.getRect(), lambda: grab_under(host, local), 12)

    def paintEvent(self, e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        host = self._host()
        key = None
        if host is not None:
            key = (host.mapFromGlobal(self.geometry().topLeft()).x(), host.mapFromGlobal(self.geometry().topLeft()).y(),
                   self.width(), self.height())
        paint_floating(p, r, 12, self.t, self._lens.image_for(key), shadow=False)
        p.end()
        super().paintEvent(e)


class _MenuGlass(QObject):
    """Paints clear glass under a plain QMenu's items (for submenus made with ``addMenu``)."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__()
        self.theme = theme

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() == QEvent.Type.Paint and isinstance(obj, QWidget):
            p = QPainter(obj)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            paint_floating(p, QRectF(obj.rect()).adjusted(1, 1, -1, -1), 12, self.theme(), None, shadow=False)
            p.end()
        return False


_MENU_GLASS: list[_MenuGlass] = []


def glassify(menu: QMenu, theme: Callable[[], Theme]) -> None:
    """Make an existing plain QMenu (a submenu) clear glass like :class:`GlassMenu` (once; restyled after)."""
    if not menu.property("glass_menu"):
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if not _MENU_GLASS:
            _MENU_GLASS.append(_MenuGlass(theme))
        _MENU_GLASS[0].theme = theme
        menu.installEventFilter(_MENU_GLASS[0])
        menu.setFont(font("callout"))
        menu.setProperty("glass_menu", True)
    menu.setStyleSheet(menu_css(theme()))


def menu_css(t: Theme) -> str:
    """The items of a :class:`GlassMenu` (the glass itself is painted): no background, no border."""
    return (f"QMenu {{ background: transparent; color: {css(t.text)}; border: none; padding: 6px; }}"
            "QMenu::item { padding: 6px 22px 6px 26px; border-radius: 8px; background: transparent; }"
            f"QMenu::item:selected {{ background: {css(t.accent_soft)}; }}"
            f"QMenu::item:disabled {{ color: {css(t.text_3)}; }}"
            f"QMenu::separator {{ height: 1px; background: {css(t.grid)}; margin: 4px 10px; }}")


# -- tooltips: clear glass, not the system's opaque box ------------------------------------------------


class GlassTip(QWidget):
    """The tooltip: a small floating glass pane that bends the app content under it (never focused)."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowTransparentForInput)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._text = ""
        self._host: QWidget | None = None
        self._lens = FloatLens(self, theme, settle_ms=0)

    def show_tip(self, text: str, gpos: object, host: QWidget | None) -> None:
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QGuiApplication

        self._text, self._host = text, host
        fm = QFontMetrics(font("caption"))
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
        self._lens.forget()
        self.show()
        self.update()
        if host is not None and host.isVisible():
            local = QRect(host.mapFromGlobal(pos), self.size())
            self._lens.request(local.getRect(), lambda: grab_under(host, local), 10)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        t = self.theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        key = None
        if self._host is not None:
            try:
                tl = self._host.mapFromGlobal(self.pos())
                key = (tl.x(), tl.y(), self.width(), self.height())
            except RuntimeError:
                key = None
        paint_floating(p, r, 10, t, self._lens.image_for(key), shadow=False)
        p.setPen(t.text)
        p.setFont(font("caption"))
        p.drawText(r.adjusted(12, 7, -12, -7), int(Qt.TextFlag.TextWordWrap), self._text)
        p.end()


class _TipFilter(QObject):
    """Shows :class:`GlassTip` instead of the system tooltip for the widgets it watches."""

    def __init__(self, theme: Callable[[], Theme]) -> None:
        super().__init__()
        from PySide6.QtCore import QTimer

        self.tip = GlassTip(theme)
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
    """Glass tooltips for ``root`` and every widget under it that has one (one filter per process; a
    filter on each widget with a tooltip, not on the whole application, so other events cost nothing)."""
    f = _TIPS.get("tips")
    if f is None:
        f = _TIPS["tips"] = _TipFilter(theme)
    f.tip.theme = theme
    f.tip._lens.theme = theme
    if not root.property("glass_tip"):   # the root always (its tooltip may be set later, e.g. the voice pill)
        root.installEventFilter(f)
        root.setProperty("glass_tip", True)
    for w in root.findChildren(QWidget):
        if w.toolTip() and not w.property("glass_tip"):
            w.installEventFilter(f)
            w.setProperty("glass_tip", True)


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
    """A placeholder while a page or a section loads: faint glass blocks with a soft light sweeping over
    them. Its timer runs only while it is shown; it is deleted when the real content takes its place."""

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
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(300, self.minimumHeight())

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        from yuki.ui import lens

        if not lens.policy().reduce_motion:
            self._timer.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QBrush, QLinearGradient

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, r, RADIUS["panel"], t.level("regular"), shadow=False, painted=True)
        base = QColor(t.text)
        base.setAlphaF(0.06 if not t.dark else 0.08)
        blocks = [QRectF(r.left() + 20, r.top() + 20, r.width() * 0.34, 14)]
        y = r.top() + 50
        for i in range(self.rows):
            if y + 12 > r.bottom() - 12:
                break
            blocks.append(QRectF(r.left() + 20, y, (r.width() - 40) * (0.92 - 0.17 * (i % 3)), 11))
            y += 24
        path = None
        for b in blocks:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(base)
            p.drawPath(rounded(b, b.height() / 2))
            path = rounded(b, b.height() / 2) if path is None else path.united(rounded(b, b.height() / 2))
        # the sweep: a soft band of light crossing every block, once every 1.4 s
        phase = (self._clock.elapsed() % 1400) / 1400.0
        x = r.left() - r.width() * 0.3 + phase * r.width() * 1.6
        g = QLinearGradient(QPointF(x - 120, 0), QPointF(x + 120, 0))
        hi = QColor(255, 255, 255, 0)
        g.setColorAt(0.0, hi)
        mid = QColor(255, 255, 255, 90 if t.dark else 200)
        g.setColorAt(0.5, mid)
        g.setColorAt(1.0, hi)
        if path is not None:
            p.setClipPath(path)
            p.fillRect(r, QBrush(g))
        p.end()


Callback = Callable[..., object]
