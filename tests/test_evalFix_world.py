"""evalFix (PLAN.md Appendix B.1, C.4): the synthetic world the eval-tool tests share, and the independent source
reference.

A stand-in "live" memory database in the test's own folder (``default_db_path`` points at it), with invented rows
shaped like the round-2 evidence: Zen's weekday-morning meeting on the 7 E1 days (seen by screen or the Meet site, or
a mic hold on 28 and 30 Sep), Discord evening calls, FL Studio / VALORANT / Settings mic holds that were never meetings,
YouTube page rows. A ``moments`` table in migration 40's shape (map-foundations §F1.2) is filled by a stand-in for
Yuki's own ``moments rebuild`` (fMoments builds the real one). No real data, no live desktop.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from yuki.eval import habits_replay as H
from yuki.memory.store import Store

#: Invented words, so a leak into the console or a report is easy to find.
SECRET_WORDS = ("zorblax quietly", "plinkett marmalade", "vexilla nimbus")

MOMENTS_DDL = """
CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, ended_at REAL, seen_at REAL NOT NULL, kind TEXT NOT NULL, app TEXT,
    service TEXT, entity_digest TEXT, entity_ciphertext TEXT, value REAL, value_from REAL, means TEXT,
    by TEXT NOT NULL DEFAULT 'user', request_id TEXT, source TEXT NOT NULL, source_id INTEGER, evidence TEXT,
    facts TEXT, UNIQUE(source, source_id, kind)
);
"""


def ts(day: str, hms: str) -> float:
    return datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").timestamp()


def evening_days(n: int = 15) -> list[str]:
    start = datetime(2026, 9, 24)
    return [(start + timedelta(days=i)).date().isoformat() for i in range(n)]


#: The world's own E1 weekdays (the onsets of its Zen rows below) and Discord window; the user's real ones live in
#: their eval folder (``habits_replay.BARS_NAME``), never here.
E1_DAYS = (
    ("2026-09-25", "08:58:20", "seen"),
    ("2026-09-28", "09:02:10", "hold"),
    ("2026-09-29", "09:01:15", "seen"),
    ("2026-09-30", "08:59:40", "hold"),
    ("2026-10-05", "08:57:30", "seen"),
    ("2026-10-07", "08:56:25", "seen"),
    ("2026-10-08", "08:58:05", "seen"),
)


@pytest.fixture()
def bars(monkeypatch) -> None:
    """The world's expectations for E1 and the Discord evenings, never the user's file."""
    monkeypatch.setattr(H, "E1_EXPECTED", E1_DAYS)
    monkeypatch.setattr(H, "EVIDENCE_FROM", ("2026-09-24", "04:00:00"))
    monkeypatch.setattr(H, "EVIDENCE_TO", ("2026-10-10", "04:00:00"))
    monkeypatch.setattr(H, "DISCORD_EVENINGS_NEED", 15)


@pytest.fixture()
def hermetic(tmp_path, monkeypatch) -> Path:
    """Never the user's settings, eval files, temp folder or memory: all under the test's own folder."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("YUKI_EVAL_DIR", str(tmp_path / "eval"))
    (tmp_path / "temp").mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    return tmp_path


@pytest.fixture()
def live(hermetic, monkeypatch) -> Path:
    """The stand-in for the live database, closed, where ``default_db_path`` points, with the world's rows."""
    path = hermetic / "live" / "memory.db"
    s = Store.open(path)
    s.close()
    fill_world(path)
    import yuki.memory.store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: path)
    return path


