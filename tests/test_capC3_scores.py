"""capC3 (report 78 §10.3; report 105): each score computed right on tiny hand-checked fixtures, and the verdict.

Element accuracy (Mind2Web style, the strict plan-only reading), step precision and recall (WONDERBREAD style,
order aside), route distance (edit distance over element keys and machine steps), the composite, the spread, paired
comparisons and the rule that the learning build must beat BOTH baselines. Pure functions, no database.
"""

from __future__ import annotations

import pytest

from yuki.eval import c3_score as S


def test_hand_checked_plan_with_one_extra_step():
    # the user: a, c; the plan: a, b, c
    got = S.score(["a", "b", "c"], ["a", "c"])
    assert got["element_accuracy"] == 50.0             # a right at step 1; at step 2 the plan has b, not c
    assert got["precision"] == pytest.approx(66.67)    # 2 of the plan's 3 steps the user took
    assert got["recall"] == 100.0                      # both of the user's steps are in the plan
    assert got["f1"] == 80.0                           # 2 * (2/3 * 1) / (2/3 + 1)
    assert got["distance"] == 1                        # delete b
    assert got["distance_share"] == pytest.approx(33.33) and got["route"] == pytest.approx(66.67)
    assert got["composite"] == pytest.approx((50 + 80 + 66.6667) / 3, abs=0.01)
    assert (got["plan_steps"], got["user_steps"]) == (3, 2)


def test_hand_checked_swap_missing_and_wrong():
    # a swap: every step there, two in the wrong place
    swap = S.score(["b", "a", "c"], ["a", "b", "c"])
    assert swap["element_accuracy"] == pytest.approx(33.33) and swap["f1"] == 100.0 and swap["distance"] == 2
    # nothing planned
    empty = S.score([], ["a", "b"])
    assert (empty["element_accuracy"], empty["precision"], empty["recall"], empty["f1"]) == (0, 0, 0, 0)
    assert empty["distance"] == 2 and empty["route"] == 0.0 and empty["composite"] == 0.0
    # all wrong, same length
    wrong = S.score(["x", "y"], ["a", "b"])
    assert wrong["composite"] == 0.0 and wrong["distance"] == 2
    # exactly right
    assert S.score(["a", "b"], ["a", "b"])["composite"] == 100.0
    # both empty: nothing to do, nothing done
    assert S.score([], [])["composite"] == 100.0
    # a repeated step counts once in each list it appears (multiset), never twice for one
    rep = S.score(["a", "b", "a"], ["a", "b"])
    assert rep["precision"] == pytest.approx(66.67) and rep["recall"] == 100.0


def test_tokens_are_compared_as_the_trace_writes_them():
    # repeats in a row fold, as C2 folds a route
    assert S.fold(["k1", "k1", "k2", "k1"]) == ["k1", "k2", "k1"]
    # a shortcut in any case and modifier order is one shortcut; the app as its key
    assert S.canon("Chatter.exe|keys|shift+ctrl+t") == S.canon("chatter|keys|Ctrl+Shift+T") == "chatter|keys|ctrl+shift+t"
    assert S.chord("ctrl + k") == "Ctrl+K" and S.chord("enter") == "Enter" and S.chord("") == ""
    # a file step without its extension (the planner cannot know it)
    assert S.canon("notepadx|file_save|.txt") == S.canon("notepadx|file_save") == "notepadx|file_save"
    assert S.score(["notepadx|file_save"], ["notepadx|file_save|.pdf"])["composite"] == 100.0
    # an element key is compared as it is
    assert S.canon("ab12") == "ab12" and S.canon("") is None


@pytest.mark.parametrize(("planned", "trace"), [
    ("Control+K", "Ctrl+K"), ("control+shift+k", "Ctrl+Shift+K"), ("Windows+E", "Win+E"), ("Cmd+K", "Win+K"),
    ("Meta+K", "Win+K"), ("Super+E", "Win+E"), ("Option+F4", "Alt+F4"), ("Escape", "Esc"), ("Return", "Enter"),
    ("Del", "Delete"), ("Shift+Del", "Shift+Delete"), ("PgUp", "PageUp"), ("PgDn", "PageDown"),
    ("Page Down", "PageDown"), ("ArrowDown", "Down"), ("Down Arrow", "Down"), ("Shift+ArrowUp", "Shift+Up"),
    ("Spacebar", "Space"), ("Ctrl+Enter", "Ctrl+Enter")])
def test_other_spellings_of_a_key_are_the_traces_names(planned, trace):
    """Second review A2: the thinker writes keys freely; the trace writes ``yuki.memory.trace_keys``' names."""
    assert S.chord(planned) == S.chord(trace) == trace
    assert S.score([f"w|keys|{planned}"], [f"w|keys|{trace}"])["composite"] == 100.0


