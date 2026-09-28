"""The app's short labels: model-written summaries and titles for memory items (migration 21, yuki.memory.titles).

Offline: temp databases and scripted model answers, no network, no real memory.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from yuki.memory import titles
from yuki.memory.api import MemoryClient
from yuki.memory.store import MIGRATIONS, FactChange, Store, _split_sql


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """The bits of PortraitWorker the titles pass uses, with scripted model answers."""

    def __init__(self, store, answers=()):
        self.store, self.answers = store, list(answers)
        self._run_lock = threading.Lock()
        self.calls, self.logs = [], []

    def _call(self, **kw):
        self.calls.append(kw)
        kw["stats"].calls += 1
        kw["stats"].cost_usd += 0.001
        return self.answers.pop(0)

    def _log(self, type, **fields):
        self.logs.append((type, fields))


def _fact(store, kind, subject, text, aspect=None, summary=None):
    [(ok, fid)] = store.commit_portrait_changes(None, [FactChange(
        "ADD", None, kind, subject, text, 0.8, [], time.time(), aspect=aspect, summary=summary)])
    assert ok
    return fid


# -- storage ---------------------------------------------------------------------------------------------


def test_migration_21_is_additive_and_old_rows_wait_for_labels():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        conn = sqlite3.connect(str(db), isolation_level=None)
        for index in range(20):   # a database as it was at migration 20
            for statement in _split_sql(MIGRATIONS[index]):
                conn.execute(statement)
        conn.execute("PRAGMA user_version=20")
        conn.close()
        s = Store.open(db)
        try:
            assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS) == 21
            for table, col in (("portrait_facts", "summary_ciphertext"), ("conversation_facts", "title_ciphertext"),
                               ("todos", "title_ciphertext"), ("knowhow", "title_ciphertext"),
                               ("knowhow", "summary_ciphertext")):
                assert col in [r[1] for r in s._query(f"PRAGMA table_info({table})")]
            fid = _fact(s, "work", "Corvana", "The user is the founding AI engineer at Corvana.")
            tid = s.add_todo("renew the car insurance before the 30th")
            assert s.labels_missing() == {"fact": [fid], "commit": [], "todo": [tid], "knowhow": []}
        finally:
            s.close()


def test_labels_are_encrypted_and_never_clobber_a_writers_label(store):
    fid = _fact(store, "person", "Varun", "Varun reviews the user's demo videos at Corvana.",
                summary="Reviews your demo videos at work")
    raw = store._query("SELECT summary_ciphertext FROM portrait_facts WHERE id=?", (fid,))[0][0]
    assert raw and "demo" not in raw                         # stored encrypted like the text
    assert store.portrait_fact(fid).summary == "Reviews your demo videos at work"
    assert store.set_labels("fact", {fid: {"summary": "Something else entirely"}}) == 0
    assert store.set_labels("fact", {fid: {"summary": "Colleague who reviews your demos"}}, overwrite=True) == 1
    assert store.portrait_fact(fid).summary == "Colleague who reviews your demos"


# -- validation --------------------------------------------------------------------------------------------


def test_clean_keeps_whole_labels_within_bounds_and_never_cuts():
    assert titles.clean('  "Send Varun the demo video."  ', "title") == "Send Varun the demo video"
    assert titles.clean("Plays Valorant most nights with his friends", "summary") is not None      # 7 words
    assert titles.clean("one two three four five six seven eight nine", "summary") is None          # 9 words
    assert titles.clean("Send Varun the updated demo video tonight", "title") is None               # 7 words
    assert titles.clean("Waiting on Varun…", "title") is None                                       # cut off
    assert titles.clean("", "title") is None and titles.clean("Valorant", "summary") is None
    assert titles.fact_label("open_loop", {"summary": "Send Varun the demo video"}) == "Send Varun the demo video"
    assert titles.fact_label("open_loop", {"summary": "Owes Varun the new demo video by Friday night"}) is None
    assert titles.fact_label("work", {"summary": None}) is None


# -- writers ---------------------------------------------------------------------------------------------------


def test_the_portrait_operations_write_the_summary_with_the_fact(store):
    from yuki.memory.portrait import AUDIT_PORTRAIT_TOOL, UPDATE_PORTRAIT_TOOL, PortraitWorker

    op = UPDATE_PORTRAIT_TOOL["input_schema"]["properties"]["operations"]["items"]
    assert "summary" in op["required"] and "summary" in op["properties"]
    assert "summary" in AUDIT_PORTRAIT_TOOL["input_schema"]["properties"]["operations"]["items"]["required"]
    store.add_journal(time.time() - 60, None, "Slack", "The user works as the founding AI engineer at Corvana.", 6)
    journal = store.journal_between(None, None)
    worker = PortraitWorker(store, client=object())
    base = {"fact_id": None, "subject": "Corvana", "confidence": 0.9, "importance": 6, "journal_ids": [journal[0].id],
            "episode_ids": [], "based_on_facts": [], "reason": "stated", "loop": None, "aspect": None}
    changes, outcomes = worker._validate([
        dict(base, op="ADD", kind="work", text="The user is the founding AI engineer at Corvana.",
             summary="Founding AI engineer at Corvana"),
        dict(base, op="ADD", kind="interest", subject="Valorant", text="The user plays Valorant most nights.",
             summary="Plays Valorant almost every single night with his friends online"),   # too long: dropped
    ], store.portrait_facts(), journal, time.time())
    assert [c.summary for c in changes] == ["Founding AI engineer at Corvana", None]
    written = store.commit_portrait_changes(None, changes)
    assert store.portrait_fact(written[0][1]).summary == "Founding AI engineer at Corvana"
    assert store.labels_missing()["fact"] == [written[1][1]]      # the titles pass fills the other


def test_conversation_commitments_and_knowhow_carry_their_titles(store):
    from yuki.memory.conversations import RECORD_CONVERSATION_TOOL, RECORD_KNOWHOW_TOOL, ConversationWorker
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL

    props = RECORD_CONVERSATION_TOOL["input_schema"]["properties"]
    assert "title" in props["operations"]["items"]["required"]
    for schema in (props["knowhow"]["items"], RECORD_KNOWHOW_TOOL["input_schema"]["properties"]["knowhow"]["items"]):
        assert {"title", "summary"} <= set(schema["required"])
    assert "title" in RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["commitments"]["items"]["required"]

    class Turn:
        id, at, actions = 7, time.time(), ["focus_window arc"]
        user_text = "no, pass the link as a launch argument"

    items, rejected = ConversationWorker.knowhow_from([{
        "app": "arc.exe", "text": "Launch Arc with the URL as its argument.", "avoid": "", "exchanges": [1],
        "quote": "pass the link as a launch argument", "title": "Open a YouTube link in Arc",
        "summary": "Pass the link as a launch argument"}], [Turn()])
    assert not rejected and items[0]["title"] == "Open a YouTube link in Arc"
    kid = store.add_knowhow("arc", items[0]["text"], title=items[0]["title"], summary=items[0]["summary"])
    k = next(k for k in store.knowhow_current() if k.id == kid)
    assert (k.title, k.summary) == ("Open a YouTube link in Arc", "Pass the link as a launch argument")
    cid = store.add_conversation_fact("commitment", "Remind the user about the PR review on Friday.",
                                      origin="model", owner="yuki", title="Remind you about the PR review")
    assert store.conversation_fact(cid).title == "Remind you about the PR review"


def test_a_corrected_item_gets_its_label_with_its_new_wording(store):
    from yuki.memory.corrections import CorrectionApplier

    fid = store.add_conversation_fact("commitment", "The user will send Barun the deck.", origin="model",
                                      owner="user", title="Send Barun the deck")
    tid = store.add_todo("call Barun about the deck")
    cid = store.add_correction('About "Barun", the user said in their own words: "not barun, its varun"')

    class W(_Worker):
        embedder = None

        def fold_corrections(self, run_id, stats, now):
            return 0

        def _render(self, run_id, stats, now):
            return "page"

        def _seal(self, value):
            return ""

    worker = W(store, [
        {"fixes": "Barun is Varun", "aliases": [], "search_terms": ["Barun"], "elsewhere": True},
        {"edits": [
            {"id": f"C{fid}", "op": "rewrite", "wrong": "Barun", "text": "The user will send Varun the deck.",
             "subject": "", "label": "Send Varun the deck", "reason": "name"},
            {"id": f"T{tid}", "op": "rewrite", "wrong": "Barun", "text": "call Varun about the deck",
             "subject": "", "label": "", "reason": "name"},
        ]},
    ])
    result = CorrectionApplier(worker).apply(cid)
    assert result.ok
    [commit] = store.conversation_facts(("commitment",))
    assert commit.id != fid and commit.title == "Send Varun the deck"
    [todo] = store.todos(("open",))
    assert todo.id != tid and todo.title is None                  # left without one: the titles pass follows
    assert store.labels_missing()["todo"] == [todo.id]


# -- the batched pass (backfill) --------------------------------------------------------------------------------


def test_the_titles_pass_fills_every_surface_in_one_call(store):
    fid = _fact(store, "person", "Elina", "Elina, whom the user helped set up Claude Code for her space science roadmap.")
    lid = _fact(store, "open_loop", "Varun", "The user owes Varun the demo video (asked by Varun in Slack on 2026-09-23).")
    cid = store.add_conversation_fact("commitment", "Remind the user to call Mom at 6 pm.", origin="model", owner="yuki")
    tid = store.add_todo("renew the car insurance before the 30th")
    kid = store.add_knowhow("arc", "Launch Arc with the URL as its argument.", source_request="open this video in arc")
    worker = _Worker(store, [{"labels": [
        {"id": f"P{fid}", "label": "Helped her set up Claude Code"},
        {"id": f"P{lid}", "label": "Send Varun the demo video"},
        {"id": f"C{cid}", "label": "Remind you to call Mom"},
        {"id": f"T{tid}", "label": "Renew the car insurance"},
        {"id": f"K{kid}", "label": "Open a video in Arc"},
        {"id": f"W{kid}", "label": "Pass the link as a launch argument"},
        {"id": "P99999", "label": "not an item"},
    ]}])
    result = titles.Titler(worker).run(reason="test")
    assert result.ok and result.pending == 6 and result.calls == 1 and result.written == 6
    assert any("P99999" in r for r in result.rejected)
    assert f"P{lid} [portrait fact, an open loop (something owed)] HEADING 'Varun' NEEDS a title" in worker.calls[0]["user"]
    assert worker.calls[0]["tool"]["name"] == "write_labels"
    assert worker.calls[0]["forced"] is True and "haiku" in worker.calls[0]["model"]
    assert not any(store.labels_missing().values())
    assert store.portrait_fact(lid).summary == "Send Varun the demo video"   # an open loop's title
    assert [r["kind"] for r in store.portrait_runs(5)] == ["titles"]
    assert titles.Titler(worker).run(reason="again").pending == 0          # nothing left: no call


def test_a_label_outside_its_bounds_is_asked_for_again_then_left(store):
    tid = store.add_todo("renew the car insurance before the 30th")
    too_long = {"labels": [{"id": f"T{tid}", "label": "Renew the car insurance before the thirtieth"}]}
    worker = _Worker(store, [too_long] * (2 * titles.MAX_ATTEMPTS))
    titler = titles.Titler(worker)
    for _ in range(titles.MAX_ATTEMPTS):
        r = titler.run()
        assert r.written == 0 and r.retried == 1 and r.calls == 2      # asked once more in the same pass
    assert f"T{tid} (7 words)" in worker.calls[1]["user"] and "LAST TRY" in worker.calls[1]["user"]
    assert titler.run().pending == 0 and len(worker.calls) == 2 * titles.MAX_ATTEMPTS
    assert store.todo(tid).title is None


def test_a_label_over_its_limit_is_fixed_by_the_retry(store):
    fid = _fact(store, "interest", "Valorant", "The user plays Valorant most nights, often on Discord calls.")
    worker = _Worker(store, [{"labels": [{"id": f"P{fid}", "label": "Plays Valorant most nights, often while on "
                                                                     "Discord voice calls"}]},
                             {"labels": [{"id": f"P{fid}", "label": "Plays Valorant most nights"}]}])
    r = titles.Titler(worker).run()
    assert r.ok and r.written == 1 and r.retried == 1
    assert store.portrait_fact(fid).summary == "Plays Valorant most nights"


def test_the_scheduler_runs_the_pass_only_when_the_user_is_idle(store, monkeypatch):
    import contextlib

    from yuki.memory.portrait import PortraitScheduler

    store.add_todo("renew the car insurance before the 30th")
    idle = {"s": 5.0}

    class W(_Worker):
        def one_render(self):
            return contextlib.nullcontext()

        def loop_check_pending(self):
            return False

    worker = W(store, [{"labels": [{"id": f"T{store.todos()[0].id}", "label": "Renew the car insurance"}]}])
    sched = PortraitScheduler(store, worker, db_path=store.path, idle_fn=lambda: idle["s"])
    monkeypatch.setattr(sched, "due", lambda now: (None, "nothing due"))
    assert sched.tick() is None and worker.calls == []            # the user is at the PC
    idle["s"] = titles.IDLE_S + 1
    assert sched.tick() == "titles" and len(worker.calls) == 1
    assert store.todos()[0].title == "Renew the car insurance"
    assert sched.tick() is None                                   # not again before EVERY_S


# -- what the window reads ----------------------------------------------------------------------------------------


def test_the_api_rows_carry_summary_and_title(store):
    fid = _fact(store, "work", "Corvana", "The user is the founding AI engineer at Corvana.",
                summary="Founding AI engineer at Corvana")
    lid = _fact(store, "open_loop", "Varun", "The user owes Varun the demo video (asked by Varun in Slack).",
                summary="Send Varun the demo video")
    cid = store.add_conversation_fact("commitment", "Remind the user to call Mom.", origin="model", owner="yuki",
                                      title="Remind you to call Mom")
    tid = store.add_todo("renew the car insurance", title="Renew the car insurance")
    store.add_knowhow("arc", "Launch Arc with the URL.", title="Open a video in Arc", summary="Pass the link at launch")
    m = MemoryClient(store)
    facts = {f["id"]: f for f in m.portrait_facts_with_evidence()}
    assert facts[fid]["summary"] == "Founding AI engineer at Corvana"
    assert facts[lid]["summary"] == "Send Varun the demo video"
    rows = {t["id"]: t for t in m.todos()}
    loop = next(t for t in rows.values() if t["source"] == "loop")
    assert loop["title"] == "Send Varun the demo video"
    assert rows[f"commit:{cid}"]["title"] == "Remind you to call Mom"
    assert rows[f"todo:{tid}"]["title"] == "Renew the car insurance"
    [k] = store.knowhow_current(None, any_app=True)
    assert (k.title, k.summary) == ("Open a video in Arc", "Pass the link at launch")
    # an item not labelled yet reads as None (the window falls back to its text)
    store.add_todo("book the dentist")
    assert [t["title"] for t in m.todos() if t["text"] == "book the dentist"] == [None]
