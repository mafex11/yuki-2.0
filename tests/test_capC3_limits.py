"""capC3 (report 78 §10.3, §12 C3; report 105): the caps, the cost refusal, the time limit, and the not-enough-data
stop.

At most 30 tasks (your own requests first); a $3 cap: refused up front when the expected cost is over it (nothing
sent), and no call ever taking the run past it (the run stops, marked incomplete, no verdict); no new task after the
time limit. Too few families or matching requests: how many families have 2+ tasks, about how many more days at the
current rate, and a stop with no result. Synthetic world from test_capC3_world; the model is a stand-in.
"""

from __future__ import annotations

import math

from test_c_practice import Words
from test_capC2_links import DAY, NOW, store  # noqa: F401 - the store fixture
from test_capC3_world import (GOAL, NAMES, NOTES_REQUEST, StubThinker, _hermetic, own_request, switcher,  # noqa: F401
                              two_kinds, world)
from test_capC4_demos import task
from yuki.eval import c3 as C
from yuki.eval import c3_data as D
from yuki.memory import trace_families as F

REQS = [own_request(), own_request(text=NOTES_REQUEST)]


def _run(store, stub, **kw):
    kw = {"need": 1, "min_requests": 1, **kw}
    return C.run(store, kw.pop("requests", REQS), planner=stub, embedder=Words(), now=NOW + 6 * DAY, names=NAMES,
                 **kw)


def test_the_task_cap_keeps_your_own_requests_first(store):
    two_kinds(store)
    conv = D.Request("send a message in the team channel please", NOW + 2 * DAY + 60, "conversation")
    insts, skipped = D.build_instances(store, [conv, *REQS], Words(), max_tasks=1, names=NAMES)
    assert [i.request.origin for i in insts] == ["you"] and insts[0].request.text == REQS[0].text
    assert sorted(s["why"] for s in skipped) == ["over the cap of 1 tasks", "over the cap of 1 tasks"]
    assert C.MAX_TASKS == 30 and C.BUDGET_USD == 3.0 and C.TIME_LIMIT_S < 3600
    out = _run(store, StubThinker(), max_tasks=1)
    assert out["evaluated"] == 1 and len(out["rows"]) == 1
    # two requests for the same task are one evaluation
    insts, skipped = D.build_instances(store, [*REQS, own_request(text="send a message in the team channel now")],
                                       Words(), names=NAMES)
    assert len(insts) == 2 and [s["why"] for s in skipped] == ["the same task as an earlier request"]


def test_over_the_cap_up_front_is_refused_before_any_call(store):
    two_kinds(store)
    stub = StubThinker(est=4.0)                       # typical 2.0 a call, 2 calls a request: $8 expected
    out = _run(store, stub)
    assert out["result"] == "refused" and not stub.calls and "rows" not in out
    assert "over the $3.00 cap" in out["why"] and "about 0 requests would fit" in out["why"]
    assert "Refused before any call" in C.report_text(out)
    # with --repeat 2 the expectation doubles: $2 for one run, $4 for two (a refusal cuts nothing: checked first)
    assert _run(store, StubThinker(est=1.0), repeat=2)["result"] == "refused"
    assert _run(store, StubThinker(est=1.0))["result"] != "refused"


def test_no_call_ever_takes_the_run_past_the_cap(store):
    two_kinds(store)
    stub = StubThinker(est=0.5, cost=0.4)             # expected $1.00; each call may cost up to $0.50
    out = _run(store, stub, budget_usd=1.2)
    assert len(stub.calls) == 2 and out["spent_usd"] == 0.8 <= 1.2   # a third call could pass $1.20: never sent
    assert out["complete"] is False and out["stopped"] == "the cost cap" and out["result"] == "cannot tell"
    assert "stopped early" in " ".join(out["verdict"]["why"])
    skipped = [a for r in out["rows"] for run in r["runs"] for a in run.values() if a["skipped"]]
    assert skipped and all(a["skipped"].startswith("cost cap") and a["scores"] is None for a in skipped)
    assert "The run stopped early at the cost cap." in C.report_text(out)


