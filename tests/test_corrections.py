"""Corrections applied everywhere, to-dos apart from Yuki's promises, talk not meant for Yuki (2026-09-27).

Offline: temp databases and stub model calls, no network, no real memory.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.conversations import ConversationWorker
from yuki.memory.corrections import CorrectionApplier, own_words
from yuki.memory.store import Store


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """The bits of PortraitWorker the applier uses, with scripted model answers."""

    def __init__(self, store, answers):
        import threading

        self.store, self.embedder, self.answers = store, None, list(answers)
        self._run_lock = threading.Lock()
        self.logs = []

    def _call(self, **kw):
        return self.answers.pop(0)

    def fold_corrections(self, run_id, stats, now):
        return 0

    def _render(self, run_id, stats, now):
        return "page"

    def _log(self, type, **fields):
        self.logs.append((type, fields))

    def _seal(self, value):
        return ""


def test_a_correction_is_queued_and_reworded_everywhere_with_the_alias(store):
    store.add_journal(time.time() - 60, None, "Yuki", "Barun must approve the user's CLI scope.", 5)
    fid = store.add_conversation_fact(
        "commitment", "The user will continue the CLI setup once Barun approves.", origin="model", owner="user",
        quote="waiting for Barun's approval")
    cid = store.add_correction('About "Barun" (memory had: "Barun owes you approval"), the user said in their own '
                               'words: "not barun, its varun"')
    assert store.corrections_to_apply() == [cid]
    assert own_words(store.portrait_fact(cid).text) == "not barun, its varun"
    jid = store.journal_between(None, None)[-1].id
    worker = _Worker(store, [
        {"fixes": "Barun is Varun", "aliases": [{"heard": "Barun", "name": "Varun", "why": "misheard"},
                                                {"heard": "Discord", "name": "Warp", "why": "wrong app"}],
         "search_terms": ["Barun"], "elsewhere": True},
        {"edits": [
            {"id": f"J{jid}", "op": "rewrite", "wrong": "Barun", "text": "Varun must approve the user's CLI scope.",
             "subject": "", "reason": "name"},
            {"id": f"C{fid}", "op": "rewrite", "wrong": "Barun",
             "text": "The user will continue the CLI setup once Varun approves.", "subject": "", "reason": "name"},
            {"id": "J999999", "op": "rewrite", "wrong": "x", "text": "y", "subject": "", "reason": "not a candidate"},
        ]},
    ])
    result = CorrectionApplier(worker).apply(cid)
    assert result.ok and result.status == "done"
    assert [c["surface"] for c in result.changes if c["ok"]] == ["journal", "conversation"]
    assert any("J999999" in r for r in result.rejected)
    assert [f.fact for f in store.journal_between(None, None)] == ["Varun must approve the user's CLI scope."]
    new = [f for f in store.conversation_facts(("commitment",))]
    assert len(new) == 1 and "Varun" in new[0].text and new[0].owner == "user"
    assert new[0].quote == "waiting for Barun's approval"          # the user's words are provenance: kept
    assert [a["name"] for a in store.aliases()] == ["Varun"]        # "Warp" never appeared in a rewrite
    status = MemoryClient(store).correction_status(cid)
    assert status["state"] == "done" and status["changed"] == 2
    assert store.corrections_to_apply() == []


def test_a_rewrite_that_appends_news_or_cites_words_the_item_lacks_is_rejected():
    from yuki.memory.corrections import Candidate

    c = Candidate("J1", "J", 1, "The pull request needs Jatin's review.")
    kept, rejected = CorrectionApplier.validate([
        {"id": "J1", "op": "rewrite", "wrong": "Jatin's review", "subject": "", "reason": "",
         "text": "The pull request needs Jatin's review. Jatin was CTO at Corvana and left in April 2026 for Globex."},
        {"id": "J1", "op": "retire", "wrong": "Komal", "text": "", "subject": "", "reason": ""},
    ], [c])
    assert kept == [] and len(rejected) == 2


def test_close_only_corrections_are_never_applied(store):
    store.add_correction("The user said this is done and no longer pending: x", apply=False)
    assert store.corrections_to_apply(since=0) == []


def test_todos_keep_yukis_promises_apart_and_end_three_ways(store):
    mine = store.add_conversation_fact("commitment", "Remind the user about the PR at 5.", origin="model", owner="yuki")
    theirs = store.add_conversation_fact("commitment", "The user will finish the CLI setup.", origin="model",
                                         owner="user")
    added = store.add_todo("laminate the certificates")
    m = MemoryClient(store)
    rows = {t["id"]: t for t in m.todos()}
    assert rows[f"commit:{mine}"]["owner"] == "yuki" and rows[f"commit:{mine}"]["source"] == "commitment"
    assert rows[f"commit:{theirs}"]["owner"] == "user" and rows[f"commit:{theirs}"]["source"] == "chat"
    standing = m.standing_context()
    assert standing.index("Yuki's own commitments") < standing.index("[commit:%d]" % mine)
    assert standing.index("The user's own to-dos") < standing.index("[commit:%d]" % theirs)
    assert m.end_todo(f"commit:{theirs}", "wrong") == f"commit:{theirs}"
    assert m.end_todo(f"todo:{added}", "not_needed") == f"todo:{added}"
    assert m.end_todo(f"commit:{mine}", "later") is None
    assert [t["id"] for t in m.todos()] == [f"commit:{mine}"]
    assert store.conversation_fact(theirs).status == "wrong"
    assert store.todo(added).status == "dropped"


def test_turns_said_to_someone_else_never_become_memory(store):
    to_yuki = store.add_turn("s", 1, time.time() - 30, "remind me to call mom at 6")
    dictated = store.add_turn("s", 2, time.time() - 20, "create a detailed document of this whole chat",
                              addressed="others")
    turns = store.turns_by_ids([to_yuki, dictated])
    assert [t.addressed for t in turns] == [None, "others"]
    worker = ConversationWorker(store, client=object(), embedder=None)
    elsewhere = worker.not_for_yuki([1], turns)          # the model also judged exchange 1 dictation
    assert elsewhere == {to_yuki, dictated}
    elsewhere = worker.not_for_yuki([], turns)
    ops = [{"op": "ADD", "item": 0, "kind": "commitment", "subject": "doc", "text": "Write the document.",
            "quote": "", "due": "", "end_as": "", "card": 0, "owner": "yuki", "exchanges": [2], "reason": ""},
           {"op": "ADD", "item": 0, "kind": "commitment", "subject": "mom", "text": "Remind the user to call mom.",
            "quote": "remind me to call mom at 6", "due": "", "end_as": "", "card": 0, "owner": "yuki",
            "exchanges": [1], "reason": ""}]
    changes, records = worker.validate(ops, turns, [], {}, elsewhere)
    assert [c.text for c in changes] == ["Remind the user to call mom."] and changes[0].owner == "yuki"
    assert records[0].status == "rejected: said to someone else, not to Yuki"
    facts = worker._said_to_yuki([{"text": "a", "exchange": 2}, {"text": "b", "exchange": 1}], turns, elsewhere,
                                 "exchange")
    assert [f["text"] for f in facts] == ["b"]


def test_every_turn_said_to_others_costs_no_call(store):
    tid = store.add_turn("s", 1, time.time(), "okay send it to him", addressed="others")

    class NoClient:
        @property
        def messages(self):
            raise AssertionError("no model call for talk that was not to Yuki")

    worker = ConversationWorker(store, client=NoClient(), embedder=None)
    result = worker.extract("s", store.turns_by_ids([tid]))
    assert result.ok and not store.turns_pending()


def test_aliases_reach_what_memory_already_knows(store):
    from yuki.memory.questions import known_text

    store.add_alias("Barun", "Varun", why="speech recognition misheard it")
    text = known_text(store, lambda v, cap: str(v)[:cap], about="waiting on Barun's approval")
    assert '"Barun" is Varun' in text
    # corrected names are part of the core profile: given whatever the model reads, no word matching
    assert '"Barun" is Varun' in known_text(store, lambda v, cap: str(v)[:cap], about="nothing about them")
    assert MemoryClient(store).speech_names()[:1] == ["Varun"]


def test_the_scheduler_applies_a_queued_correction_first(store, monkeypatch):
    import contextlib

    from yuki.memory import corrections
    from yuki.memory.portrait import PortraitScheduler

    cid = store.add_correction("The user said in their own words: \"x\"")
    applied = []

    class Result:
        ok, folded, changes, candidates, aliases, cost_usd, error = True, 0, [], 0, [], 0.0, None

    monkeypatch.setattr(corrections.CorrectionApplier, "apply", lambda self, i, **kw: applied.append(i) or Result())

    class W:
        def __init__(self):
            self.store = store

        def one_render(self):
            return contextlib.nullcontext()

        def loop_check_pending(self):
            return False

    sched = PortraitScheduler(store, W(), db_path=store.path, idle_fn=lambda: 0.0)
    store.start_correction_pass(cid)
    store.finish_correction_pass(cid, "done")   # a finished pass is not run again
    assert sched.tick() != "correct"
    cid2 = store.add_correction("The user said in their own words: \"y\"")
    assert sched._pending_corrections() == [cid2]
    assert sched.tick() == "correct" and applied == [cid2]
