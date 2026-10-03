"""The ask-Yuki overlay: one input, and a short stack of reply cards above it.

Lifecycle of a request, all of it animated and none of it timed with sleeps:
submit collapses the input, a card grows in where the input used to be, and a
fresh (empty) input expands below it. The card fills in when the answer arrives
and grows to fit. :data:`MAX_CARDS` cards stay visible; older ones fade away.

Memory's nudges live in the same stack (tone ``nudge``: labelled
``Yuki, 14:32, nudge``, with their own Reply), each
placed by the time the user saw it, so the stack reads as one conversation.

The card stack sits in a scroll area whose height follows its content up to
:data:`MAX_CARDS_FRACTION` of the screen; past that it scrolls (wheel anywhere on
the panel, or the thin scrollbar) and the input below it stays on screen and
keeps the keyboard. A new card or a reply filling in scrolls the stack to the
bottom; scrolling up stops that following until the user is back at the bottom.

The overlay never decides anything about the request -- it hands the text to
:mod:`yuki.ui.runtime` and renders what comes back.

**Thread mode** (:meth:`Overlay.attach_thread`, phase 2): the card stack gives way
to the live chat thread (:class:`yuki.ui.chatview.ChatThreadView` over the tray's
:class:`~yuki.ui.chat_model.ChatModel`), the same thread the Yuki window shows: a
bubble per message, typed or spoken, Yuki's replies streaming in, and each
response's steps. The card calls keep working (they return :class:`ThreadCard`
stand-ins), so the controller in :mod:`yuki.ui.app` is unchanged.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from yuki.ui import tokens as _tokens
from yuki.ui.surface import (
    ANIM_MS,
    CARD_BG,
    CARD_BORDER,
    ERROR_BG,
    ERROR_BORDER,
    TEXT_DIM,
    TEXT_PRIMARY,
    SurfaceWindow,
    ui_font,
)

#: Width of the ask bar (the Lattice design's ``.askbar``: 780 px) and its padding (18 px in).
PANEL_WIDTH = 780
PAD = 18
#: The ask bar's top, from the top of the monitor in use (the design's ``top: 64px``).
TOP_PX = 64

#: How many cards (requests, replies and nudges together) stay on screen.
MAX_CARDS = 5

#: Tallest the card stack gets, as a share of the screen's available height; a
#: longer conversation scrolls inside it. The panel's top sits at 18% of the
#: screen, so stack + input end well above the taskbar at any screen size.
MAX_CARDS_FRACTION = 0.55

#: The stack's scrollbar: thin, only there when it can scroll (Lattice hairline greys, :mod:`yuki.ui.tokens`).
SCROLL_STYLE = (
    "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
    "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0 2px 0; }"
    f"QScrollBar::handle:vertical {{ background: {_tokens.COLOR['hair2']}; border-radius: 4px; min-height: 24px; }}"
    f"QScrollBar::handle:vertical:hover {{ background: {_tokens.COLOR['mark']}; }}"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
)

#: After the overlay is shown, how long losing activation does not count as the
#: user clicking away. Covers the transient activate/deactivate messages of the
#: foreground hand-over (:func:`yuki.ui.focus.force_foreground`). A UI timer, not
#: a wait: nothing blocks on it.
FOCUS_GRACE_MS = 200

CardTone = Literal["reply", "question", "error", "context", "nudge"]


class AskInput(QPlainTextEdit):
    """The text field. Enter submits, Shift+Enter adds a line, Esc hides.

    It is a :class:`QPlainTextEdit` rather than a line edit only because of
    Shift+Enter; it renders as one line and grows to a few lines at most.

    Signals:
        submitted: Non-empty text, with surrounding whitespace removed.
        escaped: Esc was pressed.
        wants_height: The preferred height changed (text wrapped onto a new line).
    """

    submitted = Signal(str)
    escaped = Signal()
    wants_height = Signal(int)

    MAX_LINES = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(ui_font(13))
        self.setPlaceholderText("Ask Yuki…")
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        from yuki.ui.window.theme import css, theme

        self.setStyleSheet(
            "QPlainTextEdit {"
            "  background: transparent;"
            f"  color: {_tokens.COLOR['text']};"
            f"  selection-background-color: {css(theme().accent_soft)};"
            "  border: none; padding: 0px;"
            "}"
        )
        self.document().documentLayout().documentSizeChanged.connect(
            lambda *_: self.wants_height.emit(self.preferred_height())
        )

    def preferred_height(self) -> int:
        """Height that fits the current text, capped at :attr:`MAX_LINES`."""
        line = self.fontMetrics().lineSpacing()
        lines = max(1.0, self.document().documentLayout().documentSize().height())
        return int(line * min(lines, float(self.MAX_LINES))) + 6

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102 - Qt override
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
            return
        super().keyPressEvent(event)


class ReplyCard(QWidget):
    """One exchange: the request in dim text, and Yuki's answer below it.

    The card owns its reveal and growth animation. The height is animated and the
    window resizes to whatever the layout asks for on every frame, so a single
    animation grows both the card and the window.

    Args:
        prompt: What the user asked, shown dim above the body. Empty for a card
            that is only a message (a question, or an error with no request).
        body: Initial body text; empty means "still working".
        tone: ``context`` is something Yuki said first (a nudge being replied
            to); ``nudge`` is one of memory's nudges in the conversation, with
            a Reply of its own; ``error`` gets a red border.
        accent: Kept for callers; the look is monochrome.
        at: When the card's moment happened (epoch s; now by default): the
            stack is kept in this order.
        nudge: The nudge a ``nudge`` card shows.
        parent: Qt parent.

    Signals:
        reply_clicked: ``(nudge)`` -- Reply on a ``nudge`` card.
    """

    PENDING = "…"

    reply_clicked = Signal(object)
    #: The body was replaced (a reply arrived): the overlay brings it into view.
    content_changed = Signal()

    def __init__(
        self,
        prompt: str,
        body: str = "",
        *,
        tone: CardTone = "reply",
        accent: QColor | None = None,
        at: float | None = None,
        nudge: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tone: CardTone = tone
        self.accent = accent
        self.at: float = at if at is not None else time.time()
        self.nudge = nudge
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(4)

        self._prompt = QLabel(prompt, self)
        self._prompt.setFont(ui_font(10))
        self._prompt.setWordWrap(True)
        self._prompt.setStyleSheet(f"color: {TEXT_DIM.name()};")
        self._prompt.setVisible(bool(prompt))
        self.reply_button: QPushButton | None = None
        if tone == "nudge":
            from yuki.ui.surface import button_css

            self._prompt.setFont(ui_font(9))
            self._prompt.setTextFormat(Qt.TextFormat.PlainText)
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            header.addWidget(self._prompt, 1)
            self.reply_button = QPushButton("Reply", self)
            self.reply_button.setFont(ui_font(9, weight=QFont.Weight.Medium))
            self.reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A click must leave the keyboard in the input below.
            self.reply_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.reply_button.setStyleSheet(button_css("outline"))
            self.reply_button.clicked.connect(lambda: self.reply_clicked.emit(self.nudge))
            header.addWidget(self.reply_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(header)
        else:
            layout.addWidget(self._prompt)

        self._body = QLabel(body or self.PENDING, self)
        self._body.setFont(ui_font(12))
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Selecting text with the mouse must not pull the keyboard out of the input.
        self._body.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._body.setStyleSheet(f"color: {TEXT_PRIMARY.name()};")
        layout.addWidget(self._body)

        #: Dim time/steps/cost line under a finished reply; hidden until set.
        self._meta = QLabel("", self)
        self._meta.setFont(ui_font(9))
        self._meta.setTextFormat(Qt.TextFormat.PlainText)
        self._meta.setStyleSheet(f"color: {TEXT_DIM.name()};")
        self._meta.setVisible(False)
        layout.addWidget(self._meta)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(ANIM_MS)
        self._grow = QPropertyAnimation(self, b"maximumHeight", self)
        self._grow.setDuration(ANIM_MS)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setMaximumHeight(0)

    # -- content -----------------------------------------------------------

    def set_body(self, text: str, *, tone: CardTone | None = None) -> None:
        """Replace the body text and grow (or shrink) to fit.

        Args:
            text: New body text.
            tone: New colour treatment, if it changed.
        """
        if tone is not None:
            self.tone = tone
        self._body.setText(text)
        self.update()
        self.grow_to_fit()
        self.content_changed.emit()

    def set_meta(self, text: str) -> None:
        """Show a dim footer line (``52 s, 10 steps, 8¢``) and grow to fit.

        Args:
            text: The line; empty hides it.
        """
        self._meta.setText(text)
        self._meta.setVisible(bool(text))
        self.grow_to_fit()

    def set_reply_state(self, state: Literal["open", "replying", "replied"]) -> None:
        """What a ``nudge`` card's Reply says: ``Reply``, ``Replying…`` or ``Replied``."""
        if self.reply_button is None:
            return
        self.reply_button.setText({"open": "Reply", "replying": "Replying…", "replied": "Replied"}[state])
        self.reply_button.setEnabled(state == "open")

    def wanted_height(self) -> int:
        """Height the card needs for its current text at its current width."""
        width = self.width() or (PANEL_WIDTH - 2 * PAD)
        return self.layout().minimumHeightForWidth(width)

    def resizeEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        super().resizeEvent(event)  # type: ignore[arg-type]
        # A new width (the scrollbar came or went) rewraps the text: follow it, once the
        # grow animation is not the one setting the height.
        if self._grow.state() != QPropertyAnimation.State.Running and self.maximumHeight() > 0:
            target = self.wanted_height()
            if target != self.maximumHeight():
                self.setMaximumHeight(target)

    # -- animation ---------------------------------------------------------

    def reveal(self) -> None:
        """Fade and grow the card in from nothing."""
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.grow_to_fit()

    def grow_to_fit(self) -> None:
        """Animate the height to whatever the current text needs."""
        target = self.wanted_height()
        current = self.maximumHeight()
        self._grow.stop()
        self._grow.setStartValue(current if current < 16777215 else target)
        self._grow.setEndValue(target)
        self._grow.start()

    def dismiss(self) -> None:
        """Fade and shrink away, then delete the card."""
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        self._grow.stop()
        self._grow.setStartValue(self.maximumHeight())
        self._grow.setEndValue(0)
        self._grow.finished.connect(self.deleteLater)
        self._grow.start()

    @property
    def height_animation(self) -> QPropertyAnimation:
        """The height animation, so the window can follow it frame by frame."""
        return self._grow

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        error = self.tone == "error"
        painter.setBrush(ERROR_BG if error else CARD_BG)
        painter.setPen(QPen(ERROR_BORDER if error else CARD_BORDER, 1.0))
        painter.drawRoundedRect(rect, 8, 8)


