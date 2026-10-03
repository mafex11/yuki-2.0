"""The user's feedback round 3, memory side (builder appMem2; docs/research/revamp-2026-10-02/56-build-appMem2.md):
W1 threads you can close, cards with the next step, self-closing stale threads, the Continue context; W2 session
titles and summaries; W3 corrections from the user's own words; W5 English everywhere; W6 "maybe misheard" with
Yuki's guess.

Offline: temp databases, invented people and scripted model answers; no network, no real memory.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import FactChange, ModelCall, NewEpisode, NewFact, Store, WorkOp

NOW = time.time()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """The bits of PortraitWorker the passes use, with scripted model answers."""

    def __init__(self, store, answers=()):
        self.store, self.answers = store, list(answers)
        self.embedder = None
        self.model, self.effort = "sonnet-test", "low"
        self._run_lock = threading.Lock()
        self.calls, self.logs = [], []

    def _call(self, **kw):
        self.calls.append(kw)
        kw["stats"].calls += 1
        kw["stats"].cost_usd += 0.001
        return self.answers.pop(0)

    def _log(self, type, **fields):
        self.logs.append((type, fields))

    def _seal(self, value):
        return ""

    def fold_corrections(self, run_id, stats, now):
        return 0

    def _render(self, run_id, stats, now):
        return "page"


def _thread(store, title, state, *, at=None, capture=None, open_items=()):
    [op] = store.apply_work([WorkOp("starts", None, title=title, state=state, at=at or NOW, seen_at=at or NOW,
                                    capture_thread_id=capture, open_items=list(open_items),
                                    vector=np.ones(4, dtype=np.float32), embed_model="stub")])
    return op.result_id


def _journal(store, text, *, at, nature="fact", source=None, app="Discord", work=None):
    call = ModelCall(at=at, thread_id=None, model="test", cost_usd=0.0)
    store.commit_batch(call, [], [NewFact(at=at, thread_id=None, app=app, host=None, fact=text, importance=5,
                                          nature=nature, source=source)])
    jid = max(j.id for j in store.journal_between(None, None))
    if work is not None:
        store._write(lambda c: c.execute("UPDATE journal SET work_thread_id=? WHERE id=?", (work, jid)))
    return jid


def _episode(store, start, end, text, *, title=None, summary=None):
    [eid] = store.commit_episodes(None, start, [NewEpisode(start, end, start, end, True, text, {}, title=title,
                                                           summary=summary)])
    return eid


# ---------------------------------------------------------------------------
# W1 threads
# ---------------------------------------------------------------------------


def test_w1a_the_user_closes_a_thread_and_reopens_it_exactly(store):
    client = MemoryClient(store)
    tid = _thread(store, "Seed-swap app", "The user wired the form to the new server.")
    out = client.close_thread(f"thread:{tid}", "done")
    assert out == {"ok": True, "ref": f"thread:{tid}", "status": "done", "how": "done"}
    t = store.work_thread(tid)
    assert t.status == "done" and t.closed_by == "user" and t.closed_at
    from yuki.memory.workthreads import thread_line

    assert "closed by the user" in thread_line(t)                    # the journal reads it as finished
    back = client.close_thread(f"W{tid}", "reopen")
    assert back["ok"] and back["status"] == "active"
    t = store.work_thread(tid)
    assert t.status == "active" and t.closed_by is None
    assert client.close_thread(f"W{tid}", "reopen")["ok"] is False        # open already: nothing to undo
    assert client.close_thread("nonsense", "done")["ok"] is False
    assert client.close_thread(f"W{tid}", "finish")["ok"] is False        # only done, dismiss, reopen
    assert {d["action"] for d in store.changelog(pass_="threads")} == {"user_done", "user_reopen"}


def test_w1a_a_dismissed_thread_comes_back_with_its_facts_and_vector(store):
    client = MemoryClient(store)
    tid = _thread(store, "Watching streams", "The user watched a stream.")
    jid = _journal(store, "The user watched a stream on Twitch.", at=NOW - 60, work=tid)
    assert client.close_thread(f"thread:{tid}", "dismiss")["status"] == "dismissed"
    assert store.work_thread(tid).status == "dismissed"
    assert store.journal_by_ids([jid])[0].work_thread_id is None          # no longer a thread's fact
    assert tid not in store.work_thread_vectors()
    assert client.close_thread(f"thread:{tid}", "reopen")["status"] == "active"
    assert store.journal_by_ids([jid])[0].work_thread_id == tid           # exact: facts and vector back
    assert tid in store.work_thread_vectors()


def test_w1a_new_work_on_a_user_closed_thread_reopens_it_and_clears_the_close(store):
    tid = _thread(store, "Garden planner", "The user drew the beds.", at=NOW - 3600)
    MemoryClient(store).close_thread(f"thread:{tid}", "done")
    store.apply_work([WorkOp("continues", tid, state="The user added the watering schedule.", at=NOW, seen_at=NOW)])
    t = store.work_thread(tid)
    assert t.status == "active" and t.closed_by is None and t.shown is None and t.next is None


def _consolidate_answer(close_id, card_id, *, dismiss_id=None):
    decisions = [{"action": "close", "thread_ids": [close_id], "keep_id": None, "title": "", "state": "",
                  "shown": "", "next": "", "open_items": [], "status": "done", "reason": f"W{card_id} carries it on"}]
    if dismiss_id is not None:
        decisions.append({"action": "dismiss", "thread_ids": [dismiss_id], "keep_id": None, "title": "", "state": "",
                          "shown": "", "next": "", "open_items": [], "status": "", "reason": "watching videos"})
    return {"decisions": decisions, "questions": [], "open_threads": [
        {"thread_id": card_id, "shown": "You were wiring the seed-swap form to the new server; saving works, but "
                                        "photo uploads still fail.",
         "next": "Fix the photo upload, then send Priya the test link."},
        {"thread_id": close_id, "shown": "Closed one gets no card.", "next": ""},     # rejected: it is closed
    ]}


def test_w1bc_the_nightly_pass_closes_superseded_threads_writes_cards_and_is_undone(store):
    from yuki.memory.workthreads import Consolidator, decision_counts, load_plan, save_plan

    old = _thread(store, "Seed-swap resume", "The user resumed the seed-swap session.", at=NOW - 2 * 86400)
    new = _thread(store, "Seed-swap app", "The user made saving work; uploads fail.", at=NOW - 600,
                  open_items=["fix the photo upload"])
    vid = _thread(store, "Cat videos", "The user watched cat videos.", at=NOW - 900)
    jid = _journal(store, "The user watched cat videos on YouTube.", at=NOW - 900, work=vid)
    _episode(store, NOW - 3600, NOW - 1800, "x", title="Seed-swap uploads")
    dry = Consolidator(_Worker(store, [_consolidate_answer(old, new, dismiss_id=vid)])).run(now=NOW, dry_run=True)
    assert dry.ok and dry.run_id is None
    assert decision_counts(dry.decisions) == {"close_done": 1, "dismiss": 1, "present": 1}
    assert any("card W" in r for r in dry.rejected)                      # no card for a thread it closes
    tidy = Consolidator(_Worker(store))
    active = store.work_threads(("active",))
    message = tidy.message(active, *tidy.signals(active), NOW)
    assert "RECENT ACTIVITY" in message and "Seed-swap uploads" in message   # what the user moved on to
    assert store.work_thread(old).status == "active"                      # the dry run wrote nothing
    path = save_plan(store, dry.decisions, Path(store.path).parent / "plan.json")
    assert "seed-swap" not in path.read_text(encoding="utf-8").lower()    # encrypted: no memory words in the file
    worker = _Worker(store)
    res = Consolidator(worker).run(now=NOW, plan=load_plan(store, path))
    assert res.ok and res.run_id and worker.calls == [] and res.cards == 1  # the plan, no new judgement
    assert store.work_thread(old).status == "done" and store.work_thread(old).closed_by == "model"
    assert store.work_thread(vid).status == "dismissed" and store.journal_by_ids([jid])[0].work_thread_id is None
    card = store.work_thread(new)
    assert card.shown.startswith("You were wiring") and card.next.startswith("Fix the photo upload")
    undo = Consolidator(_Worker(store)).undo(res.run_id)
    assert set(undo["threads"]) == {old, new, vid} and undo["diverged"] == []
    assert store.work_thread(old).status == "active" and store.work_thread(old).closed_by is None
    assert store.work_thread(vid).status == "active" and store.journal_by_ids([jid])[0].work_thread_id == vid
    assert store.work_thread(new).shown is None and store.work_thread(new).next is None


def test_w1b_undo_leaves_a_thread_the_user_worked_on_since(store):
    from yuki.memory.workthreads import Consolidator

    old = _thread(store, "Old resume", "The user resumed it.", at=NOW - 3 * 86400)
    new = _thread(store, "New thing", "The user started it.", at=NOW - 60)
    res = Consolidator(_Worker(store, [_consolidate_answer(old, new)])).run(now=NOW - 30)
    assert res.ok and store.work_thread(old).status == "done"
    store.apply_work([WorkOp("continues", old, state="The user came back to it.", at=NOW, seen_at=NOW)])
    undo = Consolidator(_Worker(store)).undo(res.run_id)
    assert undo["diverged"] == [old] and store.work_thread(old).state == "The user came back to it."


def test_review_f1_undo_of_an_earlier_run_never_undoes_the_users_later_close(store):
    tid = _thread(store, "Seed-swap app", "The user made saving work.", at=NOW - 86400)
    [oc] = store.change_work_threads([{"action": "present", "id": tid, "shown": "You made saving work.",
                                       "next": "Fix uploads."}], now=NOW - 600, run_id=901)
    assert oc["ok"]
    MemoryClient(store).close_thread(f"thread:{tid}", "done")
    undo = store.undo_work_threads(901, now=NOW)
    assert undo["threads"] == [tid]                         # the run's card is taken back (it still stood) ...
    t = store.work_thread(tid)
    assert t.status == "done" and t.closed_by == "user" and t.shown is None   # ... the user's close never
    # a full image (a model close) taken back after the user's later rename and reopen is left as it is
    tid2 = _thread(store, "Old resume", "The user resumed it.", at=NOW - 86400)
    store.change_work_threads([{"action": "close", "id": tid2, "status": "done", "reason": "moved on"}],
                              now=NOW - 500, run_id=906)
    MemoryClient(store).close_thread(f"thread:{tid2}", "done")                 # the user closes it themselves
    assert store.undo_work_threads(906, now=NOW)["diverged"] == [tid2]
    assert store.work_thread(tid2).closed_by == "user"
    # and a run whose change still stands is undone
    other = _thread(store, "Garden planner", "The user drew the beds.", at=NOW - 86400)
    store.change_work_threads([{"action": "close", "id": other, "status": "paused", "reason": "quiet"}],
                              now=NOW - 300, run_id=902)
    assert store.undo_work_threads(902, now=NOW)["threads"] == [other]
    assert store.work_thread(other).status == "active"


def test_review_f1_reopen_keeps_a_later_rename_and_its_undo_never_closes_again(store):
    client = MemoryClient(store)
    tid = _thread(store, "Maths prep", "The user worked through chapter 1.", at=NOW - 86400)
    client.close_thread(f"thread:{tid}", "done")
    store.change_work_threads([{"action": "rename", "id": tid, "title": "Maths prep chapter 1 done",
                                "reason": "clearer"}], now=NOW - 100, run_id=903)
    assert client.close_thread(f"thread:{tid}", "reopen")["status"] == "active"
    t = store.work_thread(tid)
    assert t.title == "Maths prep chapter 1 done" and t.status == "active" and t.closed_by is None
    undo = store.undo_work_threads(903, now=NOW)
    assert undo["diverged"] == [tid]
    t = store.work_thread(tid)
    assert t.status == "active" and t.title == "Maths prep chapter 1 done"


def test_review_f2_an_undo_is_never_a_touch(store):
    from yuki.memory.workthreads import Consolidator

    ids = [_thread(store, f"Old thing {i}", "The user did it.", at=NOW - 10 * 86400) for i in range(3)]
    store.change_work_threads([{"action": "present", "id": i, "shown": f"You did thing {i}.", "next": ""}
                               for i in ids], now=NOW - 60, run_id=904)
    store.change_work_threads([{"action": "close", "id": ids[0], "status": "done", "reason": "finished"}],
                              now=NOW - 50, run_id=904)
    before = len(store.latest_work_events(since=NOW - 3600))
    out = Consolidator(_Worker(store)).undo(904, now=NOW)
    assert sorted(out["threads"]) == sorted(ids) and out["diverged"] == []
    assert len(store.latest_work_events(since=NOW - 3600)) == before            # nothing looks just touched
    assert store.work_thread(ids[0]).status == "active" and store.work_thread(ids[1]).shown is None


def test_review_f4_a_card_is_written_from_the_newest_facts(store):
    from yuki.memory.workthreads import NEWEST_FACTS, Consolidator

    tid = _thread(store, "Seed-swap app", "The user built it.", at=NOW - 86400)
    for i in range(8):
        _journal(store, f"Fact number {i} about the seed-swap app.", at=NOW - 8000 + i * 600, work=tid)
    got = Consolidator(_Worker(store))._newest_facts(store.work_thread(tid))
    assert [j.fact for j in got] == [f"Fact number {i} about the seed-swap app." for i in range(8 - NEWEST_FACTS, 8)]


def test_review_f5_a_card_keeps_a_small_image_and_old_images_are_pruned(store):
    tid = _thread(store, "Seed-swap app", "The user built it.", at=NOW - 86400)
    for i in range(5):
        _journal(store, f"Fact {i}.", at=NOW - 5000 + i, work=tid)
    store.change_work_threads([{"action": "present", "id": tid, "shown": "You built it.", "next": "Ship it."}],
                              now=NOW - 40 * 86400, run_id=905)
    rows = store._query("SELECT surface, before_ciphertext FROM undo_images")
    assert [r["surface"] for r in rows] == ["thread_card"]
    assert "journal" not in store.cipher.decrypt(rows[0]["before_ciphertext"])   # only the two card columns
    MemoryClient(store).close_thread(f"thread:{tid}", "done")                    # the user's own: kept
    store._write(lambda c: c.execute("UPDATE undo_images SET at=? WHERE run_id IS NULL", (NOW - 60 * 86400,)))
    assert store.prune_undo_images(older_than_days=30, now=NOW) == 1
    assert [r["surface"] for r in store._query("SELECT surface FROM undo_images")] == ["thread"]


def test_review_f6_a_saved_plan_writes_no_auto_pause_it_did_not_show(store):
    from yuki.memory.workthreads import Consolidator

    quiet = _thread(store, "Quiet thing", "The user set it up.", at=NOW - 9 * 86400)
    card = _thread(store, "Seed-swap app", "The user built it.", at=NOW - 600)
    plan = [{"action": "present", "id": card, "shown": "You built the seed-swap app.", "next": "", "reason": "card"}]
    res = Consolidator(_Worker(store)).run(now=NOW, plan=plan)
    assert res.ok and res.cards == 1 and store.work_thread(quiet).status == "active"
    # the nightly pass (no plan) still pauses it
    answer = {"decisions": [], "questions": [], "open_threads": []}
    Consolidator(_Worker(store, [answer])).run(now=NOW + 60)
    assert store.work_thread(quiet).status == "paused"


def test_review_f7_the_episode_call_sees_the_days_earlier_titles(store):
    from yuki.memory.episodes import EpisodeWorker, Window, build_user_message, window_facts

    day = time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(NOW - 86400)) + " 09:00",
                                    "%Y-%m-%d %H:%M"))
    _episode(store, day, day + 3 * 3600, "x", title="Yuki voice fixes")
    _episode(store, day + 3 * 3600, day + 6 * 3600, "y", title="Yuki wake word tests")
    w = EpisodeWorker(store, client=object(), embedder=object(), log_dir=Path(store.path).parent)
    win = Window(start=day + 6 * 3600, end=day + 9 * 3600, final=True, trigger="test")
    titles = w._used_titles(win)
    assert titles == ["Yuki voice fixes", "Yuki wake word tests"]
    msg = build_user_message(win, window_facts(store, win.start, win.end), [], used_titles=titles)
    assert "TITLES ALREADY USED TODAY" in msg and "- Yuki wake word tests" in msg


def test_w1c_the_titles_pass_writes_a_thread_card_and_its_next_step(store):
    from yuki.memory.titles import Titler

    tid = _thread(store, "Seed-swap app", "The user made saving work; uploads fail.", open_items=["fix uploads"])
    worker = _Worker(store, [{"items": [{"id": f"W{tid}", "shown": "You made saving work; photo uploads still fail.",
                                         "next": "Fix the photo upload."}]}])
    r = Titler(worker).run(what="shown", only_shown={"thread": [tid]}, now=NOW)
    assert r.ok and r.shown_written == 1
    t = store.work_thread(tid)
    assert t.shown == "You made saving work; photo uploads still fail." and t.next == "Fix the photo upload."
    assert "OPEN: fix uploads" in worker.calls[0]["user"]                 # the card is written from more than the state


def test_w1d_continue_gives_the_next_step_and_the_places_to_bring_back(store):
    client = MemoryClient(store)
    cap = store.upsert_thread("Chrome", "Seed swap - staging", "https://seeds.example/staging", process="chrome.exe")
    tid = _thread(store, "Seed-swap app", "The user made saving work.", capture=cap, open_items=["fix uploads"])
    store.change_work_threads([{"action": "present", "id": tid, "shown": "You made saving work; uploads fail.",
                                "next": "Fix the photo upload."}], origin="test")
    store.upsert_links([{"url": "https://docs.example/uploads", "how": "viewed", "at": NOW - 100, "until": NOW - 50,
                         "title": "Uploads guide", "host": "docs.example", "app": "Chrome", "work_thread_id": tid}])
    ctx = client.thread_continue(f"thread:{tid}")
    assert ctx["shown"] == "You made saving work; uploads fail." and ctx["next"] == "Fix the photo upload."
    urls = [p["url"] for p in ctx["places"]]
    assert "https://seeds.example/staging" in urls and "https://docs.example/uploads" in urls
    assert ctx["ask"] == ("Help me continue Seed-swap app. Where it stands: You made saving work; uploads fail. "
                          "Next: Fix the photo upload.")
    assert "pick up where you left off" not in ctx["ask"]
    assert client.thread_continue("thread:999") is None
    row = client.threads(statuses=("active",))[0]
    assert row["next"] == "Fix the photo upload." and row["closed_by"] is None


# ---------------------------------------------------------------------------
# W2 sessions
# ---------------------------------------------------------------------------


def test_w2_the_episode_call_writes_a_title_and_summary_and_bad_ones_are_dropped(store, monkeypatch):
    from yuki.memory import episodes
    from yuki.memory.episodes import RECORD_EPISODES_TOOL, EpisodeWorker, Window

    monkeypatch.setattr(episodes, "compact_aggregates", lambda facts: {})   # the window's numbers are not the point

    assert {"title", "summary"} <= set(RECORD_EPISODES_TOOL["input_schema"]["properties"]["episodes"]["items"]
                                       ["required"])
    w = EpisodeWorker(store, client=object(), embedder=object(), log_dir=Path(store.path).parent)
    win = Window(NOW - 3600, NOW, True, "break")
    day = __import__("datetime").datetime.fromtimestamp

    def item(title, summary):
        return {"start": f"{day(NOW - 3600):%Y-%m-%d %H:%M}", "end": f"{day(NOW - 1800):%Y-%m-%d %H:%M}",
                "text": "02:10-02:40: Claude Code worked on the wake word while the user directed it (20 min active).",
                "title": title, "summary": summary}

    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", name="record_episodes", input={"episodes": [item("Wake word timing fix",
                                                                          "Claude Code fixed the wake timing while you "
                                                                          "directed it.")]})])
    [ep] = w._parse(response, win, {})
    assert ep.title == "Wake word timing fix" and ep.summary.startswith("Claude Code fixed")
    long = "Continued development split between the app and the terminal in the final stretch at night"
    response.content[0].input = {"episodes": [item(long, "x" * 400)]}
    [ep] = w._parse(response, win, {})
    assert ep.title is None and ep.summary is None                        # a cut summary as heading never lands


def test_w2_the_sessions_backfill_dry_run_apply_and_undo(store):
    from yuki.memory.passplan import load_plan, save_plan
    from yuki.memory.sessions import SESSIONS_PASS, SessionLabeler

    a = _episode(store, NOW - 7200, NOW - 5400, "10:00-10:30: the user fixed the wake word with Claude Code.")
    b = _episode(store, NOW - 5000, NOW - 4000, "11:00-11:20: the user played Valorant with friends.")
    answer = {"sessions": [{"id": f"E{a}", "title": "Wake word fix", "summary": "You fixed the wake word with Claude "
                                                                               "Code."},
                           {"id": f"E{b}", "title": "wake word fix", "summary": "You played Valorant."}]}
    dry = SessionLabeler(_Worker(store, [answer])).run(dry_run=True)
    assert dry.ok and len(dry.decisions) == 1 and any("same title" in r for r in dry.rejected)
    path = save_plan(store, SESSIONS_PASS, dry.decisions, Path(store.path).parent / "s.json")
    res = SessionLabeler(_Worker(store)).run(plan=load_plan(store, SESSIONS_PASS, path))
    assert res.ok and res.written == 1
    rows = {e["id"]: e for e in MemoryClient(store).episodes(NOW - 86400, NOW)}
    assert rows[a]["title"] == "Wake word fix" and rows[a]["ref"] == f"episode:{a}" and rows[b]["title"] is None
    assert SessionLabeler(_Worker(store)).undo(res.run_id) == {"cleared": 1, "diverged": 0}
    assert store.episodes_by_ids([a])[0].title is None


# ---------------------------------------------------------------------------
# W3 corrections from the user's own words
# ---------------------------------------------------------------------------


def test_w3_correct_item_queues_a_targeted_correction_at_once(store):
    client = MemoryClient(store)
    fid = store.commit_portrait_changes(None, [FactChange("ADD", None, "person", "Kiran", "Kiran is the user's "
                                                          "classmate.", 0.7, [], NOW)])[0][1]
    t0 = time.perf_counter()
    out = client.correct_item(f"fact:{fid}", "he's my cousin, not a classmate")
    assert time.perf_counter() - t0 < 1.0
    assert out["ok"] and out["state"] == "updating" and out["ref"] == f"fact:{fid}"
    cid = out["correction_id"]
    assert store.correction_target(cid) == f"P{fid}" and store.corrections_to_apply() == [cid]
    assert "he's my cousin, not a classmate" in store.portrait_fact(cid).text
    assert client.correct_item(f"fact:{fid}", "  ")["ok"] is False
    assert client.correct_item("call:3", "wrong")["ok"] is False
    assert client.cancel_correction(cid)["ok"] and store.corrections_to_apply() == []


def test_w3_a_thread_corrected_in_the_users_words_is_rewritten_by_the_pass(store):
    from yuki.memory.corrections import CorrectionApplier

    tid = _thread(store, "Trip planning", "The user booked the Goa trip for next week.")
    store.change_work_threads([{"action": "present", "id": tid, "shown": "You booked the Goa trip.",
                                "next": "Pack."}], origin="test")
    out = MemoryClient(store).correct_item(f"thread:{tid}", "that was last week, it's done")
    worker = _Worker(store, [
        {"fixes": "the trip was last week", "aliases": [], "search_terms": [], "elsewhere": False},
        {"edits": [{"id": f"W{tid}", "op": "rewrite", "wrong": "for next week",
                    "text": "The user booked the Goa trip for last week.", "subject": "", "label": "", "shown": "",
                    "reason": "the week"}]}])
    r = CorrectionApplier(worker).apply(out["correction_id"], now=NOW)
    assert r.ok and [c["surface"] for c in r.changes if c["ok"]] == ["thread"]
    assert "TARGET: W" in worker.calls[1]["user"]                        # judged even though "elsewhere" was false
    t = store.work_thread(tid)
    assert t.state == "The user booked the Goa trip for last week." and t.shown is None and t.next is None
    assert MemoryClient(store).correction_status(out["correction_id"])["state"] == "done"


def test_w3_a_session_and_a_misheard_entry_corrected_by_the_pass(store):
    from yuki.memory.corrections import CorrectionApplier

    eid = _episode(store, NOW - 3600, NOW - 1800, "10:00-10:30: the user watched a lecture on YouTube.",
                   title="Lecture on YouTube", summary="You watched a lecture.")
    out = MemoryClient(store).correct_item(f"episode:{eid}", "it was a Valorant VOD, not a lecture")
    worker = _Worker(store, [
        {"fixes": "a Valorant VOD", "aliases": [], "search_terms": [], "elsewhere": False},
        {"edits": [{"id": f"E{eid}", "op": "rewrite", "wrong": "a lecture",
                    "text": "10:00-10:30: the user watched a Valorant VOD on YouTube.", "subject": "",
                    "label": "Valorant VOD review", "shown": "You watched a Valorant VOD.", "reason": "what it was"}]}])
    r = CorrectionApplier(worker).apply(out["correction_id"], now=NOW)
    new_id = next(c["new_id"] for c in r.changes if c["ok"])
    new = store.episodes_by_ids([new_id])[0]
    assert new.title == "Valorant VOD review" and new.summary == "You watched a Valorant VOD."
    jid = _journal(store, "The user asked to message 'sea ra' on Discord.", at=NOW - 60, nature="uncertain",
                   source={"kind": "call", "heard": "sea ra", "meant": "Ciara", "meant_name": True})
    out = MemoryClient(store).correct_item(f"journal:{jid}", "it was Ciara")
    worker = _Worker(store, [
        {"fixes": "sea ra is Ciara", "aliases": [{"heard": "sea ra", "name": "Ciara", "why": "misheard"}],
         "search_terms": ["sea ra"], "elsewhere": True},
        {"edits": [{"id": f"J{jid}", "op": "rewrite", "wrong": "'sea ra'",
                    "text": "The user asked to message Ciara on Discord.", "subject": "", "label": "", "shown": "",
                    "reason": "name"}]}])
    r = CorrectionApplier(worker).apply(out["correction_id"], now=NOW)
    assert r.ok and [a["name"] for a in store.aliases()] == ["Ciara"]
    [entry] = store.journal_between(None, None)[-1:]
    assert entry.fact == "The user asked to message Ciara on Discord." and entry.nature == "fact"
    assert MemoryClient(store).misheard(now=NOW)["items"] == []            # settled: off the list


# ---------------------------------------------------------------------------
# W5 English
# ---------------------------------------------------------------------------


def test_w5_every_shown_writer_carries_the_english_rule():
    from yuki.memory import titles
    from yuki.memory.corrections import JUDGE_TOOL
    from yuki.memory.journal import system_prompt
    from yuki.memory.workthreads import CONSOLIDATE_SYSTEM_PROMPT

    assert titles.ENGLISH_RULE in titles.SHOWN_RULES and titles.ENGLISH_RULE in titles.GIST_SHOWN_FIELD
    assert "the user's own quoted words stay as they said them" not in titles.SHOWN_RULES
    assert titles.ENGLISH_RULE in JUDGE_TOOL["input_schema"]["properties"]["edits"]["items"]["properties"]["shown"][
        "description"]
    assert "Both in English" in CONSOLIDATE_SYSTEM_PROMPT
    assert "IN ENGLISH" in system_prompt(["Sam"], call_side=True) and "IN ENGLISH" not in system_prompt(["Sam"])


def test_w5_the_english_pass_rewrites_only_what_holds_another_language_and_undoes(store):
    from yuki.memory.english import EnglishPass

    fid = store.commit_portrait_changes(None, [FactChange("ADD", None, "personality", "talk", "The user says 'bas "
                                                          "thoda aur'.", 0.7, [], NOW, aspect="talk",
                                                          shown="You say 'bas thoda aur' when a build drags.")])[0][1]
    ok_fid = store.commit_portrait_changes(None, [FactChange("ADD", None, "interest", "Chess", "The user plays chess.",
                                                             0.7, [], NOW, shown="You play chess.")])[0][1]
    tid = _thread(store, "Build", "The user is building it.")
    store.change_work_threads([{"action": "present", "id": tid, "shown": "You are building it.",
                                "next": "Kal tak finish karo."}], origin="test")
    answer = {"lines": [{"id": f"P{fid}", "english": "You say 'just a bit more' when a build drags."},
                        {"id": f"N{tid}", "english": "Finish it by tomorrow."},
                        {"id": f"P{ok_fid}", "english": "You play chess."}]}           # no change: rejected
    worker = _Worker(store, [answer])
    r = EnglishPass(worker).run(now=NOW)
    assert r.ok and r.written == 2 and r.by_kind == {"P": 1, "N": 1}
    assert "You say 'bas thoda aur'" in worker.calls[0]["user"]
    assert store.portrait_fact(fid).shown == "You say 'just a bit more' when a build drags."
    assert store.work_thread(tid).next == "Finish it by tomorrow."
    store.change_work_threads([{"action": "present", "id": tid, "shown": "You are building it.",
                                "next": "Ship it."}], origin="test")                  # changed since
    undo = EnglishPass(_Worker(store)).undo(r.run_id)
    assert undo == {"restored": 1, "diverged": 1}
    assert store.portrait_fact(fid).shown == "You say 'bas thoda aur' when a build drags."
    assert store.work_thread(tid).next == "Ship it."                                  # a later change is never undone


def test_w5_the_talker_speaks_english_only():
    from yuki.voice.mouth import MOUTH_PROMPT

    assert "Speak English only" in MOUTH_PROMPT and "Kuch nahi, bolo" not in MOUTH_PROMPT
    assert "feminine forms" not in MOUTH_PROMPT


# ---------------------------------------------------------------------------
# W6 maybe misheard
# ---------------------------------------------------------------------------


def test_w6_the_call_side_tool_asks_for_heard_and_the_guess_and_keeps_them(store):
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, JournalWorker, Source
    from yuki.memory.misheard import source_fields
    from yuki.memory.store import ThreadInfo

    req = RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["facts"]["items"]["required"]
    assert {"heard", "meant", "meant_why", "meant_is_name"} <= set(req)
    assert source_fields({"heard": "sea ra", "meant": "Ciara"}, "fact") == {}          # only uncertain facts
    assert source_fields({"heard": "x", "meant": "X"}, "uncertain") == {}                   # the same words
    w = JournalWorker(store, client=object(), embedder=object(), log_dir=Path(store.path).parent)
    thread = ThreadInfo(1, "Discord", None, "voice", None, NOW, NOW, scope="call:7")
    src = Source(at=NOW, capture=SimpleNamespace(by_yuki=False, trigger=None, at=NOW),
                 utterance={"text": "message sea ra on discord"}, call_app="Discord")
    facts = [{"text": "The user asked to message 'sea ra' on Discord.", "nature": "uncertain", "importance": 4,
              "source": 1, "heard": "sea ra", "meant": "Ciara", "meant_why": "Ciara is on this call",
              "meant_is_name": True},
             {"text": "The user joked about a rich spouse.", "nature": "humor", "importance": 3, "source": 1,
              "heard": "", "meant": "", "meant_why": "", "meant_is_name": False}]
    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", name="record_call_side", input={"facts": facts})])
    got = w._parse_facts(response, thread, [src], tool="record_call_side")
    assert got[0].source["heard"] == "sea ra" and got[0].source["meant"] == "Ciara"
    assert got[0].source["meant_name"] is True and "heard" not in got[1].source


def test_w6_only_real_mishearings_are_listed_and_a_right_guess_queues_the_correction(store):
    client = MemoryClient(store)
    jid = _journal(store, "The user asked to message 'sea ra' on Discord.", at=NOW - 60, nature="uncertain",
                   source={"kind": "call", "quote": "message sea ra on discord", "heard": "sea ra",
                           "meant": "Ciara", "meant_why": "Ciara is on this call", "meant_name": True})
    _journal(store, "Said something unclear.", at=NOW - 30, nature="uncertain", source={"kind": "call"})
    m = client.misheard(now=NOW)
    assert m["total"] == 1
    [item] = m["items"]
    assert (item["heard"], item["guess"], item["why"], item["guess_is_name"]) == (
        "sea ra", "Ciara", "Ciara is on this call", True)
    out = client.resolve_heard(f"journal:{jid}", "guess_right")
    assert out["ok"] and out["correction_id"] and client.misheard(now=NOW)["items"] == []
    # (review F8) answered but still uncertain until the pass rewrites it: never a fact with the misheard words
    assert out["nature"] == "uncertain" and store.journal_by_ids([jid])[0].nature == "uncertain"
    assert store.correction_target(out["correction_id"]) == f"J{jid}"
    assert "It was 'Ciara', not 'sea ra'" in store.portrait_fact(out["correction_id"]).text
    undo = client.resolve_heard(f"journal:{jid}", "undo", correction_id=out["correction_id"])
    assert undo["ok"] and undo["cancelled"] and client.misheard(now=NOW)["total"] == 1
    # (review F3) Right keeps its meaning for the window as it is: taken as heard, no correction queued
    plain = client.resolve_heard(f"journal:{jid}", "right")
    assert plain["ok"] and plain["nature"] == "fact" and plain["correction_id"] is None


def test_w6_the_judge_can_point_at_the_heard_words_of_a_paraphrased_entry(store):
    """Review F8: memory's sentence paraphrased the words, so the judge points at the heard words (the subject)."""
    from yuki.memory.corrections import CorrectionApplier, target_candidate

    jid = _journal(store, "The user asked to message someone on Discord about the trip.", at=NOW - 60,
                   nature="uncertain", source={"kind": "call", "heard": "sea ra", "meant": "Ciara", "meant_name": True})
    cand = target_candidate(store, f"J{jid}")
    assert cand.subject == 'heard "sea ra"'
    edits, rejected = CorrectionApplier.validate(
        [{"id": f"J{jid}", "op": "rewrite", "wrong": "sea ra", "reason": "misheard name",
          "text": "The user asked to message Ciara on Discord about the trip."}], [cand])
    assert rejected == [] and len(edits) == 1
    change = {"ok": True, "op": "rewrite", "old": cand.text, "held": f"{cand.subject} {cand.text}",
              "new": edits[0]["text"]}
    assert CorrectionApplier.confirmed_aliases([{"heard": "sea ra", "name": "Ciara", "why": "x"}], [change])


