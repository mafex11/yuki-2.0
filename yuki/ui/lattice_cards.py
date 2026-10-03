"""The corner cards in the Lattice design (phase E8): the base every card window shares, and the cards that are new.

:class:`LatticeCard` is the design's ``.card``: 416 px wide, the field colour with a 1 px ``#2A2D31`` border and 20 px
corners (no shadow: the design has none), 16 x 18 px padding, a header of a dot icon, a mono label and a close key;
bottom right of the monitor in use (16 px over the taskbar, 20 px from the edge; the call consent card sits top
right). It comes in as the design's ``cardin`` (520 ms, a 10 px rise, its ease) and leaves as its ``leave`` (420 ms,
an 8 px drop), never takes focus, and morphs its height in 240 ms when its contents change (a confirmation in place).

The cards built on it: the offer, question, break and notice cards (:class:`yuki.ui.nudges.NudgeCard`), the call
consent card (:class:`yuki.ui.calls.CallConsentCard`), and here the **return card** (:class:`ReturnCard`: how a run
really ended, what it changed in plain words, what needs you, what Yuki learned with Save / Edit / No), the tray's
**hover card** (:class:`TrayHoverCard`, in every state: the state line, the running task with Stop, Last, the Listen
for "Yuki" switch and Open Yuki) and its right-click menu (:class:`TrayMenu`).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QAction, QFontMetricsF, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy, QVBoxLayout, QWidget

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
        self._head_gap = col.itemAt(col.count() - 1).spacerItem()
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
    """How a run ended (the design's return card, revamp 38 §8b-c): the header by the real outcome ("Done", "Stopped",
    "Didn't finish", "Handed back") and the time, one plain sentence, **Changed** (the effects the user cares about in
    plain words, the opening steps folded into one line, "+N more"), **Needs you**, a **Learned** proposal from
    practice memory with Save / Edit / No (applied when the card goes), and a footer of the run's time and Why.
    No undo of any kind: no chips, no Undo keys, no "can't undo" text (the user, 3 Oct 19:10). A run with nothing to
    tell shows no card (the tray decides, :func:`yuki.ui.step_words.return_view`).

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
        self.more = L.Para("", font=L.ui_font(12), color=L.TEXT3, line_height=1.45, parent=self)
        self.content.addWidget(self.more)
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
        #: The run's time ("41 s"); its steps and cost are behind Why (the Activity page).
        self.took = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self, max_lines=1)
        self.took.setFixedHeight(22)
        foot.addWidget(self.took, 1)
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

    def show_run(self, *, title: str, at: str, rows: list[dict[str, str]], icon: str = "check", head: str = "Done",
                 more: int = 0, needs: str = "", learned: dict | None = None, took: str = "",
                 linger_ms: int = 30_000) -> None:
        """Show a finished run (:func:`yuki.ui.step_words.return_view` gives ``icon``, ``head``, ``title``, ``rows``
        and ``more``).

        Args:
            title: One plain sentence: the answer, or how it stopped ("Stopped before it finished. Nothing was sent.").
            at: When it ended, as people say it ("1:28 PM").
            rows: ``{"text", "icon"}`` per change (``check`` happened, ``dash`` did not).
            icon, head: The header by the real outcome.
            more: Changes that did not fit ("+2 more").
            needs: What needs the user, if anything.
            learned: A practice-memory proposal to Save / Edit / No.
            took: The run's time ("41 s").
        """
        self.set_head(icon, f"{head} · {at}" if at else head)
        self.title.set_text(title)
        for w in self._rows:
            w.deleteLater()
        self._rows = []
        for r in rows:
            row = ChangedRow(str(r.get("text") or ""), self, icon=str(r.get("icon") or "check"))
            self.changed_box.addWidget(row)
            self._rows.append(row)
        self.changed_l.setVisible(bool(rows))
        self.more.set_text(f"+{more} more" if more > 0 else "")
        self.more.setVisible(more > 0)
        self.needs.set_text(needs)
        self.needs_l.setVisible(bool(needs))
        self.needs.setVisible(bool(needs))
        self.learn.set_learned(learned)
        self.took.set_text(took)
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
    """``.chg``: one change in plain words after a small mark (``check``: it happened; ``dash``: it did not). Nothing
    else: no undo chip, no Undo key, no "can't undo" flag."""

    #: The text starts this far in (the mark and its gap).
    INDENT = 18

    def __init__(self, text: str, parent: QWidget | None = None, *, icon: str = "check") -> None:
        super().__init__(parent)
        self.icon = L.Icon(icon, 10, L.TEXT3, self)
        self.text = L.Para(text, font=L.ui_font(13), line_height=1.45, parent=self)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return max(20, self.text.heightForWidth(max(10, w - self.INDENT)))

    def sizeHint(self) -> Any:  # noqa: D102
        from PySide6.QtCore import QSize

        return QSize(CARD_W - 38, self.heightForWidth(CARD_W - 38))

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        tw = max(10, self.width() - self.INDENT)
        self.icon.move(0, 5)
        self.text.setGeometry(self.INDENT, 0, tw, self.text.heightForWidth(tw))


