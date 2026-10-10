"""Shared stand-ins for the routine lane's tests (rLane): temp databases with the routine tables, a clock on an
invented weekday, presence rows, joins, and a coach worker with no model. Nothing here touches the desktop, a model
or the real database.

The ``routines`` / ``routine_events`` tables are rMiner's migration (map-routines R1.2); until it merges they are
created here from the same spec (test-only, ``ROUTINE_DDL``).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from yuki.memory import routine_offers
from yuki.memory.nudges import NudgeConfig, NudgeWorker
from yuki.memory.store import NewNudge, NudgeCheckin, Store, TimelineRow

#: map-routines R1.2 (rMiner's migration 42, renumbered at merge): additive, content encrypted.
ROUTINE_DDL = """
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, skill_id TEXT, params_ciphertext TEXT,
    label_ciphertext TEXT, trigger TEXT NOT NULL, dayset TEXT NOT NULL, at_min REAL, mad_min REAL, after_key TEXT,
    hits INTEGER, eligible INTEGER, rate REAL, lift REAL, p REAL, stable INTEGER, last_hit_at REAL,
    misses_in_row INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, offer_mode TEXT NOT NULL DEFAULT 'early',
    told INTEGER NOT NULL DEFAULT 0, told_at REAL, confirmed_at REAL, fact_id INTEGER,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, mined_at REAL);
CREATE INDEX IF NOT EXISTS routines_key ON routines(key, state);
CREATE TABLE IF NOT EXISTS routine_events (
    id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, nudge_id INTEGER,
    detail TEXT);
CREATE INDEX IF NOT EXISTS routine_events_r ON routine_events(routine_id, at);
"""

#: An invented weekday (Wednesday 7 Oct 2026) and its weekend.
DAY = datetime(2026, 10, 7)


def at(hhmm: str, day: datetime = DAY) -> float:
    h, m, *s = (int(x) for x in hhmm.split(":"))
    return (day + timedelta(hours=h, minutes=m, seconds=s[0] if s else 0)).timestamp()


def days_later(n: int) -> datetime:
    return DAY + timedelta(days=n)


def open_store(tmp: str, *, tables: bool = True) -> Store:
    s = Store.open(Path(tmp) / "memory.db")
    if tables:
        with s._lock:
            s._conn.executescript(ROUTINE_DDL)
    routine_offers._tables.clear()
    return s


def add_routine(store: Store, *, skill: str = "meet.open", key: str = "meet.open|zen", at_min: float = 540.0,
                dayset: str = "weekdays", state: str = "active", mode: str = "early", hits: int = 7,
                eligible: int = 9, trigger: str = "clock", label: str | None = None,
                params: dict | None = None) -> int:
    now = time.time()
    return store._write(lambda conn: int(conn.execute(
        "INSERT INTO routines(key, skill_id, params_ciphertext, label_ciphertext, trigger, dayset, at_min, mad_min,"
        " hits, eligible, rate, state, offer_mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, skill, store.cipher.encrypt(json.dumps(params)) if params else None,
         store.cipher.encrypt(label) if label else None, trigger, dayset, at_min, 1.6, hits, eligible, 0.75, state,
         mode, now, now)).lastrowid))


def present(store: Store, start: float, end: float, app: str = "Google Chrome", **kw: Any) -> None:
    t = start
    while t < end:
        e = min(end, t + 300.0)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower().replace(" ", ""), app, active_s=e - t,
                                        **kw))
        t = e


_join_ids = iter(range(10_000, 1_000_000))


def join(store: Store, when: float, *, app: str = "zen", by: str = "user", kind: str = "call_join") -> None:
    """A ``call_join`` moment (fMoments' table) of the user's own (or Yuki's: ``by='yuki'``)."""
    store.save_moments([{"at": when, "kind": kind, "app": app, "service": "Google Meet", "by": by,
                         "source": "test", "source_id": next(_join_ids), "evidence": "screen",
                         "facts": {"seen_at": when}}])


def card(store: Store, when: float, *, trigger: str = "thread", kind: str = "notice", shown: bool = False,
         reaction: str | None = None) -> int:
    _cid, nid = store.record_checkin(NudgeCheckin(at=when, trigger=trigger, outcome=kind, model="m"),
                                     NewNudge(kind=kind, text="Another card.", trigger=trigger))
    if shown:
        with store._lock:
            store._conn.execute("UPDATE nudges SET shown_at=? WHERE id=?", (when + 1.0, nid))
    if reaction:
        store.react_to_nudge(nid, reaction, at=when + 5.0)
    return nid


class NoModel:
    """A model client that fails the test if anything calls it (the lane is code only)."""

    class messages:   # noqa: N801 - the SDK's shape
        @staticmethod
        def create(**kw: Any) -> Any:
            raise AssertionError("the routine lane made a model call")


class Clock:
    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def lane_worker(store: Store, clock: Clock, *, live: dict | None = None, config: NudgeConfig | None = None,
                ready: bool = True, avail: Any = True, locked: bool = False, render: list | None = None,
                calls: list | None = None) -> NudgeWorker:
    """A coach worker on ``store`` with the routine lane's trust and availability stubbed (said so in the report):
    the real gates, the real attention instance, no model (other cards are switched off so only the lane can write)."""
    cfg = config or NudgeConfig.from_dict({"presence_enabled": False, "checkins_enabled": False,
                                           "questions_enabled": False, "resume_enabled": False})
    live_state = {} if live is None else live
    tmp = Path(tempfile.gettempdir()) / "yuki-rlane-tests"
    w = NudgeWorker(store, config=cfg, client=NoModel(), log_dir=tmp, locked_fn=lambda: locked,
                    media_fn=lambda: [], render_fn=lambda: list(render or []), live_fn=lambda: dict(live_state),
                    event_name=f"Local\\YukiNudgeReadyTest{os.getpid()}", clock=clock)
    w.availability_calls = []   # type: ignore[attr-defined]

    def available(st: Any, skill: Any) -> Any:
        w.availability_calls.append(skill.key)   # type: ignore[attr-defined]
        return avail() if callable(avail) else avail

    w.routines = routine_offers.RoutineLane(w, ready=lambda st, skill: ready, available=available)
    w.logged = []   # type: ignore[attr-defined]
    original = w._log

    def log(type: str, **fields: Any) -> None:
        w.logged.append((type, fields))   # type: ignore[attr-defined]
        original(type, **fields)

    w._log = log   # type: ignore[method-assign]
    return w


def routine_cards(store: Store) -> list[Any]:
    return [n for n in store.nudges(kinds=("notice",)) if n.trigger == "routine"]


def held(w: NudgeWorker) -> list[str]:
    return [f.get("reason") for t, f in w.logged if t == "routine_held"]   # type: ignore[attr-defined]


def run_ticks(w: NudgeWorker, clock: Clock, start: float, end: float, step: float = 15.0) -> list[dict]:
    out = []
    t = start
    while t <= end:
        clock.t = t
        out.extend(r for r in w.tick(t) if r.get("trigger") == "routine")
        t += step
    return out
