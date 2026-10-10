"""Preferences: the user's usual for a skill's slot (PLAN.md Phase S rules; map-skills S4). Read skeleton (sCore).

Tables ``prefs`` / ``pref_events`` (migration 41). A preference slot is ``"<skill>:<slot>[@<scope>]"``
(``volume:control@spotify``, ``youtube.home:browser``); an option is a value of the connection's option list or a
taste entity key (a keyed digest, never a name in clear). The quote behind ``said`` / ``corrected`` is encrypted.

**The choice rule** (:func:`choose`, code only):

1. a ``usual`` option (the user's statement, confirmed once with "Keep this as your usual?") wins until at least 3
   later observed choices in the same context contradict it (then sVoice's single "still your usual?" card goes out);
2. otherwise the top observed option when its share is at least 0.8 with an effective count of at least 3 (observed
   events decay with a 30-day half-life);
3. otherwise the most recent choice in this context, **named aloud** when used, so one word corrects it.

A cancel or slowness counts against the route (``skill_runs``), never against a preference. The write side here is
the minimum the memory API's ``keep_usual`` needs; the voice builder (sVoice) owns the rest of the body (``said`` from
the user's words, corrections, the conversation extractor's ``usual`` field, the intake backfill).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

#: Observed choices decay with this half-life (days).
HALF_LIFE_DAYS = 30.0
#: Rule 2's bar.
SHARE = 0.8
MIN_N = 3.0
#: Rule 1: later observed choices against a usual that make it stop winning.
CONTRADICT_N = 3
#: Event types and their default weights (a correction weighs more than a sighting).
EVENT_WEIGHTS: dict[str, float] = {"observed": 1.0, "said": 1.0, "confirmed": 1.0, "declined": 0.0,
                                   "corrected": 3.0, "rejected": -1.0}


@dataclass(frozen=True)
class Choice:
    """What :func:`choose` picked: the option, which rule (``usual`` / ``observed`` / ``recent``), whether it must be
    named aloud (it was a guess), and the measured share and effective count behind it."""

    option: str
    source: str
    named: bool
    share: float = 0.0
    n: float = 0.0
    still_usual: bool = False      # rule 1 was outvoted: the "still your usual?" question is due


def _decay(age_s: float) -> float:
    return math.pow(0.5, max(0.0, age_s) / (HALF_LIFE_DAYS * 86400.0))


def choose(store: Any, slot: str, *, context: str = "", now: float | None = None,
           until: float | None = None) -> Choice | None:
    """The option to use for ``slot`` in ``context`` (``until``: as of that time, for replays), or ``None`` when no
    choice is known. Read only; never raises."""
    now = time.time() if now is None else float(now)
    hi = float(until) if until is not None else now + 1.0
    try:
        prefs = store._query(
            "SELECT id, option, state, confirmed_at, last_at FROM prefs WHERE slot = ? AND context = ? AND "
            "ended_at IS NULL", (slot, context))
        if not prefs:
            return None
        ids = [int(p[0]) for p in prefs]
        marks = ",".join("?" * len(ids))
        events = store._query(
            f"SELECT pref_id, at, type, weight FROM pref_events WHERE pref_id IN ({marks}) AND at < ? ORDER BY at",
            (*ids, hi))
    except Exception:  # noqa: BLE001
        return None
    option_of = {int(p[0]): str(p[1]) for p in prefs}
    observed: dict[str, float] = {}
    latest: tuple[float, str] | None = None
    for pid, at, typ, weight in events:
        opt = option_of.get(int(pid))
        if opt is None:
            continue
        if typ in ("observed", "said", "corrected", "confirmed"):
            if latest is None or float(at) >= latest[0]:
                latest = (float(at), opt)
        if typ == "observed":
            observed[opt] = observed.get(opt, 0.0) + float(weight) * _decay(now - float(at))
    usual = [p for p in prefs if str(p[2]) == "usual" and (p[3] is None or float(p[3]) < hi)]
    still = False
    if usual:
        u = max(usual, key=lambda p: float(p[3] or 0.0))
        confirmed = float(u[3] or 0.0)
        against = sum(1 for pid, at, typ, _w in events if typ == "observed" and float(at) > confirmed
                      and option_of.get(int(pid)) != str(u[1]))
        if against < CONTRADICT_N:
            return Choice(option=str(u[1]), source="usual", named=False)
        still = True
    total = sum(v for v in observed.values() if v > 0)
    if total > 0:
        top, weight = max(observed.items(), key=lambda kv: kv[1])
        share = weight / total
        if share >= SHARE and round(total, 3) >= MIN_N:   # today's events decay by a hair: 3 of them are 3
            return Choice(option=top, source="observed", named=False, share=round(share, 3), n=round(total, 2),
                          still_usual=still)
    if latest is not None:
        return Choice(option=latest[1], source="recent", named=True, n=round(total, 2), still_usual=still)
    return None


def usual(store: Any, slot: str, *, context: str = "") -> str | None:
    """The confirmed usual of ``slot``, if any (read only)."""
    try:
        rows = store._query("SELECT option FROM prefs WHERE slot = ? AND context = ? AND state = 'usual' AND "
                            "ended_at IS NULL ORDER BY confirmed_at DESC LIMIT 1", (slot, context))
    except Exception:  # noqa: BLE001
        return None
    return str(rows[0][0]) if rows else None


def record(store: Any, slot: str, option: str, kind: str, *, context: str = "", weight: float | None = None,
           source: str | None = None, quote: str | None = None, at: float | None = None) -> int | None:
    """One preference event (``observed`` / ``said`` / ``confirmed`` / ``declined`` / ``corrected`` / ``rejected``),
    its preference row upserted. Returns the event id (``None`` on failure). The quote is encrypted."""
    if kind not in EVENT_WEIGHTS:
        raise ValueError(f"unknown preference event {kind!r}")
    at = time.time() if at is None else float(at)
    w = EVENT_WEIGHTS[kind] if weight is None else float(weight)
    quote_ct = store._enc(quote) if quote else None
    counters = {"said": "said", "corrected": "corrected", "rejected": "rejected"}

    def write(conn: Any) -> int:
        conn.execute("INSERT OR IGNORE INTO prefs (slot, option, context, state, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'seen', ?, ?)", (slot, option, context, at, at))
        pid = int(conn.execute("SELECT id FROM prefs WHERE slot = ? AND option = ? AND context = ?",
                               (slot, option, context)).fetchone()[0])
        col = counters.get(kind)
        if col:
            conn.execute(f"UPDATE prefs SET {col} = {col} + 1 WHERE id = ?", (pid,))
        if kind == "said":
            conn.execute("UPDATE prefs SET state = CASE WHEN state = 'usual' THEN state ELSE 'proposed' END, "
                         "quote_ciphertext = coalesce(?, quote_ciphertext) WHERE id = ?", (quote_ct, pid))
        elif kind == "confirmed":
            # one usual per slot and context: the others step down to seen
            conn.execute("UPDATE prefs SET state = 'seen' WHERE slot = ? AND context = ? AND state = 'usual' AND "
                         "id != ?", (slot, context, pid))
            conn.execute("UPDATE prefs SET state = 'usual', confirmed_at = ? WHERE id = ?", (at, pid))
        elif kind == "declined":
            conn.execute("UPDATE prefs SET state = CASE WHEN state = 'proposed' THEN 'seen' ELSE state END "
                         "WHERE id = ?", (pid,))
        conn.execute("UPDATE prefs SET last_at = ?, updated_at = ? WHERE id = ?", (at, at, pid))
        cur = conn.execute("INSERT INTO pref_events (at, pref_id, type, weight, source, quote_ciphertext) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (at, pid, kind, w, source, quote_ct))
        return int(cur.lastrowid)

    try:
        return store._write(write)
    except Exception:  # noqa: BLE001
        return None


def keep_usual(store: Any, slot: str, option: str, answer: str, *, context: str = "", quote: str | None = None,
               source: str | None = None) -> dict[str, Any]:
    """The answer to "Keep this as your usual?": ``yes`` confirms the option as the usual; ``just_this_time`` declines
    (the choice stays one observation). Content-free result."""
    if answer not in ("yes", "just_this_time"):
        return {"ok": False, "why": "answer"}
    if answer == "yes":
        got = record(store, slot, option, "confirmed", context=context, quote=quote, source=source)
    else:
        got = record(store, slot, option, "declined", context=context, source=source)
        if got is not None:
            record(store, slot, option, "observed", context=context, source=source)
    return {"ok": got is not None, "state": "usual" if answer == "yes" else "seen"}


__all__ = ["HALF_LIFE_DAYS", "SHARE", "MIN_N", "CONTRADICT_N", "EVENT_WEIGHTS", "Choice", "choose", "usual", "record",
           "keep_usual"]
