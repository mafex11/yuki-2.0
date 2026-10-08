"""capQ2b (report 104): the task pass during long calls, its memory-log lines, and the doctor's day rollover.

The user sits in a voice call for hours, sometimes away from the PC: the task pass (one remote model call) now runs
during a call when they are away, no game runs and nothing is full screen; the families (the local embedder) still
never run during a call. Its results and the demo learner's go to the memory log, content-free. The doctor's "today"
numbers are today's, not the day before's.

Offline: temp databases, invented apps and goals; the model is a stand-in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC2_tasks import Worker, good_answer, prepared
from yuki.core import doctor
from yuki.memory import trace_families as F
from yuki.memory import trace_tasks as T
from yuki.memory.practice_learn import PracticeWorker


class _Log(list):
    def __call__(self, type_: str, **fields: Any) -> None:
        self.append((type_, fields))

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [f for t, f in self if t == type_]


def _worker(store: Any, state: dict, log: _Log | None = None, *, game_fn: Any = "state") -> tuple[PracticeWorker,
                                                                                                    Worker]:
    ids, _w, _pieces, facts = prepared(store)
    model = Worker([good_answer(ids, facts)])
    model.store = store
    pw = PracticeWorker(store, model, log=log, idle_fn=lambda: state["idle"],
                        live_fn=lambda: {"fullscreen": state["full"]},
                        game_fn=(lambda: state["game"]) if game_fn == "state" else game_fn, clock=lambda: NOW + 900)
    return pw, model


def _call(store: Any) -> int:
    return store.start_call(app="Voicehall", process="voicehall", via="screen", status="recording", at=NOW + 100)


# -- the gate ---------------------------------------------------------------------------------------------------------


def test_the_task_pass_runs_during_a_call_only_when_away_with_no_game_and_nothing_full_screen(store, monkeypatch):
    refreshed: list[bool] = []
    monkeypatch.setattr(F, "refresh", lambda *a, **kw: refreshed.append(True) or {"tasks": 1})
    state = {"idle": 30.0, "full": False, "game": False}
    pw, model = _worker(store, state)
    _call(store)
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "busy" and not model.calls        # at the PC, talking
    state.update(idle=4000.0, game=True)
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "game" and not model.calls        # away, a game runs
    state.update(game=False, full=True)
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "fullscreen" and not model.calls  # something full screen
    state["full"] = False
    out = pw.task_pass(NOW + 900)                                                            # away, in the call
    assert out["tasks"][0]["tasks"] == 3 and len(model.calls) == 1
    assert out["families_waiting"] == "call" and refreshed == []           # the local embedder: never in a call
    assert "demos" not in out and "demos_waiting" not in out               # the demo learner keeps the old gate


def test_without_a_game_gate_the_task_pass_still_waits_for_the_call(store):
    state = {"idle": 4000.0, "full": False, "game": False}
    pw, model = _worker(store, state, game_fn=None)
    _call(store)
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "call" and not model.calls
    pw.game_fn = lambda: (_ for _ in ()).throw(RuntimeError("gate down"))
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "game" and not model.calls        # fails closed


def test_out_of_a_call_the_gate_is_as_before(store, monkeypatch):
    refreshed: list[bool] = []
    monkeypatch.setattr(F, "refresh", lambda *a, **kw: refreshed.append(True) or {"tasks": 1})
    state = {"idle": 4000.0, "full": False, "game": False}
    pw, model = _worker(store, state)
    out = pw.task_pass(NOW + 900)
    assert out["tasks"][0]["tasks"] == 3 and "families_waiting" not in out and refreshed == [True]


# -- the memory log, content-free --------------------------------------------------------------------------------------


def test_task_pass_results_reach_the_memory_log_content_free(store, monkeypatch):
    monkeypatch.setattr(F, "refresh", lambda *a, **kw: {})
    log = _Log()
    state = {"idle": 4000.0, "full": False, "game": False}
    pw, model = _worker(store, state, log)
    _call(store)
    state["idle"] = 10.0
    for _ in range(3):                                                    # a long call: said once, not every tick
        pw.task_pass(NOW + 900)
    assert log.of("trace_learn_waiting") == [{"tasks_waiting": "busy"}]
    state["idle"] = 4000.0
    pw.task_pass(NOW + 900)
    passes = log.of("trace_learn_pass")
    assert len(passes) == 1
    p = passes[0]
    assert p["task_windows"] == 1 and p["tasks"] == 3 and p["noise"] == 0 and p["task_outcomes"] == {"ok": 1}
    assert p["task_cost_usd"] == 0.01
    allowed = {"task_windows", "tasks", "unclear", "noise", "task_cost_usd", "task_outcomes", "demos", "demo_results",
               "demo_cost_usd", "demo_errors", "tasks_stopped"}
    assert set(p) <= allowed
    text = json.dumps(log)
    for goal in ("You shared a page", "You wrote and saved", "You ran a command", "webby", "chatter", "Mira"):
        assert goal not in text                                           # never a goal, an app or a name
    assert log.of("trace_learn_waiting") == [{"tasks_waiting": "busy"}]   # the pass line says it ran: no second line


def test_demo_results_are_logged_as_kinds_and_costs_only(store):
    log = _Log()
    model = Worker()
    model.store = store
    pw = PracticeWorker(store, model, log=log)
    pw._log_trace({"demos": [
        {"task": 7, "result": "learned your way", "written": {"demo": [41]}, "error": None, "cost_usd": 0.004},
        {"task": 8, "result": "teaches nothing", "written": {}, "error": None, "cost_usd": 0.003},
        {"task": 9, "result": "not learnable (no checked outcome, or you said it was not what you did)",
         "written": {}, "error": None, "cost_usd": 0.0}]})
    p = log.of("trace_learn_pass")[0]
    assert p == {"demos": 3, "demo_results": {"learned": 1, "teaches_nothing": 1, "not_learnable": 1},
                 "demo_cost_usd": 0.007, "demo_errors": 0}


# -- the doctor's day rollover ----------------------------------------------------------------------------------------


def test_the_doctors_today_numbers_roll_over_at_midnight():
    evening = datetime(2026, 10, 8, 23, 30).timestamp()
    morning = (datetime(2026, 10, 8, 23, 30) + timedelta(hours=9)).timestamp()       # 08:30 the next day
    demos = json.dumps({"at": evening, "day": "2026-10-08", "ways": 0, "trials": 1, "hints": 0,
                        "learned_today": 2, "refused_today": 2})
    same = doctor._trace_demos_words(demos, evening + 600)
    assert "2 new today" in same and "2 tasks today set aside" in same
    later = doctor._trace_demos_words(demos, morning)
    assert "new today" not in later and "set aside" not in later and "seen only once" in later   # totals stay
    tasks = json.dumps({"at": evening, "tasks_today": 5, "unclear_today": 3, "cost_today": 0.12, "budget": 0.4,
                        "backlog": 2, "refused_today": 0})                                     # an old row: no day
    assert "found 5 tasks" in doctor._trace_tasks_words(tasks, evening + 600)
    nxt = doctor._trace_tasks_words(tasks, morning)
    assert "found" not in nxt and "cost" not in nxt and "2 finished sessions still to look at" in nxt
    assert T.health.__doc__ and "day" in T.health(_EmptyStore(), now=morning)           # new rows carry their day


class _EmptyStore:
    def _query(self, *a: Any) -> list:
        raise RuntimeError("no tables")

    def nudge_state(self, name: str) -> None:
        return None
