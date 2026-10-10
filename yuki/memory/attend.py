"""The one attention policy for every card (jCore, state-design.md §4.4).

Every card maker used to carry its own copy of "in a game?", "on a call?", "quiet hours?", "too soon after the last
card?" (about 15 holds and budgets in ``nudges.py``, ``checkins.py``, ``presence.py``, ``core/attention.py``,
``routine_offers.py``, ``review.py`` and ``journal.py``). This module is the one place those rules live for the card
judge (:mod:`yuki.memory.cardjudge`). Code only: every input is a number, an enum or a timestamp, and every decision
is content-free (``reason`` like ``"H3:call"``, ``"B1:gap"``, plus when to look again).

**Input**: a YouNow snapshot (:mod:`yuki.memory.younow`), the current time and the user's switches
(:func:`read_switches`, fresh reads). **Output**: per candidate, :class:`Decision` ``allow`` or a hold with its reason
and ``retry_at``.

**Staleness is checked when read**: :func:`yuki.memory.younow.holds_now` answers ``["unknown"]`` when the snapshot's
last beat is older than ``STALE_S`` (5 s) or there is no snapshot; every class is then held (``H1:stale``). A stalled
loop never leaves a stale "no game" in force.

**Hard holds** (:func:`holds`, every class, in this order; the old gate each replaces in brackets):

* H0 user switches - ``[nudges] enabled = false``, memory paused, the session locked, "Quiet for 1 hour" (snoozed)
  [``NudgeWorker.gate``, ``Attention.snooze``, ``review.py``'s own gate];
* H1 a game in front (``focus.game.front``), or the 120 s return hold after a game or full-screen stretch; a dead gate
  falls back to the open stretch's program (YouNow's ``source = "fallback"``, as ``nudges._game_front``); an unknown
  game fact holds [observe's holds, ``RETURN_HOLD_S``, ``RoutineLane.holds``];
* H2 something full screen (unknown holds) [observe, ``Attention`` facts];
* H3 a call open (kind ``call``; a game's voice chat with the game behind is not a call, D8). An unreadable call
  status **fails closed** for every class; after :data:`CALL_UNKNOWN_PASS_S` unknown, ``reminder`` and ``asked`` pass
  when the timeline shows no meeting [``_in_call``, ``_call_open``, ``presence._open_calls``, ``checkins._in_call``,
  ``RoutineLane.holds``];
* H4 a meeting: its app's audio (``meeting_audio``, unknown holds) holds every class; the meeting in front (or its
  five-minute tail) holds every class except ``routine`` (rLane: the Meet tab in front is the routine's own
  pre-state) [``_meeting_held``, ``RoutineLane.holds``];
* H5 Yuki acting for the user [observe];
* H6 away; not settled (``settling``: under ``break_min`` of presence since the return). ``routine`` needs only
  ``routine_settle_s`` (30 s, rLane D11) [observe, ``RoutineLane.holds``];
* H7 quiet hours, every class including ``reminder`` and ``asked`` (Q5); equal ends mean none (the user's setting
  today) [``in_quiet_hours``, ``review.py``];
* H8 a card showing (one at a time; ``routine`` never jumps one) [``Attention.card_shown``, ``_attention``].

**Soft budget** (:func:`budget`, :func:`writer_budget`, :func:`type_on`):

* B1 non-deadline cards: at most :data:`B1_PER_DAY` a day, at least :data:`B1_GAP_S` after any card, counted from a
  :class:`History` (stored rows when live, :class:`RowHistory`; the judge's own shadow book in shadow). The Today card
  does not count toward the day's number (it keeps the gap); the "While you were away" list does.
* B2 deadline and asked cards (a reminder at its time, the routine in its window, standing requests) skip B1; they obey
  H0-H8. ``routine`` keeps its ``routine_per_day``; ``asked`` keeps journal's own cooldown and daily budget (applied when
  journal wrote the row).
* B3 the user's toggles map a type to off (:func:`type_on`): ``checkins_enabled``, ``questions_enabled``,
  ``presence_enabled``, ``routine_offers``, ``resume_enabled``. ``offers = "off"`` means no offers on any card
  (:func:`offers_on`), never a type off - except the routine card, whose whole point is its offer (rLane).
* B4 writer calls: at most :data:`WRITER_PER_HOUR` an hour, :data:`WRITER_GAP_S` apart, :data:`WRITER_PER_DAY` a day;
  the per-moment caps (``LOOK_CAPS``) apply on top, in the judge.

**The commit-time check** (:func:`commit_check`): just before a card would be written, the game in front and the call
worker's open calls are read once more, fresh, outside the snapshot.

Public API::

    Decision(allow, reason=None, retry_at=None);  ALLOW
    Switches;  read_switches(store, db_path, config, now, *, locked_fn=None) -> Switches
    holds(you, now, sw, config, *, type, urgency) -> Decision          # H0-H8 for one class
    common_hold(you, now, sw, config, *, routine=False) -> Decision | None   # a hold no class jCore makes escapes
    budget(history, now, config, *, type, urgency) -> Decision         # B1, B2
    type_on(type, config) -> bool;  offers_on(config) -> bool           # B3
    writer_budget(calls, now) -> Decision                              # B4
    commit_check(you, now, *, game_fn=None, calls_fn=None) -> Decision
    History (protocol);  RowHistory(store, config)                     # stored rows (live, jSwitch)
    user_day_start(now, config) -> float
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from yuki.memory.younow import holds_now

#: After this long with the call status unknown, a reminder or a standing request passes H3 (when no meeting shows).
CALL_UNKNOWN_PASS_S = 10 * 60.0
#: B1: non-deadline cards a day, and the gap after any card.
B1_PER_DAY = 4
B1_GAP_S = 20 * 60.0
#: B4: writer calls an hour, the gap between two, a day.
WRITER_PER_HOUR = 6
WRITER_GAP_S = 90.0
WRITER_PER_DAY = 10
#: Types whose cards skip B1 (B2) by their urgency.
B2_URGENCIES: tuple[str, ...] = ("deadline", "asked")
#: Types that never count toward B1's day number (they keep its gap).
B1_FREE_TYPES: tuple[str, ...] = ("today",)

#: B3: the user's toggles (``[nudges]``) and the types each one turns off.
_TOGGLES: dict[str, tuple[str, ...]] = {
    "checkins_enabled": ("checkin:",),
    "questions_enabled": ("question",),
    "presence_enabled": ("stuck", "recap", "followup", "plans", "thread"),
    "routine_offers": ("routine",),
    "resume_enabled": ("away",),
}


@dataclass(frozen=True)
class Decision:
    """Whether a card of one class may go now; if not, why (content-free) and when to look again (None: the next
    hold change or breakpoint wakes the judge)."""

    allow: bool
    reason: str | None = None
    retry_at: float | None = None


ALLOW = Decision(True)


@dataclass(frozen=True)
class Switches:
    """The user's own switches, read fresh (H0, H7)."""

    disabled: bool = False
    paused: bool = False
    locked: bool = False
    snoozed_until: float | None = None
    quiet: bool = False
    quiet_end_at: float | None = None


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def quiet_end(now: float, config: Any) -> float | None:
    """When the quiet hours holding ``now`` end (epoch s), or None outside them / with none set."""
    try:
        start, end = _minutes(config.quiet_start), _minutes(config.quiet_end)
    except (AttributeError, ValueError):
        return None
    if start == end:
        return None
    t = datetime.fromtimestamp(now)
    cur = t.hour * 60 + t.minute
    inside = start <= cur < end if start < end else (cur >= start or cur < end)
    if not inside:
        return None
    at = t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=end)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


