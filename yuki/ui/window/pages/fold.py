"""Scannable sections for the Me and Yuki pages (round 2, the user: "far too much text; make them
user-friendly and scannable").

A section is a module with its title, a one-line headline (how many, and the first few names), the top few
items as short rows (:class:`~yuki.ui.window.widgets.FoldRow`: a short title and one quiet line, the date as
metadata), and the rest behind "All N". The full text, where it came from and every action (Why?, Not right,
Done, Not needed, Not mine, Answer...) are in the row's fold, built the first time it opens.

The short title of an item is its subject (a person's name, a project); its one line is, for now, the first
sentence of what memory holds. The model-written short summary this should be (memory has no such field yet)
is relayed as a request: a ``summary`` (at most eight words) for each portrait fact, a ``title`` (three to six
words) for each to-do and each know-how entry, written where they are extracted.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.data import first_sentence
from yuki.ui.window.theme import Theme
from yuki.ui.window.widgets import Button, Divider, FoldRow, Led, Panel, text_label

#: Rows a section shows before "All N".
SHOWN = 3


def one_line(text: str, limit: int = 110) -> str:
    """The quiet line under a row's title: the first sentence (the model-written summary once memory has it)."""
    return first_sentence(" ".join((text or "").split()), limit).rstrip(".")


def names_line(names: Sequence[str], total: int, *, none: str = "Nothing yet") -> str:
    """A section's headline: "Corvana, Warp and 9 more"."""
    names = [n for n in names if n][:3]
    if not names:
        return none
    rest = total - len(names)
    if rest > 0:
        return ", ".join(names) + f" and {rest} more"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def fold_panel(t: Theme, title: str, headline: str, rows: list[QWidget], *, shown: int = SHOWN,
               empty: str = "Nothing yet.", extra_top: QWidget | None = None) -> Panel:
    """A section: its title and one-line headline, the first ``shown`` rows, the rest behind "All N"."""
    more = Button(t, f"All {len(rows)}", style="key", size="mini") if len(rows) > shown else None
    panel = Panel(t, title, headline, right=more)
    panel.body.setSpacing(2)
    if extra_top is not None:
        panel.body.addWidget(extra_top)
        panel.body.addSpacing(6)
    later: list[QWidget] = []
    for i, row in enumerate(rows):
        row.setParent(panel)
        if i:
            panel.body.addWidget(Divider(t, parent=panel))
        panel.body.addWidget(row)
        if i >= shown:
            row.hide()
            later.append(row)
    if later:
        dividers = [panel.body.itemAt(k).widget() for k in range(panel.body.count())
                    if isinstance(panel.body.itemAt(k).widget(), Divider)][shown - 1:]
        for d in dividers:
            d.hide()

        def toggle() -> None:
            show = later[0].isHidden()
            for w in later + dividers:
                w.setVisible(show)
            more.setText("Fewer" if show else f"All {len(rows)}")
            more.updateGeometry()

        more.clicked.connect(toggle)
    if not rows and empty:
        panel.body.addWidget(text_label(t, empty, "body", "text_3", wrap=True, parent=panel))
    panel.rows = rows  # type: ignore[attr-defined]
    return panel


def todo_body(t: Theme, todo, act, *, wrong_word: str = "Not mine", buttons: list | None = None) -> QWidget:
    """A to-do's fold: where it came from (and your words), then Not needed and Not mine."""
    box = QWidget()
    box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(6)
    if len(todo.text) > 60 or getattr(todo, "title", ""):
        col.addWidget(text_label(t, todo.text, "body", "text_2", wrap=True, selectable=True, parent=box))
    src = ". ".join(x for x in ((todo.due[:1].upper() + todo.due[1:]) if todo.due else "", todo.source) if x)
    if todo.quote:
        src += f". “{todo.quote}”"
    col.addWidget(text_label(t, src, "micro", "text_3", wrap=True, parent=box))
    row = QHBoxLayout()
    row.setSpacing(8)
    row.setContentsMargins(0, 4, 0, 0)
    for word, how in (("Not needed", "not_needed"), (wrong_word, "wrong")):
        b = Button(t, word, style="outline", size="small", parent=box)
        if how == "wrong":
            b.setToolTip("Captured by mistake. Yuki takes it off and learns not to capture the like again.")
        b.clicked.connect(lambda _c=False, how=how, b=b: _close(todo, how, b, buttons or [], act))
        (buttons if buttons is not None else []).append(b)
        row.addWidget(b)
    row.addStretch(1)
    col.addLayout(row)
    return box


def _close(todo, how: str, pill: Button, pills: list, act) -> None:
    for p in pills:
        try:
            p.setEnabled(False)
        except RuntimeError:
            pass
    pill.setText({"done": "Done ✓", "not_needed": "Removed", "wrong": "Removed"}[how])
    pill.updateGeometry()
    act.todo_close(todo.raw, how)


def todo_row(t: Theme, todo, act, *, wrong_word: str = "Not mine") -> FoldRow:
    """One to-do: an LED (lit when due within three days), its words, when it's due, and Done on the right;
    the fold holds where it came from, Not needed and Not mine."""
    from yuki.ui.window.pages.today import TodoPanel

    due = todo.due[4:] if todo.due.startswith("due ") else todo.due
    buttons: list = []
    done = Button(t, "Done", glyph="check", style="default", size="small")
    buttons.append(done)
    done.clicked.connect(lambda _c=False: _close(todo, "done", done, buttons, act))
    words = getattr(todo, "title", "") or one_line(todo.text, 90)   # memory's short title when it has one
    row = FoldRow(t, words, "", due, led="on" if TodoPanel.soon(todo) else "off",
                  trailing=done, body=lambda: todo_body(t, todo, act, wrong_word=wrong_word, buttons=buttons))
    row.todo = todo  # type: ignore[attr-defined]
    return row


def todos_section(t: Theme, todos: list, act, *, title: str = "Your to-dos", wrong_word: str = "Not mine",
                  shown: int = 4) -> Panel:
    from yuki.ui.window.pages.today import TodoPanel

    soon = sum(TodoPanel.soon(x) for x in todos)
    headline = "Nothing on your list" if not todos else (
        f"{len(todos)} open" + (f", {soon} due in the next three days" if soon else ""))
    rows = [todo_row(t, x, act, wrong_word=wrong_word) for x in todos]
    return fold_panel(t, title, headline, rows, shown=shown, empty="Nothing on your list.")


def lines_body(t: Theme, lines: Sequence[tuple[str, str]]) -> Callable[[], QWidget]:
    """A fold of plain lines: (text, quiet note) pairs."""
    def make() -> QWidget:
        box = QWidget()
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        for text, note in lines:
            if text:
                col.addWidget(text_label(t, text, "body", "text_2", wrap=True, selectable=True, parent=box))
            if note:
                col.addWidget(text_label(t, note, "micro", "text_3", wrap=True, parent=box))
        return box
    return make


__all__ = ["SHOWN", "fold_panel", "lines_body", "names_line", "one_line", "todo_row", "todos_section", "Led"]
