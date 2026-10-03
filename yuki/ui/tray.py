"""The tray in the Lattice design (phase E10): an icon per state, the words for its tooltip, and the hover card.

The icon is the design's tray mark (the 5 x 5 ``memory`` dot icon in bone on the graphite field) with one badge that
says the state at a glance, in the one live colour (or bone):

=============  ==========================================================
armed          the mark alone: Yuki listens for its name
listening      a live ring: your turn, Yuki hears you (or speaks)
working        three live dots: a request runs
recording      a filled live dot: your side of a call is being kept
waiting        a bone dot: something waits for you (a question, cards)
off            the mark dimmed and struck through: Yuki isn't listening
=============  ==========================================================

While a full screen sends the island away (a game), the tray carries the call: its tooltip says the timer and a
:class:`~yuki.ui.lattice_cards.TrayHoverCard` shows it when the pointer rests on the icon (the island's one clock;
it never resets). The pointer is looked at only during that time (4 times a second); otherwise nothing runs.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap

from yuki.ui import lattice as L

TRAY_STATES: tuple[str, ...] = ("armed", "listening", "working", "recording", "waiting", "off")
#: What the tray says for each state (the tooltip's first line).
TRAY_WORDS: dict[str, str] = {
    "armed": "Yuki: listening for its name", "listening": "Yuki: listening", "working": "Yuki: working",
    "recording": "Yuki: keeping your side of the call", "waiting": "Yuki: something is waiting for you",
    "off": "Yuki isn't listening",
}
#: How often the pointer is looked at while the island is away for a full screen (the hover card).
HOVER_POLL_MS = 250


def tray_state(island_state: str, *, listening: str = "on", recording: bool = False, waiting: int = 0) -> str:
    """The tray's state from the island's (:func:`yuki.ui.island.resolve`) and what it does not show: a request
    running wins, then your turn, then a question for you, then a call being kept, then cards waiting, then "Yuki
    isn't listening"."""
    if island_state in ("working", "thinking"):
        return "working"
    if island_state in ("listening", "hearing", "speaking"):
        return "listening"
    if island_state == "needs-you":
        return "waiting"
    if recording:
        return "recording"
    if waiting > 0:
        return "waiting"
    if listening != "on":
        return "off"
    return "armed"


def tray_icon(listening: bool = True, state: str | None = None) -> QIcon:
    """The tray mark (64 px, scaled by Windows): ``state`` one of :data:`TRAY_STATES`; ``listening`` False alone is
    the "off" mark (v3.16: "Yuki isn't listening" visible at a glance)."""
    state = state or ("armed" if listening else "off")
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(L.BG)
    p.setPen(QPen(L.BORDER, 2.0))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 15, 15)
    off = state == "off"
    mark = QColor(L.LIT)
    if off:
        mark = QColor(L.TEXT3)
    L.paint_icon(p, QRectF(10, 10, 44, 44), "memory", mark)
    live = L.live()
    badge = QPointF(48, 48)
    if state in ("listening", "working", "recording", "waiting"):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.BG)
        p.drawEllipse(badge, 15, 15)            # the badge's clearing over the mark
    if state == "listening":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(live, 5.0))
        p.drawEllipse(badge, 8.5, 8.5)
    elif state == "working":
        p.setBrush(live)
        for dx in (-9, 0, 9):
            p.drawEllipse(QPointF(badge.x() + dx, badge.y()), 3.6, 3.6)
    elif state == "recording":
        p.setBrush(live)
        p.drawEllipse(badge, 10, 10)
    elif state == "waiting":
        p.setBrush(L.LIT)
        p.drawEllipse(badge, 9, 9)
    elif off:
        pen = QPen(L.LIT, 5.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(14, 50), QPointF(50, 14))
    p.end()
    return QIcon(pm)


