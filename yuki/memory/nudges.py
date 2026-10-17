"""The card worker: the ``[nudges]`` config, the list items, the session helpers, and :class:`NudgeWorker`, which
hosts the one card judge.

Contract: ``docs/MEMORY.md`` -> "Nudges and to-dos".

**The card judge writes every card** (18 Oct 2026, :mod:`yuki.memory.cardjudge`): :meth:`NudgeWorker.run` calls
``CardJudge.tick`` in every ``[judge] mode`` (its upkeep, then one evaluation when YouNow woke it or a deadline came)
and waits on YouNow's wake event, at most a minute. ``on`` (the default): every card. ``shadow`` / ``off``: only the
user's own reminders and standing requests (shadow also logs what it would have shown of the rest). The old
first-writer chain (the coach's look, its welcome back, their prompts and gates, the observation detectors) is
deleted; its producers are the judge's candidate generators (:mod:`yuki.memory.candidates`).

What stays here: the config (:class:`NudgeConfig`), the list items (:func:`todo_items`: open loops, Yuki's
commitments, the user's to-dos), the sessions YouNow and the generators read (:func:`activity_sessions`: a dominant
app with its short hops folded in; :func:`presence_sessions`), the RULES and CARD FEEDBACK blocks the card writer
and the questions read (:func:`rules_text`, :func:`feedback_text`), the UI's wake event (``Local\\YukiNudgeReady``),
and the worker that hosts the judge (its model client and log, the check-in fold, the events scan, the routine lane).

Every card is stored (``nudges``, encrypted) with its check-in (``nudge_checkins``, content-free) in one transaction,
and the wake event is set. Model calls are logged to ``logs/memory/nudges-YYYYMMDD.jsonl`` (usage, cost, latency in
the clear; request and response encrypted).

Public API::

    NUDGE_EVENT_NAME;  NudgeConfig(...).from_dict(table);  in_quiet_hours(at, config) -> bool
    todo_items(store, *, include_done=False, now=None) -> list[Item];  item_dict(item) -> dict
    complete_item(store, ref, *, now=None) -> str | None    # ref: an id ("todo:3"), never text
    reaction_summary(store, since, until=None) -> str;  signal_nudge(event_name) -> bool
    activity_sessions(rows, since, until, config) -> list[Session]
    coach_facts(store, at=None) -> (rules, [(feedback fact, card)]);  rules_text / feedback_text
    NudgeWorker(store, *, db_path=None, privacy=None, config=None, settings=None, client=None, log_dir=None,
                model=HAIKU_MODEL, writer_model=SONNET_MODEL, locked_fn=None, live_fn=None, clock=time.time,
                event_name=NUDGE_EVENT_NAME, log=None, embedder=None, you_fn=None)
        .run(stop, pause_path=None) / .stop()   # the card judge's loop (CardJudge.tick)
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from yuki.config import Settings
from yuki.log.events import _as_plain
from yuki.log.requests import usage_tokens
from yuki.memory.journal import HAIKU_MODEL, user_identity
from yuki.memory.store import REFRESH_FLAG, NudgeCheckin, Store, TimelineRow, flag_path
from yuki.memory.personality import nature_mark
from yuki.memory.timeline import FLUSH_EVERY_S, Run, _clip, _runs, format_duration, is_power

#: Nudge kinds the coach's budget counts (one of these per budget_min at most; reminders are exempt).
#: The coach no longer writes praise (the user's decision, 2026-09-26); old praise rows still count.
COACH_KINDS: tuple[str, ...] = ("praise", "nudge", "resume")
#: Check-in triggers of the observation cards (yuki.memory.presence): not the coach's own looks.
PRESENCE_TRIGGERS: tuple[str, ...] = ("notice", "stuck", "recap", "followup", "recall", "thread", "followup_scan",
                                      "plans")
#: Check-in triggers of Yuki's check-ins about the user's goals (yuki.memory.checkins.CHECKIN_TRIGGERS): not looks
#: at the session either.
CHECKIN_TRIGGERS: tuple[str, ...] = ("checkin_day_start", "checkin_followup", "checkin_care", "checkin_evening",
                                     "checkin_new_thread", "checkin_fold")
#: How far back the resume trigger looks for the gap and for the user's last settled stretch before it.
RESUME_SEARCH_S = 36 * 3600.0
#: The card writer: every card the user sees is written by Sonnet 5 (Haiku decides whether to speak).
SONNET_MODEL = "global.anthropic.claude-sonnet-5"

NUDGE_EVENT_NAME = "Local\\YukiNudgeReady"
#: A timeline row reaching this close to now is the open stretch (written every FLUSH_EVERY_S).
ONGOING_S = 2 * FLUSH_EVERY_S + 15.0
#: No card while a game or a full-screen app is in front, nor in the hops between rounds: a game or full-screen
#: stretch that ended less than ``[nudges] return_hold_s`` ago (this default) still holds the cards (report 76: a "go
#: stretch" card came at 01:05 on 5 Oct, in a 40-second alt-tab to Discord in the middle of a VALORANT session).
#: fGate (F4, the user's decision of 9 Oct: a game open but not in front lets cards run): this replaces the 10-minute
#: full-screen hold and the game voice row's 5-minute hold, which on 8 Oct held the cards 19 minutes with no game in
#: front (VALORANT last in front 08:41:52, cards back only at 09:00:46).
RETURN_HOLD_S = 120.0
#: Per-block caps inside a coach request (characters).
JOURNAL_CHARS = 5_000
#: Done items shown by ``todo_items(include_done=True)``: those done within this many days.
DONE_DAYS = 14


# ---------------------------------------------------------------------------
# Configuration ([nudges] in the privacy file)
# ---------------------------------------------------------------------------


def _hhmm(value: Any, default: str) -> str:
    try:
        text = str(value).strip()
        hours, minutes = text.split(":")
        h, m = int(hours), int(minutes)
        if 0 <= h <= 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except (TypeError, ValueError):
        pass
    return default


@dataclass(frozen=True)
class NudgeConfig:
    """The ``[nudges]`` table (all optional; these are the defaults)."""

    enabled: bool = True
    #: Local quiet hours "HH:MM" (start may be after end: it wraps midnight); equal = none.
    quiet_start: str = "00:30"
    quiet_end: str = "08:00"
    #: At most one nudge or welcome back per this many minutes (reminders aside).
    budget_min: float = 30.0
    periodic_min: float = 15.0
    transition_hold_s: float = 90.0
    min_gap_s: float = 90.0
    max_calls_per_hour: int = 12
    presence_min: float = 5.0
    lookback_min: float = 120.0
    break_min: float = 5.0
    #: A break suggestion only after this long at the PC without a break of break_min.
    break_after_min: float = 90.0
    deliver_within_min: float = 15.0
    reminder_late_max_h: float = 24.0
    all_day_at: str = "09:00"
    #: "Picking up" after time away (trigger ``resume``): away - the PC off, asleep, or on with
    #: nobody at it - at least resume_after_h, or it is the user's first presence of a new day
    #: (days start at resume_day_start) after at least break_min away. Sent once they have settled
    #: in (transition_hold_s), at the first calm moment within resume_within_min of their return.
    resume_enabled: bool = True
    resume_after_h: float = 3.0
    resume_day_start: str = "04:00"
    resume_within_min: float = 60.0
    #: What the message may draw on: the user's last this-many minutes at the PC before the gap.
    resume_before_min: float = 90.0
    #: Getting-to-know-you questions (kind ``question``, :mod:`yuki.memory.questions`): the most
    #: important open one, at a calm moment (the gates above, no call, no other card in the last
    #: min_gap_s), at most one per question_gap_min and questions_per_day a day (days start at
    #: resume_day_start); only questions of question_min_importance or more whose evidence is at
    #: most question_max_age_days old (older ones expire unasked).
    questions_enabled: bool = True
    #: Minutes of the user's presence at the PC (not wall-clock) between two questions, and before the
    #: first one after they come back.
    question_gap_min: float = 20.0
    #: Fewer than before 2026-10-03 (6): a question must be worth the user's attention, and check-ins share the
    #: one-waiting-at-a-time rule with them.
    questions_per_day: int = 2
    question_min_importance: int = 5
    question_max_age_days: float = 7.0
    #: Cards (praise, notice, nudge, welcome back, question, a call plan's reminder) at least this far
    #: apart; an item due at an exact time is reminded on time regardless.
    card_gap_min: float = 10.0
    #: Sessions: a hop to another app shorter than this stays part of the session it interrupts (an
    #: hour of VALORANT with 3-5 minute hops to Discord is one session).
    session_excursion_min: float = 5.0
    #: No look at all before the user has been at the PC this long (a break nudge needs a long session;
    #: time of day and routine are the model's to weigh after that).
    break_consider_min: float = 45.0
    #: A call-back only once a new session has held this long, with journal facts about it.
    callback_after_min: float = 15.0
    #: At most one break nudge per session, and at least this long between two.
    break_gap_min: float = 60.0
    #: A plan the user said on a call is offered as a reminder only while its moment is at least this far
    #: ahead (a reminder cannot be scheduled for the past, and one five minutes out is no use).
    confirm_lead_min: float = 30.0
    #: After a meeting row, the meeting is held open while its app still plays sound (Meet drops the
    #: microphone on mute), for at most this long after the microphone was last seen.
    meeting_hold_min: float = 30.0
    #: fGate (F4): cards come back this long after a game or full-screen stretch ended (the hop between rounds).
    return_hold_s: float = RETURN_HOLD_S
    #: Observation cards (yuki.memory.presence, kind notice): on, at most this many a day (days start at
    #: resume_day_start; each kind has its own budget in yuki.core.attention), and one not shown within
    #: presence_deliver_min expires unshown (it stays in the card history as missed).
    presence_enabled: bool = True
    presence_per_day: int = 12
    presence_deliver_min: float = 30.0
    #: What cards may offer (06 §4.5): "yuki_only" (the default: one small typed action of Yuki's own, run by code,
    #: yuki.memory.offers) or "off" (cards only inform; no button runs anything).
    offers: str = "yuki_only"
    #: Check-ins (yuki.memory.checkins, 2026-10-03): Yuki asks how the user's goals stand and how they are doing -
    #: past, present, future - at measured moments; at most checkins_per_day a day (one per time frame), never while
    #: another question card waits unanswered, at least checkin_gap_min of presence after the last question card.
    checkins_enabled: bool = True
    checkins_per_day: int = 3
    checkin_gap_min: float = 90.0
    #: The routine card ("Your usual", yuki.memory.routine_offers, PLAN.md Phase R): on; at most routine_per_day a day
    #: (days start at resume_day_start); in the usual window, from routine_lead_min to routine_lead_last_min before
    #: the usual time (mode early; mode late: 1-3 min after it); its button expires routine_expire_after_min after the
    #: usual time; once the user sat down it waits routine_settle_s (D11). ``offers = "off"`` stops it too.
    routine_offers: bool = True
    routine_per_day: int = 2
    routine_lead_min: float = 8.0
    routine_lead_last_min: float = 1.0
    routine_expire_after_min: float = 10.0
    routine_settle_s: float = 30.0

    @classmethod
    def from_dict(cls, table: dict | None) -> NudgeConfig:
        table = table or {}
        base = cls()
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in table:
                continue
            raw, default = table[f.name], getattr(base, f.name)
            if f.name in _CHOICES:   # a word from a fixed set of settings values (machine format)
                value = str(raw).strip().casefold()
                if value in _CHOICES[f.name]:
                    values[f.name] = value
                continue
            try:
                if isinstance(default, bool):
                    values[f.name] = bool(raw)
                elif isinstance(default, int):
                    values[f.name] = max(0, int(raw))
                elif isinstance(default, float):
                    values[f.name] = max(0.0, float(raw))
                else:
                    values[f.name] = _hhmm(raw, default)
            except (TypeError, ValueError):
                continue
        return cls(**{**{f.name: getattr(base, f.name) for f in fields(cls)}, **values})


#: Settings whose value is one of a fixed set (``[nudges] offers``).
_CHOICES: dict[str, tuple[str, ...]] = {"offers": ("yuki_only", "off")}


def _minutes_of_day(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(at: float, config: NudgeConfig) -> bool:
    """Whether local time ``at`` falls in ``[quiet_start, quiet_end)`` (wrapping midnight)."""
    start, end = _minutes_of_day(config.quiet_start), _minutes_of_day(config.quiet_end)
    if start == end:
        return False
    t = datetime.fromtimestamp(at)
    now = t.hour * 60 + t.minute
    return start <= now < end if start < end else (now >= start or now < end)


# ---------------------------------------------------------------------------
# The to-do list: open loops + commitments + the user's own to-dos
# ---------------------------------------------------------------------------


@dataclass
class Item:
    ref: str                          # loop:12 | commit:5 | todo:3
    text: str
    #: loop | commitment (Yuki's own promise) | chat (the user's own to-do, said to Yuki) | call (the user's
    #: own words on a call) | user (a to-do the user added)
    source: str
    due_at: float | None
    all_day: bool
    status: str                       # open | done | dropped (not needed) | wrong (captured by mistake)
    evidence: str | None
    since: float
    #: Whose it is (migration 18): "yuki" (Yuki promised or was handed it) or "user" (the user's own to-do).
    owner: str = "user"
    #: The app's short title (migration 21, yuki.memory.titles); None until written.
    title: str | None = None


#: What each source is, for the API and the UI: Yuki's commitments apart from the user's to-dos.
SOURCE_LABELS = {"commitment": "Yuki's promise", "chat": "your to-do, told to Yuki", "call": "your to-do, said on a call",
                 "user": "your to-do", "loop": "open loop"}


def _is_midnight(at: float) -> bool:
    t = datetime.fromtimestamp(at)
    return t.hour == 0 and t.minute == 0 and t.second == 0


def _iso(at: float | None) -> str | None:
    if at is None:
        return None
    return datetime.fromtimestamp(float(at)).astimezone().isoformat(timespec="seconds")


def _due_iso(item: Item) -> str | None:
    if item.due_at is None:
        return None
    if item.all_day:
        return datetime.fromtimestamp(item.due_at).strftime("%Y-%m-%d")
    return _iso(item.due_at)


def _split_loop(text: str) -> tuple[str, str | None]:
    """An open loop's stored text is ``"<what is owed> (<evidence>)."`` (portrait._loop_text): split it."""
    t = " ".join((text or "").split())
    if t.endswith(").") and " (" in t:
        body, _, evidence = t[:-2].rpartition(" (")
        if body and evidence:
            return body.rstrip(".") + ".", evidence
    return t, None


