"""Report 78, phase C0: elevated windows reported as such, and the doctor's capture reason codes.

* An app that runs as administrator (Task Manager on an administrator account) is out of reach of a normal
  process (Windows' UIPI), so the watcher reports its window ``elevated`` instead of reading it empty.
* The doctor's "Screen reading" line counts why reads came back empty, were skipped or were cut short, from the
  content-free health rows, in plain words.

Offline: invented apps and counts in a temp database, stubbed window calls; the only real-system reads are of this
test process's own token and of the integrity level of processes already running (read-only).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from yuki.core import elevation
from yuki.core.doctor import CAPTURE_CHECK, OK, SKIP, WARN, check_capture_health, run_checks


# -- 1. elevated windows -------------------------------------------------------------------------------------------


def test_this_process_is_not_above_itself_and_runs_at_a_known_level() -> None:
    assert elevation.own_level() >= elevation.MEDIUM
    assert elevation.runs_above_us(os.getpid()) is False
    assert elevation.runs_above_us(0) is False


def test_levels_of_running_processes_read_without_error() -> None:
    """Read-only: the integrity level of the processes running now; never raises past PermissionError."""
    seen = 0
    for proc in list(psutil.process_iter(["pid"]))[:80]:
        try:
            level = elevation.integrity_level(proc.info["pid"])
        except PermissionError:
            continue
        assert level is None or 0 <= level <= 0x5000
        seen += 1
    assert seen


@pytest.mark.parametrize("level, above", [(elevation.HIGH, True), (elevation.MEDIUM, False), (None, False)])
def test_above_us_is_the_integrity_level_against_ours(monkeypatch: pytest.MonkeyPatch, level, above) -> None:
    monkeypatch.setattr(elevation, "own_level", lambda: elevation.MEDIUM)
    monkeypatch.setattr(elevation, "integrity_level", lambda pid: level)
    assert elevation.runs_above_us(4242) is above


def test_a_process_windows_refuses_to_open_counts_as_above(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(pid: int) -> int:
        raise PermissionError(pid)

    monkeypatch.setattr(elevation, "integrity_level", refused)
    assert elevation.runs_above_us(4242) is True


class _NoStore:
    """A store the capture must never reach for an elevated window."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the store was used: {name}")


def _watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, elevated: bool):
    from yuki.memory import watcher as W
    from yuki.memory.privacy import PrivacyConfig

    w = W.Watcher(lambda: None, privacy=PrivacyConfig(tmp_path / "privacy.toml"))
    monkeypatch.setattr(W, "_user32", SimpleNamespace(IsWindow=lambda h: True, GetForegroundWindow=lambda: 77,
                                                      PostMessageW=lambda *a: True))
    monkeypatch.setattr(W, "is_user_window", lambda h: True)
    monkeypatch.setattr(W, "is_fullscreen_front", lambda h: False)
    monkeypatch.setattr(W, "_window_pid", lambda h: 4242)
    monkeypatch.setattr(W, "_window_title", lambda h: "Task Manager")
    facts = W.ProcessFacts(4242, "Taskmgr.exe", r"C:\Windows\System32\Taskmgr.exe", "Task Manager", False, elevated)
    monkeypatch.setattr(w._processes, "get", lambda pid: facts)
    reads: list[int] = []

    def no_read(hwnd, **kw):
        reads.append(hwnd)
        raise AssertionError("an elevated window was read")

    monkeypatch.setattr(W, "extract", no_read)
    monkeypatch.setattr(W, "_uia_threads_alive", lambda: 99)   # a non-elevated window stops at the backlog gate
    return W, w, reads


def test_the_watcher_reports_an_elevated_window_instead_of_reading_it(tmp_path, monkeypatch) -> None:
    W, w, reads = _watcher(tmp_path, monkeypatch, elevated=True)
    result = w._capture(_NoStore(), W._Request(hwnd=77, trigger="focus"))
    assert (result.outcome, result.reason) == ("skipped", "elevated") and not reads
    assert result.app == "Task Manager"


def test_a_normal_window_still_goes_on_to_be_read(tmp_path, monkeypatch) -> None:
    W, w, reads = _watcher(tmp_path, monkeypatch, elevated=False)
    result = w._capture(_NoStore(), W._Request(hwnd=77, trigger="focus"))
    assert result.reason == "uia_backlog"          # past the elevated gate, stopped by the next one


def test_a_visible_elevated_window_is_reported_too(tmp_path, monkeypatch) -> None:
    W, w, reads = _watcher(tmp_path, monkeypatch, elevated=True)
    monkeypatch.setattr(W, "_user32", SimpleNamespace(IsWindow=lambda h: True, GetForegroundWindow=lambda: 5))
    surface = SimpleNamespace(visible_share=0.9)
    monkeypatch.setattr(W._visibility, "layout", lambda: SimpleNamespace(by_hwnd=lambda h: surface))
    result = w._capture_visible(object(), W._BgJob(hwnd=77, reason="shown"))
    assert (result.outcome, result.reason) == ("skipped", "elevated") and not reads