class ResChip(QWidget):
    """A resolved chip (the design's ``.res``): ``who Sam`` / ``where WhatsApp``: a 30 px pill, a mono key in text-3,
    the value in 600. (No swap key: the agent acts at once, there is no plan to change yet.)"""

    def __init__(self, key: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from yuki.ui import lattice as L

        self.key, self.value = key, value
        fk, fv = L.mono_font(10.5), L.ui_font(13.5, 600)
        from PySide6.QtGui import QFontMetricsF

        w = 11 + QFontMetricsF(fk).horizontalAdvance(key) + 6 + QFontMetricsF(fv).horizontalAdvance(value) + 8 + 2
        self.setFixedSize(int(round(w)) + 1, 30)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtGui import QFontMetricsF, QPainterPath

        from yuki.ui import lattice as L

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, L.RAISE)
        p.setPen(QPen(L.HAIR2, 1.0))
        p.drawPath(path)
        fk, fv = L.mono_font(10.5), L.ui_font(13.5, 600)
        x = 12.0
        p.setFont(fk)
        p.setPen(L.TEXT3)
        kw = QFontMetricsF(fk).horizontalAdvance(self.key)
        p.drawText(QRectF(x, 0, kw + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.key)
        x += kw + 6
        p.setFont(fv)
        p.setPen(L.TEXT)
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignmentFlag.AlignVCenter, self.value)
        p.end()