def read_switches(store: Any, db_path: Any, config: Any, now: float, *,
                  locked_fn: Callable[[], bool] | None = None) -> Switches:
    """H0 and H7, fresh: ``[nudges] enabled``, memory's pause flag (a file), the session lock (``locked_fn``), the
    snooze (``nudge_state('quiet_until')``), the quiet hours. A read that fails holds (fails closed) for the pause and
    the lock only when it raised; never raises."""
    from yuki.memory.nudges import in_quiet_hours
    from yuki.memory.store import PAUSE_FLAG, flag_path

    disabled = not bool(getattr(config, "enabled", True))
    try:
        paused = bool(db_path is not None and flag_path(db_path, PAUSE_FLAG).exists())
    except Exception:  # noqa: BLE001
        paused = True
    locked = False
    if locked_fn is not None:
        try:
            locked = bool(locked_fn())
        except Exception:  # noqa: BLE001
            locked = False   # as NudgeWorker.gate: an unreadable lock is not a hold (the timeline's own state is)
    snoozed = None
    try:
        raw = store.nudge_state("quiet_until")
        if raw is not None and float(raw) > now:
            snoozed = float(raw)
    except (TypeError, ValueError):
        snoozed = None
    except Exception:  # noqa: BLE001
        snoozed = None
    try:
        quiet = bool(in_quiet_hours(now, config))
    except Exception:  # noqa: BLE001
        quiet = False
    return Switches(disabled=disabled, paused=paused, locked=locked, snoozed_until=snoozed, quiet=quiet,
                    quiet_end_at=quiet_end(now, config) if quiet else None)


