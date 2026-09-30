"""The context pack around a call (yuki.memory.callcontext) and how the digest and journal read it (2026-10-01).

Offline: temp stores and stub model clients only.
"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from yuki.memory import callcontext
from yuki.memory.calldigest import CallDigestWorker, merge_people, parse_people, render_request, Piece, Utterance
from yuki.memory.journal import build_user_message, system_prompt
from yuki.memory.store import ModelCall, Store, ThreadInfo

T0 = datetime(2026, 9, 30, 21, 0).timestamp()


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _screen(store: Store, app: str, title: str, at: float, text: str, *, kind: str = "page",
            trigger: str = "foreground", url: str | None = None) -> int:
    tid = store.upsert_thread(app, title, url, process=f"{app}.exe", kind=kind)
    store.add_capture(tid, at, trigger, text, text, kind=kind)
    return tid


def _call(store: Store, start: float, end: float, words: list[tuple[float, str]]) -> int:
    cid = store.start_call(app="Discord", process="Discord.exe", via="screen", at=start, status="recording",
                           consent="always", consent_by="pref")
    store.update_call(cid, status="ended", ended_at=end, stop_reason="call_ended", recording_started_at=start)
    utts = [{"at": at, "end": at + 3, "text": text, "lang": "en"} for at, text in words]
    cap = store.add_call_side(cid, app="Discord", process="Discord.exe", started_at=utts[0]["at"],
                              ended_at=utts[-1]["end"], utterances=utts, final=True)
    rec = store.call_record(cid)
    store.commit_call_batch(ModelCall(at=time.time(), thread_id=rec.thread_id, model="stub"), [cap], [])
    return cid


def _scene(store: Store) -> int:
    _screen(store, "Discord", "General | Friends", T0 - 120, "General\nRohan\nPriya\nVoice Connected")
    _screen(store, "Discord", "General | Friends", T0 + 600, "Rohan: push it", kind="conversation")
    _screen(store, "Google Chrome", "Fix relay #42 - GitHub", T0 + 300, "Pull request: fix the relay retry",
                    url="https://github.com/acme/relay/pull/42")
    _screen(store, "WhatsApp", "Rohan", T0 - 900, "Rohan: call in 5?", kind="conversation")
    _screen(store, "WhatsApp", "Old chat", T0 - 3 * 3600, "much earlier", kind="conversation")
    store.upsert_links([{"url": "https://github.com/acme/relay/pull/42", "how": "shared", "at": T0 + 610,
                         "until": T0 + 610, "sender": "Rohan", "title": "Fix relay #42", "app": "Discord"}])
    return _call(store, T0, T0 + 1800, [(T0 + 320, "Rohan did you see the PR"), (T0 + 620, "yeah do it")])


def test_pack_holds_the_call_screen_other_windows_links_and_chats_around_time_stamped_and_capped(store):
    cid = _scene(store)
    ctx = callcontext.for_call(store, cid)
    text = ctx.text
    assert "CALL APP (Discord)" in text and "Rohan" in text and "Priya" in text
    assert "(before the call began)" in text                       # the joining screen
    assert "OTHER SCREENS" in text and "Fix relay #42 - GitHub" in text
    assert "LINKS" in text and "shared by Rohan" in text
    assert "MESSAGES AROUND" in text and "call in 5?" in text and "much earlier" not in text
    assert "yeah do it" not in text                                 # never the call's own words
    assert f"{datetime.fromtimestamp(T0 + 300):%H:%M}" in text      # items carry their local time
    assert ctx.chars <= callcontext.MAX_CHARS and ctx.capture_thread_ids
    # parts of a long call: no chats before a later part, none after an earlier one
    later = callcontext.for_call(store, cid, start=T0 + 900, end=T0 + 1800, before=False, after=True)
    assert "call in 5?" not in later.text and "(before the call began)" not in later.text


def test_people_are_kept_only_when_something_showed_them_and_never_the_user():
    numbered = {1: (7, Utterance(at=T0, end=T0 + 2, text="Priya listen")), 2: (8, Utterance(at=T0, end=T0 + 1, text="x"))}
    answer = {"calls": [{"call": 7, "gist": "g", "where": "Discord, General in Friends", "people": [
        {"name": "Rohan", "how": "likely"}, {"name": "rohan", "how": "on_call_screen"},     # strongest kept
        {"name": "Priya", "how": "user_named"},                                              # no such kind any more
        {"name": "Ghost", "how": "on_call_screen"},                                          # nothing showed it
        {"name": "Mafex", "how": "on_call_screen"}]},                                        # the user
        {"call": 8, "gist": "g", "where": "Somewhere", "people": [{"name": "Rohan", "how": "call_chat"}]}]}
    got = parse_people(answer, numbered, {7: "General / Rohan / Mafex / Voice Connected"}, me=["Kian", "Mafex"])
    assert got[7]["people"] == [{"name": "rohan", "how": "on_call_screen"}]
    assert got[7]["where"] == "Discord, General in Friends"
    assert got[8] == {"people": [], "where": ""}                     # no context: no names, no place
    assert merge_people([{"name": "A", "how": "likely"}], [{"name": "a", "how": "call_chat"}]) == [
        {"name": "a", "how": "call_chat"}]


class _Stub:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, **request):
        self.requests.append(request)
        text = request["messages"][0]["content"]
        call = next(int(line.split()[1]) for line in text.splitlines() if line.startswith("CALL "))
        nums = [int(line[1:line.index("]")]) for line in text.splitlines() if line.startswith("[")]
        tool = SimpleNamespace(type="tool_use", name="write_call_timeline", id="t", input={
            "blocks": [{"first": nums[0], "last": nums[-1], "topic": "the relay PR",
                        "lines": [{"at": nums[-1], "text": "Told Rohan to merge the relay PR", "nature": "fact"}]}],
            "calls": [{"call": call, "gist": "Relay PR with Rohan", "where": "Discord, General in Friends",
                       "people": [{"name": "Rohan", "how": "on_call_screen"}]}]})
        return SimpleNamespace(content=[tool], stop_reason="tool_use", usage={"input_tokens": 3000, "output_tokens": 200})


def test_digest_reads_the_pack_before_the_words_and_stores_who_and_where(store, tmp_path):
    from yuki.agent.memory import format_call_timeline
    from yuki.memory.api import MemoryClient

    cid = _scene(store)
    stub = _Stub()
    summary = CallDigestWorker(store, client=stub, log_dir=tmp_path / "logs").digest([cid], force=True)
    assert summary["ok"] == [cid] and summary["context_chars"] > 0
    user = stub.requests[0]["messages"][0]["content"]
    assert user.index("CONTEXT AROUND CALL") < user.index("THE USER'S UTTERANCES") < user.index("yeah do it")
    assert user.index("===BEGIN_UNTRUSTED_DATA_") < user.index("Priya") < user.index("===END_UNTRUSTED_DATA_")
    assert "CONTEXT AROUND THE CALL" in stub.requests[0]["system"][0]["text"]
    d = store.call_digest(cid)
    assert d["context"]["people"] == [{"name": "Rohan", "how": "on_call_screen"}]
    assert d["context"]["where"] == "Discord, General in Friends"
    raw = store._query("SELECT context_ciphertext FROM call_digests WHERE call_id=?", (cid,))[0][0]
    assert "Rohan" not in raw                                       # encrypted at rest
    text = format_call_timeline(MemoryClient(store).call_timeline(cid))
    assert "With (read from the screen around the call" in text and "Rohan (on the call screen)" in text
    # the switch back to the words alone
    stub2 = _Stub()
    CallDigestWorker(store, client=stub2, log_dir=tmp_path / "logs", context=False).digest([cid], force=True)
    assert "CONTEXT AROUND CALL" not in stub2.requests[0]["messages"][0]["content"]


def test_render_request_without_context_is_unchanged():
    p = Piece(3, "Discord", 1, 1, [Utterance(at=T0, end=T0 + 2, text="hi")])
    text, numbered = render_request([p], {}, "N")
    assert "CONTEXT" not in text and numbered[1][0] == 3


def test_packing_counts_utterances_and_caps_context_separately():
    from yuki.memory.calldigest import pack_requests

    full = Piece(1, "Discord", 1, 2, [Utterance(at=T0 + i, end=T0 + i, text="x" * 1_000) for i in range(9)],
                 context="c" * 5_000)
    small = [Piece(i, "Discord", 1, 1, [Utterance(at=T0, end=T0 + 1, text="hi")], context="c" * 5_000)
             for i in (2, 3, 4)]
    # the utterances decide the packing as before (the context does not push a part into another's request) ...
    assert [[p.call_id for p in r] for r in pack_requests([full, *small], 10_000, 14_000)] == [[1, 2], [3, 4]]
    # ... and the context of the calls sharing a request is capped on its own
    assert all(sum(len(p.context) for p in r) <= 14_000 for r in pack_requests(small * 3, 10_000, 14_000))
    big = Piece(5, "Discord", 1, 1, [Utterance(at=T0 + i, end=T0 + i, text="x" * 1_000) for i in range(6)],
                context="c" * 5_000)
    assert len(pack_requests([full, big], 10_000, 14_000)) == 2


def test_journal_call_batch_gets_the_pack_after_its_sources_and_the_guidance(store):
    thread = ThreadInfo(id=1, app="Discord", host=None, title="Discord call", url=None, first_seen=T0,
                        last_seen=T0, kind="my_call_side")
    msg = build_user_message(thread, [], [], "N", context="CALL APP (Discord): - 21:00 Rohan")
    assert msg.index("SOURCES") < msg.index("CONTEXT AROUND THE CALL") and "Rohan" in msg
    assert "CONTEXT AROUND THE CALL" in system_prompt(["S"], questions=False, call_side=True)
    assert "CONTEXT AROUND THE CALL" not in system_prompt(["S"])    # screen batches keep their prompt (and cache)


def test_journal_worker_builds_the_pack_for_a_call_batch(store, tmp_path):
    from yuki.memory.journal import JournalWorker, build_sources

    cid = _scene(store)
    rec = store.call_record(cid)
    caps = [c for c in store.pending_captures() if c.thread_id == rec.thread_id] or \
        store.batch_capture_rows(store._query("SELECT journal_batch_id FROM captures WHERE thread_id=?",
                                              (rec.thread_id,))[0][0])
    worker = JournalWorker(store, log_dir=tmp_path / "logs")
    sources = build_sources(caps, {})
    texts, windows = worker._call_contexts(caps, worker._call_parts(sources))
    assert len(texts) == 1 and "Priya" in texts[0] and windows
