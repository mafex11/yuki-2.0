"""Chat: the live conversation with Yuki, the same thread as the Alt+Shift overlay and voice.

Every message is its own bubble the moment it is sent, typed here, in the
overlay, or said out loud; Yuki's replies stream in as they are spoken, with a
"worked for 12 s, 5 steps" fold under each response listing what it did.
Typing here goes to yuki-ui's runtime over the window's pipe; when Yuki isn't
running the page says so and keeps what was typed.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.paint import paint_surface
from yuki.ui.window.theme import PAGE_PAD, SPACE, Theme, css, variable_font
from yuki.ui.window.widgets import Button, page_header


class _InputPill(QWidget):
    """The composer: a glass box around a multi-line input (Enter sends, Shift+Enter adds a line) with the send
    key inside it on the right, brighter while it holds the keyboard."""

    def __init__(self, t: Theme, act: Actions) -> None:
        super().__init__()
        from yuki.ui.overlay import AskInput

        self.t = t
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 12, 12, 12)
        row.setSpacing(SPACE["s"])
        self.edit = AskInput()
        self.edit.setFont(variable_font(14.5, "Regular"))
        self.edit.setPlaceholderText("Ask Yuki anything, or tell it what to do")
        self.edit.setStyleSheet(f"QPlainTextEdit {{ background: transparent; color: {css(t.text)}; border: none;"
                                f" selection-background-color: {css(t.accent_soft)}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, t.faint)
        self.edit.setPalette(pal)
        self.edit.setMaximumHeight(self.edit.preferred_height())
        self.edit.wants_height.connect(lambda h: (self.edit.setMaximumHeight(h), self.edit.setMinimumHeight(h)))
        self.edit.setMinimumHeight(self.edit.preferred_height())
        row.addWidget(self.edit, 1, Qt.AlignmentFlag.AlignVCenter)
        self.edit.installEventFilter(self)
        self.send = Button(t, glyph="send", style="key", size="small")
        self.send.setFixedSize(34, 30)
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

    def eventFilter(self, obj: object, ev: object) -> bool:  # noqa: D102 - the line brightens with the keyboard
        from PySide6.QtCore import QEvent

        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return False

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        paint_surface(p, r, 18, self.t.level("display"), border=self.t.text_3 if self.edit.hasFocus() else None)
        p.end()


class _Hint(QWidget):
    """"Enter sends, Shift Enter for a new line, Alt Shift X stops what Yuki is doing", the keys as keycaps."""

    PARTS = ((("Enter",), "sends,"), (("Shift", "Enter"), "for a new line,"), (("Alt", "Shift", "X"),
                                                                              "stops what Yuki is doing"))

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setFixedHeight(24)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtGui import QFontMetrics

        from yuki.ui.window.paint import paint_key

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        kf, wf = variable_font(11, "Regular"), variable_font(12, "Regular")
        kfm, wfm = QFontMetrics(kf), QFontMetrics(wf)
        total = 0.0
        for keys, words in self.PARTS:
            total += sum(kfm.horizontalAdvance(k) + 14 + 4 for k in keys) + wfm.horizontalAdvance(words) + 12
        x = max(0.0, (self.width() - total) / 2)
        for keys, words in self.PARTS:
            for k in keys:
                w = kfm.horizontalAdvance(k) + 14
                paint_key(p, QRectF(x, 2, w, 20), self.t, radius=10.0, bottom=2.0)   # a capsule keycap
                p.setFont(kf)
                p.setPen(self.t.text_2)
                p.drawText(QRectF(x, 2, w, 18), Qt.AlignmentFlag.AlignCenter, k)
                x += w + 4
            p.setFont(wf)
            p.setPen(self.t.text_3)
            p.drawText(QRectF(x + 2, 0, wfm.horizontalAdvance(words) + 4, 24), Qt.AlignmentFlag.AlignVCenter, words)
            x += wfm.horizontalAdvance(words) + 12
        p.end()


def build(t: Theme, model: Any, *, act: Actions = NOOP, connected: bool = True) -> QWidget:
    """``model``: the window's replica of the chat thread (:class:`~yuki.ui.chat_model.ChatModel`)."""
    from yuki.ui.chatview import ChatThreadView

    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(0)
    stop = Button(t, "Stop", glyph="close", style="destructive", size="small")
    stop.setToolTip("Stop what Yuki is doing (Alt+Shift+X)")
    stop.clicked.connect(act.cancel)
    stop.setVisible(False)
    note = "One thread with Yuki: what you type here, in the ask bar, or say out loud."
    if not connected:
        note = "Yuki isn't running right now, so it can't answer. Start it from the Start menu and this fills in."
    col.addWidget(page_header(t, "Chat", note, [stop]))
    col.addSpacing(16)
    thread = ChatThreadView(model, lambda: t, max_bubble=640, margins=(0, 0, 8, 8), look="window")
    thread.reply_nudge.connect(act.reply_nudge)
    thread.do_nudge.connect(act.do_nudge)
    col.addWidget(thread, 1)
    col.addSpacing(14)
    field = _InputPill(t, act)
    field.setEnabled(connected)
    col.addWidget(field)
    col.addSpacing(10)
    col.addWidget(_Hint(t))

    def sync_running(*_a: object) -> None:
        stop.setVisible(any(i.running for i in model.items[-20:] if i.kind == "assistant"))

    unsub = model.subscribe(lambda what, item: sync_running())
    sync_running()
    page.thread = thread  # type: ignore[attr-defined]
    page.field = field  # type: ignore[attr-defined]
    listeners = [unsub, thread._unsub]   # plain Python: safe to call once the widgets are gone
    page.destroyed.connect(lambda *_: [f() for f in listeners])
    return page
