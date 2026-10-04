"""The live chat thread on screen: the Yuki window's Chat page and the Alt+Shift overlay.

Renders a :class:`~yuki.ui.chat_model.ChatModel`. Every bubble is its own widget,
built only when it is within the last :data:`WINDOW` items (older ones are built
when the user scrolls to the top or presses "Show earlier"), so a thread of a
thousand turns costs what sixty do. A bubble updates in place as its events
arrive (streaming speech, a step finishing), and the view follows the newest
message unless the user has scrolled up.

Under each response a collapsible header -- ``worked for 12 s, 5 steps`` --
opens the list of every tool call and action it ran, each with its short
result, success or failure and duration; open while it runs, closed once done,
the final answer below it. Text is selectable and every response has Copy.

The only timer is the one-second tick of a running response's "working 4 s"
label, and it stops when nothing runs (the thinking line's dots are a
:class:`~yuki.ui.wave.Wave`, animated only while shown).

Two looks (Horizon x Field Unit): ``window`` -- your messages on a glass bubble
on the right, Yuki's on a navy panel on the left, check-ins between two rules --
and ``overlay`` (the ask bar) -- only your own lines on a subtle bubble, Yuki's
words plain, the check-in inline.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.chat_model import ChatModel, Item, Step, fmt_seconds
from yuki.ui.window.paint import paint_surface
from yuki.ui.window.theme import ICON, RADIUS, SPACE, Theme, css, font, rich_font, variable_font

#: Bubbles built at once (the newest); older ones are built on demand.
WINDOW = 60
#: How many more "Show earlier" builds.
PAGE = 40
#: Bubbles built before the thread first paints (the newest); the rest of :data:`WINDOW` fill in above them,
#: :data:`BACKFILL` at a time a frame apart (input and painting run between), the view staying on the newest.
FIRST = 8
BACKFILL = 2

KIND_TITLES = {"praise": "Nice one", "nudge": "Check-in", "reminder": "Reminder", "review": "Weekly review",
               "resume": "Welcome back", "question": "Getting to know you", "notice": "Noticed"}


def _label(t: Theme, text: str, role: str, ink: QColor, *, wrap: bool = True, select: bool = True,
           markdown: bool = False) -> QLabel:
    lab = QLabel()
    # a formatted reply is rich text (yuki.ui.markdown); its font leaves the weight free for bold spans
    lab.setFont(rich_font(font(role).pixelSize()) if markdown else font(role))
    lab.setStyleSheet(f"color: {css(ink)}; background: transparent;")
    lab.setTextFormat(Qt.TextFormat.RichText if markdown else Qt.TextFormat.PlainText)
    lab.setText(text)
    lab.setWordWrap(wrap)
    if wrap:
        lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    if select:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                    | Qt.TextInteractionFlag.TextSelectableByKeyboard
                                    | (Qt.TextInteractionFlag.LinksAccessibleByMouse if markdown else
                                       Qt.TextInteractionFlag.NoTextInteraction))
        lab.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # selecting never pulls the keyboard out of the input
        lab.setOpenExternalLinks(markdown)
    return lab


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M")


def _day_words(at: float) -> str:
    d = datetime.fromtimestamp(at)
    return f"{d:%A} {d.day} {d:%B}"


class _DayRule(QWidget):
    """"Sunday 27 September" between two quiet rules, where the thread's day changes."""

    def __init__(self, t: Theme, text: str) -> None:
        super().__init__()
        self.t, self.text = t, text
        self.setFixedHeight(30)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        f = variable_font(12.5, "Regular")
        p.setFont(f)
        w = QFontMetrics(f).horizontalAdvance(self.text) + 28
        r = QRectF(self.rect())
        y = r.center().y()
        p.fillRect(QRectF(r.left(), y, (r.width() - w) / 2, 1), self.t.line2)
        p.fillRect(QRectF(r.center().x() + w / 2, y, (r.width() - w) / 2, 1), self.t.line2)
        p.setPen(self.t.text_3)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text)
        p.end()