def test_no_new_task_starts_after_the_time_limit(store):
    two_kinds(store)
    t = [0.0]

    def clock() -> float:
        t[0] += 2000.0
        return t[0]

    stub = StubThinker()
    out = _run(store, stub, time_limit_s=3300.0, clock=clock)
    assert len(out["rows"]) == 1 and out["stopped"] == "the time limit" and out["complete"] is False
    assert out["result"] == "cannot tell"


def test_not_enough_data_says_how_many_and_how_long_and_stops(store):
    stub = StubThinker()
    # nothing recorded yet
    out = _run(store, stub, need=10)
    assert out["result"] == "not enough data" and not stub.calls and "rows" not in out
    st = out["status"]
    assert (st["tasks"], st["families_2plus"], st["families_same_outcome"]) == (0, 0, 0)
    words = C.report_text(out)
    assert "No checked tasks yet" in words and "2 to 3 weeks" in words and "Stopped: no result." in words
    # some checked tasks, no kind done twice to the same end: a lower bound from the tasks a day
    ids = switcher(store, NOW)
    task(store, ids, GOAL, "field_submitted", ids[-1], target="k-msg")
    F.refresh(store, Words(), now=NOW + 100)
    st = D.data_status(store, now=NOW + 2 * DAY, need=10)
    assert st["checked_tasks"] == 1 and st["families_same_outcome"] == 0 and not st["enough"]
    assert st["days_more"] == math.ceil((2 * 10 - 1) / (1 / 2.0)) and st["basis"] == "lower bound"
    assert "at least 38 more days are likely needed" in D.status_words(st)[2]


def test_the_rate_estimate_and_too_few_matching_requests(store):
    world(store, future=False)
    st = D.data_status(store, now=NOW + 4 * DAY, need=3)
    assert st["families_same_outcome"] == 1 and st["families_2plus"] == 1 and not st["enough"]
    assert st["days_more"] == math.ceil((3 - 1) / (1 / st["days_recorded"])) and st["ready_by"] > NOW + 4 * DAY
    lines = D.status_words(st)
    assert lines[1].startswith("Kinds of task you did 2 or more times: 1") and lines[2].startswith("At the current rate, about")
    # enough families, but your requests match none of them: the same honest stop
    stub = StubThinker()
    out = _run(store, stub, requests=[own_request(text="water the tomato plants")])
    assert out["result"] == "not enough data" and not stub.calls
    assert out["why"].startswith("only 0 of your requests match")
    assert out["skipped"][0]["why"] == "no kind of task of yours is near it yet"
    # fewer evaluated requests than the minimum: no result either
    out = C.run(store, [own_request()], planner=stub, embedder=Words(), need=1, min_requests=10, now=NOW + 6 * DAY,
                names=NAMES)
    assert out["result"] == "not enough data" and "at least 10 are needed" in out["why"] and not stub.calls


def test_a_dry_run_makes_no_call_and_gives_no_verdict(store):
    two_kinds(store)
    stub = StubThinker()
    out = _run(store, stub, dry_run=True)
    assert not stub.calls and out["result"] == "dry run" and out["verdict"] is None and out["spent_usd"] == 0
    assert all(r["runs"][0]["B1"]["scores"] for r in out["rows"])           # B1 is code: scored even so
    assert all(r["runs"][0][a]["skipped"] == "dry run" for r in out["rows"] for a in ("B0", "L"))
    assert "Dry run: no model was called" in C.report_text(out)


class _Failing(StubThinker):
    def plan(self, **kw):
        self.calls.append(kw)
        raise ConnectionError("the model's service is down")


def test_a_failed_plan_is_never_scored_as_an_empty_one(store):
    two_kinds(store)
    out = _run(store, _Failing())
    failed = [a for r in out["rows"] for run in r["runs"] for a in run.values() if a["error"]]
    assert failed and all(a["scores"] is None and "ConnectionError" in a["error"] for a in failed)
    assert all(r["runs"][0]["B1"]["scores"] for r in out["rows"])              # the code arm still scored
    assert out["errors"] == len(failed) and out["complete"] is False and out["result"] == "cannot tell"
    assert f"{len(failed)} plans failed" in C.report_text(out)