def _get(you: Any, *path: str) -> Any:
    node = you
    for key in path:
        if not node:
            return None
        try:
            node = node.get(key)
        except AttributeError:
            return None
    return node


def holds(you: Any, now: float, sw: Switches, config: Any, *, type: str, urgency: str = "any") -> Decision:
    """H0-H8 for one class (``type`` = the candidate's ledger type, ``urgency`` = deadline | asked | any), staleness
    checked when read. The first hold that applies, in the order of the module docstring."""
    if sw.disabled:
        return Decision(False, "H0:disabled")
    if sw.paused:
        return Decision(False, "H0:paused")
    if sw.locked:
        return Decision(False, "H0:locked")
    if sw.snoozed_until is not None and sw.snoozed_until > now:
        return Decision(False, "H0:snoozed", sw.snoozed_until)
    held = set(holds_now(you, now))
    if "unknown" in held or "focus_unknown" in held:
        return Decision(False, "H1:stale")   # every source unknown: hold (never a stale "no game")
    focus = _get(you, "focus") or {}
    if "game" in held:
        return Decision(False, "H1:game")
    if "game_unknown" in held:
        return Decision(False, "H1:game_unknown")
    if "return_hold" in held:
        return Decision(False, "H1:return_hold", _get(focus, "game", "return_hold_until"))
    if "fullscreen" in held or "fullscreen_unknown" in held:
        return Decision(False, "H2:fullscreen")
    if "call" in held:
        return Decision(False, "H3:call")
    meeting = bool(held & {"meeting", "meeting_audio", "meeting_audio_unknown"})
    if "call_unknown" in held:
        since = focus.get("call_unknown_since")
        exempt = type in ("reminder", "asked") or urgency == "asked"
        long_unknown = since is not None and now - float(since) >= CALL_UNKNOWN_PASS_S
        if not (exempt and long_unknown and not meeting):
            retry = float(since) + CALL_UNKNOWN_PASS_S if exempt and since is not None and not long_unknown else None
            return Decision(False, "H3:call_unknown", retry)
    if "meeting_audio" in held or "meeting_audio_unknown" in held:
        return Decision(False, "H4:meeting_audio")
    if "meeting" in held and type != "routine":
        return Decision(False, "H4:meeting", _get(focus, "meeting", "front_until"))
    if "yuki_acting" in held:
        return Decision(False, "H5:yuki_acting")
    if "locked" in held:
        return Decision(False, "H0:locked")
    if "presence_unknown" in held:
        return Decision(False, "H6:presence_unknown")
    if "away" in held:
        return Decision(False, "H6:away")
    if "settling" in held:
        since = _get(you, "presence", "at_pc_since")
        if type == "routine":
            settle = float(getattr(config, "routine_settle_s", 30.0))
            if since is None or now - float(since) < settle:
                return Decision(False, "H6:settling", float(since) + settle if since is not None else None)
        else:
            return Decision(False, "H6:settling", _get(you, "presence", "settled_at"))
    if sw.quiet:
        return Decision(False, "H7:quiet_hours", sw.quiet_end_at)
    if "card_showing" in held:
        return Decision(False, "H8:card_showing", _get(you, "cards", "showing", "until"))
    return ALLOW


