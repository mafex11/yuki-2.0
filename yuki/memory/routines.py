"""Routines: the user's usual moments, per learned skill, mined nightly by code (PLAN.md Phase R; phase L D3).

No model, no network, $0 (it replaces the Sonnet routines pass, whose prompt, evidence and model call are gone). A
routine is "this learned skill's end state usually happens around this time on these days" (a weekday meeting tab
around one time, say). Code finds it from the occasions the skill learner keeps (:mod:`yuki.skills.learn`, built from
memory's moments), with fixed statistical bars, and keeps it true as the days go by.

**Occasions in, keys out** (D3 as phase L restates it, D7, D18; learner design §5, critique M1):

* The learner gives every occasion of the user's one skill (``occasions.end_moment_id -> skill_id``, one skill per
  moment by construction), so no key is ever ``ambiguous``. :func:`keyed_onsets` reads them through
  ``yuki.skills.learn.routine_inputs``; an occasion of no learned skill is counted as *unmapped* (the de-noise
  evidence: a call app no capability serves, a game, never reaches a routine).
* key = ``skill:<learned skill id>`` (+ ``|entity key`` when its capability's fill slot says ``routine_key``: a
  playlist), so a change of program (a means) keeps the routine. ``skill_id`` holds the learned skill's key. The
  bound values (a site, a calling service) are the skill's, never part of the routine key.
* Sends and joins never make a routine (their skills' runs commit: D24, ``message_sent`` left out); a usage record
  (no key value bound) makes one only through a ``routine_key`` fill slot.
* Moments Yuki's own runs made, Yuki's acting steps and the user's asked runs never count (D18: the learner's own
  filter); a late chat capture's time is unknown, so it never counts either.
* **Day-onsets** only: the first moment, or the first after 2 h without one, so a timeline row and a calls row of the
  same join count once.
* **D7:** a join seen on screen or on the site (``evidence`` screen / meeting / site) is *seen*; a microphone hold
  (``evidence='hold'``) counts as a hit only inside a window that already has >= 3 seen hit days of its own. So holds
  never make a routine, but they fill in the days around seen ones (E1: 5/9 seen-only, 7/9 with corroborated holds).

**The bars** (B.3, map-routines R1.5; code constants, not settings), over the last 28 days with a 14-day half-life:
a window is a centre time on a day-set (weekdays, weekends, all days); a hit is a day-onset within +-30 min of it.

* >= 4 hits on >= 4 distinct days;
* weighted hit rate >= 0.6 of the eligible days: days of the day-set with the user present within +-30 min of the
  window, on or after the window's first hit (a moved routine is re-learned: days before it never count against it);
* lift >= 3 against same-width windows at other hours on days the user was present then;
* spread: median absolute deviation <= 10 min;
* permutation p < 0.05: each day's onsets moved to random present minutes of that day, 200 draws, scored with the
  best window anywhere (so the search for the best window is paid for), on at most 3 candidates per key, each a
  different window (a candidate under 90 min from one already tested, on any day-set, is skipped);
* stability: the medians of the first and second half of the hit days within 15 min;
* windows of one key >= 90 min apart, each its own row.

**State** (R1.6): a window that passes every bar becomes an ``active`` routine row (matched to an existing row of the
same key within 45 min). An active row goes ``dormant`` after 5 eligible days in a row without a hit or a weighted
rate under 0.6, both at the best window within 45 min of its time (the hysteresis: becoming active takes every bar,
staying active only the rate and the misses near it, so a routine whose best centre wobbles doesn't flap), and
``retired`` after 28 days without a hit; a dormant row passing the bars again is active again. A
new window of a key while an older one of the same key sits under 90 min away and no longer passes moves it to
dormant ("the meeting moved"). A ``told`` row (the user said it; :func:`tell`) is active after 2 confirming seen
hits (retired unconfirmed after 28 days).

**Outputs:**

* :class:`RoutineMiner` ``(worker).run(reason=)``: the nightly pass (``yuki.memory.portrait`` calls it after the
  nightly run, right after the skill learner); it never runs during a game or a call because the nightly run doesn't.
  It loads the catalog on its own store (``catalog.load(store=, log=)``) and first feeds the user's observed program
  choices to preferences (``yuki.skills.prefs.feed_observed``: design §5).
* The portrait's Routines lines from counts (:func:`sync_facts`): one ``routine`` fact per active routine, written by
  code (origin ``measured``) through the portrait's own operations, e.g. "On 7 of the last 9 weekdays the user joined
  an Example call in BrowserX at about 09:00 (1 Sep - 12 Sep 2026)." ("N of M" counts hits on the eligible days only, so
  N never exceeds M.) The portrait's validator refuses model edits to them. The first run ends the routine facts the
  old Sonnet pass wrote. When something else ended or rewrote the line, the miner looks at who
  (:func:`fact_fate`): the user's review ("not right") or their correction turns the line off for good; a model's
  tidy-up (dreaming's merge or retire, the admission bar) is undone the next night: its version ends and the line is
  written again from counts.
* :func:`due_offers` (which offerable routines' card window is open now, for the card lane, never more than the day's
  cards left), :func:`active_near` (the talker's "now" block: offerable routines only, R1.5), :func:`why_line` (the
  card's why line, from counts), :func:`tell`.
* Logs: ``routines_mined`` (content-free: keys, unmapped counts, candidates, states, cpu ms) in the portrait log.

CLI: ``python -m yuki.memory.routines --show`` (the routines, read-only), ``--report`` (mine now on a temporary copy
and print every key's numbers and bars, near misses included; nothing written), no flag = run the pass now
(``--dry-run``: compute and print, write nothing). ``--db PATH`` for another database. (``offer-now`` is the card
lane's.)
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
import time
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

# ---------------------------------------------------------------------------------------------------------------------
# The bars (PLAN.md B.3; map-routines R1.5, R1.6): fixed code constants, tuned once on the replay, then frozen
# ---------------------------------------------------------------------------------------------------------------------

#: Days looked back at by one mining.
WINDOW_DAYS = 28
#: Half-life of a day's weight in the hit rate (days).
HALF_LIFE_D = 14.0
#: A hit: a day-onset within this many minutes of the window's centre; presence near a window uses the same width.
HIT_WINDOW_MIN = 30.0
#: At least this many hits, on at least this many distinct days.
MIN_HITS = 4
#: The weighted hit rate over eligible days.
MIN_RATE = 0.6
#: Hit rate against same-width windows at other hours.
MIN_LIFT = 3.0
#: Spread of the hits around the median (median absolute deviation, minutes).
MAX_MAD_MIN = 10.0
#: The permutation test.
P_MAX = 0.05
PERMUTATIONS = 200
#: The permutation test runs on at most this many candidates of one key (the cheap bars run first).
PERMUTED_PER_KEY = 3
#: The medians of the first and second half of the hit days are within this many minutes.
STABLE_MIN = 15.0
#: Windows of one key are at least this far apart (minutes).
WINDOW_GAP_MIN = 90.0
#: D7: hold joins count only inside a window with at least this many seen hit days of its own.
MIN_SEEN_HIT_DAYS = 3
#: Day-onsets: the first moment, or the first after this long without one.
ONSET_GAP_S = 2 * 3600.0
#: Days start at 04:00 (a night past midnight belongs to the evening before), as the welcome back's days do.
DAY_START_MIN = 4 * 60
#: A freshly mined window updates an existing row of the same key within this many minutes.
MATCH_MIN = 45.0
#: An active routine goes dormant after this many eligible days in a row without a hit.
DORMANT_MISSES = 5
#: Any routine retires after this many days without a hit.
RETIRE_DAYS = 28
#: A told routine is active after this many confirming hits.
TOLD_HITS = 2
#: The other hours the lift is measured against (offsets from the window, hours).
LIFT_HOURS = tuple(range(2, 23))
#: A routine's portrait fact is rewritten when its day-set or its time (to this many minutes) moves, else at most this
#: often (to refresh its counts).
FACT_MOVE_MIN = 5.0
FACT_REFRESH_S = 7 * 86400.0
#: The origin of the portrait facts this module writes (the portrait's validator refuses model edits to them).
MEASURED_ORIGIN = "measured"
#: Who may end or rewrite a measured routine line for good: the memory diary's passes of the user's own verdicts (the
#: review queue's "not right" / "not mine", a correction) and the portrait runs of the user's corrections. Any other
#: writer (dreaming, the admission bar, a recheck) is a tidy-up the miner undoes from the counts.
USER_PASSES = ("review", "correct")
USER_RUN_KINDS = ("correct",)
#: The card lane's windows (PLAN B.3 triggers; the lane's settings may narrow them): early [at - 8, at - 1] min,
#: late [at + 1, at + 3] min.
OFFER_LEAD_MIN = 8.0
OFFER_LEAD_LAST_MIN = 1.0
OFFER_LATE_MIN = (1.0, 3.0)
#: Routine cards a day, at most (the lane's ``routine_per_day``).
OFFERS_PER_DAY = 2

#: The day-sets a clock window is mined on: a fixed calendar menu (calendar structure, not words).
DAYSETS: dict[str, Callable[[date], bool]] = {
    "weekdays": lambda d: d.weekday() < 5,
    "weekends": lambda d: d.weekday() >= 5,
    "all": lambda d: True,
}
#: How a day-set is said.
DAYSET_WORDS = {"weekdays": "weekdays", "weekends": "weekend days", "all": "days"}
STATES = ("candidate", "active", "dormant", "retired")
OFFER_MODES = ("early", "late", "quiet", "off")
#: Moment kinds whose ``evidence`` says how a join was seen; only ``hold`` is unseen (D7). Any other moment is seen.
HOLD_EVIDENCE = "hold"

#: What the user did, per moment kind (D1's closed vocabulary, machine codes to wording, like the card titles):
#: (third person past for the portrait fact, second person for the card and the "now" block). ``{service}`` is the
#: calling service's product name the moments recorded (``"an Example call"``), else the generic thing.
KIND_PHRASES: dict[str, tuple[str, str, str]] = {
    # kind: (fact verb phrase, user verb phrase, generic object)
    "call_join": ("joined {thing} in {app}", "join {thing} in {app}", "a call"),
    "game_voice": ("was on a game voice chat in {app}", "are on a game voice chat in {app}", ""),
    "site_front": ("brought {thing} forward in {app}", "bring {thing} forward in {app}", "a page"),
    "media_play": ("started playing something in {app}", "start playing something in {app}", ""),
    "media_context": ("started a playlist in {app}", "start a playlist in {app}", ""),
    "page": ("opened {thing} in {app}", "open {thing} in {app}", "a page"),
    "volume_change": ("changed the volume of {app}", "change the volume of {app}", ""),
    "message_sent": ("sent a message in {app}", "send a message in {app}", ""),
}

LogFn = Callable[..., Any]


# ---------------------------------------------------------------------------------------------------------------------
# Days and minutes (04:00-based days; a day's minutes run from 04:00, so a routine around midnight stays whole)
# ---------------------------------------------------------------------------------------------------------------------


def day_of(at: float) -> date:
    """The 04:00-based day ``at`` belongs to."""
    return (datetime.fromtimestamp(at) - timedelta(minutes=DAY_START_MIN)).date()


def day_start(d: date) -> float:
    """When the 04:00-based day ``d`` begins (local time)."""
    return datetime.combine(d, datetime.min.time()).timestamp() + DAY_START_MIN * 60.0


def day_minute(at: float) -> float:
    """Minutes since the start of ``at``'s 04:00-based day (0-1440)."""
    return (at - day_start(day_of(at))) / 60.0


