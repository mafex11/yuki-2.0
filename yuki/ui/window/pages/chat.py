"""Chat: type to Yuki in the window. The same runtime as the Alt+Shift overlay (phase 2 wires
``AgentRuntime.submit`` and its signals); nudges Yuki sent sit in the conversation where they happened."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.data import ChatView, epoch
from yuki.ui.window.glass import paint_glass
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import Glass, Pill, TextPill, page_header, scroll, text_label

KIND_TITLES = {"praise": "Nice one", "nudge": "Check-in", "reminder": "Reminder", "review": "Your week",
               "resume": "Welcome back", "question": "Getting to know you"}


class _Bubble(QWidget):
    """A message: the user's in an accent-tinted lens on the right, Yuki's in raised glass on the left."""

    def __init__(self, t: Theme, text: str, meta: str, *, mine: bool) -> None:
        super().__init__()
        self.t, self.mine = t, mine
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(16, 10, 16, 10)
        col.setSpacing(2)
        col.addWidget(text_label(t, text, "body", "text", wrap=True))
        col.addWidget(text_label(t, meta, "caption", "text_3"))
        self.setMaximumWidth(560)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.mine:
            paint_glass(p, r, 20, self.t.level("lens"), tint=self.t.accent_soft, shadow=False)
        else:
            paint_glass(p, r, 20, self.t.level("raised"), shadow=False)
        p.end()


def _clean(text: str) -> str:
    # a request can carry an appended context block ("\n\n[Replying to ...]"): show what the user typed
    return (text or "").split("\n\n[")[0].strip()


def build(t: Theme, view: ChatView) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["m"])
    col.addWidget(page_header(t, "Chat", "Type to Yuki here. It's the same Yuki as Alt+Shift and your voice, and "
                                         "it remembers this conversation.",
                              [Pill(t, "New chat", glyph="edit", style="quiet")]))
    items: list[tuple[float, str, dict]] = [(tu["at"], "turn", tu) for tu in view.turns]
    items += [(epoch(n.get("shown_at")) or epoch(n.get("at")) or 0.0, "nudge", n) for n in view.nudges]
    items.sort(key=lambda x: x[0])
    convo_box = Glass(t, level="inset", radius=26, padding=SPACE["xs"])
    inner = QWidget()
    lay = QVBoxLayout(inner)
    lay.setContentsMargins(SPACE["m"], SPACE["m"], SPACE["m"], SPACE["m"])
    lay.setSpacing(SPACE["s"])

    if items:
        first = datetime.fromtimestamp(items[0][0])
        day = text_label(t, f"{first:%A} {first.day} {first:%B}", "caption_strong", "text_3")
        day.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(day)
    for at, kind, it in items:
        stamp = datetime.fromtimestamp(at).strftime("%H:%M")
        if kind == "nudge":
            card = Glass(t, level="raised", radius=18, padding=SPACE["m"], tint=_tint(t, it["kind"]))
            card.setMaximumWidth(560)
            card.body.setSpacing(3)
            head = QHBoxLayout()
            head.addWidget(text_label(t, f"{KIND_TITLES.get(it['kind'], 'Yuki')} · {stamp}", "caption_strong",
                                      it["kind"] if it["kind"] in t.kinds else "text_2"))
            head.addStretch(1)
            card.body.addLayout(head)
            card.body.addWidget(text_label(t, it["text"], "callout", "text", wrap=True))
            row = QHBoxLayout()
            row.addWidget(card)
            row.addStretch(1)
            lay.addLayout(row)
            continue
        user = _clean(it["user"])
        if user:
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(_Bubble(t, user, f"You · {stamp}" + (" · by voice" if it["outcome"] == "voice" else ""),
                                  mine=True))
            lay.addLayout(row)
        if it["reply"]:
            row = QHBoxLayout()
            row.addWidget(_Bubble(t, it["reply"], f"Yuki · {stamp}", mine=False))
            row.addStretch(1)
            lay.addLayout(row)
    lay.insertStretch(0, 1)   # few messages sit at the bottom, like any chat
    area = scroll(inner)
    area.setObjectName("chat_scroll")
    convo_box.body.addWidget(area)
    col.addWidget(convo_box, 1)
    field = TextPill(t, "Ask Yuki anything, or tell it what to do…", action_glyph="send", height=52)
    col.addWidget(field)
    hint = text_label(t, "Enter sends · Shift+Enter for a new line · Alt+Shift+X stops what Yuki is doing",
                      "caption", "text_3")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    col.addWidget(hint)
    return page


def _tint(t: Theme, kind: str):
    from PySide6.QtGui import QColor

    c = QColor(t.kind(kind))
    c.setAlphaF(0.10 if not t.dark else 0.14)
    return c
