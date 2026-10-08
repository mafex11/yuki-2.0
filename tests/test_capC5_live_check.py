"""capC5 (report 78 §10.3-10.4, §12 C5; report 102): the live-check tool, ``scripts/c5_live_check.py``.

The plan writes the requests down as said and alternates the switch request by request; ``next``/``done`` set the
switch and note each trial; the analyzer, on a synthetic database, finds per trial whether it was done without a
takeover, the time against the user's own median for that kind of task, the learned ways run, the deviations and
whether each was handed over, and the pass of report 78 §12.

Offline: a temp database with invented runs and families; a temp privacy file (never the real one).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from yuki.memory import practice as P

ROOT = Path(__file__).resolve().parents[1]


def _tool() -> Any:
    spec = importlib.util.spec_from_file_location("c5_live_check", ROOT / "scripts" / "c5_live_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["c5_live_check"] = mod
    spec.loader.exec_module(mod)
    return mod


C5 = _tool()
REQUESTS = [f"invented request number {i}" for i in range(1, 11)]


# -- the plan ----------------------------------------------------------------------------------------------------------

def test_the_plan_keeps_the_requests_as_said_and_alternates_the_switch_per_request():
    plan = C5.make_plan(["  open the  team notes ", "", "send the weekly figures"], paired=True, now=NOW)
    assert plan["requests"] == ["open the team notes", "send the weekly figures"]   # as said (spaces folded)
    assert [(t["request"], t["on"]) for t in plan["trials"]] == [(1, True), (1, False), (2, False), (2, True)]
    # review F8: by default each real request is done once, the switch alternating per request
    one = C5.make_plan(REQUESTS, now=NOW)
    assert [t["on"] for t in one["trials"]] == [True, False] * 5 and one["mode"] == "one_each"
    assert [t["request"] for t in one["trials"]] == list(range(1, 11))
    with pytest.raises(ValueError):
        C5.make_plan(["", "  "])


def test_next_and_done_set_the_switch_for_each_trial_and_put_it_back_on_at_the_end(tmp_path):
    from yuki.memory import trace_replay as R

    privacy = tmp_path / "privacy.toml"
    privacy.write_text("# the user's file\n[trace]\nrecord_actions = true\n", encoding="utf-8")
    flips: list[bool] = []

    def switch(on: bool) -> None:
        flips.append(on)
        C5.set_switch(on, privacy)

    plan = C5.make_plan(REQUESTS[:2], paired=True, now=NOW)
    seen = []
    for k in range(4):
        t = C5.start_next(plan, now=NOW + 100 * k, switch=switch)
        seen.append(R.use_learned(privacy))                    # read live from the file, as Yuki reads it
        with pytest.raises(RuntimeError):
            C5.start_next(plan, switch=switch)                  # one at a time
        C5.end_current(plan, now=NOW + 100 * k + 60, switch=switch)
        assert t["start"] == NOW + 100 * k and t["end"] == NOW + 100 * k + 60
    assert seen == [True, False, False, True]
    assert flips[-1] is True and R.use_learned(privacy) is True   # back on after the last trial
    assert C5.start_next(plan, switch=switch) is None
    assert "record_actions = true" in privacy.read_text(encoding="utf-8")   # the rest of the file kept


def test_the_plan_file_round_trips_and_never_sits_in_the_memory_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = C5.state_path()
    assert path.parent == tmp_path / "Yuki" and "memory" not in path.parts
    plan = C5.make_plan(REQUESTS, now=NOW)
    C5.save(plan, path)
    assert C5.load(path) == plan


# -- the analyzer on a synthetic database --------------------------------------------------------------------------

def run(store: Any, rid: str, *, source: str, at: float, secs: float, outcome: str, cited: list[int] = (),
        steps: list[dict[str, Any]] | None = None) -> None:
    P.record_run(store, {"source": source, "session_id": "s1", "request_id": rid, "lane": "voice", "at": at,
                         "ended_at": at + secs, "words": "", "reply": "", "outcome": outcome,
                         "steps": steps or [{"tool": "click", "kind": "action", "ok": True, "verdict": True,
                                             "at": at}],
                         "cited": list(cited), "wall_s": secs}, now=at + secs)


def family(store: Any, fid: int, pid: int | None, spans: list[float]) -> None:
    def op(conn: Any) -> None:
        conn.execute("INSERT INTO ui_families(id, n_tasks, apps, playbook_id, last_at) VALUES (?,?,?,?,?)",
                     (fid, len(spans), "[]", pid, NOW))
        for k, s in enumerate(spans):
            conn.execute("INSERT INTO ui_family_members(task_id, family_id, started_at, ended_at, apps, route)"
                         " VALUES (?,?,?,?,?,?)", (fid * 100 + k, fid, NOW - 9000 + k * 600,
                                                   NOW - 9000 + k * 600 + s, "[]", "[]"))
    store._write(op)


def way(store: Any) -> int:
    return P.add_playbook(store, {"kind": "procedure", "intent": "post the weekly figures", "apps": ["sheets"],
                                  "body": {"steps": ["Open the sheet"]}, "origin": "demo"},
                          events=[{"type": "observed", "run_key": "seed", "at": NOW - 9000}], now=NOW - 9000)


def trial_plan(n: int, *, done: list[tuple[float, float]]) -> dict[str, Any]:
    plan = C5.make_plan(REQUESTS[:n], paired=True, now=NOW)
    for t, (start, end) in zip(plan["trials"], done):
        t["start"], t["end"] = start, end
    return plan


def test_the_analyzer_finds_done_takeover_time_deviations_and_handovers(store):
    pid = way(store)
    family(store, 1, pid, [100.0, 120.0, 140.0])          # the user's median: 120 s
    plan = trial_plan(2, done=[(NOW, NOW + 400), (NOW + 1000, NOW + 1400), (NOW + 2000, NOW + 2400),
                               (NOW + 3000, NOW + 3400)])
    # trial 1 (on): the learned way ran and was verified in 90 s
    run(store, "r1", source="hand", at=NOW + 5, secs=90, outcome="final", cited=[pid])
    # trial 2 (off): the thinker did it, but the user took over (its pause is in its trace)
    run(store, "r2", source="agent", at=NOW + 1005, secs=200, outcome="final",
        steps=[{"tool": "click", "kind": "control", "ok": False, "why": "paused: the user took over",
                "at": NOW + 1010}])
    # trial 3 (off): the thinker alone, done in 150 s, no family: no usual time from a way
    run(store, "r3", source="agent", at=NOW + 2005, secs=150, outcome="final")
    # trial 4 (on): the way stopped off its path and the thinker took it on 3 s later, done
    run(store, "r4", source="hand", at=NOW + 3005, secs=10, outcome="handed_over", cited=[pid])
    run(store, "r5", source="agent", at=NOW + 3018, secs=100, outcome="final")
    out = C5.analyze(store, plan)
    t1, t2, t3, t4 = out["trials"]
    assert (t1["on"], t1["done"], t1["took_over"], t1["family"], t1["median"], t1["ratio"]) == \
        (True, True, False, 1, 120.0, 0.75)
    assert t1["ways"] == 1 and t1["deviations"] == 0
    assert (t2["on"], t2["done"], t2["took_over"]) == (False, False, True)
    assert (t3["done"], t3["family"], t3["ratio"]) == (True, None, None)
    assert (t4["done"], t4["ways"], t4["deviations"], t4["handed_over"]) == (True, 1, 1, 1)
    assert t4["seconds"] == pytest.approx(113.0) and t4["ratio"] == pytest.approx(0.94, abs=0.01)
    v = out["verdict"]
    assert v["on_done"] == 2 and v["off_done"] == 1 and v["parts"] == {"done": True, "time": True, "better": True,
                                                                       "deviations": True}
    assert v["passed"]
    text = C5.report(out)
    assert "PASS" in text and "2 of 2 done" in text and "handed over 1" in text


def test_the_pass_fails_on_too_few_done_too_slow_not_better_or_a_deviation_not_handed_over(store):
    pid = way(store)
    family(store, 1, pid, [60.0])
    plan = trial_plan(1, done=[(NOW, NOW + 400), (NOW + 1000, NOW + 1400)])
    run(store, "a", source="hand", at=NOW + 5, secs=200, outcome="final", cited=[pid])   # 3.3x the usual: too slow
    run(store, "b", source="agent", at=NOW + 1005, secs=50, outcome="final")
    v = C5.analyze(store, plan)["verdict"]
    assert v["parts"]["done"] and not v["parts"]["time"] and v["slow"] == [1]
    assert not v["parts"]["better"]                       # as many done, and slower than with learning off
    assert not v["passed"]


def test_a_deviation_with_no_handover_fails_and_a_missing_trial_is_said(store):
    pid = way(store)
    plan = trial_plan(1, done=[(NOW, NOW + 400)])          # the second trial never ran
    run(store, "a", source="hand", at=NOW + 5, secs=20, outcome="handed_over", cited=[pid])
    out = C5.analyze(store, plan)
    v = out["verdict"]
    assert v["deviations"] == 1 and v["handed_over"] == 0 and not v["parts"]["deviations"]
    assert v["missing"] == [2] and not v["passed"]
    assert "Trials without a start or end: 2" in C5.report(out)


def test_the_kind_of_task_comes_from_the_requests_words_when_no_way_was_used(store, monkeypatch):
    family(store, 4, None, [30.0, 50.0])
    plan = trial_plan(1, done=[(NOW, NOW + 400)])
    run(store, "a", source="agent", at=NOW + 5, secs=45, outcome="final")
    from yuki.memory import trace_families

    asked = []

    def hints(st, vec, apps, **kw):
        asked.append((list(vec), kw["floor"]))
        return {"path": None, "example": None, "family_id": 4}

    monkeypatch.setattr(trace_families, "hints", hints)
    t = C5.analyze(store, plan, embed=lambda text: [0.1, 0.2])["trials"][0]
    assert t["family"] == 4 and t["median"] == 40.0 and t["ratio"] == pytest.approx(1.12, abs=0.01)
    assert asked and asked[0][1] == P.WAY_MIN_COSINE     # the brain's own floor
    # without an embedder and no way used: no kind of task, no time ratio (said as not known)
    t = C5.analyze(store, plan)["trials"][0]
    assert t["family"] is None and t["ratio"] is None


def test_the_tool_never_writes_the_requests_into_the_database(store):
    pid = way(store)
    plan = trial_plan(1, done=[(NOW, NOW + 400)])
    run(store, "a", source="hand", at=NOW + 5, secs=20, outcome="final", cited=[pid])
    before = store._query("SELECT count(*) FROM runs")[0][0]
    C5.report(C5.analyze(store, plan))
    assert store._query("SELECT count(*) FROM runs")[0][0] == before
    assert not any("invented request" in json.dumps([tuple(r) for r in store._query(f"SELECT * FROM {t}")])
                   for t in ("runs", "ui_families", "ui_family_members"))
