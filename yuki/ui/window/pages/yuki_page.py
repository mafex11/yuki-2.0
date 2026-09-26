"""Yuki: the assistant's side, with this user. How it talks to you, the personality it has with you, your
to-dos it keeps track of, what it learned to do on this PC, and the questions it has.

Nothing here is the same for every user (2026-09-27): the generic "who Yuki is" card and the rules list
are gone (the rules are moving into the system prompt for everyone; rules of your own will show under
"How Yuki talks to you"). Everything shown comes from this user's memory:

* **How Yuki talks to you** -- the reply style memory holds for you: the portrait's reply-style section
  (length, tone, formality, swearing, humour, how to reply), in your words where it has them.
* **Yuki's personality with you** -- the persona Yuki builds from how you react to it (memory's persona
  lines), and what it picked up from how you talk with people (the portrait's personality: how you talk,
  how you are with people, your running jokes).
* **What Yuki said it would do** -- memory's owner "yuki" to-dos. The user's own to-dos moved to the Me
  page (2026-09-27: "On your plate" and "Your to-dos" were the same list; now one list, on Me).
* **Learned to do** -- know-how that worked on this PC, as a short title and a plain line.

The cards sit in a masonry layout (:class:`~yuki.ui.window.widgets.Masonry`): each as tall as its content,
in balanced columns, never stretched to a neighbour's height. No tags or badges anywhere: what a tag said
is plain secondary text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import ASPECT_TITLES, Todo, YukiView, day_label, epoch, hm, plain, second_person
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import (
    Divider,
    Glass,
    Masonry,
    Panel,
    Pill,
    TextPill,
    icon_label,
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
              parent: QWidget | None = None) -> Glass:
    """An inline "say it in your words" box (hidden until its pill opens it)."""
    box = Glass(t, level="inset", radius=18, padding=SPACE["m"], parent=parent)
    box.body.setSpacing(SPACE["s"])
    if hint:
        box.body.addWidget(text_label(t, hint, "caption", "text_2", wrap=True))
    field = TextPill(t, placeholder, action=action, height=38)

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


def _line(t: Theme, panel: Panel, text: str, glyph: str, *, note: str = "") -> None:
    row = QHBoxLayout()
    row.setSpacing(SPACE["s"])
    panel.body.addLayout(row)
    row.addWidget(icon_label(t, glyph, t.text_3, "icon_small"), 0, Qt.AlignmentFlag.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(1)
    row.addLayout(col, 1)
    col.addWidget(text_label(t, text, "callout", "text", wrap=True, selectable=True))
    if note:
        col.addWidget(text_label(t, note, "caption", "text_3", wrap=True))


def _talk(t: Theme, view: YukiView) -> Panel:
    panel = Panel(t, "How Yuki talks to you", "Your reply style, from what you've told Yuki and how you talk to it.",
                  glyph="chat")
    panel.body.setSpacing(SPACE["s"])
    if view.talk:
        for line in view.talk:
            _line(t, panel, line, "chat")
        when = f"{day_label(view.talk_at)} {hm(view.talk_at)}" if view.talk_at else ""
        panel.body.addWidget(text_label(t, f"From your portrait · updated {when}" if when else "From your portrait",
                                        "caption", "text_3"))
    else:
        panel.body.addWidget(text_label(t, "Nothing about your reply style yet. Tell Yuki how you like replies, "
                                           "short, no emoji, swear back, whatever, and it shows here.",
                                        "callout", "text_3", wrap=True))
    return panel


def _personality(t: Theme, view: YukiView) -> Panel:
    panel = Panel(t, "Yuki's personality with you", "Built from how you talk with people and how you react to "
                  "Yuki. Jokes stay jokes.", glyph="emoji")
    panel.body.setSpacing(SPACE["s"])
    if view.persona:
        panel.body.addWidget(text_label(t, "How Yuki is with you", "caption_strong", "text_2"))
        for line in view.persona:
            _line(t, panel, line, "sparkle", note="Learned from how you reacted to Yuki")
    groups: dict[str, list] = {}
    for it in view.picked_up:
        groups.setdefault(it.aspect or "talk", []).append(it)
    if groups:
        if view.persona:
            panel.body.addWidget(Divider(t))
        panel.body.addWidget(text_label(t, "What it picked up from how you talk", "caption_strong", "text_2"))
    for aspect in ("talk", "people", "joke"):
        for it in groups.get(aspect, [])[:3]:
            row = QHBoxLayout()
            row.setSpacing(SPACE["s"])
            panel.body.addLayout(row)
            col = QVBoxLayout()
            col.setSpacing(1)
            row.addLayout(col, 1)
            col.addWidget(text_label(t, it.short, "callout", "text", wrap=True, selectable=True))
            extra = tag_words([x for x in it.tags if x == "joke"])
            col.addWidget(text_label(t, " · ".join(x for x in (ASPECT_TITLES.get(aspect, ''), it.source, extra) if x),
                                     "caption", "text_3", wrap=True))
    if not view.persona and not groups:
        panel.body.addWidget(text_label(t, "Still getting to know you. This fills in as you talk to Yuki and "
                                           "your friends.", "callout", "text_3", wrap=True))
    return panel


def _todo_row(t: Theme, todo: Todo, act: Actions, parent: QWidget, *, wrong_word: str = "Not mine") -> QWidget:
    box = QWidget(parent)
    box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(3)
    head = QHBoxLayout()
    head.setSpacing(SPACE["s"])
    lay.addLayout(head)
    head.addWidget(text_label(t, todo.text, "body", "text", wrap=True, selectable=True), 1)
    due = todo.due[:1].upper() + todo.due[1:] if todo.due else ""
    src = " · ".join(x for x in (due, todo.source) if x) + (f" · “{todo.quote}”" if todo.quote else "")
    lay.addWidget(text_label(t, src, "caption", "text_3", wrap=True))
    actions = QHBoxLayout()
    actions.setSpacing(6)
    lay.addLayout(actions)
    actions.addStretch(1)
    done = Pill(t, "Done", glyph="check", style="glass", size="small")
    not_needed = Pill(t, "Not needed", style="quiet", size="small")
    wrong = Pill(t, wrong_word, style="quiet", size="small")
    wrong.setToolTip("Captured by mistake. Yuki takes it off and learns not to capture the like again.")
    pills = (done, not_needed, wrong)
    for pill, how in ((done, "done"), (not_needed, "not_needed"), (wrong, "wrong")):
        actions.addWidget(pill)

        def clicked(_c: bool = False, how: str = how, pill: Pill = pill) -> None:
            for p in pills:
                p.setEnabled(False)
            pill.setText({"done": "Done ✓", "not_needed": "Removed", "wrong": "Removed"}[how])
            pill.updateGeometry()
            act.todo_close(todo.raw, how)

        pill.clicked.connect(clicked)
    return box


#: To-dos shown before "All N" (the soonest due first, as memory orders them).
TODOS_SHOWN = 6


def todos_panel(t: Theme, todos: list[Todo], act: Actions) -> Panel:
    """The user's to-dos, with Done / Not needed / Not mine (on the Me page since 2026-09-27)."""
    more = Pill(t, f"All {len(todos)}", style="quiet", size="small")
    panel = Panel(t, "Your to-dos", "What you said you'd do, or what people are waiting on. Yuki reminds you, "
                  "you tick them off.", glyph="flag", right=more)
    panel.body.setSpacing(SPACE["s"])
    later: list[QWidget] = []
    for i, todo in enumerate(todos):
        box = QWidget(panel)
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SPACE["s"])
        if i:
            lay.addWidget(Divider(t))
        lay.addWidget(_todo_row(t, todo, act, box))
        panel.body.addWidget(box)
        if i >= TODOS_SHOWN:
            box.hide()
            later.append(box)
    if later:
        def toggle() -> None:
            show = later[0].isHidden()
            for b in later:
                b.setVisible(show)
            more.setText("Fewer" if show else f"All {len(todos)}")
            more.updateGeometry()

        more.clicked.connect(toggle)
    else:
        more.hide()
    if not todos:
        panel.body.addWidget(text_label(t, "Nothing on your list.", "callout", "text_3"))
    return panel


