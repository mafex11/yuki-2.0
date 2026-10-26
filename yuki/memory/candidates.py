"""Card candidates: every card idea, made by code, for the one card judge (jCore, state-design.md §4.2-§4.3).

Each generator is a function ``(ctx) -> list[Candidate]`` that reads the YouNow snapshot (:mod:`yuki.memory.younow`) and
the store's existing readers (or read-only ``store._query`` on content-free columns); none makes a model call and none
writes a card. What each one says, if anything, is decided later by the judge (:mod:`yuki.memory.cardjudge`): the
attention policy (:mod:`yuki.memory.attend`), the moment test, the type's mode, the rank, and - only for the one chosen
candidate that has no code text - one writer call.

Generators keep their own seen-marks under one ``nudge_state`` key, ``judge_seen`` (:class:`Seen`), so in shadow they
never touch the old producers' marks (``checkin_seen``, ``presence_seen``, ``reminded:*``, ``presence_events.asked``,
``moment_seen``). In shadow they also never *read* those marks: what the judge counts is what it would do alone (the old
chain shows the cards meanwhile, and would otherwise hide the judge's every follow-up and reminder behind its own). With
``live`` (``[judge] mode = "on"``, jSwitch) the item's real state - a reminder already written or folded, an event already
asked about, an unshown standing-request row - is read as well.

Code text (``text``, ``digest_line``) may be a function that makes it: shadow never calls it, so it decrypts nothing it
does not need to decide.

**Types** (:data:`TYPES`; the ledger key is ``type``): ``reminder``, ``confirm``, ``asked`` (deadline and user-asked);
``checkin:day_start`` (the Today card's head), ``checkin:followup``, ``checkin:evening``, ``checkin:care``,
``checkin:new_thread``; ``session:break``, ``session:callback``; ``stuck``, ``recap``, ``followup``, ``plans``;
``thread``, ``question``, ``work``, ``review`` (lasting: the Today card's lines; phase 3, 3.4: today's dated items
lead it, :func:`dated_today`, as ``plans`` lines); the judge's own ``today`` and
``away``; ``routine`` (jSwitch: the "Your usual" card, :meth:`yuki.memory.routine_offers.RoutineLane.candidates`,
live only); and ``usual`` (18 Oct: media the user measurably puts on around this time of day, :func:`gen_usual`, with a
button that runs the skill). Not ported (question 2): ``notice`` (milestone:
"you just finished X") and ``recall`` (the screen meets something memory holds: screen trivia).

**What a call promise is about** (phase 3, 3.4): a plan the user said on a call is a card question (a confirm, a
plans card, a follow-up, a Today line) only once the commitments check found what it is about
(:func:`yuki.memory.commitments.about_known`): "You said you'd play tomorrow, still on?" with no idea what is never
shown.

**The check-in guard** (:func:`guard_ok`): a check-in rests on a goal or thread id, or a measured day anchor (``DAY`` /
``HOURS``) - never on screen content. ``checkin:followup``'s "a day they named passed" rule rests on the user's own
dated list item (``todo:`` / ``commit:``), which §4.3 keeps unchanged; the guard accepts that id too (deviation, listed).

Public API::

    TYPES: {type: TypeInfo};  BEST_CLASSES;  DIGEST_LINE_CHARS
    Candidate(...);  resolve(value) -> str | None
    Seen(store) .get / .looked / .mark / .flush          # nudge_state['judge_seen']
    Context(store, you, now, config, seen, *, live=False, memo=None, lane=None)
    generate(ctx, *, only=None) -> list[Candidate]       # every generator, each wrapped (an error logs, never raises)
    guard_ok(candidate) -> bool
    evidence_lines(store, candidate, now, safe) -> (lines, ids)   # the writer's evidence (live only)
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yuki.memory.checkins import (
    BURST_MIN_EVENTS,
    BURST_WINDOW_S,
    DAY_START_SETTLE_S,
    DAY_START_WITHIN_S,
    DEADLINE_AFTER_S,
    DEADLINE_WITHIN_S,
    EVENING_MIN_PRESENT_S,
    FOLLOWUP_AGAIN_S,
    LANDED_WITHIN_S,
    NEW_THREAD_MIN_EVENTS,
    NEW_THREAD_MIN_SPAN_S,
    NEW_THREAD_WITHIN_S,
    QUIET_MAX_S,
    QUIET_MIN_S,
    THREAD_MIN_EVENTS,
    DayTotals,
    care_signal,
    day_boundary,
    evening_window,
    typical_end,
    user_day,
)
from yuki.memory.checkins import FRAMES as CHECKIN_FRAMES
from yuki.memory.checkins import LOOK_CAPS as CHECKIN_LOOK_CAPS
from yuki.memory.personality import normalize_nature
from yuki.memory.presence import (
    ALL_DAY_ASK_AT,
    FOLLOWUP_AFTER_S,
    FOLLOWUP_WITHIN_S,
    PLANS_AGAIN_S,
    PLANS_AHEAD_S,
    PLANS_MIN_LEAD_S,
    PLANS_TODO_NEAR_S,
    RECAP_MIN_SPEECH_S,
    RECAP_ROW_MIN_SPEECH_S,
    RECAP_WITHIN_S,
    STALE_MAX_S,
    STALE_MIN_IMPORTANCE,
    STALE_MIN_S,
    STUCK_MIN_FACTS,
    STUCK_RELOOK_SHOWN_FACTS,
    STUCK_RELOOK_SHOWN_S,
    STUCK_WINDOW_S,
    THREAD_AGAIN_S,
    _call_sessions,
    fold_events,
    recurrence,
)
from yuki.memory.presence import EVENTS_STATE as PRESENCE_EVENTS_STATE
from yuki.memory.presence import LOOK_CAPS as PRESENCE_LOOK_CAPS
from yuki.memory.younow import presence_now

#: The judge's seen-marks (``{key: [at, n]}``, content-free ids), kept this long.
SEEN_STATE = "judge_seen"
SEEN_KEEP_S = 30 * 86400.0
#: A Today line is code text of at most this many characters.
DIGEST_LINE_CHARS = 120
#: Today's dated items (phase 3, 3.4): at most this many lead the Today card.
DATED_MAX = 3
#: A card's code text is clipped to this.
TEXT_CHARS = 300
#: Timing classes a type in mode ``best`` may show at.
BEST_CLASSES: tuple[str, ...] = ("sit_down", "after_commit", "focus_end")
#: A standing-request row is picked up this long after journal wrote it.
ASKED_WITHIN_S = 6 * 3600.0
#: A recap is a card while its call ended less than this long ago; a Today line after.
RECAP_FRESH_S = 2 * 3600.0
#: A stuck candidate stays this long after the recurrence's newest fact (time-bound).
STUCK_TTL_S = 30 * 60.0
#: The weekly review's teaser is a Today line this long after the review was written (review.py deliver_within_h).
REVIEW_FRESH_S = 72 * 3600.0
#: Questions: the old chain may have asked one in the last this long; shadow still counts it as the judge's own.
QUESTION_SHADOW_ASKED_S = 86400.0


@dataclass(frozen=True)
class TypeInfo:
    """One ledger type: where its items live, whether they go stale before the next Today card, the nudge kind its card
    is written as, its time frame (check-ins: at most one per frame a day) and its writer-call cap a day."""

    home: str                      # card | digest | today_head
    time_bound: bool
    kind: str
    urgency: str = "any"           # deadline | asked | any
    frame: str | None = None
    look_cap: int | None = None


TYPES: dict[str, TypeInfo] = {
    "reminder": TypeInfo("card", False, "reminder", urgency="deadline"),
    "confirm": TypeInfo("card", True, "reminder"),
    "asked": TypeInfo("card", False, "reminder", urgency="asked"),
    "routine": TypeInfo("card", True, "notice", urgency="deadline"),   # jSwitch: RoutineLane.candidates()
    "usual": TypeInfo("card", True, "notice"),                          # 18 Oct: gen_usual (media at the usual time)
    "checkin:day_start": TypeInfo("today_head", True, "question", frame=CHECKIN_FRAMES["day_start"],
                                  look_cap=CHECKIN_LOOK_CAPS["day_start"]),
    "checkin:followup": TypeInfo("card", False, "question", frame=CHECKIN_FRAMES["followup"],
                                 look_cap=CHECKIN_LOOK_CAPS["followup"]),
    "checkin:evening": TypeInfo("card", True, "question", frame=CHECKIN_FRAMES["evening"],
                                look_cap=CHECKIN_LOOK_CAPS["evening"]),
    "checkin:care": TypeInfo("card", False, "question", frame=CHECKIN_FRAMES["care"],
                             look_cap=CHECKIN_LOOK_CAPS["care"]),
    "checkin:new_thread": TypeInfo("digest", False, "question", frame=CHECKIN_FRAMES["new_thread"],
                                   look_cap=CHECKIN_LOOK_CAPS["new_thread"]),
    "session:break": TypeInfo("card", True, "nudge"),
    "session:callback": TypeInfo("card", True, "nudge"),
    "stuck": TypeInfo("card", True, "notice", look_cap=PRESENCE_LOOK_CAPS["stuck"]),
    "recap": TypeInfo("card", False, "notice", look_cap=PRESENCE_LOOK_CAPS["recap"]),
    "followup": TypeInfo("card", False, "notice", look_cap=PRESENCE_LOOK_CAPS["followup"]),
    "plans": TypeInfo("card", False, "notice", look_cap=PRESENCE_LOOK_CAPS["plans"]),
    "thread": TypeInfo("digest", False, "notice"),
    "question": TypeInfo("digest", False, "question"),
    "work": TypeInfo("digest", False, "notice"),
    "review": TypeInfo("digest", False, "review"),
    "today": TypeInfo("card", False, "notice"),
    "away": TypeInfo("card", False, "notice"),
}


@dataclass
class Candidate:
    """One card idea (§4.2). ``type`` is the ledger key; ``key`` dedupes (and is the seen-mark). Everything but
    ``text`` / ``digest_line`` is content-free (numbers, enums, ids) and may be logged."""

    type: str
    key: str
    created_at: float
    expires_at: float | None = None
    urgency: str = "any"
    home: str = "card"
    time_bound: bool = False
    window: tuple[float, float] | None = None
    confidence: float = 0.5
    refs: dict[str, Any] = field(default_factory=dict)        # goal, thread, item, call, question, event, threads
    anchors: dict[str, float] = field(default_factory=dict)   # DAY | HOURS: the measured instant it rests on
    evidence: dict[str, float] = field(default_factory=dict)  # evidence id -> when
    text: Any = None              # code text, or a function that makes it (RAM only, never logged)
    digest_line: Any = None       # <= DIGEST_LINE_CHARS, code text or a function; None for time-bound types
    asks: str | None = None       # "Q<id>" (a stored question) | "checkin" (its question row is made when written)
    needs_writer: bool = False
    offer_channel: str | None = None
    offer: Any = None             # a code card's own typed offer (dict, or a function made after ``text``): "usual"
    nudge_id: int | None = None   # a row another writer already wrote (standing requests)
    why: str = ""                 # content-free reason code + counts
    seen_within: float = SEEN_KEEP_S
    seen_keys: tuple[str, ...] = ()   # more marks to set once it is shown or queued (content-free ids)
    seen_n: int = 0                   # the number kept with the mark (stuck: the newest fact id)
    lead: bool = False                # phase 3 (3.4): a Today line that leads the card (today's dated item)

    @property
    def info(self) -> TypeInfo:
        return TYPES[self.type]

    def fields(self) -> dict[str, Any]:
        """The content-free fields for a log line."""
        return {"judge_type": self.type, "key": self.key, "urgency": self.urgency, "home": self.home,
                "time_bound": self.time_bound, "confidence": round(self.confidence, 2),
                "needs_writer": self.needs_writer, "asks": bool(self.asks), "why": self.why}


def resolve(value: Any) -> str | None:
    """A candidate's code text: the string, or what its function makes (None on any error)."""
    try:
        value = value() if callable(value) else value
    except Exception:  # noqa: BLE001
        return None
    text = " ".join(str(value or "").split())
    return text or None


