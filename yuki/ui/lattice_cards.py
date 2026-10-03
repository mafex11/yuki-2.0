"""The corner cards in the Lattice design (phase E8): the base every card window shares, and the cards that are new.

:class:`LatticeCard` is the design's ``.card``: 416 px wide, the field colour with a 1 px ``#2A2D31`` border and 20 px
corners (no shadow: the design has none), 16 x 18 px padding, a header of a dot icon, a mono label and a close key;
bottom right of the monitor in use (16 px over the taskbar, 20 px from the edge; the call consent card sits top
right). It comes in as the design's ``cardin`` (520 ms, a 10 px rise, its ease) and leaves as its ``leave`` (420 ms,
an 8 px drop), never takes focus, and morphs its height in 240 ms when its contents change (a confirmation in place).

The cards built on it: the offer, question, break and notice cards (:class:`yuki.ui.nudges.NudgeCard`), the call
consent card (:class:`yuki.ui.calls.CallConsentCard`), and here the **return card** (:class:`ReturnCard`: what a run
changed, what needs you, what Yuki learned with Save / Edit / No) and the tray's **hover card** in a game
(:class:`TrayHoverCard`, the call's timer while the island is away).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from yuki.ui import lattice as L
from yuki.ui import tokens as T
from yuki.ui.surface import SurfaceWindow

CARD_W = int(T.CARD["widthPx"])            # 416
CARD_RIGHT = int(T.CARD["rightPx"])        # 20
CARD_BOTTOM = int(T.CARD["bottomPx"])      # 64 from the screen's bottom: 16 over a 48 px taskbar
PAD_Y, PAD_X = (int(x) for x in T.CARD["paddingPx"])   # 16, 18
TASKBAR_PX = 48
CARD_IN_MS = int(L.DUR["cardIn"])          # 520
LEAVE_MS = int(L.DUR["leave"])             # 420
CONFIRM_MS = int(L.DUR["confirmMorph"])    # 240
#: The call consent card's place: the design's ``.card.top`` (64 px from the top).
TOP_PX = 64


class CloseKey(L.Key):
    """The card's close key (``.card .x``): 26 px square, radius 8, its x 9 px in text-3 (text on hover)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", variant="ghost", parent=parent)
        self.setFixedSize(26, 26)
        self.setToolTip("Dismiss")

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hover = self.underMouse()
        if hover:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), 8, 8)
            p.fillPath(path, L.RAISE)
        L.paint_icon(p, QRectF(8.5, 8.5, 9, 9), "x", L.TEXT if hover else L.TEXT3)
        p.end()


