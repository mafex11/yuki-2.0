"""Yuki: the assistant's side. Who it is, how it talks, your rules, what it learned, what it owes and asks."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import YukiView, day_label, epoch, second_person
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import Chip, Divider, Glass, Panel, Pill, TextPill, icon_label, page_header, text_label


def _bullets(t: Theme, panel: Panel, lines: list[str], glyph: str) -> None:
    for line in lines:
        row = QHBoxLayout()
        row.setSpacing(SPACE["s"])
        row.addWidget(icon_label(t, glyph, t.accent, "icon_small"), 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text_label(t, line, "callout", "text", wrap=True), 1)
        panel.body.addLayout(row)


def _when(value) -> str:
    at = epoch(value)
    if at is None:
        return ""
    d = datetime.fromtimestamp(at)
    return f"{day_label(at)} {d:%H:%M}"


def _due(value) -> str:
    if not value:
        return ""
    if len(str(value)) == 10:
        d = datetime.fromisoformat(str(value))
        return f"due {day_label(d.timestamp() + 43200)}"
    at = epoch(value)
    return f"due {day_label(at)} {datetime.fromtimestamp(at):%H:%M}" if at else ""


def _composer(t: Theme, placeholder: str, action: str, on_send, *, hint: str = "") -> Glass:
    """An inline "say it in your words" box (hidden until its pill opens it)."""
    box = Glass(t, level="inset", radius=18, padding=SPACE["m"])
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
    box.setVisible(False)
    return box


def build(t: Theme, view: YukiView, *, act: Actions = NOOP) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])
    col.addWidget(page_header(t, "Yuki", "Yuki's side: who it is, the rules you gave it, what it has learned, "
                                         "and what it is waiting on."))

    top = QHBoxLayout()
    top.setSpacing(SPACE["l"])
    who = Panel(t, "Who Yuki is", "", glyph="yuki")
    _bullets(t, who, view.persona, "sparkle")
    talk = Panel(t, "How Yuki talks to you", "", glyph="chat")
    _bullets(t, talk, view.talk, "chat")
    top.addWidget(who, 1)
    top.addWidget(talk, 1)
    col.addLayout(top)

    add = Pill(t, "Add a rule", glyph="edit", style="quiet", size="small")
    rules = Panel(t, "Your rules for Yuki", "In your own words. Yuki follows these every time. Remove one and it "
                                             "stops at once.", glyph="shield", right=add)
    rules.body.setSpacing(SPACE["s"])
    new_rule = _composer(t, "e.g. “always answer me in one line”", "Add", act.add_rule,
                         hint="Say the rule the way you'd say it to Yuki.")
    add.clicked.connect(lambda: (new_rule.setVisible(not new_rule.isVisible()), new_rule.field.edit.setFocus()))
    rules.body.addWidget(new_rule)
    for i, r in enumerate(view.rules):
        if i:
            rules.body.addWidget(Divider(t))
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        c = QVBoxLayout()
        c.setSpacing(3)
        if r["kind"] == "rule":
            c.addWidget(text_label(t, f"“{r['quote'] or r['text']}”", "body", "text", wrap=True, selectable=True))
            c.addWidget(text_label(t, f"You said this {_when(r['at'])}", "caption", "text_3"))
        else:
            c.addWidget(text_label(t, second_person(r["text"]), "body", "text", wrap=True, selectable=True))
            c.addWidget(text_label(t, f"From what you said {_when(r['at'])}: “{r['quote']}”", "caption", "text_3",
                                   wrap=True))
        row.addLayout(c, 1)
        remove = Pill(t, "Remove", glyph="delete", style="quiet", size="small")

        def removed(_c: bool = False, ref: str = r["ref"], pill: Pill = remove) -> None:
            pill.setEnabled(False)
            pill.setText("Removing…")
            act.revoke_rule(ref)

        remove.clicked.connect(removed)
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)
        rules.body.addLayout(row)
    if not view.rules:
        rules.body.addWidget(text_label(t, "No rules yet. Tell Yuki one any time, here or out loud.", "callout",
                                        "text_3"))
    col.addWidget(rules)

    mid = QHBoxLayout()
    mid.setSpacing(SPACE["l"])
    learned = Panel(t, "What Yuki has learned to do", "Ways of getting things done on this PC that worked, kept "
                                                      "so it's faster next time.", glyph="bulb")
    if view.knowhow:
        for k in view.knowhow[:6]:
            learned.body.addWidget(text_label(t, k, "callout", "text", wrap=True, selectable=True))
    else:
        empty = Glass(t, level="inset", radius=16, padding=SPACE["m"])
        empty.body.addWidget(text_label(t, "Nothing yet.", "body_strong", "text_2"))
        empty.body.addWidget(text_label(t, "When Yuki finds a way that works here (say, the fastest way to open "
                                           "a site in Arc), it keeps it and shows it here.", "caption", "text_3",
                                        wrap=True))
        learned.body.addWidget(empty)
    learned.body.addStretch(1)
    promises = Panel(t, "Yuki's promises", "What Yuki said it would do, and what it will remind you of.",
                     glyph="pin")
    promises.body.setSpacing(SPACE["s"])
    for p in view.promised:
        row = QHBoxLayout()
        row.addWidget(text_label(t, p["text"], "callout", "text", wrap=True), 1)
        row.addWidget(Chip(t, "you", "Yuki will do"), 0, Qt.AlignmentFlag.AlignTop)
        promises.body.addLayout(row)
        promises.body.addWidget(text_label(t, f"You asked {_when(p['since'])}", "caption", "text_3"))
    if view.reminding:
        promises.body.addWidget(Divider(t))
        promises.body.addWidget(text_label(t, "You said you'd…  (Yuki reminds you)", "caption_strong", "text_2"))
        for p in view.reminding[:4]:
            row = QHBoxLayout()
            row.setSpacing(SPACE["s"])
            row.addWidget(text_label(t, second_person(p["text"]), "callout", "text", wrap=True), 1)
            due = _due(p.get("due"))
            if due:
                row.addWidget(text_label(t, due, "caption", "text_3"), 0, Qt.AlignmentFlag.AlignTop)
            promises.body.addLayout(row)
    if not view.promised and not view.reminding:
        promises.body.addWidget(text_label(t, "Nothing promised right now.", "callout", "text_3"))
    promises.body.addStretch(1)
    mid.addWidget(learned, 1)
    mid.addWidget(promises, 1)
    col.addLayout(mid)

    qs = Panel(t, "Questions", "When Yuki would otherwise guess, it asks. Your answers become memory, marked as "
                               "coming from you.", glyph="question")
    qs.body.setSpacing(SPACE["s"])
    if view.waiting:
        qs.body.addWidget(text_label(t, "Waiting to ask", "caption_strong", "text_2"))
        for q in view.waiting:
            card = Glass(t, level="inset", radius=18, padding=SPACE["m"])
            card.body.setSpacing(6)
            card.body.addWidget(text_label(t, q["question"].replace("the user", "you"), "body_strong", wrap=True))
            if q.get("evidence"):
                card.body.addWidget(text_label(t, f"Why it asks: {second_person(q['evidence'])}", "caption",
                                               "text_3", wrap=True))
            answer_box = _composer(t, "Your answer, in your words", "Tell Yuki",
                                   lambda words, ref=q["id"]: act.answer_question(ref, words))
            row = QHBoxLayout()
            row.addStretch(1)
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
            card.body.addLayout(row)
            card.body.addWidget(answer_box)
            qs.body.addWidget(card)
    if view.asked:
        qs.body.addWidget(text_label(t, "Asked before", "caption_strong", "text_2"))
        for q in view.asked[:4]:
            row = QHBoxLayout()
            row.setSpacing(SPACE["m"])
            c = QVBoxLayout()
            c.setSpacing(2)
            c.addWidget(text_label(t, q["question"].replace("the user", "you"), "callout", "text", wrap=True))
            if q.get("answer"):
                c.addWidget(text_label(t, f"You: “{q['answer']}”", "caption", "text_2", wrap=True, selectable=True))
            row.addLayout(c, 1)
            word = {"answered": "Answered", "skipped": "Skipped", "never": "Won't ask", "asked": "Asked"}
            row.addWidget(text_label(t, f"{word.get(q['status'], q['status'])} {_when(q['answered_at'] or q['at'])}",
                                     "caption", "text_3"), 0, Qt.AlignmentFlag.AlignTop)
            qs.body.addLayout(row)
    if not view.waiting and not view.asked:
        qs.body.addWidget(text_label(t, "No questions right now.", "callout", "text_3"))
    col.addWidget(qs)
    col.addStretch(1)
    return page