def todo_items(store: Store, *, include_done: bool = False, now: float | None = None) -> list[Item]:
    """The merged to-do list: open first (soonest due first, undated after), then done (newest first)."""
    now = time.time() if now is None else float(now)
    done_since = now - DONE_DAYS * 86400.0
    items: list[Item] = []
    for loop in store.open_loops("open"):
        text, evidence = _split_loop(loop.text)
        items.append(Item(f"loop:{loop.id}", text, "loop", None, False, "open", evidence, loop.opened_at,
                          title=loop.title))
    statuses = ("active", "done") if include_done else ("active",)
    for f in store.conversation_facts(("commitment",), statuses):
        if f.status == "done" and (f.valid_to or 0.0) < done_since:
            continue
        evidence = None
        users = f.origin == "call" or f.owner == "user"   # the user's own to-do, not Yuki's promise
        if f.quote and f.origin == "call":   # the user's own words in a call (yuki.memory.calls)
            evidence = f"the user said in a call on {datetime.fromtimestamp(f.valid_from):%Y-%m-%d}: \"{f.quote}\""
        elif f.quote:
            said = "said" if users else "asked"
            evidence = f"the user {said} on {datetime.fromtimestamp(f.valid_from):%Y-%m-%d}: \"{f.quote}\""
        source = "call" if f.origin == "call" else "chat" if f.owner == "user" else "commitment"
        items.append(Item(
            f"commit:{f.id}", f.text, source, f.due_at,
            f.due_at is not None and _is_midnight(f.due_at), "open" if f.status == "active" else "done", evidence,
            f.valid_from, owner="user" if users else "yuki", title=f.title,
        ))
    for t in store.todos(("open", "done") if include_done else ("open",), done_since=done_since):
        items.append(Item(f"todo:{t.id}", t.text, "user", t.due_at, t.due_all_day, t.status, None, t.created_at,
                          title=t.title))
    if include_done:
        for loop in store.open_loops_done_since(done_since):
            text, evidence = _split_loop(loop.text)
            items.append(Item(f"loop:{loop.id}", text, "loop", None, False, "done", evidence, loop.opened_at,
                              title=loop.title))
    open_items = sorted((i for i in items if i.status == "open"),
                        key=lambda i: (i.due_at is None, i.due_at or 0.0, i.since))
    done = sorted((i for i in items if i.status != "open"), key=lambda i: -i.since)
    return open_items + done