def test_a_modifier_the_trace_has_no_name_for_is_kept_never_dropped():
    """Second review A2: an unknown modifier never turns the chord into the plain key (or a smaller chord)."""
    assert S.chord("Hyper+K") == "Hyper+K" and S.chord("Ctrl+Hyper+K") == "Ctrl+Hyper+K"
    assert S.score(["w|keys|Hyper+K"], ["w|keys|K"])["composite"] == 0.0
    assert S.score(["w|keys|Control+Shift+K"], ["w|keys|Shift+K"])["composite"] == 0.0
    assert S.score(["w|keys|Control+K"], ["w|keys|K"])["composite"] == 0.0


def test_edit_distance_hand_checked():
    assert S.edit_distance(list("kitten"), list("sitting")) == 3
    assert S.edit_distance([], ["a"]) == 1 and S.edit_distance(["a"], ["a"]) == 0


def test_aggregate_paired_and_drift():
    agg = S.aggregate([10, 20, 30])
    assert agg == {"n": 3, "mean": 20.0, "sd": 10.0, "min": 10.0, "max": 30.0}
    assert S.aggregate([5])["sd"] == 0.0 and S.aggregate([])["mean"] is None
    p = S.paired([50, 60, 70, 40], [40, 59, 80, 40])
    assert p["mean_diff"] == pytest.approx(0.25) and (p["wins"], p["ties"], p["losses"]) == (1, 2, 1)
    s1 = {"L": S.arm_summary([S.score(["a"], ["a"]), S.score([], ["a"])])}
    s2 = {"L": S.arm_summary([S.score(["a"], ["a"]), S.score(["a"], ["a"])])}
    d = S.drift(s1, s2)
    assert s1["L"]["composite"]["mean"] == 50.0 and d["max"] == 50.0 and d["by_arm"]["L"]["composite"] == 50.0
    assert d["composite"] == 50.0


def _row(b0: float, b1: float, lv: float) -> dict:
    return {a: {k: v for k in S.SCORE_KEYS} for a, v in (("B0", b0), ("B1", b1), ("L", lv))}


def test_the_learning_build_must_beat_both_baselines():
    rows = [_row(20, 40, 80)] * 10
    v = S.verdict(rows, repeat_drift=0.0)
    assert v["result"] == "pass" and v["beats"] == {"B0": True, "B1": True} and v["L_vs_B1"]["mean_diff"] == 40.0
    # beating today's Yuki alone proves nothing
    v = S.verdict([_row(20, 80, 80)] * 10, repeat_drift=0.0)
    assert v["result"] == "fail" and v["beats"] == {"B0": True, "B1": False}
    assert "copying the nearest past example" in " ".join(v["why"])
    # within the margin is no win
    v = S.verdict([_row(20, 78, 80)] * 10, repeat_drift=0.0)
    assert v["result"] == "fail"
    # a mean carried by one big win while most requests are lost is no win either
    rows = [_row(0, 0, 100)] + [_row(0, 50, 45)] * 9                   # +100 once, -5 nine times: +5.5 mean
    v = S.verdict(rows, repeat_drift=0.0)
    assert v["L_vs_B1"]["mean_diff"] > 3 and v["L_vs_B1"]["losses"] == 9 and v["result"] == "fail"


def test_no_verdict_on_too_few_an_incomplete_run_or_runs_that_disagree():
    assert S.verdict([_row(0, 0, 100)] * 9, repeat_drift=0.0)["result"] == "cannot tell"
    v = S.verdict([_row(0, 0, 100)] * 10, complete=False, repeat_drift=0.0)
    assert v["result"] == "cannot tell" and "stopped early" in v["why"][0]
    v = S.verdict([_row(0, 0, 100)] * 10, repeat_drift=3.5)
    assert v["result"] == "cannot tell" and "differed" in v["why"][0]
    assert S.verdict([_row(0, 0, 100)] * 10, repeat_drift=3.0)["result"] == "pass"
    # review F4: one run never passes (or fails): whether a second would agree within 3 points is not known
    v = S.verdict([_row(10, 10, 50)] * 10)
    assert v["result"] == "cannot tell" and v["repeat_drift"] is None and "planned only once" in v["why"][0]
    # a request an arm did not plan is left out of the comparison
    rows = [_row(0, 0, 100)] * 10 + [{"B0": None, "B1": None, "L": {k: 100 for k in S.SCORE_KEYS}}]
    assert S.verdict(rows, repeat_drift=0.0)["requests"] == 10