def common_hold(you: Any, now: float, sw: Switches, config: Any, *, routine: bool = False) -> Decision | None:
    """A hold that every class the judge could make right now shares, read from the snapshot and the switches before
    any database read (most wakes during a game or a call stop here). None when some class may pass: no hold, or the
    call status long unknown with no meeting (a reminder or standing request may then pass), or - with ``routine``
    possible - a meeting in front or a settling stretch (the routine's exceptions)."""
    d = holds(you, now, sw, config, type="*", urgency="any")
    if d.allow:
        return None
    if d.reason == "H3:call_unknown":
        exempt = holds(you, now, sw, config, type="reminder", urgency="deadline")
        if exempt.allow:
            return None
        if exempt.reason != "H3:call_unknown":
            return exempt   # past the call hold, another hold still holds every class
    if routine and d.reason in ("H4:meeting", "H6:settling"):
        return None
    return d


def user_day_start(now: float, config: Any) -> float:
    """When the user's day holding ``now`` began (days start at ``[nudges] resume_day_start``)."""
    day_start = getattr(config, "resume_day_start", "04:00")
    shift = timedelta(minutes=_minutes(day_start))
    t = datetime.fromtimestamp(now) - shift
    return (t.replace(hour=0, minute=0, second=0, microsecond=0) + shift).timestamp()


class History(Protocol):
    """What B1 and B4 count (stored rows when live; the judge's own shadow book in shadow)."""

    def last_card_at(self, now: float) -> float | None: ...

    def counted_today(self, now: float, config: Any) -> int: ...

    def shown_today(self, now: float, config: Any, type: str) -> int: ...

    def writer_calls(self, now: float) -> list[float]: ...


def budget(history: History, now: float, config: Any, *, type: str, urgency: str = "any") -> Decision:
    """B1 and B2: deadline and asked cards skip the day's number and the gap (the routine keeps its
    ``routine_per_day``); every other card waits :data:`B1_GAP_S` after any card, and at most :data:`B1_PER_DAY` of
    them a day (the Today card keeps the gap but is not counted)."""
    if urgency in B2_URGENCIES:
        if type == "routine":
            per_day = int(getattr(config, "routine_per_day", 2))
            if history.shown_today(now, config, "routine") >= per_day:
                return Decision(False, "B2:routine_day", user_day_start(now, config) + 86400.0)
        return ALLOW
    last = history.last_card_at(now)
    if last is not None and now - last < B1_GAP_S:
        return Decision(False, "B1:gap", last + B1_GAP_S)
    if type not in B1_FREE_TYPES and history.counted_today(now, config) >= B1_PER_DAY:
        return Decision(False, "B1:day", user_day_start(now, config) + 86400.0)
    return ALLOW


def type_on(type: str, config: Any) -> bool:
    """B3: whether the user's toggles leave this type on (the user's word beats the ledger)."""
    for toggle, prefixes in _TOGGLES.items():
        if any(type == p or (p.endswith(":") and type.startswith(p)) for p in prefixes):
            if not bool(getattr(config, toggle, True)):
                return False
    if type == "routine" and not offers_on(config):
        return False   # rLane: the routine card is its offer; offers = "off" stops it
    return True


def offers_on(config: Any) -> bool:
    """B3: ``[nudges] offers = "off"`` means no offers on any card (``allowed_dos(enabled=False)``), never a type off."""
    return str(getattr(config, "offers", "yuki_only")) != "off"