def _todos(t: Theme, view: Any, act: Actions) -> Panel:   # kept for older callers
    return todos_panel(t, view.todos, act)


def _promises(t: Theme, view: YukiView, act: Actions) -> Panel:
    """What Yuki itself said it would do (memory's owner "yuki"), apart from the user's to-dos."""
    panel = Panel(t, "What Yuki said it would do", "Things you handed Yuki for later, or it offered to do.",
                  glyph="pin")
    panel.body.setSpacing(SPACE["s"])
    for i, todo in enumerate(view.promises[:6]):
        if i:
            panel.body.addWidget(Divider(t))
        panel.body.addWidget(_todo_row(t, todo, act, panel, wrong_word="Never asked"))
    return panel


def _learned(t: Theme, view: YukiView) -> Panel:
    panel = Panel(t, "Learned to do", "Ways of getting things done on this PC that worked, kept so it's faster "
                  "next time.", glyph="bulb")
    panel.body.setSpacing(SPACE["s"])
    if view.knowhow:
        for i, k in enumerate(view.knowhow[:10]):
            if i:
                panel.body.addWidget(Divider(t))
            col = QVBoxLayout()
            col.setSpacing(2)
            panel.body.addLayout(col)
            col.addWidget(text_label(t, k.title, "body_strong", "text", wrap=True))
            if k.line:
                col.addWidget(text_label(t, k.line, "callout", "text_2", wrap=True, selectable=True))
            if k.app:
                col.addWidget(text_label(t, f"In {k.app[:1].upper() + k.app[1:]}", "caption", "text_3"))
    else:
        empty = Glass(t, level="inset", radius=16, padding=SPACE["m"], parent=panel)
        empty.body.addWidget(text_label(t, "Nothing yet.", "body_strong", "text_2"))
        empty.body.addWidget(text_label(t, "When Yuki finds a way that works here, say hovering YouTube's volume "
                                           "button to reach the slider, it keeps it and shows it here.", "caption",
                                        "text_3", wrap=True))
        panel.body.addWidget(empty)
    return panel


