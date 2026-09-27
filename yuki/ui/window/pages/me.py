"""Me: who Yuki thinks the user is, each item with where it came from and a way to correct it.

Every item shows its source ("You told Yuki · yesterday", "Seen in Discord ·
25 Sep"). **Why?** folds open the journal entries it rests on. **Not right**
folds open a composer: the user says what is true in their own words, which
goes through ``MemoryClient.correct_portrait`` (a user-origin correction the
portrait folds in within about a minute), phrased as
``About "<subject>" (memory had: "<text>"), the user said in their own words:
"<words>"`` so the fold knows which fact it answers. A joke says so in plain
secondary text and never reads as a fact (no chips: 2026-09-27); what Yuki may
have misheard sits in its own card.

**Your to-dos** is the one list of what the user owes people and what they are
waiting on (memory's ``todos()``, owner "user"), with Done / Not needed / Not
mine. It replaced "On your plate" (the portrait's open loops: the same items)
and moved here from the Yuki page (2026-09-27).
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import ASPECT_TITLES, Item, MeView, day_label, first_sentence, hm, plain, second_person
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import (
    Avatar,
    Divider,
    Masonry,
    Card,
    ItemRow,
    Panel,
    Button,
    Shimmer,
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
        text = first_sentence(plain(second_person(e.text)), 200)
        if e.nature in ("humor", "uncertain"):
            text = f"{'Said as a joke' if e.nature == 'humor' else 'May be misheard'}: {text}"
        out.append((f"{d.day} {d:%b} {d:%H:%M}", where, text))
    return out


#: Corrections the user sent that memory has not applied yet (ref -> {kind, words, at, late}); set by
#: :func:`build` for the rows it makes.
_PENDING: dict[str, dict] = {}


def _row(t: Theme, item: Item, expand: str | None, compose: str | None, act: Actions, *,
         leading: QWidget | None = None, title: bool = True) -> ItemRow:
    row = ItemRow(t, item.ref, item.title if title else "", item.short, item.source, tags=item.tags,
                  leading=leading, evidence=_evidence(item), show_evidence=item.ref == expand,
                  composer=item.ref == compose, pending=_PENDING.get(item.ref),
                  placeholder=f"e.g. “{item.title} is my friend from college”" if item.kind == "person" else "")
    row.correct.connect(lambda _ref, words, it=item: act.correct_item(it, words))
    row.forget.connect(lambda _ref, it=item: act.forget_item(it))
    return row


def _more(t: Theme, panel: Panel, rows: list[QWidget], limit: int, total: int) -> None:
    """Rows past ``limit`` start hidden; the panel's "All N" pill shows them (and "Fewer" hides them)."""
    if total <= limit:
        return
    pill = Button(t, f"All {total}", style="outline", size="small")
    for r in rows[limit:]:
        r.setVisible(False)

    def toggle() -> None:
        show = not rows[limit].isVisible()
        for r in rows[limit:]:
            r.setVisible(show)
        pill.setText("Fewer" if show else f"All {total}")
        pill.updateGeometry()

    pill.clicked.connect(toggle)
    head = panel.body.itemAt(0)
    if head is not None and head.layout() is not None:
        head.layout().addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)


def _list_panel(t: Theme, title: str, subtitle: str, glyph: str, items: list[Item], expand: str | None,
                compose: str | None, act: Actions, *, limit: int = 4) -> Panel:
    panel = Panel(t, title, subtitle, glyph=glyph)
    panel.body.setSpacing(SPACE["s"])
    rows: list[QWidget] = []
    for i, it in enumerate(items):
        box = QWidget()
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SPACE["s"])
        if i:
            lay.addWidget(Divider(t))
        lay.addWidget(_row(t, it, expand, compose, act))
        panel.body.addWidget(box)
        rows.append(box)
    _more(t, panel, rows, limit, len(items))
    if not items:
        panel.body.addWidget(text_label(t, "Nothing yet.", "callout", "text_3"))
    return panel


def _people(t: Theme, view: MeView, expand: str | None, compose: str | None, act: Actions) -> Panel:
    panel = Panel(t, "People", "Friends, family and colleagues, and what Yuki knows about each.", glyph="people")
    panel.body.setSpacing(SPACE["s"])
    # the ones the user told Yuki about first, then the expanded / composing examples, then the rest
    order = sorted(view.people, key=lambda i: (0 if i.ref == expand else 1 if i.ref == compose else
                                               2 if "you" in i.tags else 3, -i.since))
    rows: list[QWidget] = []
    for n, it in enumerate(order):
        box = QWidget()
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SPACE["s"])
        if n:
            lay.addWidget(Divider(t))
        lay.addWidget(_row(t, it, expand, compose, act, leading=Avatar(t, it.title)))
        panel.body.addWidget(box)
        rows.append(box)
    _more(t, panel, rows, 7, len(order))
    if not order:
        panel.body.addWidget(text_label(t, "Nobody yet.", "callout", "text_3"))
    return panel


def _personality(t: Theme, view: MeView, expand: str | None, compose: str | None, act: Actions) -> Panel:
    panel = Panel(t, "Personality", "How you talk, what you care about, your running jokes and your stories. "
                                    "A joke stays a joke, never a fact.", glyph="emoji")
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
            panel.body.addWidget(_row(t, it, expand, compose, act, title=False))
    return panel