class _Tiny(QAbstractButton):
    """A small quiet word with an optional glyph (Copy, Reply, the steps header); ``pill``: an outline pill
    ("Show earlier")."""

    def __init__(self, t: Theme, text: str, glyph: str | None = None, *, ink: QColor | None = None,
                 pill: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.ink, self.pill = t, glyph, ink, pill
        #: The Lattice fold (the ask bar): the chevron as a 9 px dot icon (turned down when open), mono words.
        self.dots = False
        self.setText(text)
        self.setFont(variable_font(12.5, "Regular"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:  # noqa: D102
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) + (16 if self.glyph else 0) + (26 if self.pill else 4)
        return QSize(w, 28 if self.pill else 20)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        ink = QColor(self.ink or self.t.text_3)
        if self.underMouse():
            ink = QColor(self.t.text_2)
        if self.pill:   # an outline pill
            paint_surface(p, r, r.height() / 2, self.t.level("card"), fill=QColor(self.t.subtle) if
                          self.underMouse() else QColor(0, 0, 0, 0), border=self.t.border)
            ink = QColor(self.t.text_2)
        if not self.isEnabled():
            ink = QColor(self.t.faint)
        p.setPen(ink)
        x = r.left() + (13 if self.pill else 1)
        if self.glyph and self.dots:
            from yuki.ui import lattice as L

            L.paint_icon(p, QRectF(x, r.center().y() - 4.5, 9, 9), "chev", ink,
                         rotate=90.0 if self.glyph == "down" else 0.0)
            x += 17
        elif self.glyph:
            f = font("icon_small")
            f.setPixelSize(10 if self.glyph in ("next", "down") else 11)
            p.setFont(f)
            p.drawText(QRectF(x, r.top(), 12, r.height()), Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
            x += 16
        p.setFont(self.font())
        p.drawText(QRectF(x, r.top(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)


class _Spin(QWidget):
    """A small arc that turns while something runs. It turns on its own timer (about 16 frames a second)
    only while it is shown; hidden, it costs nothing."""

    def __init__(self, t: Theme, size: int = 12) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(size, size)
        self.angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._turn)

    def _turn(self) -> None:
        self.angle = (self.angle + 24) % 360
        self.update()

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setPen(QPen(self.t.baseline, 1.6))
        p.drawEllipse(r)
        pen = QPen(self.t.accent, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(-self.angle * 16), int(-110 * 16))
        p.end()


class _Bubble(QWidget):
    """A message. Window look: the user's on glass on the right, Yuki's on a navy panel on the left (10 px
    corners). Ask-bar look: only the user's own line has a subtle bubble; Yuki's words stand plain. An error
    gets a red line (the words say what failed)."""

    def __init__(self, t: Theme, *, mine: bool, tone: str = "", look: str = "window") -> None:
        super().__init__()
        self.t, self.mine, self.tone, self.look = t, mine, tone, look
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.body = QVBoxLayout(self)
        if look == "overlay":
            self.body.setContentsMargins(*((13, 7, 13, 7) if mine else (0, 0, 0, 0)))
        else:
            self.body.setContentsMargins(14, 11, 14, 11)
        self.body.setSpacing(5 if look == "window" else 4)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        t = self.t
        error = self.tone == "error"
        if self.look == "overlay" and not self.mine and not error:
            return
        p = QPainter(self)
        r = QRectF(self.rect())
        if self.look == "overlay":
            paint_surface(p, r, 16, t.level("card"), fill=QColor(t.bubble),
                          border=t.danger if error else QColor(0, 0, 0, 0))
        else:
            level = t.level("display" if self.mine else "card")
            paint_surface(p, r, RADIUS["bubble"], level, border=t.danger if error else None)
        p.end()


class _CheckIn(QWidget):
    """A check-in in the thread: no bubble, a rule above and below (the mockups' ``.chk``)."""

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 11, 0, 11)
        self.body.setSpacing(4)
        self.tone = ""

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect())
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 1), self.t.border)
        p.fillRect(QRectF(r.left(), r.bottom() - 1, r.width(), 1), self.t.border)
        p.end()


class _StepRow(QWidget):
    """One tool call or action: status, what it did, its short result, how long (the time it took from the
    step before, the model's thinking included: :meth:`~yuki.ui.chat_model.Item.step_times`)."""

    def __init__(self, t: Theme, step: Step, now: float, secs: float | None = None) -> None:
        super().__init__()
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(2 + 18 * step.depth, 2, 2, 2)
        row.setSpacing(8)
        self.spin = None
        if step.running:
            from yuki.ui.wave import Wave

            self.spin = Wave("think", theme=lambda: t, size=(14, 10))
            row.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            glyph, ink = ("chat", t.text_3) if step.kind == "say" else (
                ("check", t.text_2) if step.ok else ("close", t.kind("error")) if step.ok is False
                else ("more", t.text_3))
            icon = QLabel(ICON[glyph])
            f = font("icon_small")
            f.setPixelSize(10)
            icon.setFont(f)
            icon.setStyleSheet(f"color: {css(ink)}; background: transparent;")
            icon.setFixedWidth(14)
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        kind_word = {"hand": "Hands", "job": "Thinker"}.get(step.kind)
        text = step.label if not kind_word else f"{kind_word}, {step.label}"
        if step.kind == "helper" and step.depth and not step.name.startswith("helper"):
            text = f"reads, {step.label}"   # a helper's own tool use, under its "Helper N" line
        main = _label(t, text, "caption", t.text_2 if step.kind != "say" else t.text_3, wrap=False)
        main.setFont(variable_font(12.5, "Regular"))
        main.setMinimumWidth(min(170, QFontMetrics(main.font()).horizontalAdvance(text) + 4))
        main.setToolTip(text)
        row.addWidget(main, 0)
        self.result = _label(t, step.result, "caption", t.text_3, wrap=False)
        self.result.setFont(variable_font(12.5, "Regular"))
        self.result.setToolTip(step.result)
        self.result.setMinimumWidth(0)
        self.result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.result, 1)
        shown = step.seconds(now) if secs is None else secs
        self.secs = _label(t, "" if step.kind == "say" else fmt_seconds(shown), "caption", t.text_3,
                           wrap=False, select=False)
        self.secs.setFont(variable_font(12.5, "Regular"))
        row.addWidget(self.secs, 0, Qt.AlignmentFlag.AlignRight)
        self.step = step

    def tick(self, now: float, secs: float | None = None) -> None:
        if self.spin is not None:
            self.secs.setText(fmt_seconds(self.step.seconds(now) if secs is None else secs))

    def resizeEvent(self, e: object) -> None:  # noqa: D102 - elide the result to what fits
        super().resizeEvent(e)
        fm = QFontMetrics(self.result.font())
        self.result.setText(fm.elidedText(self.step.result, Qt.TextElideMode.ElideRight, max(0, self.result.width())))


