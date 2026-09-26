"""Chat: the live conversation with Yuki, the same thread as the Alt+Shift overlay and voice.

Every message is its own bubble the moment it is sent, typed here, in the
overlay, or said out loud; Yuki's replies stream in as they are spoken, with a
"worked for 12 s · 5 steps" fold under each response listing what it did.
Typing here goes to yuki-ui's runtime over the window's pipe; when Yuki isn't
running the page says so and keeps what was typed.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.glass import paint_glass
from yuki.ui.window.theme import SPACE, Theme, css, font
from yuki.ui.window.widgets import Glass, Pill, page_header, text_label


class _InputPill(QWidget):
    """A glass pill around a multi-line input (Enter sends, Shift+Enter adds a line) and a send button."""

    def __init__(self, t: Theme, act: Actions) -> None:
        super().__init__()
        from yuki.ui.overlay import AskInput

        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 10, 8, 10)
        row.setSpacing(SPACE["s"])
        self.edit = AskInput()
        self.edit.setFont(font("body"))
        self.edit.setPlaceholderText("Ask Yuki anything, or tell it what to do…")
        self.edit.setStyleSheet(f"QPlainTextEdit {{ background: transparent; color: {css(t.text)}; border: none;"
                                f" selection-background-color: {css(t.accent_soft)}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, t.text_3)
        self.edit.setPalette(pal)
        self.edit.setMaximumHeight(self.edit.preferred_height())
        self.edit.wants_height.connect(lambda h: (self.edit.setMaximumHeight(h), self.edit.setMinimumHeight(h)))
        self.edit.setMinimumHeight(self.edit.preferred_height())
        row.addWidget(self.edit, 1, Qt.AlignmentFlag.AlignVCenter)
        self.send = Pill(t, glyph="send", style="accent")
        self.send.setToolTip("Send (Enter)")
        row.addWidget(self.send, 0, Qt.AlignmentFlag.AlignBottom)
        self.act = act
        self.edit.submitted.connect(self._submit)
        self.send.clicked.connect(lambda: self._submit(self.edit.toPlainText().strip()))

    def _submit(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.act.submit(text)
            self.edit.clear()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_glass(p, r, min(26.0, r.height() / 2), self.t.level("control"), shadow=False, painted=True)
        p.end()


def build(t: Theme, model: Any, *, act: Actions = NOOP, connected: bool = True) -> QWidget:
    """``model``: the window's replica of the chat thread (:class:`~yuki.ui.chat_model.ChatModel`)."""
    from yuki.ui.chatview import ChatThreadView

    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["m"])
    stop = Pill(t, "Stop", glyph="close", style="danger", size="small")
    stop.setToolTip("Stop what Yuki is doing (Alt+Shift+X)")
    stop.clicked.connect(act.cancel)
    stop.setVisible(False)
    note = ("Type to Yuki here. It's the same Yuki as Alt+Shift and your voice: everything said or typed shows up "
            "here as it happens.")
    if not connected:
        note = "Yuki isn't running right now, so it can't answer. Start it from the Start menu and this fills in."
    col.addWidget(page_header(t, "Chat", note, [stop]))
    box = Glass(t, level="regular", radius=26, padding=SPACE["xs"])   # frosted panel glass: text reads over it
    thread = ChatThreadView(model, lambda: t, max_bubble=620)
    thread.reply_nudge.connect(act.reply_nudge)
    box.body.addWidget(thread)
    col.addWidget(box, 1)
    field = _InputPill(t, act)
    field.setEnabled(connected)
    col.addWidget(field)
    hint = text_label(t, "Enter sends · Shift+Enter for a new line · Alt+Shift+X stops what Yuki is doing",
                      "caption", "text_3")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    col.addWidget(hint)

    def sync_running(*_a: object) -> None:
        stop.setVisible(any(i.running for i in model.items[-20:] if i.kind == "assistant"))

    unsub = model.subscribe(lambda what, item: sync_running())
    sync_running()
    page.thread = thread  # type: ignore[attr-defined]
    page.field = field  # type: ignore[attr-defined]
    listeners = [unsub, thread._unsub]   # plain Python: safe to call once the widgets are gone
    page.destroyed.connect(lambda *_: [f() for f in listeners])
    return page