class TrayWatch(QObject):
    """Keeps the tray's icon and tooltip on the island's state, and shows the hover card while the island is away for
    a full screen. ``tray`` is the :class:`QSystemTrayIcon`; ``island`` the :class:`~yuki.ui.island.Island`;
    ``waiting`` returns how many cards wait; ``extra`` the tooltip's other lines (model, devices)."""

    def __init__(self, tray: Any, island: Any, *, waiting: Callable[[], int] = lambda: 0,
                 extra: Callable[[], str] = lambda: "") -> None:
        super().__init__()
        self.tray, self.island = tray, island
        self.waiting, self.extra = waiting, extra
        self.state = ""
        self._icons: dict[str, QIcon] = {}
        self.card = None
        self._hover_since = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(HOVER_POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        island.state_changed.connect(lambda _s: self.refresh())

    def icon(self, state: str) -> QIcon:
        if state not in self._icons:
            self._icons[state] = tray_icon(state != "off", state)
        return self._icons[state]

    def current(self) -> str:
        i = self.island.inputs
        recording = bool(i.call) and i.call.get("status") == "recording"
        return tray_state(self.island.state, listening=i.listening, recording=recording, waiting=int(self.waiting()))

    def words(self) -> str:
        """The tooltip: the state, the call's timer while one is kept, then the other lines."""
        i = self.island.inputs
        state = self.current()
        first = TRAY_WORDS.get(state, "Yuki")
        if state == "off":
            from yuki.ui.island import NOT_LISTENING_WORDS

            first = NOT_LISTENING_WORDS.get(i.listening, first)
        lines = [first]
        if i.run is not None and i.run.line:
            lines.append(i.run.line)
        if i.call and i.call.get("status") == "recording":
            lines.append(f"Call, your side · {i.call.get('app') or 'call'} · {self.island.call_time()}")
        if i.fullscreen:
            lines.append("Quiet while you play" + (f" {i.fullscreen_title}" if i.fullscreen_title else ""))
        extra = self.extra()
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def refresh(self) -> None:
        state = self.current()
        if state != self.state:
            self.state = state
            self.tray.setIcon(self.icon(state))
        self.tray.setToolTip(self.words())
        if self.island.inputs.fullscreen:
            if not self._poll.isActive():
                self._poll.start()
        else:
            self._poll.stop()
            self._hide_card()

    def _on_poll(self) -> None:
        """While the island is away: the pointer on the tray icon brings the hover card (after 400 ms), and the
        tooltip keeps the call's time."""
        import time

        if not self.island.inputs.fullscreen:
            self._poll.stop()
            self._hide_card()
            return
        try:
            rect = self.tray.geometry()
        except Exception:
            return
        over = rect.width() > 0 and rect.contains(QCursor.pos())
        card_over = self.card is not None and self.card.isVisible() and self.card.geometry().contains(QCursor.pos())
        now = time.monotonic()
        if over or card_over:
            if not self._hover_since:
                self._hover_since = now
            if now - self._hover_since >= 0.4:
                self._show_card(rect)
        else:
            self._hover_since = 0.0
            self._hide_card()
        if self.card is not None and self.card.isVisible():
            self.card.timer.set_text(self.island.call_time())

    def _show_card(self, rect: Any) -> None:
        from yuki.ui.lattice_cards import TrayHoverCard

        i = self.island.inputs
        if self.card is None:
            self.card = TrayHoverCard()
        recording = bool(i.call) and i.call.get("status") == "recording"
        what = f"Quiet while you play {i.fullscreen_title}" if i.fullscreen_title else "Quiet while you're in full screen"
        self.card.set_card(what=what, call=(f"{i.call.get('app') or 'Call'} call, your side" if recording else ""),
                           timer=self.island.call_time() if recording else "")
        if not self.card.isVisible():
            self.card.place_over(rect)

    def _hide_card(self) -> None:
        if self.card is not None and self.card.isVisible() and not self.card.closing:
            self.card.fade_out()


__all__ = ["TRAY_STATES", "TRAY_WORDS", "TrayWatch", "tray_icon", "tray_state"]
