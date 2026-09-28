"""Status: what Yuki is doing or waiting on, in a few plain words each, and nothing to set.

Four dot-matrix displays (things seen today, facts written, calls waiting, dollars today), then one row per
thing with at most the one action it needs (memory remembering your day, calls waiting to be remembered, a
call going on, questions waiting, the coach, listening for "Yuki"), and the Health module beside them (memory
service, last thing seen, portrait rebuilt, microphone, speaker, Start with Windows, last check-in), each with
an LED: the accent while live, secondary ink when fine, off when not.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import StatusView, day_label, dur, first_sentence, hm, plain, second_person
from yuki.ui.window.paint import paint_led, paint_surface
from yuki.ui.window.theme import ICON, PAGE_PAD, RADIUS, Theme, font
from yuki.ui.window.widgets import Button, Card, DotDisplay, Led, Masonry, page_header, text_label

#: A status row's height.
CARD_H = 68


class _IconKey(QWidget):
    """A row's icon on a small key face; ``live`` lights its LED."""

    def __init__(self, t: Theme, glyph: str, live: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.glyph, self.live = t, glyph, live
        self.setFixedSize(38, 38)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["key"], self.t.level("key"))
        if self.live:
            paint_led(p, QPointF(6.5, 6.5), self.t.accent, 5)
        f = font("icon")
        f.setPixelSize(15)
        p.setFont(f)
        p.setPen(self.t.text_2)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, ICON.get(self.glyph, self.glyph))
        p.end()


def _row(t: Theme, glyph: str, title: str, text: str, actions: list[QWidget], *, live: bool = False) -> Card:
    """One status row: an icon key, the state in a short title, one short line, its action."""
    card = Card(t, padding=0)
    card.setMinimumHeight(CARD_H)
    card.body.setContentsMargins(18, 14, 18, 14)
    row = QHBoxLayout()
    row.setSpacing(16)
    row.addWidget(_IconKey(t, glyph, live), 0, Qt.AlignmentFlag.AlignVCenter)
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addStretch(1)
    col.addWidget(text_label(t, title, "body_strong", wrap=True))
    if text:
        col.addWidget(text_label(t, text, "caption", "text_3", wrap=True))
    col.addStretch(1)
    row.addLayout(col, 1)
    for a in actions:
        row.addWidget(a, 0, Qt.AlignmentFlag.AlignVCenter)
    card.body.addLayout(row)
    return card


def _pill(t: Theme, text: str, on_click, **kw) -> Button:
    pill = Button(t, text, **kw)
    pill.clicked.connect(lambda _c=False: on_click())
    return pill


def _confirm_pill(t: Theme, text: str, sure: str, on_click, **kw) -> Button:
    """A pill that asks once ("Erase" -> "Sure? Tap again") and acts on the second click within 4 s."""
    pill = Button(t, text, **kw)
    armed = {"on": False}
    timer = QTimer(pill)
    timer.setSingleShot(True)

    def disarm() -> None:
        armed["on"] = False
        pill.setText(text)
        pill.updateGeometry()

    timer.timeout.connect(disarm)

    def click(_c: bool = False) -> None:
        if not armed["on"]:
            armed["on"] = True
            pill.setText(sure)
            pill.updateGeometry()
            timer.start(4000)
            return
        timer.stop()
        pill.setEnabled(False)
        on_click()

    pill.clicked.connect(click)
    return pill


def minutes_until(hour: int = 6) -> int:
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds() // 60))


def _when(at: float | None) -> str:
    if not at:
        return "not yet"
    label = day_label(at)
    return hm(at) if label == "today" else f"{label} {hm(at)}"