def writer_budget(calls: Iterable[float], now: float) -> Decision:
    """B4: at most :data:`WRITER_PER_HOUR` writer calls an hour, :data:`WRITER_GAP_S` apart, :data:`WRITER_PER_DAY` in
    24 h (``calls``: when each was made)."""
    times = sorted(float(t) for t in calls if t is not None and float(t) <= now)
    if not times:
        return ALLOW
    if now - times[-1] < WRITER_GAP_S:
        return Decision(False, "B4:gap", times[-1] + WRITER_GAP_S)
    hour = [t for t in times if now - t < 3600.0]
    if len(hour) >= WRITER_PER_HOUR:
        return Decision(False, "B4:hour", hour[0] + 3600.0)
    day = [t for t in times if now - t < 86400.0]
    if len(day) >= WRITER_PER_DAY:
        return Decision(False, "B4:day", day[0] + 86400.0)
    return ALLOW


def commit_check(you: Any, now: float, *, game_fn: Callable[[], bool | None] | None = None,
                 calls_fn: Callable[[], Sequence[Any]] | None = None) -> Decision:
    """The commit-time check: the game in front (``game_fn``: the live gate's ``focus_game()["front"]``, None when the
    gate is dead - the snapshot's fallback then decides) and the open calls (``calls_fn``: the store's open call rows),
    read once, fresh, just before a card would be written. Fails closed on a read that raises."""
    game: bool | None = None
    if game_fn is not None:
        try:
            game = game_fn()
        except Exception:  # noqa: BLE001
            return Decision(False, "commit:game_unknown")
    if game is None:
        game = _get(you, "focus", "game", "front")
    if game is None:
        return Decision(False, "commit:game_unknown")
    if game:
        return Decision(False, "commit:game")
    if calls_fn is not None:
        try:
            calls = list(calls_fn() or [])
        except Exception:  # noqa: BLE001
            return Decision(False, "commit:call_unknown")
        if any(getattr(c, "kind", "call") != "game" for c in calls):
            return Decision(False, "commit:call")
    return ALLOW


class RowHistory:
    """B1 / B4 from stored rows (content-free columns): the judge's own cards (``nudges.trigger`` = its type) and its
    writer calls (``nudge_checkins.trigger`` = ``judge:<type>``). For ``[judge] mode = "on"`` (jSwitch)."""

    def __init__(self, store: Any, types: Iterable[str], b1_free: Iterable[str] = B1_FREE_TYPES) -> None:
        self.store = store
        self.types = tuple(types)
        self.b1_free = tuple(b1_free)

    def _rows(self, since: float, now: float) -> list[Any]:
        if not self.types:
            return []
        marks = ",".join("?" * len(self.types))
        return list(self.store._query(
            "SELECT at, trigger FROM nudges WHERE at >= ? AND at < ? AND trigger IN (%s) ORDER BY at" % marks,
            (since, now + 1.0, *self.types)))

    def last_card_at(self, now: float) -> float | None:
        rows = self.store._query("SELECT max(at) FROM nudges WHERE at < ?", (now + 1.0,))
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def counted_today(self, now: float, config: Any) -> int:
        from yuki.memory.candidates import TYPES

        return sum(1 for r in self._rows(user_day_start(now, config), now)
                   if r[1] not in self.b1_free and TYPES.get(r[1]) is not None
                   and TYPES[r[1]].urgency not in B2_URGENCIES)

    def shown_today(self, now: float, config: Any, type: str) -> int:
        return sum(1 for r in self._rows(user_day_start(now, config), now) if r[1] == type)

    def writer_calls(self, now: float) -> list[float]:
        rows = self.store._query("SELECT at FROM nudge_checkins WHERE at >= ? AND trigger LIKE 'judge:%'"
                                 " AND model IS NOT NULL", (now - 86400.0,))
        return [float(r[0]) for r in rows]


__all__ = [
    "ALLOW", "B1_GAP_S", "B1_PER_DAY", "CALL_UNKNOWN_PASS_S", "Decision", "History", "RowHistory", "Switches",
    "WRITER_GAP_S", "WRITER_PER_DAY", "WRITER_PER_HOUR", "budget", "commit_check", "common_hold", "holds",
    "offers_on", "quiet_end", "read_switches", "type_on", "user_day_start", "writer_budget",
]
