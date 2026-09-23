"""The ask-Yuki overlay: one input, and a short stack of reply cards above it.

Lifecycle of a request, all of it animated and none of it timed with sleeps:
submit collapses the input, a card grows in where the input used to be, and a
fresh (empty) input expands below it. The card fills in when the answer arrives
and grows to fit. Three cards stay visible; older ones fade away.

The overlay never decides anything about the request -- it hands the text to
:mod:`yuki.ui.runtime` and renders what comes back.
"""

from __future__ import annotations

from typing import Literal

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
    QLabel,
    QPlainTextEdit,
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

#: How many reply cards stay on screen.
MAX_CARDS = 3

#: After the overlay is shown, how long losing activation does not count as the
#: user clicking away. Covers the transient activate/deactivate messages of the
#: foreground hand-over (:func:`yuki.ui.focus.force_foreground`). A UI timer, not
#: a wait: nothing blocks on it.
FOCUS_GRACE_MS = 200

CardTone = Literal["reply", "question", "error"]


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
        tone: Colour treatment.
        parent: Qt parent.
    """

    PENDING = "…"

    def __init__(
        self,
        prompt: str,
        body: str = "",
        *,
        tone: CardTone = "reply",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tone: CardTone = tone
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(4)

        self._prompt = QLabel(prompt, self)
        self._prompt.setFont(ui_font(10))
        self._prompt.setWordWrap(True)
        self._prompt.setStyleSheet(f"color: rgba(238,240,245,{TEXT_DIM.alpha()});")
        self._prompt.setVisible(bool(prompt))
        layout.addWidget(self._prompt)

        self._body = QLabel(body or self.PENDING, self)
        self._body.setFont(ui_font(12))
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setStyleSheet("color: rgba(238,240,245,255);")
        layout.addWidget(self._body)

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
        painter.setBrush(ERROR_BG if error else CARD_BG)
        painter.setPen(QPen(ERROR_BORDER if error else CARD_BORDER, 1.0))
        painter.drawRoundedRect(rect, 10, 10)
        if self.tone == "question":
            painter.setPen(QPen(QColor(126, 180, 255, 180), 2.0))
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
    """

    submitted = Signal(str)
    dismissed = Signal()

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
        self._focus_grace = True
        self._grace_timer.start()
        self.expand_input()
        self.fit()
        self.fade_in(self.anchor())

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

    def add_card(self, prompt: str, body: str = "", *, tone: CardTone = "reply") -> ReplyCard:
        """Add a card at the bottom of the stack and animate it in.

        Args:
            prompt: Dim prompt line, usually what the user asked.
            body: Body text; empty for a card that is still waiting.
            tone: Colour treatment.

        Returns:
            The new card, so the caller can fill it in when the answer arrives.
        """
        card = ReplyCard(prompt, body, tone=tone, parent=self)
        self._cards.addWidget(card)
        card.height_animation.valueChanged.connect(lambda *_: self.fit())
        card.show()
        card.reveal()
        self._trim()
        return card

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