def item_dict(item: Item) -> dict[str, Any]:
    """The API shape of one to-do."""
    return {"id": item.ref, "text": item.text, "source": item.source, "due": _due_iso(item),
            "status": item.status, "evidence": item.evidence, "since": _iso(item.since),
            "owner": item.owner, "label": SOURCE_LABELS.get(item.source, item.source), "title": item.title}


def _parse_ref(ref: str) -> tuple[str, int] | None:
    """``"todo:3"`` (also as the lists show it, ``"[todo:3]"``) -> ``("todo", 3)``: our own id format."""
    prefix, sep, number = (ref or "").strip().strip("[]").strip().partition(":")
    if sep and prefix in ("loop", "commit", "todo") and number.strip().isdigit():
        return prefix, int(number)
    return None


def complete_item(store: Store, ref: str, *, now: float | None = None) -> str | None:
    """Mark one item done by its id ("loop:12", "commit:5", "todo:3"); returns the id, or None.

    The model picks the id from the to-do list or the standing block it was
    shown (docs/research/no-pattern-audit.md F): no text is matched here. None
    when ``ref`` is not an id or the item is not open.
    """
    return end_item(store, ref, "done", now=now)


#: How each way of ending an item reads in memory: the note kept on a commitment, and the correction an
#: open loop's portrait fact gets (so later runs do not re-open it from old evidence).
END_NOTES = {
    "done": ("the user marked it done", "The user said this is done and no longer pending: {text}"),
    "dropped": ("the user said it is not needed", "The user said this is not needed any more: {text}"),
    "wrong": ("the user said it was captured by mistake, never a real to-do",
              "The user said this was captured by mistake and was never pending: {text}"),
}


