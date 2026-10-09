"""evalFix: the caps (≤ 55 min, ≤ $3, enforced in code), the requests suite's plan, dry run, refusal and scoring
(right skill and slots ≥ 90%, Spotify volume always Spotify's own, two runs within 1 fixture), the scorecard and the
user's-words reading. The resolver is a stand-in (S builds the real one); no model is called.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from test_evalFix_world import hermetic, live, ts  # noqa: F401
from yuki.eval import dbcopy
from yuki.eval import habits_replay as H

FIX = [
    H.Fixture("a1", "open my meeting", 1.0, "meet.open", {}),
    H.Fixture("a2", "spotify down a bit", 2.0, "volume", {"target": "Spotify", "change": "-10"}),
    H.Fixture("a3", "play my japanese playlist", 3.0, "spotify.playlist", {"playlist": "Japanese"}),
    H.Fixture("a4", "what's the weather", 4.0, None, {}),
]


class Stub:
    """A resolver stand-in: answers from a table, costs ``cost`` a call, estimates ``(typical, worst)``."""

    def __init__(self, answers, *, cost=0.01, estimate=(0.01, 0.02), clock=None, tick=0.0):
        self.answers, self.cost, self.est, self.calls = answers, cost, estimate, []
        self.clock, self.tick = clock, tick

    def estimate_usd(self, words):
        return self.est

    def resolve(self, words, at):
        self.calls.append((words, at))
        if self.clock is not None:
            self.clock.t += self.tick
        a = self.answers.get(words)
        if isinstance(a, Exception):
            raise a
        return {**(a or {"skill": None, "slots": {}}), "cost_usd": self.cost}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


GOOD = {"open my meeting": {"skill": "meet.open", "slots": {}},
        "spotify down a bit": {"skill": "volume", "slots": {"target": "spotify ", "change": -10, "control": "in_app"}},
        "play my japanese playlist": {"skill": "spotify.playlist", "slots": {"playlist": "japanese"}},
        "what's the weather": {"skill": None, "slots": {}}}


# ---------------------------------------------------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("budget,minutes", [(3.01, 55), (10, 30), (3, 56), (3, 0), (-1, 10)])
def test_caps_over_the_users_limits_are_refused_up_front(budget, minutes):
    with pytest.raises(ValueError):
        H.Caps(budget, minutes)


def test_the_defaults_are_the_users_caps():
    c = H.Caps()
    assert c.budget_usd == 3.0 and c.limit_s == 55 * 60


def test_no_call_can_take_a_run_past_the_cost_cap():
    caps = H.Caps(0.05, 10)
    stub = Stub(GOOD, cost=0.02, estimate=(0.001, 0.02))
    out = H.run_requests(FIX, stub, caps, repeat=2)
    assert caps.spent <= 0.05 + 1e-9 and len(stub.calls) == 2
    assert out["stopped"].startswith("the cost cap") and out["result"] == "incomplete"


def test_no_call_starts_after_the_time_limit():
    clock = Clock()
    caps = H.Caps(3.0, 1, clock=clock)            # one minute
    stub = Stub(GOOD, clock=clock, tick=25.0)     # each call takes 25 s
    out = H.run_requests(FIX, stub, caps, repeat=2)
    assert len(stub.calls) == 3 and out["stopped"] == "the time limit" and out["result"] == "incomplete"


def test_an_expected_cost_over_the_cap_is_refused_before_any_call():
    stub = Stub(GOOD, estimate=(0.5, 0.6))
    out = H.run_requests(FIX * 2, stub, H.Caps(3.0, 55), repeat=2)
    assert out["result"] == "refused" and "cap" in out["refusal"] and stub.calls == []


def test_a_dry_run_calls_nothing_and_prints_the_plan():
    stub = Stub(GOOD)
    out = H.run_requests(FIX, stub, H.Caps(), repeat=2, dry_run=True)
    assert stub.calls == [] and out["result"] == "dry run" and out["plan"]["calls"] == 8
    text = "\n".join(H.requests_text(out))
    assert "8 talker calls" in text and "no model was called" in text


def test_the_planned_spend_of_25_fixtures_twice_fits_the_cap():
    fx = [H.Fixture(f"f{i}", "turn spotify down a little bit please", 0.0, "volume", {}) for i in range(25)]
    plan = H.plan_requests(fx, 2)
    assert plan["calls"] == 50
    assert plan["expected_usd"] < 0.5 and plan["worst_usd"] < 3.0
    assert H.Caps().refusal(plan["expected_usd"], plan["expected_s"]) is None


def test_without_the_resolver_the_suite_says_what_it_needs():
    out = H.run_requests(FIX, None, H.Caps(), repeat=2)
    assert out["result"] == "not available" and "sCore" in out["why"]
    assert H.load_resolver() is None or callable(getattr(H.load_resolver(), "resolve", None))


# ---------------------------------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------------------------------


def test_all_right_passes_every_bar():
    out = H.run_requests(FIX, Stub(GOOD), H.Caps(), repeat=2)
    assert out["result"] == "pass", out["bars"]
    assert [r["right"] for r in out["per_run"]] == [4, 4]
    assert out["per_run"][0]["spotify_volume"] == 1 and out["per_run"][0]["spotify_in_app"] == 1
    assert out["disagree"] == 0


def test_spotify_volume_to_the_mixer_fails_even_with_the_right_skill():
    bad = {**GOOD, "spotify down a bit": {"skill": "volume", "slots": {"target": "spotify", "change": "-10",
                                                                      "control": "mixer"}}}
    out = H.run_requests(FIX, Stub(bad), H.Caps(), repeat=1)
    assert out["per_run"][0]["right"] == 4 and out["per_run"][0]["spotify_in_app"] == 0
    assert not out["bars"]["spotify_in_app"]["pass"] and out["result"] == "fail"


def test_a_wrong_slot_value_is_wrong_and_blank_label_slots_are_not_compared():
    bad = {**GOOD, "play my japanese playlist": {"skill": "spotify.playlist", "slots": {"playlist": "Lo-fi"}}}
    out = H.run_requests(FIX, Stub(bad), H.Caps(), repeat=1)
    assert out["per_run"][0]["skill_right"] == 4 and out["per_run"][0]["right"] == 3
    assert out["per_run"][0]["right_rate"] == 0.75 and not out["bars"]["right"]["pass"]


def test_runs_that_disagree_on_two_fixtures_fail_the_repeat_bar():
    class Flaky(Stub):
        def resolve(self, words, at):
            got = super().resolve(words, at)
            if len(self.calls) > len(FIX) and words in ("open my meeting", "what's the weather"):
                return {"skill": "youtube.home", "slots": {}, "cost_usd": 0.01}
            return got

    out = H.run_requests(FIX, Flaky(GOOD), H.Caps(), repeat=2)
    assert out["disagree"] == 2 and not out["bars"]["agree"]["pass"]


def test_a_failed_call_is_that_fixtures_error_not_a_crash():
    out = H.run_requests(FIX, Stub({**GOOD, "open my meeting": TimeoutError("slow")}), H.Caps(), repeat=1)
    assert out["per_run"][0]["errors"] == 1 and out["per_run"][0]["right"] == 3


def test_the_score_is_content_free():
    out = H.run_requests(FIX, Stub(GOOD), H.Caps(), repeat=2)
    text = json.dumps(out) + "\n".join(H.requests_text(out))
    for f in FIX:
        assert f.words not in text
    assert "Japanese" not in text and "japanese" not in text


# ---------------------------------------------------------------------------------------------------------------------
# The user's words
# ---------------------------------------------------------------------------------------------------------------------


def test_user_words_reads_yukis_formats_only():
    assert H.user_words('[The user pressed "Do it" on a card]\nopen it') == ""
    assert H.user_words("[Card: break]\nnot now\n\n[coach note]") == "not now"
    heard = ('spotify volume down\n\n[Asked by voice. What the user said most recently (newest last): '
             '"uh spotify" / "spotify down a bit"]')
    assert H.user_words(heard) == "uh spotify / spotify down a bit"
    assert H.user_words("  play   my mix  ") == "play my mix"


# ---------------------------------------------------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------------------------------------------------


def _skill_tables(path, now):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE skills (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE);
        CREATE TABLE skill_runs (id INTEGER PRIMARY KEY, at REAL NOT NULL, skill_id INTEGER NOT NULL,
            outcome TEXT NOT NULL, checked INTEGER, ms REAL, recheck INTEGER, request_id TEXT);
        CREATE TABLE routine_events (id INTEGER PRIMARY KEY, routine_id INTEGER, at REAL NOT NULL, kind TEXT NOT NULL,
            detail TEXT);
    """)
    conn.execute("INSERT INTO skills(id, key) VALUES (1, 'volume'), (2, 'meet.open')")
    rows = [(1, "done", 1, 1200, 1), (1, "done", 1, 1800, 1), (1, "done", None, 2500, None),
            (1, "failed", 0, 4000, None), (2, "done", 1, 900, 1), (2, "done", 1, 1100, 0)]
    conn.executemany("INSERT INTO skill_runs(at, skill_id, outcome, checked, ms, recheck) VALUES (?,?,?,?,?,?)",
                     [(now - 3600 * (i + 1), *r) for i, r in enumerate(rows)])
    conn.execute("INSERT INTO skill_runs(at, skill_id, outcome, checked, ms) VALUES (?, 1, 'done', 1, 100)",
                 (now - 30 * 86400,))                                   # outside the week
    conn.executemany("INSERT INTO routine_events(routine_id, at, kind, detail) VALUES (1, ?, ?, ?)",
                     [(now - 7200, "offered", json.dumps({"lead_s": 300})), (now - 7100, "accepted", "{}"),
                      (now - 3600, "offered", json.dumps({"game_front": True})), (now - 3000, "held", "{}")])
    conn.execute("INSERT INTO portrait_runs(at, kind, model, cost_usd, outcome) VALUES (?, 'nightly', 'm', 0.70,"
                 " 'ok')", (now - 86400,))
    conn.commit()
    conn.close()