class IntentCard(QWidget):
    """The Intent card inside the ask bar (the design's ``.intent``: a clearing framed by four crop marks).

    * **Unsent** (an offer put words in the ask bar): "In the ask bar · not sent", that nothing is sent until Enter,
      and Send / Clear, with where the words came from;
    * **Resolved** (a typed request whose task frame named a person): "Resolved from memory", the request, its
      chips (who, where) and **because** (where the frame found them, and how long ago).
    """

    send = Signal()
    clear = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from yuki.ui import lattice as L

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 16)
        col.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = L.Icon("key", 10, L.TEXT3, self)
        head.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self, max_lines=1)
        head.addWidget(self.label, 1)
        col.addLayout(head)
        self.title = L.Para("", font=L.ui_font(19, 600), line_height=1.45, parent=self)
        self.title_gap = QWidget(self)
        self.title_gap.setFixedHeight(10)
        col.addWidget(self.title_gap)
        col.addWidget(self.title)
        self.text = L.Para("", font=L.ui_font(14), color=L.TEXT2, line_height=1.45, parent=self)
        self.text_gap = QWidget(self)
        self.text_gap.setFixedHeight(8)
        col.addWidget(self.text_gap)
        col.addWidget(self.text)
        grid = QHBoxLayout()
        grid.setContentsMargins(0, 16, 0, 0)
        grid.setSpacing(14)
        self.dt = L.Para("Resolved", font=L.mono_font(11), color=L.TEXT3, line_height=24 / 11, parent=self)
        self.dt.setFixedWidth(78)
        grid.addWidget(self.dt, 0, Qt.AlignmentFlag.AlignTop)
        dd = QVBoxLayout()
        dd.setSpacing(8)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        self.chips_row.addStretch(1)
        dd.addLayout(self.chips_row)
        self.because = L.Para("", font=L.ui_font(13), color=L.TEXT2, line_height=1.45, parent=self)
        dd.addWidget(self.because)
        grid.addLayout(dd, 1)
        self.grid_box = QWidget(self)
        self.grid_box.setLayout(grid)
        col.addWidget(self.grid_box)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 18, 0, 0)
        foot.setSpacing(8)
        self.k_send = L.Key("Send", variant="pri", icon="up", parent=self)
        self.k_clear = L.Key("Clear", variant="ghost", parent=self)
        self.k_send.clicked.connect(self.send.emit)
        self.k_clear.clicked.connect(self.clear.emit)
        foot.addWidget(self.k_send)
        foot.addWidget(self.k_clear)
        foot.addStretch(1)
        self.source = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self, max_lines=1)
        foot.addWidget(self.source, 0, Qt.AlignmentFlag.AlignVCenter)
        self.foot_box = QWidget(self)
        self.foot_box.setLayout(foot)
        col.addWidget(self.foot_box)
        self._chips: list[QWidget] = []
        self.mode = ""

    def show_unsent(self, source: str = "") -> None:
        self.mode = "unsent"
        self.icon.name = "key"
        self.label.set_text("In the ask bar · not sent")
        self.title.setVisible(False)
        self.title_gap.setVisible(False)
        self.text.set_text("I put it here for you. Nothing is sent until you press Enter, and you can change any "
                           "word first.")
        self.text.setVisible(True)
        self.text_gap.setVisible(True)
        self.grid_box.setVisible(False)
        self.foot_box.setVisible(True)
        self.source.set_text(source)
        self.source.setFixedWidth(int(self.source.natural_width()) + 2)
        self.show()

    def show_resolved(self, request: str, frame: dict[str, Any]) -> bool:
        """The frame's first person and their usual route as chips; ``False`` when the frame named nobody."""
        persons = [p for p in (frame or {}).get("persons") or [] if isinstance(p, dict) and p.get("name")]
        if not persons:
            return False
        person = persons[0]
        routes = [r for r in person.get("routes") or [] if isinstance(r, dict)]
        self.mode = "resolved"
        self.icon.name = "memory"
        self.label.set_text("Plan · resolved from memory")
        self.title.set_text(request)
        self.title.setVisible(bool(request))
        self.title_gap.setVisible(bool(request))
        self.text.setVisible(False)
        self.text_gap.setVisible(False)
        for c in self._chips:
            c.deleteLater()
        self._chips = [ResChip("who", str(person["name"]), self)]
        if routes and (routes[0].get("label") or routes[0].get("app")):
            self._chips.append(ResChip("where", str(routes[0].get("label") or routes[0].get("app")), self))
        for k, c in enumerate(self._chips):
            self.chips_row.insertWidget(k, c)
        mine = routes[0].get("mine") if routes else None
        because = []
        where = (frame or {}).get("where")
        if where:
            because.append(str(where))
        if isinstance(mine, (int, float)) and mine > 0:
            because.append(f"you sent {int(mine)} messages there")
        self.because.set_text(("because " + ", and ".join(because)) if because else "")
        if because:
            self.because.set_text("because " + ", and ".join(because),
                                  [__import__("yuki.ui.lattice", fromlist=["Span"]).Span(0, 7, color=None, weight=500)])
        self.because.setVisible(bool(because))
        self.grid_box.setVisible(True)
        self.foot_box.setVisible(False)
        self.show()
        return True

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: D102
        return self.layout().totalHeightForWidth(w)

    def paintEvent(self, _e: object) -> None:  # noqa: D102 - a clearing: four crop marks, nothing else
        from yuki.ui import lattice as L

        p = QPainter(self)
        L.paint_crop_marks(p, QRectF(self.rect()))
        p.end()