def test_process_facts_carry_elevation_and_default_to_not_elevated() -> None:
    from yuki.memory.watcher import ProcessFacts, _ProcessCache

    assert ProcessFacts(1, "a.exe", "", "A", False).elevated is False
    mine = _ProcessCache().get(os.getpid())
    assert mine.is_own and mine.elevated is False


# -- 2. the doctor's capture reason codes ----------------------------------------------------------------------------

NOW = 1_780_000_000.0


def _health_db(tmp_path: Path, rows: list[tuple[float, str, str, str, int]]) -> Path:
    """A memory database holding only the health table (as migration 1 made it), with these rows:
    (seconds ago, app, outcome, reason, how many)."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE health (id INTEGER PRIMARY KEY, at REAL NOT NULL, app TEXT, trigger TEXT,"
                 " outcome TEXT NOT NULL, reason TEXT, chars INTEGER NOT NULL DEFAULT 0, ms REAL NOT NULL DEFAULT 0)")
    for ago, app, outcome, reason, n in rows:
        conn.executemany("INSERT INTO health(at, app, trigger, outcome, reason) VALUES (?,?,?,?,?)",
                         [(NOW - ago, app, "focus", outcome, reason)] * n)
    conn.commit()
    conn.close()
    return db


def test_the_doctor_says_why_reads_came_back_empty_skipped_or_cut_short(tmp_path) -> None:
    db = _health_db(tmp_path, [
        (60, "Notepad", "captured", "", 40),
        (60, "Notepad", "unchanged", "truncated", 3),
        (60, "Terminal Nine", "empty", "no_text:terminal", 30),
        (60, "Task Manager", "skipped", "elevated", 12),
        (60, "Fieldrunner", "paused", "fullscreen", 20),
        (60, "Fieldrunner", "skipped", "fullscreen", 5),
        (60, "Example Browser", "skipped", "blocked_host_before", 4),
        (60, "Example Browser", "skipped", "password_field", 2),
        (60, "Example Browser", "skipped", "not_user_window:Shell_TrayWnd", 1),
        (60, "Example Browser", "skipped", "something_new", 1),
        (3 * 86400, "Old App", "empty", "no_text:generic", 500),        # outside the last day: not counted
    ])
    c = check_capture_health(db, now=NOW)
    assert c.name == CAPTURE_CHECK and c.status == OK
    d = c.detail
    assert d.startswith("In the last day, 118 reads: 43 kept text (3 of them cut short")
    assert "30 came back empty: the window showed no text (30, mostly Terminal Nine)" in d
    assert "45 were skipped:" in d
    assert "a full-screen app was in front (25, mostly Fieldrunner)" in d
    assert "the window runs as administrator, which Windows keeps out of reach (12, mostly Task Manager)" in d
    assert "Old App" not in d and "_" not in d and "no_text" not in d       # plain words, never a code
    week = check_capture_health(db, now=NOW, hours=168)
    assert "618 reads" in week.detail and "Old App" in week.detail


def test_many_failed_reads_are_a_warning_and_no_reads_or_no_database_are_not(tmp_path) -> None:
    db = _health_db(tmp_path, [(60, "Notepad", "captured", "", 50), (60, "Notepad", "failed", "extract_error", 12)])
    c = check_capture_health(db, now=NOW)
    assert c.status == WARN and "12 failed: something went wrong (12, mostly Notepad)" in c.detail and c.fix
    (tmp_path / "quiet").mkdir()
    empty = _health_db(tmp_path / "quiet", [])
    assert check_capture_health(empty, now=NOW).status == OK
    assert check_capture_health(tmp_path / "missing.db", now=NOW).status == SKIP


def test_the_doctor_reads_without_writing(tmp_path) -> None:
    db = _health_db(tmp_path, [(60, "Notepad", "captured", "", 5)])
    before = db.stat().st_mtime_ns
    time.sleep(0.01)
    check_capture_health(db, now=NOW)
    assert db.stat().st_mtime_ns == before


def test_the_check_is_on_the_doctor_page(monkeypatch) -> None:
    import yuki.core.doctor as doctor

    for name in dir(doctor):
        if name.startswith("check_") and name != "check_capture_health":
            monkeypatch.setattr(doctor, name, lambda *a, _n=name, **k: doctor.Check(_n, SKIP, "stubbed"))
    monkeypatch.setattr(doctor, "check_capture_health", lambda *a, **k: doctor.Check(CAPTURE_CHECK, OK, "counted"))
    names = [c.name for c in run_checks(SimpleNamespace(), offline=True)]
    assert CAPTURE_CHECK in names
