"""sVoice: the memory side of preferences (map-skills S4.2, S4.3; PLAN.md D.2 row ``memory/conversations.py:399``).

r30: the old learners' tests (no text pass for a skill run, no pitfall for an app a trusted skill covers) went with
``memory/practice_learn.py`` (deleted by iMem, 18 Oct).

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
    assert store._query("SELECT count(*) FROM pref_events WHERE type = 'asked'")[0][0] == 1
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


def test_a_usual_said_again_after_its_card_was_answered_gets_a_new_card(store: Store) -> None:
    """Review r1 M1: each card has its own subject (the words' own moment), so an answered card never swallows a
    later proposal of the same option; the same words processed again never ask twice."""
    w = _worker(store)
    first = [_turn(1, "always use the mixer for demo player")]
    ops = [{"op": "ADD", "kind": "preference", "quote": "always use the mixer for demo player", "exchanges": [1],
            "usual": f"{CONTROL}=mixer"}]
    assert w.usual_pass(ops, first, {}, set())["proposed"] == 1
    qid = P.pending_question(store, CONTROL, "mixer")
    store._write(lambda c: c.execute("UPDATE questions SET status = 'answered' WHERE id = ?", (qid,)))   # declined
    assert w.usual_pass(ops, first, {}, set())["proposed"] == 0          # the same words again: no second card
    later = [_turn(9, "no really, always use the mixer for demo player")]
    ops2 = [{"op": "ADD", "kind": "preference", "quote": "always use the mixer for demo player", "exchanges": [1],
             "usual": f"{CONTROL}=mixer"}]
    assert w.usual_pass(ops2, later, {}, set())["proposed"] == 1         # said again later: a new card goes out
    new = P.pending_question(store, CONTROL, "mixer")
    assert new is not None and new != qid
    assert store._query("SELECT count(*) FROM pref_events WHERE type = 'asked'")[0][0] == 2   # one tie per card