def clip_words(value: Any, cap: int) -> str | None:
    """White space squashed; when longer than ``cap``, cut after the last whole word (one past half the cap), the
    separators left at the end dropped, then "…" (tc1, 26 Oct: "... on Google Meet - pres… at 10:30")."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= cap:
        return text
    cut = text[: cap - 1]
    space = cut.rfind(" ")
    if space > cap // 2:
        cut = cut[:space]
    return cut.rstrip(" -–—,;:/(") + "…"


_clip = clip_words


def _when(at: float, now: float, all_day: bool = False) -> str:
    d = datetime.fromtimestamp(at)
    days = (d.date() - datetime.fromtimestamp(now).date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else "yesterday" if days == -1 else d.strftime("%a %d %b")
    return day if all_day else f"{day} {d:%H:%M}"


# ---------------------------------------------------------------------------
# Seen-marks and the generators' context
# ---------------------------------------------------------------------------


class Seen:
    """The judge's seen-marks, ``nudge_state['judge_seen']`` (``{key: [at, n]}``, content-free ids, kept
    :data:`SEEN_KEEP_S`). Loaded once, kept in RAM (only the judge writes it), written back by :meth:`flush` only when
    a mark was set."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._data: dict[str, list[float]] | None = None
        self._dirty = False

    def _load(self) -> dict[str, list[float]]:
        if self._data is None:
            try:
                raw = json.loads(self.store.nudge_state(SEEN_STATE) or "{}")
                self._data = {str(k): list(v) for k, v in raw.items() if isinstance(v, list) and v} \
                    if isinstance(raw, dict) else {}
            except (TypeError, ValueError):
                self._data = {}
            except Exception:  # noqa: BLE001 - unreadable: nothing seen (a duplicate "would show" at worst)
                self._data = {}
        return self._data

    def get(self, key: str) -> list[float] | None:
        return self._load().get(key)

    def looked(self, key: str, now: float, within_s: float = SEEN_KEEP_S) -> bool:
        v = self.get(key)
        return bool(v) and now - float(v[0]) < within_s

    def mark(self, key: str, now: float, n: int = 0) -> None:
        self._load()[str(key)] = [round(float(now), 1), int(n)]
        self._dirty = True

    def flush(self, now: float) -> None:
        if not self._dirty:
            return
        data = {k: v for k, v in self._load().items() if now - float(v[0]) < SEEN_KEEP_S}
        self._data = data
        try:
            self.store.set_nudge_state(SEEN_STATE, json.dumps(data, separators=(",", ":")))
            self._dirty = False
        except Exception:  # noqa: BLE001 - written at the next flush
            pass


class Memo:
    """Per-generator results reused while their change signature holds (or for their minimum interval)."""

    def __init__(self) -> None:
        self._v: dict[str, tuple[Any, float, Any]] = {}

    def get(self, name: str, signature: Any, now: float, every_s: float, fn: Callable[[], Any]) -> Any:
        """``fn()``'s result, reused while ``signature`` is the same - for at most ``every_s`` (``every_s <= 0``: as
        long as the signature holds)."""
        hit = self._v.get(name)
        if hit is not None and hit[0] == signature and (every_s <= 0 or 0 <= now - hit[1] < every_s):
            return hit[2]
        value = fn()
        self._v[name] = (signature, now, value)
        return value


class Context:
    """What the generators share for one evaluation: the store, the snapshot, now, ``[nudges]``, the seen-marks, and
    lazily the list items, the goals and the personal events (each read once). ``deadlines`` collects the instants a
    generator wants to be looked at again (an item becoming due, a window opening)."""

    def __init__(self, store: Any, you: Any, now: float, config: Any, seen: Seen, *, live: bool = False,
                 memo: Memo | None = None, lane: Any = None, worker: Any = None) -> None:
        self.store = store
        #: The card worker (its model client and log), live only: the "usual" card's one wording call.
        self.worker = worker
        #: The routine lane (:class:`yuki.memory.routine_offers.RoutineLane`), live only (jSwitch); None: no routine.
        self.lane = lane
        self.you = you
        self.now = float(now)
        self.config = config
        self.seen = seen
        self.live = bool(live)
        self.memo = memo or Memo()
        self.deadlines: list[float] = []
        self.errors: Counter = Counter()
        self._items: list[Any] | None = None
        self._goals: list[Any] | None = None
        self._events: list[dict[str, Any]] | None = None
        self._referents: dict[int, dict[str, Any]] | None = None
        #: Every ref (an item's, ``event:<id>`` for each stored row of an event) on today's dated lines, shown, seen
        #: or not: :func:`gen_plans` makes no plans card about them (one mention of one plan a day).
        self.dated_refs: set[str] = set()

    def deadline(self, at: float | None) -> None:
        if at is not None and float(at) > self.now:
            self.deadlines.append(float(at))

    def section(self, name: str) -> Any:
        return (self.you or {}).get(name) or {}

    @property
    def items(self) -> list[Any]:
        """The open list items (nudges.todo_items: to-dos, commitments, open loops)."""
        if self._items is None:
            from yuki.memory.nudges import todo_items

            self._items = [i for i in todo_items(self.store, now=self.now) if i.status == "open"]
        return self._items

    @property
    def goals(self) -> list[Any]:
        if self._goals is None:
            try:
                self._goals = list(self.store.goals(("open", "unknown")))
            except Exception:  # noqa: BLE001
                self._goals = []
        return self._goals

    @property
    def referents(self) -> dict[int, dict[str, Any]]:
        """What each call promise is about, as the commitments check judged it (read once; content-free)."""
        if self._referents is None:
            try:
                from yuki.memory.commitments import referent_states

                self._referents = referent_states(self.store)
            except Exception:  # noqa: BLE001 - an older build without it: nothing judged, no call promise asked
                self._referents = {}
        return self._referents

    def asks_ok(self, item: Any) -> bool:
        """Whether a list item may be a card question: anything but a call promise not known to be about something
        (:func:`yuki.memory.commitments.about_known`)."""
        try:
            from yuki.memory.commitments import about_known
        except Exception:  # noqa: BLE001
            return getattr(item, "source", None) != "call"
        return about_known(self.referents, getattr(item, "ref", None), getattr(item, "source", None))

    @property
    def events(self) -> list[dict[str, Any]]:
        """The user's dated personal events the events scan found (``presence_events``, encrypted state), one row per
        event (:func:`yuki.memory.presence.fold_events`: its stored rows' ids in ``ids``; a time only when it is
        sure, else ``all_day``)."""
        if self._events is None:
            try:
                raw = self.store.nudge_state(PRESENCE_EVENTS_STATE)
                data = json.loads(self.store.cipher.decrypt(raw)) if raw else []
                self._events = fold_events(data if isinstance(data, list) else [])
            except Exception:  # noqa: BLE001
                self._events = []
        return self._events


@dataclass(frozen=True)
class _Fact:
    """One journal fact's content-free columns (what recurrence and the guards need)."""

    id: int
    at: float
    work_thread_id: int | None


_FACT_SQL = ("SELECT id, at, work_thread_id, nature FROM journal WHERE superseded_at IS NULL AND at >= ? AND at < ?"
             " AND by_yuki = 0 AND tainted = 0 AND app != ? AND coalesce(attention, 'front') = 'front'"
             " AND created_at <= ? ORDER BY at, id")


def _user_facts(store: Any, since: float, now: float) -> list[_Fact]:
    """The user's own usable journal facts dated in ``[since, now)`` that memory had written by ``now`` - not Yuki's,
    not uncertain or humour, not another AI tool's text, not said to Yuki, from the window in front (presence's
    ``_known_facts`` rule, :1795), read from content-free columns only."""
    from yuki.memory.store import Store

    rows = store._query(_FACT_SQL, (since, now, Store.CONVERSATION_APP, now))
    return [_Fact(int(r[0]), float(r[1]), r[2]) for r in rows
            if normalize_nature(r[3]) not in ("uncertain", "humor")]


def _journal_meta(store: Any, ids: Sequence[int]) -> list[_Fact]:
    """Content-free columns of these journal ids, the user's own usable ones only."""
    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return []
    rows = store._query("SELECT id, at, work_thread_id, nature FROM journal WHERE superseded_at IS NULL AND by_yuki = 0"
                        " AND tainted = 0 AND id IN (%s)" % ",".join("?" * len(wanted)), wanted)
    return [_Fact(int(r[0]), float(r[1]), r[2]) for r in rows if normalize_nature(r[3]) != "uncertain"]


# ---------------------------------------------------------------------------
# Deadline and user-asked
# ---------------------------------------------------------------------------


def remind_at(item: Any, config: Any) -> float:
    """When an item is reminded: its due time, or ``all_day_at`` on its day (``NudgeWorker._remind_at``, :2774)."""
    from yuki.memory.nudges import _minutes_of_day

    if not item.all_day:
        return float(item.due_at)
    day = datetime.fromtimestamp(item.due_at)
    return day.replace(hour=0, minute=0, second=0).timestamp() + _minutes_of_day(config.all_day_at) * 60.0


def gen_reminders(ctx: Context) -> list[Candidate]:
    """``reminder`` and ``confirm`` - ported from ``NudgeWorker._due_items`` / ``_send_due`` (nudges.py:2780-2811) and
    ``_write_reminder``'s own fallback words (:2859-2860).

    * ``reminder``: a to-do or Yuki's commitment at its due time (an all-day one at ``all_day_at``), late by at most
      ``reminder_late_max_h``; urgency ``deadline`` (B2). Code text from the item's short ``title``; an item with no
      title goes through the writer, which may say none (``suppressed:vague``, folded), as ``_write_reminder`` does.
    * ``confirm``: a plan the user said (on a call or told Yuki) with a day, while it is at least ``confirm_lead_min``
      ahead: Yuki asks once whether they want a reminder. Code template plus the title; no title: the writer.
      Time-bound (it expires ``confirm_lead_min`` before the plan).

    Differences: once per item and due time by the judge's own mark (``reminder:<ref>:<due>``); ``store.reminded`` is
    read only live (in shadow the old chain's marks would hide the judge's own reminder). A reminder that is not due
    yet asks for a deadline (its due time wakes the judge). The old ``_send_due`` order (an exact-time reminder at once,
    a confirm only when cards are spaced) is the policy's: B2 for the reminder, B1 for the confirm."""
    out: list[Candidate] = []
    cfg = ctx.config
    now = ctx.now
    late = float(cfg.reminder_late_max_h) * 3600.0
    lead = float(cfg.confirm_lead_min) * 60.0
    today = user_day(now, cfg.resume_day_start)   # 3.4: a card about an item shown today is its Today line's mention
    for item in ctx.items:
        if item.due_at is None or item.source == "loop":
            continue
        at = remind_at(item, cfg)
        if now - at > late:
            continue
        title = _clip(item.title, TEXT_CHARS)
        when = _when(at, now, bool(item.all_day))
        if item.source in ("call", "chat"):   # the user's own plan: Yuki asks before it reminds
            if at - now < lead or not ctx.asks_ok(item):   # 3.4: a call promise once known what it is about
                continue
            key = f"confirm:{item.ref}:{int(item.due_at)}"
            if ctx.seen.looked(key, now) or (ctx.live and ctx.store.reminded(item.ref, item.due_at)):
                continue
            said = "You said on a call" if item.source == "call" else "You told me"
            out.append(Candidate(
                "confirm", key, created_at=float(item.since or now), expires_at=at - lead, home="card", time_bound=True,
                confidence=0.8, refs={"item": item.ref, "due_at": float(item.due_at), "source": item.source},
                evidence={item.ref: float(item.since or now)},
                text=(f"{said}: {title} ({when}). Want a reminder?" if title else None), needs_writer=not title,
                offer_channel="reminder", seen_keys=(f"dated:{item.ref}:{today}",),
                why=f"confirm lead_h={(at - now) / 3600.0:.1f} titled={int(bool(title))}"))
        elif at <= now:
            key = f"reminder:{item.ref}:{int(item.due_at)}"
            if ctx.seen.looked(key, now) or (ctx.live and ctx.store.reminded(item.ref, item.due_at)):
                continue
            hm = "today" if item.all_day else datetime.fromtimestamp(item.due_at).strftime("%H:%M")
            out.append(Candidate(
                "reminder", key, created_at=at, expires_at=at + late, urgency="deadline", home="card",
                window=(at, at + late), confidence=1.0,
                refs={"item": item.ref, "due_at": float(item.due_at), "source": item.source,
                      "all_day": bool(item.all_day)},
                evidence={item.ref: float(item.since or at)},
                text=(f"Reminder ({hm}): {title}" if title else None), needs_writer=not title,
                offer_channel="reminder", seen_keys=(f"dated:{item.ref}:{today}",),
                why=f"reminder late_min={(now - at) / 60.0:.0f} titled={int(bool(title))}"))
        else:
            ctx.deadline(at)
    return out