def _questions(t: Theme, view: YukiView, act: Actions) -> Panel:
    qs = Panel(t, "Questions", "When Yuki would otherwise guess, it asks. Your answers become memory, marked as "
                               "coming from you.", glyph="question")
    qs.body.setSpacing(SPACE["s"])
    if view.waiting:
        qs.body.addWidget(text_label(t, "Waiting to ask", "caption_strong", "text_2"))
        for q in view.waiting:
            card = Glass(t, level="inset", radius=18, padding=SPACE["m"], parent=qs)
            qs.body.addWidget(card)
            card.body.setSpacing(6)
            # the question leads: larger, semibold, full-strength ink; why it asks sits under it, small and dim
            card.body.addWidget(text_label(t, plain(second_person(q["question"])), "headline", "text", wrap=True))
            if q.get("evidence"):
                card.body.addWidget(text_label(t, f"Why it asks: {plain(second_person(q['evidence']))}", "caption",
                                               "text_3", wrap=True))
            row = QHBoxLayout()
            card.body.addLayout(row)
            row.addStretch(1)
            answer_box = _composer(t, "Your answer, in your words", "Tell Yuki",
                                   lambda words, ref=q["id"]: act.answer_question(ref, words), parent=card)
            never = Pill(t, "Don't ask", style="quiet", size="small")
            never.clicked.connect(lambda _c=False, ref=q["id"], box=card: (act.skip_question(ref, True),
                                                                           box.setVisible(False)))
            skip = Pill(t, "Skip", style="quiet", size="small")
            skip.clicked.connect(lambda _c=False, ref=q["id"], box=card: (act.skip_question(ref, False),
                                                                          box.setVisible(False)))
            answer = Pill(t, "Answer", glyph="edit", style="tinted", tint=t.kind("question"), size="small")
            answer.clicked.connect(lambda _c=False, box=answer_box: (box.setVisible(True), box.field.edit.setFocus()))
            row.addWidget(never)
            row.addWidget(skip)
            row.addWidget(answer)
            card.body.addWidget(answer_box)
    if view.asked:
        qs.body.addWidget(text_label(t, "Asked before", "caption_strong", "text_2"))
        for q in view.asked[:4]:
            row = QHBoxLayout()
            row.setSpacing(SPACE["m"])
            qs.body.addLayout(row)
            c = QVBoxLayout()
            c.setSpacing(2)
            row.addLayout(c, 1)
            c.addWidget(text_label(t, plain(second_person(q["question"])), "body_strong", "text", wrap=True))
            if q.get("answer"):
                c.addWidget(text_label(t, f"You: “{q['answer']}”", "caption", "text_3", wrap=True, selectable=True))
            word = {"answered": "Answered", "skipped": "Skipped", "never": "Won't ask", "asked": "Asked"}
            row.addWidget(text_label(t, f"{word.get(q['status'], q['status'])} {_when(q['answered_at'] or q['at'])}",
                                     "caption", "text_3"), 0, Qt.AlignmentFlag.AlignTop)
    if not view.waiting and not view.asked:
        qs.body.addWidget(text_label(t, "No questions right now.", "callout", "text_3"))
    return qs


def build(t: Theme, view: YukiView, *, act: Actions = NOOP) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    col.addWidget(page_header(t, "Yuki", "Yuki's side, with you: how it talks to you, its personality with you, "
                                         "and what it learned. Your to-dos are on the Me page."))
    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=2, spacing=SPACE["l"], min_col=380)
    for make in (lambda: _talk(t, view), lambda: _personality(t, view),
                 (lambda: _promises(t, view, act)) if view.promises else None,
                 lambda: _learned(t, view), lambda: _questions(t, view, act)):
        if make is not None:
            card = make()
            card.setParent(grid)
            cards.addWidget(card)
    col.addWidget(grid)
    col.addStretch(1)
    return page
