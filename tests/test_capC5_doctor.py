"""capC5 (report 78 §10.4; report 102): the doctor's words for doing things your way - learned ways tried this week,
the times one stopped because the screen was not as when you did it, how many of those went on another way, the
takeovers and checks, and the switch when it is off. Counts over the runs table's cleartext columns; plain words.

Offline: temp databases and a temp privacy file.
"""

from __future__ import annotations

import time
from pathlib import Path

from yuki.core import doctor
from yuki.memory import practice as P
from yuki.memory import trace_replay as R
from yuki.memory import trace_store
from yuki.memory.store import Store


def _privacy(tmp: Path, *, learned: bool | None = None) -> Path:
    path = tmp / "privacy.toml"
    extra = "" if learned is None else f"use_learned = {'true' if learned else 'false'}\n"
    path.write_text(f"[trace]\nrecord_actions = true\nleave_out = []\n{extra}", encoding="utf-8")
    return path


def _run(s: Store, rid: str, source: str, at: float, outcome: str, *, cited: bool = True,
         takeover: bool = False) -> None:
    steps = [{"tool": "click", "kind": "action", "ok": True, "at": at}]
    if takeover:
        steps.append({"tool": R.TAKEOVER_TOOL, "kind": "control", "ok": False, "at": at + 1})
    P.record_run(s, {"source": source, "session_id": "s", "request_id": rid, "at": at, "ended_at": at + 4,
                     "outcome": outcome, "cited": [1] if cited else [], "steps": steps}, now=at + 4)


def _db(tmp: Path) -> Path:
    path = tmp / "memory.db"
    s = Store.open(path)
    now = time.time()
    trace_store.write_batch(s, [{"at": now - 60, "process": "fieldnotes", "kind": "click", "source": "user",
                                 "resolved": "point"}])
    _run(s, "a", "hand", now - 4000, "final")                       # done your way
    _run(s, "b", "hand", now - 3000, "handed_over")                 # stopped off the path ...
    _run(s, "c", "agent", now - 2990, "final", cited=False)         # ... and went on another way 6 s later
    _run(s, "d", "hand", now - 2000, "handed_over")                 # stopped, nothing went on
    _run(s, "e", "hand", now - 1000, "unverified", takeover=True)   # you took over
    _run(s, "f", "hand", now - 500, "held")                         # it checked with you first
    _run(s, "g", "hand", now - 400, "final", cited=False)           # a plain hand, no learned way: not counted
    _run(s, "h", "hand", now - 9 * 86400, "final")                  # older than a week
    s.close()
    return path


def test_the_doctor_words_learned_ways_run_deviations_handovers_and_takeovers(tmp_path):
    c = doctor.check_trace(_db(tmp_path), _privacy(tmp_path))
    assert c.status == doctor.OK
    assert c.detail.endswith("; tried 5 things your way this week (1 done; it stopped 2 times because the screen was "
                             "not as when you did it, and went on another way 1 of those times; you took over 1 "
                             "time; it checked with you first 1 time)")
    assert "switched off" not in c.detail


def test_the_doctor_says_when_the_switch_is_off(tmp_path):
    c = doctor.check_trace(_db(tmp_path), _privacy(tmp_path, learned=False))
    assert "using what it learned from how you work is switched off, so it works as it did before" in c.detail


def test_health_counts_only_and_nothing_to_say_is_nothing(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    try:
        assert R.health(s) == {"runs": 0, "done": 0, "deviations": 0, "handovers": 0, "took_over": 0, "held": 0}
        assert R.health_words(R.health(s)) == ""
        assert R.health(object()) == {}                              # never raises
    finally:
        s.close()