class _RuledBox(QWidget):
    """The steps' box: a 1 px rule down its left edge."""

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.fillRect(QRectF(2, 0, 1, self.height()), self.t.border)
        p.end()


class _Steps(QWidget):
    """The collapsible "worked for 12 s, 5 steps" section under a response: a quiet header, and the steps
    under a thin rule on the left."""

    toggled = Signal()

    def __init__(self, t: Theme, *, lattice: bool = False) -> None:
        super().__init__()
        self.t = t
        self.lattice = lattice
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 2)
        col.setSpacing(1)
        self.head = _Tiny(t, "", "next", ink=t.text_3)
        self.head.clicked.connect(self.toggled.emit)
        col.addWidget(self.head, 0, Qt.AlignmentFlag.AlignLeft)
        self.rows_box = _RuledBox(t)
        self.rows = QVBoxLayout(self.rows_box)
        self.rows.setContentsMargins(14, 2, 0, 4)
        self.rows.setSpacing(0)
        col.addWidget(self.rows_box)
        self._sig: tuple | None = None
        self._rows: list[_StepRow] = []

    def refresh(self, item: Item, now: float) -> None:
        open_ = item.expanded_now()
        self.head.glyph = "down" if open_ else "next"
        self.head.setText(self._line(item, now))
        self.head.updateGeometry()
        self.head.update()
        self.rows_box.setVisible(open_)
        if not open_:
            return
        from yuki.ui.chat_model import CLOSING_TOOLS

        shown = [s for s in item.steps if not (s.kind == "tool" and s.name in CLOSING_TOOLS)]
        times, tail = item.step_times(now)
        sig = (tuple((s.key, s.ended, s.ok, s.result, s.label) for s in shown), tail is not None and tail[1],
               tail is not None)
        if sig == self._sig:
            self.tick(item, now)
            return
        self._sig = sig
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._rows = [_StepRow(self.t, s, now, times.get(s.key)) for s in shown]
        if tail is not None:   # after the last tool: the reply being written (so the times add up)
            seconds, running = tail
            last = Step("tail", "tool", "reply", "Writing the reply", now - seconds,
                        ended=None if running else now, ok=None if running else True)
            self._rows.append(_StepRow(self.t, last, now, seconds))
        for r in self._rows:
            self.rows.addWidget(r)

    def _line(self, item: Item, now: float) -> str:
        """The fold's head: "worked for 12 s, 5 steps"; the Lattice ask bar's "3 steps · 7 s, 8¢"."""
        if not self.lattice:
            return item.work_line(now)
        n = len(item.real_steps())
        parts = [fmt_seconds(item.work_seconds(now))] + ([item.meta] if item.meta else [])
        return f"{n} step{'' if n == 1 else 's'} · {', '.join(parts)}"

    def tick(self, item: Item, now: float) -> None:
        self.head.setText(self._line(item, now))
        self.head.updateGeometry()
        times, tail = item.step_times(now)
        for r in self._rows:
            r.tick(now, tail[0] if r.step.key == "tail" and tail is not None else times.get(r.step.key))


#: What a card's offer button says once it was pressed (``nudge_state`` ``offer:<state>``, or the card's stored
#: ``outcome``): honest states, never "Done" at the press (06 §4.4). Dismissed / "less" cards lose the button.
OFFER_STATE_WORDS: dict[str, str] = {
    "running": "Running…", "done": "Done", "undoing": "Undoing…", "undone": "Undone", "prefilled": "In the ask bar",
    "stopped": "Stopped", "abandoned": "Not sent", "expired": "Expired", "gone": "No longer there",
    "off": "Offers are off", "already": "Already done", "error": "Didn't work",
}
#: Offer states after which the card's offer button is gone altogether.
OFFER_GONE_STATES: frozenset[str] = frozenset({"dismissed", "less"})


