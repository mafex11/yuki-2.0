"""The routine lane: one "Your usual" card at the user's usual moment, written by code (PLAN.md Phase R; D10, D11, D14,
D17, D18; map-routines R2).

The miner (:mod:`yuki.memory.routines`, phase R ``rMiner``, keyed by phase L's learned skills) keeps the user's
measured usual moments in the ``routines`` table (map-routines R1.2: ``key`` ``skill:<learned skill id>``,
``skill_id`` the learned skill's key, ``trigger``, ``dayset``, ``at_min``, ``hits`` / ``eligible``, ``state``,
``offer_mode``, a told ``label`` and bound ``params`` encrypted). This lane turns an active routine into one card in
its usual window, with no model and no cost:

* **due** (:meth:`RoutineLane.due`): an active routine of a skill, ``offer_mode`` ``early`` or ``late``, today in its
  day-set, now inside the mode's window (``early``: ``routine_lead_min`` to ``routine_lead_last_min`` before the usual
  time, 8 to 1 min; ``late``: 1 to 3 min after it), its end state not reached today (the user reached the learned
  skill's end: ``routines.skill_onsets``, or a skill run of it that ended done), not offered today, fewer than
  ``routine_per_day`` routine cards
  today. ``offer-now <id> [--in N]`` (:func:`request_offer_now`, a request file the worker reads each pass) makes one
  routine due at once, through the same card gates (it skips only the window and the day-set; its card counts
  against the day's 2, never uses up the routine's once-a-day slot, and is never learned from but for an explicit
  Less of these: its events say ``forced``);
* **held by itself (D11)**: it passes only the coach's gates None, ``settling`` (once ``routine_settle_s`` past
  sitting down) and ``meeting``, and holds whatever the gate says on a game or a full screen in front, the 120 s return
  hold, any open call (a game's voice chat with the game behind is no call here: D8, map-routines R2.3), the last
  meeting's app still playing (meeting audio), away, and Yuki acting. Screen sharing happens inside a call (D10). The
  coarse gates (off, paused, locked, snoozed, quiet hours) give it no view at all. A calls or meeting-audio read that
  fails counts as a hold (fail closed);
* **the shared gates** (:mod:`yuki.core.attention`, channel ``routine``): at most 2 a day; it skips the 10-minute card
  gap (critique M20) and moves it for every other card. A card on screen is the tray's to see (memory learns of it
  only when it fades): the tray's queue shows one card at a time, and a routine card that waited asks
  :func:`card_still_due` before it shows;
* **the offer** (:func:`yuki.memory.offers.routine_offer`): the learned skill is loaded and passes the offer gate
  (``offers.skill_offerable``: never sends or commits, rung A or a verified B, its capability's ``offer_class``, a name
  for a ``judged`` one, its Won't words known), its route trusted (``yuki.skills.registry.offer_ready``, D14), and
  available: for a site tab (``web_route.site_tab``) the tab census of its bound service
  (:class:`yuki.memory.callevidence.SiteTabs`, at most once a day per routine; one tab found by identity, never two,
  never an unreadable strip). Not available: no card that day;
* **the card**: kind ``notice``, trigger ``routine``, heading "Your usual" (the UI), its text from fixed templates per
  end kind ("Your 9:00 meeting is coming up." late: "... started a minute ago ...") and the learned name as the
  question ("<name>?"; a nameless skill gets its end kind's fixed question), the why line from the counts ("You've
  joined it around 09:00 on 7 of the last 9 weekdays.") and the "Won't" line from the capability's ``wont`` words or
  its commit word (``offers.SKILL_WONT_LINES``). Buttons are today's: Open it, Not now, Less of these.
  It expires ``routine_expire_after_min`` after the usual time, or at once when the end state lands. The tray asks
  :func:`card_still_due` right before it shows a card that waited in its queue (expiry, end state, outcome, an open
  call, a game in front) and drops or keeps it;
* **learning** (:meth:`RoutineLane.learn`, :func:`feedback`; B.3), in ``routine_events``: Open it and done
  -> ``accepted`` (``joined`` when the join followed within 10 min); Not now -> ``declined``; shown or not, no
  reaction -> ``ignored``; pressed but refused (no trusted route any more, gone, expired) -> ``accepted`` with
  ``run: refused``; joined within 15 min without a card it showed -> also ``self_served``; Less of these -> a mode step
  (early -> late -> quiet; on quiet, off); >= 3 ``self_served`` in 10 days with <= 1 accept -> late; >= 3 ignores or
  declines of the late card in 10 days, or acceptance under 30% after 10 meeting days -> quiet (pre-declared). A run
  the PC couldn't confirm counts against the route only (the skill ledger), never against the routine.

Logs (content-free, the coach's log and the memory log): ``routine_offer`` (routine id, skill key, mode, lead s,
forced), ``routine_held`` (the reason, on change), ``tab_census``. The tables are never in the MCP (D13).

Public API::

    RoutineLane(worker).tick(now, config, view, gate) -> dict | None;  .due(...);  .learn(now, config)
    routines(store) / routine(store, id) -> Routine;  record_event(store, id, kind, at=, nudge_id=, detail=)
    feedback(store, nudge_id, kind, now=None) -> dict;  why_line(r);  card_text(r, skill, mode)
    card_still_due(store, nudge_id, now=None, game=None) -> {"due", "why", "final"}
    request_offer_now(db_path, routine_id, in_s=0) -> Path;  read_request(db_path) -> dict | None
    python -m yuki.memory.routine_offers offer-now <id> [--in N] [--db PATH] [--wait S] | status [--db PATH]
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: The card's trigger and the attention channel (yuki.memory.offers.ROUTINE_CHANNEL).
TRIGGER = "routine"
#: The late variant's window around the usual time (minutes after it): "started a minute ago".
LATE_FROM_MIN = 1.0
LATE_TO_MIN = 3.0
#: After a card: the join within this long makes an accepted card ``joined``; within the longer one, the user
#: joining without the card is ``self_served`` (B.3).
JOINED_WITHIN_S = 10 * 60.0
SELF_SERVED_WITHIN_S = 15 * 60.0
#: A pressed card whose run never reported back (the tray closed mid-run) settles as pressed after this long (s).
RUN_STUCK_S = 10 * 60.0
#: The mode ladder (B.3): its window, and its thresholds.
LADDER_WINDOW_S = 10 * 86400.0
LATE_AFTER_SELF_SERVED = 3
LATE_MAX_ACCEPTS = 1
QUIET_AFTER_LATE_MISSES = 3
QUIET_MEETING_DAYS = 10
QUIET_BELOW_ACCEPTANCE = 0.3
#: The modes, in ladder order (``off``: "don't offer this", for good).
MODES: tuple[str, ...] = ("early", "late", "quiet", "off")
#: How far back the learning pass looks at routine cards, and how often it runs (s).
LEARN_BACK_S = 36 * 3600.0
LEARN_EVERY_S = 60.0
#: The request file ``offer-now`` writes next to the database, read by the worker each pass.
REQUEST_FILE = "routine_offer_now.json"
#: An ``offer-now`` request waits at most this long past its time for the card gates (s).
REQUEST_WAIT_S = 10 * 60.0
#: Event kinds of ``routine_events`` this lane writes (map-routines R1.2).
EVENT_KINDS: tuple[str, ...] = ("offered", "held", "accepted", "run_ok", "run_failed", "declined", "ignored",
                                "self_served", "never", "mode_change")
#: A card's one settle event (written once per card).
SETTLE_KINDS: tuple[str, ...] = ("accepted", "declined", "ignored")

#: The card's words per end kind of the skill's habit (its capability's served end: ``routines.served_kind``), fixed
#: templates; a skill whose end kind has none is never offered (fail closed). ``{at}``: the usual time to the quarter
#: hour ("9:00"); ``{when}``: the measured median ("09:05"); ``{label}``: the name the user gave the routine. ``ask``:
#: the question when the learned skill has no real name (``offers.card_question``: its name otherwise).
ONSET_WORDS: dict[str, dict[str, str]] = {
    "call_join": {
        "early": "Your {at} meeting is coming up.",
        "late": "Your {at} meeting started a minute ago.",
        "named_early": "{label} is at {at}.",
        "named_late": "{label} started a minute ago.",
        "why": "You've joined it around {when} on {hits} of the last {eligible} {days}.",
        "ask": "Bring its tab forward?",
    },
    "media_context": {
        "early": "Your usual music around {at}.",
        "late": "Your usual music around {at}.",
        "named_early": "Your usual {label}?",
        "named_late": "Your usual {label}?",
        "why": "You've put it on around {when} on {hits} of the last {eligible} {days}.",
        "ask": "Put it on?",
    },
    "page": {
        "early": "Your usual around {at}.",
        "late": "Your usual around {at}.",
        "named_early": "{label} around {at}.",
        "named_late": "{label} around {at}.",
        "why": "You've opened it around {when} on {hits} of the last {eligible} {days}.",
        "ask": "Open it?",
    },
}
#: The day-set's words in the why line (the miner's fixed calendar menu: weekdays, weekends, all, one weekday).
DAY_WORDS: dict[str, str] = {"weekdays": "weekdays", "weekends": "weekend days", "all": "days", "mon": "Mondays",
                             "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays", "fri": "Fridays",
                             "sat": "Saturdays", "sun": "Sundays"}
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------------------------------------------------------------
# The tables (rMiner's migration; read and written here through the store's own connection)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Routine:
    """One row of ``routines`` (the told label and bound params decrypted, in RAM only)."""

    id: int
    key: str
    skill_id: str | None
    trigger: str
    dayset: str
    at_min: float | None
    mad_min: float | None = None
    hits: int = 0
    eligible: int = 0
    rate: float | None = None
    state: str = "candidate"
    offer_mode: str = "early"
    label: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    last_hit_at: float | None = None

    @property
    def offering(self) -> bool:
        """An active routine of a skill, on a clock (or a told clock time), in an offering mode."""
        return (self.state == "active" and bool(self.skill_id) and self.offer_mode in ("early", "late")
                and self.trigger in ("clock", "told") and self.at_min is not None)


_tables: dict[str, bool] = {}


def has_tables(store: Any) -> bool:
    """Whether the routine tables exist in this database (rMiner's migration). Checked once per database and
    process: both processes apply pending migrations when they open it."""
    key = str(getattr(store, "path", id(store)))
    hit = _tables.get(key)
    if hit is None:
        try:
            names = {r[0] for r in store._query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('routines', 'routine_events')")}
            hit = names == {"routines", "routine_events"}
        except Exception:
            hit = False
        _tables[key] = hit
    return hit


def _dec(store: Any, text: Any) -> str | None:
    try:
        return store.cipher.decrypt(text) if text else None
    except Exception:
        return None


def _routine(store: Any, r: Any) -> Routine:
    keys = r.keys()
    params: dict[str, Any] = {}
    raw = _dec(store, r["params_ciphertext"]) if "params_ciphertext" in keys else None
    if raw:
        try:
            got = json.loads(raw)
            params = got if isinstance(got, dict) else {}
        except ValueError:
            params = {}
    return Routine(id=int(r["id"]), key=str(r["key"] or ""), skill_id=r["skill_id"] or None,
                   trigger=str(r["trigger"] or ""), dayset=str(r["dayset"] or ""),
                   at_min=float(r["at_min"]) if r["at_min"] is not None else None,
                   mad_min=float(r["mad_min"]) if "mad_min" in keys and r["mad_min"] is not None else None,
                   hits=int(r["hits"] or 0) if "hits" in keys else 0,
                   eligible=int(r["eligible"] or 0) if "eligible" in keys else 0,
                   rate=float(r["rate"]) if "rate" in keys and r["rate"] is not None else None,
                   state=str(r["state"] or ""), offer_mode=str(r["offer_mode"] or "early"),
                   label=_dec(store, r["label_ciphertext"]) if "label_ciphertext" in keys else None,
                   params=params,
                   last_hit_at=float(r["last_hit_at"]) if "last_hit_at" in keys and r["last_hit_at"] is not None
                   else None)


def routines(store: Any, *, offering: bool = True) -> list[Routine]:
    """The routines (``offering``: only those that may be offered now: active, of a skill, early or late), by id."""
    if not has_tables(store):
        return []
    sql = "SELECT * FROM routines"
    if offering:
        sql += (" WHERE state='active' AND skill_id IS NOT NULL AND offer_mode IN ('early', 'late')"
                " AND at_min IS NOT NULL")
    try:
        rows = store._query(sql + " ORDER BY id")
    except sqlite3.Error:
        return []
    out = [_routine(store, r) for r in rows]
    return [r for r in out if r.offering] if offering else out


def routine(store: Any, routine_id: Any) -> Routine | None:
    """One routine by id, or None."""
    if routine_id is None or not has_tables(store):
        return None
    try:
        rows = store._query("SELECT * FROM routines WHERE id=?", (int(routine_id),))
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return _routine(store, rows[0]) if rows else None


def record_event(store: Any, routine_id: int, kind: str, *, at: float | None = None, nudge_id: int | None = None,
                 detail: dict[str, Any] | None = None) -> int | None:
    """One ``routine_events`` row (``detail``: content-free JSON: code words and numbers). Returns its id."""
    if kind not in EVENT_KINDS or not has_tables(store):
        return None
    when = float(at) if at is not None else time.time()

    def op(conn: Any) -> int:
        _insert_event(conn, routine_id, kind, when, nudge_id, detail)
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    try:
        return int(store._write(op))
    except sqlite3.Error:
        return None


def events(store: Any, *, routine_id: int | None = None, since: float | None = None, nudge_id: int | None = None,
           kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """``routine_events`` rows (``detail`` parsed), oldest first."""
    if not has_tables(store):
        return []
    clauses, params = [], []
    if routine_id is not None:
        clauses.append("routine_id = ?")
        params.append(int(routine_id))
    if since is not None:
        clauses.append("at >= ?")
        params.append(float(since))
    if nudge_id is not None:
        clauses.append("nudge_id = ?")
        params.append(int(nudge_id))
    if kinds is not None:
        kinds = list(kinds)
        clauses.append(f"kind IN ({','.join('?' * len(kinds))})" if kinds else "0")
        params.extend(kinds)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = store._query("SELECT * FROM routine_events" + where + " ORDER BY at, id", params)
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except ValueError:
            detail = {}
        out.append({"id": int(r["id"]), "routine_id": int(r["routine_id"]), "at": float(r["at"]),
                    "kind": str(r["kind"]), "nudge_id": r["nudge_id"], "detail": detail})
    return out


def _insert_event(conn: Any, routine_id: int, kind: str, at: float, nudge_id: int | None,
                  detail: dict[str, Any] | None) -> None:
    """One ``routine_events`` row inside the caller's transaction."""
    body = json.dumps(detail, separators=(",", ":"), sort_keys=True) if detail else None
    conn.execute("INSERT INTO routine_events(routine_id, at, kind, nudge_id, detail) VALUES (?,?,?,?,?)",
                 (int(routine_id), float(at), kind, nudge_id, body))


def set_mode(store: Any, routine_id: int, mode: str, *, at: float | None = None, why: str = "",
             nudge_id: int | None = None, only_from: str | None = None) -> bool:
    """Move a routine's ``offer_mode`` (the miner keeps it when it updates the row) and record ``mode_change`` (or
    ``never`` for ``off``), the read, the update and the event in one transaction (the tray's Less of these and the
    learning pass never step it twice). ``only_from``: only while the routine is still in that mode (the ladder's
    reading). False when the routine is gone, already in that mode, or no longer in ``only_from``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if not has_tables(store):
        return False
    when = float(at) if at is not None else time.time()

    def op(conn: Any) -> bool:
        row = conn.execute("SELECT offer_mode FROM routines WHERE id=?", (int(routine_id),)).fetchone()
        if row is None:
            return False
        cur = str(row[0] or "early")
        if cur == mode or (only_from is not None and cur != only_from):
            return False
        conn.execute("UPDATE routines SET offer_mode=?, updated_at=? WHERE id=?", (mode, when, int(routine_id)))
        _insert_event(conn, routine_id, "never" if mode == "off" else "mode_change", when, nudge_id,
                      {"from": cur, "to": mode, "why": why})
        return True

    try:
        return bool(store._write(op))
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------------------------------------------------
# Time: the routine's day, its usual time and its windows
# ---------------------------------------------------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm or "04:00").split(":")
    return int(h) * 60 + int(m)


def routine_day(now: float, day_start: str = "04:00") -> date:
    """The day ``now`` belongs to: days start at ``day_start`` (04:00), as the miner's days do."""
    return (datetime.fromtimestamp(now) - timedelta(minutes=_minutes(day_start))).date()


def day_start_at(now: float, day_start: str = "04:00") -> float:
    """When ``now``'s day began (epoch s)."""
    d = routine_day(now, day_start)
    return datetime.combine(d, datetime.min.time()).timestamp() + _minutes(day_start) * 60.0


def usual_at(r: Routine, day: date, day_start: str = "04:00") -> float:
    """The routine's usual time on ``day`` (epoch s): ``at_min`` minutes into that calendar date, or the next one for
    a time before the day's start (after midnight)."""
    d = day if float(r.at_min or 0.0) >= _minutes(day_start) else day + timedelta(days=1)
    return datetime.combine(d, datetime.min.time()).timestamp() + float(r.at_min or 0.0) * 60.0


def in_dayset(dayset: str, day: date) -> bool:
    """The miner's fixed calendar menu: weekdays, weekends, all, or one weekday (``mon`` .. ``sun``)."""
    wd = day.weekday()
    if dayset == "weekdays":
        return wd < 5
    if dayset == "weekends":
        return wd >= 5
    if dayset == "all":
        return True
    return dayset in _WEEKDAYS and _WEEKDAYS.index(dayset) == wd


def window(r: Routine, day: date, config: Any, mode: str | None = None) -> tuple[float, float, float]:
    """``(start, end, usual time)`` of the routine's offer window on ``day`` in ``mode`` (default its own)."""
    at = usual_at(r, day, getattr(config, "resume_day_start", "04:00"))
    if (mode or r.offer_mode) == "late":
        return at + LATE_FROM_MIN * 60.0, at + LATE_TO_MIN * 60.0, at
    return (at - float(getattr(config, "routine_lead_min", 8.0)) * 60.0,
            at - float(getattr(config, "routine_lead_last_min", 1.0)) * 60.0, at)


# ---------------------------------------------------------------------------------------------------------------------
# The words (fixed templates, numbers and the told label filled by code)
# ---------------------------------------------------------------------------------------------------------------------


def _clock(minutes: float, *, quarter: bool) -> str:
    m = int(round(minutes / 15.0) * 15) if quarter else int(round(minutes))
    m %= 1440
    return f"{m // 60}:{m % 60:02d}" if quarter else f"{m // 60:02d}:{m % 60:02d}"


def _onset_kind(skill: Any) -> str | None:
    """The end kind of the skill's habit: an old ``routine_on`` pair's kind, else its capability's served end."""
    pairs = list(getattr(skill, "routine_on", ()) or ())
    if pairs:
        return str(pairs[0][0])
    from yuki.memory.routines import served_kind

    return served_kind(skill) or None


def _words(skill: Any) -> dict[str, str] | None:
    kind = _onset_kind(skill)
    return ONSET_WORDS.get(kind) if kind else None


def why_line(r: Routine, skill: Any = None) -> str:
    """The card's why line from the routine's counts ("You've joined it around 09:00 on 7 of the last 9 weekdays.").
    Without the skill (or for an onset kind with no words) the meeting wording is the fallback."""
    words = (_words(skill) if skill is not None else None) or ONSET_WORDS["call_join"]
    hits = max(0, int(r.hits))
    eligible = max(hits, int(r.eligible or 0))
    return words["why"].format(when=_clock(float(r.at_min or 0.0), quarter=False), hits=hits, eligible=eligible,
                               days=DAY_WORDS.get(r.dayset, "days"))


def card_question(store: Any, skill: Any) -> str:
    """The card's question for ``skill``: its learned name as a question, else its end kind's fixed ``ask`` words
    (``offers.card_question``); ``""`` when its end kind has no words (never offered)."""
    words = _words(skill)
    if words is None:
        return ""
    from yuki.memory import offers

    return offers.card_question(store, skill, fallback=words.get("ask", ""))


def card_text(r: Routine, skill: Any, mode: str, question: str = "") -> str | None:
    """The card's text: the template of the skill's end kind and mode, then the card's question (``question``: the
    learned name or the fixed ``ask`` words, :func:`card_question`; an old skill's ``preview`` otherwise); None when
    there are no words for it (never offered)."""
    words = _words(skill)
    preview = " ".join(str(question or getattr(skill, "preview", "") or "").split())
    if words is None or not preview:
        return None
    at = _clock(float(r.at_min or 0.0), quarter=True)
    label = " ".join(str(r.label or "").split())
    head = (words["named_late" if mode == "late" else "named_early"].format(label=label, at=at) if label
            else words["late" if mode == "late" else "early"].format(at=at))
    return f"{head} {preview}"


def routine_title(store: Any, r: Routine) -> str:
    """A routine's name for a resolved ref: the told label, else "Your usual"."""
    return " ".join(str(r.label or "").split())[:80] or "Your usual"


# ---------------------------------------------------------------------------------------------------------------------
# Availability: the skill's own target is there (a site tab census), read only when a card is about to be written
# ---------------------------------------------------------------------------------------------------------------------


def _site_tab(store: Any, route: Any, log: Callable[..., Any] | None) -> bool | None:
    """A site tab (``web_route.site_tab``): the census finds exactly one tab of the route's ``service`` (the learned
    skill's bound value, filled into the route's arguments by the catalog) by identity (D8). Not learned yet, no
    window of its browser, none or two found: False; nothing readable: None."""
    service = str((getattr(route, "args", None) or {}).get("service") or "")
    if not service:
        return None
    from yuki.memory.callevidence import SiteTabs

    tabs = SiteTabs(None, store=store, log=log)
    ident = tabs.identities(service)
    if not (ident.get("runtime_ids") or ident.get("names") or ident.get("positions")):
        return False
    result = tabs.census(service)
    if not int(result.get("windows") or 0):
        return False
    if not int(result.get("tabs") or 0):
        return None
    return str(result.get("decision") or "") == "one" and bool(result.get("chosen"))


#: The availability check per (driver, call) of a skill's rung A: plumbing on the routes' own data, not a skill list.
#: A skill whose rung A has none here is available (its own run says plainly when it can't).
AVAILABILITY: dict[tuple[str, str], Callable[[Any, Any, Callable[..., Any] | None], bool | None]] = {
    ("web_route", "site_tab"): _site_tab,
}


def available(store: Any, skill: Any, log: Callable[..., Any] | None = None) -> bool | None:
    """Whether the skill's target is there now (the first of its rung-A routes that has a check)."""
    for route in getattr(skill, "routes", ()) or ():
        if str(getattr(route, "rung", "")) != "A":
            continue
        check = AVAILABILITY.get((str(getattr(route, "driver", "") or ""), str(getattr(route, "call", "") or "")))
        if check is not None:
            return check(store, route, log)
    return True


# ---------------------------------------------------------------------------------------------------------------------
# offer-now: a request file the worker reads each pass (the real card gates live in the service process)
# ---------------------------------------------------------------------------------------------------------------------


def request_path(db_path: str | Path | None) -> Path:
    from yuki.memory.store import flag_path

    return flag_path(db_path, REQUEST_FILE)


def request_offer_now(db_path: str | Path | None, routine_id: int, in_s: float = 0.0, *,
                      now: float | None = None) -> Path:
    """Ask the memory service for the routine's card in ``in_s`` seconds, through the real card gates (``routines
    offer-now <id> [--in N]``; the card counts against the day's 2 routine cards and is never learned from but for
    an explicit Less of these). The worker answers in the same file (``state``: pending, shown, expired, budget,
    unknown_routine, ``not_offering:...``, off, or the offer's own refusal such as not_ready / unavailable, plus the
    last hold ``reason``). Returns the file."""
    now = time.time() if now is None else float(now)
    at = now + max(0.0, float(in_s))
    path = request_path(db_path)
    _write_request(path, {"routine_id": int(routine_id), "made_at": now, "at": at, "until": at + REQUEST_WAIT_S,
                          "state": "pending"})
    return path


def read_request(db_path: str | Path | None) -> dict[str, Any] | None:
    """The request file's content, or None."""
    try:
        got = json.loads(request_path(db_path).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def _write_request(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Due:
    """A routine due now: in ``mode``, its usual time ``at``, its window's end, and whether ``offer-now`` forced it."""

    routine: Routine
    skill: Any
    mode: str
    at: float
    until: float
    forced: bool = False


class RoutineLane:
    """The routine card in ``NudgeWorker.tick`` (see the module docstring). ``worker``: the
    :class:`yuki.memory.nudges.NudgeWorker` (its store, log, attention, calls and meeting-audio reads).

    ``skills``: the skill loader (default :func:`yuki.skills.connection.load`); ``ready`` replaces D14's
    ``offer_ready`` and ``available`` the availability check (tests, replays that say so)."""

    def __init__(self, worker: Any, *, skills: Callable[[], Any] | None = None,
                 ready: Callable[[Any, Any], bool] | None = None,
                 available: Callable[[Any, Any], bool | None] | None = None) -> None:
        self.w = worker
        self._skills = skills
        self._ready = ready
        self._available = available
        #: The last hold reason logged per routine (``routine_held`` is logged on change only).
        self._held: dict[int, str] = {}
        #: (routine id, day, reason) held events already written (one row each a day).
        self._held_rows: set[tuple[int, str, str]] = set()
        #: (routine id, day) -> the day's census answer (at most one census a day per routine; offer-now asks again).
        self._census: dict[tuple[int, str], bool | None] = {}
        self._learned_at = 0.0
        self._request_mtime: float | None = None
        self._request: dict[str, Any] | None = None

    @property
    def store(self) -> Any:
        return self.w.store

    def _log(self, type: str, **fields: Any) -> None:
        try:
            self.w._log(type, **fields)
        except Exception:
            pass

    def _loaded(self) -> Any:
        if self._skills is not None:
            return self._skills()
        from yuki.skills.catalog import load

        # the memory service's own store: its learned rows (one count query when unchanged; phase L C.2), a skipped
        # file or row told to the lane's log once
        return load(store=self.store, log=self._log)

    def _skill(self, key: str | None) -> Any:
        if not key:
            return None
        try:
            return self._loaded().get(key)
        except Exception:
            return None

    # -- one pass ------------------------------------------------------------------------------------------------------

    def tick(self, now: float, config: Any, view: Any, gate: str | None) -> dict[str, Any] | None:
        """One pass: the learning upkeep, then at most one card (``{"trigger": "routine", "nudge_id", ...}``)."""
        if not has_tables(self.store):
            return None
        try:
            self.learn(now, config)
        except Exception as exc:
            self._log("nudge_error", where="routine_learn", error=f"{type(exc).__name__}: {exc}")
        request = self._read_request(now)
        if not getattr(config, "routine_offers", True) or getattr(config, "offers", "yuki_only") == "off":
            if request is not None:
                self._finish_request(request, "off")
            return None
        due = self.due(now, config, request=request)
        if not due:
            return None
        d = due[0]
        if view is None:   # a coarse gate (locked, snoozed, quiet hours): nothing shows, a request keeps waiting
            self._hold(d, now, config, str(gate or "gate"))
            return None
        why = self.holds(now, config, view, gate)
        if why is None:
            why = self._attention(now, config, d.forced)
        if why is not None:
            self._hold(d, now, config, why)
            return None
        return self._write(d, now, config)

    # -- what is due ---------------------------------------------------------------------------------------------------

    def _today_cards(self, now: float, config: Any) -> list[Any]:
        """Today's routine cards (since the day's start), oldest first."""
        start = day_start_at(now, getattr(config, "resume_day_start", "04:00"))
        try:
            ids = [int(r[0]) for r in self.store._query(
                "SELECT id FROM nudges WHERE kind='notice' AND trigger=? AND at >= ? AND at <= ? ORDER BY at, id",
                (TRIGGER, start, now + 1.0))]
        except sqlite3.Error:
            return []
        return [n for n in (self.store.nudge(i) for i in ids) if n is not None]

    def done_today(self, r: Routine, skill: Any, now: float, config: Any, since: float | None = None) -> float | None:
        """When the routine's end state was reached today (the user reached its learned skill's end, or a skill run of
        it ended done: :func:`first_onset`), or None. ``since`` narrows the start (a card's time)."""
        start = since if since is not None else day_start_at(now, getattr(config, "resume_day_start", "04:00"))
        return first_onset(self.store, skill, start, now, include_runs=True, entity=_entity(r))

    def due(self, now: float, config: Any, *, request: dict[str, Any] | None = None) -> list[Due]:
        """The routines due now (see the module docstring), by window start; a forced one (``offer-now``) first."""
        out: list[Due] = []
        day_start = getattr(config, "resume_day_start", "04:00")
        day = routine_day(now, day_start)
        if request is not None:
            r = routine(self.store, request.get("routine_id"))
            if r is None or not r.skill_id:
                self._finish_request(request, "unknown_routine")
            elif not r.offering:
                # a candidate, dormant or retired routine, or one the user quieted or switched off: no card
                self._finish_request(request, f"not_offering:{r.state}:{r.offer_mode}")
            else:
                at = usual_at(r, day, day_start)
                out.append(Due(r, self._skill(r.skill_id), r.offer_mode, at,
                               float(request.get("until") or now + REQUEST_WAIT_S), forced=True))
        # in their window first (no card read at all outside every window)
        windowed = []
        for r in routines(self.store):
            if not in_dayset(r.dayset, day) or any(d.routine.id == r.id for d in out):
                continue
            start, end, at = window(r, day, config)
            if start <= now <= end:
                windowed.append((r, at, end))
        if not windowed:
            return out
        cards = self._today_cards(now, config)
        # once a day per routine: its natural card; an offer-now card (a test) never uses up the day's slot, but no
        # second card comes while one of the routine's cards is still live (unanswered, not expired). Every card on
        # screen counts against the day's budget, offer-now's included (Phase R: "its own limit of 2 a day")
        offered = {int((n.inputs or {}).get("routine_id") or 0) for n in cards
                   if not (n.inputs or {}).get("forced") or _live(n, now)}
        if len(cards) >= int(getattr(config, "routine_per_day", 2)):
            return out
        for r, at, end in windowed:
            if r.id in offered:
                continue
            skill = self._skill(r.skill_id)
            if skill is None or self.done_today(r, skill, now, config) is not None:
                continue
            out.append(Due(r, skill, r.offer_mode, at, end))
        out.sort(key=lambda d: (not d.forced, d.at))
        return out

    # -- D11: holds of its own, whatever the gate says -----------------------------------------------------------------

    def holds(self, now: float, config: Any, view: Any, gate: str | None) -> str | None:
        """Why the lane may not show a card now (D11), or None. The coach's gate passes only None, settling and
        meeting; then it holds by itself on each screen hold (``view.holds``), any open call and the meeting audio."""
        held = set(getattr(view, "holds", ()) or ())
        for reason in ("yuki_acting", "game", "fullscreen", "return_hold", "away"):
            if reason in held:
                return reason
        if gate not in (None, "settling", "meeting"):
            return str(gate)
        if self.w._call_open(fail_closed=True):   # a read that failed counts as a call
            return "call"
        if self.w._meeting_held(now, view, config, fail_closed=True):
            return "meeting_audio"
        if "settling" in held or gate == "settling":
            since = getattr(view, "at_pc_since", None)
            if since is None or now < since + float(getattr(config, "routine_settle_s", 30.0)) \
                    or float(getattr(view, "present_recent_s", 0.0)) <= 0.0:
                return "settling"
        return None

    def _attention(self, now: float, config: Any, forced: bool) -> str | None:
        """The shared gates (channel ``routine``): a card showing, the day's budget of 2 (``offer-now`` cards
        included: it goes through the real card gates)."""
        try:
            att = self.w._attention(now, config)
            att.load(TRIGGER, [n.at for n in self._today_cards(now, config)
                               if n.at >= now - 86400.0])
            decision = att.check(TRIGGER, now=now, facts={})
        except Exception:
            return "attention:error"   # fail closed: never a card that might jump one showing
        if decision.allowed:
            return None
        return f"attention:{decision.reason}"

    def _hold(self, d: Due, now: float, config: Any, reason: str) -> None:
        rid = d.routine.id
        if self._held.get(rid) != reason:
            self._held[rid] = reason
            self._log("routine_held", routine=rid, skill=d.routine.skill_id, reason=reason, mode=d.mode,
                      forced=d.forced)
        day = routine_day(now, getattr(config, "resume_day_start", "04:00")).isoformat()
        if (rid, day, reason) not in self._held_rows:
            self._held_rows.add((rid, day, reason))
            if len(self._held_rows) > 256:
                self._held_rows = {k for k in self._held_rows if k[1] == day}
            record_event(self.store, rid, "held", at=now, detail={"reason": reason, "mode": d.mode,
                                                                  "forced": d.forced})
        if d.forced and self._request is not None:
            if reason == "attention:budget":
                # the day's 2 routine cards are spent: final for this request (the screen holds come first, so a
                # call or a game in front still says "call" / "game" while it lasts)
                self._request["reason"] = reason
                self._finish_request(self._request, "budget")
            elif now > float(self._request.get("until") or 0.0):
                self._request["reason"] = reason
                self._finish_request(self._request, "expired")
            elif self._request.get("reason") != reason:   # the CLI's status shows why it waits (written on change)
                self._request["reason"] = reason
                self._save_request(self._request)

    # -- the card ------------------------------------------------------------------------------------------------------

    def _census_for(self, d: Due, now: float, config: Any) -> Callable[[Any], bool | None]:
        day = routine_day(now, getattr(config, "resume_day_start", "04:00")).isoformat()

        def check(skill: Any) -> bool | None:
            key = (d.routine.id, day)
            if key in self._census and not d.forced:
                return self._census[key]
            if self._available is not None:
                got = self._available(self.store, skill)
            else:
                got = available(self.store, skill, log=self._log)
            self._census[key] = got
            if len(self._census) > 64:
                self._census = {k: v for k, v in self._census.items() if k[1] == day}
            return got

        return check

    def _write(self, d: Due, now: float, config: Any) -> dict[str, Any] | None:
        from yuki.memory import offers
        from yuki.memory.store import NewNudge, NudgeCheckin

        r, skill = d.routine, d.skill
        why_not = None
        question = card_question(self.store, skill) if skill is not None else ""
        if skill is None:
            why_not = "no_skill"
        elif (text := card_text(r, skill, d.mode, question)) is None:
            why_not = "no_words"
        elif "run_skill" not in offers.allowed_dos(self.store, offers.ROUTINE_CHANNEL, now,
                                                   enabled=getattr(config, "offers", "yuki_only") != "off"):
            why_not = "turned_down"   # the per-kind learning: Less of these / Undo kept coming
        offer = None
        if why_not is None:
            why = why_line(r, skill)
            expire = float(getattr(config, "routine_expire_after_min", 10.0)) * 60.0
            expires_at = (now if d.forced else d.at) + expire
            routine = r
            params = routine_params(self.store, r, skill)
            if params and not r.params:
                from dataclasses import replace

                routine = replace(r, params=params)   # the routine's own entity (a playlist): RAM and the sealed card
            offer, why_not = offers.routine_offer(self.store, routine, now, expires_at=max(expires_at, now + 60.0),
                                                  why=why, available=self._census_for(d, now, config),
                                                  ready=self._ready, skills=self._loaded, question=question)
        if offer is None:
            self._hold(d, now, config, str(why_not))
            if d.forced and self._request is not None:
                # offer-now: past the screen gates, the offer itself said no (no trusted route yet, no tab...):
                # the answer is final for this request (never a census every pass while it waits)
                self._finish_request(self._request, str(why_not))
            return None
        lead = round(d.at - now)
        checkin = NudgeCheckin(at=now, trigger=TRIGGER, outcome="notice", model=None,
                               reason=f"routine R{r.id} ({r.key}) in its usual window, mode {d.mode}")
        nudge = NewNudge(kind="notice", text=text, reason=why, trigger=TRIGGER, ref=f"R{r.id}",
                         inputs={"offer": offer, "routine_id": r.id, "mode": d.mode, "lead_s": lead,
                                 "forced": d.forced, "at": d.at})
        checkin_id, nudge_id = self.store.record_checkin(checkin, nudge)
        if nudge_id is None:
            self._hold(d, now, config, "not_written")
            return None
        try:
            self.w._event.set()
        except Exception:
            pass
        record_event(self.store, r.id, "offered", at=now, nudge_id=nudge_id,
                     detail={"mode": d.mode, "lead_s": lead, "forced": d.forced})
        self._held.pop(r.id, None)
        self._log("routine_offer", routine=r.id, skill=r.skill_id, mode=d.mode, lead_s=lead, forced=d.forced,
                  nudge_id=nudge_id, checkin_id=checkin_id)
        try:
            self.w._log_checkin(checkin_id, checkin, nudge_id)
        except Exception:
            pass
        if d.forced and self._request is not None:
            self._finish_request(self._request, "shown", nudge_id=nudge_id)
        return {"trigger": TRIGGER, "nudge_id": nudge_id, "routine": r.id, "mode": d.mode, "text": text,
                "offer": offer, "reason": why, "lead_s": lead, "forced": d.forced, "outcome": "notice",
                "cost_usd": 0.0}

    # -- offer-now -----------------------------------------------------------------------------------------------------

    def _read_request(self, now: float) -> dict[str, Any] | None:
        """The pending ``offer-now`` request whose time has come (read again only when the file changed)."""
        path = request_path(getattr(self.w, "db_path", None))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._request, self._request_mtime = None, None
            return None
        if mtime != self._request_mtime:
            self._request_mtime = mtime
            try:
                got = json.loads(path.read_text(encoding="utf-8"))
                self._request = got if isinstance(got, dict) else None
            except (OSError, ValueError):
                self._request = None
        req = self._request
        if req is None or req.get("state") != "pending":
            return None
        if now > float(req.get("until") or 0.0):
            self._finish_request(req, "expired")
            return None
        return req if now >= float(req.get("at") or 0.0) else None

    def _save_request(self, req: dict[str, Any]) -> None:
        path = request_path(getattr(self.w, "db_path", None))
        try:
            _write_request(path, req)
            self._request_mtime = path.stat().st_mtime
        except OSError:
            pass

    def _finish_request(self, req: dict[str, Any], state: str, **fields: Any) -> None:
        req.update(state=state, finished_at=time.time(), **fields)
        self._save_request(req)
        self._log("routine_offer_now", routine=req.get("routine_id"), state=state, reason=req.get("reason"),
                  nudge_id=fields.get("nudge_id"))

    # -- learning (B.3) ------------------------------------------------------------------------------------------------

    def learn(self, now: float, config: Any) -> None:
        """Settle the routine cards of the last day and a half (at most every :data:`LEARN_EVERY_S`): expire a pending
        one whose end state landed, a Less of these steps the mode at once, then once its time is over one settle event
        (accepted / declined / ignored, plus self_served), and the mode ladder."""
        if now - self._learned_at < LEARN_EVERY_S:
            return
        self._learned_at = now
        try:
            ids = [int(r[0]) for r in self.store._query(
                "SELECT id FROM nudges WHERE kind='notice' AND trigger=? AND at >= ? ORDER BY at, id",
                (TRIGGER, now - LEARN_BACK_S))]
        except sqlite3.Error:
            return
        if not ids:
            return
        touched: set[int] = set()
        for nid in ids:
            n = self.store.nudge(nid)
            if n is None:
                continue
            rid = int((n.inputs or {}).get("routine_id") or 0)
            r = routine(self.store, rid) if rid else None
            if r is None:
                continue
            if self._settle(n, r, now, config):
                touched.add(rid)
        for rid in touched:
            self.ladder(rid, now, config)

    def _settle(self, n: Any, r: Routine, now: float, config: Any) -> bool:
        """One card's outcome (idempotent through its events). True when something was recorded."""
        from yuki.memory import offers

        offer = offers.stored_offer(n) or {}
        recorded = {e["kind"]: e for e in events(self.store, nudge_id=n.id)}
        skill = self._skill(r.skill_id)
        forced = bool((n.inputs or {}).get("forced"))
        changed = False
        if n.reaction == "less" and not any(e["kind"] in ("mode_change", "never")
                                            and e["detail"].get("why") == "less" for e in recorded.values()):
            feedback(self.store, n.id, "less", now=now)   # an explicit press counts, on an offer-now card too
            changed = True
        joined_at = (first_onset(self.store, skill, n.at, min(now, n.at + SELF_SERVED_WITHIN_S), entity=_entity(r))
                     if skill else None)
        if n.outcome is None and joined_at is not None and n.reaction not in ("accepted",):
            # the end state landed while the card waited: its button stops (expires when the join lands), and a card
            # never shown leaves the pending list (the tray asks again before it shows a queued one: card_still_due)
            offers._set_outcome(self.store, n.id, "expired", at=now, only_if=(None,))
            _expire_unshown(self.store, n.id, now)
        if any(k in recorded for k in SETTLE_KINDS):
            return changed
        expires = float(offer.get("expires_at") or (n.at + 600.0))
        if now < max(expires, n.at + SELF_SERVED_WITHIN_S):
            return changed
        if n.outcome == "running" and now - float(n.outcome_at or n.at) < RUN_STUCK_S:
            return changed   # the tray is still reporting (a run that never reported settles as pressed)
        mode = str((n.inputs or {}).get("mode") or offer.get("mode") or "")
        detail: dict[str, Any] = {"mode": mode}
        if forced:
            # an offer-now card (the user's test): recorded, but never in the ladder or the acceptance rule
            detail["forced"] = True
        if n.outcome in ("done", "failed", "running"):
            # a run the PC couldn't confirm counts against the route only (an Undo after it keeps it "failed")
            kind = "accepted"
            detail["run"] = n.outcome
            run_at = float(n.outcome_at or n.at)
            detail["joined"] = bool(joined_at is not None and joined_at - run_at <= JOINED_WITHIN_S)
        elif n.outcome == "undone" or n.reaction in ("dismissed", "less"):
            kind = "declined"
            detail["how"] = "undone" if n.outcome == "undone" else str(n.reaction)
        elif n.reaction == "accepted":
            # pressed, but memory refused the run (no trusted route any more, the routine gone, its own expiry) or the
            # press never reached it: the user said yes; nothing ran. Never a miss
            kind = "accepted"
            detail["run"] = "refused"
            press_at = float(n.reacted_at or n.at)
            detail["joined"] = bool(joined_at is not None and joined_at - press_at <= JOINED_WITHIN_S)
        else:
            kind = "ignored"
            detail["seen"] = n.shown_at is not None
        record_event(self.store, r.id, kind, at=now, nudge_id=n.id, detail=detail)
        if kind != "accepted" and joined_at is not None and n.shown_at is not None:
            # joined without the card, only for a card the screen showed (one never shown says nothing about it)
            served: dict[str, Any] = {"mode": mode, "after_s": round(joined_at - n.at)}
            if forced:
                served["forced"] = True
            record_event(self.store, r.id, "self_served", at=now, nudge_id=n.id, detail=served)
        return True

    def ladder(self, routine_id: int, now: float, config: Any) -> str | None:
        """The pre-declared mode ladder (B.3) over the last 10 days; returns the new mode, if it moved. Cards forced
        by ``offer-now`` (the user's tests) never count here (their explicit Less of these still does, at once)."""
        r = routine(self.store, routine_id)
        if r is None or r.offer_mode not in ("early", "late"):
            return None
        recent = [e for e in events(self.store, routine_id=routine_id, since=now - LADDER_WINDOW_S)
                  if not e["detail"].get("forced")]

        def count(kind: str, mode: str | None = None) -> int:
            # a card the screen never showed (busy all through its window) was not ignored by the user
            return sum(1 for e in recent if e["kind"] == kind and (mode is None or e["detail"].get("mode") == mode)
                       and not (kind == "ignored" and e["detail"].get("seen") is False))

        if r.offer_mode == "early" and count("self_served") >= LATE_AFTER_SELF_SERVED \
                and count("accepted") <= LATE_MAX_ACCEPTS:
            if set_mode(self.store, routine_id, "late", at=now, why="self_served", only_from="early"):
                self._log("routine_mode", routine=routine_id, mode_from="early", mode_to="late", why="self_served")
                return "late"
            return None
        if r.offer_mode == "late" and count("ignored", "late") + count("declined", "late") >= QUIET_AFTER_LATE_MISSES:
            if set_mode(self.store, routine_id, "quiet", at=now, why="late_ignored", only_from="late"):
                self._log("routine_mode", routine=routine_id, mode_from="late", mode_to="quiet", why="late_ignored")
                return "quiet"
            return None
        days, offered, accepted = self.acceptance(routine_id, now, config)
        if days >= QUIET_MEETING_DAYS and offered and accepted / offered < QUIET_BELOW_ACCEPTANCE:
            if set_mode(self.store, routine_id, "quiet", at=now, why="acceptance", only_from=r.offer_mode):
                self._log("routine_mode", routine=routine_id, mode_from=r.offer_mode, mode_to="quiet",
                          why="acceptance", days=days, offered=offered, accepted=accepted)
                return "quiet"
        return None

    def acceptance(self, routine_id: int, now: float, config: Any) -> tuple[int, int, int]:
        """``(meeting days, cards seen, cards accepted)`` since the routine's first natural card: meeting days are the
        days its end state happened (the user's own onsets), counted from the day of that card; a card the screen
        never showed is not counted as seen; cards forced by ``offer-now`` (the user's tests) never count."""
        first_card = [e for e in events(self.store, routine_id=routine_id, kinds=("offered",))
                      if not e["detail"].get("forced")]
        if not first_card:
            return 0, 0, 0
        first = first_card[0]["at"]
        settled = [e for e in events(self.store, routine_id=routine_id, kinds=SETTLE_KINDS)
                   if not e["detail"].get("forced")]
        accepted = sum(1 for e in settled if e["kind"] == "accepted")
        offered = sum(1 for e in settled if not (e["kind"] == "ignored" and e["detail"].get("seen") is False))
        r = routine(self.store, routine_id)
        skill = self._skill(r.skill_id) if r is not None else None
        day_start = getattr(config, "resume_day_start", "04:00")
        start = day_start_at(first, day_start)
        days = {routine_day(t, day_start) for t in onsets(self.store, skill, start, now)} if skill else set()
        return len(days), offered, accepted


# ---------------------------------------------------------------------------------------------------------------------
# The routine's end state: the user's own onsets (D3 as phase L restates it, D18: Yuki's runs never count)
# ---------------------------------------------------------------------------------------------------------------------


def routine_params(store: Any, r: Routine, skill: Any) -> dict[str, Any]:
    """The routine's own slot values for its card run: for a routine keyed by an entity (its capability's
    ``routine_key`` fill slot: a playlist), that entity's name from the user's latest moment of it (``moments
    .entity_key``; decrypted in RAM, sealed in the card's encrypted inputs, never logged). ``{}`` otherwise."""
    parts = str(r.key or "").split("|")
    slot = next((s for s in getattr(skill, "slots", ()) or () if s.role == "fill" and s.routine_key), None)
    if len(parts) < 2 or slot is None or not parts[1]:
        return {}
    try:
        from yuki.memory.routines import served_kind

        rows = store._query("SELECT entity_ciphertext FROM moments WHERE kind = ? AND entity_key LIKE ? AND"
                            " coalesce(by, 'user') = 'user' AND entity_ciphertext IS NOT NULL ORDER BY at DESC LIMIT 1",
                            (served_kind(skill), parts[1] + "%"))
        name = " ".join(str(store._dec(rows[0][0]) or "").split()) if rows else ""
    except Exception:
        name = ""
    return {slot.name: name} if name else {}


def _entity(r: Any) -> str | None:
    """A routine's one entity (a playlist routine's key part), else ``None``."""
    from yuki.memory.routines import key_entity

    return key_entity(getattr(r, "key", None))


def onsets(store: Any, skill: Any, since: float, until: float, *, app: str | None = None,
           entity: str | None = None) -> list[float]:
    """When the user reached the skill's end state, oldest first, never Yuki's own (D18): a learned skill's
    (:func:`yuki.memory.routines.skill_onsets`: its occasions and the user's matching moments; ``entity``: a
    ``routine_key`` routine's one entity, :func:`yuki.memory.routines.key_entity`), or an old skill's ``routine_on``
    (kind, app) moments."""
    if getattr(skill, "origin", "") == "learned":
        from yuki.memory.routines import skill_onsets

        try:
            return skill_onsets(store, skill, since, until, app=app, entity=entity)
        except Exception:
            return []
    pairs = [(str(k), str(a)) for k, a in (getattr(skill, "routine_on", ()) or ())]
    if not pairs:
        return []
    try:
        rows = store.moments_between(since, until + 1.0, kinds=sorted({k for k, _ in pairs}))
    except Exception:
        return []
    want = set(pairs)
    return [float(m["at"]) for m in rows if (str(m.get("kind")), str(m.get("app") or "")) in want
            and str(m.get("by") or "user") != "yuki"]


def first_onset(store: Any, skill: Any, since: float, until: float, *, include_runs: bool = False,
                entity: str | None = None) -> float | None:
    """The first time in ``[since, until]`` the routine's end state was reached: the user's own onset, or (with
    ``include_runs``) a run of the skill's habit that ended done (said "open my meeting" already; a usage record's
    capability runs too: :func:`yuki.memory.routines.habit_run_done`). ``entity``: a ``routine_key`` routine's one
    entity (a playlist): another one never counts."""
    got = onsets(store, skill, since, until, entity=entity)
    first = got[0] if got else None
    if include_runs and getattr(skill, "key", None):
        if getattr(skill, "origin", "") == "learned":
            from yuki.memory.routines import habit_run_done

            ran = habit_run_done(store, skill, since, until, entity=entity)
        else:
            try:
                rows = store._query("SELECT min(coalesce(t_done, t0)) FROM skill_runs WHERE skill=? AND t0 >= ? AND"
                                    " t0 <= ? AND outcome IN ('done', 'already')",
                                    (skill.key, float(since), float(until) + 1.0))
                ran = rows[0][0] if rows else None
            except sqlite3.Error:
                ran = None
        if ran is not None and (first is None or float(ran) < first):
            first = float(ran)
    return first


# ---------------------------------------------------------------------------------------------------------------------
# The user's buttons, from the UI (``MemoryClient.routine_feedback``) and the learning pass
# ---------------------------------------------------------------------------------------------------------------------


def feedback(store: Any, nudge_id: int, kind: str, *, now: float | None = None) -> dict[str, Any]:
    """The user's "Less of these" (``less``: one mode step, early -> late -> quiet, and on a quiet routine off) or a
    "don't offer this" (``never``: off) on a routine card. Once per card (a second call changes nothing):
    ``{"ok", "routine", "mode_from", "mode_to"}`` or ``{"ok": False, "why"}`` (``unknown``, ``not_routine``,
    ``gone``, ``already``, ``bad_kind``)."""
    now = time.time() if now is None else float(now)
    if kind not in ("less", "never"):
        return {"ok": False, "why": "bad_kind"}
    n = store.nudge(int(nudge_id))
    if n is None:
        return {"ok": False, "why": "unknown"}
    if n.kind != "notice" or n.trigger != TRIGGER:
        return {"ok": False, "why": "not_routine"}
    rid = int((n.inputs or {}).get("routine_id") or 0)
    if not rid or not has_tables(store):
        return {"ok": False, "why": "gone"}

    def op(conn: Any) -> dict[str, Any]:
        # the check, the step and its event in one transaction: the tray's press and the learning pass (which reads
        # the same reaction) never step the routine twice
        row = conn.execute("SELECT offer_mode FROM routines WHERE id=?", (rid,)).fetchone()
        if row is None:
            return {"ok": False, "why": "gone"}
        cur = str(row[0] or "early")
        for (body,) in conn.execute("SELECT detail FROM routine_events WHERE nudge_id=? AND kind IN"
                                    " ('mode_change', 'never')", (int(n.id),)).fetchall():
            try:
                why = (json.loads(body) if body else {}).get("why")
            except ValueError:
                why = None
            if why == kind:
                return {"ok": False, "why": "already", "routine": rid, "mode": cur}
        if kind == "never" or cur in ("quiet", "off"):
            to = "off"
        else:
            to = MODES[min(MODES.index(cur) + 1, MODES.index("quiet"))] if cur in MODES else "quiet"
        moved = to != cur
        if moved:
            conn.execute("UPDATE routines SET offer_mode=?, updated_at=? WHERE id=?", (to, now, rid))
        # already there: the press is still recorded once, so it is never applied again
        _insert_event(conn, rid, "never" if to == "off" else "mode_change", now, int(n.id),
                      {"from": cur, "to": to if moved else cur, "why": kind})
        return {"ok": True, "routine": rid, "mode_from": cur, "mode_to": to if moved else cur}

    try:
        return dict(store._write(op))
    except sqlite3.Error as exc:
        return {"ok": False, "why": "error", "error": type(exc).__name__}


def _live(n: Any, now: float) -> bool:
    """A routine card still live: no outcome, no reaction (queued or on screen), its own offer not expired."""
    if n.outcome is not None or n.reaction is not None:
        return False
    from yuki.memory import offers

    offer = offers.stored_offer(n) or {}
    return now <= float(offer.get("expires_at") or (n.at + 600.0))


def _expire_unshown(store: Any, nudge_id: int, now: float) -> None:
    """A card the screen never showed leaves the pending list (reaction ``expired``, as the coach's own expiry)."""
    try:
        store._write(lambda conn: conn.execute(
            "UPDATE nudges SET reaction='expired', reacted_at=? WHERE id=? AND shown_at IS NULL AND reaction IS NULL",
            (float(now), int(nudge_id))))
    except sqlite3.Error:
        pass


def card_still_due(store: Any, nudge_id: int, *, now: float | None = None,
                   game: Callable[[], str] | None = None) -> dict[str, Any]:
    """The tray's question right before it shows a queued routine card (it may have waited behind another card or a
    busy screen since the lane wrote it): ``{"due": True}`` or ``{"due": False, "why", "final"}``.

    Final (the tray drops the card): ``unknown``, an ``outcome`` or a ``reaction`` already recorded, its own expiry
    (``expired``), its routine no longer offering (``gone``: Less of these elsewhere, quieted, retired), the end state
    reached since the card (``done``: the user joined, or said "open my meeting"). Not final (the tray keeps it queued
    and asks again): an open call (``call``; a read that failed counts as one) or a game in front (``game``; D9's local
    read, ``game`` replaces it; a read that failed counts as one). Full screen is the tray's own check. Never raises."""
    now = time.time() if now is None else float(now)
    try:
        n = store.nudge(int(nudge_id))
    except Exception:   # unreadable now: not shown, asked again later
        return {"due": False, "why": "error", "final": False}
    if n is None:
        return {"due": False, "why": "unknown", "final": True}
    if n.kind != "notice" or n.trigger != TRIGGER:
        return {"due": True}
    if n.outcome is not None:
        return {"due": False, "why": f"outcome:{n.outcome}", "final": True}
    if n.reaction is not None:
        return {"due": False, "why": f"reaction:{n.reaction}", "final": True}
    from yuki.memory import offers

    offer = offers.stored_offer(n) or {}
    if now > float(offer.get("expires_at") or (n.at + 600.0)):
        return {"due": False, "why": "expired", "final": True}
    r = routine(store, (n.inputs or {}).get("routine_id"))
    if r is None or not r.offering or (offer.get("skill") and r.skill_id != offer.get("skill")):
        return {"due": False, "why": "gone", "final": True}
    try:
        from yuki.skills.catalog import load

        skill = load(store=store).get(str(r.skill_id))
    except Exception:
        skill = None
    if skill is not None and first_onset(store, skill, n.at, now, include_runs=True, entity=_entity(r)) is not None:
        return {"due": False, "why": "done", "final": True}
    try:
        from yuki.memory.store import CALL_OPEN_STATUSES

        in_call = any(getattr(c, "kind", "call") != "game" for c in store.calls(statuses=CALL_OPEN_STATUSES))
    except Exception:
        in_call = True   # fail closed: never a card into a call it couldn't rule out
    if in_call:
        return {"due": False, "why": "call", "final": False}
    try:
        if game is None:
            from yuki.memory.gamegate import game_in_front as game
        in_game = bool(game())
    except Exception:
        in_game = True   # fail closed
    if in_game:
        return {"due": False, "why": "game", "final": False}
    return {"due": True}


# ---------------------------------------------------------------------------------------------------------------------
# CLI: offer-now (the user's live check, PLAN.md Phase R) and the request's state
# ---------------------------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m yuki.memory.routine_offers",
                                     description="The routine card: ask for it now through the real card gates.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    now_p = sub.add_parser("offer-now", help="ask the memory service for a routine's card (the real card gates)")
    now_p.add_argument("routine_id", type=int)
    now_p.add_argument("--in", dest="in_s", type=float, default=0.0, help="seconds from now (switch to a game first)")
    now_p.add_argument("--db", default=None, help="the memory database (default: Yuki's own)")
    now_p.add_argument("--wait", type=float, default=0.0, help="wait up to this many seconds for the answer")
    st_p = sub.add_parser("status", help="the last offer-now request and what came of it")
    st_p.add_argument("--db", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cmd == "offer-now":
        path = request_offer_now(args.db, args.routine_id, args.in_s)
        print(f"requested routine {args.routine_id} in {args.in_s:g} s ({path.name}); the card comes through the usual "
              f"gates (no game or full screen in front, no call, not away, at most 2 routine cards a day with this "
              f"one) within a pass (~15 s) of that time.")
        deadline = time.monotonic() + max(0.0, args.wait)
        while args.wait and time.monotonic() < deadline:
            got = read_request(args.db) or {}
            if got.get("state") not in (None, "pending"):
                break
            time.sleep(1.0)
        if args.wait:
            got = read_request(args.db) or {}
            print(json.dumps({k: got.get(k) for k in ("routine_id", "state", "reason", "nudge_id")}))
        return 0
    got = read_request(args.db)
    print(json.dumps({k: (got or {}).get(k) for k in ("routine_id", "state", "reason", "nudge_id", "at", "until")}))
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())


__all__ = ["TRIGGER", "ONSET_WORDS", "DAY_WORDS", "MODES", "EVENT_KINDS", "REQUEST_FILE", "Routine", "Due",
           "RoutineLane", "has_tables", "routines", "routine", "record_event", "events", "set_mode", "routine_day",
           "day_start_at", "usual_at", "in_dayset", "window", "why_line", "card_question", "card_text", "routine_title",
           "available", "routine_params",
           "AVAILABILITY", "onsets", "first_onset", "feedback", "card_still_due", "request_offer_now", "read_request",
           "request_path", "main"]
