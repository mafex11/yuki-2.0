"""Offline checks of the tricky logic behind connecting the dots (docs/MEMORY.md "Threads of work",
"Connecting the dots"): the journal's work answer checked in code, the store applying it, the
episode overlap rule (the 29 Sept duplicate), links found in text, the index's affinity boost, and
"before" picking the break. Temp databases, no model, nothing touches the desktop.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory.store import ModelCall, NewFact, Store, TimelineRow, WorkOp, WorkThread

DAY = datetime(2026, 9, 28)


def at(hhmm: str, day: int = 0) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return (DAY + timedelta(days=day, hours=h, minutes=m)).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def _thread(tid: int, title: str = "Yuki voice pipeline") -> WorkThread:
    return WorkThread(tid, title, None, "active", "", [], [], at("10:00"), at("11:00"), None, "journal", None, 1,
                      at("10:00"), at("11:00"))


def _src(t: float, by_yuki: bool = False) -> SimpleNamespace:
    return SimpleNamespace(at=t, capture=SimpleNamespace(by_yuki=by_yuki))


# -- the journal's work answer, checked in code ------------------------------------------------


def test_parse_work_checks_threads_titles_and_links_facts_by_source():
    from yuki.memory.workthreads import parse_work

    offered = {7: _thread(7)}
    sources = [_src(at("12:00")), _src(at("12:05")), _src(at("12:10"), by_yuki=True)]
    fact_sources = [0, 1, 1, 2]   # the facts' source indexes
    items = [
        {"thread": 7, "action": "continues", "title": "", "state": "tests pass", "open_items": ["ship"],
         "entities": ["Warp"], "sources": [1]},
        {"thread": 9, "action": "continues", "title": "", "state": "", "open_items": [], "entities": [], "sources": []},
        {"thread": 0, "action": "starts", "title": "", "state": "", "open_items": [], "entities": [], "sources": [2]},
        {"thread": 0, "action": "starts", "title": "Yuki's own", "state": "", "open_items": [], "entities": [],
         "sources": [3]},
    ]
    ops, rejected = parse_work(items, offered, sources, fact_sources, thread=SimpleNamespace(id=4, app="Warp", host=None))
    assert [op.work_thread_id for op in ops] == [7]
    assert ops[0].fact_indexes == [0] and ops[0].at == at("12:00") and ops[0].capture_thread_id == 4
    assert rejected == ["not a thread it was offered", "a new thread without a title",
                        "what Yuki put on screen starts no thread"]
    # a "start" that names an offered thread continues it; an op citing no source takes the facts left over
    ops, _ = parse_work([{"thread": 7, "action": "starts", "title": "x", "state": "", "open_items": [], "entities": [],
                          "sources": []}], offered, sources, fact_sources, thread=SimpleNamespace(id=4, app="", host=None))
    assert ops[0].action == "continues" and ops[0].fact_indexes == [0, 1, 2, 3]


def test_store_applies_work_in_the_batch_and_keeps_the_newer_state(store):
    tid = store.upsert_thread("Warp", "yuki", None, scope="terminal:yuki", kind="terminal")
    call = ModelCall(at=at("12:00"), thread_id=tid, model="stub")
    fact = NewFact(at=at("12:00"), thread_id=tid, app="Warp", host=None, fact="The user ran the tests.", importance=4)
    start = WorkOp("starts", None, title="Yuki voice pipeline", state="tests failing", open_items=["fix barge-in"],
                   entities=["Warp"], at=at("12:00"), fact_indexes=[0], capture_thread_id=tid, importance=5,
                   vector=np.ones(4, dtype=np.float32))
    store.commit_batch(call, [], [fact], work=[start])
    wid = start.result_id
    assert start.outcome == "started" and wid is not None
    assert store.journal_for_work_thread(wid)[0].fact == "The user ran the tests."
    later = WorkOp("continues", wid, state="tests pass", at=at("13:00"), capture_thread_id=tid)
    older = WorkOp("continues", wid, state="an old message says it is broken", at=at("09:00"), capture_thread_id=tid)
    store.apply_work([later])
    store.apply_work([older])
    t = store.work_thread(wid)
    assert t.state == "tests pass" and t.events == 3 and t.first_seen == at("09:00") and t.last_seen == at("13:00")
    assert store.work_threads_of_capture_threads([tid]) == [wid]
    # the nightly tidy: a thread judged no piece of work lets go of its facts and takes no more ops
    store.change_work_threads([{"action": "dismiss", "id": wid, "reason": "test"}])
    assert store.work_thread(wid).status == "dismissed" and not store.journal_for_work_thread(wid)
    again = WorkOp("continues", wid, state="x", at=at("14:00"))
    store.apply_work([again])
    assert again.outcome == "rejected:dismissed thread"


def test_merge_moves_facts_and_events_to_the_thread_kept(store):
    a = WorkOp("starts", None, title="A", at=at("10:00"))
    b = WorkOp("starts", None, title="B", at=at("11:00"))
    store.apply_work([a, b])
    fact = NewFact(at=at("11:00"), thread_id=None, app="Slack", host=None, fact="f", importance=3)
    store.commit_batch(ModelCall(at=at("11:00"), thread_id=None, model="stub"), [], [fact],
                       work=[WorkOp("continues", b.result_id, at=at("11:05"), fact_indexes=[0])])
    out = store.change_work_threads([{"action": "merge", "ids": [a.result_id, b.result_id], "keep": a.result_id,
                                      "title": "A and B", "state": "one", "open": []}])
    assert out[0]["ok"]
    assert store.work_thread(b.result_id).merged_into == a.result_id
    assert store.resolve_work_thread(b.result_id) == a.result_id
    assert [j.fact for j in store.journal_for_work_thread(a.result_id)] == ["f"]
    assert store.work_thread(a.result_id).title == "A and B"


# -- episodes: one stretch told once (the 29 Sept 03:45-04:08 duplicate) --------------------------------


def test_episodes_never_tell_one_stretch_twice():
    from yuki.memory.episodes import non_overlapping, retellings

    def ep(i: int, a: str, b: str) -> SimpleNamespace:
        return SimpleNamespace(id=i, started_at=at(a), ended_at=at(b))

    kept, dropped = non_overlapping([ep(1, "02:44", "03:45"), ep(2, "03:45", "04:08"), ep(3, "03:45", "04:08")])
    assert [e.id for e in kept] == [1, 2] and dropped == 1
    # told whole and in parts: the parts stay; a small overlap is trimmed off the later one
    kept, dropped = non_overlapping([ep(4, "03:00", "05:13"), ep(5, "03:00", "04:05"), ep(6, "04:05", "05:13")])
    assert [e.id for e in kept] == [5, 6] and dropped == 1
    kept, _ = non_overlapping([ep(7, "10:00", "11:00"), ep(8, "10:50", "12:00")])
    assert kept[1].started_at == at("11:00")
    assert retellings([ep(223, "03:45", "04:08"), ep(224, "03:45", "04:08")]) == [(224, 223)]


# -- links: a fixed machine format, never words --------------------------------------------------------


def test_links_found_in_text_and_messages():
    from yuki.memory.links import message_links, urls_in

    assert urls_in("see https://github.com/a/b/pull/12. and (http://x.io/y?q=1), ftp://no.com https://localhost") == [
        "https://github.com/a/b/pull/12", "http://x.io/y?q=1"]
    assert message_links("[new] 18:12 Varun: https://docs.stripe.com/api\n[history] - the user: ok") == [
        ("https://docs.stripe.com/api", "Varun")]


# -- the whole-memory index: affinity never lets an item in by itself -------------------------------------


def test_affinity_boost_ranks_linked_items_only_above_the_floor(store):
    from yuki.memory.relevant import RelevantIndex

    q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    vecs = {1: [0.9, 0.0, 0.44, 0.0], 2: [0.88, 0.47, 0.0, 0.0], 3: [0.2, 0.0, 0.0, 0.98]}
    ids = {}
    for key, v in vecs.items():
        v = np.asarray(v, dtype=np.float32)
        ids[key] = store.add_journal(at("10:00"), None, "Warp", f"fact {key}", 5, vector=v / np.linalg.norm(v),
                                     embed_model="stub")
    index = RelevantIndex(store)
    index.refresh()
    plain = index.search(q, now=at("12:00"), kinds=("journal",), min_cosine=0.5)
    boosted = index.search(q, now=at("12:00"), kinds=("journal",), min_cosine=0.5, boost={"journal": {ids[2], ids[3]}},
                           boost_weight=0.1)
    assert [h["id"] for h in plain] == [ids[1], ids[2]]
    assert [h["id"] for h in boosted] == [ids[2], ids[1]]   # item 3 is under the floor: the boost cannot add it


# -- before: the break nearest a moment, and the stretch before it --------------------------------------------


def test_before_picks_the_break_nearest_the_moment(store):
    from yuki.memory.before import before, breaks_near

    for a, b in (("09:00", "13:00"), ("14:10", "18:00"), ("19:00", "23:00")):
        t = at(a)
        while t < at(b):
            store.save_timeline(TimelineRow(None, t, min(t + 300, at(b)), "present", "warp", "Warp",
                                            active_s=min(300.0, at(b) - t)))
            t += 300
    found = breaks_near(store, at("13:30"), hours=12)
    assert [(b["start"], b["end"]) for b in found] == [(at("13:00"), at("14:10")), (at("18:00"), at("19:00"))]
    r = before(store, at("13:40"), minutes=60, now=at("23:30"))
    assert r["ok"] and r["break"]["start"] == at("13:00") and r["until"] == at("13:00")
    assert "LAST THING IN FRONT" in r["text"]
    r = before(store, at("12:00"), event="time", minutes=30, now=at("23:30"))
    assert r["break"] is None and r["since"] == at("11:30")


# -- windows beside the one in front and popups: weaker evidence of attention (migration 24) ---------------


def test_glimpsed_sources_start_nothing_and_are_no_touch(store):
    from yuki.memory.store import capture_attention
    from yuki.memory.workthreads import parse_work

    assert [capture_attention(t) for t in ("settle", "visible:changed", "popup", None)] == [
        "front", "visible", "popup", "front"]

    def src(t: float, trigger: str) -> SimpleNamespace:
        return SimpleNamespace(at=t, capture=SimpleNamespace(by_yuki=False, trigger=trigger))

    wid = store.apply_work([WorkOp("starts", None, title="Yuki voice pipeline", state="tests pass",
                                   at=at("10:00"))])[0].result_id
    offered = {wid: store.work_thread(wid)}
    sources = [src(at("12:00"), "visible:new"), src(at("12:01"), "popup")]
    items = [{"thread": 0, "action": "starts", "title": "Stripe payments", "state": "", "open_items": [],
              "entities": [], "sources": [1]},
             {"thread": wid, "action": "continues", "title": "", "state": "Varun says the deploy is fixed",
              "open_items": [], "entities": [], "sources": [2]}]
    ops, rejected = parse_work(items, offered, sources, [0, 1], thread=SimpleNamespace(id=None, app="Slack", host=None))
    assert rejected == ["what was only glimpsed starts no thread"]
    assert ops[0].attention == "popup"
    store.apply_work(ops)
    t = store.work_thread(wid)
    assert ops[0].outcome == "seen" and t.state == "Varun says the deploy is fixed" and t.last_seen == at("10:00")
    assert not store.latest_work_events(since=at("11:00"))   # no touch of the user's


def test_links_only_beside_the_front_rank_lower_until_seen_in_front(store):
    from yuki.memory.relevant import ATTENTION_DISCOUNT, RelevantIndex

    [lid] = store.upsert_links([{"url": "https://example.com/a", "how": "viewed", "at": at("10:00"),
                                 "attention": "visible"}])
    assert store.links(ids=[lid])[0].attention == "visible"
    v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    store.add_link_vectors([(lid, v)], "stub")
    index = RelevantIndex(store)
    index.refresh()
    weak = index.search(v, now=at("10:00"), kinds=("link",), min_cosine=0.5)[0]["score"]
    store.upsert_links([{"url": "https://example.com/a", "how": "viewed", "at": at("10:05")}])   # now in front
    assert store.links(ids=[lid])[0].attention is None
    index = RelevantIndex(store)
    index.refresh()
    strong = index.search(v, now=at("10:00"), kinds=("link",), min_cosine=0.5)[0]["score"]
    assert round(strong - weak, 4) == ATTENTION_DISCOUNT