def gen_routine(ctx: Context) -> list[Candidate]:
    """``routine`` (jSwitch) - the "Your usual" card, from :meth:`yuki.memory.routine_offers.RoutineLane.candidates`
    (``RoutineLane.due`` :663: an active routine in its window, its end state not reached today, not offered today, at
    most ``routine_per_day`` routine cards; an ``offer-now`` request first). Urgency ``deadline`` (B2: it skips B1 and
    keeps its own ``routine_per_day``), time-bound (its window closes). Code text: the lane's fixed template and the
    learned name (``card_text``); the offer, its why line and the capability's Won't line are built by the lane when it
    writes (``RoutineLane.write``).

    Live only: in shadow the lane's own pass shows the routine card (``RoutineLane.tick``), and its offer-now request
    file is the lane's to read. The policy replaces the lane's own holds (``RoutineLane.holds`` / ``_attention``):
    a meeting in front passes this type, meeting audio and a call fail closed, settling needs only
    ``routine_settle_s``, and a card showing holds it (it never jumps one)."""
    if not ctx.live or ctx.lane is None:
        return []
    return list(ctx.lane.candidates(ctx.now, ctx.config, deadline=ctx.deadline))


def gen_asked(ctx: Context) -> list[Candidate]:
    """``asked`` - the "tell me when" card journal already wrote through ``fire_standing_request``
    (journal.py:2077-2091; kind ``reminder``, trigger ``request``, ref ``request:<id>``), with journal's own cooldown
    and daily budget unchanged. The pile picks the row up as a pre-written candidate (its own text, never read here);
    the policy decides when it may show (skips B1, obeys H0-H8, H7 per Q5). Live: only rows not yet shown."""
    now = ctx.now
    rows = ctx.store._query("SELECT id, at, shown_at, reaction FROM nudges WHERE kind = 'reminder' AND trigger = 'request'"
                            " AND at >= ? AND at < ? ORDER BY at, id", (now - ASKED_WITHIN_S, now + 1.0))
    out = []
    for r in rows:
        if ctx.live and (r[2] is not None or r[3] is not None):
            continue
        key = f"asked:N{int(r[0])}"
        if ctx.seen.looked(key, now):
            continue
        out.append(Candidate("asked", key, created_at=float(r[1]), expires_at=float(r[1]) + ASKED_WITHIN_S,
                             urgency="asked", confidence=1.0, nudge_id=int(r[0]), refs={"nudge": int(r[0])},
                             why="asked row"))
    return out


# ---------------------------------------------------------------------------
# Check-ins (CheckinWorker's five detectors, checkins.py:1126-1445)
# ---------------------------------------------------------------------------


def _day_totals(ctx: Context) -> tuple[list[DayTotals], DayTotals, str]:
    """The measured days from YouNow (``presence.days``: the last 7, plus today's time at the PC as read now)."""
    pres = ctx.section("presence")
    day_start = ctx.config.resume_day_start
    today = user_day(ctx.now, day_start)
    out = [DayTotals(str(d.get("day")), present_s=float(d.get("at_pc_s") or 0.0), last=d.get("end"))
           for d in (pres.get("days") or []) if d.get("day") and d.get("day") != today]
    t_today = DayTotals(today, present_s=float(presence_now(ctx.you, ctx.now)["today_s"] or 0.0), last=ctx.now)
    return out + [t_today], t_today, today


def _no_checkins(store: Any, g: Any) -> bool:
    try:
        return any(e["type"] == "never" for e in store.goal_events(g.id))
    except Exception:  # noqa: BLE001
        return False


def _about_recent(ctx: Context, *, goal_id: int | None = None, thread_id: int | None = None) -> bool:
    """A check-in about this goal or thread within ``FOLLOWUP_AGAIN_S`` (the judge's own marks; ``_recent_checkin_about``,
    checkins.py:1110, read the cards' encrypted inputs)."""
    return bool((goal_id is not None and ctx.seen.looked(f"about:G{goal_id}", ctx.now, FOLLOWUP_AGAIN_S))
                or (thread_id is not None and ctx.seen.looked(f"about:W{thread_id}", ctx.now, FOLLOWUP_AGAIN_S)))


def _about_keys(goal_id: int | None, thread_id: int | None) -> tuple[str, ...]:
    return tuple(k for k in ((f"about:G{goal_id}" if goal_id else None), (f"about:W{thread_id}" if thread_id else None))
                 if k)


def gen_day_start(ctx: Context) -> list[Candidate]:
    """``checkin:day_start`` - ``CheckinWorker._day_start`` (checkins.py:1126-1162): the first settled stretch after a
    break of at least ``DAY_BREAK_S`` (their night), once they have been back ``DAY_START_SETTLE_S`` and within
    ``DAY_START_WITHIN_S``. It is the **head line of the Today card** (one card, not two; §4.7); the writer asks it.

    Differences: ``day_began`` comes from YouNow (the same pure rule, ``younow.day_began``); "back 10 min" is the wall
    time since the day began with the stretch settled (YouNow's ``settled_at``), where the old code summed presence
    since then; no 20-minute wait after a welcome back (the Today card *is* the welcome back)."""
    pres = ctx.section("presence")
    began, gap = pres.get("day_began"), pres.get("day_gap_s")
    now = ctx.now
    if began is None:
        return []
    began = float(began)
    if now - began < DAY_START_SETTLE_S:
        ctx.deadline(began + DAY_START_SETTLE_S)
        return []
    if now - began > DAY_START_WITHIN_S or not presence_now(ctx.you, now)["settled"]:
        return []
    key = f"checkin:day_start:{int(began)}"
    if ctx.seen.looked(key, now):
        return []
    return [Candidate("checkin:day_start", key, created_at=began + DAY_START_SETTLE_S,
                      expires_at=began + DAY_START_WITHIN_S, home="today_head", time_bound=True, confidence=0.8,
                      anchors={"DAY": began}, evidence={"DAY": began}, asks="checkin", needs_writer=True,
                      why=f"day_start gap_h={float(gap or 0.0) / 3600.0:.1f}")]


def gen_followup_checkin(ctx: Context) -> list[Candidate]:
    """``checkin:followup`` (past) - ``CheckinWorker._followup`` (checkins.py:1191-1312), its four rules unchanged and in
    its order: (0) a goal's next check-in came; (1) something landed (a milestone on a goal's thread, or on an active
    thread with open items and ``THREAD_MIN_EVENTS`` touches) within ``LANDED_WITHIN_S``; (2) a day they named passed (a
    goal's due day, or a dated list item of theirs still open), by ``DEADLINE_AFTER_S`` and at most
    ``DEADLINE_WITHIN_S``; (3) a burst of ``BURST_MIN_EVENTS`` touches within ``BURST_WINDOW_S``, then quiet
    ``QUIET_MIN_S``-``QUIET_MAX_S``. The first open one (rule, then newest) is the candidate.

    Differences: milestones are read from content-free journal columns (no decryption); "asked about it within
    ``FOLLOWUP_AGAIN_S``" is the judge's own ``about:`` mark; the per-moment spacing doubling (``checkin_scores``) is
    the ledger's mode now. Rule 2's list-item case rests on the item id (the guard accepts it)."""
    cands: list[tuple[int, float, Candidate]] = []
    store, now = ctx.store, ctx.now
    goals = ctx.goals
    by_thread = {g.work_thread_id: g for g in goals if g.work_thread_id}

    def cand(key: str, *, goal: Any = None, thread_id: int | None = None, because: str, evidence: dict[str, float],
             item: str | None = None, title: Any = None) -> Candidate:
        gid = goal.id if goal is not None else None
        tid = thread_id if thread_id is not None else (goal.work_thread_id if goal is not None else None)
        refs: dict[str, Any] = {k: v for k, v in (("goal", gid), ("thread", tid), ("item", item)) if v}
        words = title if title is not None else (lambda: _clip(goal.text, 90) if goal is not None else None)

        def line(words: Any = words) -> str | None:
            name = resolve(words)
            return _clip(f"{name}: did it land, or are you still on it?", DIGEST_LINE_CHARS) if name else None

        return Candidate("checkin:followup", key, created_at=now, home="card", confidence=0.8, refs=refs,
                         evidence=evidence, asks="checkin", needs_writer=True, digest_line=line,
                         seen_keys=_about_keys(gid, tid), why=f"followup because={because}")

    # 0. a goal whose next check-in came
    for g in goals:
        if g.next_checkin_at and g.next_checkin_at <= now and now - g.next_checkin_at <= DEADLINE_WITHIN_S:
            key = f"checkin:followup:G{g.id}:next:{int(g.next_checkin_at)}"
            if ctx.seen.looked(key, now) or _no_checkins(store, g) or _about_recent(ctx, goal_id=g.id):
                continue
            cands.append((0, float(g.next_checkin_at), cand(key, goal=g, because="next",
                                                            evidence={g.ref: float(g.updated_at)})))
        elif g.next_checkin_at and g.next_checkin_at > now:
            ctx.deadline(g.next_checkin_at)
    # 1. something landed
    try:
        fresh = store.milestones_since(0, now - LANDED_WITHIN_S, until=now)
        entries = [e for e in _journal_meta(store, [i for i, _at in fresh]) if e.work_thread_id is not None]
    except Exception:  # noqa: BLE001
        entries = []
    landed: dict[int, list[_Fact]] = {}
    for e in entries:
        tid = store.resolve_work_thread(e.work_thread_id)
        if tid is not None:
            landed.setdefault(tid, []).append(e)
    for tid, es in landed.items():
        t = store.work_thread(tid)
        if t is None or t.status not in ("active", "paused"):
            continue
        g = by_thread.get(tid)
        newest = max(e.at for e in es)
        if g is not None and (_no_checkins(store, g) or (g.last_checkin_at and newest <= g.last_checkin_at)):
            continue
        if g is None and not (t.open_items and (t.events or 0) >= THREAD_MIN_EVENTS):
            continue
        key = f"checkin:followup:W{tid}:landed:J{max(e.id for e in es)}"
        if ctx.seen.looked(key, now) or _about_recent(ctx, thread_id=tid):
            continue
        ev = {f"J{e.id}": e.at for e in es}
        ev[f"W{tid}"] = float(t.last_seen)
        if g is not None:
            ev[g.ref] = float(g.updated_at)
        cands.append((1, newest, cand(key, goal=g, thread_id=tid, because="landed", evidence=ev,
                                      title=(lambda t=t: _clip(t.title, 90)))))
    # 2. a day they named passed: a goal's due day, or a dated list item of theirs still open
    for g in goals:
        if g.due_at and DEADLINE_AFTER_S <= now - g.due_at <= DEADLINE_WITHIN_S:
            key = f"checkin:followup:G{g.id}:due:{int(g.due_at)}"
            if ctx.seen.looked(key, now) or _no_checkins(store, g) or _about_recent(ctx, goal_id=g.id):
                continue
            cands.append((2, float(g.due_at), cand(key, goal=g, because="due", evidence={g.ref: float(g.updated_at)})))
        elif g.due_at and now - g.due_at < DEADLINE_AFTER_S:
            ctx.deadline(g.due_at + DEADLINE_AFTER_S)
    for i in ctx.items:
        if i.owner != "user" or i.due_at is None or not ctx.asks_ok(i):
            continue
        if DEADLINE_AFTER_S <= now - i.due_at <= DEADLINE_WITHIN_S:
            key = f"checkin:followup:{i.ref}:due"
            if ctx.seen.looked(key, now):
                continue
            cands.append((2, float(i.due_at), cand(key, because="due", item=i.ref,
                                                   evidence={i.ref: float(i.since or i.due_at)},
                                                   title=(lambda i=i: _clip(i.title or i.text, 90)))))
    # 3. a burst of work, then quiet
    try:
        threads = [t for t in store.work_threads(("active", "paused"), limit=60)
                   if QUIET_MIN_S <= now - t.last_seen <= QUIET_MAX_S and (t.open_items or t.id in by_thread)]
        spans = store.work_event_spans([t.id for t in threads]) if threads else {}
    except Exception:  # noqa: BLE001
        threads, spans = [], {}
    for t in threads:
        touches = [at for at, _c in spans.get(t.id, []) if t.last_seen - BURST_WINDOW_S <= at <= t.last_seen]
        if len(touches) < BURST_MIN_EVENTS:
            continue
        g = by_thread.get(t.id)
        if g is not None and (_no_checkins(store, g) or (g.last_checkin_at and g.last_checkin_at >= t.last_seen)):
            continue
        key = f"checkin:followup:W{t.id}:quiet:{int(t.last_seen)}"
        if ctx.seen.looked(key, now) or _about_recent(ctx, thread_id=t.id):
            continue
        ev = {f"W{t.id}": float(t.last_seen)}
        if g is not None:
            ev[g.ref] = float(g.updated_at)
        cands.append((3, float(t.last_seen), cand(key, goal=g, thread_id=t.id, because="quiet", evidence=ev,
                                                  title=(lambda t=t: _clip(t.title, 90)))))
    if not cands:
        return []
    cands.sort(key=lambda c: (c[0], -c[1]))
    return [cands[0][2]]