def _health(t: Theme, v: StatusView) -> Card:
    card = Card(t, padding=0)
    card.body.setContentsMargins(22, 20, 22, 16)
    card.body.setSpacing(0)
    card.body.addWidget(text_label(t, "Health", "headline"))
    card.body.addSpacing(10)
    running = v.service_running and not v.paused
    rows = [
        ("on" if running else "off", "Memory service",
         "running" if running else "paused" if v.paused else "not running"),
        ("ok" if v.last_capture_at else "off", "Last thing seen", _when(v.last_capture_at)),
        ("ok" if v.portrait_at else "off", "Portrait rebuilt", _when(v.portrait_at)),
        ("ok", "Microphone", v.mic),
        ("ok", "Speaker", v.speaker),
        ("ok" if v.start_with_windows else "off", "Start with Windows",
         "on" if v.start_with_windows else "off" if v.start_with_windows is False else "unknown"),
        ("ok" if v.last_nudge_at else "off", "Last check-in", _when(v.last_nudge_at)),
    ]
    for i, (state, name, value) in enumerate(rows):
        line = QWidget(card)
        line.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QHBoxLayout(line)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(10)
        lay.addWidget(Led(t, state, 7), 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(text_label(t, name, "body", "text_2"), 1)
        val = text_label(t, value, "micro", "text_3")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setToolTip(value)
        val.setMaximumWidth(210)
        lay.addWidget(val)
        if i:
            from yuki.ui.window.widgets import Divider

            card.body.addWidget(Divider(t))
        card.body.addWidget(line)
    return card


def build(t: Theme, v: StatusView, *, act: Actions = NOOP) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(0)
    waiting_calls = (v.calls_waiting or {}).get("calls", 0)
    waiting = waiting_calls + len(v.questions_waiting)
    if not v.service_running:
        lead = "Memory isn't running, so today isn't being remembered."
    elif waiting:
        lead = f"All running. {waiting} thing{'s' if waiting != 1 else ''} waiting, nothing urgent."
    else:
        lead = "All running. Nothing is waiting on you."
    col.addWidget(page_header(t, "Status", lead))
    col.addSpacing(24)

    displays = QHBoxLayout()
    displays.setSpacing(12)
    live = v.service_running and not v.paused
    displays.addWidget(DotDisplay(t, f"{v.captures_today:,}".replace(",", ""), "things seen today", px=46,
                                  ink="accent" if live else "text", height=104, top=14))
    displays.addWidget(DotDisplay(t, f"{v.facts_today}", "facts written", px=46,
                                  ink="text" if v.facts_today else "faint", height=104, top=14))
    displays.addWidget(DotDisplay(t, f"{waiting_calls}", "calls waiting", px=46,
                                  ink="text" if waiting_calls else "faint", height=104, top=14))
    displays.addWidget(DotDisplay(t, f"{v.cost_today:.2f}", "dollars today", px=46, height=104, top=14))
    col.addLayout(displays)
    col.addSpacing(14)

    rows = QWidget(page)
    rows.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    rcol = QVBoxLayout(rows)
    rcol.setContentsMargins(0, 0, 0, 0)
    rcol.setSpacing(10)
    if v.service_running and not v.paused:
        seen = f"{v.captures_today:,} things seen, {v.facts_today:,} facts written today"
        rcol.addWidget(_row(t, "history", "Remembering your day", seen,
                            [_pill(t, "Pause", lambda: act.set_paused(True), glyph="pause")], live=True))
    elif v.paused:
        rcol.addWidget(_row(t, "pause", "Memory is paused", "Nothing on your screen is remembered",
                            [_pill(t, "Resume", lambda: act.set_paused(False), glyph="play", style="default")]))
    else:
        rcol.addWidget(_row(t, "warning", "Memory isn't running", "Yuki restarts it within two minutes",
                            [_pill(t, "Start", act.start_memory, style="default")]))
    if waiting_calls:
        w = v.calls_waiting
        rcol.addWidget(_row(t, "mic", f"{waiting_calls} call{'s' if waiting_calls != 1 else ''} waiting to "
                                               "be remembered",
                            f"{dur(w['minutes'] * 60)} of your voice, kept on this PC until the GPU is free",
                            [_confirm_pill(t, "Erase", "Sure? Tap again", act.erase_calls, glyph="delete",
                                           style="destructive")]))
    else:
        rcol.addWidget(_row(t, "mic", "No calls waiting to be remembered",
                            "A recorded call waits here, on this PC, until the GPU is free", []))
    if v.call_now:
        recording = v.call_now.get("status") == "recording"
        rcol.addWidget(_row(t, "phone", f"On a {v.call_now['app']} call",
                            "Only your side is remembered" if recording else "This call isn't remembered",
                            [_pill(t, "Stop", act.stop_call, glyph="close")] if recording else [], live=recording))
    if v.questions_waiting:
        q = v.questions_waiting[0]
        n = len(v.questions_waiting)
        rcol.addWidget(_row(t, "question", "A question for you" if n == 1 else f"{n} questions for you",
                            first_sentence(plain(second_person(q["question"])), 90),
                            [_pill(t, "Answer", lambda: act.navigate("yuki"), glyph="edit", style="default")]))
    if v.quiet_until:
        rcol.addWidget(_row(t, "moon", f"Coach is quiet until {hm(v.quiet_until)}", "Reminders wait, none are lost",
                            [_pill(t, "Turn back on", lambda: act.snooze(0), style="default")]))
    else:
        start, end = v.quiet_hours
        quiet = f"Quiet from {start} to {end}" if start != end else "No quiet hours"
        rcol.addWidget(_row(t, "heart", "Coach is on", quiet,
                            [_pill(t, "Quiet 1 hour", lambda: act.snooze(60), glyph="moon"),
                             _pill(t, "Until tomorrow", lambda: act.snooze(minutes_until(6)))]))
    rcol.addWidget(_row(t, "wave", "Say “Yuki” to start" if v.wake_on else "Voice is off",
                        "Nothing leaves this PC until you say it" if v.wake_on else "Turn it on in Settings", [],
                        live=v.wake_on))
    rcol.addStretch(1)

    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=2, spacing=14, min_col=330, weights=(1.9, 1.0))
    rows.setParent(grid)
    cards.add(rows, 0)
    health = _health(t, v)
    health.setParent(grid)
    cards.add(health, 1)
    col.addWidget(grid)
    col.addStretch(1)
    return page
