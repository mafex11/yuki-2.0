"""Yuki: the assistant's side, with this user. How it talks to you, the personality it has with you, what it
learned to do on this PC, what it said it would do, and the questions it has.

Nothing here is the same for every user (2026-09-27): everything shown comes from this user's memory:

* **How Yuki talks to you** -- the reply style memory holds for you (the portrait's reply-style section).
* **Yuki's personality with you** -- the persona Yuki builds from how you react to it, and what it picked up
  from how you talk with people.
* **Learned to do** -- know-how that worked on this PC, as a short title and a plain line.
* **What Yuki said it would do** -- memory's owner "yuki" to-dos (your own to-dos are on the Me page).
* **Questions** -- waiting ones with Answer / Skip / Don't ask, and the ones asked before with your answers.

Round 2 (the user: "far too much text"): each section is scannable (:mod:`yuki.ui.window.pages.fold`): a
one-line headline, the top items as short rows, the rest behind "All N", the full text in each row's fold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import ASPECT_TITLES, Todo, YukiView, day_label, epoch, hm, plain, natural
from yuki.ui.window.pages.fold import fold_panel, lines_body, one_line, todo_row, todos_section
from yuki.ui.window.theme import PAGE_PAD, SPACE, Theme
from yuki.ui.window.widgets import (
    Button,
    Card,
    FoldRow,
    Input,
    Masonry,
    Panel,
    page_header,
    tag_words,
    text_label,
)


def _when(value) -> str:
    at = epoch(value)
    if at is None:
        return ""
    d = datetime.fromtimestamp(at)
    return f"{day_label(at)} {d:%H:%M}"


def _composer(t: Theme, placeholder: str, action: str, on_send, *, hint: str = "",
              parent: QWidget | None = None) -> Card:
    """An inline "say it in your words" box (hidden until its button opens it)."""
    box = Card(t, level="muted", padding=SPACE["m"], parent=parent)
    box.body.setSpacing(SPACE["s"])
    if hint:
        box.body.addWidget(text_label(t, hint, "caption", "text_2", wrap=True))
    field = Input(t, placeholder, action=action, height=36)

    def send(text: str) -> None:
        text = " ".join((text or "").split())
        if text:
            on_send(text)
            field.edit.clear()
            box.setVisible(False)

    field.submitted.connect(send)
    box.body.addWidget(field)
    box.field = field  # type: ignore[attr-defined]
    box.hide()
    return box


def _empty(t: Theme, text: str, parent: QWidget | None = None) -> Card:
    """An empty state: a glass inset with a plain sentence."""
    box = Card(t, level="muted", padding=0, parent=parent)
    box.body.setContentsMargins(16, 14, 16, 14)
    box.body.addWidget(text_label(t, text, "body", "text_2", wrap=True))
    return box


def _talk(t: Theme, view: YukiView) -> Panel:
    rows = [FoldRow(t, "", one_line(line, 110), "", body=lines_body(t, [(line, "")]) if len(line) > 100 else None,
                    full=line) for line in view.talk]
    when = f"From your portrait, updated {day_label(view.talk_at)} {hm(view.talk_at)}" if view.talk_at else ""
    headline = (f"{len(rows)} things about your reply style. {when}".strip() if rows else
                "Nothing about your reply style yet")
    panel = fold_panel(t, "How Yuki talks to you", headline, rows, shown=4, empty="")
    if not rows:
        panel.body.addWidget(_empty(t, "Tell Yuki how you like replies, “keep it short”, “no emoji”, "
                                       "“swear back”, and it shows here.", panel))
    return panel


def _personality(t: Theme, view: YukiView) -> Panel:
    rows: list[QWidget] = []
    for line in view.persona:
        rows.append(FoldRow(t, "", one_line(line, 110), "", led="ok",
                            body=lines_body(t, [(line, "Learned from how you reacted to Yuki")])))
    for it in view.picked_up:
        joke = tag_words([x for x in it.tags if x == "joke"])
        note = ". ".join(x for x in (it.source, joke) if x)
        rows.append(FoldRow(t, ASPECT_TITLES.get(it.aspect or "talk", ""), it.summary or one_line(it.text, 100),
                            day_label(it.since) if it.since else "", body=lines_body(t, [(it.text, note)])))
    n_persona, n_picked = len(view.persona), len(view.picked_up)
    headline = ", ".join(x for x in (f"{n_persona} ways it is with you" if n_persona else "",
                                     f"{n_picked} it picked up from how you talk" if n_picked else "") if x) or \
        "Still getting to know you"
    return fold_panel(t, "Yuki's personality with you", headline, rows, shown=4,
                      empty="Still getting to know you. This fills in as you talk to Yuki and your friends.")


def _learned(t: Theme, view: YukiView) -> Panel:
    rows = [FoldRow(t, k.title, one_line(k.line, 100), k.app,   # the app's own name (data.learned_items)
                    body=lines_body(t, [(k.line, f"In {k.app}" if k.app else "")]))
            for k in view.knowhow[:10]]
    headline = f"{len(rows)} ways that worked on this PC" if rows else "Nothing yet"
    panel = fold_panel(t, "Learned to do", headline, rows, shown=3, empty="")
    if not rows:
        panel.body.addWidget(_empty(t, "When Yuki finds a way that works here, say hovering YouTube's volume button "
                                       "to reach the slider, it keeps it and shows it here.", panel))
    return panel


def _promises(t: Theme, view: YukiView, act: Actions) -> Panel:
    """What Yuki itself said it would do (memory's owner "yuki"), apart from the user's to-dos."""
    return todos_section(t, list(view.promises), act, title="What Yuki said it would do", wrong_word="Never asked",
                         shown=3)


def _question_body(t: Theme, q: dict, act: Actions, row_ref: list) -> QWidget:
    box = QWidget()
    box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(8)
    if q.get("evidence"):
        col.addWidget(text_label(t, f"Why it asks: {plain(natural(q['evidence']))}", "micro", "text_3",
                                 wrap=True, parent=box))
    answer_box = _composer(t, "Your answer, in your words", "Tell Yuki",
                           lambda words, ref=q["id"]: act.answer_question(ref, words), parent=box)
    row = QHBoxLayout()
    row.setSpacing(8)
    answer = Button(t, "Answer", glyph="edit", style="default", size="small", parent=box)
    skip = Button(t, "Skip", style="outline", size="small", parent=box)
    never = Button(t, "Don't ask", style="outline", size="small", parent=box)

    def gone(never_ask: bool) -> None:
        act.skip_question(q["id"], never_ask)
        if row_ref and row_ref[0] is not None:
            row_ref[0].hide()

    answer.clicked.connect(lambda: (answer_box.setVisible(True), answer_box.field.edit.setFocus()))
    skip.clicked.connect(lambda: gone(False))
    never.clicked.connect(lambda: gone(True))
    for b in (answer, skip, never):
        row.addWidget(b)
    row.addStretch(1)
    col.addLayout(row)
    col.addWidget(answer_box)
    return box


def _questions(t: Theme, view: YukiView, act: Actions) -> Panel:
    rows: list[QWidget] = []
    for i, q in enumerate(view.waiting):
        ref: list = [None]
        words = plain(natural(q["question"]))
        row = FoldRow(t, words, "", "waiting", led="on", full=words,
                      body=lambda q=q, ref=ref: _question_body(t, q, act, ref), expanded=i == 0)
        ref[0] = row
        rows.append(row)
    word = {"answered": "Answered", "skipped": "Skipped", "never": "Won't ask", "asked": "Asked"}
    for q in view.asked[:6]:
        status = f"{word.get(q['status'], q['status'])} {day_label(epoch(q['answered_at'] or q['at']) or 0)}"
        answer = q.get("answer")
        words = plain(natural(q["question"]))
        rows.append(FoldRow(t, words, f"You: “{one_line(answer, 90)}”" if answer else "", status.strip(),
                            body=lines_body(t, [(f"You: “{answer}”", "")]) if answer and len(answer) > 90
                            else None, full=words + (f"\n\nYou: “{answer}”" if answer and len(answer) <= 90 else "")))
    n_wait, n_asked = len(view.waiting), len(view.asked)
    headline = ", ".join(x for x in (f"{n_wait} waiting for you" if n_wait else "",
                                     f"{n_asked} asked before" if n_asked else "") if x) or "No questions right now"
    return fold_panel(t, "Questions", headline, rows, shown=max(4, n_wait), empty="No questions right now.")


def todos_panel(t: Theme, todos: list[Todo], act: Actions) -> Panel:
    """The user's to-dos (kept for older callers; the Me page builds them with
    :func:`yuki.ui.window.pages.fold.todos_section`)."""
    return todos_section(t, todos, act)


def _todo_row(t: Theme, todo: Todo, act: Actions, parent: QWidget, *, wrong_word: str = "Not mine") -> QWidget:
    row = todo_row(t, todo, act, wrong_word=wrong_word)
    row.setParent(parent)
    return row


def _todos(t: Theme, view: Any, act: Actions) -> Panel:   # kept for older callers
    return todos_panel(t, view.todos, act)


def build(t: Theme, view: YukiView, *, act: Actions = NOOP) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(24)
    col.addWidget(page_header(t, "Yuki", "Yuki's side, with you: how it talks to you, its personality with you, "
                                         "what it learned and what it asked. Your to-dos are on the Me page."))
    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=2, spacing=14, min_col=340)
    page.sections = {}  # type: ignore[attr-defined]
    # left: how it talks, what it learned, what it promised; right: its personality, its questions
    for key, column, make in (("talk", 0, lambda: _talk(t, view)), ("personality", 1, lambda: _personality(t, view)),
                              ("learned", 0, lambda: _learned(t, view)),
                              ("promises", 0, (lambda: _promises(t, view, act)) if view.promises else None),
                              ("questions", 1, lambda: _questions(t, view, act))):
        if make is None:
            continue
        card = make()
        card.setParent(grid)
        cards.add(card, column)
        page.sections[key] = card  # type: ignore[attr-defined]
    col.addWidget(grid)
    col.addStretch(1)
    return page
