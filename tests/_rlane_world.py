"""Shared stand-ins for the routine lane's tests (rLane): temp databases with the routine tables, a clock on an
invented weekday, presence rows and joins. Nothing here touches the desktop, a model or the real database.

r30: the coach worker stand-in (``lane_worker``) went with the lane's card tests: NudgeWorker lost ``media_fn`` /
``render_fn`` in kProactive (18 Oct).

The ``routines`` / ``routine_events`` tables are rMiner's migration (map-routines R1.2); until it merges they are
created here from the same spec (test-only, ``ROUTINE_DDL``).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from yuki.memory import routine_offers
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


class Clock:
    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