def gen_care(ctx: Context) -> list[Candidate]:
    """``checkin:care`` - ``CheckinWorker._care`` (checkins.py:1330-1348): measured long days (``care_signal``: a streak of
    long days, a very long day today, late nights), once today holds ``EVENING_MIN_PRESENT_S``; once a day. Anchor
    ``HOURS``. Its Today line is code: what the numbers say ("3 long days in a row").

    Differences: the days come from YouNow (``presence.days``, 7 kept, plus today read now), where the old code read 14
    days of rows; ``care_signal`` needs at most the last 4."""
    totals, t_today, today = _day_totals(ctx)
    if t_today.present_s < EVENING_MIN_PRESENT_S:
        return []
    signal = care_signal(totals, today, ctx.config.resume_day_start)
    if signal is None:
        return []
    key = f"checkin:care:{today}"
    if ctx.seen.looked(key, ctx.now):
        return []
    parts = []
    if signal.get("streak", 0) >= 3:
        parts.append(f"{signal['streak']} long days in a row")
    if signal.get("today_s", 0.0) >= 12 * 3600.0:
        parts.append(f"{signal['today_s'] / 3600.0:.0f}h at the PC today")
    if signal.get("late_nights", 0) >= 3:
        parts.append(f"{signal['late_nights']} late nights lately")
    line = _clip("; ".join(parts) + " - how are you holding up?", DIGEST_LINE_CHARS) if parts else None
    return [Candidate("checkin:care", key, created_at=ctx.now, home="card", confidence=0.6,
                      anchors={"HOURS": ctx.now}, evidence={"HOURS": ctx.now}, asks="checkin", needs_writer=True,
                      digest_line=line, why=f"care streak={signal.get('streak', 0)} late={signal.get('late_nights', 0)}")]


def gen_evening(ctx: Context) -> list[Candidate]:
    """``checkin:evening`` (future) - ``CheckinWorker._evening`` (checkins.py:1350-1398): the end of their day is near
    (``evening_window``: from 19:00 or their usual end less 4 h, to quiet hours or the usual end plus 1 h), with
    ``EVENING_MIN_PRESENT_S`` at the PC today; once a day. Anchor ``DAY``. Time-bound (the window closes).

    Differences: the usual end is the median of YouNow's 7 kept days (``typical_end`` needs 4), where the old code read
    14; the ``DAY`` anchor is when their day began (else the start of the user's day)."""
    totals, t_today, today = _day_totals(ctx)
    now = ctx.now
    day_start = ctx.config.resume_day_start
    opens, closes = evening_window(now, ctx.config, typical_end(totals, today, day_start))
    if now < opens:
        ctx.deadline(opens)
        return []
    if now >= closes or t_today.present_s < EVENING_MIN_PRESENT_S:
        return []
    key = f"checkin:evening:{today}"
    if ctx.seen.looked(key, now):
        return []
    began = ctx.section("presence").get("day_began")
    anchor = float(began) if began and float(began) >= day_boundary(today, day_start) else day_boundary(today, day_start)
    return [Candidate("checkin:evening", key, created_at=opens, expires_at=closes, home="card", time_bound=True,
                      confidence=0.7, anchors={"DAY": anchor}, evidence={"DAY": anchor}, asks="checkin",
                      needs_writer=True, why=f"evening today_h={t_today.present_s / 3600.0:.1f}")]


def gen_new_thread(ctx: Context) -> list[Candidate]:
    """``checkin:new_thread`` (present) - ``CheckinWorker._new_thread`` (checkins.py:1400-1445): a thread of work that
    started within ``NEW_THREAD_WITHIN_S`` and holds ``NEW_THREAD_MIN_EVENTS`` touches over ``NEW_THREAD_MIN_SPAN_S``, with
    no goal yet; the one with the most touches. Home: the Today card, as a code line from the thread's title."""
    store, now = ctx.store, ctx.now
    try:
        threads = [t for t in store.work_threads(("active",), since=now - NEW_THREAD_WITHIN_S, limit=40)
                   if 0 <= now - t.first_seen <= NEW_THREAD_WITHIN_S]
        spans = store.work_event_spans([t.id for t in threads]) if threads else {}
        with_goal = {g.work_thread_id for g in store.goals(None) if g.work_thread_id}
    except Exception:  # noqa: BLE001
        return []
    best = None
    for t in threads:
        if t.id in with_goal:
            continue
        touches = [at for at, _c in spans.get(t.id, []) if at <= now]
        if len(touches) < NEW_THREAD_MIN_EVENTS or max(touches) - min(touches) < NEW_THREAD_MIN_SPAN_S:
            continue
        key = f"checkin:new_thread:W{t.id}"
        if ctx.seen.looked(key, now):
            continue
        if best is None or len(touches) > len(best[1]):
            best = (t, touches, key)
    if best is None:
        return []
    t, touches, key = best
    return [Candidate("checkin:new_thread", key, created_at=now, home="digest", confidence=0.6,
                      refs={"thread": t.id}, evidence={f"W{t.id}": float(t.last_seen)}, asks="checkin",
                      digest_line=(lambda t=t: _clip(f"New: {_clip(t.title, 80)} - what are you going for with it?",
                                                     DIGEST_LINE_CHARS)),
                      seen_keys=_about_keys(None, t.id), why=f"new_thread touches={len(touches)}")]