def _unsure(t: Theme, view: MeView, act: Actions) -> Panel:
    panel = Panel(t, "Not sure Yuki heard these right", "From calls, where words can be misheard. Yuki won't treat "
                  "them as facts until you say.", glyph="question")
    panel.body.setSpacing(SPACE["s"])
    for i, it in enumerate(view.unsure[:3]):
        if i:
            panel.body.addWidget(Divider(t))
        row = ItemRow(t, it.ref, "", it.short, it.source, tags=[], evidence=_evidence(it),
                      placeholder="Say what was really said, or what's true", pending=_PENDING.get(it.ref))
        row.correct.connect(lambda _ref, words, item=it: act.correct_item(item, words))
        row.forget.connect(lambda _ref, item=it: act.forget_item(item))
        right = Button(t, "That's right", glyph="check", style="outline", size="small")

        def confirmed(_c: bool = False, item: Item = it, pill: Button = right) -> None:
            act.confirm_item(item)
            pill.setText("Confirmed")
            pill.setEnabled(False)
            pill.updateGeometry()

        right.clicked.connect(confirmed)
        panel.body.addWidget(row)
        line = QHBoxLayout()
        line.addStretch(1)
        line.addWidget(right)
        panel.body.addLayout(line)
    if not view.unsure:
        panel.body.addWidget(text_label(t, "Nothing unclear this week.", "callout", "text_3"))
    return panel


def build(t: Theme, view: MeView, *, expand: str | None = None, compose: str | None = None,
          act: Actions = NOOP, pending: dict[str, dict] | None = None, progressive: bool = True) -> QWidget:
    """The page. ``pending``: corrections not applied yet (shown as "Updating…" in the user's words).

    ``progressive``: the first panel of each column is built now and the rest one per turn of the event
    loop (``page.deferred``, run by the window), each into a shimmering placeholder already in the page.
    """
    _PENDING.clear()
    _PENDING.update(pending or {})
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    when = f"updated {day_label(view.portrait_at)} {hm(view.portrait_at)}" if view.portrait_at else "not written yet"
    refresh = Button(t, f"Portrait {when}", glyph="history", style="outline", size="small")
    refresh.setToolTip("Rebuild it now from what Yuki has seen")
    refresh.clicked.connect(act.refresh_portrait)
    col.addWidget(page_header(
        t, "Me", "What Yuki understands about you. Tap Why? to see where something came from, or Not right to "
                 "fix it in your own words.", [refresh]))
    if view.questions_open:
        banner = Card(t, padding=SPACE["l"])
        row = QHBoxLayout()
        row.setSpacing(SPACE["s"])
        row.addWidget(text_label(t, "Yuki has " + ("a question" if view.questions_open == 1 else
                                                   f"{view.questions_open} questions") +
                                 " about people it doesn't know yet.", "body", "text", wrap=True), 1)
        go = Button(t, "Answer on the Yuki page", glyph="next", style="default", size="small")
        go.clicked.connect(lambda: act.navigate("yuki"))
        row.addWidget(go)
        banner.body.addLayout(row)
        col.addWidget(banner)

    grid = QWidget(page)
    cards = Masonry(grid, columns=2, spacing=SPACE["l"], min_col=380)   # one column in a narrow window
    col.addWidget(grid)
    left_parts = [
        lambda: _list_panel(t, "Work and projects", "What you do, now and lately.", "work", view.work,
                            expand, compose, act, limit=3),
        lambda: _todo_list(t, view, act),
        lambda: _list_panel(t, "Interests", "What you're into.", "heart", view.interests, expand, compose, act,
                            limit=3),
        lambda: _list_panel(t, "Routines", "Patterns in how your days go.", "history", view.routines,
                            expand, compose, act, limit=2),
    ]
    right_parts = [
        lambda: _people(t, view, expand, compose, act),
        lambda: _personality(t, view, expand, compose, act),
        lambda: _unsure(t, view, act),
    ]
    columns: list[list] = []
    for column, parts in enumerate((left_parts, right_parts)):
        later: list = []
        for i, make in enumerate(parts):
            if i == 0 or not progressive:
                panel = make()
                panel.setParent(grid)
                cards.add(panel, column)
                continue
            # a holder keeps the card's place in the layout: the skeleton now, the card when it is built
            holder = QWidget(grid)
            hold = QVBoxLayout(holder)
            hold.setContentsMargins(0, 0, 0, 0)
            skeleton = Shimmer(t, rows=3, height=150, parent=holder)
            hold.addWidget(skeleton)
            cards.add(holder, column)

            def fill(make=make, holder=holder, hold=hold, skeleton=skeleton) -> None:
                panel = make()
                panel.setParent(holder)
                hold.addWidget(panel)
                skeleton.hide()
                skeleton.deleteLater()
                holder.updateGeometry()
                cards.invalidate()

            later.append(fill)
        columns.append(later)
    col.addStretch(1)
    # both columns fill in together, top first
    from itertools import zip_longest

    page.deferred = [f for pair in zip_longest(*columns) for f in pair if f is not None]  # type: ignore[attr-defined]
    return page


def _todo_list(t: Theme, view: MeView, act: Actions) -> Panel:
    from yuki.ui.window.pages.yuki_page import todos_panel

    return todos_panel(t, list(getattr(view, "todos", []) or []), act)