class LearnedBox(QWidget):
    """The design's ``.learned``: a raised box with "Learned", the lesson, why, and Save / Edit / No; each choice
    morphs to a one-line confirmation (no Undo on the return card: the user, 3 Oct 19:10). Emits
    ``decided(ref, action, words)``."""

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
        self.confirm.hide()
        self.col.addWidget(self.confirm)
        self.k_save.clicked.connect(lambda: self._decide("save"))
        self.k_no.clicked.connect(lambda: self._decide("forget"))
        self.k_edit.clicked.connect(self._edit)
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


class Toggle(QWidget):
    """The design's ``.tog`` switch: 34 x 20, a 12 px knob; on = lit bone with a dark knob on the right."""

    toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 20)
        self.on = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Listen for Yuki")

    def set_on(self, on: bool) -> None:
        if bool(on) != self.on:
            self.on = bool(on)
            self.update()

    def mouseReleaseEvent(self, e: Any) -> None:  # noqa: D102
        if self.rect().contains(e.position().toPoint()):
            self.on = not self.on
            self.update()
            self.toggled.emit(self.on)

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(L.LIT if self.on else L.HAIR2, 1.0))
        p.setBrush(L.LIT if self.on else L.RAISE)
        p.drawRoundedRect(r, 9.5, 9.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.BG if self.on else L.TEXT3)
        p.drawEllipse(QRectF(3 + (14 if self.on else 0), 4, 12, 12))
        p.end()


