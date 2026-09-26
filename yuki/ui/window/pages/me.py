"""Me: who Yuki thinks the user is, each item with where it came from and a way to correct it.

Every item shows its source ("You told Yuki · yesterday", "Seen in Discord ·
25 Sep"). **Why?** folds open the journal entries it rests on. **Not right**
folds open a composer: the user says what is true in their own words, which
phase 2 sends through ``MemoryClient.correct_portrait`` (a user-origin
correction the portrait folds in within about a minute), phrased as
``About "<subject>" (memory had: "<text>"), the user said in their own words:
"<words>"`` so the fold knows which fact it answers. Jokes wear a Joke chip and
never read as facts; what Yuki may have misheard sits in its own card.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.data import ASPECT_TITLES, Item, MeView, day_label, first_sentence, hm, second_person
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import (
    Avatar,
    Divider,
    Glass,
    ItemRow,
    Panel,
    Pill,
    page_header,
    text_label,
)


def pick_examples(view: MeView) -> tuple[str | None, str | None]:
    """For the mockup: one item shown with its evidence open, one with the correction composer open.

    Evidence: the person with the most evidence entries; composer: the least
    certain inferred person (the kind of item a user would want to fix).
    """
    people = view.people
    inferred = [i for i in people if "you" not in i.tags and "unsure" not in i.tags] or people
    with_ev = max(inferred, key=lambda i: len(i.evidence), default=None)
    unsure = [i for i in people if "unsure" in i.tags]
    compose = unsure[-1] if unsure else None
    return (with_ev.ref if with_ev else None), (compose.ref if compose else None)


def _evidence(item: Item) -> list[tuple[str, str, str]]:
    out = []
    for e in item.evidence[:4]:
        d = datetime.fromtimestamp(e.at)
        where = "a call" if e.app.startswith("call") else e.app
        text = first_sentence(second_person(e.text), 200)
        if e.nature in ("humor", "uncertain"):
            text = f"[{'joke' if e.nature == 'humor' else 'may be misheard'}] {text}"
        out.append((f"{d.day} {d:%b} {d:%H:%M}", where, text))
    return out


def _row(t: Theme, item: Item, expand: str | None, compose: str | None, *, leading: QWidget | None = None,
         title: bool = True) -> ItemRow:
    return ItemRow(t, item.ref, item.title if title else "", item.short, item.source, tags=item.tags,
                   leading=leading, evidence=_evidence(item), show_evidence=item.ref == expand,
                   composer=item.ref == compose,
                   placeholder=f"e.g. “{item.title} is my friend from college”" if item.kind == "person" else "")


def _list_panel(t: Theme, title: str, subtitle: str, glyph: str, items: list[Item], expand: str | None,
                compose: str | None, *, limit: int = 4, more: bool = True) -> Panel:
    panel = Panel(t, title, subtitle, glyph=glyph,
                  right=Pill(t, f"All {len(items)}", style="quiet", size="small") if more and len(items) > limit else None)
    panel.body.setSpacing(SPACE["s"])
    shown = items[:limit]
    for i, it in enumerate(shown):
        if i:
            panel.body.addWidget(Divider(t))
        panel.body.addWidget(_row(t, it, expand, compose))
    if not shown:
        panel.body.addWidget(text_label(t, "Nothing yet.", "callout", "text_3"))
    return panel


def _people(t: Theme, view: MeView, expand: str | None, compose: str | None) -> Panel:
    panel = Panel(t, "People", "Friends, family and colleagues, and what Yuki knows about each.", glyph="people",
                  right=Pill(t, f"All {len(view.people)}", style="quiet", size="small"))
    panel.body.setSpacing(SPACE["s"])
    palette = t.series
    # the ones the user told Yuki about first, then the expanded / composing examples, then the rest
    order = sorted(view.people, key=lambda i: (0 if i.ref == expand else 1 if i.ref == compose else
                                               2 if "you" in i.tags else 3, -i.since))
    for n, it in enumerate(order[:7]):
        if n:
            panel.body.addWidget(Divider(t))
        color = palette[sum(map(ord, it.title)) % len(palette)]
        panel.body.addWidget(_row(t, it, expand, compose, leading=Avatar(t, it.title, color)))
    return panel


def _personality(t: Theme, view: MeView, expand: str | None, compose: str | None) -> Panel:
    panel = Panel(t, "Personality", "How you talk, what you care about, your running jokes and your stories. "
                                    "A joke is kept as a joke, never as a fact.", glyph="emoji")
    panel.body.setSpacing(SPACE["s"])
    first = True
    for aspect, title in ASPECT_TITLES.items():
        items = view.personality.get(aspect) or []
        if not items:
            continue
        if not first:
            panel.body.addWidget(Divider(t))
        first = False
        panel.body.addWidget(text_label(t, title, "caption_strong", "text_2"))
        for it in items[:3]:
            panel.body.addWidget(_row(t, it, expand, compose, title=False))
    return panel


def _unsure(t: Theme, view: MeView) -> Panel:
    panel = Panel(t, "Not sure Yuki heard these right", "From calls, where words can be misheard. Yuki won't treat "
                  "them as facts until you say.", glyph="question")
    panel.body.setSpacing(SPACE["s"])
    for i, it in enumerate(view.unsure[:3]):
        if i:
            panel.body.addWidget(Divider(t))
        row = QVBoxLayout()
        row.setSpacing(4)
        row.addWidget(text_label(t, it.short, "callout", "text", wrap=True))
        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.addWidget(text_label(t, it.source, "caption", "text_3"))
        foot.addStretch(1)
        foot.addWidget(Pill(t, "That's right", glyph="check", style="quiet", size="small"))
        foot.addWidget(Pill(t, "Not right", glyph="edit", style="quiet", size="small"))
        row.addLayout(foot)
        panel.body.addLayout(row)
    if not view.unsure:
        panel.body.addWidget(text_label(t, "Nothing unclear this week.", "callout", "text_3"))
    return panel


def build(t: Theme, view: MeView, *, expand: str | None = None, compose: str | None = None) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    when = f"updated {day_label(view.portrait_at)} {hm(view.portrait_at)}" if view.portrait_at else "not written yet"
    col.addWidget(page_header(
        t, "Me", "What Yuki understands about you. Tap Why? to see where something came from, or Not right to "
                 "put it right in your own words.",
        [Pill(t, f"Portrait {when}", glyph="history", style="quiet")]))
    if view.questions_open:
        banner = Glass(t, level="raised", radius=20, padding=SPACE["m"], tint=_soft(t.kind("question"), t))
        row = QHBoxLayout()
        row.setSpacing(SPACE["s"])
        row.addWidget(text_label(t, "Yuki has " + ("a question" if view.questions_open == 1 else
                                                   f"{view.questions_open} questions") +
                                 " about people it doesn't know yet.", "body", "text"))
        row.addStretch(1)
        row.addWidget(Pill(t, "Answer on the Yuki page", glyph="next", style="tinted", tint=t.kind("question"),
                           size="small"))
        banner.body.addLayout(row)
        col.addWidget(banner)

    grid = QHBoxLayout()
    grid.setSpacing(SPACE["l"])
    left = QVBoxLayout()
    left.setSpacing(SPACE["l"])
    left.addWidget(_list_panel(t, "Work and projects", "What you do, now and lately.", "work", view.work,
                               expand, compose, limit=3))
    left.addWidget(_list_panel(t, "On your plate", "What you owe people, and what they owe you.", "flag",
                               view.on_your_plate, expand, compose, limit=4))
    left.addWidget(_list_panel(t, "Interests", "What you're into.", "heart", view.interests, expand, compose,
                               limit=3))
    left.addWidget(_list_panel(t, "Routines", "Patterns in how your days go.", "history", view.routines,
                               expand, compose, limit=2))
    left.addStretch(1)
    right = QVBoxLayout()
    right.setSpacing(SPACE["l"])
    right.addWidget(_people(t, view, expand, compose))
    right.addWidget(_personality(t, view, expand, compose))
    right.addWidget(_unsure(t, view))
    right.addStretch(1)
    grid.addLayout(left, 1)
    grid.addLayout(right, 1)
    col.addLayout(grid)
    col.addStretch(1)
    return page


def _soft(c, t: Theme):
    from PySide6.QtGui import QColor

    s = QColor(c)
    s.setAlphaF(0.10 if not t.dark else 0.14)
    return s