def test_w6_the_misheard_backfill_finds_guesses_drops_random_lines_and_undoes(store):
    from yuki.memory.misheard import MisheardPass

    a = _journal(store, "The user said what sounded like 'generic prices' about the design.", at=NOW - 600,
                 nature="uncertain", source={"kind": "call", "quote": "generic prices on the design"})
    b = _journal(store, "The user said something about the weekend.", at=NOW - 300, nature="uncertain",
                 source={"kind": "call", "quote": "haan weekend pe dekhte"})
    answer = {"entries": [
        {"id": f"J{a}", "misheard": True, "heard": "generic prices", "meant": "generous praise",
         "why": "the talk was feedback on the design", "is_name": False,
         "shown": "You said the design got generous praise."},
        {"id": f"J{b}", "misheard": False, "heard": "", "meant": "", "why": "", "is_name": False, "shown": ""}]}
    worker = _Worker(store, [answer])
    r = MisheardPass(worker).run(now=NOW)
    assert r.ok and (r.entries, r.misheard, r.dropped, r.written) == (2, 1, 1, 1)
    [item] = MemoryClient(store).misheard(now=NOW)["items"]
    assert item["id"] == a and item["guess"] == "generous praise" and item["shown"].startswith("You said")
    assert store.journal_by_ids([a])[0].source["quote"] == "generic prices on the design"   # provenance kept
    assert MisheardPass(_Worker(store)).undo(r.run_id) == {"restored": 1, "diverged": 0}
    assert "heard" not in store.journal_by_ids([a])[0].source
