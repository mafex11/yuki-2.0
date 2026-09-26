"""Status: what Yuki is doing or waiting on, in a few plain words each, and nothing to set.

Status only (2026-09-27): memory remembering your day, calls waiting to be remembered, a call going on,
questions waiting, the coach, whether Yuki listens for its name, and today's cost. The settings that used
to sit here (listening for "Yuki", Start with Windows) live in Settings. Each card is a capsule with a short
title, one short line under it and at most the one action it needs, as a big glass button.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import StatusView, dur, first_sentence, hm, plain, second_person
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import Glass, Pill, icon_label, page_header, text_label

#: A status card's height; its ends are fully round (a capsule).
CARD_H = 84


def _row(t: Theme, tone: QColor | None, glyph: str, title: str, text: str, actions: list[QWidget]) -> Glass:
    """One status card: a capsule with the state in a short title, one short line, and its action. Something
    that needs the user (``tone``) gets a faint wash of that colour on its glass instead of a coloured dot."""
    wash = None
    if tone is not None:
        wash = QColor(tone)
        wash.setAlphaF(0.07 if not t.dark else 0.10)
    card = Glass(t, level="regular", radius=999, padding=0, tint=wash)   # radius past half the height: a capsule
    card.setMinimumHeight(CARD_H)
    card.body.setContentsMargins(18, 14, 22, 14)
    row = QHBoxLayout()
    row.setSpacing(SPACE["m"] + 2)
    icon = Glass(t, level="inset", radius=999, padding=0)
    icon.setFixedSize(52, 52)
    lab = icon_label(t, glyph, t.text_2, "icon_large")
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.body.addWidget(lab)
    row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addStretch(1)
    col.addWidget(text_label(t, title, "headline"))
    if text:
        col.addWidget(text_label(t, text, "caption", "text_3", wrap=True))
    col.addStretch(1)
    row.addLayout(col, 1)
    for a in actions:
        row.addWidget(a, 0, Qt.AlignmentFlag.AlignVCenter)
    card.body.addLayout(row)
    return card


def _pill(t: Theme, text: str, on_click, **kw) -> Pill:
    pill = Pill(t, text, **kw)
    pill.clicked.connect(lambda _c=False: on_click())
    return pill


def _confirm_pill(t: Theme, text: str, sure: str, on_click, **kw) -> Pill:
    """A pill that asks once ("Erase" -> "Sure? Tap again") and acts on the second click within 4 s."""
    pill = Pill(t, text, **kw)
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


def build(t: Theme, v: StatusView, *, act: Actions = NOOP) -> QWidget:
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["m"])
    # fine needs no colour at all; what needs the user gets a faint warm or red wash, with the words
    good, warn, bad = None, t.kind("nudge"), t.kind("error")
    waiting_calls = (v.calls_waiting or {}).get("calls", 0)
    waiting = waiting_calls + len(v.questions_waiting)
    if not v.service_running:
        lead = "Memory isn't running, so today isn't being remembered."
    elif waiting:
        lead = f"All running. {waiting} thing{'s' if waiting != 1 else ''} waiting, nothing urgent."
    else:
        lead = "All running. Nothing is waiting on you."
    col.addWidget(page_header(t, "Status", lead))

    if v.service_running and not v.paused:
        seen = f"{v.captures_today:,} things seen, {v.facts_today:,} facts written today"
        col.addWidget(_row(t, good, "history", "Remembering your day", seen,
                           [_pill(t, "Pause", lambda: act.set_paused(True), glyph="pause")]))
    elif v.paused:
        col.addWidget(_row(t, warn, "pause", "Memory is paused", "Nothing on your screen is remembered",
                           [_pill(t, "Resume", lambda: act.set_paused(False), glyph="play", style="accent")]))
    else:
        col.addWidget(_row(t, bad, "warning", "Memory isn't running", "Yuki restarts it within two minutes",
                           [_pill(t, "Start", act.start_memory, style="accent")]))

    if waiting_calls:
        w = v.calls_waiting
        col.addWidget(_row(t, warn, "mic", f"{waiting_calls} call{'s' if waiting_calls != 1 else ''} waiting to "
                                              "be remembered",
                           f"{dur(w['minutes'] * 60)} of your voice, kept on this PC until it's free",
                           [_confirm_pill(t, "Erase", "Sure? Tap again", act.erase_calls, glyph="delete",
                                          style="danger")]))
    if v.call_now:
        recording = v.call_now.get("status") == "recording"
        col.addWidget(_row(t, t.kind("call"), "phone", f"On a {v.call_now['app']} call",
                           "Only your side is remembered" if recording else "This call isn't remembered",
                           [_pill(t, "Stop", act.stop_call, glyph="close")] if recording else []))
    if v.questions_waiting:
        q = v.questions_waiting[0]
        n = len(v.questions_waiting)
        col.addWidget(_row(t, t.kind("question"), "question", "A question for you" if n == 1 else
                           f"{n} questions for you", first_sentence(plain(second_person(q["question"])), 90),
                           [_pill(t, "Answer", lambda: act.navigate("yuki"), glyph="edit", style="tinted",
                                  tint=t.kind("question"))]))

    if v.quiet_until:
        col.addWidget(_row(t, warn, "moon", f"Coach is quiet until {hm(v.quiet_until)}",
                           "Reminders wait, none are lost",
                           [_pill(t, "Turn back on", lambda: act.snooze(0), style="accent")]))
    else:
        start, end = v.quiet_hours
        quiet = f"Quiet from {start} to {end}" if start != end else "No quiet hours"
        col.addWidget(_row(t, good, "heart", "Coach is on", quiet,
                           [_pill(t, "Quiet 1 hour", lambda: act.snooze(60), glyph="moon"),
                            _pill(t, "Until tomorrow", lambda: act.snooze(minutes_until(6)))]))
    col.addWidget(_row(t, None, "wave", "Say “Yuki” to start" if v.wake_on else "Voice is off",
                       "Nothing leaves this PC until you say it" if v.wake_on else "Turn it on in Settings", []))
    col.addWidget(_row(t, None, "money", f"${v.cost_today:.2f} today", "What remembering cost today", []))
    col.addStretch(1)
    return page
