"""The live chat thread on screen: the Yuki window's Chat page and the Alt+Shift overlay.

Renders a :class:`~yuki.ui.chat_model.ChatModel`. Every bubble is its own widget,
built only when it is within the last :data:`WINDOW` items (older ones are built
when the user scrolls to the top or presses "Show earlier"), so a thread of a
thousand turns costs what sixty do. A bubble updates in place as its events
arrive (streaming speech, a step finishing), and the view follows the newest
message unless the user has scrolled up.

Under each response a collapsible header -- ``worked for 12 s · 5 steps`` --
opens the list of every tool call and action it ran, each with its short
result, success or failure and duration; open while it runs, closed once done,
the final answer below it. Text is selectable and every response has Copy.

The only timer is the one-second tick of a running response's "working 4 s"
label, and it stops when nothing runs.
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
from yuki.ui.window.glass import paint_glass, rounded
from yuki.ui.window.theme import ICON, SPACE, Theme, css, font

#: Bubbles built at once (the newest); older ones are built on demand.
WINDOW = 60
#: How many more "Show earlier" builds.
PAGE = 40

KIND_TITLES = {"praise": "Nice one", "nudge": "Check-in", "reminder": "Reminder", "review": "Your week",
               "resume": "Welcome back", "question": "Getting to know you"}


def _label(t: Theme, text: str, role: str, ink: QColor, *, wrap: bool = True, select: bool = True,
           markdown: bool = False) -> QLabel:
    lab = QLabel()
    lab.setFont(font(role))
    lab.setStyleSheet(f"color: {css(ink)}; background: transparent;")
    lab.setTextFormat(Qt.TextFormat.MarkdownText if markdown else Qt.TextFormat.PlainText)
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


class _Tiny(QAbstractButton):
    """A small text/icon button (Copy, Reply, the steps header), painted from the theme."""

    def __init__(self, t: Theme, text: str, glyph: str | None = None, *, ink: QColor | None = None,
                 pill: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.ink, self.pill = t, glyph, ink, pill
        self.setText(text)
        self.setFont(font("caption_strong" if pill else "caption"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:  # noqa: D102
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) + (18 if self.glyph else 0) + (24 if self.pill else 6)
        return QSize(w, 24 if self.pill else 20)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        ink = QColor(self.ink or self.t.text_2)
        if self.pill:
            wash = QColor(ink)
            wash.setAlphaF(0.20 if self.t.dark else 0.13)
            edge = QColor(ink)
            edge.setAlphaF(0.40)
            p.setBrush(wash)
            p.setPen(QPen(edge, 1.0))
            p.drawPath(rounded(r, r.height() / 2))
        elif self.underMouse():
            hover = QColor(self.t.grid)
            p.setBrush(hover)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(rounded(r, 6))
        if not self.isEnabled():
            ink = QColor(self.t.text_3)
        p.setPen(ink)
        x = r.left() + (12 if self.pill else 3)
        if self.glyph:
            p.setFont(font("icon_small"))
            p.drawText(QRectF(x, r.top(), 14, r.height()), Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
            x += 18
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
    """A small arc that turns while a step runs (a Qt timer shared through the view's tick)."""

    def __init__(self, t: Theme, size: int = 12) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(size, size)
        self.angle = 0

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
    """A message's glass: the user's accent-tinted on the right, Yuki's clear on the left."""

    def __init__(self, t: Theme, *, mine: bool, tone: str = "") -> None:
        super().__init__()
        self.t, self.mine, self.tone = t, mine, tone
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 9, 14, 9)
        self.body.setSpacing(3)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        t = self.t
        if self.mine:
            paint_glass(p, r, 18, t.level("lens"), tint=t.accent_soft, shadow=False, painted=True)
        else:
            tint = None
            if self.tone in ("error", "question", "interrupted"):
                tint = QColor(t.kind("error" if self.tone == "error" else "question" if self.tone == "question"
                                     else "unclear"))
                tint.setAlphaF(0.10 if not t.dark else 0.14)
            paint_glass(p, r, 18, t.level("raised"), tint=tint, shadow=False, painted=True)
        p.end()