def clock_minute(dm: float) -> float:
    """A day minute as a minute of the local clock day (0-1440): 04:00 is 240."""
    return (dm + DAY_START_MIN) % 1440.0


def from_clock(clock_min: float) -> float:
    """A minute of the clock day as a day minute (the inverse of :func:`clock_minute`)."""
    return (clock_min - DAY_START_MIN) % 1440.0


def hm(clock_min: float | None) -> str:
    """``"09:05"`` (the minute begun, as the prototype printed it)."""
    if clock_min is None:
        return "--:--"
    m = int(float(clock_min) % 1440.0)
    return f"{m // 60:02d}:{m % 60:02d}"


def twelve_hour(clock_min: float) -> str:
    """``"8:58 AM"``: times as people say them (the shown form's rule)."""
    m = int(float(clock_min) % 1440.0)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"


def dayset_has(dayset: str, d: date) -> bool:
    sel = DAYSETS.get(dayset)
    return bool(sel and sel(d))


def _weight(until: float, d: date) -> float:
    return 0.5 ** ((until - day_start(d)) / 86400.0 / HALF_LIFE_D)


def _short_key(key: str) -> str:
    """A key as logs carry it: a third part (an entity digest) shortened to 12 hex characters (D20)."""
    parts = str(key).split("|")
    if len(parts) > 2:
        from yuki.log.events import log_digest

        parts[2] = log_digest(parts[2]) or "-"
    return "|".join(parts)


# ---------------------------------------------------------------------------------------------------------------------
# Presence (the user's own present stretches, indexed per day once per run)
# ---------------------------------------------------------------------------------------------------------------------


class Presence:
    """The user's present stretches (timeline rows ``present`` not begun while Yuki acted), clipped and merged per
    04:00-day once, in day minutes; answers "present within +-w minutes of minute m on day d" in O(log n)."""

    def __init__(self, spans: Iterable[tuple[float, float]]) -> None:
        self.raw = sorted((float(a), float(b)) for a, b in spans if b >= a)
        per: dict[date, list[tuple[float, float]]] = defaultdict(list)
        for a, b in self.raw:
            d = day_of(a)
            while True:
                lo = day_start(d)
                hi = day_start(d + timedelta(days=1))
                if a >= hi:
                    d += timedelta(days=1)
                    continue
                if b < lo:
                    break
                per[d].append(((max(a, lo) - lo) / 60.0, (min(b, hi) - lo) / 60.0))
                if b <= hi:
                    break
                d += timedelta(days=1)
        self._days: dict[date, tuple[list[float], list[float]]] = {}
        for d, items in per.items():
            items.sort()
            merged: list[list[float]] = []
            for a, b in items:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            self._days[d] = ([m[0] for m in merged], [m[1] for m in merged])
        self._starts = [a for a, _b in self.raw]

    def observed_days(self, since: float, until: float) -> list[date]:
        """Days with a present stretch beginning in ``[since, until)`` (the days a mining looks at)."""
        lo = bisect.bisect_left(self._starts, since)
        hi = bisect.bisect_left(self._starts, until)
        return sorted({day_of(a) for a in self._starts[lo:hi]})

    def near(self, d: date, dm: float, w: float = HIT_WINDOW_MIN) -> bool:
        """Present on day ``d`` at some point within ``[dm - w, dm + w]`` (day minutes, both ends included)."""
        got = self._days.get(d)
        if not got:
            return False
        starts, ends = got
        i = bisect.bisect_right(starts, dm + w) - 1
        return i >= 0 and ends[i] >= dm - w

    def spans(self, d: date) -> tuple[list[float], list[float]]:
        """Day ``d``'s merged present stretches, in day minutes: (starts, ends)."""
        return self._days.get(d, ([], []))

    def at(self, t: float, grace_s: float = 30.0) -> bool:
        """Present at ``t``: the last stretch begun by ``t`` still holds it, or ended under ``grace_s`` before."""
        i = bisect.bisect_right(self._starts, t) - 1
        return i >= 0 and self.raw[i][0] <= t <= self.raw[i][1] + grace_s


# ---------------------------------------------------------------------------------------------------------------------
# Moments in, keys out
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyInfo:
    """What a routine key is: the skill, the means app, the moment kind its onsets are, and an entity digest."""

    key: str
    skill: str
    app: str
    kind: str
    entity_digest: str | None = None
    offerable: bool = False
    skill_kind: str = "change"


@dataclass
class Inputs:
    """What a mining reads: day-onsets per key (every onset, and the seen ones apart), presence, and the counts of
    moments that mapped to no skill."""

    onsets: dict[str, list[float]] = field(default_factory=dict)
    seen: dict[str, list[float]] = field(default_factory=dict)
    keys: dict[str, KeyInfo] = field(default_factory=dict)
    services: dict[str, Counter] = field(default_factory=dict)
    presence: Presence = field(default_factory=lambda: Presence(()))
    unmapped: Counter = field(default_factory=Counter)
    by_yuki: int = 0
    moments: int = 0
    #: The learned skills the user marked Forget this / Not mine (``skills.id``): their routines end at once.
    gone: set[int] = field(default_factory=set)


def tombstoned_ids(store: Any) -> set[int]:
    """The ``skills.id`` of the learned rows the user marked Forget this / Not mine (lPage's tombstones). Never
    raises."""
    try:
        return {int(r[0]) for r in store._query("SELECT id FROM skills WHERE origin = 'learned' AND (deleted_at IS NOT"
                                                 " NULL OR state IN ('deleted', 'not_mine'))")}
    except Exception:  # noqa: BLE001 - a database before migration 43
        return set()


def key_skill_id(key: Any) -> int | None:
    """The learned ``skills.id`` of a routine key (``skill:<id>|...``), else ``None``."""
    head = str(key or "").split("|", 1)[0]
    if not head.startswith("skill:"):
        return None
    try:
        return int(head.split(":", 1)[1])
    except ValueError:
        return None


def load_skills(store: Any = None, log: LogFn | None = None) -> tuple[Any, ...]:
    """The catalog (``yuki.skills.catalog.load``): the capabilities and the user's live learned skills. With
    ``store`` its learned rows are read from that database (one count query when unchanged; phase L C.2: the memory
    service's nightly passes load it with ``store=`` and ``log=``); ``log`` is told a skipped file or row once. Empty
    when the skill files can't be read."""
    try:
        from yuki.skills.catalog import load

        return tuple(load(store=store, log=log).skills)
    except Exception:  # noqa: BLE001 - a broken connection folder means no routines, never a crash
        return ()


def routine_map(skills: Iterable[Any]) -> dict[tuple[str, str], list[Any]]:
    """``(moment kind, app) -> [skills whose routine_on lists it]``: the pre-phase-L mapping, kept for old callers. No
    shipped capability lists ``routine_on`` any more (the habit-key refusal), so this is empty; :func:`keyed_onsets`
    reads the learner's occasions instead."""
    out: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for s in skills:
        for kind, app in getattr(s, "routine_on", ()) or ():
            if s not in out[(kind, app)]:
                out[(kind, app)].append(s)
    return out


def _routine_key_slot(skill: Any) -> bool:
    return any(getattr(sl, "routine_key", False) for sl in getattr(skill, "slots", ()) or ())


def served_kind(skill: Any) -> str:
    """The end kind a skill's habit ends in (its capability's first ``serves`` end: a meeting tab's ``call_join``),
    else its own end (data, design §4.1)."""
    serves = tuple(getattr(skill, "serves", ()) or ())
    return str(serves[0].end) if serves else str(getattr(skill, "end", "") or "")


def learned_id(store: Any, skill_key: str) -> int | None:
    """The ``skills.id`` of a learned skill's key (``None``: not a learned row). Never raises."""
    try:
        rows = store._query("SELECT id FROM skills WHERE key = ? AND origin = 'learned' LIMIT 1", (str(skill_key),))
    except Exception:  # noqa: BLE001
        return None
    return int(rows[0][0]) if rows else None


def _moment_attrs(cap: Any, m: Mapping[str, Any]) -> dict[str, Any]:
    """A moment's slot values under a capability's ``bind`` (the learner's own reading: ``occasions.Builder._src`` then
    ``bind.attrs_for``; a chat send's thread is never needed here, sends never make a routine)."""
    from types import SimpleNamespace

    from yuki.skills.learn.bind import attrs_for
    from yuki.skills.learn.occasions import Builder

    src = Builder._src(SimpleNamespace(_threads={}), dict(m))   # type: ignore[arg-type]
    return attrs_for(cap, src)


def _matches(skill: Any, cap: Any, m: Mapping[str, Any], app: str | None) -> bool:
    """A moment is the learned skill's end state: each bound key value matches the moment's (as one identity,
    ``connection.scope_value``); a moment missing a key value counts in the skill's own means app (the learner's
    missing-key rule: a microphone hold with no service)."""
    from yuki.skills.connection import scope_value

    bound = dict(getattr(skill, "bound", None) or {})
    try:
        attrs = _moment_attrs(cap, m)
    except Exception:  # noqa: BLE001 - unreadable: not the skill's
        return False
    for slot in getattr(cap, "slots", ()) or ():
        if slot.role != "key" or slot.name not in bound:
            continue
        got = attrs.get(slot.name)
        if got in (None, ""):
            if not app or str(m.get("app") or "") != app:
                return False
            continue
        if scope_value(got) != scope_value(bound[slot.name]):
            return False
    return True


def key_entity(key: Any) -> str | None:
    """The entity part of a learned routine key (``skill:<id>|<entity key>``: a playlist routine), else ``None``."""
    parts = str(key or "").split("|")
    if len(parts) == 2 and parts[0].startswith("skill:") and parts[1]:
        return parts[1]
    return None


def _learned_browsers(store: Any) -> set[str] | None:
    """The programs the learner's last full pass measured as browsers (``yuki.skills.learn.BROWSERS_STATE``), else
    ``None`` (no pass yet: no page is left out on that ground)."""
    try:
        from yuki.skills.learn import BROWSERS_STATE

        kept = json.loads(store.nudge_state(BROWSERS_STATE) or "{}")
        got = kept.get("browsers") if isinstance(kept, dict) else None
        return {str(k) for k in got} if isinstance(got, dict) and got else None
    except Exception:  # noqa: BLE001
        return None