def gen_checkins(ctx: Context) -> list[Candidate]:
    """The five check-in moments (``MOMENTS`` order). ``FRAMES`` (one per past / present / future frame a day) and the
    ``LOOK_CAPS`` writer caps are kept, in the judge (they count what it showed and asked)."""
    out: list[Candidate] = []
    for fn in (gen_day_start, gen_followup_checkin, gen_care, gen_evening, gen_new_thread):
        try:
            out += fn(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.errors[f"{fn.__name__}:{type(exc).__name__}"] += 1
    return out


# ---------------------------------------------------------------------------
# Moment cards
# ---------------------------------------------------------------------------


def _user_fact_count(store: Any, since: float, now: float) -> int:
    from yuki.memory.store import Store

    rows = store._query("SELECT nature FROM journal WHERE superseded_at IS NULL AND at >= ? AND at < ? AND by_yuki = 0"
                        " AND tainted = 0 AND app != ?", (since, now, Store.CONVERSATION_APP))
    return sum(1 for r in rows if normalize_nature(r[0]) != "uncertain")


def gen_session(ctx: Context) -> list[Candidate]:
    """``session:break`` and ``session:callback`` - the coach's two kinds of card, a new card (the coach persona is
    retired; nothing is seeded from its old looks).

    * ``session:break`` - ``break_due`` (nudges.py:1158) and the once-per-session rule of ``NudgeWorker._checkin``
      (:2912-2917): at least ``break_after_min`` (90) at the PC with no break of ``break_min``, once per activity session
      (YouNow's ``presence.session``), at least ``break_gap_min`` (60) after the last one. Time-bound: a break ends it
      (a new stretch is a new key). Personal, session-aware, no praise: the writer's.
    * ``session:callback`` - the ``drift`` trigger (:2923-2928) and ``decide``'s call-back rule (:3059-3062): a new
      activity session after another, held ``callback_after_min``, with journal facts of its own; once per session.

    Differences: the session and its start come from YouNow (``activity_sessions``, the same rule); "held 15 min" is
    wall time since the session began (the old code summed its presence); the coach's Haiku look and its periodic /
    calm / milestone triggers are gone - the moment test picks the pause."""
    out: list[Candidate] = []
    cfg, now = ctx.config, ctx.now
    pres = ctx.section("presence")
    p = presence_now(ctx.you, now)
    since = pres.get("at_pc_since")
    sess = pres.get("session") or {}
    if since is not None and not p["away"]:
        since = float(since)
        need = float(cfg.break_after_min) * 60.0
        if now - since < need:
            ctx.deadline(since + need)
        else:
            skey = int(float(sess.get("start") or since))
            key = f"session:break:{int(since)}:{skey}"
            if not ctx.seen.looked(key, now) and not ctx.seen.looked("session:break", now, cfg.break_gap_min * 60.0):
                out.append(Candidate("session:break", key, created_at=since + need, home="card", time_bound=True,
                                     confidence=0.6, anchors={"HOURS": since}, evidence={"BREAK": since},
                                     needs_writer=True, offer_channel="nudge", seen_keys=("session:break",),
                                     why=f"break at_pc_min={(now - since) / 60.0:.0f}"))
    prev = sess.get("prev") if sess else None
    if sess and prev and prev.get("key") != sess.get("key") and sess.get("start") is not None:
        start = float(sess["start"])
        need = float(cfg.callback_after_min) * 60.0
        if now - start < need:
            ctx.deadline(start + need)
        else:
            key = f"session:callback:{int(start)}"
            if not ctx.seen.looked(key, now):
                n = _user_fact_count(ctx.store, start, now)
                if n:
                    out.append(Candidate("session:callback", key, created_at=start + need, home="card",
                                         time_bound=True, confidence=0.4, evidence={"S": start}, needs_writer=True,
                                         offer_channel="nudge", why=f"callback facts={n}"))
    return out


def gen_stuck(ctx: Context) -> list[Candidate]:
    """``stuck`` - ``PresenceWorker._stuck`` (presence.py:819-858): a fresh recurrence (``recurrence``: cosine 0.75 of the
    stored embeddings, at least 3 facts over at least 6 min, the newest within 15 min) in the last hour; looked at again
    only after ``STUCK_RELOOK_SHOWN_FACTS`` more facts and ``STUCK_RELOOK_SHOWN_S``. Confidence 0.5 + 0.1 per fact past
    3, at most 0.9. Time-bound: :data:`STUCK_TTL_S` after the newest fact.

    Differences: the facts are read from content-free columns (the same filters as ``_known_facts``); the result is
    reused while nothing new is in memory and for five minutes (the old ``_marks``)."""
    store, now = ctx.store, ctx.now

    def groups() -> list[list[_Fact]]:
        facts = _user_facts(store, now - STUCK_WINDOW_S, now)
        return recurrence(facts, store.journal_vectors([f.id for f in facts]), now=now) if facts else []

    try:
        newest = store.journal_newest()
    except Exception:  # noqa: BLE001
        newest = None
    for group in ctx.memo.get("stuck", (newest, int(now // 300)), now, 0.0, groups):
        key = f"stuck:J{group[0].id}"
        v = ctx.seen.get(key)
        if v:
            newer = sum(1 for f in group if f.id > int(v[1]))
            if newer < STUCK_RELOOK_SHOWN_FACTS or now - float(v[0]) < STUCK_RELOOK_SHOWN_S:
                continue
        expires = group[-1].at + STUCK_TTL_S
        if expires <= now:
            continue
        threads = [t for t in dict.fromkeys(f.work_thread_id for f in group) if t is not None]
        return [Candidate("stuck", key, created_at=group[-1].at, expires_at=expires, home="card", time_bound=True,
                          confidence=min(0.9, 0.5 + 0.1 * max(0, len(group) - STUCK_MIN_FACTS)),
                          refs={"threads": threads}, evidence={f"J{f.id}": f.at for f in group}, needs_writer=True,
                          offer_channel="stuck", seen_n=max(f.id for f in group),
                          why=f"stuck facts={len(group)} span_min={(group[-1].at - group[0].at) / 60.0:.0f}")]
    return []


def gen_recap(ctx: Context) -> list[Candidate]:
    """``recap`` - ``PresenceWorker._recap`` / ``_keep_look`` / ``_recap_look`` (presence.py:891-1003): a call session's
    digests are all written, it ended within ``RECAP_WITHIN_S`` and holds ``RECAP_MIN_SPEECH_S`` of the user's speech; a
    game's own voice chat never gets one. While fresh (:data:`RECAP_FRESH_S`) it is a card: code text when the call has
    facts waiting for the user's yes (the keep list, ``admission.recap_text``), else the writer; older, a Today line
    (code: the app and the time).

    Differences: once per call session by the judge's own mark; a game-only or too-short session is skipped each time
    (the old code marked it)."""
    store, now = ctx.store, ctx.now
    try:
        ready = store.call_digests_ready(now - RECAP_WITHIN_S - 86400.0, now)
    except Exception:  # noqa: BLE001
        return []
    if not ready:
        return []

    def sessions() -> list[list[Any]]:
        calls = [c for c in store.calls(since=now - RECAP_WITHIN_S - 2 * 86400.0)
                 if c.ended_at is not None and c.ended_at <= now]
        return _call_sessions(calls)

    for session in reversed(ctx.memo.get("recap", tuple(sorted(ready.items())), now, 600.0, sessions)):
        end = max(c.ended_at for c in session)
        if now - end > RECAP_WITHIN_S:
            continue
        key = f"recap:call:{session[0].id}"
        if ctx.seen.looked(key, now):
            continue
        if all(getattr(c, "kind", "call") == "game" for c in session):
            continue
        need = [c for c in session if (c.speech_s or 0.0) >= RECAP_ROW_MIN_SPEECH_S]
        if not need or any(c.id not in ready for c in need):
            continue
        if sum(c.speech_s or 0.0 for c in session) < RECAP_MIN_SPEECH_S:
            continue
        ids = [c.id for c in session]
        fresh = now - end < RECAP_FRESH_S
        if fresh:
            ctx.deadline(end + RECAP_FRESH_S)   # it becomes a Today line then
        app = str(getattr(session[0], "app", "") or "")
        keep_text = None
        if fresh:
            def keep_count(ids: list[int] = ids) -> int:
                from yuki.memory import admission

                return len(admission.recap_items(store, ids, now=now))

            try:   # the keep list is read again at most every ten minutes while the recap waits
                n_keep = ctx.memo.get(f"recap_keep:{key}", tuple(ids), now, 600.0, keep_count)
                if n_keep:
                    from yuki.memory import admission

                    keep_text = admission.recap_text(n_keep, app=app)
            except Exception:  # noqa: BLE001
                keep_text = None
        start = min(c.detected_at for c in session)
        line = _clip(f"Notes from your {app or 'call'} call {_when(start, now)} are ready", DIGEST_LINE_CHARS)
        return [Candidate("recap", key, created_at=end, expires_at=end + RECAP_WITHIN_S,
                          home="card" if fresh else "digest", confidence=0.7, refs={"call": ids},
                          evidence={f"CALL{c.id}": float(c.detected_at) for c in session}, text=keep_text,
                          digest_line=line, needs_writer=bool(fresh and keep_text is None), offer_channel="recap",
                          why=f"recap fresh={int(fresh)} keep={int(keep_text is not None)} calls={len(ids)}")]
    return []


def _fact_ids(evidence: Iterable[Any]) -> list[int]:
    return [int(str(e)[1:]) for e in evidence or [] if str(e)[:1] == "J" and str(e)[1:].isdigit()]


def gen_event_followups(ctx: Context) -> list[Candidate]:
    """``followup`` - ``PresenceWorker._followup`` (presence.py:1084-1118): a dated personal event of the user's (found
    by the events scan) whose moment passed by ``FOLLOWUP_AFTER_S`` (an all-day one: from ``ALL_DAY_ASK_AT`` that day),
    at most ``FOLLOWUP_WITHIN_S`` ago, with the facts it rests on still in memory. The writer asks how it went; its
    Today line is code (the event's own few words and its day).

    Differences: the 3-hourly events scan is not run here (``PresenceWorker._followup`` still runs it; jSwitch moves it
    onto the judge's schedule); an event's ``asked`` flag is read only live; once per event by the judge's own mark.

    tc1 (26 Oct): one follow-up per event, however many rows it was stored as (``ctx.events`` is folded; every row's
    ``followup:`` mark counts), and never before the event: an event with no sure time is asked from
    ``ALL_DAY_ASK_AT``, and not before ``FOLLOWUP_AFTER_S`` past the latest time any of its rows named (an offered
    07:30 made 08:30 "how did it go?" eligible two hours before a 10:30 interview)."""
    from yuki.memory.checkins import _minutes

    now = ctx.now
    out: list[Candidate] = []
    for ev in ctx.events:
        if ctx.live and ev.get("asked"):
            continue
        try:
            at, all_day = float(ev["at"]), bool(ev.get("all_day"))
            latest = float(ev.get("latest") or at)
        except (KeyError, TypeError, ValueError):
            continue
        ask_from = max(at + _minutes(ALL_DAY_ASK_AT) * 60.0, latest + FOLLOWUP_AFTER_S) if all_day \
            else at + FOLLOWUP_AFTER_S
        until = at + (86400.0 if all_day else 0.0) + FOLLOWUP_WITHIN_S
        if now < ask_from:
            ctx.deadline(ask_from)
            continue
        if now > until:
            continue
        keys = [f"followup:{i}" for i in ev.get("ids") or [ev.get("id")]]
        key = f"followup:{ev.get('id')}"
        if any(ctx.seen.looked(k, now) for k in keys):
            continue
        facts = _journal_meta(ctx.store, _fact_ids(ev.get("evidence")))
        if not facts:
            continue
        what, day = ev.get("what"), _when(at, now, all_day)
        out.append(Candidate("followup", key, created_at=ask_from, expires_at=until, home="card", confidence=0.6,
                             refs={"event": ev.get("id")}, evidence={f"J{f.id}": f.at for f in facts},
                             digest_line=(lambda w=what, d=day: _clip(f"{_clip(w, 70)} ({d}) - how did it go?",
                                                                       DIGEST_LINE_CHARS)),
                             needs_writer=True, offer_channel="followup", why=f"followup facts={len(facts)}",
                             seen_keys=tuple(k for k in keys if k != key)))
        break
    return out


def gen_plans(ctx: Context) -> list[Candidate]:
    """``plans`` - ``PresenceWorker._plans`` (presence.py:1228-1288): a plan of the user's (their own dated commitment,
    said on a call or told Yuki; a goal with a day; a personal event) at most ``PLANS_AHEAD_S`` and at least
    ``PLANS_MIN_LEAD_S`` ahead, with no timed to-do of theirs within ``PLANS_TODO_NEAR_S`` of it and no answer yet to its
    own "want a reminder?" card; once per plan (``PLANS_AGAIN_S``). The writer may offer ``todo_add``; the Today line is
    code ("Coming up: ...").

    Differences: an event's ``asked`` flag is read only live; a plan that also has a ``confirm`` candidate is left to
    the confirm (one card about one plan, :func:`generate`). Phase 3 (3.4): today's dated items (:func:`dated_today`)
    come with it; a plan with a dated line this pass gets no plans card, and a plans card shown marks today's dated
    line for it seen (one mention per plan a day)."""
    now = ctx.now
    dated = dated_today(ctx)
    dated_refs = set(ctx.dated_refs) | {c.refs.get("item") or c.refs.get("plan") for c in dated}
    today = user_day(now, ctx.config.resume_day_start)
    cands: list[dict[str, Any]] = []
    for item in ctx.items:
        if item.due_at is None or item.source not in ("call", "chat") or not ctx.asks_ok(item):
            continue
        day0 = datetime.fromtimestamp(item.due_at).replace(hour=0, minute=0, second=0).timestamp()
        cands.append({"ref": item.ref, "at": day0 if item.all_day else float(item.due_at), "all_day": bool(item.all_day),
                      "due": float(item.due_at), "words": (lambda i=item: i.title or i.text),
                      "since": float(item.since or now), "keys": (f"plans:{item.ref}",)})
    planned = {c["ref"] for c in cands}
    for g in ctx.goals:
        if g.due_at is not None:
            at, all_day = float(g.due_at), False
        elif g.day:
            try:
                at, all_day = datetime.strptime(g.day, "%Y-%m-%d").timestamp(), True
            except ValueError:
                continue
        else:
            continue
        cands.append({"ref": g.ref, "at": at, "all_day": all_day, "due": at, "words": (lambda g=g: g.text),
                      "since": float(g.updated_at), "keys": (f"plans:{g.ref}",)})
    for ev in ctx.events:
        if (ctx.live and ev.get("asked")) or not ev.get("at"):
            continue
        if planned & _cited_items(ev):
            continue   # the event rests on a plan on their list: that plan is the one card about it
        at = float(ev["at"])
        cands.append({"ref": f"event:{ev.get('id')}", "at": at, "all_day": bool(ev.get("all_day")), "due": at,
                      "words": (lambda e=ev: e.get("what")), "since": float(ev.get("found_at") or now),
                      "keys": tuple(f"plans:event:{i}" for i in ev.get("ids") or [ev.get("id")]),
                      "refs": {f"event:{i}" for i in ev.get("ids") or [ev.get("id")]}})
    todos = [float(i.due_at) for i in ctx.items if i.source == "user" and i.due_at is not None]
    answered = _confirm_answered(ctx.store, now)
    for c in sorted(cands, key=lambda c: c["at"]):
        lead = c["at"] - now
        if lead > PLANS_AHEAD_S:
            ctx.deadline(c["at"] - PLANS_AHEAD_S)
            break
        if lead < PLANS_MIN_LEAD_S:
            continue
        if any(abs(t - c["due"]) <= PLANS_TODO_NEAR_S for t in todos):
            continue   # a timed to-do of theirs already sits by it
        key = f"plans:{c['ref']}"
        refs = c.get("refs") or {c["ref"]}
        if any(ctx.seen.looked(k, now, PLANS_AGAIN_S) for k in c["keys"]) or refs & answered or refs & dated_refs:
            continue
        when = _when(c["due"], now, c["all_day"])   # an event with no sure time: its day only
        return [Candidate("plans", key, created_at=now, expires_at=c["at"] - PLANS_MIN_LEAD_S, home="card",
                          confidence=0.7, refs={"plan": c["ref"], "all_day": bool(c["all_day"])},
                          evidence={"PLAN": c["due"], c["ref"]: c["since"]},
                          digest_line=(lambda w=c["words"], d=when: _clip(f"Coming up: {_clip(resolve(w), 80)} ({d})",
                                                                         DIGEST_LINE_CHARS)),
                          needs_writer=True, offer_channel="plans", seen_within=PLANS_AGAIN_S,
                          seen_keys=tuple(k for k in c["keys"] if k != key)
                          + tuple(f"dated:{r}:{today}" for r in sorted(refs)),
                          why=f"plans lead_h={lead / 3600.0:.1f} all_day={int(c['all_day'])}"), *dated]
    return dated


def _cited_items(ev: dict[str, Any]) -> set[str]:
    """The list items an event rests on: its evidence's ``C<id>`` lines are the user's dated plans (``commit:<id>``)."""
    return {f"commit:{str(e)[1:]}" for e in ev.get("evidence") or [] if str(e)[:1] == "C" and str(e)[1:].isdigit()}


def dated_today(ctx: Context) -> list[Candidate]:
    """Today's dated items, the Today card's leading lines (phase 3, 3.4; 18 Oct: the card spent a line on reply counts
    while a plan for that day waited): the user's own open list items (a plan said on a call or told Yuki, a to-do of
    theirs) due today, a goal for today, a personal event today. "Today" is the user's day (``resume_day_start``); an
    all-day item by its date, a timed one only while still ahead. A call promise only once known what it is about
    (:meth:`Context.asks_ok`). Type ``plans`` (its ledger and toggle), home ``digest``, ``lead`` (first on the card),
    timed ones first; at most :data:`DATED_MAX`. Each once a day; shown, it marks its plans card seen (one mention per
    plan). Content-free ``why``.

    tc1 (26 Oct 2026: "Today: Hollis ... at 07:30" / "... at 10:30" / "... at 10:30" for one 10:30 interview):

    * **One line per thing.** An event is one row however many times the scan stored it (``ctx.events`` is folded,
      :func:`yuki.memory.presence.fold_events`). An event and the list item it rests on (its evidence cites the plan,
      ``C<id>`` = ``commit:<id>``) or with the same words are one line, in the item's words. A line is skipped when any
      of its rows was shown or seen today, and every row's mark is set with it (a copy never comes back as new).
    * **A time only when it is sure and agreed.** A list item's or goal's time is the user's own; an event has a time
      only when the scan stated it settled, with its zone, converted by code. When a line's sure times disagree it
      shows no time. A line with a time leaves once the time has passed; one without stays for the day.
    * **Plain words.** The title (clipped at a whole word), then " at HH:MM" only when sure: no "Today:" on the
      Today card."""
    now = ctx.now
    day = user_day(now, ctx.config.resume_day_start)
    start = day_boundary(day, ctx.config.resume_day_start)
    end = start + 86400.0
    found: list[dict[str, Any]] = []

    def add(ref: str, refs: dict[str, Any], at: float | None, all_day: bool, words: Any, *, rows: Sequence[str] = (),
            cites: set[str] | None = None) -> None:
        if at is None:
            return
        if all_day:
            if datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d") != day:
                return
        elif not (start <= float(at) < end):
            return
        found.append({"ref": ref, "refs": refs, "at": float(at), "all_day": bool(all_day), "words": words,
                      "rows": list(rows) or [ref], "cites": cites or set(), "item": "item" in refs})

    for item in ctx.items:
        if item.due_at is None or item.source not in ("call", "chat", "user") or not ctx.asks_ok(item):
            continue
        add(item.ref, {"item": item.ref}, item.due_at, bool(item.all_day), lambda i=item: i.title or i.text)
    for g in ctx.goals:
        if g.due_at is not None:
            add(g.ref, {"plan": g.ref}, float(g.due_at), False, lambda g=g: g.text)
        elif g.day:
            try:
                at = datetime.strptime(g.day, "%Y-%m-%d").timestamp()
            except ValueError:
                continue
            add(g.ref, {"plan": g.ref}, at, True, lambda g=g: g.text)
    for ev in ctx.events:
        if ev.get("at"):
            ref = f"event:{ev.get('id')}"
            add(ref, {"plan": ref}, float(ev["at"]), bool(ev.get("all_day")), lambda e=ev: e.get("what"),
                rows=[f"event:{i}" for i in ev.get("ids") or [ev.get("id")]], cites=_cited_items(ev))
    for f in found:
        f["text"] = _clip(resolve(f["words"]), 80)
    # one line per thing: items first (their words lead), then by (all day, time)
    lines: list[list[dict[str, Any]]] = []
    for f in sorted(found, key=lambda f: (not f["item"], f["all_day"], f["at"])):
        same = next((g for g in lines for m in g
                     if m["ref"] in f["cites"] or f["ref"] in m["cites"]
                     or (f["text"] and m["text"] and f["text"].casefold() == m["text"].casefold())), None)
        if same is not None:
            same.append(f)
        else:
            lines.append([f])
    out: list[tuple[tuple[int, float], Candidate]] = []
    for members in lines:
        rows = [r for m in members for r in m["rows"]]
        ctx.dated_refs.update(rows)
        keys = [f"dated:{r}:{day}" for r in rows]
        if any(ctx.seen.looked(k, now, 86400.0) for k in keys):
            continue
        lead = members[0]
        text = next((m["text"] for m in members if m["text"]), None)
        if not text:
            continue
        sure = {int(round(m["at"] / 60.0)) for m in members if not m["all_day"]}
        at = sure.pop() * 60.0 if len(sure) == 1 else None
        if at is not None and at < now:
            continue   # its time has passed
        if at is None and not any(m["all_day"] or m["at"] >= now for m in members):
            continue
        line = text + (f" at {datetime.fromtimestamp(at):%H:%M}" if at is not None else "")
        key = f"dated:{lead['ref']}:{day}"
        out.append(((0 if at is not None else 1, at if at is not None else lead["at"]),
                    Candidate("plans", key, created_at=now, expires_at=end, home="digest", confidence=0.9,
                              refs=lead["refs"], evidence={"PLAN": at if at is not None else start},
                              digest_line=_clip(line, DIGEST_LINE_CHARS), lead=True, seen_within=86400.0,
                              seen_keys=tuple(k for k in keys if k != key) + tuple(f"plans:{r}" for r in rows),
                              why=f"dated all_day={int(at is None)} rows={len(rows)}")))
    return [c for _k, c in sorted(out, key=lambda x: x[0])][:DATED_MAX]


def _confirm_answered(store: Any, now: float) -> set[str]:
    """Plans whose own "want a reminder?" card got an answer (replied, pressed, dismissed, Less of these): the user's
    reaction, read in shadow too (``PresenceWorker._confirm_answered``, :1290; content-free columns)."""
    try:
        rows = store._query("SELECT ref FROM nudges WHERE kind = 'reminder' AND trigger = 'confirm' AND at >= ?"
                            " AND reaction IN ('replied', 'accepted', 'dismissed', 'less')", (now - 14 * 86400.0,))
    except Exception:  # noqa: BLE001
        return set()
    return {str(r[0]) for r in rows if r[0]}


# ---------------------------------------------------------------------------
# Lasting items (the Today card's lines)
# ---------------------------------------------------------------------------


def gen_thread(ctx: Context) -> list[Candidate]:
    """``thread`` - ``PresenceWorker._stale`` (presence.py:1189-1211): an important active thread (importance at least
    ``STALE_MIN_IMPORTANCE``) with open items, untouched ``STALE_MIN_S``-``STALE_MAX_S``, not brought up within
    ``THREAD_AGAIN_S``; at most one a day; the most important first. A Today line, code: the thread's title, how long it
    has been quiet and its open items.

    Differences: ``_transition`` (a thread just left for other work) is not ported: §4.3 keeps only the quiet rule, and a
    switch is the moment test's now."""
    store, now = ctx.store, ctx.now
    day = user_day(now, ctx.config.resume_day_start)
    if ctx.seen.looked(f"thread:stale-day:{day}", now, 86400.0):
        return []
    try:
        threads = store.work_threads(("active",), limit=40)
    except Exception:  # noqa: BLE001
        return []
    quiet = [t for t in threads if t.open_items and (t.importance or 0) >= STALE_MIN_IMPORTANCE
             and STALE_MIN_S <= now - t.last_seen <= STALE_MAX_S
             and not ctx.seen.looked(f"thread:stale:{t.id}", now, THREAD_AGAIN_S)]
    if not quiet:
        return []
    t = max(quiet, key=lambda x: ((x.importance or 0), x.last_seen))
    days = (now - t.last_seen) / 86400.0
    n_open = len(t.open_items or [])
    return [Candidate("thread", f"thread:stale:{t.id}", created_at=now, home="digest", confidence=0.5,
                      refs={"thread": t.id}, evidence={f"W{t.id}": float(t.last_seen)},
                      digest_line=(lambda t=t, d=days, n=n_open: _clip(
                          f"{_clip(t.title, 70)}: untouched for {d:.0f} days, {n} open item{'s' if n != 1 else ''}",
                          DIGEST_LINE_CHARS)),
                      seen_within=THREAD_AGAIN_S, seen_keys=(f"thread:stale-day:{day}",),
                      why=f"thread quiet_d={days:.1f} open={n_open} importance={t.importance or 0}")]


def gen_question(ctx: Context) -> list[Candidate]:
    """``question`` - the getting-to-know-you question ``store.next_question`` would give (store.py:7687): the most
    important open one of at least ``question_min_importance``, its evidence at most ``question_max_age_days`` old
    (generation and vetting unchanged, in ``questions.py``). A Today line that asks: the stored question, by its id.

    Differences: read from content-free columns (the text is read only when a Today card is written); in shadow a
    question the old chain asked in the last day still counts (the judge's own view); once per question by its mark."""
    cfg, now = ctx.config, ctx.now
    since = now - float(cfg.question_max_age_days) * 86400.0
    statuses = ("open",) if ctx.live else ("open", "asked")
    rows = ctx.store._query(
        "SELECT id, importance, at, status, asked_at FROM questions WHERE origin != 'checkin' AND importance >= ?"
        " AND at >= ? AND status IN (%s) ORDER BY importance DESC, at DESC, id DESC LIMIT 8"
        % ",".join("?" * len(statuses)), (int(cfg.question_min_importance), since, *statuses))
    for r in rows:
        if r[3] == "asked" and (r[4] is None or now - float(r[4]) > QUESTION_SHADOW_ASKED_S):
            continue
        qid = int(r[0])
        key = f"question:Q{qid}"
        if ctx.seen.looked(key, now):
            continue
        store = ctx.store

        def line(qid: int = qid) -> str | None:
            q = store.question(qid)
            return _clip(q.question, DIGEST_LINE_CHARS) if q is not None else None

        return [Candidate("question", key, created_at=float(r[2]), home="digest",
                          confidence=max(0.0, min(1.0, int(r[1]) / 10.0)), refs={"question": qid},
                          evidence={f"Q{qid}": float(r[2])}, digest_line=line, asks=f"Q{qid}",
                          why=f"question importance={int(r[1])}")]
    return []


def gen_work(ctx: Context) -> list[Candidate]:
    """``work`` - the work board's counts (YouNow ``work.board``, from ``workboard.board(replies=False)``): replies the
    user owes and things they wait on, when the counts changed. A Today line, code: the counts (the top chat is read
    only when a Today card is written, jSwitch)."""
    board = ctx.section("work").get("board") or {}
    owed, waiting = int(board.get("replies_owed") or 0), int(board.get("waiting_on") or 0)
    if not owed and not waiting:
        return []
    day = user_day(ctx.now, ctx.config.resume_day_start)
    key = f"work:{day}:{owed}:{waiting}"
    if ctx.seen.looked(key, ctx.now, 86400.0):
        return []
    parts = ([f"{owed} repl{'ies' if owed != 1 else 'y'} you owe"] if owed else []) + \
        ([f"waiting on {waiting} from others"] if waiting else [])
    return [Candidate("work", key, created_at=ctx.now, home="digest", confidence=0.5,
                      evidence={"BOARD": ctx.now}, digest_line=_clip("; ".join(parts), DIGEST_LINE_CHARS),
                      seen_within=86400.0, why=f"work owed={owed} waiting={waiting}")]


def gen_review(ctx: Context) -> list[Candidate]:
    """``review`` - the stored weekly review (``review.py``): the latest finished one with a teaser, within
    :data:`REVIEW_FRESH_S` of being written. A Today line, code: its stored teaser (read only when a Today card is
    written). Live: only while its own teaser card was not delivered (``nudge_id``)."""
    try:
        rows = ctx.store._query("SELECT id, at, finished_at, nudge_id FROM weekly_reviews WHERE outcome = 'ok'"
                                " AND teaser_ciphertext IS NOT NULL ORDER BY at DESC, id DESC LIMIT 1")
    except Exception:  # noqa: BLE001
        return []
    if not rows:
        return []
    rid, at, finished, nudge_id = int(rows[0][0]), float(rows[0][1]), rows[0][2], rows[0][3]
    done_at = float(finished or at)
    if ctx.now - done_at > REVIEW_FRESH_S or (ctx.live and nudge_id is not None):
        return []
    key = f"review:{rid}"
    if ctx.seen.looked(key, ctx.now):
        return []
    store = ctx.store

    def line(rid: int = rid) -> str | None:
        r = store.weekly_review(rid)
        return _clip(r.teaser, DIGEST_LINE_CHARS) if r is not None else None

    return [Candidate("review", key, created_at=done_at, expires_at=done_at + REVIEW_FRESH_S, home="digest",
                      confidence=0.6, refs={"review": rid}, evidence={f"R{rid}": done_at}, digest_line=line,
                      why="review written")]




# ---------------------------------------------------------------------------
# "Your usual now": the user's media at their usual time of day (18 Oct)
# ---------------------------------------------------------------------------

#: The capability ends a "usual" card may offer: media (a video, music). Generic end kinds (moment kinds), never the
#: user's values; which things are the user's usual, and when, is measured below. Never a message, a call, a page (a
#: page can be work) or anything that sends or commits.
USUAL_ENDS: tuple[str, ...] = ("media_play", "media_context")
#: How far back the usual is measured; how near now's time of day a day's pick must be; the bar: at least
#: USUAL_MIN_DAYS such days, and USUAL_MIN_SHARE of the same kind of days (weekday / weekend) the user did anything at
#: the PC around this time.
USUAL_BACK_DAYS = 21
USUAL_NEAR_S = 45 * 60.0
USUAL_MIN_DAYS = 3
USUAL_MIN_SHARE = 0.33
#: Not when they put it on within this long (they are on it, or just were): read fresh at every evaluation.
USUAL_RECENT_S = 2 * 3600.0
#: The day counts (21 days of moments) are read again at most this often (they move by the day, not the minute).
USUAL_STATS_S = 600.0
#: The card is about now: it goes stale after this long.
USUAL_TTL_S = 30 * 60.0
#: The one cheap wording call (only with a you-card to word it from): the model, its cap and its wait.
USUAL_WORDS_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
USUAL_WORDS_CHARS = 140
USUAL_WORDS_PROMPT = """\
You word one line on a small card that Yuki, the user's desktop assistant, shows when they sit back down at their PC. \
The card offers to put on something the user usually puts on at about this time (the facts below come from what they \
measurably do). Write ONE short, casual line (at most 18 words) that names the pick exactly as given and ends with a \
question ("Put it on?" or similar). Use the profile only to make it personal and right for this moment (for example \
"Lunch?" only when the profile says they usually eat around now). No praise, no exclamation marks, no emoji, no \
"back at it", nothing about their work. Yuki is the one speaking: first person for Yuki ("I'll pick one you like"), second person for the user; never the word "Yuki"."""
_USUAL_TOOL = {"name": "card_line", "description": "The card's one line.",
               "input_schema": {"type": "object", "properties": {"line": {"type": "string"}}, "required": ["line"]}}


def _usual_caps() -> list[Any]:
    """The shipped capabilities a "usual" card may run: a media end (:data:`USUAL_ENDS`), nothing that sends, a run that
    commits nothing (the engine's own table). Data only; ``[]`` when the files can't be read."""
    try:
        from yuki.skills.connection import load
        from yuki.skills.engine import run_commit

        caps = load().capabilities
    except Exception:  # noqa: BLE001
        return []
    out = []
    for cap in caps:
        try:
            if (cap.end in USUAL_ENDS and not getattr(cap, "sends", False) and not getattr(cap, "sends_when", ())
                    and run_commit(cap) == "none"):
                out.append(cap)
        except Exception:  # noqa: BLE001
            continue
    return out


def usual_offerable(skill: Any) -> bool:
    """Whether a "usual" card's button may run ``skill`` (checked again at the click, ``offers.act``)."""
    key = str(getattr(skill, "key", "") or "")
    return bool(key) and any(c.key == key for c in _usual_caps())


def _day_kind(at: float) -> bool:
    return datetime.fromtimestamp(at).weekday() >= 5


def _tod_s(at: float) -> float:
    d = datetime.fromtimestamp(at)
    return d.hour * 3600.0 + d.minute * 60.0 + d.second


def _near(a: float, b: float) -> bool:
    gap = abs(_tod_s(a) - _tod_s(b))
    return min(gap, 86400.0 - gap) <= USUAL_NEAR_S


def usual_now(store: Any, now: float, caps: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """The media the user usually puts on around now: per capability (:func:`_usual_caps`), the days in the last
    :data:`USUAL_BACK_DAYS` of the same kind as today (weekday / weekend) on which the user's own end moment of it came
    within :data:`USUAL_NEAR_S` of now's time of day, against the days they did anything at the PC around then.
    Content-free counts (``{"cap", "hits", "days", "share", "at_min", "apps"}``; ``apps``: the programs its picks came
    from, for :func:`_usual_recent`), best first; only those over the bar. A browser capability (its connection app is no program of the moments) counts the programs that also record
    pages (a browser, measured). Never raises (``[]``)."""
    caps = _usual_caps() if caps is None else list(caps)
    if not caps:
        return []
    own_apps = {c.app for c in caps}
    since = now - USUAL_BACK_DAYS * 86400.0
    try:
        rows = store._query("SELECT at, kind, app, by FROM moments WHERE at >= ? AND at < ?", (since, now + 1.0))
    except Exception:  # noqa: BLE001
        return []
    weekend = _day_kind(now)
    today = datetime.fromtimestamp(now).date()
    browsers = {str(r[2] or "") for r in rows if r[1] == "page"}
    present: set[Any] = set()
    for at, _kind, _app, by in rows:
        at = float(at)
        if by == "user" and _day_kind(at) == weekend and _near(at, now) and datetime.fromtimestamp(at).date() != today:
            present.add(datetime.fromtimestamp(at).date())
    out = []
    for cap in caps:
        apps = {str(r[2] or "") for r in rows if r[1] == cap.end}
        own = {cap.app} if cap.app in apps else (apps & browsers) - own_apps   # a browser, not another's own app
        picks = [float(r[0]) for r in rows if r[1] == cap.end and r[3] == "user" and str(r[2] or "") in own]
        if not picks:
            continue
        hit_days: dict[Any, float] = {}
        for at in picks:
            d = datetime.fromtimestamp(at).date()
            if d != today and _day_kind(at) == weekend and _near(at, now):
                hit_days.setdefault(d, at)
        hits = len(hit_days)
        days = max(hits, len(present))
        share = hits / days if days else 0.0
        if hits >= USUAL_MIN_DAYS and share >= USUAL_MIN_SHARE:
            tods = sorted(_tod_s(at) for at in hit_days.values())
            out.append({"cap": cap, "hits": hits, "days": days, "share": round(share, 2),
                        "at_min": round(tods[len(tods) // 2] / 60.0), "apps": frozenset(own)})
    out.sort(key=lambda u: (-u["share"], -u["hits"]))
    return out


def _usual_recent(store: Any, u: dict[str, Any], now: float) -> bool:
    """Whether the user put this on themselves within :data:`USUAL_RECENT_S` (one indexed query, fresh each
    evaluation; unknown counts as yes: no card)."""
    try:
        rows = store._query("SELECT app FROM moments WHERE kind = ? AND at >= ? AND at < ? AND by = 'user'",
                            (u["cap"].end, now - USUAL_RECENT_S, now + 1.0))
    except Exception:  # noqa: BLE001
        return True
    return any(str(r[0] or "") in u["apps"] for r in rows)


def _usual_pick(store: Any, cap: Any, now: float) -> dict[str, Any]:
    """What the card names and its button runs (read only when the card is written): the capability's taste slot
    filled from the user's own top pick at this hour (``taste.top``: a playlist), else a fresh upload of the user's
    channels at this hour for a slot filled from offered options (``youtube_feed.offer``, ``taste.takes_offers``: a
    video), else contract C2's ``choose`` (the resolver picks from taste). ``{"slots", "name", "title", "offers"}``."""
    from yuki.skills import taste

    for slot in getattr(cap, "slots", ()) or ():
        kind = str(getattr(slot, "taste", "") or "")
        if kind and getattr(slot, "role", "") == "fill":
            top = taste.top(store, kind, now=now, n=1)
            if top and top[0].name:
                return {"slots": {slot.name: top[0].name}, "name": top[0].name, "title": "", "offers": []}
        if taste.takes_offers(cap, slot):
            try:
                from yuki.skills import youtube_feed

                offers = youtube_feed.offer(store, now=now, budget_s=3.0)
            except Exception:  # noqa: BLE001
                offers = []
            if offers:
                o = offers[0]
                return {"slots": {slot.name: o.value()}, "name": " ".join(o.name.split()),
                        "title": " ".join(o.title.split()), "offers": [o]}
    return {"slots": {"choose": True}, "name": "", "title": "", "offers": []}


def _usual_lead(ctx: Context, facts: str, must: str) -> str | None:
    """One cheap wording call (:data:`USUAL_WORDS_MODEL`) for the card's line, only when there is a you-card
    (``yuki.memory.youcard``, purpose ``judge``) to make it personal; the line must name ``must`` (checked in code).
    None: use the code's own words. Content goes to the model only; the log is content-free."""
    w = ctx.worker
    if w is None:
        return None
    try:
        from yuki.memory.youcard import build_you_card

        card = build_you_card(ctx.store, purpose="judge", now=ctx.now)
    except Exception:  # noqa: BLE001 - no you-card yet (kYou not merged) or it failed: code words
        return None
    if not card:
        return None
    t0 = time.perf_counter()
    try:
        r = w.client.messages.create(
            model=USUAL_WORDS_MODEL, max_tokens=120, system=USUAL_WORDS_PROMPT, tools=[_USUAL_TOOL],
            tool_choice={"type": "tool", "name": "card_line"},
            messages=[{"role": "user", "content": f"PROFILE (code-built)\n{card}\n\nTHE CARD\n{facts}"}])
        block = next((b for b in r.content if getattr(b, "type", "") == "tool_use"), None)
        line = " ".join(str((block.input or {}).get("line") or "").split()) if block is not None else ""
        try:
            from yuki.log.requests import usage_tokens

            cost = w.settings.estimate_cost(USUAL_WORDS_MODEL, usage_tokens(r.usage))
        except Exception:  # noqa: BLE001
            cost = None
        w._log("judge_usual_words", ms=round((time.perf_counter() - t0) * 1000.0), cost_usd=cost,
               ok=bool(line and must.casefold() in line.casefold() and len(line) <= USUAL_WORDS_CHARS))
    except Exception as exc:  # noqa: BLE001
        w._log("judge_usual_words", error=type(exc).__name__)
        return None
    if not line or must.casefold() not in line.casefold() or len(line) > USUAL_WORDS_CHARS:
        return None
    return line


def gen_usual(ctx: Context) -> list[Candidate]:
    """``usual`` (18 Oct) - "Your usual now": media the user measurably puts on around this time of day
    (:func:`usual_now`), not already on in the last :data:`USUAL_RECENT_S`. A card (time-bound,
    :data:`USUAL_TTL_S`), so the moment test lets it show only at a natural break (sitting down, a call or game just
    over, a long focus stretch ending); at most one per thing a day (its seen-mark) and ``USUAL_PER_DAY`` in all
    (``attend.budget``). Its button runs the skill through the engine (``offers``: ``run_skill``, the tray's desk lease
    and game re-check): the user's own top pick at this hour named on the card, or C2's ``choose``. Code text; one
    cheap wording call only with a you-card (:func:`_usual_lead`). Never the user's work: media ends only."""
    now = ctx.now
    day = user_day(now, ctx.config.resume_day_start)
    out: list[Candidate] = []
    for u in ctx.memo.get("usual:stats", None, now, USUAL_STATS_S, lambda: usual_now(ctx.store, now)):
        cap = u["cap"]
        key = f"usual:{cap.key}:{day}"
        if ctx.seen.looked(key, now, 86400.0) or _usual_recent(ctx.store, u, now):
            continue
        store = ctx.store
        made: dict[str, Any] = {}
        when = f"{int(u['at_min']) // 60:02d}:{int(u['at_min']) % 60:02d}"
        days_word = "weekend days" if _day_kind(now) else "weekdays"
        thing = "music" if cap.end == "media_context" else "a video"
        why = f"You've put on {thing} around {when} on {u['hits']} of the last {u['days']} {days_word}."

        def text(cap: Any = cap, made: dict[str, Any] = made, thing: str = thing, why: str = why) -> str | None:
            pick = _usual_pick(store, cap, now)
            if pick["title"]:
                words = f"{pick['name']} posted \"{pick['title']}\". Put it on?"
                must, facts = pick["title"][:40], f"{pick['name']}'s new video \"{pick['title']}\""
            elif pick["name"]:
                words = f"Your usual {pick['name']} around now? Put it on?"
                must, facts = pick["name"], f"their playlist \"{pick['name']}\""
            else:
                words = f"Your usual {'music' if thing == 'music' else 'video'} around now? I'll pick one you like."
                must, facts = "", ""
            if must:   # the wording call only when the card names a pick (the code's own words are clearer otherwise)
                words = _usual_lead(ctx, f"{facts}; {why}", must) or words
            made.update(pick=pick, words=words)
            return _clip(words, TEXT_CHARS)

        def offer(cap: Any = cap, made: dict[str, Any] = made, why: str = why, thing: str = thing,
                  when: str = when) -> dict[str, Any] | None:
            pick = made.get("pick")
            if not pick:
                return None
            slots = dict(pick["slots"])
            if slots.get("choose"):
                slots["context"] = f"the user's usual {thing} around {when} ({made.get('words') or ''})"
            from yuki.memory.offers import SKILL_WONT_LINES

            step = (f"put on \"{pick['title']}\"" if pick["title"] else f"put on {pick['name']}" if pick["name"]
                    else f"pick {thing} you like and put it on")
            return {"do": "run_skill", "label": "Put it on", "steps": [step], "refs": [f"U:{cap.key}"], "text": "",
                    "due": None, "skill": cap.key, "slots": slots, "expires_at": now + USUAL_TTL_S,
                    "wont": SKILL_WONT_LINES["none"], "why": why, "usual": cap.key}

        out.append(Candidate(
            "usual", key, created_at=now, expires_at=now + USUAL_TTL_S, home="card", time_bound=True,
            window=(now, now + USUAL_TTL_S), confidence=min(0.9, 0.4 + float(u["share"]) / 2.0),
            refs={"capability": cap.key}, evidence={"USUAL": now}, text=text, offer=offer, seen_within=86400.0,
            why=f"usual end={cap.end} hits={u['hits']} days={u['days']} share={u['share']}"))
    return out


# ---------------------------------------------------------------------------
# All of them
# ---------------------------------------------------------------------------

#: (name, generator, minimum seconds between two runs; 0 = every evaluation). A reused result is filtered again by its
#: seen-marks and expiry.
GENERATORS: tuple[tuple[str, Callable[[Context], list[Candidate]], float], ...] = (
    ("routine", gen_routine, 0.0),
    ("usual", gen_usual, 0.0),
    ("reminder", gen_reminders, 0.0),
    ("asked", gen_asked, 0.0),
    ("checkin", gen_checkins, 60.0),
    ("session", gen_session, 0.0),
    ("stuck", gen_stuck, 0.0),
    ("recap", gen_recap, 0.0),
    ("followup", gen_event_followups, 60.0),
    ("plans", gen_plans, 60.0),
    ("thread", gen_thread, 600.0),
    ("question", gen_question, 300.0),
    ("work", gen_work, 0.0),
    ("review", gen_review, 600.0),
)
#: The generators that may run while the call status is long unknown (only reminders and standing requests pass H3).
CALL_UNKNOWN_GENERATORS: tuple[str, ...] = ("reminder", "asked")


def generate(ctx: Context, *, only: Iterable[str] | None = None) -> list[Candidate]:
    """Every candidate open now, deduped by key (one card about one plan: a ``plans`` candidate - its card, or today's
    dated line for an item - yields to a ``confirm`` or a ``reminder`` on the same item). Each generator is wrapped: an
    error is counted in ``ctx.errors`` (content-free), never raised."""
    wanted = set(only) if only is not None else None
    out: list[Candidate] = []
    for name, fn, every in GENERATORS:
        if wanted is not None and name not in wanted:
            continue
        try:
            if every > 0:
                got, deadlines = ctx.memo.get(f"gen:{name}", None, ctx.now, every,
                                              lambda fn=fn: _with_deadlines(ctx, fn))
                ctx.deadlines += [d for d in deadlines if d > ctx.now]
                got = [c for c in got if not ctx.seen.looked(c.key, ctx.now, c.seen_within)
                       and (c.expires_at is None or c.expires_at > ctx.now)]
            else:
                got = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.errors[f"{name}:{type(exc).__name__}"] += 1
            continue
        out += got
    confirmed = {c.refs.get("item") for c in out if c.type == "confirm"}
    carded = confirmed | {c.refs.get("item") for c in out if c.type == "reminder"}
    seen: set[str] = set()
    pile: list[Candidate] = []
    for c in out:
        if c.key in seen or (c.type == "plans" and (c.refs.get("plan") in confirmed
                                                     or (c.lead and c.refs.get("item") in carded))):
            continue
        seen.add(c.key)
        pile.append(c)
    return pile


def _with_deadlines(ctx: Context, fn: Callable[[Context], list[Candidate]]) -> tuple[list[Candidate], list[float]]:
    before = len(ctx.deadlines)
    got = fn(ctx)
    added = ctx.deadlines[before:]
    del ctx.deadlines[before:]
    return got, added


def guard_ok(c: Candidate) -> bool:
    """The check-in guard (§4.3): a check-in rests on a goal or thread id, or a measured ``DAY`` / ``HOURS`` anchor
    (never screen content); the followup's "a day they named passed" rule rests on the user's own list item id."""
    if not c.type.startswith("checkin:"):
        return True
    if c.refs.get("goal") or c.refs.get("thread"):
        return True
    if any(k in c.anchors for k in ("DAY", "HOURS")):
        return True
    item = str(c.refs.get("item") or "")
    return c.type == "checkin:followup" and item.split(":", 1)[0] in ("todo", "commit")


def line_ref(c: Candidate) -> str:
    """A Today line's id (content-free): ``Q<id>`` for a stored question, ``W<id>`` a thread, ``C<id>`` a call, ``R<id>``
    the weekly review, ``G<id>`` a goal, an item's own ref, ``E<id>`` a personal event, else the candidate's type."""
    refs = c.refs or {}
    if refs.get("question") is not None:
        return f"Q{int(refs['question'])}"
    if refs.get("thread") is not None:
        return f"W{int(refs['thread'])}"
    if refs.get("review") is not None:
        return f"R{int(refs['review'])}"
    calls = refs.get("call")
    if isinstance(calls, (list, tuple)) and calls:
        return f"C{int(calls[0])}"
    if refs.get("goal") is not None:
        return f"G{int(refs['goal'])}"
    if refs.get("item"):
        return str(refs["item"])
    if refs.get("plan"):
        return str(refs["plan"])
    if refs.get("event"):
        return f"event:{refs['event']}"
    return c.type


# ---------------------------------------------------------------------------
# The writer's evidence lines (live only: the writer is not called in shadow)
# ---------------------------------------------------------------------------


def evidence_lines(store: Any, c: Candidate, now: float, safe: Callable[[Any, int], str]
                   ) -> tuple[list[str], dict[str, float]]:
    """The candidate's own evidence, in lines with their ids, for the one writer call (``ids``: every id offered).
    Journal facts, threads, goals, a call's digest lines, list items, the personal event, the question, and the
    measured anchors (``DAY``, ``HOURS``, ``BREAK``, ``S``, ``PLAN``, ``BOARD``). Never raises (what fails is left out)."""
    from yuki.memory.personality import nature_mark
    from yuki.memory.workthreads import thread_line

    lines: list[str] = []
    ids: dict[str, float] = {}
    keys = list(c.evidence)
    try:
        for j in store.journal_by_ids([int(k[1:]) for k in keys if k[:1] == "J" and k[1:].isdigit()]):
            ids[f"J{j.id}"] = j.at
            where = safe(j.app + (" / " + j.host if j.host else ""), 80)
            lines.append(f"- {_when(j.at, now)} [J{j.id}] [{where}] {safe(j.fact, 300)}{nature_mark(j.nature)}")
    except Exception:  # noqa: BLE001
        pass
    for k in keys:
        try:
            if k[:1] == "W" and k[1:].isdigit():
                t = store.work_thread(int(k[1:]))
                if t is not None:
                    ids[k] = float(t.last_seen)
                    lines.append(f"- [{k}] {safe(thread_line(t, now), 460)}")
            elif k[:1] == "G" and k[1:].isdigit():
                from yuki.memory.checkins import goal_line

                g = store.goal(int(k[1:]))
                if g is not None:
                    ids[k] = float(g.updated_at)
                    lines.append("- " + goal_line(g, now, safe))
            elif k.startswith("CALL") and k[4:].isdigit():
                d = store.call_digests([int(k[4:])]).get(int(k[4:]))
                if d:
                    ids[k] = float(c.evidence[k])
                    lines.append(f"- [{k}] {safe(d.get('app'), 40)} call: {safe(d.get('gist'), 300)}")
                    n = 0
                    for b in d.get("blocks") or []:
                        for ln in (b.get("lines") or [])[:4]:
                            n += 1
                            lid = f"D{k[4:]}.{n}"
                            ids[lid] = float(ln.get("at") or c.evidence[k])
                            who = " [others]" if ln.get("who") == "others" else ""
                            lines.append(f"  - [{lid}] {safe(ln.get('text'), 240)}{who}")
            elif k[:1] == "Q" and k[1:].isdigit():
                q = store.question(int(k[1:]))
                if q is not None:
                    ids[k] = float(c.evidence[k])
                    lines.append(f"- [{k}] the question memory holds: {safe(q.question, 240)}")
            elif ":" in k and k.split(":", 1)[0] in ("todo", "commit", "loop", "event"):
                ids[k] = float(c.evidence[k])
                lines.append(f"- [{k}] (an item of theirs, as DEADLINES and TO-DOS show it)")
            elif k == "PLAN" and c.type == "plans":
                # the plan's own day and time, as their list or the events scan holds it (not a measurement); a plan
                # with no sure time gives its day only (tc1: an offered 07:30 and a Japan 14:00 were not its time)
                ids[k] = float(c.evidence[k])
                lines.append(f"- [{k}] when it is: {_when(float(c.evidence[k]), now, bool(c.refs.get('all_day')))}")
            elif k in ("DAY", "HOURS", "BREAK", "S", "PLAN", "BOARD") or (k[:1] == "R" and k[1:].isdigit()):
                ids[k] = float(c.evidence[k])
                lines.append(f"- [{k}] measured at {_when(float(c.evidence[k]), now)} ({c.why})")
        except Exception:  # noqa: BLE001
            continue
    return lines, ids


__all__ = [
    "BEST_CLASSES", "CALL_UNKNOWN_GENERATORS", "Candidate", "Context", "DIGEST_LINE_CHARS", "GENERATORS", "Memo",
    "SEEN_STATE", "Seen", "TYPES", "TypeInfo", "clip_words", "dated_today", "evidence_lines", "gen_routine",
    "gen_usual", "generate", "guard_ok", "line_ref", "remind_at", "resolve", "usual_now", "usual_offerable",
]