class _StepRow(QWidget):
    """One tool call or action: status, what it did, its short result, how long."""

    def __init__(self, t: Theme, step: Step, now: float) -> None:
        super().__init__()
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(4 + 18 * step.depth, 1, 2, 1)
        row.setSpacing(8)
        self.spin: _Spin | None = None
        if step.running:
            self.spin = _Spin(t)
            row.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            glyph, ink = ("chat", t.text_3) if step.kind == "say" else (
                ("check", t.status["good"]) if step.ok else ("close", t.status["critical"]) if step.ok is False
                else ("more", t.text_3))
            icon = QLabel(ICON[glyph])
            icon.setFont(font("icon_small"))
            icon.setStyleSheet(f"color: {css(ink)}; background: transparent;")
            icon.setFixedWidth(12)
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        kind_word = {"hand": "Hands", "job": "Thinker"}.get(step.kind)
        text = step.label if not kind_word else f"{kind_word} · {step.label}"
        main = _label(t, text, "caption", t.text_2 if step.kind != "say" else t.text_3, wrap=False)
        main.setToolTip(text)
        row.addWidget(main, 0)
        self.result = _label(t, step.result, "caption", t.text_3, wrap=False)
        self.result.setToolTip(step.result)
        self.result.setMinimumWidth(0)
        self.result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.result, 1)
        self.secs = _label(t, "" if step.kind == "say" else fmt_seconds(step.seconds(now)), "caption", t.text_3,
                           wrap=False, select=False)
        row.addWidget(self.secs, 0, Qt.AlignmentFlag.AlignRight)
        self.step = step

    def tick(self, now: float) -> None:
        if self.spin is not None:
            self.spin.angle = (self.spin.angle + 40) % 360
            self.spin.update()
            self.secs.setText(fmt_seconds(self.step.seconds(now)))

    def resizeEvent(self, e: object) -> None:  # noqa: D102 - elide the result to what fits
        super().resizeEvent(e)
        fm = QFontMetrics(self.result.font())
        self.result.setText(fm.elidedText(self.step.result, Qt.TextElideMode.ElideRight, max(0, self.result.width())))


class _Steps(QWidget):
    """The collapsible "worked for 12 s · 5 steps" section under a response."""

    toggled = Signal()

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 2)
        col.setSpacing(1)
        self.head = _Tiny(t, "", "next", ink=t.text_3)
        self.head.clicked.connect(self.toggled.emit)
        col.addWidget(self.head, 0, Qt.AlignmentFlag.AlignLeft)
        self.rows_box = QWidget()
        self.rows_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.rows = QVBoxLayout(self.rows_box)
        self.rows.setContentsMargins(6, 2, 0, 4)
        self.rows.setSpacing(1)
        col.addWidget(self.rows_box)
        self._sig: tuple | None = None
        self._rows: list[_StepRow] = []

    def refresh(self, item: Item, now: float) -> None:
        open_ = item.expanded_now()
        self.head.glyph = "down" if open_ else "next"
        self.head.setText(item.work_line(now))
        self.head.updateGeometry()
        self.head.update()
        self.rows_box.setVisible(open_)
        if not open_:
            return
        sig = tuple((s.key, s.ended, s.ok, s.result, s.label) for s in item.steps)
        if sig == self._sig:
            return
        self._sig = sig
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._rows = [_StepRow(self.t, s, now) for s in item.steps]
        for r in self._rows:
            self.rows.addWidget(r)

    def tick(self, item: Item, now: float) -> None:
        self.head.setText(item.work_line(now))
        self.head.updateGeometry()
        for r in self._rows:
            r.tick(now)