class LatticeCard(SurfaceWindow):
    """A corner card window in the Lattice look (see the module docstring). Subclasses fill :attr:`content`."""

    SHADOW = 0
    corner = "bottom"

    def __init__(self, *, icon: str = "dot") -> None:
        super().__init__(activates=False, radius=20)
        self.setFixedWidth(CARD_W)
        col = QVBoxLayout(self)
        col.setContentsMargins(PAD_X + 1, PAD_Y + 1, PAD_X + 1, PAD_Y + 1)
        col.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.head_icon = L.Icon(icon, 10, L.TEXT3, self)
        head.addWidget(self.head_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.head = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self, max_lines=1)
        head.addWidget(self.head, 1, Qt.AlignmentFlag.AlignVCenter)
        self.x_button = CloseKey(self)
        head.addWidget(self.x_button, 0, Qt.AlignmentFlag.AlignVCenter)
        col.addLayout(head)
        col.addSpacing(12)                           # the header row is 26 px (its close key); then 12
        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(0)
        col.addLayout(self.content)
        self._height_anim = QVariantAnimation(self)
        self._height_anim.setDuration(CONFIRM_MS)
        self._height_anim.setEasingCurve(L.ease_curve())
        self._height_anim.valueChanged.connect(self._on_height)
        self._anchor_bottom = 0
        #: Widgets placed outside the padding (the foot reaches 8 px past it each side, as ``.card-foot``):
        #: ``(widget, slot in the layout, (left, top, right, bottom) growth)``.
        self._floats: list[tuple[QWidget, QWidget, tuple[int, int, int, int]]] = []

    def float_over(self, widget: QWidget, slot: QWidget, grow: tuple[int, int, int, int]) -> None:
        self._floats.append((widget, slot, grow))
        self._place_floats()

    def _place_floats(self) -> None:
        for w, slot, (l, t, r, b) in self._floats:
            vis = not slot.isHidden()
            w.setVisible(vis)
            if vis:
                g = slot.geometry()
                w.setGeometry(g.x() - l, g.y() - t, g.width() + l + r, g.height() + t + b)
                w.raise_()

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._place_floats()

    def event(self, e: Any) -> bool:  # noqa: D102 - a relayout moves the slots: the floats follow
        out = super().event(e)
        from PySide6.QtCore import QEvent

        if e.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show):
            self._place_floats()
        return out

    # -- look ------------------------------------------------------------------------------------------

    def set_head(self, icon: str, text: str) -> None:
        self.head_icon.name = icon
        self.head_icon.update()
        self.head.set_text(text)

    def panel_rect(self) -> Any:
        return self.rect()

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 19.5, 19.5)
        p.fillPath(path, L.BG)
        p.setPen(QPen(L.BORDER, 1.0))
        p.drawPath(path)
        p.end()

    # -- place -----------------------------------------------------------------------------------------

    def anchor(self) -> QPoint:
        """Bottom right (top right for the consent card) of the monitor in use, as the design places ``.card``."""
        from yuki.ui.island import active_screen

        screen = active_screen()
        if screen is None:
            return self.pos()
        area = screen.availableGeometry()
        x = area.right() + 1 - CARD_RIGHT - self.width()
        if self.corner == "top":
            return QPoint(x, area.top() + TOP_PX)
        return QPoint(x, area.bottom() + 1 - (CARD_BOTTOM - TASKBAR_PX) - self.height())

    def fit_height(self, *, animate: bool = False) -> None:
        """Size to the contents; a visible card grows or shrinks from its anchored edge (240 ms when ``animate``)."""
        lay = self.layout()
        lay.activate()
        want = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else lay.sizeHint().height()
        want = max(96, int(want))
        if want == self.height():
            return
        if animate and self.isVisible() and not L.reduced():
            self._anchor_bottom = self.geometry().bottom()
            self._height_anim.stop()
            self._height_anim.setStartValue(self.height())
            self._height_anim.setEndValue(want)
            self._height_anim.start()
            return
        bottom = self.geometry().bottom()
        self.setFixedHeight(want)
        if self.isVisible() and self.corner == "bottom":
            self.move(self.x(), bottom - want + 1)

    def _on_height(self, v: Any) -> None:
        h = int(v)
        self.setFixedHeight(h)
        if self.corner == "bottom":
            self.move(self.x(), self._anchor_bottom - h + 1)

    # -- in and out (the design's cardin and leave) ----------------------------------------------------

    def fade_in(self, target: QPoint) -> None:  # noqa: D102
        self._anim.stop()
        self._closing = False
        ms = 0 if L.reduced() else CARD_IN_MS
        for a in (self._opacity, self._move):
            a.setDuration(ms)
            a.setEasingCurve(L.ease_curve())
        start = self.windowOpacity() if self.isVisible() else 0.0
        if not self.isVisible():
            self.setWindowOpacity(0.0 if ms else 1.0)
            self.move(target + QPoint(0, 10 if ms else 0))
            from yuki.ui.focus import set_no_activate
            from PySide6.QtGui import QGuiApplication

            if QGuiApplication.platformName() == "windows":
                set_no_activate(int(self.winId()), True)
            self.show()
        self._opacity.setStartValue(start)
        self._opacity.setEndValue(1.0)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(target)
        self._anim.start()

    def fade_out(self) -> None:  # noqa: D102
        if not self.isVisible():
            return
        self._anim.stop()
        self._closing = True
        ms = 0 if L.reduced() else LEAVE_MS
        for a in (self._opacity, self._move):
            a.setDuration(ms)
            a.setEasingCurve(L.ease_curve())
        self._opacity.setStartValue(self.windowOpacity())
        self._opacity.setEndValue(0.0)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(self.pos() + QPoint(0, 8 if ms else 0))
        self._anim.start()