def digest(folder: Path) -> dict[str, str]:
    """Every file's content, except SQLite's shared-memory index and an empty write-ahead log (any reader makes
    those)."""
    out = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.endswith("-shm") or (p.name.endswith("-wal") and p.stat().st_size == 0):
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def fill_world(path: Path) -> None:
    """The source rows: calls and timeline, shaped like the evidence (invented, content-free)."""
    conn = sqlite3.connect(path)
    try:
        now = ts("2026-10-09", "12:00:00")

        def call(app: str, process: str, via: str, at: float, kind: str = "call", hold: float | None = None) -> int:
            return conn.execute(
                "INSERT INTO calls(app, process, via, detected_at, ended_at, status, updated_at, kind, hold_since)"
                " VALUES (?,?,?,?,?,?,?,?,?)", (app, process, via, at, at + 1800, "ended", now, kind, hold)).lastrowid

        def row(process: str, app: str, start: float, *, host: str | None = None, meeting: str | None = None,
                mic: float = 0.0) -> int:
            return conn.execute(
                "INSERT INTO timeline(started_at, ended_at, state, process, app, host, page_key, updated_at, meeting,"
                " mic_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (start, start + 600, "present", process, app, host, f"pk{start}", now, meeting, mic)).lastrowid

        # E1: seen by the Meet site (25, 29 Sep, 5 Oct), by the screen (7, 8 Oct), mic holds (28, 30 Sep)
        call("Zen", "zen", "meeting", ts("2026-09-25", "08:58:20"))
        row("zen", "Zen", ts("2026-09-25", "08:58:20"), host="meet.google.com", meeting="Google Meet", mic=30)
        call("Zen", "zen", "mic", ts("2026-09-28", "09:02:10"))
        row("zen", "Zen", ts("2026-09-28", "09:02:43"), meeting="Zen", mic=20)
        row("zen", "Zen", ts("2026-09-29", "09:01:15"), host="meet.google.com", meeting="Google Meet", mic=30)
        row("zen", "Zen", ts("2026-09-30", "08:59:40"), meeting="Zen", mic=9)
        row("zen", "Zen", ts("2026-10-05", "08:57:30"), host="meet.google.com", meeting="Google Meet", mic=30)
        call("Zen", "zen", "screen", ts("2026-10-07", "08:56:25"))
        call("Zen", "zen", "screen", ts("2026-10-08", "08:58:05"))
        # never meetings: mic holds of FL Studio, VALORANT (a game's voice) and Settings
        for d in ("2026-09-26", "2026-09-27", "2026-10-01"):
            row("fl64", "FL Studio", ts(d, "05:40:00"), meeting="FL Studio", mic=50)
            row("systemsettings", "Settings", ts(d, "09:00:00"), meeting="Settings", mic=10)
            call("VALORANT", "valorant-win64-shipping", "mic", ts(d, "20:00:00"), kind="game")
        # Discord evening calls on 15 evenings (one after midnight)
        for i, d in enumerate(evening_days()):
            at = ts(d, "19:30:00") if i else ts(d, "23:59:00") + 19 * 60   # 00:18 the next day
            call("Discord", "discord", "screen", at)
        # YouTube pages
        for d in ("2026-10-03", "2026-10-06"):
            row("chrome", "Chrome", ts(d, "13:02:00"), host="www.youtube.com")
        conn.commit()
    finally:
        conn.close()


def good_moments(path: Path) -> list[tuple]:
    """What a correct ``moments rebuild`` writes for the world (source rows looked up by their shape)."""
    conn = sqlite3.connect(path)
    try:
        calls = conn.execute("SELECT id, process, via, kind, detected_at FROM calls").fetchall()
        tl = conn.execute("SELECT id, process, host, meeting, started_at FROM timeline").fetchall()
    finally:
        conn.close()
    out = []
    for cid, process, via, kind, at in calls:
        if kind != "call":
            out.append(("game_voice", process, at, "calls", cid, "hold" if via == "mic" else "screen", None))
        elif process in ("zen", "discord"):
            out.append(("call_join", process, at, "calls", cid, "hold" if via == "mic" else via, None))
    for tid, process, host, meeting, at in tl:
        if host == "meet.google.com":
            out.append(("call_join", process, at, "timeline", tid, "site", None))
        elif process == "zen" and meeting:
            if not any(abs(m[2] - at) <= 60 and m[0] == "call_join" for m in out):
                out.append(("call_join", process, at, "timeline", tid, "hold", None))
        elif host == "www.youtube.com":
            out.append(("page", process, at, "timeline", tid, None, "enc-video-id"))
    return out


def write_moments(path: Path, items: list[tuple], *, append: bool = False) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(MOMENTS_DDL)
        if not append:
            conn.execute("DELETE FROM moments")
        for kind, app, at, source, source_id, evidence, entity in items:
            conn.execute("INSERT OR IGNORE INTO moments(at, seen_at, kind, app, source, source_id, evidence,"
                         " entity_digest, entity_ciphertext) VALUES (?,?,?,?,?,?,?,?,?)",
                         (at, at + 5, kind, app, source if not append else f"{source}+", source_id, evidence,
                          "d" * 64 if entity else None, entity))
        conn.commit()
    finally:
        conn.close()


class Rebuild:
    """A stand-in for ``python -m yuki.memory.moments rebuild --db <copy>``: writes ``items`` (fresh each time, so
    idempotent unless ``append``), reports ``seconds``."""

    def __init__(self, path: Path, items: list[tuple] | None = None, *, append: bool = False,
                 seconds: float = 0.4) -> None:
        self.path, self.items, self.append, self.seconds = path, items, append, seconds
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> dict[str, Any]:
        self.calls.append(list(args))
        items = self.items if self.items is not None else good_moments(self.path)
        write_moments(self.path, items, append=self.append and len(self.calls) > 1)
        return {"code": 0, "s": self.seconds, "how": "stub", "lines": 1}


# ---------------------------------------------------------------------------------------------------------------------
# The independent source reference
# ---------------------------------------------------------------------------------------------------------------------


def test_the_reference_finds_e1_from_raw_rows_alone(live, bars):
    from yuki.eval import dbcopy

    copy = dbcopy.copy_db(live)
    try:
        ref = H.source_reference(copy)
    finally:
        dbcopy.remove_copy(copy)
    assert ref["e1_days_with_a_source"] == 7
    by_day = {d["day"]: d for d in ref["e1"]}
    assert by_day["2026-10-07"]["calls_via"] == ["screen"]
    assert by_day["2026-09-29"]["meet_host_row"] and not by_day["2026-09-29"]["calls_via"]
    assert by_day["2026-09-30"]["zen_mic_row"]
    assert ref["discord_call_evenings"] == 15
    assert ref["meeting_rows"]["fl64"] == 3 and ref["meeting_rows"]["systemsettings"] == 3
    assert ref["call_rows"]["valorant-win64-shipping"] == {"game/mic": 3}


def test_the_reference_is_content_free(live):
    from yuki.eval import dbcopy

    copy = dbcopy.copy_db(live)
    try:
        text = repr(H.source_reference(copy))
    finally:
        dbcopy.remove_copy(copy)
    assert "meet.google.com" not in text and "youtube.com" not in text and "Google Meet" not in text