class DotProgress(QWidget):
    """The design's ``.dprog``: one 7 px dot per step (4 px apart): done lit, the current one live, the rest a ring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.done, self.total, self.cur = 0, 0, -1
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(7)

    def set_progress(self, done: int, total: int, cur: int = -1) -> None:
        self.done, self.total, self.cur = int(done), max(0, int(total)), int(cur)
        self.setFixedWidth(max(1, self.total * 11 - 4))
        self.update()

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for k in range(self.total):
            r = QRectF(k * 11 + 0.5, 0.5, 6, 6)
            if k < self.done:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(L.LIT)
            elif k == self.cur:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(L.live())
            else:
                p.setPen(QPen(L.KEY_HOVER_BORDER, 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(r)
        p.end()


class TrayHoverCard(QWidget):
    """The tray's hover card in every state (the mock's ``.traycard``, 318 px; 270 in a game): the face and the state
    line, the running task with Stop (or the question with Answer, or the broken voice with Retry, or "Nothing
    running"), Last, the Listen for “Yuki” switch (its off words under it), and Open Yuki. It grows out of the icon's
    square (a 300 ms clip morph, its contents fading in from 35 %) and folds back in place (240 ms). Never focused."""

    stop = Signal()
    answer = Signal()
    retry = Signal()
    open_yuki = Signal()
    listen_toggled = Signal(bool)
    hovered = Signal(bool)

    W, W_GAME, PAD_X, PAD_Y = 318, 270, 16, 14

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.setObjectName("YukiTrayCard")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        from yuki.ui.dots import FaceWidget
        from yuki.ui.tray import LISTEN_OFF_TEXT

        self.content = QWidget(self)
        C = self.content
        self.face = FaceWidget(4, C)
        self.line = L.Para("", font=L.ui_font(14, 600), line_height=1.3, parent=C, max_lines=2)
        self.sub = L.Para("", font=L.mono_font(11, 500), color=L.TEXT3, line_height=1.35, parent=C, max_lines=2)
        self.spin = L.Spinner(11, C)
        self.task_icon = L.Icon("lesson", 11, L.live(), C)
        self.task = L.Para("", font=L.ui_font(13), line_height=1.4, parent=C, max_lines=2)
        self.prog = DotProgress(C)
        self.k_stop = L.Key("Stop", variant="stop", size="xs", icon="stop", parent=C)
        self.k_answer = L.Key("Answer", variant="pri", size="xs", parent=C)
        self.k_retry = L.Key("Retry", variant="pri", size="xs", parent=C)
        self.idle = L.Para("Nothing running", font=L.ui_font(13), color=L.TEXT3, line_height=1.4, parent=C, max_lines=1)
        self.last_l = L.Para("Last", font=L.mono_font(11, 500), color=L.TEXT3, line_height=1.2, parent=C, max_lines=1)
        self.last = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.4, parent=C, max_lines=1)
        self.listen_l = L.Para("Listen for “Yuki”", font=L.ui_font(13), line_height=1.4, parent=C, max_lines=1)
        self.tog = Toggle(C)
        self.off_text = L.Para(LISTEN_OFF_TEXT, font=L.ui_font(12), color=L.TEXT3, line_height=1.45, parent=C)
        self.k_open = L.Key("Open Yuki", variant="pri", size="sm", icon="memory", parent=C)
        self.more = L.Para("right-click for more", font=L.mono_font(11, 500), color=L.TEXT3, line_height=1.2,
                           parent=C, max_lines=1)
        self.k_stop.clicked.connect(self.stop.emit)
        self.k_answer.clicked.connect(self.answer.emit)
        self.k_retry.clicked.connect(self.retry.emit)
        self.k_open.clicked.connect(self.open_yuki.emit)
        self.tog.toggled.connect(self._on_tog)
        from yuki.ui.tray import TrayCardInfo

        self.info = TrayCardInfo()
        self._card_h = 200
        self._below = False
        self._icon = QRect()            # the icon's square in this window's coordinates
        self._k = 1.0                   # the clip morph: 0 = the icon's square, 1 = the whole card
        self._content_a = 1.0
        self._mode = ""                 # "open" | "fold" | ""
        self.closing = False
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._anim_done)

    # -- contents ----------------------------------------------------------------------------------------------

    def width_now(self) -> int:
        return self.W_GAME if self.info.game else self.W

    def set_info(self, info: Any) -> None:
        from yuki.ui.tray import TRAY_STATES

        self.info = info
        face = {"resting": "idle", "stopped": "voice-stopped", "call-ask": "quiet-in-call", "call": "quiet-in-call",
                "off": "not-listening", "game": "game-quiet"}.get(info.state, info.state)
        self.face.set_state(face if info.state in TRAY_STATES else "idle",
                            (max(1, info.cur + 1), max(1, info.of)) if info.of else None)
        at = info.line.find(info.timer) if getattr(info, "timer", "") else -1
        self.line.set_text(info.line, [L.Span(at, len(info.timer), color=L.live())] if at >= 0 else None)
        self.sub.set_text(info.sub)
        self.sub.setVisible(bool(info.sub))
        kind = info.task_kind if info.task else ""
        self.spin.setVisible(kind == "run")
        self.task_icon.setVisible(kind in ("question", "voice"))
        self.task_icon.name = "x" if kind == "voice" else "lesson"
        self.task.set_text(info.task if kind else "")
        self.task.setVisible(bool(kind))
        self.prog.setVisible(kind == "run" and info.of > 0)
        if kind == "run":
            self.prog.set_progress(info.done, info.of, info.cur)
        self.k_stop.setVisible(kind == "run")
        self.k_answer.setVisible(kind == "question")
        self.k_retry.setVisible(kind == "voice")
        self.idle.setVisible(not kind)
        self.last.set_text(info.last)
        self.tog.set_on(info.listen)
        self.off_text.setVisible(not info.listen)
        self.more.setVisible(not info.game)
        self._layout()

    def _on_tog(self, on: bool) -> None:
        self.info.listen = bool(on)
        self.off_text.setVisible(not on)
        self._layout()
        self.listen_toggled.emit(bool(on))

    def _layout(self) -> int:
        """Place the rows (the mock's spacing) and size the window; returns the card's height."""
        w = self.width_now()
        px, py = self.PAD_X + 1, self.PAD_Y + 1
        inner = w - 2 * px
        y = py
        fh = self.face.height()
        tx = px + self.face.width() + 12
        tw = inner - (tx - px)
        lh = self.line.heightForWidth(tw)
        sh = self.sub.heightForWidth(tw) if self.sub.isVisibleTo(self.content) else 0
        block = lh + (3 + sh if sh else 0)
        head = max(fh, block)
        self.face.move(px, y + (head - fh) // 2)
        self.line.setGeometry(tx, y + (head - block) // 2, tw, lh)
        if sh:
            self.sub.setGeometry(tx, y + (head - block) // 2 + lh + 3, tw, sh)
        y += head
        self._task_rect = None
        if self.task.isVisibleTo(self.content):
            y += 12
            key = next((k for k in (self.k_stop, self.k_answer, self.k_retry) if k.isVisibleTo(self.content)), None)
            kw = key.width() if key is not None else 0
            col_x = px + 1 + 12 + 14 + 10
            col_w = max(10, inner - 2 - 24 - 14 - 10 - (10 + kw if kw else 0))
            th = self.task.heightForWidth(col_w)
            ph = (5 + 7) if self.prog.isVisibleTo(self.content) else 0
            box_h = 1 + 10 + max(th + ph, key.height() if key is not None else 0) + 10 + 1
            self._task_rect = QRectF(px + 0.5, y + 0.5, inner - 1, box_h - 1)
            cy = y + box_h // 2
            self.spin.move(px + 1 + 12 + 1, cy - 6)
            self.task_icon.move(px + 1 + 12 + 1, cy - 6)
            ty = y + (box_h - (th + ph)) // 2
            self.task.setGeometry(col_x, ty, col_w, th)
            if ph:
                self.prog.move(col_x, ty + th + 5)
            if key is not None:
                key.move(px + inner - 1 - 12 - kw, cy - key.height() // 2)
            y += box_h
        else:
            y += 10
            ih = int(round(13 * 1.4))
            self.idle.setGeometry(px, y, inner, ih)
            y += ih
        y += 10
        rh = int(round(13 * 1.4))
        lw = int(self.last_l.natural_width()) + 2
        self.last_l.setGeometry(px, y + (rh - 14) // 2, lw, 14)
        self.last.setGeometry(px + lw + 10, y, inner - lw - 10, rh)
        self.last._elide_left = False
        y += rh + 10
        self.listen_l.setGeometry(px, y, inner - 34 - 10, rh)
        self.tog.move(px + inner - 34, y + (rh - 20) // 2)
        y += rh
        if self.off_text.isVisibleTo(self.content):
            y += 6
            oh = self.off_text.heightForWidth(inner)
            self.off_text.setGeometry(px, y, inner, oh)
            y += oh
        y += 12
        self._foot_y = y
        y += 10
        self.k_open.move(px, y)
        mw = int(self.more.natural_width()) + 2
        self.more.setGeometry(px + inner - mw, y + (self.k_open.height() - 14) // 2, mw, 14)
        y += self.k_open.height() + py
        self._card_h = y
        self._fit_window()
        return y

    def _fit_window(self) -> None:
        """The window is the card alone: it never reaches over the tray icon (a layered window hit-tests by alpha, so
        a box painted over the icon would take the pointer from it and the card could fold and reopen under a still
        pointer)."""
        w, h = self.width_now(), self._card_h
        self.setFixedSize(w, h)
        self.content.setGeometry(0, 0, w, h)

    def card_rect(self) -> QRectF:
        """The card's box in window coordinates."""
        return QRectF(0, 0, self.width(), self._card_h)

    # -- placing, growing and folding -------------------------------------------------------------------------

    def open_from(self, icon_rect: QRect) -> None:
        """Grow out of ``icon_rect`` (the tray icon's square, logical px): above it like Windows (10 px clear of it),
        below when the taskbar is at the top; the card's right edge 14 px past the icon's. The clip morph starts from
        the icon's square at the card's edge next to the icon (in line with it), never over the icon itself."""
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.screenAt(icon_rect.center()) or QGuiApplication.primaryScreen()
        area = screen.geometry() if screen is not None else QRect(0, 0, 1920, 1080)
        self._below = icon_rect.top() - 10 - self._card_h < area.top() + 8
        self._layout()
        w, h = self.width(), self.height()
        x = max(area.left() + 8, min(icon_rect.right() + 1 + 14 - w, area.right() + 1 - 8 - w))
        y = icon_rect.bottom() + 1 + 10 if self._below else icon_rect.top() - 10 - h
        self.move(x, y)
        s = max(16, icon_rect.height())
        sx = max(0, min(icon_rect.left() - x, w - s))
        self._icon = QRect(sx, 0 if self._below else h - s, s, s)
        self.closing = False
        self._anim.stop()
        if L.reduced():
            self._k, self._content_a, self._mode = 1.0, 1.0, ""
            self._apply_content()
            self._show()
            return
        self._mode = "open"
        self._k, self._content_a = 0.0, 0.0
        self._apply_content()
        self._show()
        self._anim.setDuration(340)
        self._anim.setEasingCurve(L.ease_curve())
        self._anim.start()

    def _show(self) -> None:
        self.show()
        if QGuiApplication_platform() == "windows":
            from yuki.ui.focus import set_no_activate

            set_no_activate(int(self.winId()), True)
            try:
                from yuki.ui.focus import assert_topmost

                assert_topmost(int(self.winId()))   # shown again at its old z-order place otherwise (as the island)
            except Exception:
                pass
        self.update()

    def fold(self) -> None:
        """Fold back toward the icon in place and fade (240 ms), then hide."""
        if not self.isVisible() or self.closing:
            return
        self.closing = True
        if L.reduced():
            self._anim_done_fold()
            return
        self._mode = "fold"
        self._anim.stop()
        self._anim.setDuration(240)
        self._anim.setEasingCurve(L.ease_curve())
        self._anim.start()

    def _on_anim(self, v: Any) -> None:
        t = float(v)
        if self._mode == "open":
            # the clip morph runs 300 of the 340 ms; the contents wait until 35 %, then fade in
            self._k = L.ease(min(1.0, t * 340 / 300))
            self._content_a = max(0.0, (t - 0.35) / 0.65)
        elif self._mode == "fold":
            self._k = 1.0 - t
            self._content_a = max(0.0, 1.0 - t / 0.55)
        self._apply_content()
        self.update()

    def _anim_done(self) -> None:
        if self._mode == "fold":
            self._anim_done_fold()
            return
        self._k, self._content_a, self._mode = 1.0, 1.0, ""
        self._apply_content()
        self.update()

    def _anim_done_fold(self) -> None:
        self._mode = ""
        self.hide()
        self.closing = False
        self._k, self._content_a = 1.0, 1.0
        self._apply_content()

    def _apply_content(self) -> None:
        a = max(0.0, min(1.0, self._content_a))
        eff = self.content.graphicsEffect()
        if a >= 0.999:
            if eff is not None:
                self.content.setGraphicsEffect(None)
        else:
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            if not isinstance(eff, QGraphicsOpacityEffect):
                eff = QGraphicsOpacityEffect(self.content)
                self.content.setGraphicsEffect(eff)
            eff.setOpacity(a)

    def _clip(self) -> tuple[QRectF, float]:
        """The card's box now: from the icon's square at the card's edge next to the icon (it grows and folds there)
        to the whole card."""
        full = self.card_rect()
        k = max(0.0, min(1.0, self._k))
        if k >= 0.999:
            return full, 13.5
        s = float(self._icon.height() or 24)
        start = QRectF(float(self._icon.left()), float(self._icon.top()), s, s)
        r = QRectF(start.left() + (full.left() - start.left()) * k, start.top() + (full.top() - start.top()) * k,
                   start.width() + (full.width() - start.width()) * k,
                   start.height() + (full.height() - start.height()) * k)
        return r, 6 + (13.5 - 6) * k

    def paintEvent(self, _e: Any) -> None:  # noqa: D102 - the card's box, 14 px corners (clipped while it grows)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r, rad = self._clip()
        r = r.adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        p.fillPath(path, L.BG)
        p.setPen(QPen(L.BORDER, 1.0))
        p.drawPath(path)
        if self._k >= 0.999 and getattr(self, "_foot_y", 0):
            top = self.card_rect().top()
            if getattr(self, "_task_rect", None) is not None:
                p.setPen(QPen(L.HAIR2, 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(self._task_rect.translated(0, top), 9.5, 9.5)
            p.fillRect(QRectF(self.PAD_X + 1, top + self._foot_y, self.width() - 2 * (self.PAD_X + 1), 1), L.HAIR)
        p.end()

    def enterEvent(self, e: Any) -> None:  # noqa: D102
        super().enterEvent(e)
        self.hovered.emit(True)

    def leaveEvent(self, e: Any) -> None:  # noqa: D102
        super().leaveEvent(e)
        self.hovered.emit(False)

    def keyPressEvent(self, e: Any) -> None:  # noqa: D102
        if e.key() == Qt.Key.Key_Escape:
            self.fold()
            return
        super().keyPressEvent(e)


def QGuiApplication_platform() -> str:  # noqa: N802 - a tiny helper
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.platformName()


#: The tray menu's look (the mock's ``.traymenu``): 252 px, the field colour, a ``#34373C`` border, 12 px corners,
#: 32 px rows with 8 px corners, hover ``raise-2``, the right-hand state in mono.
TRAY_MENU_CSS = (
    "QMenu {{ background: {bg}; border: 1px solid #34373C; border-radius: 12px; padding: 5px; color: {text};"
    " }}"
    "QMenu::item {{ height: 32px; padding: 0 12px 0 8px; border-radius: 8px; background: transparent; }}"
    "QMenu::item:selected {{ background: {raise2}; }}"
    "QMenu::item:disabled {{ color: {text3}; }}"
    "QMenu::icon {{ padding-left: 10px; }}"
    "QMenu::separator {{ height: 1px; background: {hair}; margin: 4px 6px; }}"
    "QMenu::right-arrow {{ width: 8px; height: 8px; }}"
)


class TrayMenu(QMenu):
    """The tray's right-click menu in the Lattice look: each item a dot icon, its words and (on the right, as the
    item's shortcut text) its state ("on", "off", "click"). Closes on Esc and on a click elsewhere (a Qt menu)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("YukiTrayMenu")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        self.setMinimumWidth(252)
        self.setFont(L.ui_font(13))
        self.setStyleSheet(TRAY_MENU_CSS.format(bg=L.css(L.BG), text=L.css(L.TEXT), text3=L.css(L.TEXT3),
                                                raise2=L.css(L.RAISE2), hair=L.css(L.HAIR)))
        self._words: dict[int, str] = {}

    @staticmethod
    def dot_icon(name: str, color: Any = None) -> QIcon:
        icon = QIcon()
        for size, dpr in ((11, 1.0), (11, 1.5), (11, 2.0)):
            pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            L.paint_icon(p, QRectF(0, 0, size, size), name, color or L.TEXT3)
            p.end()
            icon.addPixmap(pm)
        return icon

    def item(self, text: str, *, icon: str = "dot", right: str = "") -> QAction:
        act = QAction(self.dot_icon(icon), text + (f"\t{right}" if right else ""), self)
        self._words[id(act)] = text
        self.addAction(act)
        return act

    def set_right(self, act: QAction, right: str, *, live: bool = False) -> None:
        del live
        words = self._words.get(id(act), act.text().split("\t")[0])
        act.setText(words + (f"\t{right}" if right else ""))


__all__ = ["CARD_W", "CardFoot", "CloseKey", "ChangedRow", "ConfirmRow", "Detail", "DotProgress", "LatticeCard",
           "LearnedBox", "ReturnCard", "Toggle", "TrayHoverCard", "TrayMenu"]