class Overlay(SurfaceWindow):
    """The ask bar in the Lattice design (phase E9): 780 px, 64 px from the top of the monitor in use, the field colour
    with a ``#2E3135`` border and 22 px corners. On top the input row (the face at 4 px, your words at 19 px, Speak and
    the shortcut), then the Intent card when there is one (words an offer put here unsent; a request's resolved chips),
    the thread (the live chat, in the design's turns), and a foot of key hints with the call's timer.

    Signals:
        submitted: Text the user entered. The controller decides whether that is a
            new request or the answer to a pending question -- the overlay does not
            interpret it.
        dismissed: The overlay was closed by Esc, by a click elsewhere, or by the
            hotkey.
        opened: The overlay came up (from hidden or fading out; not a refocus
            of an overlay already open).
        do_nudge: ``(nudge)`` -- Do it on a card with an offer in the thread.
        speak: Speak (a voice conversation).
    """

    SHADOW = 0
    submitted = Signal(str)
    dismissed = Signal()
    opened = Signal()
    do_nudge = Signal(object)
    speak = Signal()

    def __init__(self) -> None:
        super().__init__(activates=True, radius=22)
        self.setObjectName("YukiOverlay")
        from yuki.ui import lattice as L
        from yuki.ui.dots import FaceWidget

        #: Thread mode (attach_thread): the live chat thread, and the stand-ins handed out for cards.
        self.thread: Any = None
        self._thread_model: Any = None
        self._proxies: list[ThreadCard] = []
        #: The call's ``(timer, app)`` for the foot, or None (the tray gives it: the island's clock).
        self.call_line: Any = lambda: None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        # the input row (the design's .ab-in): 64 px, 18 px in, the face, the words, Speak, the shortcut
        self.top_row = QWidget(self)
        self.top_row.setMinimumHeight(64 - 1)
        row = QHBoxLayout(self.top_row)
        row.setContentsMargins(18, 0, 12, 0)
        row.setSpacing(14)
        self.face = FaceWidget(4, self.top_row, blink=True)
        self.face.set_state("listening")
        row.addWidget(self.face, 0, Qt.AlignmentFlag.AlignVCenter)
        self.input = AskInput(self.top_row)
        self.input.setFont(L.ui_font(19))
        self.input.setPlaceholderText("Ask Yuki")
        self.input.submitted.connect(self._on_submit)
        self.input.escaped.connect(self.dismiss)
        self.input.wants_height.connect(self._on_input_height)
        self.input.textChanged.connect(self._on_text)
        row.addWidget(self.input, 1, Qt.AlignmentFlag.AlignVCenter)
        self.speak_key = L.Key("Speak", size="sm", icon="mic", parent=self.top_row)
        self.speak_key.clicked.connect(self.speak.emit)
        row.addWidget(self.speak_key, 0, Qt.AlignmentFlag.AlignVCenter)
        #: The shortcut that opens the ask bar, as a key hint ("Alt Shift").
        self.hint = L.Kbd("Alt Shift", self.top_row)
        row.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.top_row)
        # the Intent card (the design's .intent: 16 px under the input, 18 px in)
        self.intent_box = QWidget(self)
        ib = QVBoxLayout(self.intent_box)
        ib.setContentsMargins(18, 16, 18, 0)
        self.intent = IntentCard(self.intent_box)
        self.intent.send.connect(lambda: self._on_submit(self.input.toPlainText().strip())
                                 if self.input.toPlainText().strip() else None)
        self.intent.clear.connect(self._clear_unsent)
        ib.addWidget(self.intent)
        self.intent_box.setVisible(False)
        outer.addWidget(self.intent_box)
        # the thread (the design's .ab-thread: a label, then the turns)
        self.thread_head = L.Para("Earlier in this thread · today", font=L.mono_font(11), color=L.TEXT3,
                                  line_height=1.2, parent=self, max_lines=1)
        self.thread_head.setContentsMargins(0, 0, 0, 0)
        head_box = QWidget(self)
        hb = QVBoxLayout(head_box)
        hb.setContentsMargins(18, 18, 18, 4)
        hb.addWidget(self.thread_head)
        self.head_box = head_box
        head_box.setVisible(False)
        outer.addWidget(head_box)
        self.thread_box = QWidget(self)
        self.thread_lay = QVBoxLayout(self.thread_box)
        self.thread_lay.setContentsMargins(18, 0, 18, 10)
        self.thread_lay.setSpacing(0)
        outer.addWidget(self.thread_box)
        # the card stack (the older non-thread mode) scrolls in the same place
        self._content = QWidget()
        self._content.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(0, 0, 0, 0)
        self._cards.setSpacing(8)
        self._scroll = QScrollArea(self)
        self._scroll.setWidget(self._content)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setStyleSheet(SCROLL_STYLE)
        from yuki.ui.window.widgets import smooth

        smooth(self._scroll)
        self._scroll.setFixedHeight(0)
        self._scroll.setVisible(False)
        self.thread_lay.addWidget(self._scroll)
        self._follow = True
        self._forwarding_wheel = False
        bar = self._scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_scroll_range)
        bar.valueChanged.connect(self._on_scroll_value)
        # the foot (the design's .ab-foot): key hints, the call's timer on the right
        self.foot = QWidget(self)
        self.foot.setFixedHeight(10 + 13 + 10)
        fr = QHBoxLayout(self.foot)
        fr.setContentsMargins(18, 0, 18, 0)
        fr.setSpacing(18)
        self.foot_keys: list[tuple[Any, Any]] = []
        for key, words in (("Enter", "go"), ("Tab", "swap a chip"), ("Esc", "close")):
            pair = QHBoxLayout()
            pair.setSpacing(6)
            k = L.Kbd(key, self.foot)
            w = L.Para(words, font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self.foot, max_lines=1)
            w.setFixedSize(int(w.natural_width()) + 2, 13)
            pair.addWidget(k, 0, Qt.AlignmentFlag.AlignVCenter)
            pair.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
            fr.addLayout(pair)
            self.foot_keys.append((k, w))
        fr.addStretch(1)
        self.call_icon = L.Icon("rec", 11, L.TEXT3, self.foot)
        fr.addWidget(self.call_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.call_text = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self.foot,
                                max_lines=1)
        fr.addWidget(self.call_text, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.foot)
        self._call_tick = QTimer(self)
        self._call_tick.setInterval(1000)
        self._call_tick.timeout.connect(self._update_call)
        self._unsent_source = ""

        self._input_effect = None
        self.input.setMaximumHeight(self.input.preferred_height())
        self.resize(PANEL_WIDTH + 2 * self.SHADOW, self.layout().sizeHint().height())

        #: True for :data:`FOCUS_GRACE_MS` after :meth:`open`: a deactivation then
        #: is the focus hand-over settling, not the user leaving.
        self._focus_grace = False
        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.setInterval(FOCUS_GRACE_MS)
        self._grace_timer.timeout.connect(self._end_focus_grace)
        self.restyle()

    # -- look (the Lattice ask bar) -------------------------------------------------------------------------

    def restyle(self) -> None:  # noqa: D102 - SurfaceWindow hook (theme changed)
        if not hasattr(self, "input"):
            return
        from yuki.ui import lattice as L
        from yuki.ui import ui_settings
        from yuki.ui.window.widgets import combo_words

        combo = combo_words(ui_settings.current().open_hotkey or "alt+shift")
        self.hint.text = combo.replace(" + ", " ")
        self.hint.setFixedSize(L.kbd_size(self.hint.text))
        self.hint.update()
        live = L.live()
        self.input.setStyleSheet(
            "QPlainTextEdit { background: transparent;"
            f" color: {L.css(L.TEXT)};"
            f" selection-background-color: {L.css(live)}; selection-color: {L.css(L.live_ink())};"
            " border: none; padding: 0px; }")
        pal = self.input.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, L.TEXT3)
        self.input.setPalette(pal)
        if self.thread is not None:
            self.thread.restyle()

    def paintEvent(self, event: object) -> None:  # noqa: D102 - Qt override
        del event
        from PySide6.QtGui import QPainterPath

        from yuki.ui import lattice as L

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 21.5, 21.5)
        painter.fillPath(path, L.BG)
        painter.setPen(QPen(L.ASKBAR_BORDER, 1.0))
        painter.drawPath(path)
        y = self.top_row.geometry().bottom() + 1
        painter.fillRect(QRectF(1, y, self.width() - 2, 1), L.HAIR)    # under the input row
        fy = self.foot.geometry().top()
        painter.fillRect(QRectF(1, fy, self.width() - 2, 1), L.HAIR)   # over the foot
        painter.end()

    def _on_text(self) -> None:
        """Words an offer put here stay "not sent" until Enter; the face listens while the field is empty."""
        empty = not self.input.toPlainText().strip()
        # the design's face listens in the ask bar, and rests while an offer's words wait unsent
        self.face.set_state("idle" if (self.intent.mode == "unsent" and not empty) else "listening")
        if empty and self.intent.mode == "unsent":
            self.intent.hide()
            self.intent.mode = ""
            self.intent_box.setVisible(False)
            self.fit()

    def show_unsent(self, source: str = "") -> None:
        """An offer's words are in the input, unsent: the Intent card says so (Send / Clear)."""
        self.intent.show_unsent(source)
        self.face.set_state("idle")
        self.intent_box.setVisible(True)
        self.foot_keys[0][1].set_text("send")
        self.fit()

    def show_resolved(self, request: str, frame: dict[str, Any]) -> None:
        """A request's task frame named a person: its chips (who, where, because) in the Intent card."""
        if self.intent.show_resolved(request, frame):
            self.intent_box.setVisible(True)
            self.fit()

    def clear_intent(self) -> None:
        self.intent.mode = ""
        self.intent.hide()
        self.intent_box.setVisible(False)
        self.foot_keys[0][1].set_text("go")
        self.fit()

    def _clear_unsent(self) -> None:
        self.input.clear()
        self.clear_intent()
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _update_call(self) -> None:
        line = None
        try:
            line = self.call_line()
        except Exception:
            line = None
        on = bool(line)
        self.call_icon.setVisible(on)
        self.call_text.setVisible(on)
        if on:
            timer, app = line
            self.call_text.set_text(f"{timer} on {app}" if app else str(timer))
            self.call_text.setFixedSize(int(self.call_text.natural_width()) + 4, 13)
            self.foot.layout().activate()

    def showEvent(self, e: object) -> None:  # noqa: D102 - the call's clock runs only while the ask bar is up
        super().showEvent(e)
        self._update_call()
        self._call_tick.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        self._call_tick.stop()

    # -- thread mode (the live chat thread) ------------------------------------

    def attach_thread(self, model: Any) -> None:
        """Show the live chat thread instead of the card stack (see the module docstring)."""
        if self.thread is not None:
            return
        from yuki.ui.chatview import ChatThreadView

        self._thread_model = model
        self.thread = ChatThreadView(model, self.theme, max_bubble=PANEL_WIDTH - 2 * PAD - 92,
                                     margins=(0, 0, 2, 0), look="lattice", parent=self.thread_box)
        self.thread.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.thread.content_resized.connect(self.fit)
        self.thread.reply_nudge.connect(self._on_thread_reply)
        self.thread.do_nudge.connect(self.do_nudge.emit)
        self._scroll.setVisible(False)
        self.thread_lay.insertWidget(0, self.thread)
        self.fit()

    def _on_thread_reply(self, nudge: object) -> None:
        card = self.nudge_card(nudge.get("id")) if isinstance(nudge, dict) else None
        if card is None and isinstance(nudge, dict):
            card = self._proxy("nudge", nudge=nudge)
        if card is not None:
            card.reply_clicked.emit(card.nudge)

    def _proxy(self, tone: str, *, nudge: dict[str, Any] | None = None, at: float | None = None) -> "ThreadCard":
        model = self._thread_model

        def reply_state(state: str) -> None:
            if nudge is not None and model is not None:
                model.apply({"type": "nudge_state", "id": nudge.get("id"), "state": state})

        card = ThreadCard(tone, at=at, nudge=nudge, on_reply_state=reply_state)
        self._proxies.append(card)
        del self._proxies[:-100]
        return card

    # -- geometry ----------------------------------------------------------

    def anchor(self) -> QPoint:
        """Top-left to sit at: centred, 64 px from the top of the monitor in use (the design's ``.askbar``)."""
        from yuki.ui.island import active_screen

        screen = active_screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        width = PANEL_WIDTH + 2 * self.SHADOW
        x = area.left() + (area.width() - width) // 2
        y = area.top() + TOP_PX - self.SHADOW
        return QPoint(x, y)

    def max_cards_height(self) -> int:
        """Tallest the thread may be on the overlay's screen (:data:`MAX_CARDS_FRACTION`)."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        return max(120, int(area.height() * MAX_CARDS_FRACTION))

    def fit(self) -> None:
        """Size the thread to its content (capped), then the window to the layout. Keeps the top-left fixed."""
        if self.thread is not None:
            content = self.thread.content_height() if self._thread_model.items else 0
            stack = min(content, self.max_cards_height())
            if self.thread.height() != stack or self.thread.isVisible() != (stack > 0):
                self.thread.setFixedHeight(stack)
                self.thread.setVisible(stack > 0)
        else:
            content = self._cards.sizeHint().height() if self.cards() else 0
            stack = min(content, self.max_cards_height())
            if self._scroll.height() != stack or self._scroll.isVisible() != (stack > 0):
                self._scroll.setFixedHeight(stack)
                self._scroll.setVisible(stack > 0)
        self.head_box.setVisible(stack > 0)
        self.thread_box.setVisible(stack > 0)
        width = PANEL_WIDTH + 2 * self.SHADOW
        self.layout().activate()
        height = self.layout().totalHeightForWidth(width) if self.layout().hasHeightForWidth() else \
            self.layout().sizeHint().height()
        if (self.width(), self.height()) != (width, height):
            self.resize(width, height)
        self.update()

    # -- scrolling ---------------------------------------------------------

    def scroll_to_latest(self) -> None:
        """Follow the newest card again and bring the bottom of the stack into view."""
        if self.thread is not None:
            self.thread.scroll_to_latest()
        self._follow = True
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scroll_range(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self._scroll.verticalScrollBar().setValue(maximum)

    def _on_scroll_value(self, value: int) -> None:
        self._follow = value >= self._scroll.verticalScrollBar().maximum() - 2

    def scroll_range(self) -> tuple[int, int]:
        """``(value, maximum)`` of the stack's scrollbar (0, 0 when it fits)."""
        bar = self._scroll.verticalScrollBar()
        return bar.value(), bar.maximum()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: D102 - Qt override
        area = self.thread.area if self.thread is not None else self._scroll
        shown = self.thread.isVisible() if self.thread is not None else self._scroll.isVisible()
        if shown and not self._forwarding_wheel:
            self._forwarding_wheel = True
            try:
                from yuki.ui.window.widgets import smooth

                smooth(area).wheel(event)
            finally:
                self._forwarding_wheel = False
            event.accept()
            return
        super().wheelEvent(event)

    def _on_input_height(self, height: int) -> None:
        """Follow the input's own growth as the user types onto a new line."""
        if self.input.maximumHeight() > 0:
            self.input.setMaximumHeight(height)
            self.top_row.setMinimumHeight(max(63, height + 22))
            self.fit()

    # -- show / hide -------------------------------------------------------

    def focus_target(self) -> QWidget:  # noqa: D102 - SurfaceWindow hook
        return self.input

    def open(self) -> None:
        """Show the overlay (or just refocus it) with the input ready to type in (the grace period is armed before
        anything is shown, so the hand-over's activation messages never read as "clicked away")."""
        coming_up = not self.isVisible() or self.closing
        self._focus_grace = True
        self._grace_timer.start()
        self.expand_input()
        self.fit()
        self.fade_in(self.anchor())
        if coming_up:
            self.opened.emit()

    def dismiss(self) -> None:
        """Hide the overlay and tell the controller it is gone."""
        if not self.isVisible() or self.closing:
            return
        self.fade_out()
        self.dismissed.emit()

    def toggle(self) -> None:
        """Open if hidden, dismiss if shown."""
        if self.isVisible() and not self.closing:
            self.dismiss()
        else:
            self.open()

    def changeEvent(self, event: QEvent) -> None:  # noqa: D102 - Qt override
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and self.isVisible()
            and not self.isActiveWindow()
            and not self._focus_grace
        ):
            self.dismiss()

    def _end_focus_grace(self) -> None:
        """Close the grace window; if the hand-over did not stick, try once more (without the injected key-up)."""
        self._focus_grace = False
        if self.isVisible() and not self.closing and not self.isActiveWindow():
            self.take_focus(reason="grace_end", allow_unlock=False)

    # -- cards -------------------------------------------------------------

    def cards(self) -> list[ReplyCard]:
        """The cards currently in the stack, oldest first."""
        if self.thread is not None:
            return list(self._proxies)  # type: ignore[arg-type]
        widgets = (
            self._cards.itemAt(index).widget() for index in range(self._cards.count())
        )
        return [widget for widget in widgets if isinstance(widget, ReplyCard)]

    def add_card(
        self,
        prompt: str,
        body: str = "",
        *,
        tone: CardTone = "reply",
        accent: QColor | None = None,
    ) -> ReplyCard:
        """Add a card at the bottom of the stack (thread mode: a stand-in; the thread shows the exchange)."""
        if self.thread is not None:   # the thread shows the exchange from the chat model
            self.scroll_to_latest()
            return self._proxy(tone)  # type: ignore[return-value]
        card = ReplyCard(prompt, body, tone=tone, accent=accent, parent=self._content)
        self._cards.addWidget(card)
        self._watch(card)
        card.show()
        card.reveal()
        self._trim()
        self.scroll_to_latest()
        return card

    def _watch(self, card: ReplyCard) -> None:
        """Resize with the card's growth, and bring a reply into view when it lands."""
        card.height_animation.valueChanged.connect(lambda *_: self.fit())
        card.content_changed.connect(self.scroll_to_latest)

    def add_nudge_card(
        self, nudge: dict[str, Any], label: str, *, accent: QColor | None = None, at: float
    ) -> ReplyCard | None:
        """Put one of memory's nudges in the stack, in time order (``None`` when the stack is full of newer cards)."""
        existing = self.nudge_card(nudge.get("id"))
        if existing is not None:
            return existing
        if self.thread is not None:
            self._thread_model.apply({"type": "nudge", "nudge": nudge, "at": at})
            self.scroll_to_latest()
            return self._proxy("nudge", nudge=nudge, at=at)  # type: ignore[return-value]
        cards = self.cards()
        index = next((i for i, c in enumerate(cards) if c.at > at), len(cards))
        if index == 0 and len(cards) >= MAX_CARDS:
            return None
        card = ReplyCard(
            label, " ".join(str(nudge.get("text") or "").split()), tone="nudge", accent=accent,
            at=at, nudge=nudge, parent=self._content,
        )
        self._cards.insertWidget(index, card)
        self._watch(card)
        card.show()
        card.reveal()
        self._trim()
        return card

    def nudge_cards(self) -> list[ReplyCard]:
        """The nudge cards in the stack, oldest first."""
        return [card for card in self.cards() if card.tone == "nudge" and card.nudge is not None]

    def nudge_card(self, nudge_id: Any) -> ReplyCard | None:
        """The card showing nudge ``nudge_id``, if it is in the stack."""
        if nudge_id is None:
            return None
        return next(
            (c for c in self.nudge_cards() if str(c.nudge.get("id")) == str(nudge_id)), None
        )

    def _trim(self) -> None:
        """Fade out the oldest cards until at most :data:`MAX_CARDS` remain."""
        cards = self.cards()
        for card in cards[: max(0, len(cards) - MAX_CARDS)]:
            self._cards.removeWidget(card)
            card.setParent(self._content)
            card.dismiss()

    def clear_cards(self) -> None:
        """Remove every card immediately."""
        if self.thread is not None:
            self._proxies.clear()
            return
        for card in self.cards():
            self._cards.removeWidget(card)
            card.deleteLater()
        self.fit()

    # -- input -------------------------------------------------------------

    def collapse_input(self) -> None:
        """After a submit: the field empties at once (the words are in the thread now); it stays in place."""
        self.input.clear()

    def expand_input(self) -> None:
        """A cleared, focused input (an offer's words put back with :meth:`show_unsent` after this)."""
        self.input.clear()
        self.clear_intent()
        self.input.setMaximumHeight(self.input.preferred_height())
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_submit(self, text: str) -> None:
        """Hand the text to the controller; the field empties."""
        self.collapse_input()
        self.clear_intent()
        self.submitted.emit(text)


class ThreadCard(QObject):
    """What the card calls return in thread mode: the exchange itself is drawn from the chat model.

    It keeps the card interface the controller uses (``set_body``, ``set_meta``,
    ``set_reply_state``, ``reply_clicked``, ``nudge``, ``tone``, ``at``).
    """

    reply_clicked = Signal(object)
    content_changed = Signal()

    def __init__(self, tone: str, *, at: float | None = None, nudge: dict[str, Any] | None = None,
                 on_reply_state: Any = None) -> None:
        super().__init__()
        self.tone = tone
        self.at = at if at is not None else time.time()
        self.nudge = nudge
        self._on_reply_state = on_reply_state

    def set_body(self, text: str, *, tone: str | None = None) -> None:
        if tone is not None:
            self.tone = tone

    def set_meta(self, text: str) -> None:
        del text

    def set_reply_state(self, state: str) -> None:
        if self._on_reply_state is not None:
            self._on_reply_state(state)

    def dismiss(self) -> None:
        pass