class Detail(QWidget):
    """Under a card's text: an offer's **Will / Won't** grid (the design's ``.wl``: mono labels in a 52 px column,
    13 px values, Won't in text-2), or a plain line (a question's "why"). :meth:`text` reads as the two lines
    ``Will: ...`` / ``Won't: ...`` (or the plain line), as the card said them before."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self.will_l = L.Para("Will", font=L.mono_font(11), color=L.TEXT3, line_height=19 / 11, parent=self)
        self.wont_l = L.Para("Won’t", font=L.mono_font(11), color=L.TEXT3, line_height=19 / 11, parent=self)
        self.will = L.Para("", font=L.ui_font(13), line_height=1.45, parent=self)
        self.wont = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.45, parent=self)
        self.plain = L.Para("", font=L.ui_font(12.5), color=L.TEXT2, line_height=1.45, parent=self)
        self.mode = "plain"
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def text(self) -> str:
        return self._text

    def set_lines(self, will: str, wont: str) -> None:
        self.mode = "grid"
        self.will.set_text(will)
        self.wont.set_text(wont)
        for w in (self.will_l, self.will):
            w.setVisible(bool(will))
        for w in (self.wont_l, self.wont):
            w.setVisible(bool(wont))
        self.plain.hide()
        lines = ([f"Will: {will}"] if will else []) + ([f"Won't: {wont}"] if wont else [])
        self._text = "\n".join(lines)
        self.updateGeometry()

    def set_plain(self, text: str) -> None:
        self.mode = "plain"
        self._text = text
        self.plain.set_text(text)
        self.plain.show()
        for w in (self.will_l, self.wont_l, self.will, self.wont):
            w.hide()
        self.updateGeometry()

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        if self.mode == "plain":
            return self.plain.heightForWidth(w)
        vw = max(10, w - 52 - 10)
        h = 0
        if self.will.text():
            h += max(19, self.will.heightForWidth(vw))
        if self.wont.text():
            h += (3 if h else 0) + max(19, self.wont.heightForWidth(vw))
        return h

    def sizeHint(self) -> Any:  # noqa: D102
        from PySide6.QtCore import QSize

        return QSize(CARD_W - 38, self.heightForWidth(CARD_W - 38))

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        w = self.width()
        if self.mode == "plain":
            self.plain.setGeometry(0, 0, w, self.plain.heightForWidth(w))
            return
        vw = max(10, w - 52 - 10)
        y = 0
        for lab, val in ((self.will_l, self.will), (self.wont_l, self.wont)):
            if not val.text():
                continue
            vh = val.heightForWidth(vw)
            lab.setGeometry(0, y, 52, 19)
            val.setGeometry(62, y + (19 - int(round(13 * 1.45))) // 2, vw, vh)
            y += max(19, vh) + 3


class ConfirmRow(QWidget):
    """The design's ``.confirm``: a live mark, the words, and an optional key (Undo) at the end; it fades in 4 px
    up over 260 ms when it replaces a card's contents."""

    def __init__(self, parent: QWidget | None = None, *, icon: str = "check") -> None:
        super().__init__(parent)
        self.icon = L.Icon(icon, 12, L.live(), self)
        self.words = L.Para("", font=L.ui_font(13.5), line_height=1.45, parent=self)
        self.keys: list[L.Key] = []
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def add_key(self, key: L.Key) -> None:
        key.setParent(self)
        self.keys.append(key)

    def set_text(self, text: str, icon: str = "check") -> None:
        self.icon.name = icon
        self.icon.update()
        self.words.set_text(text)
        self.updateGeometry()

    def setText(self, text: str) -> None:  # noqa: N802 - a QLabel's name, for the card's older callers
        self.set_text(text, self.icon.name)

    def text(self) -> str:
        return self.words.text()

    def _keys_w(self) -> int:
        vis = [k for k in self.keys if not k.isHidden()]
        return sum(k.width() for k in vis) + 8 * len(vis)

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return max(30, 8 + self.words.heightForWidth(max(10, w - 22 - self._keys_w())))

    def sizeHint(self) -> Any:  # noqa: D102
        from PySide6.QtCore import QSize

        return QSize(CARD_W - 38, self.heightForWidth(CARD_W - 38))

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        w, h = self.width(), self.height()
        kw = self._keys_w()
        th = self.words.heightForWidth(max(10, w - 22 - kw))
        self.icon.move(0, (h - 12) // 2)
        self.words.setGeometry(22, (h - th) // 2, max(10, w - 22 - kw), th)
        x = w
        for k in reversed([k for k in self.keys if not k.isHidden()]):
            x -= k.width()
            k.move(x, (h - k.height()) // 2)
            x -= 8


class CardFoot(QWidget):
    """The design's ``.card-foot``: a hair rule, then ghost keys on the left and a mono note on the right (it
    reaches 8 px past the card's padding each side: place it with :meth:`LatticeCard.float_over`). ``rule=False,
    align="right"``: the question card's second row (keys on the right, no rule)."""

    def __init__(self, parent: QWidget | None = None, *, rule: bool = True, align: str = "left") -> None:
        super().__init__(parent)
        self.rule, self.align = rule, align
        self.keys: list[L.Key] = []
        self.note = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self, max_lines=1)
        self.setFixedHeight(1 + 10 + 22 if rule else 22)

    def set_keys(self, keys: list[L.Key], note: str = "") -> None:
        for k in self.keys:
            if k not in keys:
                k.hide()
        self.keys = list(keys)
        for k in keys:
            k.setParent(self)
            k.show()
        self.note.set_text(note)
        self.note.setVisible(bool(note))
        self._place()

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        top = 11 if self.rule else 0
        vis = [k for k in self.keys if not k.isHidden()]
        if self.align == "right":
            x = self.width()
            for k in reversed(vis):
                x -= k.width()
                k.move(x, top)
                x -= 2
        else:
            x = 0
            for k in vis:
                k.move(x, top)
                x += k.width() + 2
        if self.note.text():
            nw = int(self.note.natural_width()) + 2
            self.note.setGeometry(self.width() - 8 - nw, top + 6, nw, 11)

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        if not self.rule:
            return
        p = QPainter(self)
        p.fillRect(QRectF(0, 0, self.width(), 1), L.HAIR)
        p.end()


class ReturnCard(LatticeCard):
    """What a run changed (the design's return card): the outcome, **Changed** (each with "can't undo from here"),
    **Needs you**, a **Learned** proposal from practice memory with Save / Edit / No (applied when the card goes, so
    Undo can take it back), the cost and Why. A run with nothing to report shows no card (the tray decides).

    Signals:
        learned(ref, action, words): ``save`` | ``edit`` | ``forget`` for a proposal, once the card is gone.
        why(): Why (the Activity page).
    """

    learned = Signal(str, str, str)
    why = Signal()

    def __init__(self) -> None:
        super().__init__(icon="check")
        self.setObjectName("YukiReturnCard")
        self.title = L.Para("", font=L.ui_font(17, 600), line_height=1.45, parent=self)
        self.content.addWidget(self.title)
        self.changed_l = L.Para("Changed", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self)
        self.changed_box = QVBoxLayout()
        self.changed_box.setSpacing(0)
        self.needs_l = L.Para("Needs you", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self)
        self.needs = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.45, parent=self)
        self.content.addSpacing(12)
        self.content.addWidget(self.changed_l)
        self.content.addSpacing(6)
        self.content.addLayout(self.changed_box)
        self.content.addSpacing(12)
        self.content.addWidget(self.needs_l)
        self.content.addSpacing(6)
        self.content.addWidget(self.needs)
        self.learn = LearnedBox(self)
        self.learn.decided.connect(self._on_decided)
        self.content.addSpacing(12)
        self.content.addWidget(self.learn)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 12, 0, 0)
        self.cost = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self, max_lines=1)
        self.cost.setFixedHeight(22)
        foot.addWidget(self.cost, 1)
        self.why_key = L.Key("Why", variant="ghost", size="xs", parent=self)
        self.why_key.clicked.connect(self.why.emit)
        foot.addWidget(self.why_key)
        self.content.addLayout(foot)
        self.x_button.clicked.connect(self.close_card)
        self._rows: list[QWidget] = []
        self._pending: tuple[str, str, str] | None = None
        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.close_card)
        self.faded_out.connect(self._apply)

    def show_run(self, *, title: str, at: str, changed: list[str], needs: str = "", learned: dict | None = None,
                 cost: str = "", linger_ms: int = 30_000) -> None:
        self.set_head("check", f"Done · {at}" if at else "Done")
        self.title.set_text(title)
        for w in self._rows:
            w.deleteLater()
        self._rows = []
        for text in changed:
            row = ChangedRow(text, self)
            self.changed_box.addWidget(row)
            self._rows.append(row)
        for w in (self.changed_l,):
            w.setVisible(bool(changed))
        self.needs.set_text(needs)
        self.needs_l.setVisible(bool(needs))
        self.needs.setVisible(bool(needs))
        self.learn.set_learned(learned)
        self.cost.set_text(cost)
        self._pending = None
        self.fit_height()
        self.fade_in(self.anchor())
        self._linger.start(linger_ms)

    def enterEvent(self, e: Any) -> None:  # noqa: D102 - read: the clock stops
        super().enterEvent(e)
        self._linger.stop()

    def leaveEvent(self, e: Any) -> None:  # noqa: D102
        super().leaveEvent(e)
        if self.isVisible() and not self.closing:
            self._linger.start(12_000)

    def _on_decided(self, ref: str, action: str, words: str) -> None:
        self._pending = (ref, action, words) if action else None
        self.fit_height(animate=True)

    def close_card(self) -> None:
        self._linger.stop()
        self.fade_out()

    def _apply(self) -> None:
        if self._pending is not None:
            ref, action, words = self._pending
            self._pending = None
            self.learned.emit(ref, action, words)