class _ItemWidget(QWidget):
    """One row of the thread: a user bubble, a response (steps + text), a nudge or a notice."""

    reply_nudge = Signal(object)
    #: ``(nudge)`` -- the offer's button on a card with a typed offer (run by code in memory, never the agent).
    do_nudge = Signal(object)
    #: ``(nudge)`` -- Undo after a card's offer ran (adding to the list, opening a page again).
    undo_nudge = Signal(object)
    toggle = Signal(str)

    def __init__(self, t: Theme, item: Item, max_w: int, look: str = "window") -> None:
        super().__init__()
        self.t, self.item_id, self.kind, self.look = t, item.id, item.kind, look
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.max_w = max_w
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 2, 0, 2)
        self.row.setSpacing(0)
        self.steps: _Steps | None = None
        self.text: QLabel | None = None
        self.meta: QLabel | None = None
        self.bubble: _Bubble | None = None
        self.copy: _Tiny | None = None
        self._build(item)
        self.refresh(item)

    def _build(self, item: Item) -> None:
        t = self.t
        if item.kind == "notice":
            lab = _label(t, item.text, "caption", t.text_3)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.row.addWidget(lab, 1)
            return
        overlay = self.look in ("overlay", "lattice")
        if self.look == "lattice" and item.kind in ("user", "assistant"):
            self._build_turn(item)
            return
        if item.kind == "user":
            self.row.addStretch(1)
            self.bubble = _Bubble(t, mine=True, look=self.look)
            self.bubble.setMaximumWidth(self.max_w)
            self.text = _label(t, "", "body", t.text)
            if overlay:   # the ask bar: your words in Segoe UI Variable Display Light
                self.text.setFont(variable_font(16, "Light", display=True))
            self.meta = _label(t, "", "micro", t.text_3, wrap=False, select=False)
            self.meta.setFont(variable_font(11.5, "Regular"))
            self.meta.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.bubble.body.addWidget(self.text)
            self.bubble.body.addWidget(self.meta)
            self.meta.setVisible(not overlay)
            self.row.addWidget(self.bubble)
            return
        if item.kind == "nudge":   # a check-in: between two rules, a small grey wave, its kind and time
            from yuki.ui.wave import Wave

            nudge = item.nudge or {}
            kind = str(nudge.get("kind") or "nudge")
            self.bubble = _CheckIn(t)
            self.bubble.setMaximumWidth(self.max_w)
            self.bubble.setMinimumWidth(min(self.max_w, 320))
            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(Wave("checkin", theme=lambda: t, size=(34, 10)), 0, Qt.AlignmentFlag.AlignVCenter)
            from yuki.ui.nudges import card_offer, card_title, checkin_moment

            joiner = " at " if overlay else ", "
            checkin = checkin_moment(nudge) is not None
            title = card_title(nudge) if kind == "notice" or checkin else KIND_TITLES.get(kind, "Yuki")
            missed = ", missed while you were away" if nudge.get("missed") else ""
            when = _label(t, f"{title}{joiner}{_stamp(item.at)}{missed}", "micro", t.text_3,
                          wrap=False, select=False)
            when.setFont(variable_font(11.5, "Regular"))
            head.addWidget(when)
            head.addStretch(1)
            self.bubble.body.addLayout(head)
            if kind == "question":   # the question bold and bright; what it rests on small and dim under it
                from yuki.ui.nudges import split_question

                asked, rest = split_question(item.text)
                self.text = _label(t, asked, "body_strong", t.text)
                self.bubble.body.addWidget(self.text)
                if rest:
                    self.bubble.body.addWidget(_label(t, rest, "caption", t.text_3))
            else:
                self.text = _label(t, item.text, "body", t.text)
                if overlay:
                    self.text.setFont(variable_font(14, "Regular"))
                self.bubble.body.addWidget(self.text)
            self.do: _Tiny | None = None
            self.undo: _Tiny | None = None
            self.offer_info: QLabel | None = None
            self.offer_result: QLabel | None = None
            #: The offer's state as this thread last heard it (``nudge_state`` ``offer:<state>``), else its outcome.
            self.offer_state: str | None = str(nudge.get("_offer_state") or nudge.get("outcome") or "") or None
            offer = card_offer(nudge) if kind != "question" or checkin else None   # a check-in may carry one
            if offer is not None:   # what the button will and won't do, in sight before the press
                from yuki.ui.nudges import offer_label, offer_lines

                self.offer_info = _label(t, offer_lines(nudge), "caption", t.text_3)
                self.bubble.body.addWidget(self.offer_info)
                self.offer_result = _label(t, "", "caption", t.text)   # a draft, or what Yuki holds, once shown
                self.offer_result.setVisible(False)
                self.bubble.body.addWidget(self.offer_result)
            acts = QHBoxLayout()
            acts.setSpacing(12)
            if offer is not None:   # named for the action; runs by code in memory, never the agent
                self.do = _Tiny(t, offer_label(nudge) or "Do it")
                self.do.clicked.connect(lambda: self.do_nudge.emit(item.nudge))
                acts.addWidget(self.do)
                self.undo = _Tiny(t, "Undo")
                self.undo.clicked.connect(lambda: self.undo_nudge.emit(item.nudge))
                self.undo.setVisible(False)
                acts.addWidget(self.undo)
            self.reply = _Tiny(t, "Reply")
            self.reply.clicked.connect(lambda: self.reply_nudge.emit(item.nudge))
            acts.addWidget(self.reply)
            acts.addStretch(1)
            self.bubble.body.addLayout(acts)
            self.row.addWidget(self.bubble, 1 if overlay else 0)
            if not overlay:
                self.row.addStretch(1)
            return
        # Yuki's response: steps above, then the answer's bubble
        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)
        self.steps = _Steps(t)
        self.steps.toggled.connect(lambda: self.toggle.emit(self.item_id))
        self.steps.setMaximumWidth(self.max_w)
        self.steps.setMinimumWidth(min(self.max_w, 460))   # the times line up in a column on the right
        col.addWidget(self.steps)
        # the instant a message is sent: a turning arc and "Thinking…" (or the thinking summary as it is
        # written, one muted line) until the first words of the reply stream in
        from yuki.ui.wave import Wave

        self.live = QWidget()
        self.live.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        live_row = QHBoxLayout(self.live)
        live_row.setContentsMargins(2, 4, 4, 4)
        live_row.setSpacing(10)
        self.live_spin = Wave("think", theme=lambda: t, size=(34, 10))   # the thinking line: drifting dots
        live_row.addWidget(self.live_spin, 0, Qt.AlignmentFlag.AlignVCenter)
        self.live_label = _label(t, "Thinking…", "caption", t.text_3, wrap=False, select=False)
        live_row.addWidget(self.live_label, 1)
        self.live.setMaximumWidth(self.max_w)
        self.live.setVisible(False)
        col.addWidget(self.live, 0, Qt.AlignmentFlag.AlignLeft)
        self.bubble = _Bubble(t, mine=False, look=self.look)
        self.bubble.setMaximumWidth(self.max_w)
        self.text = _label(t, "", "body", t.text, markdown=True)
        if overlay:
            self.text.setFont(rich_font(15))
        self.bubble.body.addWidget(self.text)
        foot = QHBoxLayout()
        foot.setSpacing(12)
        self.meta = _label(t, "", "micro", t.text_3, wrap=False, select=False)
        self.meta.setFont(variable_font(11.5, "Regular"))
        foot.addWidget(self.meta)
        self.copy = _Tiny(t, "Copy", "copy")
        self.copy.clicked.connect(self._copy)
        foot.addWidget(self.copy)
        foot.addStretch(1)
        self.bubble.body.addLayout(foot)
        if overlay:   # the ask bar: Yuki's words run the width of the panel
            col.addWidget(self.bubble)
            self.row.addLayout(col, 1)
        else:
            col.addWidget(self.bubble, 0, Qt.AlignmentFlag.AlignLeft)
            self.row.addLayout(col, 0)
            self.row.addStretch(1)
        self._raw = ""

    def _build_turn(self, item: Item) -> None:
        """The ask bar's Lattice look (the design's ``.turn``): a mono "who" column (82 px: "you · 21:25", "yuki")
        and the words beside it at 14 px; Yuki's steps fold under its words; a hair rule closes each turn."""
        from yuki.ui import lattice as L

        t = self.t
        mine = item.kind == "user"
        self.row.setContentsMargins(0, 10, 0, 0 if mine else 10)
        self.row.setSpacing(10)
        who = QLabel("you · " + _stamp(item.at) if mine else "yuki")
        who.setFont(L.mono_font(11))
        who.setFixedWidth(82)
        who.setStyleSheet(f"color: {css(L.TEXT3)}; background: transparent;")
        who.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        who.setContentsMargins(0, 2, 0, 0)
        self.row.addWidget(who, 0, Qt.AlignmentFlag.AlignTop)
        self.who = who
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setContentsMargins(0, 0, 0, 0)
        if mine:
            self.text = _label(t, "", "body", t.text)
            self.text.setFont(L.ui_font(14))
            self.meta = _label(t, "", "micro", t.text_3, wrap=False, select=False)
            self.meta.setVisible(False)
            col.addWidget(self.text)
            self.row.addLayout(col, 1)
            return
        self.bubble = _Bubble(t, mine=False, look="overlay")
        self.text = _label(t, "", "body", t.text, markdown=True)
        self.text.setFont(rich_font(14))
        self.bubble.body.addWidget(self.text)
        foot = QHBoxLayout()
        foot.setSpacing(12)
        self.meta = _label(t, "", "micro", t.text_3, wrap=False, select=False)
        self.meta.setFont(L.mono_font(11))
        foot.addWidget(self.meta)
        self.copy = _Tiny(t, "Copy", "copy")
        self.copy.clicked.connect(self._copy)
        foot.addWidget(self.copy)
        foot.addStretch(1)
        self.bubble.body.addLayout(foot)
        col.addWidget(self.bubble)
        from yuki.ui.wave import Wave

        self.live = QWidget()
        self.live.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        live_row = QHBoxLayout(self.live)
        live_row.setContentsMargins(0, 2, 4, 2)
        live_row.setSpacing(10)
        self.live_spin = Wave("think", theme=lambda: t, size=(34, 10))
        live_row.addWidget(self.live_spin, 0, Qt.AlignmentFlag.AlignVCenter)
        self.live_label = _label(t, "Thinking…", "caption", t.text_3, wrap=False, select=False)
        live_row.addWidget(self.live_label, 1)
        self.live.setVisible(False)
        col.addWidget(self.live, 0, Qt.AlignmentFlag.AlignLeft)
        self.steps = _Steps(t, lattice=True)
        self.steps.toggled.connect(lambda: self.toggle.emit(self.item_id))
        self.steps.head.setFont(L.mono_font(11.5))
        self.steps.head.dots = True
        self.meta.setVisible(False)   # the design's turn: no time or Copy under Yuki's words
        self.copy.setVisible(False)
        self._lattice_quiet = True
        col.addSpacing(8)
        col.addWidget(self.steps)
        self.row.addLayout(col, 1)
        self._raw = ""

    def paintEvent(self, _e: object) -> None:  # noqa: D102 - the Lattice turn's closing hair rule
        if self.look == "lattice" and self.kind == "assistant":
            p = QPainter(self)
            p.fillRect(QRectF(0, self.height() - 1, self.width(), 1), self.t.border)
            p.end()

    def _fit_width(self, text: str) -> None:
        """A reply takes the width its words need, up to the bubble's widest (Qt's own guess for wrapped rich
        text is a narrow column). Only grows while it streams, so the bubble never jumps narrower."""
        if not text or self.text is None:
            return
        cap = max(80, self.max_w - 30)
        now = self.text.minimumWidth()
        if now >= cap:
            return
        from PySide6.QtGui import QTextDocument

        doc = QTextDocument()
        doc.setDefaultFont(self.text.font())
        doc.setHtml(self.text.text())
        want = min(cap, int(doc.idealWidth()) + 6)
        if want > now:
            self.text.setMinimumWidth(want)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._raw)
        if self.copy is not None:
            self.copy.setText("Copied")
            QTimer.singleShot(1200, lambda: self.copy.setText("Copy") if self.copy is not None else None)

    def refresh(self, item: Item, now: float | None = None) -> None:
        t = self.t
        now = time.time() if now is None else now
        if item.kind == "user" and self.text is not None and self.meta is not None:
            self.text.setText(item.text + (" …" if item.status == "hearing" else ""))
            # a short message keeps its bubble one line wide (a wrapping label would squeeze it to a column)
            longest = max((QFontMetrics(self.text.font()).horizontalAdvance(line) for line in
                           (item.text or " ").split("\n")), default=0)
            self.text.setMinimumWidth(min(longest + 4, max(80, self.max_w - 30)))
            ink = t.text_2 if item.status in ("hearing", "others", "unanswered") else t.text
            self.text.setStyleSheet(f"color: {css(ink)}; background: transparent;")
            how = {"voice": ", said", "window": ", typed", "overlay": ", typed", "history": ""}.get(item.source, "")
            if item.status == "others":
                how = ", said to others on the call"
            elif item.status == "unanswered":
                how = ", said in the call, not answered"
            self.meta.setText(f"You, {_stamp(item.at)}{how}")
        elif item.kind == "nudge" and hasattr(self, "reply"):
            state = item.reply_state or "open"
            if state.startswith("offer:"):   # the offer's own state; the Reply button stays as it was
                self.offer_state = state.split(":", 1)[1] or None
                state = "open"
            self.reply.setText({"open": "Reply", "replying": "Replying…", "replied": "Replied"}.get(state, "Reply"))
            self.reply.setEnabled(state == "open")
            if getattr(self, "do", None) is not None:
                self._refresh_offer(item, now)
        elif item.kind == "assistant" and self.text is not None and self.meta is not None:
            if self.steps is not None:
                real = bool(item.real_steps())   # a plain reply (only the closing call) shows no fold
                self.steps.setVisible(real)
                if real:
                    self.steps.refresh(item, now)
            text = item.text
            self._raw = text
            waiting = not text and item.status in ("pending", "running", "streaming")
            _times, tail = item.step_times(now)
            # the fold's own last line says the reply is being written: no second "Thinking…" under it
            self.live.setVisible(waiting and not (tail is not None and tail[1] and not item.thinking))
            if waiting:
                line = " ".join((item.thinking or "").split())
                if len(line) > 110:
                    line = line[:108].rsplit(" ", 1)[0] + "…"
                busy = any(st.running for st in item.real_steps())
                self.live_label.setText(line or ("Working…" if busy else "Thinking…"))
            self.bubble.setVisible(bool(text) or item.status == "failed")
            if text and item.source != "voice":   # typed replies are Markdown: summary, bullets, bold, code
                from yuki.ui.markdown import to_html

                px = 15 if self.look == "overlay" else 14 if self.look == "lattice" else font("body").pixelSize()
                self.text.setText(to_html(text, t, px=px))
            else:   # spoken replies are plain words, shown as they were said
                import html as _html

                self.text.setText(_html.escape(text).replace("\n", "<br>") if text else "")
            self._fit_width(text)
            tone = {"failed": "error", "question": "question", "interrupted": "interrupted"}.get(item.status, "")
            if self.bubble.tone != tone:
                self.bubble.tone = tone
                self.bubble.update()
            if self.look in ("overlay", "lattice"):
                parts = [f"Spoken at {_stamp(item.at)}" if item.source == "voice" else _stamp(item.at)]
            else:
                parts = ["Yuki", _stamp(item.at)]
                if item.source == "voice":
                    parts.append("spoken")
            if item.status == "interrupted":
                parts.append("interrupted")
            if item.meta:
                parts.append(item.meta)
            self.meta.setText(", ".join(parts))
            if getattr(self, "_lattice_quiet", False):
                self.meta.setVisible(False)
            elif self.copy is not None:
                self.copy.setVisible(bool(text))

    def _refresh_offer(self, item: Item, now: float) -> None:
        """The offer's button says honestly where it stands: its label while it can run; Running / Done / Undone /
        In the ask bar / Expired ... after; gone on a card that was dismissed or given "Less of these", or whose
        offer expired unpressed (also in the history). A draft or what Yuki holds shows in the card once run."""
        from yuki.ui.nudges import card_offer, offer_expired, offer_label

        nudge = item.nudge or {}
        offer = card_offer(nudge) or {}
        state = self.offer_state
        reaction = str(nudge.get("reaction") or "")
        gone = (state in OFFER_GONE_STATES or (state is None and reaction in ("dismissed", "less", "snoozed"))
                or (state is None and offer_expired(nudge, now)))
        self.do.setVisible(not gone)
        if self.offer_info is not None:
            self.offer_info.setVisible(not gone and state is None)
        if not gone:
            self.do.setText(OFFER_STATE_WORDS.get(state, state) if state else (offer_label(nudge) or "Do it"))
            self.do.setEnabled(state is None)
        if self.undo is not None:
            self.undo.setVisible(state == "done" and offer.get("do") in ("todo_add", "bring_back"))
        if self.offer_result is not None:
            shown = ""
            if state == "done" and offer.get("do") == "draft":
                shown = str(offer.get("text") or "")
            elif state == "done" and offer.get("do") == "show_memory":
                shown = "\n\n".join(f"{i.get('title')}: {i.get('text')}" for i in offer.get("items") or []
                                     if isinstance(i, dict))
            self.offer_result.setText(shown)
            self.offer_result.setVisible(bool(shown))

    def tick(self, item: Item, now: float) -> None:
        if self.steps is not None and item.steps and self.steps.isVisible():
            self.steps.tick(item, now)


