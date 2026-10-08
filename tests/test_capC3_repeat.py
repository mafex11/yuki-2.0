"""capC3 (report 78 §12 C3; report 105): repeatable within plus or minus 3 points.

The harness itself is deterministic: two whole runs on two copies of the same synthetic database give the same
scores to the point. ``repeat`` 2 runs the arms twice per task and measures how far the arms' means moved; a model
whose answers move more than 3 points leaves no verdict ("cannot tell"). The model is a stand-in.
"""

from __future__ import annotations

from pathlib import Path

from test_c_practice import Words
from test_capC2_links import DAY, NOW, store  # noqa: F401 - the store fixture
from test_capC3_world import NAMES, NOTES_REQUEST, StubThinker, _hermetic, own_request, two_kinds  # noqa: F401
from yuki.eval import c3 as C
from yuki.eval import c3_data as D

REQS = [own_request(), own_request(text=NOTES_REQUEST)]


def _run(store, stub, **kw):
    return C.run(store, REQS, planner=stub, embedder=Words(), need=1, min_requests=1, now=NOW + 6 * DAY,
                 names=NAMES, **kw)


def _scores(out):
    return [[{a: (x[a]["scores"] or {}).get("composite") for a in C.ARMS} for x in r["runs"]] for r in out["rows"]]


def test_two_whole_runs_on_two_copies_agree_to_the_point(store, tmp_path):
    two_kinds(store, learned=True)
    copies = [D.copy_db(Path(store.path), tmp_path / f"copy{i}") for i in (1, 2)]
    outs = []
    for db in copies:
        s = D.open_copy(db)
        try:
            outs.append(_run(s, StubThinker()))
        finally:
            s.close()
    a, b = outs
    assert _scores(a) == _scores(b) and a["summary"] == b["summary"]
    assert a["verdict"]["L_vs_B0"] == b["verdict"]["L_vs_B0"] and a["result"] == b["result"]
    # within 3 points: here exactly the same
    from yuki.eval import c3_score as S

    assert S.drift(a["summary"][0], b["summary"][0])["max"] == 0.0


def test_repeat_two_measures_the_drift_and_a_steady_model_passes_it(store):
    two_kinds(store)
    out = _run(store, StubThinker(), repeat=2)
    assert all(len(r["runs"]) == 2 for r in out["rows"]) and len(out["summary"]) == 2
    assert out["drift"]["max"] == 0.0 and out["verdict"]["repeat_drift"] == 0.0
    assert "differed" not in " ".join(out["verdict"]["why"])
    assert "repeatable within 3 points: yes" in C.report_text(out)


def test_a_model_that_moves_more_than_3_points_leaves_no_verdict(store):
    two_kinds(store)
    out = _run(store, StubThinker(jitter=True), repeat=2)
    assert out["drift"]["max"] > 3.0
    assert out["result"] == "cannot tell" and any("differed" in w for w in out["verdict"]["why"])
    assert "repeatable within 3 points: no" in C.report_text(out)