def end_item(store: Store, ref: str, status: str = "done", *, now: float | None = None) -> str | None:
    """End one item by its id ("loop:12", "commit:5", "todo:3") as ``done``, ``dropped`` (not needed) or
    ``wrong`` (captured by mistake); returns the id, or None when ``ref`` is not an id, the item is not open
    or ``status`` is not one of those. No text is matched: the caller (the UI's buttons, the agent's tool)
    names the item by its id. Ended items leave the list, the coach and the reminders; a wrong capture is
    shown to the conversation extractor among the recently ended items, so the like is not captured again.
    """
    if status not in END_NOTES:
        return None
    now = time.time() if now is None else float(now)
    parsed = _parse_ref(ref)
    if parsed is None:
        return None
    kind, number = parsed
    note, correction = END_NOTES[status]
    if kind == "todo":
        ok = store.end_todo(number, status, at=now)
    elif kind == "commit":
        fact = store.conversation_fact(number)
        ok = fact is not None and fact.kind == "commitment" and store.end_conversation_fact(
            number, status, at=now, note=note)
    else:
        loops = {l.id: l for l in store.open_loops("open")}
        loop = loops.get(number)
        ok = loop is not None and store.complete_open_loop(number, at=now)
        if ok:
            # the user's word, through the portrait's own path: the render follows it at once and the
            # next run folds it in (so the loop is not re-added from old evidence); re-render soon
            text, _ = _split_loop(loop.text)
            store.add_correction(correction.format(text=text), at=now, apply=False)
            try:
                path = flag_path(store.path, REFRESH_FLAG)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
            except OSError:
                pass
    return f"{kind}:{number}" if ok else None


# ---------------------------------------------------------------------------
# Reactions (read by the coach and by the portrait's Relationship input)
# ---------------------------------------------------------------------------


def reaction_summary(store: Store, since: float, until: float | None = None) -> str:
    """How the user took the coach's nudges in ``[since, until)``, as one line of counts ("" if none)."""
    rows = store.nudges(since, until)
    if not rows:
        return ""
    by_kind: dict[str, dict[str, int]] = {}
    for n in rows:
        counts = by_kind.setdefault(n.kind, {})
        key = n.reaction or "not reacted to"
        counts[key] = counts.get(key, 0) + 1
    parts = []
    for kind in ("praise", "nudge", "reminder", "review", "resume", "question", "notice"):
        counts = by_kind.get(kind)
        if not counts:
            continue
        total = sum(counts.values())
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        parts.append(f"{kind} {total} ({detail})")
    return f"{len(rows)} nudges: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# The wake-up event for the UI
# ---------------------------------------------------------------------------


class _NudgeEvent:
    """The named auto-reset event the UI waits on; created (and held open) by the worker."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateEventW.restype = ctypes.c_void_p
            kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
            self._handle = kernel32.CreateEventW(None, False, False, name)   # auto-reset, not signalled
        except Exception:
            self._handle = None

    def set(self) -> bool:
        if not self._handle:
            return signal_nudge(self.name)
        return bool(ctypes.windll.kernel32.SetEvent(ctypes.c_void_p(self._handle)))

    def close(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


def signal_nudge(event_name: str = NUDGE_EVENT_NAME) -> bool:
    """Set the UI's wake-up event if it exists (False when nobody created it)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.OpenEventW(0x0002, False, event_name)   # EVENT_MODIFY_STATE
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# What the timeline says now
# ---------------------------------------------------------------------------


def _break_between(clips: Sequence[Any], start: float, end: float) -> dict[str, Any]:
    """What the time between two stretches at the PC was (``timeline.breaks`` shape): PC off or asleep, away, gap."""
    power: Counter = Counter()
    away = 0.0
    for c in clips:
        overlap = min(end, c.end) - max(start, c.start)
        if overlap <= 0 or c.row.state == "present":
            continue
        if is_power(c.row):
            power[c.row.state] += overlap
        else:
            away += overlap
    seconds = max(0.0, end - start)
    gap = max(0.0, seconds - sum(power.values()) - away)
    parts = {**power, "away": away, "gap": gap}
    kind = max(parts, key=lambda k: parts[k]) if seconds > 0 else "gap"
    return {"start": start, "end": end, "seconds": round(seconds, 1), "ongoing": False, "kind": kind,
            "power": {k: round(v, 1) for k, v in power.items()}, "away_s": round(away, 1), "gap_s": round(gap, 1)}


#: The content-free rows of the last day and a half, per database: (fetched at, split, rows ended by split).
_WIDE: dict[str, tuple[float, float, list[TimelineRow]]] = {}
#: Rows that ended this long ago no longer change (the recorder rewrites only its open stretch).
WIDE_SETTLED_S = 600.0
#: The settled part is read again at most this often (a 36-hour read is ~16 ms; the fresh tail ~1 ms).
WIDE_REFRESH_S = 300.0


def wide_rows(store: Store, now: float) -> list[TimelineRow]:
    """The user's content-free timeline rows of the last :data:`RESUME_SEARCH_S`, read cheaply every tick.

    Rows that ended more than :data:`WIDE_SETTLED_S` ago are cached (refreshed every
    :data:`WIDE_REFRESH_S`); only the fresh tail is read each time.
    """
    key = str(store.path)
    split = now - WIDE_SETTLED_S
    hit = _WIDE.get(key)
    if hit is None or not 0 <= now - hit[0] < WIDE_REFRESH_S or hit[1] > split:
        settled = [r for r in store.timeline_states(now - RESUME_SEARCH_S, split) if r.ended_at <= split]
        hit = _WIDE[key] = (now, split, settled)
    since = now - RESUME_SEARCH_S
    rows = [r for r in hit[2] if r.ended_at > since]
    rows += [r for r in store.timeline_states(hit[1], now) if r.ended_at > hit[1]]
    return [r for r in rows if not r.by_yuki]