class ChatThreadView(QWidget):
    """The thread in a scroll area. ``theme`` is a callable (the surface's current theme).

    Signals:
        reply_nudge: ``(nudge)`` -- Reply on a nudge in the thread.
        do_nudge: ``(nudge)`` -- the offer's button on a card with a typed offer.
        undo_nudge: ``(nudge)`` -- Undo after a card's offer ran.
    """

    reply_nudge = Signal(object)
    do_nudge = Signal(object)
    undo_nudge = Signal(object)
    content_resized = Signal()

    def __init__(self, model: ChatModel, theme: Callable[[], Theme], *, max_bubble: int = 560,
                 margins: tuple[int, int, int, int] = (SPACE["m"], SPACE["m"], SPACE["m"], SPACE["m"]),
                 look: str = "window", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.theme = theme
        self.max_bubble = max_bubble
        self.look = look
        self._last_day: str | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.area = QScrollArea(self)
        self.area.setObjectName("chat_scroll")
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.viewport().setAutoFillBackground(False)
        self.inner = QWidget()
        self.inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.inner.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(*margins)
        self.lay.setSpacing(12 if look == "window" else 0 if look == "lattice" else 14)
        self.area.setWidget(self.inner)
        outer.addWidget(self.area)
        self._widgets: dict[str, _ItemWidget] = {}
        self._first = 0            # index into model.items of the first built bubble
        self._follow = True
        self._earlier: _Tiny | None = None
        self._empty: QLabel | None = None
        from yuki.ui.window.widgets import smooth

        self.scroller = smooth(self.area)   # pixel-smooth, eased, kinetic (wheel and touchpad)
        bar = self.area.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range)
        bar.valueChanged.connect(self._on_value)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._unsub = model.subscribe(self._on_model)
        self._style()
        self.rebuild()

    # -- building -------------------------------------------------------------------------------

    def _style(self) -> None:
        from yuki.ui.window.widgets import scrollbar_css

        self.area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
                                + scrollbar_css(self.theme()))

    def restyle(self) -> None:
        """The theme changed: build every visible bubble again in the new colours."""
        self._style()
        self.rebuild()

    def rebuild(self) -> None:
        while self.lay.count():
            it = self.lay.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self._widgets.clear()
        self._earlier = None
        self._empty = None
        self._last_day = None
        items = self.model.items
        self._target_first = max(0, len(items) - WINDOW)
        self._first = max(0, len(items) - FIRST)
        self._gen = getattr(self, "_gen", 0) + 1
        self.lay.addStretch(1)            # few messages sit at the bottom, like any chat
        self._sync_earlier()
        for item in items[self._first:]:
            self._append(item)
        self._sync_empty()
        self._sync_tick()
        self._follow = True
        if self._first > self._target_first:
            gen = self._gen
            QTimer.singleShot(16, lambda: self._backfill(gen))

    def _backfill(self, gen: int) -> None:
        """Build the rest of the newest :data:`WINDOW` bubbles above the ones shown, a few per turn."""
        try:
            if gen != self._gen or self._first <= self._target_first:
                return
            self.show_earlier(min(BACKFILL, self._first - self._target_first))
        except RuntimeError:   # the view is gone
            return
        if self._first > self._target_first:
            QTimer.singleShot(16, lambda: self._backfill(gen))

    def _sync_empty(self) -> None:
        if self.model.items:
            if self._empty is not None:
                self._empty.deleteLater()
                self._empty = None
            return
        if self._empty is None:
            t = self.theme()
            self._empty = _label(t, "Nothing yet. Type below, or say “Yuki”.", "body", t.text_3, select=False)
            self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lay.addWidget(self._empty)

    def _sync_earlier(self) -> None:
        if self._first > 0 and self._earlier is None:
            t = self.theme()
            self._earlier = _Tiny(t, f"Show earlier ({self._first})", "history", ink=t.text_2, pill=True)
            self._earlier.clicked.connect(self.show_earlier)
            self.lay.insertWidget(1, self._earlier, 0, Qt.AlignmentFlag.AlignHCenter)
        elif self._earlier is not None:
            if self._first > 0:
                self._earlier.setText(f"Show earlier ({self._first})")
            else:
                self._earlier.deleteLater()
                self._earlier = None

    def _make(self, item: Item) -> _ItemWidget:
        w = _ItemWidget(self.theme(), item, self.max_bubble, self.look)
        w.reply_nudge.connect(self.reply_nudge.emit)
        w.do_nudge.connect(self.do_nudge.emit)
        w.undo_nudge.connect(self.undo_nudge.emit)
        w.toggle.connect(self.model.toggle)
        self._widgets[item.id] = w
        from yuki.ui.window.widgets import attach_tips

        attach_tips(w, self.theme)
        return w

    def _append(self, item: Item) -> None:
        if self.look == "window":   # the day, where it changes
            day = _day_words(item.at)
            if day != self._last_day:
                self._last_day = day
                self.lay.addWidget(_DayRule(self.theme(), day))
        self.lay.addWidget(self._make(item))

    def show_earlier(self, count: int = PAGE) -> None:
        """Build the next ``count`` older bubbles above the ones shown (the view stays where it is)."""
        if self._first <= 0:
            return
        bar = self.area.verticalScrollBar()
        before = bar.maximum() - bar.value()
        start = max(0, self._first - max(1, int(count or PAGE)))
        self._target_first = min(getattr(self, "_target_first", start), start)
        at = 1 + (1 if self._earlier is not None else 0)
        top = self.lay.itemAt(at).widget() if self.lay.count() > at else None
        rule = top if isinstance(top, _DayRule) else None
        if rule is not None:   # the top day rule stays on top, naming the oldest day shown
            self.lay.removeWidget(rule)
        for item in self.model.items[start:self._first]:
            self.lay.insertWidget(at, self._make(item))
            at += 1
        if rule is not None:
            rule.text = _day_words(self.model.items[start].at)
            self.lay.insertWidget(1 + (1 if self._earlier is not None else 0), rule)
            rule.update()
        self._first = start
        self._sync_earlier()
        self.inner.adjustSize()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum() - before))

    # -- the model ------------------------------------------------------------------------------

    def _on_model(self, what: str, item: Item | None) -> None:
        if what == "reset":
            self.rebuild()
            return
        if item is None:
            return
        if what == "added":
            self._sync_empty()
            self._append(item)
            if len(self._widgets) > WINDOW + PAGE:   # keep the built set bounded while it streams
                oldest = next(iter(self._widgets))
                self._widgets.pop(oldest).deleteLater()
                self._first += 1
                self._sync_earlier()
        elif what == "changed":
            w = self._widgets.get(item.id)
            if w is not None:
                w.refresh(item)
        elif what == "removed":
            w = self._widgets.pop(item.id, None)
            if w is not None:
                w.deleteLater()
            else:
                self._first = max(0, self._first - 1)
            self._sync_earlier()
        self._sync_tick()
        self.content_resized.emit()

    def _sync_tick(self) -> None:
        running = any(i.running for i in self.model.items[-WINDOW:])
        if running and not self._tick.isActive():
            self._tick.start()
        elif not running and self._tick.isActive():
            self._tick.stop()

    def _on_tick(self) -> None:
        now = time.time()
        for item in self.model.items[-WINDOW:]:
            if item.running:
                w = self._widgets.get(item.id)
                if w is not None:
                    w.tick(item, now)
        self._sync_tick()

    # -- scrolling ------------------------------------------------------------------------------

    def _on_range(self, _lo: int, hi: int) -> None:
        if self._follow:
            self.area.verticalScrollBar().setValue(hi)
        self.content_resized.emit()

    def _on_value(self, value: int) -> None:
        bar = self.area.verticalScrollBar()
        self._follow = value >= bar.maximum() - 2
        if value <= 2 and self._first > 0 and bar.maximum() > 0:
            self.show_earlier()

    def scroll_to_latest(self) -> None:
        self._follow = True
        bar = self.area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def content_height(self) -> int:
        """Height the thread wants (for a surface that grows with it, up to a cap)."""
        self.inner.adjustSize()
        return self.inner.sizeHint().height()

    def closeEvent(self, e: object) -> None:  # noqa: D102
        self._tick.stop()
        self._unsub()
        super().closeEvent(e)

    def detach(self) -> None:
        self._tick.stop()
        self._unsub()