def _page_counts(m: Mapping[str, Any], browsers: set[str] | None) -> bool:
    """A ``page`` moment the learner would take as an occasion (its page drops, ``occasions.Builder``): shown in a
    measured browser, and a host-scope visit or a site template's page."""
    if browsers is not None and str(m.get("app") or "") not in browsers:
        return False
    facts = m.get("facts") or {}
    if not isinstance(facts, Mapping):
        return False
    return facts.get("scope") == "host" or bool(facts.get("template"))


def skill_onsets(store: Any, skill: Any, since: float, until: float, *, app: str | None = None,
                 skills: Sequence[Any] | None = None, entity: str | None = None) -> list[float]:
    """When the user reached a learned skill's end state in ``[since, until]``, oldest first (D3, D18): the learner's
    user occasions of that skill, and the user's own moments of its end kind whose bound key values match (the
    occasions lag the moments by up to the learner's incremental pass, so a meeting joined minutes ago counts at once).
    Never Yuki's own (``by='yuki'``). ``app``: the routine's means app (a moment with no key value counts there).
    ``entity``: a ``routine_key`` routine's entity (:func:`key_entity`: one playlist), so another playlist never
    counts; a page moment counts only as the learner would take it (:func:`_page_counts`; lWire review r1 L2)."""
    if skill is None or getattr(skill, "origin", "") != "learned":
        return []
    out: set[float] = set()
    ent = str(entity or "")[:16]
    sid = learned_id(store, skill.key)
    if sid is not None:
        try:
            for (at,) in store._query(
                    "SELECT at FROM occasions WHERE skill_id = ? AND by = 'user' AND at >= ? AND at <= ?"
                    + (" AND substr(entity_key, 1, 16) = ?" if ent else ""),
                    (sid, float(since), float(until), *((ent,) if ent else ()))):
                out.add(float(at))
        except Exception:  # noqa: BLE001 - a database before migration 43
            pass
    kind = served_kind(skill)
    cap = None
    try:
        from yuki.skills.catalog import capabilities

        cap = capabilities().get(str(getattr(skill, "capability", "") or ""))
    except Exception:  # noqa: BLE001
        cap = None
    if kind and cap is not None:
        try:
            rows = store.moments_between(since, float(until) + 1.0, kinds=[kind])
        except Exception:  # noqa: BLE001
            rows = []
        browsers = _learned_browsers(store) if kind == "page" else None
        for m in rows:
            if str(m.get("by") or "user") == "yuki":
                continue
            if ent and str(m.get("entity_key") or "")[:16] != ent:
                continue
            if kind == "page" and not _page_counts(m, browsers):
                continue
            if _matches(skill, cap, m, app):
                out.add(float(m["at"]))
    return sorted(out)


def habit_run_done(store: Any, skill: Any, since: float, until: float, *, entity: str | None = None) -> float | None:
    """The first time in ``[since, until]`` a run of the skill's habit ended done (``done`` / ``already``): its own key,
    and a usage record's capability (``offers.habit_keys``: "play my X playlist" runs on the capability; lWire review
    r1 M3). ``entity``: a ``routine_key`` routine's entity; a run counts only when its played entity
    (``skill_runs.entity_digest``, the moments' own digest) is that one, read through the moments that carry both.
    Never raises."""
    from yuki.memory.offers import habit_keys

    keys = habit_keys(skill)
    if not keys:
        return None
    ent = str(entity or "")[:16]
    digests: list[str] = []
    if ent:
        try:
            rows = store.moments_between(float(since) - ONSET_GAP_S, float(until) + 1.0, kinds=[served_kind(skill)])
        except Exception:  # noqa: BLE001
            rows = []
        digests = sorted({str(m["entity_digest"]) for m in rows if m.get("entity_digest")
                          and str(m.get("entity_key") or "")[:16] == ent})
        if not digests:
            return None
    try:
        from yuki.memory.testtag import sql_real_request, sql_real_session

        # lBrain: a live test's run never counts as the habit done today
        rows = store._query(
            f"SELECT min(coalesce(t_done, t0)) FROM skill_runs WHERE skill IN ({','.join('?' * len(keys))}) AND"
            " t0 >= ? AND t0 <= ? AND outcome IN ('done', 'already') AND " + sql_real_session("session_id")
            + " AND " + sql_real_request("request_id")
            + (f" AND entity_digest IN ({','.join('?' * len(digests))})" if digests else ""),
            (*keys, float(since), float(until) + 1.0, *digests))
    except Exception:  # noqa: BLE001 - no skill ledger yet
        return None
    return float(rows[0][0]) if rows and rows[0][0] is not None else None


def _collapse(times: Iterable[float]) -> list[float]:
    """Day-onsets: the first time, or the first after :data:`ONSET_GAP_S` without one (gaps from the previous time)."""
    out: list[float] = []
    prev: float | None = None
    for t in sorted(times):
        if prev is None or t - prev >= ONSET_GAP_S:
            out.append(t)
        prev = t
    return out


def keyed_onsets(store: Any, since: float, until: float, *, skills: Sequence[Any] | None = None,
                 presence: Presence | None = None) -> Inputs:
    """Day-onsets per routine key from the learner's occasions in ``[since, until)`` (phase L: D3, D7, D18; design §5),
    with the user's presence: ``yuki.skills.learn.routine_inputs`` (one learned skill per occasion, so no key is ever
    ``ambiguous``), each key's means app the one its occasions used most, and whether its skill passes the offer gate
    (``yuki.memory.offers.skill_offerable``: trust and the routine itself are checked when a card is due). A database
    the learner never wrote to falls back to the old ``routine_on`` mapping (empty for every shipped capability)."""
    skills = load_skills(store) if skills is None else tuple(skills)
    try:
        from yuki.skills.learn import routine_inputs

        got = routine_inputs(store, since, until)
    except Exception:  # noqa: BLE001 - no learner tables yet
        got = None
    if got is not None and (got.keys or got.unmapped):
        return _learned_inputs(store, got, skills, since, until, presence)
    mapping = routine_map(skills)
    kinds = sorted({k for k, _a in mapping})
    every: dict[str, list[float]] = defaultdict(list)
    seen: dict[str, list[float]] = defaultdict(list)
    out = Inputs()
    lo = since - ONSET_GAP_S   # the onset rule looks at the moment before the window too
    rows = store.moments_between(lo, until, kinds=kinds) if kinds else []
    for m in rows:
        out.moments += 1
        if (m.get("by") or "user") == "yuki":   # D18: Yuki's own runs never make a routine
            out.by_yuki += 1
            continue
        kind, app = str(m.get("kind") or ""), str(m.get("app") or "")
        hits = mapping.get((kind, app), [])
        if len(hits) != 1:   # no skill, or two skills list it: never guessed
            out.unmapped[f"{kind}|{app or '-'}" + ("" if not hits else "|ambiguous")] += 1
            continue
        skill = hits[0]
        key = f"{skill.key}|{app}"
        digest = None
        if _routine_key_slot(skill):
            digest = m.get("entity_digest")
            if not digest:
                out.unmapped[f"{kind}|{app}|no_entity"] += 1
                continue
            key += f"|{digest}"
        if key not in out.keys:
            out.keys[key] = KeyInfo(key, skill.key, app, kind, digest, bool(getattr(skill, "offerable", False)),
                                    str(getattr(skill, "kind", "change")))
        at = float(m["at"])
        every[key].append(at)
        if (m.get("evidence") or "") != HOLD_EVIDENCE:
            facts = m.get("facts") or {}
            seen[key].append(float(facts.get("seen_at") or at))
        if m.get("service"):
            out.services.setdefault(key, Counter())[str(m["service"])] += 1
    out.onsets = {k: [t for t in _collapse(v) if since <= t < until] for k, v in every.items()}
    out.seen = {k: [t for t in _collapse(v) if since <= t < until] for k, v in seen.items()}
    out.presence = presence if presence is not None else Presence(store.presence_spans(since, until))
    out.gone = tombstoned_ids(store)
    return out


def _learned_inputs(store: Any, got: Inputs, skills: Sequence[Any], since: float, until: float,
                    presence: Presence | None) -> Inputs:
    """The learner's inputs with each key's means app (its occasions' most used app; a key's first occasion otherwise)
    and its offer gate, and ``presence`` when the caller has one. Never drops a key."""
    from dataclasses import replace

    from yuki.memory.offers import skill_offerable

    by_key = {s.key: s for s in skills}
    apps: dict[int, str] = {}
    try:
        for sid, app, _n in store._query(
                "SELECT skill_id, app, count(*) AS n FROM occasions WHERE skill_id IS NOT NULL AND by = 'user' AND"
                " at >= ? AND at < ? AND app IS NOT NULL GROUP BY skill_id, app ORDER BY n", (since, until)):
            apps[int(sid)] = str(app)   # ordered by count: the most used app is written last
    except Exception:  # noqa: BLE001
        apps = {}
    gone = got.gone if got.gone else tombstoned_ids(store)
    got.gone = gone
    gate: dict[str, bool] = {}
    for key, info in list(got.keys.items()):
        sid = key_skill_id(key)
        sid = -1 if sid is None else sid
        if sid in gone:
            # the user's Forget this / Not mine: its occasions feed no routine (lPage's tombstone contract)
            got.unmapped[f"{info.kind}|tombstone"] += len(got.onsets.get(key, ()))
            got.keys.pop(key, None)
            got.onsets.pop(key, None)
            got.seen.pop(key, None)
            continue
        if info.skill not in gate:
            try:
                gate[info.skill] = bool(skill_offerable(store, by_key.get(info.skill))[0])
            except Exception:  # noqa: BLE001
                gate[info.skill] = False
        got.keys[key] = replace(info, app=apps.get(sid, info.app), offerable=gate[info.skill])
    if presence is not None:
        got.presence = presence
    return got


# ---------------------------------------------------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Window:
    """One candidate window of a key on a day-set, with its bars' numbers."""

    key: str
    dayset: str
    centre: float                     # the candidate centre (day minutes)
    median: float                     # the hits' median (day minutes)
    mad: float
    hits: int
    seen_hits: int
    eligible: int
    rate: float
    hit_days: list[date]
    hit_at: dict[date, float]         # each hit day's onset nearest the centre (day minutes)
    elig_hits: int = 0                # hits on eligible days (what the rate counts, and "N of M" says)
    elig_seen: int = 0                # seen hits on eligible days
    lift: float | None = None
    p: float | None = None
    stable: bool | None = None
    passed: bool = False
    failed: list[str] = field(default_factory=list)
    first_hit: date | None = None
    last_hit_at: float | None = None

    @property
    def at_min(self) -> float:
        """The window as a minute of the clock day."""
        return clock_minute(self.median)

    def summary(self) -> dict[str, Any]:
        """Content-free numbers (the report, the log)."""
        return {"key": _short_key(self.key), "dayset": self.dayset, "at": hm(self.at_min), "hits": self.hits,
                "seen_hits": self.seen_hits, "eligible": self.eligible, "hits_on_eligible": self.elig_hits,
                "rate": round(self.rate, 2),
                "lift": None if self.lift is None else round(self.lift, 1), "mad": round(self.mad, 1),
                "p": None if self.p is None else round(self.p, 3), "stable": self.stable, "pass": self.passed,
                "failed": list(self.failed)}


