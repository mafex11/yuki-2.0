"""Liquid Glass restyles of Yuki's existing surfaces: the Alt+Shift overlay, nudge and question
cards, the voice pill and the status strip.

Phase 1 draws them as mockups with the same content and the same buttons as
the live widgets (yuki/ui/overlay.py, nudges.py, status.py, yuki/voice/indicator.py),
so the look can be approved before those files are touched. Phase 2 moves
:class:`GlassSurface`'s painting into ``yuki/ui/glass.py``'s ``GlassWindow`` (one
``paintEvent``) and swaps the stylesheets for the :mod:`yuki.ui.window.widgets`
pills; behaviour (focus, fades, queueing) stays exactly as it is.

What changes, in one line each:
* the panel: near-black 85% glass -> Liquid Glass (DWM Acrylic behind the
  window where available, a tint, top sheen, specular edge, softer and deeper shadow),
  radius 16 -> 28 (cards 24, pill fully round);
* buttons: 8 px rounded rectangles -> pills; the main action tinted in the card's kind colour;
* nudge kind: a 3 px bar on the left -> a coloured dot and title (the tint carries it);
* light and dark follow Windows' app mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.data import epoch
from yuki.ui.window.glass import paint_glass, rounded
from yuki.ui.window.refract import collecting
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import Pill, TextPill, text_label

KIND_TITLES = {"praise": "Nice one", "nudge": "Check-in", "reminder": "Reminder", "review": "Your week",
               "resume": "Welcome back", "question": "Getting to know you"}


class GlassSurface(QWidget):
    """A floating glass surface with a margin for its shadow; paints the blurred backdrop inside its shape."""

    MARGIN = 40

    def __init__(self, t: Theme, *, radius: float = 28, level: str = "raised", tint: QColor | None = None) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.t, self.radius, self.level, self.tint = t, radius, level, tint
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._backdrop: QImage | None = None
        self._pos = QPointF(0, 0)
        self.body = QVBoxLayout(self)
        m = self.MARGIN
        self.body.setContentsMargins(m + SPACE["l"], m + SPACE["m"] + 2, m + SPACE["l"], m + SPACE["m"] + 2)
        self.body.setSpacing(SPACE["s"])

    def fit(self) -> None:
        """Height for the current width (wrapped labels), like the live cards do."""
        lay = self.layout()
        self.setFixedHeight(lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth()
                            else lay.sizeHint().height())

    def set_backdrop(self, image: QImage, pos: QPointF) -> None:
        self._backdrop, self._pos = image, pos

    def panel(self) -> QRectF:
        return QRectF(self.rect()).adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.panel()
        if collecting():   # real glass: the GPU refracts what is behind; nothing to paint here
            paint_glass(p, r, self.radius, self.t.level(self.level), tint=self.tint)
            p.end()
            return
        from dataclasses import replace

        from yuki.ui.window.glass import paint_shadow

        lvl = self.t.level(self.level)
        # the shadow must fade out inside the widget's margin, or its edge shows as a band
        paint_shadow(p, r, self.radius, replace(lvl, shadow_blur=min(lvl.shadow_blur, self.MARGIN // 2),
                                                shadow_y=min(lvl.shadow_y, self.MARGIN // 4)))
        if self._backdrop is not None:   # what DWM's acrylic would show behind the surface
            p.save()
            p.setClipPath(rounded(r, self.radius))
            dpr = self._backdrop.devicePixelRatio()
            src = QRectF((self._pos.x() + r.x()) * dpr, (self._pos.y() + r.y()) * dpr, r.width() * dpr,
                         r.height() * dpr)
            p.drawImage(r, self._backdrop, src)
            p.restore()
        paint_glass(p, r, self.radius, lvl, tint=self.tint, shadow=False)
        p.end()


def _soft(c: QColor, a: float) -> QColor:
    s = QColor(c)
    s.setAlphaF(a)
    return s


class _Dot(QWidget):
    def __init__(self, c: QColor, d: int = 8) -> None:
        super().__init__()
        self.c = c
        self.setFixedSize(d + 2, d + 2)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.c)
        p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        p.end()


def _stamp(value: Any) -> str:
    at = epoch(value)
    return datetime.fromtimestamp(at).strftime("%H:%M") if at else ""


# -- the overlay ------------------------------------------------------------------------------------


class _Card(QWidget):
    """One exchange (or a nudge) inside the overlay: inset glass, the prompt dim above the reply."""

    def __init__(self, t: Theme, prompt: str, body: str, meta: str = "", *, accent: QColor | None = None,
                 reply: bool = False) -> None:
        super().__init__()
        self.t, self.accent = t, accent
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(16, 12, 16, 12)
        col.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        if accent is not None:
            head.addWidget(_Dot(accent))
            head.addWidget(text_label(t, prompt, "caption_strong", "text_2"))
        else:
            head.addWidget(text_label(t, prompt, "callout", "text_3", wrap=True), 1)
        head.addStretch(1)
        if reply:
            head.addWidget(Pill(t, "Reply", style="tinted", tint=accent, size="small"))
        col.addLayout(head)
        col.addWidget(text_label(t, body, "body", "text", wrap=True))
        if meta:
            col.addWidget(text_label(t, meta, "caption", "text_3"))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        tint = _soft(self.accent, 0.10 if not self.t.dark else 0.14) if self.accent is not None else None
        paint_glass(p, r, 20, self.t.level("inset"), tint=tint, shadow=False)
        p.end()


class OverlayMock(GlassSurface):
    WIDTH = 620

    def __init__(self, t: Theme, turns: list[dict[str, Any]], nudge: dict[str, Any] | None) -> None:
        super().__init__(t, radius=28)
        self.setFixedWidth(self.WIDTH + 2 * self.MARGIN)
        items: list[tuple[float, QWidget]] = []
        for tu in turns[-2:]:
            user = (tu["user"] or "").split("\n\n[")[0].strip()
            items.append((tu["at"], _Card(t, user, tu["reply"], f"{_stamp(tu['at'])} · 6 s · 2¢")))
        if nudge is not None:
            items.append((epoch(nudge.get("shown_at")) or 0.0,
                          _Card(t, f"Yuki · {_stamp(nudge.get('at'))} · {KIND_TITLES.get(nudge['kind'], 'Yuki')}",
                                nudge["text"], accent=t.kind(nudge["kind"]), reply=True)))
        items.sort(key=lambda x: x[0])
        for _, w in items:
            self.body.addWidget(w)
        self.body.addSpacing(2)
        field = TextPill(t, "Ask Yuki…", action_glyph="send", height=48)
        self.body.addWidget(field)
        hint = QHBoxLayout()
        hint.addWidget(text_label(t, "Enter to send · Shift+Enter new line · Esc to close", "caption", "text_3"))
        hint.addStretch(1)
        hint.addWidget(text_label(t, "Alt+Shift", "caption_strong", "text_3"))
        self.body.addLayout(hint)
        self.fit()


# -- nudge and question cards ----------------------------------------------------------------------


class NudgeCardMock(GlassSurface):
    WIDTH = 380

    def __init__(self, t: Theme, nudge: dict[str, Any]) -> None:
        kind = nudge.get("kind") or "nudge"
        super().__init__(t, radius=24, tint=_soft(t.kind(kind), 0.07 if not t.dark else 0.10))
        self.setFixedWidth(self.WIDTH + 2 * self.MARGIN)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(_Dot(t.kind(kind), 9))
        head.addWidget(text_label(t, KIND_TITLES.get(kind, "Yuki"), "caption_strong", kind))
        head.addWidget(text_label(t, _stamp(nudge.get("at")), "caption", "text_3"))
        head.addStretch(1)
        self.body.addLayout(head)
        self.body.addWidget(text_label(t, nudge["text"], "body", "text", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        if kind == "question":
            row.addWidget(Pill(t, "Don't ask", style="quiet", size="small"))
            row.addWidget(Pill(t, "Skip", style="quiet", size="small"))
            row.addWidget(Pill(t, "Answer", glyph="edit", style="tinted", tint=t.kind(kind), size="small"))
        else:
            row.addWidget(Pill(t, "Dismiss", style="quiet", size="small"))
            row.addWidget(Pill(t, "Reply", style="tinted", tint=t.kind(kind), size="small"))
        self.body.addSpacing(2)
        self.body.addLayout(row)
        self.fit()


# -- the voice pill and the status strip ---------------------------------------------------------------

#: state -> (label, kind colour used for the glow)
VOICE_STATES = {"listening": ("Listening", "reminder"), "hearing": ("Hearing you", "praise"),
                "thinking": ("Thinking", "nudge"), "speaking": ("Speaking", "review"),
                "working": ("Working on it", "nudge"), "error": ("Voice needs headphones for now", "error")}


class VoicePillMock(GlassSurface):
    """Bottom-centre pill: a glowing glass bead (colour = state) and the state in words; a level
    meter while hearing or speaking."""

    MARGIN = 30

    def __init__(self, t: Theme, state: str) -> None:
        super().__init__(t, radius=22)
        self.state = state
        label, kind = VOICE_STATES[state]
        self.color = t.kind(kind)
        self.body.setContentsMargins(self.MARGIN + 44, self.MARGIN, self.MARGIN + 18, self.MARGIN)
        row = QHBoxLayout()
        row.addWidget(text_label(t, label, "callout", "text"))
        row.addStretch(1)
        self.body.addLayout(row)
        w = max(220, 44 + 18 + text_label(t, label, "callout").sizeHint().width() + (54 if state in
                                                                                     ("hearing", "speaking") else 12))
        self.setFixedSize(w + 2 * self.MARGIN, 44 + 2 * self.MARGIN)

    def paintEvent(self, e: object) -> None:  # noqa: D102
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.panel()
        c = QPointF(r.left() + 24, r.center().y())
        for rad, a in ((13, 0.16), (9.5, 0.28)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_soft(self.color, a))
            p.drawEllipse(c, rad, rad)
        p.setBrush(self.color)
        p.drawEllipse(c, 6, 6)
        p.setBrush(QColor(255, 255, 255, 150))
        p.drawEllipse(QPointF(c.x() - 1.8, c.y() - 2), 2.2, 1.6)
        if self.state in ("hearing", "speaking"):
            levels = (0.35, 0.7, 1.0, 0.55, 0.8, 0.4, 0.6)
            x = r.right() - 60
            for lv in levels:
                h = 6 + 16 * lv
                p.setBrush(_soft(self.color, 0.85))
                p.drawRoundedRect(QRectF(x, r.center().y() - h / 2, 3.5, h), 1.75, 1.75)
                x += 6.5
        if self.state in ("thinking", "working"):
            p.setPen(QPen(_soft(self.color, 0.9), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(r.right() - 34, r.center().y() - 8, 16, 16), 90 * 16, -250 * 16)
        p.end()


class StatusStripMock(GlassSurface):
    """Bottom-right while Yuki works: a spinner and what it is doing, in one line; never focused."""

    def __init__(self, t: Theme, text: str, meta: str = "") -> None:
        super().__init__(t, radius=26)
        self.setFixedWidth(380 + 2 * self.MARGIN)
        self.body.setContentsMargins(self.MARGIN + 52, self.MARGIN + 10, self.MARGIN + 16, self.MARGIN + 10)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(text_label(t, text, "callout", "text"))
        if meta:
            col.addWidget(text_label(t, meta, "caption", "text_3"))
        self.body.addLayout(col)
        self.fit()

    def paintEvent(self, e: object) -> None:  # noqa: D102
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.panel()
        c = QRectF(r.left() + 18, r.center().y() - 10, 20, 20)
        p.setPen(QPen(self.t.grid if not self.t.dark else QColor(255, 255, 255, 30), 2.4))
        p.drawEllipse(c)
        p.setPen(QPen(self.t.accent, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(c, 90 * 16, -110 * 16)
        p.end()


# -- rendering -------------------------------------------------------------------------------------


def _pick(nudges: list[dict[str, Any]], kinds: tuple[str, ...]) -> dict[str, Any] | None:
    for n in reversed(nudges):
        if n.get("kind") in kinds:
            return n
    return None


def render_all(t: Theme, views: dict, compose: Callable[[list, Theme, QSize], QImage]) -> list[tuple[str, QImage]]:
    """(name, image) for every restyled surface, over the stand-in wallpaper."""
    nudges = views.get("nudges") or []
    chat = views["chat"]
    out: list[tuple[str, QImage]] = []

    ov = OverlayMock(t, chat.turns, _pick(nudges, ("reminder", "nudge", "praise")))
    size = QSize(ov.width() + 160, ov.height() + 120)
    out.append(("overlay", compose([(ov, QPointF(80, 60))], t, size)))

    nudge = _pick(nudges, ("nudge", "praise", "reminder"))
    if nudge:
        card = NudgeCardMock(t, nudge)
        out.append(("nudge_card", compose([(card, QPointF(40, 30))], t, QSize(card.width() + 80,
                                                                             card.height() + 60))))
    question = _pick(nudges, ("question",))
    if question:
        card = NudgeCardMock(t, question)
        out.append(("question_card", compose([(card, QPointF(40, 30))], t, QSize(card.width() + 80,
                                                                                card.height() + 60))))
    pills = [VoicePillMock(t, s) for s in VOICE_STATES]
    widest = max(pl.width() for pl in pills)
    y = 20.0
    placed = []
    for pl in pills:
        placed.append((pl, QPointF(40 + (widest - pl.width()) / 2, y)))
        y += pl.height() - 34
    out.append(("voice_pill", compose(placed, t, QSize(widest + 80, int(y) + 40))))
    strip = StatusStripMock(t, "Launching app — Spotify", "step 2 · 14 s")
    out.append(("status_strip", compose([(strip, QPointF(40, 30))], t, QSize(strip.width() + 80,
                                                                            strip.height() + 60))))
    return out

