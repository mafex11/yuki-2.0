"""Questions are run past what memory already holds (yuki.memory.questions: the vetting plumbing).

Temp databases and a stub model client only; the judgement itself is the model's (measured on a DB copy,
docs/MEMORY.md "Questions for the user").
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory import questions as Q
from yuki.memory.store import FactChange, NewQuestion, Store

T0 = time.time() - 3600


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


class StubClient:
    """Answers every vet_questions call with ``decisions`` and records the requests."""

    def __init__(self, decisions):
        self.decisions = decisions
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        block = SimpleNamespace(type="tool_use", name="vet_questions", input={"decisions": self.decisions})
        return SimpleNamespace(content=[block], usage={"input_tokens": 900, "output_tokens": 80})


class StubEmbedder:
    model_name = "stub"

    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


def _question(subject, question, **kw):
    return NewQuestion(at=T0, kind="fact", subject=subject, question=question, evidence="seen on screen",
                       reason="", importance=kw.pop("importance", 6), origin="journal", **kw)


def test_parse_vetting_keeps_a_question_unless_a_shown_item_settles_it():
    shown = {"F1": Q.MemoryItem("F1", "work: works at Acme", 1), "J9": Q.MemoryItem("J9", "seen: an Acme letter")}
    out = Q.parse_vetting({"decisions": [
        {"n": 1, "decision": "drop", "refs": ["[F1]"], "why": "works there", "question": None},
        {"n": 2, "decision": "drop", "refs": ["F77"], "why": "not shown", "question": None},
        {"n": 3, "decision": "drop", "refs": ["J9"], "why": "its own evidence", "question": None},
        {"n": 4, "decision": "rephrase", "refs": ["F1"], "why": "", "question": None},
        {"n": 5, "decision": "rephrase", "refs": ["F1"], "why": "", "question": "What is the new Acme letter for?"},
        {"n": 5, "decision": "drop", "refs": ["F1"], "why": "a second answer for 5", "question": None},
    ]}, 6, shown, own=[set(), set(), {"J9"}, set(), set(), set()])
    assert [v.decision for v in out] == ["drop", "keep", "keep", "keep", "rephrase", "keep"]
    assert out[0].fact_id == 1 and out[0].refs == ["F1"]
    assert out[4].question == "What is the new Acme letter for?"
    assert Q.parse_vetting(None, 2, shown)[1].decision == "keep"   # an unreadable answer loses nothing


def test_vet_new_drops_and_rephrases_before_storing_and_logs_ids_only(store):
    [(_ok, fid)] = store.commit_portrait_changes(None, [FactChange(
        "ADD", None, "work", "Acme", "The user works at Acme as a founding engineer.", 0.9, [], T0, importance=8)])
    client = StubClient([
        {"n": 1, "decision": "drop", "refs": [f"F{fid}"], "why": "they work there", "question": None},
        {"n": 2, "decision": "rephrase", "refs": [f"F{fid}"], "why": "", "question": "What is the new Acme letter for?"},
        {"n": 3, "decision": "keep", "refs": [], "why": "unknown", "question": None},
    ])
    logged = []
    qs = [_question("Acme offer", "Has the user accepted the Acme offer?"),
          _question("Acme letter", "Has the user joined Acme?"),
          _question("Bro", "Who is Bro to the user?")]
    kept = Q.vet_new(store, qs, client=client, embedder=StubEmbedder(), where="journal",
                     log=lambda type, **f: logged.append((type, f)))
    assert [q.subject for q in kept] == ["Acme letter", "Bro"]
    assert kept[0].question == "What is the new Acme letter for?" and kept[0].vector is not None
    sent = client.requests[0]["messages"][0]["content"]
    assert f"[F{fid}] work Acme" in sent and "Has the user accepted the Acme offer?" in sent   # the core profile
    (kind, fields), = logged
    assert kind == "question_vet" and fields["kept"] == 2
    assert [d["decision"] for d in fields["decisions"]] == ["drop", "rephrase", "keep"]
    assert fields["decisions"][0]["refs"] == [f"F{fid}"]
    assert "Acme" not in repr(fields)   # content-free


def test_revet_drops_a_waiting_question_with_the_fact_that_answers_it(store):
    [(_ok, fid)] = store.commit_portrait_changes(None, [FactChange(
        "ADD", None, "work", "Acme", "The user works at Acme as a founding engineer.", 0.9, [], T0)])
    (qid, outcome), (qid2, _o) = store.add_questions([_question("Acme offer", "Has the user accepted the Acme offer?"),
                                                      _question("Bro", "Who is Bro to the user?")])
    assert outcome == "added"
    client = StubClient([{"n": 1, "decision": "keep", "refs": [], "why": "", "question": None},
                         {"n": 2, "decision": "drop", "refs": [f"F{fid}"], "why": "", "question": None}])
    dry = Q.revet(store, client=client, dry_run=True)   # newest first: Bro is 1, the offer is 2
    assert (dry["dropped"], dry["kept"]) == (1, 1) and store.question(qid).status == "open"
    Q.revet(store, client=client)
    q = store.question(qid)
    assert q.status == "dropped" and q.answer_fact_id == fid
    assert store.question(qid2).status == "open"
    # the same question found again is known, not asked again
    assert store.add_questions([_question("Acme offer", "Has the user accepted the Acme offer?")])[0][1] == "known"


def test_known_text_always_gives_the_core_profile_without_matching_words(store):
    store.commit_portrait_changes(None, [FactChange(
        "ADD", None, "work", "Acme", "The user works at Acme as a founding engineer.", 0.9, [], T0)])
    store.add_alias("Barun", "Varun", why="speech recognition misheard it")
    text = Q.known_text(store, lambda v, cap: str(v)[:cap], about="an offer letter in a cloud drive")
    assert "works at Acme" in text and '"Barun" is Varun' in text
