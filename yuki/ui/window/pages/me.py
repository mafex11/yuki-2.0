"""Me: who Yuki thinks the user is, each item with where it came from and a way to correct it.

Round 2 (the user: "far too much text"): every section is scannable (:mod:`yuki.ui.window.pages.fold`): a
one-line headline, the top three items as short rows (a title, one quiet line, the date), the rest behind
"All N"; a row folds open to the full text, where it came from, **Why?** and **Not right**.

Every item shows its source ("You told Yuki, yesterday", "Seen in Discord,
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
from typing import Callable

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import ASPECT_TITLES, Item, MeView, day_label, hm, plain, second_person
from yuki.ui.window.theme import PAGE_PAD, RADIUS, SPACE, Theme, variable_font
from yuki.ui.window.pages.fold import SHOWN, fold_panel, one_line
from yuki.ui.window.widgets import (
    FoldRow,
    NavCard,
    Avatar,
    Divider,
    Masonry,
    ItemRow,
    Panel,
    Button,
    page_header,
    text_label,
)


class PortraitStamp(QAbstractButton):
    """When the portrait was rebuilt, on a small dot-matrix display ("00:13  Portrait rebuilt today"); a click
    rebuilds it now."""

    def __init__(self, t: Theme, at: float | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.at = t, at
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.sizeHint())

    def words(self) -> tuple[str, str, str]:
        if not self.at:
            return "--:--", "Portrait not", "written yet"
        return hm(self.at), "Portrait rebuilt", day_label(self.at)

    def sizeHint(self) -> QSize:  # noqa: D102
        from PySide6.QtGui import QFontMetrics

        from yuki.ui.window.paint import dots_width

        stamp, a, b = self.words()
        fm = QFontMetrics(variable_font(12.5, "Regular"))
        return QSize(int(10 + dots_width(stamp, 20) + 12 + max(fm.horizontalAdvance(a), fm.horizontalAdvance(b)) + 14),
                     50)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from yuki.ui.window.paint import dots_width, paint_dots, paint_surface

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["display"], self.t.level("display"),
                      border=self.t.faint if self.underMouse() else None)
        stamp, a, b = self.words()
        w = paint_dots(p, QRectF(r.left() + 10, r.top(), dots_width(stamp, 20), r.height()), stamp, 20, self.t.accent)
        p.setFont(variable_font(12.5, "Regular"))
        p.setPen(self.t.text_3)
        x = r.left() + 10 + w + 12
        p.drawText(QRectF(x, r.top() + 8, r.width() - x, 17), Qt.AlignmentFlag.AlignVCenter, a)
        p.drawText(QRectF(x, r.top() + 25, r.width() - x, 17), Qt.AlignmentFlag.AlignVCenter, b)
        p.end()

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self.update()
        super().leaveEvent(e)


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
        text = plain(second_person(e.text))   # the evidence in full (it sits in the open row, wrapped)
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
    pill = Button(t, f"All {total}", style="key", size="mini")
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
            panel.body.addSpacing(6)
        first = False
        panel.body.addWidget(text_label(t, title, "caption", "text_3"))
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


def _item_body(t: Theme, item: Item, act: Actions, *, expand: bool = False, compose: bool = False,
               tags: bool = True) -> Callable[[], QWidget]:
    """A fold's body: the whole item as an :class:`ItemRow` (text, source, Why?, Not right), built when opened."""
    def make() -> QWidget:
        row = ItemRow(t, item.ref, "", item.text, item.source, tags=item.tags if tags else [],
                      evidence=_evidence(item), show_evidence=expand, composer=compose,
                      pending=_PENDING.get(item.ref),
                      placeholder=f"e.g. “{item.title} is my friend from college”" if item.kind == "person"
                      else "")
        row.correct.connect(lambda _ref, words, it=item: act.correct_item(it, words))
        row.forget.connect(lambda _ref, it=item: act.forget_item(it))
        return row
    return make


def _when(item: Item) -> str:
    return day_label(item.since) if item.since else ""


def _fold(t: Theme, item: Item, act: Actions, *, expand: str | None, compose: str | None,
          initial: bool = False, title: str | None = None, line: str | None = None) -> FoldRow:
    open_now = item.ref in (expand, compose) or item.ref in _PENDING
    joke = "joke" in item.tags
    meta = ("joke, " if joke else "") + _when(item)
    if line is None:
        line = item.summary or one_line(item.text)   # memory's own summary when it has written one
    row = FoldRow(t, item.title if title is None else title, line, meta,
                  initial=next((c for c in item.title if c.isalnum()), "?").upper() if initial else "",
                  body=_item_body(t, item, act, expand=item.ref == expand, compose=item.ref == compose),
                  expanded=open_now)
    row.item = item  # type: ignore[attr-defined]
    return row


def counts_line(items: list[Item], one: str, many: str, *, none: str = "Nothing yet") -> str:
    """A section's headline: how many, and how many of them you told Yuki yourself ("14 people, 5 you told
    Yuki"); the rows under it already say which."""
    n = len(items)
    if not n:
        return none
    told = sum(1 for it in items if "you" in it.tags)
    return f"{n} {one if n == 1 else many}" + (f", {told} you told Yuki" if told else "")


def _section(t: Theme, title: str, items: list[Item], act: Actions, expand: str | None, compose: str | None, *,
             one: str = "thing", many: str = "things", people: bool = False, shown: int = SHOWN,
             none: str = "Nothing yet") -> Panel:
    rows = [_fold(t, it, act, expand=expand, compose=compose, initial=people) for it in items]
    return fold_panel(t, title, counts_line(items, one, many, none=none), rows, shown=shown, empty=none + ".")