#: A program's game verdict is reused this long (the game set changes when a game is learned or the user edits it).
_GAME_PROCESS_FRESH_S = 60.0
_game_processes: dict[str, tuple[float, bool]] = {}


def _game_process(process: str) -> bool:
    """fGate (D9 for replays): whether a timeline row's program is a game, by the game gate's machine facts
    (:func:`yuki.memory.gamegate.judge_program`: the fixed list, the user's additions, Windows' detected games, the
    live gate's learned set when one runs here). Never the live Direct3D state: a row is history."""
    if not process:
        return False
    now = time.monotonic()
    hit = _game_processes.get(process)
    if hit is not None and now - hit[0] < _GAME_PROCESS_FRESH_S:
        return hit[1]
    from yuki.memory.gamegate import judge_program

    try:
        game = bool(judge_program(process, d3d_fn=lambda: False)[0])
    except Exception:
        game = False
    if len(_game_processes) > 256:
        _game_processes.clear()
    _game_processes[process] = (now, game)
    return game


def game_row(row: TimelineRow) -> bool:
    """A timeline row whose program is a game (:func:`_game_process`)."""
    return _game_process(str(row.process or ""))


# ---------------------------------------------------------------------------
# Lines and sessions (YouNow, before.py, the rules and feedback blocks)
# ---------------------------------------------------------------------------


