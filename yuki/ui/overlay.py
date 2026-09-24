"""The ask-Yuki overlay: one input, and a short stack of reply cards above it.

Lifecycle of a request, all of it animated and none of it timed with sleeps:
submit collapses the input, a card grows in where the input used to be, and a
fresh (empty) input expands below it. The card fills in when the answer arrives
and grows to fit. :data:`MAX_CARDS` cards stay visible; older ones fade away.

Memory's nudges live in the same stack (tone ``nudge``: tinted in their
kind's accent, labelled ``Yuki · 14:32 · nudge``, with their own Reply), each
placed by the time the user saw it, so the stack reads as one conversation.

The overlay never decides anything about the request -- it hands the text to
:mod:`yuki.ui.runtime` and renders what comes back.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from yuki.ui.glass import (
    ANIM_MS,
    CARD_BG,
    CARD_BORDER,
    ERROR_BG,
    ERROR_BORDER,
    TEXT_DIM,
    GlassWindow,
    ui_font,
)

#: Width of the panel (excluding the shadow margin), in logical pixels.
PANEL_WIDTH = 620

#: Padding inside the panel.
PAD = 14

#: How many cards (requests, replies and nudges together) stay on screen.
MAX_CARDS = 5

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
        self.setStyleSheet(
            "QPlainTextEdit {"
            "  background: transparent;"
            "  color: rgba(238,240,245,255);"
            "  selection-background-color: rgba(126,180,255,90);"
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
        tone: Colour treatment. ``context`` is something Yuki said first (a
            nudge being replied to), marked with a bar in ``accent``.
            ``nudge`` is one of memory's nudges in the conversation: tinted in
            ``accent``, its label in the accent, and a Reply of its own.
        accent: Colour of the ``context`` bar / the ``nudge`` tint.
        at: When the card's moment happened (epoch s; now by default): the
            stack is kept in this order.
        nudge: The nudge a ``nudge`` card shows.
        parent: Qt parent.

    Signals:
        reply_clicked: ``(nudge)`` -- Reply on a ``nudge`` card.
    """

    PENDING = "…"

    reply_clicked = Signal(object)

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
        self._prompt.setStyleSheet(f"color: rgba(238,240,245,{TEXT_DIM.alpha()});")
        self._prompt.setVisible(bool(prompt))
        self.reply_button: QPushButton | None = None
        if tone == "nudge":
            a = QColor(accent or QColor(126, 180, 255))
            self._prompt.setFont(ui_font(9))
            self._prompt.setTextFormat(Qt.TextFormat.PlainText)
            self._prompt.setStyleSheet(f"color: rgba({a.red()},{a.green()},{a.blue()},230);")
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            header.addWidget(self._prompt, 1)
            self.reply_button = QPushButton("Reply", self)
            self.reply_button.setFont(ui_font(9))
            self.reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A click must leave the keyboard in the input below.
            self.reply_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.reply_button.setStyleSheet(
                f"QPushButton {{ color: rgb({a.red()},{a.green()},{a.blue()});"
                f" background: rgba({a.red()},{a.green()},{a.blue()},26);"
                f" border: 1px solid rgba({a.red()},{a.green()},{a.blue()},80);"
                " border-radius: 7px; padding: 1px 10px; }"
                f"QPushButton:hover {{ background: rgba({a.red()},{a.green()},{a.blue()},60); }}"
                "QPushButton:disabled { color: rgba(238,240,245,120); background: transparent;"
                " border: 1px solid rgba(255,255,255,20); }"
            )
            self.reply_button.clicked.connect(lambda: self.reply_clicked.emit(self.nudge))
            header.addWidget(self.reply_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(header)
        else:
            layout.addWidget(self._prompt)

        self._body = QLabel(body or self.PENDING, self)
        self._body.setFont(ui_font(12))
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setStyleSheet("color: rgba(238,240,245,255);")
        layout.addWidget(self._body)

        #: Dim time/steps/cost line under a finished reply; hidden until set.
        self._meta = QLabel("", self)
        self._meta.setFont(ui_font(9))
        self._meta.setTextFormat(Qt.TextFormat.PlainText)
        self._meta.setStyleSheet(f"color: rgba(238,240,245,{TEXT_DIM.alpha()});")
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

    def set_meta(self, text: str) -> None:
        """Show a dim footer line (``52 s · 10 steps · 8¢``) and grow to fit.

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
        if self.tone == "nudge":
            tint = QColor(self.accent or QColor(126, 180, 255))
            border = QColor(tint)
            tint.setAlpha(22)
            border.setAlpha(70)
            painter.setBrush(tint)
            painter.setPen(QPen(border, 1.0))
        else:
            painter.setBrush(ERROR_BG if error else CARD_BG)
            painter.setPen(QPen(ERROR_BORDER if error else CARD_BORDER, 1.0))
        painter.drawRoundedRect(rect, 10, 10)
        if self.tone in ("question", "context", "nudge"):
            bar = QColor(self.accent or QColor(126, 180, 255)) if self.tone != "question" else (
                QColor(126, 180, 255)
            )
            bar.setAlpha(180 if self.tone == "question" else 200)
            painter.setPen(QPen(bar, 2.0))
            painter.drawLine(
                rect.left() + 1.0, rect.top() + 8.0, rect.left() + 1.0, rect.bottom() - 8.0
            )


class Overlay(GlassWindow):
    """The overlay window.

    Signals:
        submitted: Text the user entered. The controller decides whether that is a
            new request or the answer to a pending question -- the overlay does not
            interpret it.
        dismissed: The overlay was closed by Esc, by a click elsewhere, or by the
            hotkey.
        opened: The overlay came up (from hidden or fading out; not a refocus
            of an overlay already open).
    """

    submitted = Signal(str)
    dismissed = Signal()
    opened = Signal()

    def __init__(self) -> None:
        super().__init__(activates=True, radius=16)
        self.setObjectName("YukiOverlay")

        outer = QVBoxLayout(self)
        margin = self.SHADOW + PAD
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.setSpacing(10)

        self._cards = QVBoxLayout()
        self._cards.setContentsMargins(0, 0, 0, 0)
        self._cards.setSpacing(8)
        outer.addLayout(self._cards)

        self.input = AskInput(self)
        self.input.submitted.connect(self._on_submit)
        self.input.escaped.connect(self.dismiss)
        self.input.wants_height.connect(self._on_input_height)
        outer.addWidget(self.input)

        self._input_effect = QGraphicsOpacityEffect(self.input)
        self._input_effect.setOpacity(1.0)
        self.input.setGraphicsEffect(self._input_effect)
        self._input_fade = QPropertyAnimation(self._input_effect, b"opacity", self)
        self._input_fade.setDuration(ANIM_MS)
        self._input_collapse = QPropertyAnimation(self.input, b"maximumHeight", self)
        self._input_collapse.setDuration(ANIM_MS)
        self._input_collapse.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._input_collapse.valueChanged.connect(lambda *_: self.fit())

        self.input.setMaximumHeight(self.input.preferred_height())
        self.resize(PANEL_WIDTH + 2 * self.SHADOW, self.layout().sizeHint().height())

        #: True for :data:`FOCUS_GRACE_MS` after :meth:`open`: a deactivation then
        #: is the focus hand-over settling, not the user leaving.
        self._focus_grace = False
        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.setInterval(FOCUS_GRACE_MS)
        self._grace_timer.timeout.connect(self._end_focus_grace)

    # -- geometry ----------------------------------------------------------

    def anchor(self) -> QPoint:
        """Top-left to sit at: centred horizontally, upper third of the screen."""
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        width = PANEL_WIDTH + 2 * self.SHADOW
        x = area.left() + (area.width() - width) // 2
        y = area.top() + int(area.height() * 0.18) - self.SHADOW
        return QPoint(x, y)

    def fit(self) -> None:
        """Resize to the height the layout wants, keeping the top-left fixed."""
        width = PANEL_WIDTH + 2 * self.SHADOW
        height = self.layout().sizeHint().height()
        if (self.width(), self.height()) != (width, height):
            self.resize(width, height)

    def _on_input_height(self, height: int) -> None:
        """Follow the input's own growth as the user types onto a new line."""
        if self.input.maximumHeight() > 0 and self._input_collapse.state() != (
            QPropertyAnimation.State.Running
        ):
            self.input.setMaximumHeight(height)
            self.fit()

    # -- show / hide -------------------------------------------------------

    def focus_target(self) -> QWidget:  # noqa: D102 - GlassWindow hook
        return self.input

    def open(self) -> None:
        """Show the overlay (or just refocus it) with the input ready to type in.

        The grace period is armed before anything is shown, so the activation
        messages of the hand-over cannot reach :meth:`changeEvent` as a "user
        clicked away". :meth:`fade_in` then shows the window and takes the
        foreground at once (:meth:`GlassWindow.take_focus`), focusing the input
        before the first animation frame.
        """
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
        """Close the grace window; if the hand-over did not stick, try once more.

        Losing activation from here on is a real focus-out again. If the overlay
        is showing but not active right now, the activation lost a race (for
        example to the window behind it); take it back once, without the
        injected-key fallback, which :meth:`open` has already spent.
        """
        self._focus_grace = False
        if self.isVisible() and not self.closing and not self.isActiveWindow():
            self.take_focus(reason="grace_end", allow_unlock=False)

    # -- cards -------------------------------------------------------------

    def cards(self) -> list[ReplyCard]:
        """The cards currently in the stack, oldest first."""
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
        """Add a card at the bottom of the stack and animate it in.

        Args:
            prompt: Dim prompt line, usually what the user asked.
            body: Body text; empty for a card that is still waiting.
            tone: Colour treatment.
            accent: Bar colour for a ``context`` card.

        Returns:
            The new card, so the caller can fill it in when the answer arrives.
        """
        card = ReplyCard(prompt, body, tone=tone, accent=accent, parent=self)
        self._cards.addWidget(card)
        card.height_animation.valueChanged.connect(lambda *_: self.fit())
        card.show()
        card.reveal()
        self._trim()
        return card

    def add_nudge_card(
        self, nudge: dict[str, Any], label: str, *, accent: QColor, at: float
    ) -> ReplyCard | None:
        """Put one of memory's nudges in the stack, in time order, and animate it in.

        Args:
            nudge: The nudge (kept on the card as ``card.nudge``).
            label: The small label (``Yuki · 14:32 · nudge``).
            accent: Its kind's colour.
            at: When the user saw it (epoch s): where it goes in the stack.

        Returns:
            The card, or ``None`` when the stack is full of newer cards, so it
            would only be trimmed away again at once.
        """
        existing = self.nudge_card(nudge.get("id"))
        if existing is not None:
            return existing
        cards = self.cards()
        index = next((i for i, c in enumerate(cards) if c.at > at), len(cards))
        if index == 0 and len(cards) >= MAX_CARDS:
            return None
        card = ReplyCard(
            label, " ".join(str(nudge.get("text") or "").split()), tone="nudge", accent=accent,
            at=at, nudge=nudge, parent=self,
        )
        # Layout index of the card it goes before (cards are the only items).
        self._cards.insertWidget(index, card)
        card.height_animation.valueChanged.connect(lambda *_: self.fit())
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
            card.setParent(self)
            card.dismiss()

    def clear_cards(self) -> None:
        """Remove every card immediately."""
        for card in self.cards():
            self._cards.removeWidget(card)
            card.deleteLater()
        self.fit()

    # -- input -------------------------------------------------------------

    def collapse_input(self) -> None:
        """Fade and collapse the input away (after a submit)."""
        self._input_fade.stop()
        self._input_fade.setStartValue(self._input_effect.opacity())
        self._input_fade.setEndValue(0.0)
        self._input_fade.start()
        self._input_collapse.stop()
        self._input_collapse.setStartValue(self.input.maximumHeight())
        self._input_collapse.setEndValue(0)
        self._input_collapse.start()

    def expand_input(self) -> None:
        """Bring a cleared, focused input back below the cards."""
        self.input.clear()
        self._input_fade.stop()
        self._input_fade.setStartValue(self._input_effect.opacity())
        self._input_fade.setEndValue(1.0)
        self._input_fade.start()
        self._input_collapse.stop()
        self._input_collapse.setStartValue(self.input.maximumHeight())
        self._input_collapse.setEndValue(self.input.preferred_height())
        self._input_collapse.start()
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_submit(self, text: str) -> None:
        """Collapse the input and hand the text to the controller."""
        self.collapse_input()
        self.submitted.emit(text)