def _people(t: Theme, view: MeView, expand: str | None, compose: str | None, act: Actions) -> Panel:
    order = sorted(view.people, key=lambda i: (0 if i.ref in (expand, compose) else 2 if "you" in i.tags else 3,
                                               -i.since))
    return _section(t, "People", order, act, expand, compose, one="person", many="people", people=True, shown=5,
                    none="Nobody yet")


def _personality(t: Theme, view: MeView, expand: str | None, compose: str | None, act: Actions) -> Panel:
    """A few short trait lines: one per side of you first (how you talk, what you care about, running jokes,
    stories, how you are with people), the rest behind "All N"."""
    firsts, rest = [], []
    for aspect, title in ASPECT_TITLES.items():
        for n, it in enumerate(view.personality.get(aspect) or []):
            row = _fold(t, it, act, expand=expand, compose=compose, title=title,
                        line=it.summary or one_line(it.text, 90))
            (firsts if n == 0 else rest).append(row)
    rows = firsts + rest
    sides = sum(1 for a in ASPECT_TITLES if view.personality.get(a))
    headline = f"{len(rows)} traits, across {sides} sides of you" if rows else "Still getting to know you"
    return fold_panel(t, "Personality", headline, rows, shown=max(3, len(firsts)), empty="Still getting to know you.")


def _unsure(t: Theme, view: MeView, act: Actions) -> Panel:
    rows = []
    for it in view.unsure[:4]:
        def make(it: Item = it) -> QWidget:
            box = QWidget()
            box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            col = QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            row = ItemRow(t, it.ref, "", it.text, it.source, tags=[], evidence=_evidence(it),
                          placeholder="Say what was really said, or what's true", pending=_PENDING.get(it.ref),
                          parent=box)
            row.correct.connect(lambda _ref, words, item=it: act.correct_item(item, words))
            row.forget.connect(lambda _ref, item=it: act.forget_item(item))
            col.addWidget(row)
            right = Button(t, "That's right", glyph="check", style="outline", size="small", parent=box)

            def confirmed(_c: bool = False, item: Item = it, pill: Button = right) -> None:
                act.confirm_item(item)
                pill.setText("Confirmed")
                pill.setEnabled(False)
                pill.updateGeometry()

            right.clicked.connect(confirmed)
            line = QHBoxLayout()
            line.addWidget(right)
            line.addStretch(1)
            col.addLayout(line)
            return box
        fold = FoldRow(t, "", it.summary or one_line(it.text, 120), _when(it), body=make, expanded=it.ref in _PENDING)
        fold.item = it  # type: ignore[attr-defined]
        rows.append(fold)
    n = len(view.unsure)
    headline = "Nothing unclear this week" if not n else f"{n} line{'s' if n != 1 else ''} from calls, where words " \
                                                         "can be misheard"
    return fold_panel(t, "Not sure Yuki heard these right", headline, rows, shown=2, empty="Nothing unclear this week.")


def build(t: Theme, view: MeView, *, expand: str | None = None, compose: str | None = None,
          act: Actions = NOOP, pending: dict[str, dict] | None = None, progressive: bool = True) -> QWidget:
    """The page. ``pending``: corrections not applied yet (shown as "Updating…" in the user's words, their
    rows open). ``progressive`` is accepted for older callers: the page's rows are painted and cheap, so it is
    built in one go."""
    del progressive
    _PENDING.clear()
    _PENDING.update(pending or {})
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(24)
    refresh = PortraitStamp(t, view.portrait_at)
    refresh.setToolTip("Rebuild your portrait now from what Yuki has seen")
    refresh.clicked.connect(act.refresh_portrait)
    col.addWidget(page_header(t, "Me", "What Yuki understands about you. Open a line to see why, or to put it "
                                       "right in your own words.", [refresh], right_top=34))
    if view.questions_open:
        banner = NavCard(t, "Yuki has questions for you", str(view.questions_open),
                         "Answer them on the Yuki page", lit=True, height=92)
        banner.clicked.connect(lambda: act.navigate("yuki", "questions"))
        col.addWidget(banner)
    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=2, spacing=14, min_col=340)   # one column in a narrow window
    from yuki.ui.window.pages.fold import todos_section

    parts = (
        ("work", 0, lambda: _section(t, "Work and projects", view.work, act, expand, compose)),
        ("todos", 0, lambda: todos_section(t, list(getattr(view, "todos", []) or []), act)),
        ("interests", 0, lambda: _section(t, "Interests", view.interests, act, expand, compose, one="interest",
                                          many="interests")),
        ("routines", 0, lambda: _section(t, "Routines", view.routines, act, expand, compose, one="pattern",
                                         many="patterns", shown=2)),
        ("people", 1, lambda: _people(t, view, expand, compose, act)),
        ("personality", 1, lambda: _personality(t, view, expand, compose, act)),
        ("unsure", 1, lambda: _unsure(t, view, act)),
    )
    page.sections = {}  # type: ignore[attr-defined]
    for key, column, make in parts:
        panel = make()
        panel.setParent(grid)
        cards.add(panel, column)
        page.sections[key] = panel  # type: ignore[attr-defined]
    col.addWidget(grid)
    col.addStretch(1)
    page.deferred = []  # type: ignore[attr-defined]
    return page


def _todo_list(t: Theme, view: MeView, act: Actions) -> Panel:
    from yuki.ui.window.pages.fold import todos_section

    return todos_section(t, list(getattr(view, "todos", []) or []), act)