class ChangedRow(QWidget):
    """``.chg``: what changed, and the flag "can't undo from here"."""

    def __init__(self, text: str, parent: QWidget | None = None, flag: str = "can’t undo from here") -> None:
        super().__init__(parent)
        self.text = L.Para(text, font=L.ui_font(13), line_height=1.45, parent=self)
        self.flag_text = flag
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _flag_w(self) -> int:
        return int(QFontMetricsF(L.mono_font(10.5)).horizontalAdvance(self.flag_text)) + 12 + 2

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return max(20, self.text.heightForWidth(max(10, w - self._flag_w() - 10)))

    def sizeHint(self) -> Any:  # noqa: D102
        from PySide6.QtCore import QSize

        return QSize(CARD_W - 38, self.heightForWidth(CARD_W - 38))

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        tw = max(10, self.width() - self._flag_w() - 10)
        self.text.setGeometry(0, 0, tw, self.text.heightForWidth(tw))

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fw = self._flag_w()
        r = QRectF(self.width() - fw + 0.5, (self.height() - 17) / 2 + 0.5, fw - 1, 17 - 1)
        p.setPen(QPen(L.HAIR2, 1.0))
        p.drawRoundedRect(r, 7.5, 7.5)
        p.setPen(L.TEXT2)
        p.setFont(L.mono_font(10.5))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.flag_text)
        p.end()


