"""Call timeline digests (yuki.memory.calldigest), the calls / call_timeline tools, and the recall pointer.

Offline: temp databases and a stub model client; the real database is never read.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.calldigest import (
    PART_CHARS,
    CallDigestWorker,
    Piece,
    Utterance,
    pack_requests,
    parse_blocks,
    prepare,
    render_request,
    split_parts,
)
from yuki.memory.store import MIGRATIONS, ModelCall, Store

T0 = datetime(2026, 9, 25, 13, 20).timestamp()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utt(at: float, text: str, dur: float = 3.0, **kw) -> dict:
    return {"at": at, "end": at + dur, "text": text, "lang": kw.get("lang", "en"), **kw}


def _call(store: Store, start: float, end: float, utts: list[dict], *, stop_reason: str = "service_restart",
          journal: bool = True, app: str = "Discord") -> int:
    cid = store.start_call(app=app, process=f"{app}.exe", via="mic", at=start, status="recording",
                           consent="always", consent_by="pref")
    store.update_call(cid, status="ended", ended_at=end, stop_reason=stop_reason, recording_started_at=start,
                      speech_s=sum(u["end"] - u["at"] for u in utts))
    if utts:
        cap = store.add_call_side(cid, app=app, process=f"{app}.exe", started_at=utts[0]["at"],
                                  ended_at=utts[-1]["end"], utterances=utts, final=True)
        if journal:
            rec = store.call_record(cid)
            store.commit_call_batch(ModelCall(at=time.time(), thread_id=rec.thread_id, model="stub"), [cap], [])
    return cid


class StubClient:
    """Answers write_call_timeline: one block per call in the request, a line on its first and last utterance."""

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[dict] = []
        self.fail = fail

    @property
    def messages(self):
        return self

    def create(self, **request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("bedrock down")
        text = request["messages"][0]["content"]
        calls: dict[int, list[int]] = {}
        current = None
        for line in text.splitlines():
            if line.startswith("CALL "):
                current = int(line.split()[1])
                calls[current] = []
            elif line.startswith("[") and current is not None and "]" in line:
                calls[current].append(int(line[1:line.index("]")]))
        blocks = [{"first": nums[0], "last": nums[-1], "topic": f"talk {cid}",
                   "lines": [{"at": nums[0], "text": f"Started call {cid}", "nature": "fact"},
                             {"at": nums[-1], "text": "Joked about the match", "nature": "humor"}]}
                  for cid, nums in calls.items() if nums]
        tool = SimpleNamespace(type="tool_use", name="write_call_timeline", id="t1",
                               input={"blocks": blocks, "calls": [{"call": c, "gist": f"gist of {c}"} for c in calls]})
        return SimpleNamespace(content=[tool], stop_reason="tool_use",
                               usage={"input_tokens": 2000, "output_tokens": 300})


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# block splitting and structure
# ---------------------------------------------------------------------------


def test_prepare_marks_recogniser_loops_and_drops_double_stored_utterances():
    caps = [{"data": {"utterances": [_utt(T0, "कर दो"), _utt(T0 + 4, "कर दो।"), _utt(T0 + 8, "कर दो"),
                                     _utt(T0 + 20, "we push the build tonight")]}},
            {"data": {"utterances": [_utt(T0 + 20, "we push the build tonight"), _utt(T0 + 900, "कर दो")]}}]
    utts = prepare(caps)
    assert [u.text for u in utts] == ["कर दो", "कर दो।", "कर दो", "we push the build tonight", "कर दो"]
    assert [u.repeat for u in utts] == [False, True, True, False, False]   # 15 min later it is said again


def test_split_parts_cuts_at_the_longest_silence_and_keeps_everything():
    utts, t = [], T0
    for i in range(60):
        gap = 600.0 if i == 30 else 5.0          # one long silence in the back half of the first part
        t += gap
        utts.append(Utterance(at=t, end=t + 3, text="x" * 180))
    parts = split_parts(utts, 8_000)
    assert [u for p in parts for u in p] == utts
    assert all(sum(len(u.text) + 24 for u in p) <= 8_000 for p in parts)
    # the first part (39 fit) closes at the long silence, not where it filled up: utterance 30 starts a part
    assert parts[1][0] is utts[30]


def test_split_parts_single_part_when_small_and_empty_input():
    utts = [Utterance(at=T0 + i, end=T0 + i + 1, text="hi") for i in range(5)]
    assert split_parts(utts) == [utts]
    assert split_parts([]) == []


def test_pack_requests_packs_small_calls_and_sends_big_parts_alone():
    small = [Piece(i, "Meet", 1, 1, [Utterance(at=T0, end=T0 + 1, text="ok")]) for i in (1, 2, 3)]
    big = Piece(4, "Discord", 1, 2, [Utterance(at=T0 + i, end=T0 + i + 1, text="y" * (PART_CHARS // 10 - 24))
                                     for i in range(10)])   # exactly PART_CHARS: a part that filled up
    reqs = pack_requests([*small, big, small[0]], PART_CHARS)
    assert [[p.call_id for p in r] for r in reqs] == [[1, 2, 3], [4], [1]]


def test_render_request_numbers_across_calls_marks_silence_and_fences():
    a = Piece(7, "Discord", 1, 1, [Utterance(T0, T0 + 2, "hello"), Utterance(T0 + 400, T0 + 402, "back", unclear=True,
                                                                           lang="en")])
    b = Piece(8, "Zoom", 1, 1, [Utterance(T0 + 1000, T0 + 1002, "hi all")])
    text, numbered = render_request([a, b], {7: "EARLIER GIST: setting up"}, "NONCE")
    assert text.startswith("===BEGIN_UNTRUSTED_DATA_NONCE===") and text.endswith("===END_UNTRUSTED_DATA_NONCE===")
    assert "(silence 7 min)" in text and "UNCLEAR" in text and "EARLIER GIST: setting up" in text
    assert {n: cid for n, (cid, _) in numbered.items()} == {1: 7, 2: 7, 3: 8}


def test_parse_blocks_checks_numbers_orders_clips_and_takes_times_from_utterances():
    u = [Utterance(T0 + 60 * i, T0 + 60 * i + 5, f"u{i}") for i in range(6)]
    numbered = {1: (7, u[0]), 2: (7, u[1]), 3: (7, u[2]), 4: (7, u[3]), 5: (8, u[4]), 6: (8, u[5])}
    answer = {
        "blocks": [
            {"first": 3, "last": 6, "topic": "second", "lines": [{"at": 3, "text": "Asked about the build",
                                                                   "nature": "fact"},
                                                                  {"at": 5, "text": "other call line", "nature": "fact"}]},
            {"first": 1, "last": 3, "topic": "first", "lines": [{"at": 1, "text": " Said hi ", "nature": "odd"}]},
            {"first": 99, "last": 100, "topic": "ghost", "lines": [{"at": 99, "text": "invented", "nature": "fact"}]},
            {"first": 5, "last": 6, "topic": "zoom", "lines": [{"at": 6, "text": "Joked", "nature": "humor"}]},
            {"first": 2, "last": 2, "topic": "empty", "lines": []},
        ],
        "calls": [{"call": 7, "gist": "Build talk"}, {"call": 42, "gist": "not in this request"}],
    }
    blocks, gists = parse_blocks(answer, numbered)
    assert gists == {7: "Build talk"}
    # "second" was clipped to its own call (up to 4) and moved past "first" (which ends at 3); its one line,
    # on utterance 3, went to the block that holds 3, leaving it empty, so it is gone; the line on the other
    # call's utterance 5 was dropped; the ghost block (no such utterances) and the empty block too
    assert [(b["call_id"], b["topic"]) for b in blocks] == [(7, "first"), (8, "zoom")]
    first, zoom = blocks
    assert [ln["text"] for ln in first["lines"]] == ["Said hi", "Asked about the build"]
    assert first["start"] == u[0].at and first["end"] == u[2].end
    assert first["lines"][0]["nature"] == "fact" and first["lines"][0]["at"] == u[0].at
    assert zoom["lines"][0]["nature"] == "humor" and zoom["start"] == u[4].at
    assert parse_blocks(None, numbered) == ([], {})


# ---------------------------------------------------------------------------
# storage and migration
# ---------------------------------------------------------------------------


def test_migration_19_is_additive_from_18(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    from yuki.memory.store import _split_sql

    for script in MIGRATIONS[:18]:
        for statement in _split_sql(script):
            conn.execute(statement)
    conn.execute("PRAGMA user_version=18")
    conn.execute("INSERT INTO calls(app, via, detected_at, status, updated_at) VALUES ('Discord','mic',1,'ended',1)")
    conn.commit()
    conn.close()
    with Store.open(path) as s:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS) >= 19
        assert s.call_record(1).app == "Discord"
        assert s._query("SELECT count(*) FROM call_digests")[0][0] == 0


def test_digest_storage_is_encrypted_claimed_and_redone_when_words_change(store):
    cid = _call(store, T0, T0 + 600, [_utt(T0 + 5, "hello"), _utt(T0 + 50, "bye")])
    assert [c["call_id"] for c in store.call_digest_candidates()] == [cid]
    caps = [c["id"] for c in store.call_side_captures(cid)]
    assert store.claim_call_digest(cid, app="Discord", capture_ids=caps, owner="a")
    assert not store.claim_call_digest(cid, app="Discord", capture_ids=caps, owner="b")   # fresh claim held
    assert store.call_digest_candidates() == []
    store.save_call_digest(cid, app="Discord", capture_ids=caps, status="ok", gist="secret gist words",
                           blocks=[{"start": T0 + 5, "end": T0 + 53, "topic": "t",
                                    "lines": [{"at": T0 + 5, "text": "Said hello", "nature": "fact"}]}],
                           started_at=T0 + 5, ended_at=T0 + 53, speech_s=6, utterances=2, kept=2, model="m",
                           usage={"input_tokens": 10, "output_tokens": 5}, requests=1, cost_usd=0.01)
    raw = store._query("SELECT gist_ciphertext, blocks_ciphertext FROM call_digests")[0]
    assert "secret" not in raw[0] and "Said hello" not in raw[1]
    d = store.call_digest(cid)
    assert d["gist"] == "secret gist words" and d["blocks"][0]["lines"][0]["text"] == "Said hello"
    assert store.call_digest_candidates() == []
    assert store.digested_call_threads() == {store.call_record(cid).thread_id: cid}
    # the call gains words (another capture): it is a candidate again
    store.add_call_side(cid, app="Discord", process="Discord.exe", started_at=T0 + 100, ended_at=T0 + 104,
                        utterances=[_utt(T0 + 100, "one more")])
    assert store.call_digest_candidates(journaled_only=False)[0]["call_id"] == cid
    assert store.call_digest_candidates() == []   # not journaled yet: the journal goes first
    # the digest outlives the transcript's TTL
    store.prune(older_than_days=0)
    assert store.call_side_captures(cid) == [] and store.call_digest(cid)["gist"] == "secret gist words"


def test_failing_digest_is_given_up_after_its_attempts(store, tmp_path):
    cid = _call(store, T0, T0 + 600, [_utt(T0 + 5, "hello")])
    worker = CallDigestWorker(store, client=StubClient(fail=True), log_dir=tmp_path / "logs")
    for _ in range(3):
        summary = worker.digest()
        assert summary["failed"] == [cid]
    assert store.call_digest(cid)["attempts"] == 3
    assert worker.digest()["calls"] == []
    assert "bedrock down" in store.call_digest(cid)["error"]


# ---------------------------------------------------------------------------
# the worker, end to end with a stub model
# ---------------------------------------------------------------------------


def test_worker_writes_digests_batches_small_calls_and_logs_cost(store, tmp_path):
    a = _call(store, T0, T0 + 3600, [_utt(T0 + 60 * i, f"line {i} about the match") for i in range(10)])
    b = _call(store, T0 + 3600, T0 + 7200, [_utt(T0 + 3700, "second row"), _utt(T0 + 3710, "second row")])
    stub = StubClient()
    worker = CallDigestWorker(store, client=stub, log_dir=tmp_path / "logs")
    summary = worker.digest()
    assert summary["ok"] == [a, b] and summary["failed"] == []
    assert len(stub.requests) == 1                     # two small calls, one request
    assert summary["cost_usd"] > 0 and summary["cost_per_speech_hour_usd"] > 0
    da, db = store.call_digest(a), store.call_digest(b)
    assert da["gist"] == f"gist of {a}" and da["blocks"][0]["start"] == T0
    assert db["kept"] == 1 and db["utterances"] == 2   # the repeat was left out
    assert da["cost_usd"] > 0
    log = (tmp_path / "logs").glob("calldigest-*.jsonl")
    records = [json.loads(line) for f in log for line in f.read_text(encoding="utf-8").splitlines()]
    req = next(r for r in records if r["type"] == "call_digest_request")
    assert req["usage"]["input_tokens"] == 2000 and "request_ciphertext" in req and "line 0" not in json.dumps(req)
    assert worker.digest()["calls"] == []              # nothing left to do
    assert store.activity_today(0)["call_digest_cost_usd"] > 0


def test_worker_waits_during_a_call_and_while_paused(store, tmp_path):
    worker = CallDigestWorker(store, client=StubClient(), log_dir=tmp_path)
    pause = tmp_path / "paused"
    pause.write_text("")
    assert worker.blocked(pause) == "paused"
    store.start_call(app="Discord", process="Discord.exe", via="mic", status="recording")
    assert worker.blocked(None) == "call"
    worker2 = CallDigestWorker(Store.open(tmp_path / "other.db"), client=StubClient(), log_dir=tmp_path,
                               live_fn=lambda: {"fullscreen": True})
    assert worker2.blocked(None) == "fullscreen"
    worker2.store.close()


# ---------------------------------------------------------------------------
# the API and the agent's tools
# ---------------------------------------------------------------------------


def _day_of_calls(store: Store, tmp_path: Path) -> tuple[int, int, int]:
    """Three rows: two split by a service restart (one session), and a separate call hours later."""
    a = _call(store, T0, T0 + 1800, [_utt(T0 + 60 * i, f"first part {i}") for i in range(5)])
    b = _call(store, T0 + 1800, T0 + 3600, [_utt(T0 + 1900 + 30 * i, f"second part {i}") for i in range(4)]
              + [_utt(T0 + 2100, "second part 3")], stop_reason="call_ended")
    c = _call(store, T0 + 20000, T0 + 21000, [_utt(T0 + 20100, "later call")], stop_reason="call_ended")
    CallDigestWorker(store, client=StubClient(), log_dir=tmp_path / "logs").digest()
    return a, b, c


def test_calls_in_merges_one_session_and_reports_speech_and_gists(store, tmp_path):
    from yuki.memory.api import MemoryClient

    a, b, c = _day_of_calls(store, tmp_path)
    client = MemoryClient(store)
    day = datetime.fromtimestamp(T0).replace(hour=0, minute=0)
    calls = client.calls_in(day, day + timedelta(days=1))
    assert [x["call_ids"] for x in calls] == [[a, b], [c]]
    first = calls[0]
    assert first["ref"] == f"call:{a}" and first["recorded"] and first["speech_s"] > 0
    assert [g["call_id"] for g in first["gists"]] == [a, b]
    assert all(r["state"] == "timeline written" for r in first["rows"])


def test_call_timeline_merges_rows_and_narrows_to_a_window(store, tmp_path):
    from yuki.memory.api import MemoryClient

    a, b, _ = _day_of_calls(store, tmp_path)
    client = MemoryClient(store)
    whole = client.call_timeline(f"call:{b}")            # any row of the session opens all of it
    assert [bl["call_id"] for bl in whole["blocks"]] == [a, b] and whole["words_kept_until"]
    part = client.call_timeline(a, since=T0 + 1850)
    assert [bl["call_id"] for bl in part["blocks"]] == [b]
    assert client.call_timeline("call:999") is None and client.call_id_of("[call:16-21]") == 16


def test_call_transcript_pages_and_marks_loops(store, tmp_path):
    from yuki.memory.api import MemoryClient

    a, b, _ = _day_of_calls(store, tmp_path)
    client = MemoryClient(store)
    t = client.call_transcript(a, per_page=10)
    assert t["total"] == 9 and t["loops_left_out"] == 1 and t["pages"] == 1
    looped = next(ln for ln in t["lines"] if ln["text"] == "second part 3")
    assert looped["repeats"] == 1
    p2 = client.call_transcript(a, page=2, per_page=10)
    assert p2["page"] == 1                              # clamped to the last page
    many = client.call_transcript(a, per_page=10, since=T0 + 1850)
    assert all(ln["call_id"] == b for ln in many["lines"])


def test_agent_tools_calls_and_call_timeline(store, tmp_path):
    from yuki.agent.memory import CALL_SIDE_NOTE, MemoryAccess

    a, b, c = _day_of_calls(store, tmp_path)
    from yuki.memory.api import MemoryClient

    access = MemoryAccess(lambda: MemoryClient(store))
    day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
    listed = access.run_tool("calls", {"since": day, "until": day})
    assert listed.ok and f"[call:{a}]" in listed.content[0]["text"] and CALL_SIDE_NOTE in listed.content[0]["text"]
    assert f"rows {a}-{b}" in listed.content[0]["text"] and "gist:" in listed.content[0]["text"]
    tl = access.run_tool("call_timeline", {"call": f"call:{a}"})
    text = tl.content[0]["text"]
    assert tl.ok and "Started call" in text and "[joke]" in text and CALL_SIDE_NOTE in text
    assert text.index(f"talk {a}") < text.index(f"talk {b}")
    words = access.run_tool("call_timeline", {"call": str(a), "words": True, "page": 1})
    assert words.ok and "first part 0" in words.content[0]["text"] and "repeated 1x" in words.content[0]["text"]
    bad = access.run_tool("call_timeline", {"call": "call:12345"})
    assert not bad.ok
    assert not access.run_tool("call_timeline", {}).ok


def test_timeline_text_is_capped_with_a_pointer_to_the_rest():
    from yuki.agent.memory import TIMELINE_MAX_CHARS, format_call_timeline

    start = datetime(2026, 9, 25, 13, 0)
    blocks = [{"call_id": 1, "start": (start + timedelta(minutes=5 * i)).isoformat(),
               "end": (start + timedelta(minutes=5 * i + 4)).isoformat(), "topic": f"topic {i}",
               "lines": [{"at": (start + timedelta(minutes=5 * i)).isoformat(), "text": "z" * 200, "nature": "fact"}]}
              for i in range(600)]
    text = format_call_timeline({"ref": "call:1", "call_ids": [1], "app": "Discord", "start": start.isoformat(),
                                 "end": (start + timedelta(hours=25)).isoformat(), "seconds": 90000,
                                 "recorded": True, "speech_s": 4000, "blocks": blocks, "rows": [], "gists": [],
                                 "words_kept_until": None})
    assert len(text) < TIMELINE_MAX_CHARS + 2_000 and "call_timeline again with since=" in text and "gone" in text


def test_recall_points_at_a_call_with_a_timeline():
    from yuki.agent.memory import format_recall

    text = format_recall([{"kind": "fact", "at": "2026-09-25T21:40:00", "app": "Discord", "fact": "Said the build failed",
                           "nature": "fact", "call": "call:20"}], query="discord call")
    assert "call:20" in text and "call_timeline" in text


def test_helpers_and_fences_cover_the_call_tools():
    from yuki.agent.subagents import helper_tools
    from yuki.agent.tools import MEMORY_TOOLS, PERCEPTION_TOOLS
    from yuki.agent.untrusted import EXTERNAL_TEXT_TOOLS

    names = {t["name"] for t in helper_tools(True)}
    # revamp D: the call tools are kinds of the one memory tool, which helpers, fences and stubbing all cover
    from yuki.agent.tools import TOOLS_BY_NAME

    assert "memory" in names and "memory" in MEMORY_TOOLS and "memory" in EXTERNAL_TEXT_TOOLS
    assert "memory" in PERCEPTION_TOOLS
    kinds = TOOLS_BY_NAME["memory"]["input_schema"]["properties"]["kind"]["enum"]
    for tool in ("calls", "call_timeline"):
        assert tool in kinds and tool in EXTERNAL_TEXT_TOOLS


def test_prompt_and_mouth_route_call_questions_to_the_timeline():
    from yuki.agent.prompt import SYSTEM_PROMPT
    from yuki.voice.mouth import __dict__ as mouth

    # both sides are kept since 3 Oct 2026 (report 61c): the lines say who spoke, never "their side alone"
    assert "call_timeline" in SYSTEM_PROMPT and "each line says who spoke" in SYSTEM_PROMPT
    assert "their side alone" not in SYSTEM_PROMPT and "only their microphone" not in SYSTEM_PROMPT
    assert any("what they said on a call or in a meeting" in v for v in mouth.values() if isinstance(v, str))


# ---------------------------------------------------------------------------
# round 2: speech said to Yuki, stretches, meetings without a call row, the MCP tools
# ---------------------------------------------------------------------------


def test_speech_to_yuki_is_found_by_voice_turn_times_not_words(store, tmp_path):
    from yuki.memory.calldigest import said_to_yuki_spans, voice_session_start

    utts = [_utt(T0 + 60 * i, f"line {i}") for i in range(8)]          # 13:20, 13:21, ... 13:27
    cid = _call(store, T0, T0 + 3600, utts)
    session = datetime.fromtimestamp(T0 + 20).strftime("%Y%m%d-%H%M%S")
    # voice turns logged when they close, a few seconds after the words they answered
    store.add_turn("s", f"voice-{session}-ab12-1", T0 + 60 + 8, "open youtube", "ok", [], "voice", addressed="yuki")
    store.add_turn("s", f"voice-{session}-ab12-2", T0 + 240 + 6, "what", "hm", [], "voice")        # addressee unknown
    store.add_turn("s", f"voice-{session}-ab12-3", T0 + 360 + 5, "bro lol", None, [], "voice", addressed="others")
    store.add_turn("s", "worker-3", T0 + 420 + 5, "typed", "ok", [], "done")                        # not a voice turn
    store.flush_turns()
    assert voice_session_start(f"voice-{session}-ab12-1") == pytest.approx(T0 + 20, abs=1)
    assert voice_session_start("worker-3") is None
    spans = said_to_yuki_spans(store.voice_turns(T0 - 60, T0 + 3600))
    assert len(spans) == 3 and sum(1 for *_, sure in spans if sure) == 1
    stub = StubClient()
    CallDigestWorker(store, client=stub, log_dir=tmp_path).digest()
    text = stub.requests[0]["messages"][0]["content"]
    assert "line 1" not in text                                   # said to Yuki: left out
    assert "YUKI LISTENING) line 0" in text                       # the wake, addressee unknown: the model judges
    assert "YUKI LISTENING) line 4" in text                       # a turn from before voice judged addressees
    assert "(en) line 6" in text and "(en) line 7" in text        # said to the call; a typed turn changes nothing
    from yuki.memory.api import MemoryClient

    lines = {ln["text"]: ln["to_yuki"] for ln in MemoryClient(store).call_transcript(cid)["lines"]}
    assert lines["line 1"] == "yes" and lines["line 4"] == "maybe" and lines["line 6"] == ""


def test_stretches_group_by_quiet_gaps_and_cut_long_ones():
    from yuki.memory.calldigest import stretches

    def b(start_min, end_min):
        return {"start": T0 + 60 * start_min, "end": T0 + 60 * end_min, "topic": f"t{start_min}"}

    blocks = [b(0, 10), b(12, 20), b(60, 70), b(71, 80), *[b(100 + 10 * i, 108 + 10 * i) for i in range(12)]]
    groups = stretches(blocks)
    assert [len(g) for g in groups][:2] == [2, 2]                 # a 40-minute silence splits
    assert all(g[-1]["end"] - g[0]["start"] <= 90 * 60 for g in groups)
    assert [x for g in groups for x in g] == blocks


def test_timeline_result_is_in_answer_shape(store, tmp_path):
    from yuki.agent.memory import format_call_timeline
    from yuki.memory.api import MemoryClient

    a, b, _ = _day_of_calls(store, tmp_path)
    t = MemoryClient(store).call_timeline(a)
    # 13:20-13:24 and 13:51-13:53: 27 quiet minutes apart, so two stretches
    assert [g["topics"] for g in t["stretches"]] == [[f"talk {a}"], [f"talk {b}"]]
    text = format_call_timeline(t)
    assert "Timeline in 2 stretches" in text and f"13:20-13:24: talk {a}" in text
    assert f"\n  13:20 talk {a}\n      13:20 Started call" in text and "(nothing said 13:24-13:51)" in text


def test_calls_lists_a_meeting_the_timeline_saw_without_a_call_row(store, tmp_path):
    from yuki.memory.api import MemoryClient
    from yuki.memory.store import TimelineRow
    from yuki.agent.memory import format_calls

    a, b, _ = _day_of_calls(store, tmp_path)
    for start, end, meeting in ((T0 + 30000, T0 + 32000, "Zoom"), (T0 + 100, T0 + 900, "Discord")):
        store.save_timeline(TimelineRow(id=None, started_at=start, ended_at=end, state="present",
                                        process=meeting.lower(), app=meeting, active_s=end - start, meeting=meeting))
    day = datetime.fromtimestamp(T0).replace(hour=0, minute=0)
    calls = MemoryClient(store).calls_in(day, day + timedelta(days=1))
    extra = [c for c in calls if c["ref"] is None]
    assert [c["service"] for c in extra] == ["Zoom"]              # the Discord meeting row is the call itself
    assert extra[0]["transcript"] is False and extra[0]["speech_s"] == 0
    assert "[meeting, no transcript] Zoom" in format_calls(calls)


def test_mcp_offers_read_only_call_tools(store, tmp_path):
    import asyncio

    from yuki.mcp.server import build_server

    a, _, _ = _day_of_calls(store, tmp_path)
    server, _memory = build_server(store.path, tmp_path / "mcplogs")

    async def go():
        names = [t.name for t in await server.list_tools()]
        day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
        listed = await server.call_tool("get_calls", {"since": day, "until": day})
        timeline = await server.call_tool("get_call_timeline", {"call": f"call:{a}"})
        words = await server.call_tool("get_call_timeline", {"call": str(a), "words": True})
        return names, str(listed), str(timeline), str(words)

    names, listed, timeline, words = asyncio.run(go())
    assert "get_calls" in names and "get_call_timeline" in names
    assert f"[call:{a}]" in listed and "Started call" in timeline and "first part 0" in words