def _hm(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M")


def _run_line(run: Run, safe) -> str:
    app = run.apps.most_common(1)[0][0] if run.apps else None
    where = f" in {safe(app, 60)}" if app and app != run.label else ""
    how = f"active {format_duration(run.active_s)}"
    if run.passive_s >= 30:
        how += f", watching {format_duration(run.passive_s)}"
    title = run.titles.most_common(1)[0][0] if run.titles else None
    shown = f", window title \"{safe(title, 140)}\" (a title alone is not what they are doing)" if title else ""
    return (f"{safe(run.label, 100)}{where}: {format_duration(run.present_s)} ({how}), "
            f"{_hm(run.start)}-{_hm(run.end)}{shown}")


@dataclass
class Session:
    """A stretch of one dominant activity with its short hops elsewhere folded in (content-free, measured).

    An hour of VALORANT with 3-5 minute hops to Discord is one session; Warp with hops to
    Chrome, Slack or Discord is one. What the session *is* (gaming, which project, their
    job) is the model's judgement from content; code only measures the block.
    """

    key: str                          # the dominant activity (timeline.group_of at app grouping)
    label: str
    start: float
    end: float
    present_s: float = 0.0            # all present time in it, hops included
    anchor_s: float = 0.0             # time on the dominant activity itself
    fullscreen_s: float = 0.0
    hops: Counter = field(default_factory=Counter)     # label -> present seconds away from the anchor
    titles: Counter = field(default_factory=Counter)   # the anchor's titles

    @property
    def seconds(self) -> float:
        return self.end - self.start


def activity_sessions(rows: Sequence[TimelineRow], since: float, until: float, config: NudgeConfig) -> list[Session]:
    """The user's sessions in ``[since, until)``, oldest first (plumbing on the timeline's own numbers).

    Runs are grouped by app. A run of another app shorter than
    ``session_excursion_min`` is a hop of the current session; a longer one starts a
    new session; a break (away or nothing recorded) of ``break_min`` ends one. A
    session squeezed between two sessions of the same app, shorter than twice the
    hop tolerance and than each of its neighbours, is folded back in as a hop (a
    ten-minute PR review in the middle of an afternoon in Warp).
    """
    hop = config.session_excursion_min * 60.0
    brk = config.break_min * 60.0
    clips = _clip([r for r in rows if not r.by_yuki], since, until)
    sessions: list[Session] = []
    cur: Session | None = None
    for run, _adjacent in _runs(clips, "app"):
        if cur is not None and run.start - cur.end >= brk:
            cur = None
        if cur is not None and run.key != cur.key and run.present_s < hop:
            cur.end = max(cur.end, run.end)
            cur.present_s += run.present_s
            cur.hops[run.label] += run.present_s
            continue
        if cur is None or run.key != cur.key:
            cur = Session(run.key, run.label, run.start, run.end)
            sessions.append(cur)
        cur.end = max(cur.end, run.end)
        cur.present_s += run.present_s
        cur.anchor_s += run.present_s
        cur.fullscreen_s += run.fullscreen_s
        cur.titles.update(run.titles)
    merged: list[Session] = []
    for s in sessions:
        if (len(merged) >= 2 and merged[-2].key == s.key and merged[-1].present_s < 2 * hop
                and merged[-1].present_s < min(merged[-2].anchor_s, s.anchor_s)
                and s.start - merged[-2].end < 2 * hop + brk):
            mid = merged.pop()
            keep = merged[-1]
            keep.hops[mid.label] += mid.anchor_s
            for label, secs in mid.hops.items():
                keep.hops[label] += secs
            keep.end, keep.present_s = s.end, keep.present_s + mid.present_s + s.present_s
            keep.anchor_s += s.anchor_s
            keep.fullscreen_s += mid.fullscreen_s + s.fullscreen_s
            keep.titles.update(s.titles)
            for label, secs in s.hops.items():
                keep.hops[label] += secs
            continue
        merged.append(s)
    return merged


def session_line(s: Session, safe: Callable[[Any, int], str], now: float) -> str:
    """``13:45-15:20 VALORANT: 1h35m in all, 1h28m in VALORANT itself (full screen 1h20m); hops: Discord 6m``."""
    going = " (still going)" if s.end >= now - ONGOING_S else ""
    hops = ", ".join(f"{safe(label, 40)} {format_duration(secs)}" for label, secs in s.hops.most_common(4) if secs >= 30)
    title = s.titles.most_common(1)[0][0] if s.titles else None
    parts = [f"{_hm(s.start)}-{_hm(s.end)}{going} {safe(s.label, 60)}: {format_duration(s.present_s)} in all, "
             f"{format_duration(s.anchor_s)} in {safe(s.label, 60)} itself"]
    if s.fullscreen_s >= 60:
        parts.append(f" (full screen {format_duration(s.fullscreen_s)})")
    if hops:
        parts.append(f"; hops: {hops}")
    if title:
        parts.append(f"; its window title most of the time \"{safe(title, 100)}\"")
    return "".join(parts)


def _jline(j: Any, safe: Callable[[Any, int], str], cap: int = 400, indent: str = "- ") -> str:
    """One journal fact with its id, for evidence: ``- 13:22 [J1234] [Chrome / github.com] ...``."""
    where = safe(j.app + (' / ' + j.host if j.host else ''), 80)
    mark = " (milestone: something finished or shipped)" if getattr(j, "milestone", False) else ""
    return f"{indent}{_hm(j.at)} [J{j.id}] [{where}] {safe(j.fact, cap)}{nature_mark(j.nature)}{mark}"


def _facts_at(store: Store, kinds: Sequence[str], at: float | None) -> list[Any]:
    """Conversation facts of these kinds in force at ``at`` (None: the active ones now), oldest first."""
    if at is None:
        return store.conversation_facts(kinds)
    return [f for f in store.conversation_facts(kinds, None)
            if f.valid_from <= at and (f.valid_to is None or f.valid_to > at)]


def coach_facts(store: Store, at: float | None = None) -> tuple[list[Any], list[tuple[Any, Any]]]:
    """``(rules, feedback)`` for the coach: the user's standing rules, and what they said back to cards.

    Feedback is every item of kind ``feedback`` and every preference that came from a
    reply to a card (:meth:`Store.fact_card`: the 2026-09-25 "stfu" to a wrong call-back
    was stored as a preference and read as a rule for two days). Each feedback item
    comes with the card it answers (or None: the coach's cards in general). A rule stays
    a rule wherever it was said.
    """
    rules: list[Any] = []
    feedback: list[tuple[Any, Any]] = []
    try:
        facts = _facts_at(store, ("rule", "preference", "feedback"), at)
    except Exception:
        return [], []
    for f in facts:
        if f.kind == "rule":
            rules.append(f)
            continue
        try:
            card = store.fact_card(f)
        except Exception:
            card = None
        if f.kind == "feedback" or card is not None:
            feedback.append((f, card))
        else:
            rules.append(f)
    return rules, feedback


def rules_text(store: Store, safe: Callable[[Any, int], str], at: float | None = None) -> str:
    """The user's current standing rules and preferences for Yuki, as the RULES block of a coach or reminder call.

    Read fresh from conversation memory on every call (a rule saved a minute
    ago applies to the next message). It sits *outside* the untrusted-data
    fence: these are the user's own instructions for how Yuki talks to them,
    and inside the fence they read as data to ignore (after the babe rule of
    2026-09-24 20:05, no coach message used it). Each is the user's own words
    (the quote), with the rule's text when it adds the situation it covers.
    What the user said back to a card is not here but in :func:`feedback_text`.
    ``at``: the rules in force then (a replay of an old check-in).
    """
    rules, _feedback = coach_facts(store, at)
    lines = ["RULES - the user's standing rules for how Yuki talks to them, in their own words. Every "
             "message you write follows every one of them, a two-word praise included:"]
    for r in rules:
        quote, text = safe(r.quote, 240), safe(r.text, 240)
        said = f"\"{quote}\"" if quote else text
        if quote and text and text.casefold().strip(" .\"") != quote.casefold().strip(" .\""):
            said += f" (meaning: {text})"
        lines.append(f"- {said} (since {datetime.fromtimestamp(r.valid_from):%Y-%m-%d})")
    if not rules:
        lines.append("(none)")
    return "\n".join(lines)


def feedback_text(store: Store, safe: Callable[[Any, int], str], at: float | None = None) -> str:
    """CARD FEEDBACK: what the user said back to Yuki's cards, each tied to the kind of card it answered."""
    _rules, feedback = coach_facts(store, at)
    lines = ["CARD FEEDBACK - what the user said back to your cards, in their own words. Each is about that "
             "kind of card only: a sharp reply to a call-back means fewer and surer call-backs, not silence, and "
             "it never turns off notices, reminders, check-ins or the welcome back. Only RULES can turn a kind of "
             "card off:"]
    for f, card in feedback:
        from yuki.memory.conversations import card_label

        when = datetime.fromtimestamp(f.valid_from).strftime("%Y-%m-%d %H:%M")
        about = (f"about your {card_label(card)} card \"{safe(card.text, 200)}\"" if card is not None
                 else "about your cards in general")
        words = f"\"{safe(f.quote, 240)}\"" if f.quote else safe(f.text, 240)
        meaning = (f" (read as: {safe(f.text, 240)})" if f.quote and f.text
                   and f.text.casefold().strip(" .\"") != (f.quote or "").casefold().strip(" .\"") else "")
        lines.append(f"- {when}, {about}: the user said {words}{meaning}")
    try:   # the cards' own buttons: "Less of these" and what came of each offer (yuki.memory.presence, offers)
        from yuki.memory.presence import card_feedback_lines

        buttons = card_feedback_lines(store, safe, at)
    except Exception:
        buttons = []
    lines += buttons
    if not feedback and not buttons:
        lines.append("(none)")
    return "\n".join(lines)


def _plain(value: Any, cap: int) -> str:
    """One line of the user's own words for a system layer (outside any data fence), capped."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def coach_system(prompt: str, identity: str, rules: str, feedback: str | None = None) -> list[dict[str, Any]]:
    """The layered system prompt of a coach, reminder or welcome-back call, most stable first.

    1. The frozen prompt (the same bytes on every call of its kind).
    2. WHO THE USER IS: their names (changes rarely).
    3. RULES, then CARD FEEDBACK: the user's standing rules and what they said back to
       cards, in their own words (read fresh on every call).

    Each is its own block with a cache breakpoint (three of the API's four; the message
    carries none), so a rule change invalidates only the last block. The fenced data stays
    in the user message. Haiku caches only prefixes of 4,096 tokens or more, so on these
    prompts the breakpoints are mostly no-ops today; the layout is what keeps the rules
    outside the data fence and last before the data.
    """
    last = rules if not feedback else f"{rules}\n\n{feedback}"
    blocks = [prompt, f"WHO THE USER IS: {identity}", last]
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}} for text in blocks]


# ---------------------------------------------------------------------------
# The user's presence as stretches (YouNow, check-ins)
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    """A stretch of the user's presence: present rows with less than a break (``break_min``) between them."""

    start: float
    end: float
    present_s: float = 0.0
    settled: bool = False


def presence_sessions(clips: Sequence[Any], config: NudgeConfig) -> list[_Session]:
    """The user's presence among clipped timeline rows as sessions, each marked settled or not (plumbing).

    Presence less than ``break_min`` apart is one session (a pause shorter than
    a break is not a break). A session is settled - the user really sat down -
    once it holds ``break_min`` of presence: a glance, a mouse bump or a burst
    of drift is shorter than a break, so it never ends one. Measured on the
    night of 2026-09-24/25: one-minute presence blips every 5-20 minutes from
    05:07 to 08:43 (up to 3 min of presence each); the return at 08:56 held
    8.7 min within ten minutes.
    """
    join = config.break_min * 60.0
    sessions: list[_Session] = []
    for c in clips:
        if c.row.state != "present" or c.row.by_yuki:
            continue
        present = c.present_s
        if present <= 0:
            continue
        if sessions and c.start - sessions[-1].end < join:
            sessions[-1].end = max(sessions[-1].end, c.end)
            sessions[-1].present_s += present
        else:
            sessions.append(_Session(c.start, c.end, present))
    for s in sessions:
        s.settled = s.present_s >= join
    return sessions


def _id_list(values: Any) -> list[Any]:
    """An id list as the model gave it. Sonnet is sent without strict (Bedrock refuses it), and once wrote the
    list as one string (``'J1630", "J1631", "W7'``): a string is read as the JSON list it was meant to be."""
    if not isinstance(values, str):
        return list(values or [])
    text = values.strip()
    for candidate in (text, f'["{text}"]', f"[{text}]"):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return parsed
    return [text] if text else []


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class NudgeWorker:
    """The coach in ``yuki-memory``. :meth:`tick` is one pass; :meth:`run` loops until stopped."""

    def __init__(
        self,
        store: Store,
        *,
        db_path: str | Path | None = None,
        privacy: Any = None,
        config: NudgeConfig | None = None,
        settings: Settings | None = None,
        client: Any = None,
        log_dir: Path | None = None,
        model: str = HAIKU_MODEL,
        writer_model: str = SONNET_MODEL,
        locked_fn: Callable[[], bool] | None = None,
        live_fn: Callable[[], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
        event_name: str = NUDGE_EVENT_NAME,
        log: Any = None,
        embedder: Any = None,
        you_fn: Callable[[], Any] | None = None,
    ) -> None:
        from yuki.memory.checkins import CheckinWorker
        from yuki.memory.presence import PresenceWorker
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.db_path = Path(db_path) if db_path is not None else store.path
        self._fixed_config = config
        self._section = PrivacySection(privacy, "nudges", NudgeConfig.from_dict) if config is None else None
        self.settings = settings or Settings()
        self._client = client
        self.log_dir = Path(log_dir) if log_dir else self.settings.project_root / "logs" / "memory"
        #: Decides whether to speak (cheap, every look); ``writer_model`` writes every card the user sees.
        self.model = model
        self.writer_model = writer_model
        self.locked_fn = locked_fn
        self.live_fn = live_fn
        self._privacy = privacy
        self.clock = clock
        self._event = _NudgeEvent(event_name)
        self._service_log = log
        self._stop = threading.Event()
        self._log_lock = threading.Lock()
        self._started_at = float(clock())
        #: The personal events scan (yuki.memory.presence), scheduled by the judge.
        self.presence = PresenceWorker(self)
        #: The shared embedder (the check-in vetting ranks memory near a question; None: the core profile only).
        self.embedder = embedder
        #: Yuki's check-ins about the user's goals and their day (yuki.memory.checkins).
        self.checkins = CheckinWorker(self)
        from yuki.memory.routine_offers import RoutineLane

        #: The "Your usual" routine card (yuki.memory.routine_offers): code only, no model.
        self.routines = RoutineLane(self)
        #: jCore (state-design.md §4): the one card judge, reading the you-right-now record (``you_fn``: the service's
        #: YouBuilder, read late), the only card writer since 18 Oct (``[judge] mode = "on"``, the default; ``"shadow"``
        #: logs what it would show of Yuki's own cards, ``"off"`` makes none; both still deliver the user's own reminders
        #: and standing requests and run the upkeep). None when it could not be built: then no card at all.
        self.judge = None
        try:
            from yuki.memory.cardjudge import CardJudge

            self.judge = CardJudge.for_worker(self, you_fn=you_fn)
        except Exception as exc:
            self._log("nudge_error", where="judge_start", error=f"{type(exc).__name__}: {exc}")

    # -- dependencies ------------------------------------------------------------

    def config(self) -> NudgeConfig:
        if self._fixed_config is not None:
            return self._fixed_config
        return self._section.get()

    @property
    def client(self) -> Any:
        if self._client is None:
            from yuki.agent.failover import make_client   # backup model / region when Bedrock errors

            self._client = make_client(self.settings, log=self._log)
        return self._client

    def _log(self, type: str, **fields: Any) -> None:
        record = {"ts": time.time(), "type": type, **{k: _as_plain(v) for k, v in fields.items()}}
        path = self.log_dir / f"nudges-{datetime.now():%Y%m%d}.jsonl"
        try:
            with self._log_lock:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, default=_as_plain) + "\n")
        except OSError:
            pass
        if self._service_log is not None and type in ("nudge_checkin", "nudge_gate", "nudge_error", "worker_error",
                                                      "routine_offer", "routine_held", "tab_census"):
            try:
                self._service_log(type, **{k: v for k, v in record.items() if not k.endswith("_ciphertext")
                                           and k not in ("ts", "type", "traceback")})
            except Exception:
                pass

    def _seal(self, value: Any) -> str:
        return self.store.cipher.encrypt(json.dumps(_as_plain(value), ensure_ascii=False, default=str))

    def _system(self, prompt: str, at: float | None = None) -> list[dict[str, Any]]:
        """A coach call's system prompt as cached layers: the frozen prompt, who the user is, RULES + CARD FEEDBACK."""
        return coach_system(prompt, self._identity(), rules_text(self.store, _plain, at),
                            feedback_text(self.store, _plain, at))

    def _identity(self) -> str:
        names = list(self.settings.user_names)
        try:
            names += [n for n in self.store.me_names() if n not in names]
        except Exception:
            pass
        return user_identity(names)

    def _tool_call(self, model: str, system: Any, user: str, tool: dict[str, Any], max_tokens: int,
                   checkin: NudgeCheckin) -> tuple[dict[str, Any], Any, dict[str, Any]]:
        """One forced tool call on ``model``; adds its usage and cost to ``checkin``. (input, response, request).

        Bedrock refuses ``strict`` for Sonnet 5 (review.py, 2026-09-24), so it is sent
        only to Haiku; every field is checked in code either way.
        """
        if model != HAIKU_MODEL:
            tool = {k: v for k, v in tool.items() if k != "strict"}
        request = {"model": model, "max_tokens": max_tokens, "system": system, "tools": [tool],
                   "tool_choice": {"type": "tool", "name": tool["name"]},
                   "messages": [{"role": "user", "content": user}]}
        t0 = time.perf_counter()
        response = self.client.messages.create(**request)
        checkin.latency_ms += (time.perf_counter() - t0) * 1000
        tokens = usage_tokens(response.usage)
        checkin.input_tokens += tokens["input_tokens"]
        checkin.output_tokens += tokens["output_tokens"]
        checkin.cache_write_tokens += tokens["cache_write_tokens"]
        checkin.cache_read_tokens += tokens["cache_read_tokens"]
        checkin.cost_usd = (checkin.cost_usd or 0.0) + (self.settings.estimate_cost(model, tokens) or 0.0)
        checkin.stop_reason = response.stop_reason
        if response.stop_reason == "max_tokens":
            raise RuntimeError("response hit max_tokens; tool input may be incomplete")
        block = next((b for b in response.content if b.type == "tool_use" and b.name == tool["name"]), None)
        if block is None or not isinstance(block.input, dict):
            raise RuntimeError(f"no {tool['name']} call (stop_reason={response.stop_reason})")
        return dict(block.input), response, request

    # -- the events scan's pace (PresenceWorker._scan_events) ---------------------------

    def _rate_ok(self, now: float, config: NudgeConfig) -> bool:
        checkins = self.store.nudge_checkins(now - 3600.0)
        calls = [c for c in checkins if c["model"] and c["trigger"] not in ("reminder", "review", "confirm")
                 and c["at"] <= now]
        return len(calls) < config.max_calls_per_hour and (not calls or now - calls[-1]["at"] >= config.min_gap_s)

    # -- accounting ------------------------------------------------------------------

    @staticmethod
    def _usage(c: NudgeCheckin) -> dict[str, int]:
        return {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                "cache_write_tokens": c.cache_write_tokens, "cache_read_tokens": c.cache_read_tokens}

    def _log_checkin(self, checkin_id: int, c: NudgeCheckin, nudge_id: int | None) -> None:
        self._log("nudge_checkin", checkin_id=checkin_id, trigger=c.trigger, outcome=c.outcome, nudge_id=nudge_id,
                  model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
                  cost_usd=round(c.cost_usd, 6) if c.cost_usd is not None else None,
                  latency_ms=round(c.latency_ms, 1), error=c.error)

    # -- loop ------------------------------------------------------------------------

    def run(self, stop: threading.Event | None = None, pause_path: Path | None = None) -> None:
        """Until stopped: the one card judge (18 Oct: the old first-writer chain is gone), ``CardJudge.tick`` in every
        ``[judge] mode``: its upkeep (expiry, check-in answers into goals, questions, the routine's learning), then one
        evaluation when YouNow woke it or a deadline came. ``"on"`` (the default): every card. ``"shadow"`` / ``"off"``:
        only the user's own reminders and standing requests (shadow also logs what it would have shown). Waits on
        YouNow's wake event with a timeout of the judge's next deadline, at most a minute (a switch in Settings takes
        effect then). No judge: nothing to run."""
        stop = stop or self._stop
        self._stop = stop
        self._log("worker_start", model=self.model, writer_model=self.writer_model, event=self._event.name)
        while not stop.is_set():
            judge = self.judge
            if judge is None:
                stop.wait(60.0)
                continue
            failed = False
            try:
                judge.tick(float(self.clock()))
            except Exception as exc:
                failed = True
                self._log("worker_error", where="judge", error=f"{type(exc).__name__}: {exc}",
                          traceback=traceback.format_exc())
            if stop.is_set():
                break
            if failed:   # never a spin on a wake the failed pass could not clear
                stop.wait(15.0)
                continue
            try:
                judge.wait(judge.wait_s(float(self.clock())))
            except Exception:
                stop.wait(60.0)
        self._event.close()
        self._log("worker_stop")

    def stop(self) -> None:
        self._stop.set()
        if self.judge is not None:   # the live loop waits on YouNow's wake: wake it so it sees the stop
            try:
                self.judge.interrupt()
            except Exception:
                pass


__all__ = [
    "NUDGE_EVENT_NAME", "NudgeConfig", "NudgeWorker", "Item", "Session", "in_quiet_hours", "todo_items", "item_dict",
    "complete_item", "reaction_summary", "signal_nudge", "activity_sessions", "coach_facts", "rules_text",
    "feedback_text", "COACH_KINDS", "SONNET_MODEL", "PRESENCE_TRIGGERS", "CHECKIN_TRIGGERS",
]