class LearnedBox(QWidget):
    """The design's ``.learned``: a raised box with "Learned", the lesson, why, and Save / Edit / No; each choice
    morphs to a confirmation with Undo. Emits ``decided(ref, action, words)`` (``action`` "" after Undo)."""

    decided = Signal(str, str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ref = ""
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(15, 13, 15, 13)
        self.col.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(L.Icon("lesson", 10, L.TEXT3, self))
        head.addWidget(L.Para("Learned", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self), 1)
        self.col.addLayout(head)
        self.col.addSpacing(6)
        self.text = L.Para("", font=L.ui_font(14), line_height=1.45, parent=self)
        self.col.addWidget(self.text)
        self.why = L.Para("", font=L.ui_font(12), color=L.TEXT3, line_height=1.45, parent=self)
        self.col.addSpacing(3)
        self.col.addWidget(self.why)
        self.edit = None
        acts = QHBoxLayout()
        acts.setContentsMargins(0, 10, 0, 0)
        acts.setSpacing(6)
        self.k_save = L.Key("Save", variant="pri", size="sm", parent=self)
        self.k_edit = L.Key("Edit", size="sm", parent=self)
        self.k_no = L.Key("No", variant="ghost", size="sm", parent=self)
        for k in (self.k_save, self.k_edit, self.k_no):
            acts.addWidget(k)
        acts.addStretch(1)
        self.acts = acts
        self.col.addLayout(acts)
        self.confirm = ConfirmRow(self, icon="lesson")
        self.k_undo = L.Key("Undo", variant="ghost", size="xs")
        self.confirm.add_key(self.k_undo)
        self.confirm.hide()
        self.col.addWidget(self.confirm)
        self.k_save.clicked.connect(lambda: self._decide("save"))
        self.k_no.clicked.connect(lambda: self._decide("forget"))
        self.k_edit.clicked.connect(self._edit)
        self.k_undo.clicked.connect(self._undo)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def set_learned(self, learned: dict | None) -> None:
        self.setVisible(bool(learned))
        if not learned:
            return
        self.ref = str(learned.get("id") or "")
        self.text.set_text(str(learned.get("text") or learned.get("title") or ""))
        why = str(learned.get("why") or "")
        self.why.set_text(why)
        self.why.setVisible(bool(why))
        self._show_choices(True)

    def _show_choices(self, on: bool) -> None:
        for w in (self.text, self.why, self.k_save, self.k_edit, self.k_no):
            w.setVisible(on and (w is not self.why or bool(self.why.text())))
        self.confirm.setVisible(not on)
        if self.edit is not None:
            self.edit.setVisible(False)

    def _decide(self, action: str, words: str = "") -> None:
        text = {"save": "Saved to your playbook. I’ll show it when I use it.",
                "forget": "Dropped. I won’t keep that.",
                "edit": f"Saved your version: “{words}”"}[action]
        self.confirm.set_text(text, "dash" if action == "forget" else "lesson")
        self._show_choices(False)
        self.decided.emit(self.ref, action, words)

    def _edit(self) -> None:
        from yuki.ui.island import TypingInput

        if self.edit is None:
            self.edit = TypingInput("Say it your way", self)
            self.edit.setFixedHeight(36)
            self.edit.submitted.connect(lambda words: self._decide("edit", words))
            self.col.insertWidget(self.col.indexOf(self.text) + 1, self.edit)
        self.edit.setText(self.text.text())
        self.edit.show()
        self.edit.begin_typing()

    def _undo(self) -> None:
        self._show_choices(True)
        self.decided.emit(self.ref, "", "")

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return self.col.totalHeightForWidth(w)

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 11.5, 11.5)
        p.fillPath(path, L.RAISE)
        p.setPen(QPen(L.HAIR2, 1.0))
        p.drawPath(path)
        p.end()