class _ItemWidget(QWidget):
    """One row of the thread: a user bubble, a response (steps + text), a nudge or a notice."""

    reply_nudge = Signal(object)
    toggle = Signal(str)

    def __init__(self, t: Theme, item: Item, max_w: int) -> None:
        super().__init__()
        self.t, self.item_id, self.kind = t, item.id, item.kind
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
        if item.kind == "user":
            self.row.addStretch(1)
            self.bubble = _Bubble(t, mine=True)
            self.bubble.setMaximumWidth(self.max_w)
            self.text = _label(t, "", "body", t.text)
            self.meta = _label(t, "", "caption", t.text_3, wrap=False, select=False)
            self.meta.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.bubble.body.addWidget(self.text)
            self.bubble.body.addWidget(self.meta)
            self.row.addWidget(self.bubble)
            return
        if item.kind == "nudge":
            nudge = item.nudge or {}
            kind = str(nudge.get("kind") or "nudge")
            self.bubble = _Bubble(t, mine=False)
            self.bubble.setMaximumWidth(self.max_w)
            head = QHBoxLayout()
            head.setSpacing(6)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {css(t.kind(kind))}; background: transparent;")
            dot.setFont(font("caption"))
            head.addWidget(dot)
            head.addWidget(_label(t, f"{KIND_TITLES.get(kind, 'Yuki')} · {_stamp(item.at)}", "caption_strong",
                                  t.kind(kind), wrap=False, select=False))
            head.addStretch(1)
            self.reply = _Tiny(t, "Reply", ink=t.kind(kind), pill=True)
            self.reply.clicked.connect(lambda: self.reply_nudge.emit(item.nudge))
            head.addWidget(self.reply)
            self.bubble.body.addLayout(head)
            self.text = _label(t, item.text, "callout", t.text)
            self.bubble.body.addWidget(self.text)
            self.row.addWidget(self.bubble)
            self.row.addStretch(1)
            return
        # Yuki's response: steps above, then the answer's bubble
        col = QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)
        self.steps = _Steps(t)
        self.steps.toggled.connect(lambda: self.toggle.emit(self.item_id))
        self.steps.setMaximumWidth(self.max_w)
        col.addWidget(self.steps)
        self.bubble = _Bubble(t, mine=False)
        self.bubble.setMaximumWidth(self.max_w)
        self.text = _label(t, "", "body", t.text, markdown=True)
        self.bubble.body.addWidget(self.text)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.meta = _label(t, "", "caption", t.text_3, wrap=False, select=False)
        foot.addWidget(self.meta)
        foot.addStretch(1)
        self.copy = _Tiny(t, "Copy", "link")
        self.copy.clicked.connect(self._copy)
        foot.addWidget(self.copy)
        self.bubble.body.addLayout(foot)
        col.addWidget(self.bubble, 0, Qt.AlignmentFlag.AlignLeft)
        self.row.addLayout(col, 0)
        self.row.addStretch(1)
        self._raw = ""

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
            ink = t.text_2 if item.status in ("hearing", "others") else t.text
            self.text.setStyleSheet(f"color: {css(ink)}; background: transparent;")
            how = {"voice": " · said", "window": "", "overlay": "", "history": ""}.get(item.source, "")
            if item.status == "others":
                how = " · said to others on the call"
            self.meta.setText(f"You · {_stamp(item.at)}{how}")
        elif item.kind == "nudge" and hasattr(self, "reply"):
            self.reply.setText({"open": "Reply", "replying": "Replying…", "replied": "Replied"}.get(item.reply_state,
                                                                                                   "Reply"))
            self.reply.setEnabled(item.reply_state == "open")
        elif item.kind == "assistant" and self.text is not None and self.meta is not None:
            if self.steps is not None:
                self.steps.setVisible(bool(item.steps))
                if item.steps:
                    self.steps.refresh(item, now)
            text = item.text
            self._raw = text
            waiting = not text and item.status in ("pending", "running", "streaming")
            self.bubble.setVisible(bool(text) or waiting or item.status == "failed")
            self.text.setText(text if text else ("…" if waiting else ""))
            tone = {"failed": "error", "question": "question", "interrupted": "interrupted"}.get(item.status, "")
            if self.bubble.tone != tone:
                self.bubble.tone = tone
                self.bubble.update()
            parts = [f"Yuki · {_stamp(item.at)}"]
            if item.source == "voice":
                parts.append("spoken")
            if item.status == "interrupted":
                parts.append("interrupted")
            if item.meta:
                parts.append(item.meta)
            self.meta.setText(" · ".join(parts))
            if self.copy is not None:
                self.copy.setVisible(bool(text))

    def tick(self, item: Item, now: float) -> None:
        if self.steps is not None and item.steps and self.steps.isVisible():
            self.steps.tick(item, now)


class ChatThreadView(QWidget):
    """The thread in a scroll area. ``theme`` is a callable (the surface's current theme).

    Signals:
        reply_nudge: ``(nudge)`` -- Reply on a nudge in the thread.
    """

    reply_nudge = Signal(object)
    content_resized = Signal()

    def __init__(self, model: ChatModel, theme: Callable[[], Theme], *, max_bubble: int = 560,
                 margins: tuple[int, int, int, int] = (SPACE["m"], SPACE["m"], SPACE["m"], SPACE["m"]),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.theme = theme
        self.max_bubble = max_bubble
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
        self.lay.setSpacing(SPACE["s"])
        self.area.setWidget(self.inner)
        outer.addWidget(self.area)
        self._widgets: dict[str, _ItemWidget] = {}
        self._first = 0            # index into model.items of the first built bubble
        self._follow = True
        self._earlier: _Tiny | None = None
        self._empty: QLabel | None = None
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
        t = self.theme()
        handle = "rgba(255,255,255,70)" if t.dark else "rgba(20,24,36,60)"
        self.area.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0 2px 0; }"
            f"QScrollBar::handle:vertical {{ background: {handle}; border-radius: 3px; min-height: 24px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }")

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
        items = self.model.items
        self._first = max(0, len(items) - WINDOW)
        self.lay.addStretch(1)            # few messages sit at the bottom, like any chat
        self._sync_earlier()
        for item in items[self._first:]:
            self._append(item)
        self._sync_empty()
        self._sync_tick()
        self._follow = True

    def _sync_empty(self) -> None:
        if self.model.items:
            if self._empty is not None:
                self._empty.deleteLater()
                self._empty = None
            return
        if self._empty is None:
            t = self.theme()
            self._empty = _label(t, "Nothing yet. Type below, or say “Yuki”.", "callout", t.text_3, select=False)
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
        w = _ItemWidget(self.theme(), item, self.max_bubble)
        w.reply_nudge.connect(self.reply_nudge.emit)
        w.toggle.connect(self.model.toggle)
        self._widgets[item.id] = w
        return w

    def _append(self, item: Item) -> None:
        self.lay.addWidget(self._make(item))

    def show_earlier(self) -> None:
        """Build the next :data:`PAGE` older bubbles above the ones shown (the view stays where it is)."""
        if self._first <= 0:
            return
        bar = self.area.verticalScrollBar()
        before = bar.maximum() - bar.value()
        start = max(0, self._first - PAGE)
        at = 1 + (1 if self._earlier is not None else 0)
        for item in self.model.items[start:self._first]:
            self.lay.insertWidget(at, self._make(item))
            at += 1
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
