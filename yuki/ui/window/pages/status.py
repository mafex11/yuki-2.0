"""Status: everything Yuki is doing or waiting on, in plain words, each with the one action it needs."""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import StatusView, day_label, dur, hm
from yuki.ui.window.theme import SPACE, Theme
from yuki.ui.window.widgets import Dot, Glass, Pill, Toggle, icon_label, page_header, text_label


def _row(t: Theme, dot: QColor, glyph: str, title: str, text: str, actions: list[QWidget]) -> Glass:
    card = Glass(t, level="regular", radius=22, padding=SPACE["l"])
    row = QHBoxLayout()
    row.setSpacing(SPACE["m"])
    icon = Glass(t, level="inset", radius=18, padding=0)
    icon.setFixedSize(40, 40)
    lab = icon_label(t, glyph, t.text_2, "icon_large")
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.body.addWidget(lab)
    row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(3)
    head = QHBoxLayout()
    head.setSpacing(4)
    head.addWidget(Dot(t, dot, 8))
    head.addWidget(text_label(t, title, "headline"))
    head.addStretch(1)
    col.addLayout(head)
    col.addWidget(text_label(t, text, "callout", "text_2", wrap=True))
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
    """A pill that asks once ("Erase it" -> "Erase — sure?") and acts on the second click within 4 s."""
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
    good, warn, bad = t.status["good"], t.status["warning"], t.status["critical"]
    waiting_calls = (v.calls_waiting or {}).get("calls", 0)
    waiting = waiting_calls + len(v.questions_waiting)
    if not v.service_running:
        lead = "Memory isn't running, so Yuki can't remember today. Start it below."
    elif waiting:
        lead = f"Everything is running. {waiting} thing{'s' if waiting != 1 else ''} waiting, none of it urgent."
    else:
        lead = "Everything is running and nothing is waiting on you."
    col.addWidget(page_header(t, "Status", lead))

    if v.service_running and not v.paused:
        last = f"last look {hm(v.last_capture_at)}" if v.last_capture_at else "nothing seen yet today"
        col.addWidget(_row(t, good, "history", "Remembering your day",
                           f"{v.captures_today} things seen and {v.facts_today} facts written today, {last}. "
                           f"Your portrait was updated {day_label(v.portrait_at)} {hm(v.portrait_at)}."
                           if v.portrait_at else f"{v.captures_today} things seen today, {last}.",
                           [_pill(t, "Pause memory", lambda: act.set_paused(True), glyph="pause", style="glass",
                                  size="small")]))
    elif v.paused:
        col.addWidget(_row(t, warn, "pause", "Memory is paused", "Nothing on your screen is being remembered. "
                           "What you type to Yuki still is.",
                           [_pill(t, "Resume", lambda: act.set_paused(False), glyph="play", style="accent")]))
    else:
        col.addWidget(_row(t, bad, "warning", "Memory isn't running", "Yuki normally restarts it within two "
                           "minutes. If this stays, start it now.",
                           [_pill(t, "Start memory", act.start_memory, style="accent")]))

    if waiting_calls:
        w = v.calls_waiting
        oldest = datetime.fromtimestamp(v.oldest_wait_at) if v.oldest_wait_at else None
        since = f" The oldest is from {day_label(v.oldest_wait_at)} {oldest:%H:%M}." if oldest else ""
        col.addWidget(_row(t, warn, "mic", f"{waiting_calls} call{'s' if waiting_calls != 1 else ''} waiting to "
                                              "be remembered",
                           f"{dur(w['minutes'] * 60)} of your own voice, kept encrypted on this PC. Yuki turns it "
                           f"into memory when the PC is free, never while you play or are on a call.{since}",
                           [_confirm_pill(t, "Erase it", "Erase — sure?", act.erase_calls, glyph="delete",
                                          style="danger", size="small")]))
    if v.call_now:
        col.addWidget(_row(t, t.kind("call"), "phone", f"On a {v.call_now['app']} call",
                           "Remembering only your side, only your microphone." if v.call_now.get("status") ==
                           "recording" else "Not remembering this call.",
                           [_pill(t, "Stop remembering", act.stop_call, style="glass", size="small")]
                           if v.call_now.get("status") == "recording" else []))
    if v.questions_waiting:
        q = v.questions_waiting[0]
        more = f" and {len(v.questions_waiting) - 1} more" if len(v.questions_waiting) > 1 else ""
        col.addWidget(_row(t, t.kind("question"), "question", "A question for you" if not more else
                           f"{len(v.questions_waiting)} questions for you",
                           f"“{q['question'].replace('the user', 'you')}”{more} Yuki asks at a calm moment, or you can "
                           "answer it now.",
                           [_pill(t, "Answer", lambda: act.navigate("yuki"), glyph="edit", style="tinted",
                                  tint=t.kind("question"), size="small")]))

    today = v.nudges_today
    said = ", ".join(f"{n} {word}{'s' if n != 1 else ''}" for word, n in
                     (("check-in", today.get("nudge", 0) + today.get("praise", 0)),
                      ("reminder", today.get("reminder", 0)), ("welcome back", today.get("resume", 0)),
                      ("question", today.get("question", 0))) if n)
    if v.quiet_until:
        col.addWidget(_row(t, warn, "moon", f"Coach is quiet until {hm(v.quiet_until)}",
                           "No check-ins or reminders until then. A due reminder waits; it isn't lost.",
                           [_pill(t, "Turn back on", lambda: act.snooze(0), style="accent", size="small")]))
    else:
        col.addWidget(_row(t, good, "heart", "Coach is on",
                           f"Quiet hours {v.quiet_hours[0]}–{v.quiet_hours[1]}. Today: {said or 'nothing yet'}.",
                           [_pill(t, "Quiet 1 hour", lambda: act.snooze(60), glyph="moon", size="small"),
                            _pill(t, "Until tomorrow", lambda: act.snooze(minutes_until(6)), size="small")]))
    col.addWidget(_row(t, good if v.wake_on else t.text_3, "mic",
                       "Listening for “Yuki”" if v.wake_on else "Not listening for “Yuki”",
                       f"Say “Yuki” or press Ctrl+Alt+Y. Mic: {v.mic}. Speaker: {v.speaker}. "
                       "Nothing is sent anywhere until you say its name.",
                       [_pill(t, "Turn off" if v.wake_on else "Turn on", lambda: act.set_wake(not v.wake_on),
                              size="small")]))
    col.addWidget(_row(t, t.accent, "money", f"${v.cost_today:.2f} today",
                       "What remembering cost today: reading your screen into facts, your portrait, the story "
                       "of your day, your chats with Yuki and the coach (list prices).", []))
    if v.start_with_windows is not None:
        sw = Toggle(t, bool(v.start_with_windows))
        sw.toggled.connect(act.set_startup)
        col.addWidget(_row(t, good if v.start_with_windows else t.text_3, "power",
                           "Starts with Windows" if v.start_with_windows else "Doesn't start with Windows",
                           "Yuki and its memory come up when you sign in." if v.start_with_windows else
                           "Start Yuki yourself from the Start menu.", [sw]))
    col.addStretch(1)
    return page
