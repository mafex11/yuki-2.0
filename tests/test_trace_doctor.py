"""capC1 (report 96): the doctor's "Learning how you work" line and the ``yuki trace`` summary, on temp databases
and a temp privacy file. Counts and plain words only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from yuki import cli
from yuki.core import doctor
from yuki.memory import trace_store
from yuki.memory.store import Store


def _privacy(tmp: Path, on: bool) -> Path:
    path = tmp / "privacy.toml"
    path.write_text(f"[trace]\nrecord_actions = {'true' if on else 'false'}\nleave_out = []\n", encoding="utf-8")
    return path


def _db(tmp: Path, *, health: dict | None = None) -> Path:
    path = tmp / "memory.db"
    s = Store.open(path)
    now = time.time()
    trace_store.write_batch(s, [
        {"at": now - 60, "process": "fieldnotes", "kind": "click", "source": "user", "resolved": "point"},
        {"at": now - 50, "process": "fieldnotes", "kind": "type", "source": "user", "typed": 9, "resolved": "focus"},
        {"at": now - 40, "process": "sketchpad", "kind": "click", "source": "yuki", "resolved": "unresolved"},
    ])
    if health is not None:
        s.set_nudge_state("trace:health", json.dumps({"at": now, **health}))
    s.close()
    return path


def test_off_says_so_in_plain_words(tmp_path):
    c = doctor.check_trace(tmp_path / "none.db", _privacy(tmp_path, False))
    assert c.name == "Learning how you work" and c.status == doctor.OK and c.detail.startswith("off.")


def test_on_counts_the_last_day(tmp_path):
    c = doctor.check_trace(_db(tmp_path), _privacy(tmp_path, True))
    assert c.status == doctor.OK
    assert c.detail == ("on: 3 actions noted in the last day across 2 apps (2 of them yours; 67% with the button or"
                        " field named)")


def test_a_game_pause_is_said_and_a_dead_trace_warns(tmp_path):
    c = doctor.check_trace(_db(tmp_path, health={"state": "game"}), _privacy(tmp_path, True))
    assert c.status == doctor.OK and c.detail.endswith("paused right now: a game is running")
    (tmp_path / "memory.db").unlink()
    for p in tmp_path.glob("memory.db*"):
        p.unlink()
    c = doctor.check_trace(_db(tmp_path, health={"state": "hooks_down"}), _privacy(tmp_path, True))
    assert c.status == doctor.WARN and c.fix


def test_the_cli_summary_is_counts_only(tmp_path, capsys):
    db = _db(tmp_path)
    assert cli.main(["trace", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Last hour: 3 actions, 9 keys typed (counted, never kept)" in out and "by source: user 2, yuki 1" in out
    assert cli.main(["trace", "--db", str(db), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["last_day"]["kind"] == {"click": 2, "type": 1}
