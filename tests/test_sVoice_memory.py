"""sVoice: the memory side of preferences and the old learners (map-skills S4.2, S4.3; PLAN.md D.2 rows
``memory/conversations.py:399`` and ``memory/practice_learn.py:1007``, ``:1176``).

Temp databases, sCore's invented "Demo Player" connection, stand-in turns and cards. No model call.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.store import ConversationTurn, Store
from yuki.skills import connection as C
from yuki.skills import prefs as P
from yuki.skills import registry as R

FIXTURES = Path(__file__).with_name("skills_fixtures")
CONTROL = "demo.volume:control"


@pytest.fixture(autouse=True)
def _demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    yield
    C.clear_cache()


@pytest.fixture()
def store(tmp_path: Path):
    s = Store.open(tmp_path / "memory.db")
    R.seed(s, C.load())
    yield s
    s.close()


def _turn(n: int, words: str) -> ConversationTurn:
    return ConversationTurn(id=n, session_id="s", request_id=n, at=time.time() - 60 + n, user_text=words,
                            reply_text="Okay.", actions=[], outcome="final", extracted=False, attempts=0,
                            summary_id=None, created_at=time.time())


def _worker(store: Store):
    from yuki.memory.conversations import ConversationWorker

    w = ConversationWorker.__new__(ConversationWorker)
    w.store = store
    return w


# -- the extractor's usual field: one string from a closed list --------------------------------------------------------


def test_the_extractor_schema_gains_one_strict_string_field() -> None:
    from yuki.memory.conversations import RECORD_CONVERSATION_TOOL, USUAL_FIELD

    item = RECORD_CONVERSATION_TOOL["input_schema"]["properties"]["operations"]["items"]
    assert item["properties"]["usual"] == {"type": "string", "description": USUAL_FIELD}
    assert "usual" in item["required"] and set(item["required"]) == set(item["properties"])   # strict: all required
    assert RECORD_CONVERSATION_TOOL["strict"] is True


def test_the_message_lists_the_closed_choices(store: Store) -> None:
    lines = _worker(store).usual_lines()
    assert lines.startswith("USUAL CHOICES") and f"- {CONTROL}=in_app:" in lines and f"- {CONTROL}=mixer:" in lines


def test_a_usual_from_their_words_is_proposed_and_asked_once_never_applied(store: Store) -> None:
    w = _worker(store)
    turns = [_turn(1, "from now on always use the mixer for demo player")]
    ops = [{"op": "ADD", "kind": "preference", "quote": "always use the mixer for demo player", "exchanges": [1],
            "usual": f"{CONTROL}=mixer"}]
    out = w.usual_pass(ops, turns, {}, set())
    assert out == {"proposed": 1, "confirmed": 0, "rejected": 0, "already": 0}
    assert P.usual(store, CONTROL) is None                       # never applied silently
    qid = P.pending_question(store, CONTROL, "mixer")
    assert qid is not None and store.question(qid).origin == "usual"
    assert w.usual_pass(ops, turns, {}, set())["already"] == 1   # one card per proposal
    # their words answering that very card confirm it
    card = (SimpleNamespace(id=5, ref=f"question:{qid}", kind="question"), True)
    answer = [_turn(2, "yes keep the mixer")]
    got = w.usual_pass([{"op": "ADD", "kind": "feedback", "quote": "yes keep the mixer", "exchanges": [1],
                         "usual": f"{CONTROL}=mixer"}], answer, {2: card}, set())
    assert got["confirmed"] == 1 and P.usual(store, CONTROL) == "mixer"


def test_a_usual_not_in_their_words_or_not_said_to_yuki_is_rejected(store: Store) -> None:
    w = _worker(store)
    turns = [_turn(1, "turn it down"), _turn(2, "always use the mixer")]
    bad = [{"quote": "always use the slider", "exchanges": [1], "usual": f"{CONTROL}=in_app"},       # not their words
           {"quote": "always use the mixer", "exchanges": [2], "usual": "demo.volume:control=loud"},  # not listed
           {"quote": "always use the mixer", "exchanges": [2], "usual": "use the mixer"}]            # free text
    assert w.usual_pass(bad, turns, {}, set())["rejected"] == 3
    said_elsewhere = [{"quote": "always use the mixer", "exchanges": [2], "usual": f"{CONTROL}=mixer"}]
    assert w.usual_pass(said_elsewhere, turns, {}, {2})["rejected"] == 1   # said to someone else
    assert store._query("SELECT count(*) FROM pref_events")[0][0] == 0
    assert w.usual_pass([{"quote": "x", "exchanges": [1], "usual": ""}], turns, {}, set()) == {
        "proposed": 0, "confirmed": 0, "rejected": 0, "already": 0}


# -- the old learners: no post-mortem for a skill run, no pitfall for an app a trusted skill covers ---------------------


def test_a_request_that_ran_a_skill_gets_no_text_pass(store: Store) -> None:
    from yuki.memory import practice_learn as L

    key = R.start_run(store, skill=C.load().get("demo.volume"), origin="voice", request_id="s-3")
    R.finish_run(store, key, outcome="failed", checked=False)
    header = SimpleNamespace(request_id="s-3")
    assert L.skill_of_run(store, header) == "demo.volume"
    assert L.skill_of_run(store, SimpleNamespace(request_id="s-4")) is None
    signals = {"cancelled": True, "outcome": "cancelled", "actions": 2}
    mode, reasons = L.decide(signals, [], [], skill="demo.volume")
    assert mode is None and "skill run" in reasons[0]
    assert L.decide(signals, [], [])[0] == "postmortem"   # without a skill the old learner is unchanged


def test_no_pitfall_is_written_for_an_app_a_trusted_skill_covers(store: Store) -> None:
    from yuki.memory import practice_learn as L

    assert L.skill_covered_apps(store) == set()          # no verified run yet: nothing is covered
    key = R.start_run(store, skill=C.load().get("demo.volume"), origin="voice")
    R.finish_run(store, key, outcome="done", checked=True)
    assert L.skill_covered_apps(store) == {"demoplayer"}
    checked = L.Checked(outcome="failed", ops=[{"op": "ADD", "kind": "pitfall", "intent": "change the volume",
                                                "apps": ["DemoPlayer.exe"], "body": {"text": "x"}}])
    ctx = L.Ctx(mode="postmortem", run_key="hand:s:1")
    written = L.apply(store, checked, ctx, embedder=None, origin="postmortem", now=time.time())
    assert written["added"] == [] and written["skipped"] == ["skill_covered"]
    assert store._query("SELECT count(*) FROM playbooks")[0][0] == 0