@dataclass
class Mined:
    """One mining's result: the windows that passed (per key, >= 90 min apart) and each key's best near miss."""

    until: float
    passed: list[Window] = field(default_factory=list)
    near: dict[str, Window] = field(default_factory=dict)
    candidates: int = 0
    permuted: int = 0


def _complete(d: date, dm: float, until: float) -> bool:
    """Day ``d``'s window around ``dm`` is over by ``until`` (a day still under way never counts as a miss)."""
    return day_start(d) + (dm + HIT_WINDOW_MIN) * 60.0 <= until


def _byday(onsets: Iterable[float], since: float, until: float) -> dict[date, list[float]]:
    out: dict[date, list[float]] = defaultdict(list)
    for t in onsets:
        if since <= t < until:
            out[day_of(t)].append(day_minute(t))
    return out


def _lift(byday: Mapping[date, list[float]], days: Sequence[date], presence: Presence, median: float,
          rate: float) -> float:
    """The window's rate against same-width windows at the other hours, on days present then (circular minutes)."""
    w = HIT_WINDOW_MIN
    base_hit = base_n = 0
    for k in LIFT_HOURS:
        c2 = (median + k * 60.0) % 1440.0
        for d in days:
            if presence.near(d, c2, w):
                base_n += 1
                base_hit += any(min(abs(m - c2), 1440.0 - abs(m - c2)) <= w for m in byday.get(d, ()))
    base = base_hit / base_n if base_n else 0.0
    return rate / base if base else 99.0


