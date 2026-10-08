"""capC3 (report 105): nothing is written to the repo or to the real memory database; content stays on this PC.

The script copies the database with the backup API into the temp folder (the real file only read, never opened as a
store), cuts the copy, writes the report to the temp folder and the requests next to the settings, and deletes the
copy. A copy you made is copied again (your file is never changed). The console carries counts and scores, not your
requests, unless asked. Here "the real database" is a stand-in in the test's own folder; the model is a stand-in.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from test_c_practice import Words
from test_capC2_links import DAY, NOW  # noqa: F401
from test_capC3_world import NAMES, NOTES_REQUEST, REQUEST, StubThinker, _hermetic, two_kinds  # noqa: F401
from yuki.eval import c3_arms as A
from yuki.eval import c3_data as D
from yuki.memory.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _digest(folder: Path) -> dict[str, str]:
    """Every file's content, except the shared-memory index and an EMPTY write-ahead log: SQLite makes those for any
    reader of a database in WAL mode (the live one has them anyway while Yuki runs); they hold no data."""
    out = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.endswith("-shm") or (p.name.endswith("-wal") and p.stat().st_size == 0):
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _git_status() -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("c3_eval_script", ROOT / "scripts" / "c3_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def real(tmp_path, monkeypatch) -> Path:
    """A stand-in for the live memory database (with the world in it), closed, where ``default_db_path`` points."""
    path = tmp_path / "live" / "memory.db"
    s = Store.open(path)
    try:
        two_kinds(s, learned=True)
    finally:
        s.close()
    import yuki.memory.store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: path)
    return path


def test_the_live_database_is_only_read_and_never_opened_as_a_store(real):
    before = _digest(real.parent)
    with pytest.raises(RuntimeError, match="never the live one"):
        D.open_copy(real)
    tmp = Path(tempfile.mkdtemp())
    copy = D.copy_db(real, tmp)
    s = D.open_copy(copy)
    try:
        D.cut(s, NOW + DAY, embedder=Words())                 # cutting the copy...
    finally:
        s.close()
    assert _digest(real.parent) == before                       # ...leaves the live file as it was


def test_a_whole_run_from_the_script_writes_only_the_copy_the_report_and_your_requests(real, monkeypatch, capsys,
                                                                                       tmp_path):
    repo_before = _git_status()
    live_before = _digest(real.parent)
    mod = _script()
    monkeypatch.setattr(mod, "_embedder", lambda db, off: Words())
    monkeypatch.setattr(A, "ThinkerPlanner", lambda *a, **k: StubThinker())
    from yuki.eval import c3_data

    orig = c3_data.build_instances
    monkeypatch.setattr(c3_data, "build_instances", lambda *a, **k: orig(*a, **{**k, "names": NAMES}))
    assert mod.main(["requests", REQUEST, NOTES_REQUEST]) == 0
    assert mod.main(["run", "--copy-real", "--need", "1", "--min-requests", "1", "--no-conversations", "--show-plans"]) == 0
    out = capsys.readouterr().out
    assert "Requests evaluated: 2" in out and "The copy is deleted." in out
    assert REQUEST not in out and NOTES_REQUEST not in out              # your words stay off the console
    # nothing in the repo, the live database unchanged, no copy left behind
    assert _git_status() == repo_before
    assert _digest(real.parent) == live_before
    temp = Path(tempfile.gettempdir())
    assert temp.is_relative_to(tmp_path) and not list(temp.glob("yuki-c3-*/"))
    reports = sorted(temp.glob("yuki-c3-report-*"))
    assert [p.suffix for p in reports] == [".json", ".txt"]
    assert REQUEST in reports[1].read_text(encoding="utf-8")            # the report on this PC has them
    # the requests file next to the settings; once planned steps were shown, later requests are no longer blind
    data = D.load_requests()
    assert D.requests_path().is_relative_to(tmp_path) and data["steps_shown_at"]
    assert "c3-requests.json" not in repo_before + _git_status()


def test_a_copy_you_made_is_copied_again_and_never_changed(real, monkeypatch, capsys, tmp_path):
    mine = D.copy_db(real, tmp_path / "mine")
    before = _digest(mine.parent)
    mod = _script()
    monkeypatch.setattr(mod, "_embedder", lambda db, off: Words())
    monkeypatch.setattr(A, "ThinkerPlanner", lambda *a, **k: StubThinker())
    D.add_requests([REQUEST])
    assert mod.main(["run", "--db", str(mine), "--need", "1", "--min-requests", "1", "--no-conversations", "--dry-run",
                     "--show-requests"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: no model was called" in out and REQUEST in out    # asked for: shown
    assert _digest(mine.parent) == before
    assert mod.main(["status", "--db", str(mine), "--need", "1"]) == 0
    assert "That is enough to run the evaluation." in capsys.readouterr().out
    assert mod.main(["run"]) == 2                                       # no database named: nothing happens
    assert mod.main(["requests", "--list"]) == 0
    assert "1 requests written down" in capsys.readouterr().out