class TrayHoverCard(LatticeCard):
    """The tray's hover card while the island is away for a full screen (the design's ``.trayhover``): "Yuki · game
    quiet", what is in front, the call's running time in Doto (the same clock as the island's), and a note. Placed
    over the tray, never focused, transparent to clicks."""

    def __init__(self) -> None:
        super().__init__(icon="memory")
        self.setObjectName("YukiTrayHover")
        self.setFixedWidth(292)
        self.layout().setContentsMargins(17, 15, 17, 15)
        self.x_button.hide()
        self.tt = L.Para("", font=L.ui_font(14, 600), line_height=1.45, parent=self)
        self.content.addSpacing(8 - 7)
        self.content.addWidget(self.tt)
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 0)
        self.row_text = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.45, parent=self, max_lines=1)
        row.addWidget(self.row_text, 1, Qt.AlignmentFlag.AlignVCenter)
        self.timer = L.Para("", font=L.doto_font(22, 800, 0.05), color=L.live(), line_height=1.2, parent=self,
                            max_lines=1)
        row.addWidget(self.timer, 0, Qt.AlignmentFlag.AlignVCenter)
        self.content.addLayout(row)
        self.note = L.Para("No cards, no capture in the game. The timer keeps running here.", font=L.ui_font(12),
                           color=L.TEXT3, line_height=1.45, parent=self)
        self.content.addSpacing(6)
        self.content.addWidget(self.note)

    def paintEvent(self, _e: Any) -> None:  # noqa: D102 - 14 px corners
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 13.5, 13.5)
        p.fillPath(path, L.BG)
        p.setPen(QPen(L.BORDER, 1.0))
        p.drawPath(path)
        p.end()

    def set_card(self, *, what: str, call: str, timer: str, game: bool = True) -> None:
        self.set_head("memory", "Yuki · game quiet" if game else "Yuki")
        self.tt.set_text(what)
        self.row_text.set_text(call)
        self.timer.set_text(timer)
        self.timer.set_color(L.live())
        self.timer.setFixedWidth(int(self.timer.natural_width()) + 4)
        self.row_text.setVisible(bool(call))
        self.timer.setVisible(bool(timer))
        self.fit_height()

    def place_over(self, tray_rect: Any) -> None:
        """Above the tray icon (the design: 116 px from the right, 58 px from the bottom, i.e. over the taskbar)."""
        from yuki.ui.island import active_screen

        screen = active_screen()
        geo = screen.geometry() if screen is not None else self.geometry()
        x = (tray_rect.center().x() - self.width() // 2) if tray_rect is not None and tray_rect.width() else (
            geo.right() + 1 - 116 - self.width())
        x = max(geo.left() + 8, min(x, geo.right() + 1 - 8 - self.width()))
        y = geo.bottom() + 1 - 58 - self.height()
        self.fade_in(QPoint(x, y))


__all__ = ["CARD_W", "CardFoot", "CloseKey", "ChangedRow", "ConfirmRow", "Detail", "LatticeCard", "LearnedBox", "ReturnCard",
           "TrayHoverCard"]