def _permutation_p(days: Sequence[date], counts: Mapping[date, int], presence: Presence, observed: int,
                   rng: np.random.Generator, n: int = PERMUTATIONS) -> float:
    """p of seeing a window with ``observed`` hit days by chance: each day's onsets moved to random present minutes
    of that day; the statistic is the best window anywhere (max over centres of the days with an onset within the
    hit width), so the search for the best window is paid for. Vectorised; draws in chunks to bound memory."""
    w = HIT_WINDOW_MIN
    blocks: list[tuple[int, int, np.ndarray, np.ndarray, np.ndarray, float]] = []
    total = 0
    for di, d in enumerate(days):
        k = int(counts.get(d, 0))
        if k <= 0:
            continue
        starts, ends = presence.spans(d)
        if not starts:
            continue
        a = np.asarray(starts, dtype=np.float64)
        lens = np.asarray(ends, dtype=np.float64) - a
        tot = float(lens.sum())
        if tot <= 0:
            continue
        blocks.append((di, k, a, lens, np.cumsum(lens), tot))
        total += k
    if total == 0:
        return 1.0
    day_index = np.concatenate([np.full(k, di, dtype=np.int64) for di, k, *_ in blocks])
    onehot = np.zeros((total, len(days)), dtype=np.float32)
    onehot[np.arange(total), day_index] = 1.0
    chunk = max(1, min(n, 2_000_000 // max(1, total * total)))
    ge = 0
    done = 0
    while done < n:
        m = min(chunk, n - done)
        u = rng.random((m, total))
        fake = np.empty((m, total), dtype=np.float64)
        col = 0
        for _di, k, a, lens, cum, tot in blocks:
            x = u[:, col:col + k] * tot
            idx = np.minimum(np.searchsorted(cum, x, side="left"), len(cum) - 1)
            fake[:, col:col + k] = a[idx] + (x - (cum[idx] - lens[idx]))
            col += k
        within = (np.abs(fake[:, :, None] - fake[:, None, :]) <= w).astype(np.float32)
        covered = (within @ onehot) > 0
        best = covered.sum(axis=2).max(axis=1)
        ge += int((best >= observed).sum())
        done += m
    return (ge + 1) / (n + 1)


def _seed(key: str, dayset: str, centre: float, until: float) -> int:
    return zlib.crc32(f"{key}|{dayset}|{round(centre, 3)}|{day_of(until).toordinal()}".encode())


def mine(inputs: Inputs, until: float, *, keys: Iterable[str] | None = None) -> Mined:
    """Every key's windows from the 28 days before ``until``: cheap bars first, the permutation test only on
    survivors (at most :data:`PERMUTED_PER_KEY` per key); each key's best failing window is kept as its near miss."""
    since = until - WINDOW_DAYS * 86400.0
    w = HIT_WINDOW_MIN
    presence = inputs.presence
    days_obs = presence.observed_days(since, until)
    out = Mined(until)
    wanted = set(keys) if keys is not None else None
    for key in sorted(inputs.onsets):
        if wanted is not None and key not in wanted:
            continue
        ts = [t for t in inputs.onsets[key] if since <= t < until]
        if len(ts) < MIN_HITS:
            continue
        byday = _byday(ts, since, until)
        seen_byday = _byday(inputs.seen.get(key, ()), since, until)
        cheap: list[Window] = []
        best_fail: Window | None = None

        def note_fail(c: Window) -> None:
            nonlocal best_fail
            if best_fail is None or c.rate * c.hits > best_fail.rate * best_fail.hits:
                best_fail = c

        for ds_name, sel in DAYSETS.items():
            days = [d for d in days_obs if sel(d)]
            for c in sorted({m for d in days for m in byday.get(d, ())}):
                hit_at = {d: min(byday[d], key=lambda m: abs(m - c)) for d in days
                          if any(abs(m - c) <= w for m in byday.get(d, ()))}
                if len(hit_at) < MIN_HITS:
                    continue
                seen_days = [d for d in hit_at if any(abs(m - c) <= w for m in seen_byday.get(d, ()))]
                if len(seen_days) < MIN_SEEN_HIT_DAYS:   # D7: holds alone never make a window
                    continue
                med = statistics.median(hit_at.values())
                mad = statistics.median(abs(h - med) for h in hit_at.values())
                first_hit = min(hit_at)
                elig = [d for d in days if d >= first_hit and _complete(d, med, until) and presence.near(d, med, w)]
                if not elig:
                    continue
                wts = {d: _weight(until, d) for d in elig}
                rate = sum(wts[d] for d in hit_at if d in wts) / sum(wts.values())
                misses = 0   # eligible days in a row without a hit, most recent first (R1.6's dormant rule)
                for d in reversed(elig):
                    if d in hit_at:
                        break
                    misses += 1
                out.candidates += 1
                cand = Window(key, ds_name, c, med, mad, len(hit_at), len(seen_days), len(elig), rate,
                              sorted(hit_at), hit_at, elig_hits=sum(1 for d in hit_at if d in wts),
                              elig_seen=sum(1 for d in seen_days if d in wts), first_hit=first_hit)
                # a window whose last 5 eligible days all missed is dormant by R1.6, so it never passes (else a
                # long-held routine that stopped would stay active on its old hits, matched night after night)
                ok = rate >= MIN_RATE and mad <= MAX_MAD_MIN and misses < DORMANT_MISSES
                if ok:
                    cand.lift = _lift(byday, days, presence, med, rate)
                    ok = cand.lift >= MIN_LIFT
                if ok:
                    cheap.append(cand)
                else:
                    cand.failed = [n for n, bad in (("rate", rate < MIN_RATE), ("mad", mad > MAX_MAD_MIN),
                                                    ("misses", misses >= DORMANT_MISSES),
                                                    ("lift", cand.lift is not None and cand.lift < MIN_LIFT)) if bad]
                    note_fail(cand)
        chosen: list[Window] = []
        tested: list[Window] = []
        permuted = 0
        for cand in sorted(cheap, key=lambda x: (-x.rate * x.hits, x.mad)):
            # windows of one key stay 90 min apart, and the permutation slots go to different windows: a candidate
            # under 90 min from one already tested (passed or failed, any day-set) is a near duplicate of it and is
            # skipped (review L1), so one failing window can't use up the slots of another; the best candidate of a
            # neighbourhood is its test (a weekday-only routine sorts its weekdays window first: same hits, higher rate)
            if any(_gap(cand.median, t.median) < WINDOW_GAP_MIN for t in tested):
                continue
            if permuted >= PERMUTED_PER_KEY:
                break
            permuted += 1
            tested.append(cand)
            days = [d for d in days_obs if DAYSETS[cand.dayset](d)]
            rng = np.random.default_rng(_seed(key, cand.dayset, cand.centre, until))
            cand.p = _permutation_p(days, {d: len(v) for d, v in byday.items()}, presence, cand.hits, rng)
            hd = cand.hit_days
            h1, h2 = hd[: len(hd) // 2], hd[len(hd) // 2:]

            def medof(ds: Sequence[date], c: Window = cand) -> float:
                return statistics.median(min(byday[d], key=lambda m: abs(m - c.median)) for d in ds)

            cand.stable = len(h1) < 2 or len(h2) < 2 or abs(medof(h1) - medof(h2)) <= STABLE_MIN
            cand.failed = [n for n, bad in (("days", len(set(hd)) < MIN_HITS), ("p", cand.p >= P_MAX),
                                            ("stable", not cand.stable)) if bad]
            cand.passed = not cand.failed
            if cand.passed:
                chosen.append(cand)
            else:
                note_fail(cand)
        out.permuted += permuted
        for cand in chosen:
            cand.last_hit_at = max(t for t in ts if day_of(t) in cand.hit_at
                                   and abs(day_minute(t) - cand.hit_at[day_of(t)]) < 1e-6)
            out.passed.append(cand)
        if not chosen and best_fail is not None:
            if best_fail.lift is None:
                days = [d for d in days_obs if DAYSETS[best_fail.dayset](d)]
                best_fail.lift = _lift(byday, days, presence, best_fail.median, best_fail.rate)
            out.near[key] = best_fail
    return out


@dataclass
class Stats:
    """A fixed window's numbers now (decay, the told rule, the portrait line)."""

    hits: int = 0
    seen_hits: int = 0
    eligible: int = 0
    rate: float = 0.0
    misses_in_row: int = 0
    elig_hits: int = 0            # hits on eligible days ("N of M" says these)
    elig_seen: int = 0
    hit_days: list[date] = field(default_factory=list)
    first_hit_at: float | None = None
    last_hit_at: float | None = None


def window_stats(inputs: Inputs, key: str, dayset: str, at_min: float, until: float, *,
                 since: float | None = None) -> Stats:
    """The numbers of a fixed window (``at_min`` a clock minute) over the 28 days before ``until`` (or from
    ``since``, later): hits, D7's seen hits, eligible days from its first hit, the weighted rate, and the eligible
    days in a row without a hit, most recent first."""
    lo = until - WINDOW_DAYS * 86400.0
    if since is not None:
        lo = max(lo, since)
    w = HIT_WINDOW_MIN
    dm = from_clock(at_min)
    presence = inputs.presence
    days = [d for d in presence.observed_days(lo, until) if dayset_has(dayset, d)]
    ts = [t for t in inputs.onsets.get(key, ()) if lo <= t < until]
    byday: dict[date, list[float]] = defaultdict(list)
    for t in ts:
        byday[day_of(t)].append(t)
    seen = {day_of(t) for t in inputs.seen.get(key, ()) if lo <= t < until and abs(day_minute(t) - dm) <= w}
    out = Stats()
    day_set = set(days)
    hit_times = {d: min(v, key=lambda t: abs(day_minute(t) - dm)) for d, v in byday.items()
                 if d in day_set and any(abs(day_minute(t) - dm) <= w for t in v)}
    if not hit_times:
        return out
    out.hit_days = sorted(hit_times)
    out.hits = len(hit_times)
    out.seen_hits = sum(1 for d in hit_times if d in seen)
    out.first_hit_at = min(hit_times.values())
    out.last_hit_at = max(hit_times.values())
    first = min(hit_times)
    elig = [d for d in days if d >= first and _complete(d, dm, until) and presence.near(d, dm, w)]
    out.eligible = len(elig)
    if elig:
        wts = {d: _weight(until, d) for d in elig}
        out.rate = sum(wts[d] for d in hit_times if d in wts) / sum(wts.values())
        out.elig_hits = sum(1 for d in hit_times if d in wts)
        out.elig_seen = sum(1 for d in hit_times if d in wts and d in seen)
        for d in reversed(elig):
            if d in hit_times:
                break
            out.misses_in_row += 1
    return out


# ---------------------------------------------------------------------------------------------------------------------
# State: mined windows into routine rows
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Change:
    """One routine row to write (a new row has no ``id``) and the events that go with it."""

    row: dict[str, Any]
    events: list[tuple[str, float, dict[str, Any] | None]] = field(default_factory=list)
    stats: Stats | None = None


def _gap(a: float | None, b: float | None) -> float:
    if a is None or b is None:
        return 1e9
    d = abs(a - b) % 1440.0
    return min(d, 1440.0 - d)


def _window_numbers(w: Window) -> dict[str, Any]:
    # the row says "N of M": hits on its eligible days (the raw hit count is the bar's, kept in the report)
    return {"at_min": round(w.at_min, 3), "mad_min": round(w.mad, 3), "hits": w.elig_hits, "seen_hits": w.elig_seen,
            "eligible": w.eligible, "rate": round(w.rate, 4), "lift": None if w.lift is None else round(w.lift, 2),
            "p": None if w.p is None else round(w.p, 4), "stable": None if w.stable is None else int(w.stable)}


def _near_best(inputs: Inputs, key: str, dayset: str, at_min: float, until: float, *,
               since: float | None = None) -> Stats | None:
    """Decay's numbers (review L3, the hysteresis): over windows centred within :data:`MATCH_MIN` of a row's time
    (its own time and each onset of its key near it), the best weighted rate and the fewest eligible misses in a row
    (a :class:`Stats` holding just those), or None when no such window has a hit. A row is matched to a passing window
    within the same distance, so it stays active while the routine still holds near its time."""
    lo = until - WINDOW_DAYS * 86400.0
    if since is not None:
        lo = max(lo, since)
    dm = from_clock(at_min)
    centres = {round(float(at_min) % 1440.0, 1)}
    for t in inputs.onsets.get(key, ()):
        if lo <= t < until and dayset_has(dayset, day_of(t)) and _gap(day_minute(t), dm) <= MATCH_MIN:
            centres.add(round(clock_minute(day_minute(t)), 1))
    best: Stats | None = None
    for c in sorted(centres):
        st = window_stats(inputs, key, dayset, c, until, since=since)
        if not st.hits:
            continue
        if best is None:
            best = Stats(eligible=st.eligible, rate=st.rate, misses_in_row=st.misses_in_row)
            continue
        best.eligible = max(best.eligible, st.eligible)
        best.rate = max(best.rate, st.rate)
        best.misses_in_row = min(best.misses_in_row, st.misses_in_row)
    return best


def plan(rows: Sequence[Mapping[str, Any]], mined: Mined, inputs: Inputs, now: float) -> list[Change]:
    """What one mining changes in the routine rows (R1.6), as :class:`Change` s; nothing is written here."""
    live = [dict(r) for r in rows if r.get("state") != "retired"]
    by_id = {r["id"]: r for r in live}
    changes: dict[Any, Change] = {}
    matched: set[int] = set()
    new_rows: list[Change] = []

    def change_of(row: dict[str, Any]) -> Change:
        if row["id"] not in changes:
            changes[row["id"]] = Change({"id": row["id"]})
        return changes[row["id"]]

    for w in sorted(mined.passed, key=lambda x: (x.key, x.median)):
        info = inputs.keys.get(w.key)
        # an existing row of the key within 45 min: the same day-set first, a told one (it keeps the user's label)
        # before a mined one, then the nearest
        cands = [r for r in live if r["key"] == w.key and r.get("trigger") in ("clock", "told")
                 and r["id"] not in matched and _gap(r.get("at_min"), w.at_min) <= MATCH_MIN]
        cands.sort(key=lambda r: (r.get("dayset") != w.dayset, r.get("trigger") != "told",
                                  _gap(r.get("at_min"), w.at_min)))
        numbers = _window_numbers(w)
        stats = window_stats(inputs, w.key, w.dayset, w.at_min, mined.until)
        common = {**numbers, "first_hit_at": stats.first_hit_at, "last_hit_at": w.last_hit_at or stats.last_hit_at,
                  "misses_in_row": stats.misses_in_row, "updated_at": now, "mined_at": now}
        if cands:
            row = cands[0]
            matched.add(row["id"])
            ch = change_of(row)
            ch.row.update(common)
            ch.stats = stats
            moved_days = row.get("dayset") != w.dayset
            if moved_days:
                ch.row["dayset"] = w.dayset
            if row["state"] != "active":
                ch.row["state"] = "active"
                ch.row["active_since"] = now
                if row.get("trigger") == "told" and not row.get("confirmed_at"):
                    ch.row["confirmed_at"] = now
            if moved_days or row["state"] != "active":
                ch.events.append(("state", now, {"state_from": row["state"], "state_to": "active",
                                                 "why": "bars" if row["state"] != "active" else "dayset",
                                                 **({"dayset_from": row.get("dayset"), "dayset_to": w.dayset}
                                                    if moved_days else {})}))
            continue
        ch = Change({"key": w.key, "skill_id": info.skill if info else w.key.split("|")[0],
                     "app": info.app if info else w.key.split("|")[1],
                     "entity_digest": info.entity_digest if info else None, "trigger": "clock", "dayset": w.dayset,
                     "state": "active", "active_since": now, "offer_mode": "early", "created_at": now, **common},
                    [("state", now, {"state_from": None, "state_to": "active", "why": "bars"})], stats)
        new_rows.append(ch)
    # rows no window matched: decay (R1.6), the told rule, and a routine that moved
    for row in live:
        if row["id"] in matched:
            continue
        since = row.get("told_at") if row.get("trigger") == "told" else None
        ch_stats = window_stats(inputs, row["key"], row["dayset"], float(row.get("at_min") or 0.0), mined.until,
                                since=since)
        upd: dict[str, Any] = {"hits": ch_stats.elig_hits, "seen_hits": ch_stats.elig_seen,
                               "eligible": ch_stats.eligible, "rate": round(ch_stats.rate, 4),
                               "misses_in_row": ch_stats.misses_in_row, "mined_at": now}
        # an active row decays on the best window near its time (the hysteresis), never on its stored centre alone
        near = (_near_best(inputs, row["key"], row["dayset"], float(row.get("at_min") or 0.0), mined.until,
                           since=since) if row["state"] == "active" else None) or ch_stats
        if ch_stats.last_hit_at is not None:
            upd["last_hit_at"] = max(ch_stats.last_hit_at, float(row.get("last_hit_at") or 0.0))
        last_hit = upd.get("last_hit_at", row.get("last_hit_at"))
        state, new_state, why = row["state"], row["state"], None
        if key_skill_id(row["key"]) in inputs.gone:
            # the user marked its skill Forget this / Not mine (lWire review r1 M1): the routine ends now, and with it
            # its portrait line (sync_facts ends the fact of a routine no longer active) and its cards
            new_state, why = "retired", "skill_removed"
        elif row.get("trigger") == "told" and state == "candidate":
            if ch_stats.seen_hits >= TOLD_HITS:   # D7: a microphone hold alone never confirms anything
                new_state, why = "active", "told_confirmed"
                upd["confirmed_at"] = now
            elif mined.until - float(row.get("told_at") or row.get("created_at") or 0.0) >= RETIRE_DAYS * 86400.0:
                new_state, why = "retired", "told_unconfirmed"
        elif state in ("active", "dormant") and (last_hit is None or mined.until - float(last_hit)
                                                  >= RETIRE_DAYS * 86400.0):
            new_state, why = "retired", "no_hit_28_days"
        elif state == "active" and near.misses_in_row >= DORMANT_MISSES:
            new_state, why = "dormant", "misses_in_row"
        elif state == "active" and near.eligible and near.rate < MIN_RATE:
            new_state, why = "dormant", "rate"
        elif state == "active" and any(
                w.key == row["key"] and _gap(w.at_min, row.get("at_min")) < WINDOW_GAP_MIN for w in mined.passed):
            new_state, why = "dormant", "moved"
        ch = change_of(row)
        ch.stats = ch_stats
        if new_state != state:
            upd["state"] = new_state
            upd["updated_at"] = now
            if new_state == "active":
                upd["active_since"] = now
            ch.events.append(("state", now, {"state_from": state, "state_to": new_state, "why": why}))
        ch.row.update(upd)
    for ch in changes.values():
        prev = by_id.get(ch.row["id"], {})
        ch.row.setdefault("updated_at", prev.get("updated_at", now))
    return [*changes.values(), *new_rows]


# ---------------------------------------------------------------------------------------------------------------------
# Wording from counts (the portrait line, the card's why line)
# ---------------------------------------------------------------------------------------------------------------------


def _date_text(t: float | None, *, year: bool = False) -> str:
    if t is None:
        return "?"
    d = datetime.fromtimestamp(t)
    return f"{d.day} {d:%b}" + (f" {d:%Y}" if year else "")


def _bound_thing(skill: Any) -> str | None:
    """What a learned skill's habit is about, from its bound key values: the value of a key slot its capability reads
    from a calling service or a site (a product name or a site's name, as memory kept it), else ``None``."""
    bound = dict(getattr(skill, "bound", None) or {})
    bind = dict(getattr(skill, "bind", None) or {})
    for slot in getattr(skill, "slots", ()) or ():
        if slot.role == "key" and bind.get(slot.name) in ("service", "site") and bound.get(slot.name):
            return " ".join(str(bound[slot.name]).split())[:60] or None
    return None


def _things(store: Any, row: Mapping[str, Any], services: Mapping[str, Counter] | None,
            skills: Sequence[Any] | None = None) -> tuple[str, str, str]:
    """(kind, thing, app name) for a routine's wording: the end kind its learned skill's habit ends in, what it is
    about (the calling service the moments named most, else the skill's own bound service or site), the means app's
    display name. A skill no longer loaded words as "did this"."""
    key = str(row["key"])
    kind = ""
    skill = None
    for s in (load_skills(store) if skills is None else skills):
        if s.key == row.get("skill_id"):
            skill = s
            kind = next((k for k, a in getattr(s, "routine_on", ()) or () if a == row.get("app")), "") or served_kind(s)
            break
    generic = KIND_PHRASES.get(kind, ("", "", ""))[2]
    service = None
    if services and services.get(key):
        service = services[key].most_common(1)[0][0]
    if not service and skill is not None:
        service = _bound_thing(skill)
    thing = f"a {service} call" if service and kind == "call_join" else (service or generic)
    try:
        app = store.app_display_name(row.get("app"))
    except Exception:  # noqa: BLE001
        app = None
    if not app:
        from yuki.core.appnames import prettify

        app = prettify(str(row.get("app") or "")) or str(row.get("app") or "")
    return kind, thing, app


def fact_texts(store: Any, row: Mapping[str, Any], stats: Stats, services: Mapping[str, Counter] | None,
               skills: Sequence[Any] | None = None) -> tuple[str, str, str]:
    """(text, summary, shown) of a routine's portrait fact, from counts: memory's third-person line with the
    numbers, an eight-word label, and the line said to the user (second person, 12-hour time)."""
    kind, thing, app = _things(store, row, services, skills)
    verb, verb2, _generic = KIND_PHRASES.get(kind, ("did this in {app}", "do this in {app}", ""))
    words = DAYSET_WORDS.get(str(row["dayset"]), "days")
    at = float(row.get("at_min") or 0.0)
    first = stats.hit_days[0] if stats.hit_days else None
    last = stats.hit_days[-1] if stats.hit_days else None
    span = (f" ({_date_text(day_start(first) + 43200)} - {_date_text(day_start(last) + 43200, year=True)})"
            if first and last else "")
    hits, elig = int(row.get("hits") or stats.hits), int(row.get("eligible") or stats.eligible)
    text = (f"On {hits} of the last {elig} {words} the user {verb.format(thing=thing, app=app)} at about "
            f"{hm(at)}{span}.")
    summary_thing = (thing[2:] if thing.startswith("a ") else thing) or kind.replace("_", " ")
    summary = f"{summary_thing[:1].upper() + summary_thing[1:]} in {app}, {words} ~{twelve_hour(at)}"
    shown = (f"You {verb2.format(thing=thing, app=app)} around {twelve_hour(at)} on most {words} ({hits} of the last "
             f"{elig}).")
    return text, summary, shown


def why_line(store: Any, row: Mapping[str, Any], *, services: Mapping[str, Counter] | None = None,
             skills: Sequence[Any] | None = None) -> str:
    """The card's why line, from counts (R2.4): "You've joined it around 09:00 on 7 of the last 9 weekdays."."""
    kind, _thing, _app = _things(store, row, services, skills)
    verbs = {"call_join": "joined it", "media_context": "started it", "media_play": "started it",
             "page": "opened it", "site_front": "opened it", "message_sent": "sent one", "volume_change": "changed it"}
    did = verbs.get(kind, "done it")
    return (f"You've {did} around {hm(row.get('at_min'))} on {int(row.get('hits') or 0)} of the last "
            f"{int(row.get('eligible') or 0)} {DAYSET_WORDS.get(str(row.get('dayset')), 'days')}.")


# ---------------------------------------------------------------------------------------------------------------------
# The portrait's Routines lines, from counts
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class FactsResult:
    run_id: int | None = None
    added: int = 0
    updated: int = 0
    invalidated: int = 0
    old_invalidated: int = 0
    user_ended: int = 0          # lines the user ended or rewrote (review, correction): off for good
    rewritten: int = 0           # lines a model's tidy-up ended or rewrote, written again from counts
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)


def _fact_sig(row: Mapping[str, Any]) -> str:
    """What a routine's line was written for: its day-set and its time (a clock minute, to 0.1)."""
    return f"{row.get('dayset')}|{float(row.get('at_min') or 0.0) % 1440.0:.1f}"


def _sig_holds(sig: Any, row: Mapping[str, Any]) -> bool:
    """The line written for ``sig`` still says ``row``: the same day-set and its time under :data:`FACT_MOVE_MIN`
    away (a distance, not a rounding, so a time sitting on a boundary never rewrites the line night after night)."""
    try:
        dayset, at = str(sig).split("|", 1)
        return dayset == str(row.get("dayset")) and _gap(float(at), float(row.get("at_min") or 0.0)) < FACT_MOVE_MIN
    except (TypeError, ValueError):
        return False


def model_routine_facts(store: Any) -> list[Any]:
    """The live routine facts the old Sonnet routines pass wrote (origin model, written by a ``routines`` run)."""
    try:
        ids = {int(r[0]) for r in store._query(
            "SELECT f.id FROM portrait_facts f JOIN portrait_runs r ON r.id = f.run_id WHERE f.kind = 'routine'"
            " AND f.valid_to IS NULL AND f.origin = 'model' AND r.kind = 'routines'")}
    except Exception:  # noqa: BLE001
        return []
    return [f for f in store.portrait_facts(("routine",)) if f.id in ids]


@dataclass
class Fate:
    """What happened to a routine's line that is no longer current (:func:`fact_fate`)."""

    by_user: bool
    #: The current versions a model's tidy-up wrote in its place (the newest of its rewrites, a lasting line), which
    #: the miner replaces with the line from counts.
    replaced_by: list[int] = field(default_factory=list)


def fact_fate(store: Any, fact_id: int) -> Fate:
    """Who ended or rewrote a routine's measured line (review M1), from the fact's own history and the memory diary:

    * the user (``by_user``): a version on the way was written by their correction (a ``correct`` run, or origin
      ``user``), or the last version was ended by their review ("not right" / "not mine") or a correction (the diary
      pass ``review`` / ``correct`` at its end). The line stays off;
    * anyone else (dreaming's merge or retire, the admission bar, a recheck, or nothing on record): a tidy-up. The
      current version that replaced it (the end of its ``superseded_by`` chain) and any current line the tidy-up wrote
      in its place (the bar's lasting part) are ``replaced_by``; the miner ends them and writes its line again."""
    trail = store.fact_trail(int(fact_id))
    if not trail:
        return Fate(False)
    for version in trail[1:]:   # each rewrite: who wrote the new version
        if version.get("run_kind") in USER_RUN_KINDS or version.get("origin") == "user":
            return Fate(True)
    tail = trail[-1]
    if tail.get("valid_to") is None:
        return Fate(False, [int(tail["id"])])
    lines = store.fact_ended_by(int(tail["id"]))
    if lines and (lines[0]["pass"] in USER_PASSES or lines[0].get("run_kind") in USER_RUN_KINDS):
        return Fate(True)
    kind = tail.get("kind") or "routine"
    news: list[int] = []
    for line in lines[:1]:
        for i in line.get("new_fact_ids") or ():
            f = store.portrait_fact(int(i))
            if f is not None and f.valid_to is None and f.kind == kind and f.origin != "user" and int(i) not in news:
                news.append(int(i))
    return Fate(False, news)


def sync_facts(store: Any, now: float, rows: Sequence[Mapping[str, Any]], stats: Mapping[int, Stats], *,
               services: Mapping[str, Counter] | None = None, skills: Sequence[Any] | None = None,
               dry_run: bool = False) -> FactsResult:
    """One ``routine`` portrait fact per active routine, written by code (origin ``measured``) through
    ``Store.commit_portrait_changes`` in a ``routines`` portrait run of model ``code`` (cost 0): ADD for a new one,
    UPDATE when its day-set or time moved (or weekly, for its counts), INVALIDATE when the routine stops being active.
    The live routine facts the old Sonnet pass wrote are ended (the first run ends them all).

    A line something else ended or rewrote (:func:`fact_fate`): the user's review or correction turns it off for that
    routine (``fact_off``); a model's tidy-up (dreaming merges look-alikes and keeps the fact's origin, so its merged
    words would pass for counts) is undone: the version it wrote in the line's place becomes the line from counts
    again (an UPDATE, so its history, pin and review carry over), any other current line it wrote there ends, and a
    line it simply ended is written again (an ADD). For a routine no longer active its versions just end."""
    from yuki.memory.store import FactChange

    out = FactsResult()
    current = {f.id: f for f in store.portrait_facts(("routine",))}
    owned = {int(r["fact_id"]) for r in rows if r.get("fact_id") is not None and int(r["fact_id"]) in current}
    claimed: set[int] = set()
    changes: list[FactChange] = []
    targets: list[tuple[int | None, str]] = []   # (routine id, what) per change, in order
    for f in model_routine_facts(store):
        changes.append(FactChange("INVALIDATE", f.id, f.kind, f.subject, "", None, [], now, MEASURED_ORIGIN))
        targets.append((None, "old"))
        claimed.add(f.id)
    for row in rows:
        rid = int(row["id"])
        fid = row.get("fact_id")
        active = row.get("state") == "active"
        forced = False
        if fid is not None and fid not in current:   # ended or rewritten by something else: by whom?
            fate = fact_fate(store, int(fid))
            if fate.by_user:
                out.rows[rid] = {"fact_id": None, "fact_off": 1, "fact_sig": None}
                out.user_ended += 1
                continue
            out.rewritten += 1
            theirs = [i for i in fate.replaced_by if i in current and i not in claimed and i not in owned]
            claimed.update(theirs)
            keep = theirs[0] if active and not row.get("fact_off") and theirs else None
            for i in theirs:
                if i != keep:
                    changes.append(FactChange("INVALIDATE", i, "routine", "", "", None, [], now, MEASURED_ORIGIN))
                    targets.append((None, "tidy_end"))
            out.rows[rid] = {"fact_id": None, "fact_sig": None, "fact_at": now}
            fid, forced = keep, True
            if not active or row.get("fact_off"):
                continue
        if not active:
            if fid is not None:
                changes.append(FactChange("INVALIDATE", fid, "routine", "", "", None, [], now, MEASURED_ORIGIN))
                targets.append((rid, "invalidate"))
            continue
        if row.get("fact_off"):
            continue
        st = stats.get(rid) or Stats()
        sig = _fact_sig(row)
        fresh = (not forced and fid is not None and _sig_holds(row.get("fact_sig"), row)
                 and now - float(row.get("fact_at") or 0.0) < FACT_REFRESH_S)
        if fresh:
            continue
        text, summary, shown = fact_texts(store, row, st, services, skills)
        if not forced and fid is not None and current[fid].text == text:
            out.rows[rid] = {"fact_sig": sig, "fact_at": now}
            continue
        episodes: list[int] = []
        for d in st.hit_days:
            try:
                eid = store.longest_episode_id(day_start(d), day_start(d) + 86400.0)
            except Exception:  # noqa: BLE001
                eid = None
            if eid is not None and eid not in episodes:
                episodes.append(eid)
        confidence = round(min(0.9, max(0.0, float(row.get("rate") or st.rate))), 2)
        if fid is None:
            changes.append(FactChange("ADD", None, "routine", "", text, confidence, [],
                                      st.first_hit_at or now, MEASURED_ORIGIN, episode_ids=episodes,
                                      summary=summary, shown=shown))
            targets.append((rid, "add"))
        else:
            changes.append(FactChange("UPDATE", fid, "routine", "", text, confidence, [], now, MEASURED_ORIGIN,
                                      episode_ids=episodes, summary=summary, shown=shown))
            targets.append((rid, "update"))
    out.old_invalidated = sum(1 for _r, what in targets if what == "old")
    if not changes or dry_run:
        out.added = sum(1 for _r, what in targets if what == "add")
        out.updated = sum(1 for _r, what in targets if what == "update")
        out.invalidated = sum(1 for _r, what in targets if what in ("invalidate", "tidy_end"))
        return out
    from yuki.memory.store import PortraitRunStats

    run_id = store.start_portrait_run("routines", "code", at=now)
    run_stats = PortraitRunStats(kind="routines", model="code", window_since=now - WINDOW_DAYS * 86400.0,
                                 window_until=now)
    try:
        written = store.commit_portrait_changes(run_id, changes)
        for (rid, what), (ok, new_id) in zip(targets, written):
            if not ok:
                continue
            if what == "add":
                out.added += 1
            elif what == "update":
                out.updated += 1
            elif what in ("invalidate", "tidy_end"):
                out.invalidated += 1
            if rid is None:
                continue
            row = next(r for r in rows if int(r["id"]) == rid)
            if what in ("add", "update"):
                out.rows[rid] = {"fact_id": new_id, "fact_sig": _fact_sig(row), "fact_at": now}
            elif what == "invalidate":
                out.rows[rid] = {"fact_id": None, "fact_sig": None, "fact_at": now}
        run_stats.ops_add = out.added
        run_stats.ops_update = out.updated
        run_stats.ops_invalidate = out.invalidated + out.old_invalidated
        run_stats.outcome = "ok"
    except Exception as exc:  # noqa: BLE001
        run_stats.outcome, run_stats.error = "error", f"{type(exc).__name__}"
        store.finish_portrait_run(run_id, run_stats)
        raise
    store.finish_portrait_run(run_id, run_stats)
    out.run_id = run_id
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The nightly pass
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class RoutinesResult:
    """What a run did (``yuki.memory.portrait`` logs ``ok``, ``run_id``, ``applied``, ``rejected``, ``cost_usd``,
    ``error``)."""

    run_id: int | None
    ok: bool = True
    applied: int = 0
    rejected: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    mined: dict[str, Any] = field(default_factory=dict)


class RoutineMiner:
    """The nightly routines pass: code only (no model call, $0). ``RoutineMiner(worker).run(reason=...)`` from the
    portrait's night (its store and its log), or ``RoutineMiner(store=..., log=...)`` by hand and in tests."""

    def __init__(self, worker: Any = None, *, store: Any = None, log: LogFn | None = None,
                 skills: Sequence[Any] | None = None, days: int = WINDOW_DAYS) -> None:
        self.worker = worker
        self.store = store if store is not None else getattr(worker, "store", None)
        if self.store is None:
            raise ValueError("RoutineMiner needs a worker or a store")
        self._log_fn = log if log is not None else getattr(worker, "_log", None)
        self.skills = skills
        self.days = int(days)

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:  # noqa: BLE001 - a log never breaks the pass
            pass

    def run(self, *, now: float | None = None, reason: str = "", dry_run: bool = False) -> RoutinesResult:
        cpu0 = time.thread_time()
        now = time.time() if now is None else float(now)
        result = RoutinesResult(None)
        store = self.store
        prefs_fed: dict[str, Any] = {}
        if not dry_run:
            # design §5: one observed preference event per user occasion with a known program (never an asked one,
            # never a late send's), the night after the learner wrote them; its own errors never stop the miner
            try:
                from yuki.skills.prefs import feed_observed

                prefs_fed = feed_observed(store, now=now)
            except Exception as exc:  # noqa: BLE001
                prefs_fed = {"error": type(exc).__name__}
        try:
            skills = load_skills(store, self._log_fn) if self.skills is None else tuple(self.skills)
            inputs = keyed_onsets(store, now - self.days * 86400.0, now, skills=skills)
            mined = mine(inputs, now)
            rows = store.routines()
            changes = plan(rows, mined, inputs, now)
            stats: dict[int, Stats] = {}
            after: dict[int, dict[str, Any]] = {int(r["id"]): dict(r) for r in rows}
            for ch in changes:
                rid = ch.row.get("id")
                if not dry_run:
                    rid = store.save_routine(ch.row, events=ch.events)
                elif rid is None:
                    rid = -1 - len(stats)
                merged = {**after.get(int(rid), {}), **ch.row, "id": int(rid)}
                after[int(rid)] = merged
                if ch.stats is not None:
                    stats[int(rid)] = ch.stats
                for kind, _at, detail in ch.events:
                    result.decisions.append({"routine_id": int(rid), "key": _short_key(merged["key"]),
                                             "event": kind, **(detail or {})})
            facts = sync_facts(store, now, list(after.values()), stats, services=inputs.services, skills=skills,
                               dry_run=dry_run)
            if not dry_run:
                for rid, upd in facts.rows.items():
                    if rid >= 0:
                        store.save_routine({"id": rid, **upd})
                if facts.run_id is not None:
                    self._want_render()
            result.run_id = facts.run_id
            result.applied = facts.added + facts.updated + facts.invalidated + facts.old_invalidated
            states = Counter(r.get("state") for r in after.values())
            offerable = sum(1 for r in after.values() if r.get("state") == "active"
                            and inputs.keys.get(r["key"], KeyInfo("", "", "", "")).offerable)
            result.mined = {
                "keys": {_short_key(k): len(v) for k, v in sorted(inputs.onsets.items())},
                "unmapped": dict(sorted(inputs.unmapped.items())), "by_yuki": inputs.by_yuki,
                "candidates": mined.candidates, "permuted": mined.permuted,
                "passed": [w.summary() for w in mined.passed],
                "near": [w.summary() for w in mined.near.values()],
                "active": states.get("active", 0), "dormant": states.get("dormant", 0),
                "retired": states.get("retired", 0), "candidate": states.get("candidate", 0),
                "offerable": offerable,
                "facts": {"added": facts.added, "updated": facts.updated, "invalidated": facts.invalidated,
                          "user_ended": facts.user_ended, "rewritten": facts.rewritten},
                "first_run_invalidated": facts.old_invalidated,
                "prefs_observed": prefs_fed,
            }
        except Exception as exc:  # noqa: BLE001 - logged; the next night tries again
            result.ok, result.error = False, f"{type(exc).__name__}: {exc}"
        cpu_ms = round((time.thread_time() - cpu0) * 1000.0, 1)
        self._log("routines_mined", reason=reason, dry_run=dry_run, ok=result.ok, cpu_ms=cpu_ms,
                  run_id=result.run_id, error=result.error, decisions=result.decisions, **result.mined)
        result.mined["cpu_ms"] = cpu_ms
        return result

    def _want_render(self) -> None:
        """The portrait page shows the new Routines lines: inside the nightly pass (renders held) the one render at
        its end; otherwise the service's debounced render flag (never a model call from here)."""
        worker = self.worker
        if worker is not None and getattr(worker, "_hold_render", False):
            worker._render_held = True
            return
        try:
            from yuki.memory.store import request_render

            request_render(getattr(self.store, "path", None))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------------------------------
# For the card lane, the talker and the user's own words
# ---------------------------------------------------------------------------------------------------------------------


def offer_window(row: Mapping[str, Any], d: date, *, lead_min: float = OFFER_LEAD_MIN,
                 lead_last_min: float = OFFER_LEAD_LAST_MIN,
                 late_min: tuple[float, float] = OFFER_LATE_MIN) -> tuple[float, float] | None:
    """The card window of a routine on day ``d`` by its offer mode (early ``[at - 8, at - 1]``, late
    ``[at + 1, at + 3]`` minutes), as epoch seconds; None for quiet or off, or a day outside its day-set."""
    mode = str(row.get("offer_mode") or "early")
    if mode not in ("early", "late") or not dayset_has(str(row.get("dayset")), d) or row.get("at_min") is None:
        return None
    dm = from_clock(float(row["at_min"]))
    lo, hi = (dm - lead_min, dm - lead_last_min) if mode == "early" else (dm + late_min[0], dm + late_min[1])
    return day_start(d) + lo * 60.0, day_start(d) + hi * 60.0


def done_today(store: Any, row: Mapping[str, Any], now: float, *, skills: Sequence[Any] | None = None) -> bool:
    """The routine's own end state already happened today (since 04:00): the user reached its learned skill's end
    (:func:`skill_onsets`), or a run of its skill's habit ended done (:func:`habit_run_done`); a playlist routine
    counts only its own playlist (:func:`key_entity`)."""
    since = day_start(day_of(now))
    skill = next((s for s in (load_skills(store) if skills is None else skills) if s.key == row.get("skill_id")), None)
    entity = key_entity(row.get("key"))
    if skill is not None:
        if skill_onsets(store, skill, since, now, app=row.get("app"), entity=entity):
            return True
        return habit_run_done(store, skill, since, now, entity=entity) is not None
    try:
        from yuki.memory.testtag import sql_real_request, sql_real_session

        got = store._query("SELECT 1 FROM skill_runs WHERE skill = ? AND t0 >= ? AND t0 < ? AND outcome = 'done' AND "
                           + sql_real_session("session_id") + " AND " + sql_real_request("request_id") + " LIMIT 1",
                           (str(row.get("skill_id")), since, now))
    except Exception:  # noqa: BLE001 - no skill ledger yet
        got = []
    return bool(got)


def due_offers(store: Any, now: float | None = None, *, skills: Sequence[Any] | None = None,
               trusted: Callable[[Any], bool] | None = None, per_day: int = OFFERS_PER_DAY,
               lead_min: float = OFFER_LEAD_MIN, lead_last_min: float = OFFER_LEAD_LAST_MIN,
               late_min: tuple[float, float] = OFFER_LATE_MIN) -> list[dict[str, Any]]:
    """The routines whose card window is open now (R2.3 "when a routine is due"), first window first: active, of a
    learned skill that passes the offer gate (``offers.skill_offerable``) and whose route is trusted for offers
    (``registry.offer_ready``, D14, unless ``trusted`` says otherwise), offer mode early or late, today in its day-set,
    ``now`` inside the mode's window, its end state not yet happened today, not offered today, and fewer than
    ``per_day`` routine cards offered today. The card lane (rLane) adds its own holds (game in front, calls, quiet
    hours, the tab census) on top. Content-free dicts."""
    from yuki.memory.offers import skill_offerable

    now = time.time() if now is None else float(now)
    skills = load_skills(store) if skills is None else tuple(skills)
    by_key = {s.key: s for s in skills}
    today = day_of(now)
    since = day_start(today)
    offered = store.routine_events(since=since, until=now + 1.0, kinds=["offered"])
    if len({e["routine_id"] for e in offered}) >= per_day:
        return []
    offered_ids = {e["routine_id"] for e in offered}
    if trusted is None:
        def trusted(skill: Any) -> bool:
            from yuki.memory.offers import offer_trusted

            return offer_trusted(store, skill)   # D14; a usage record's runs pooled with its capability's
    out: list[dict[str, Any]] = []
    for row in store.routines(states=["active"]):
        skill = by_key.get(row.get("skill_id"))
        if skill is None or row["id"] in offered_ids or not skill_offerable(store, skill)[0]:
            continue
        win = offer_window(row, today, lead_min=lead_min, lead_last_min=lead_last_min, late_min=late_min)
        if win is None or not win[0] <= now <= win[1]:
            continue
        if any(_gap(o["at_min"], row.get("at_min")) < MATCH_MIN and o["key"] == row["key"] for o in out):
            continue   # a told row and a mined row of one moment: one card
        if done_today(store, row, now, skills=skills):
            continue
        try:
            ok = bool(trusted(skill))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            continue
        out.append({"id": int(row["id"]), "key": row["key"], "skill": row.get("skill_id"), "app": row.get("app"),
                    "dayset": row["dayset"], "at_min": row["at_min"], "mode": row.get("offer_mode") or "early",
                    "window": [win[0], win[1]], "expires_at": day_start(today) + (from_clock(float(row["at_min"]))
                                                                                  + 10.0) * 60.0,
                    "hits": row.get("hits"), "eligible": row.get("eligible"), "rate": row.get("rate")})
    out.sort(key=lambda o: o["window"][0])
    return out[:max(0, per_day - len(offered_ids))]   # never more than the day's routine cards left (review L5)


def active_near(store: Any, now: float | None = None, minutes: float = 60.0, *, limit: int = 2,
                skills: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Active routines of a skill that passes the offer gate whose time is within ``minutes`` of now on a day of their
    day-set (the talker's "now" block), nearest first, at most ``limit``: content-free numbers plus the label when one
    was told. Context routines (a skill that is never offered, such as a voice channel) never reach it (R1.5)."""
    from yuki.memory.offers import skill_offerable

    now = time.time() if now is None else float(now)
    today = day_of(now)
    dm_now = day_minute(now)
    offerable = {s.key for s in (load_skills(store) if skills is None else skills)
                 if getattr(s, "origin", "") == "learned" and skill_offerable(store, s)[0]}
    out = []
    for row in store.routines(states=["active"]):
        if row.get("skill_id") not in offerable:
            continue
        if not dayset_has(str(row["dayset"]), today) or row.get("at_min") is None:
            continue
        delta = from_clock(float(row["at_min"])) - dm_now
        if abs(delta) <= minutes:
            out.append({"id": int(row["id"]), "key": row["key"], "skill": row.get("skill_id"), "app": row.get("app"),
                        "at": hm(row["at_min"]), "in_min": round(delta, 1), "dayset": row["dayset"],
                        "hits": row.get("hits"), "eligible": row.get("eligible"),
                        "label": store.routine_label(row)})
    out.sort(key=lambda r: abs(r["in_min"]))
    return out[:limit]


def tell(store: Any, skill: str, app: str, dayset: str, at_min: float, *, label: str | None = None,
         now: float | None = None) -> int:
    """A routine the user said ("my standup is at 9 on weekdays"): a ``told`` row, a candidate until
    :data:`TOLD_HITS` confirming hits at that time (the miner counts them; never promoted silently). Returns its id."""
    if dayset not in DAYSETS:
        raise ValueError(f"dayset must be one of {', '.join(DAYSETS)}")
    now = time.time() if now is None else float(now)
    sid = learned_id(store, skill)
    # a learned skill's routine key is its id (phase L, design §5), the key the miner's onsets carry
    key = f"skill:{sid}" if sid is not None else f"{skill}|{app}"
    row = {"key": key, "skill_id": skill, "app": app, "trigger": "told", "dayset": dayset,
           "at_min": float(at_min) % 1440.0, "state": "candidate", "offer_mode": "early", "told": 1, "told_at": now,
           "label_ciphertext": store.cipher.encrypt(label) if label else None, "created_at": now, "updated_at": now}
    return store.save_routine(row, events=[("told", now, {"dayset": dayset, "at": hm(at_min)})])


# ---------------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------------


def _show_rows(rows: Sequence[Mapping[str, Any]], *, ready: Callable[[str], str] | None = None) -> list[str]:
    lines = []
    for r in rows:
        last = datetime.fromtimestamp(r["last_hit_at"]).strftime("%Y-%m-%d %H:%M") if r.get("last_hit_at") else "-"
        p = "-" if r.get("p") is None else f"{float(r['p']):.3f}"
        mad = "-" if r.get("mad_min") is None else f"{float(r['mad_min']):.1f}"
        rate = "-" if r.get("rate") is None else f"{float(r['rate']):.2f}"
        offer = ready(str(r.get("skill_id"))) if ready else ""
        lines.append(f"{r['id']:>3}  {_short_key(r['key'])} {r['dayset']} ~{hm(r.get('at_min'))} {r['state']}"
                     f"  mode {r.get('offer_mode')}  hits {r.get('hits')}/{r.get('eligible')}"
                     f" (seen {r.get('seen_hits')})"
                     f"  rate {rate}  mad {mad}  p {p}  last hit {last}  {r.get('trigger')}"
                     + (f"  {offer}" if offer else ""))
    return lines


def show(db: str | None = None) -> int:
    """``--show``: the routines, read-only (a plain read-only connection: nothing migrated, nothing decrypted)."""
    import sqlite3
    from pathlib import Path

    from yuki.memory.store import default_db_path

    path = Path(db) if db else default_db_path()
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"can't open {path}: {exc}")
        return 1
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM routines WHERE state != 'retired' ORDER BY id")]
        except sqlite3.OperationalError:
            print("no routines table yet (migration 42 not applied: the miner hasn't run on this version)")
            return 0
        skills = {s.key: s for s in load_skills()}

        def ready(key: str) -> str:
            s = skills.get(key)
            if s is None:
                # a learned skill lives in the database (encrypted spec): this read-only look decrypts nothing
                return "learned skill" if "#" in key else "skill not loaded"
            if getattr(s, "origin", "") != "learned" or s.offer_class == "never":
                return "not offered (context only)"
            try:
                n = conn.execute("SELECT count(*) FROM skill_runs WHERE skill = ? AND checked = 1 AND"
                                 " coalesce(recheck, 1) != 0", (key,)).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            return f"offerable; checked runs {n}"

        if not rows:
            print("no routines yet")
        for line in _show_rows(rows, ready=ready):
            print(line)
    finally:
        conn.close()
    return 0


def report(store: Any, *, at: float | None = None) -> dict[str, Any]:
    """``--report``: mine as of ``at`` (now) and say everything, writing nothing: per key its onsets, every passing
    window and its best near miss with all the bars, unmapped counts, and what the run would change."""
    now = time.time() if at is None else float(at)
    cpu0 = time.thread_time()
    skills = load_skills(store)
    inputs = keyed_onsets(store, now - WINDOW_DAYS * 86400.0, now, skills=skills)
    mined = mine(inputs, now)
    changes = plan(store.routines(), mined, inputs, now)
    return {
        "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "learned_skills": sum(1 for s in skills if getattr(s, "origin", "") == "learned"),
        "keys": {_short_key(k): {"onsets": len(v), "seen": len(inputs.seen.get(k, ())),
                                 "offerable": inputs.keys[k].offerable} for k, v in sorted(inputs.onsets.items())},
        "unmapped": dict(sorted(inputs.unmapped.items())), "by_yuki": inputs.by_yuki,
        "passed": [w.summary() for w in mined.passed], "near_misses": [w.summary() for w in mined.near.values()],
        "candidates": mined.candidates, "permuted": mined.permuted,
        "would_change": [{"id": ch.row.get("id"), "key": _short_key(ch.row.get("key") or ""),
                          "events": [{"kind": k, **(d or {})} for k, _a, d in ch.events]}
                         for ch in changes if ch.events],
        "cpu_ms": round((time.thread_time() - cpu0) * 1000.0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.routines",
                                     description="The routine miner (code only, no model): run it, or look.")
    parser.add_argument("--db", default=None, help="another database (default: Yuki's own)")
    parser.add_argument("--log-dir", default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="list the routines (read-only)")
    group.add_argument("--report", action="store_true",
                       help="mine now on a temporary copy and print every key's numbers (nothing written)")
    parser.add_argument("--at", default=None, help="with --report: mine as of this local time (ISO)")
    parser.add_argument("--dry-run", action="store_true", help="run the pass but write nothing")
    args = parser.parse_args(argv)
    from pathlib import Path

    if args.show:
        return show(args.db)
    if args.report:
        from yuki.eval.dbcopy import working_copy

        at = datetime.fromisoformat(args.at).timestamp() if args.at else None
        with working_copy(Path(args.db) if args.db else None) as (store, _path):
            print(json.dumps(report(store, at=at), indent=1, default=str))
        return 0
    from yuki.memory.store import Store

    store = Store.open(args.db)
    try:
        log_dir = Path(args.log_dir) if args.log_dir else (Path(args.db).resolve().parent / "logs" if args.db else None)
        log = None
        if log_dir is not None:
            def log(type: str, **fields: Any) -> None:
                from yuki.log.events import _as_plain

                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / f"portrait-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "type": type, **fields}, default=_as_plain) + "\n")
        res = RoutineMiner(store=store, log=log).run(reason="by hand", dry_run=args.dry_run)
        print(json.dumps({"ok": res.ok, "applied": res.applied, "decisions": res.decisions, "mined": res.mined,
                          "cost_usd": 0.0, "error": res.error}, indent=1, default=str))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