def test_the_scorecard_measures_the_purpose_tests_content_free(live, tmp_path):
    now = ts("2026-10-11", "19:00:00")
    path = dbcopy.copy_db(live, tmp_path / "copy")
    _skill_tables(path, now)
    card = H.scorecard(path, days=7, now=now)
    p1 = card["purpose_1_fast_and_true"]
    vol, meet = p1["per_skill"]["volume"], p1["per_skill"]["meet.open"]
    assert (vol["runs"], vol["done"], vol["unconfirmed"], vol["failed"]) == (4, 2, 1, 1)
    assert vol["p50_s"] == 1.8 and not vol["pass"]              # done 2/4 < 9/10
    assert meet["false_done"] == 1 and not meet["pass"] and p1["false_done"] == 1
    p3 = card["purpose_3_picks_it_up"]
    assert p3["events"] == {"accepted": 1, "held": 1, "offered": 2} and p3["over_game_or_call"] == 1 and not p3["pass"]
    assert card["background_cost"]["total_usd"] == 0.7 and card["background_cost"]["usd_per_day"] == 0.1
    assert card["purpose_2_shown_once"]["available"] is False
    text = json.dumps(card) + "\n".join(H.scorecard_text(card))
    assert "volume" in text and "Zen" not in text


def test_the_scorecard_says_what_is_not_built_yet(live, tmp_path):
    path = dbcopy.copy_db(live, tmp_path / "copy")
    card = H.scorecard(path, days=7)
    assert card["purpose_1_fast_and_true"] == {"available": False, "needs": "skill_runs (sCore, migration 41)"}
    assert card["purpose_3_picks_it_up"]["available"] is False
    assert card["footprint"]["available"] is False
    lines = H.scorecard_text(card)
    assert any(line.startswith("1. Fast and true: not yet") for line in lines)
