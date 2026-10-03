"""Calls on both sides, the memory side (report 61): every reader takes ``Store.call_lines``, every prompt line says
who spoke, and a commitment or a question is only ever the user's own words.

Offline: temp databases, stub model clients, invented people (Mira, Kabir, Tara). Nothing here reads the real
database, the microphone or the desktop.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory.store import ModelCall, Store

T0 = datetime(2026, 10, 3, 21, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class StubEmbedder:
    model_name = "stub"

    def embed(self, texts):
        return [np.ones(8, dtype=np.float32) for _ in texts]


class ToolClient:
    """``messages.create`` answering the one tool of each request with a queued input."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = answers
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        self.requests.append(request)
        name = request["tools"][0]["name"]
        data = self.answers[name].pop(0) if len(self.answers[name]) > 1 else self.answers[name][0]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))


def line(at: float, text: str, who: str = "user", app: str = "Discord", kind: str = "call", **kw) -> dict:
    return {"at": at, "end": at + 3.0, "text": text, "lang": "en", "who": who, "app": app, "track_kind": kind, **kw}


#: A Discord call while the user plays VALORANT: a friend's plan, a friend's request the user agrees to, game
#: callouts and the user's reply to them (invented).
SCENE = [
    line(T0 + 10, "I'll send you the photos from the trek tomorrow", "others"),
    line(T0 + 20, "can you send me the link to the hostel?", "others"),
    line(T0 + 24, "yeah sure, after dinner", heard_by=["Discord"]),
    line(T0 + 40, "two on A, rotate!", "others", "VALORANT", "game"),
    line(T0 + 43, "rotating, watch heaven", heard_by=["VALORANT", "Discord"]),
    line(T0 + 60, "Spike planted", "others", "VALORANT", "game"),
]


def call_with(store: Store, lines: list[dict], *, kind: str = "call", app: str = "Discord",
              start: float = T0) -> tuple[int, int]:
    cid = store.start_call(app=app, process=f"{app.lower()}.exe", via="mic" if kind == "call" else "game",
                           at=start, status="recording", consent="always", consent_by="pref", kind=kind)
    store.update_call(cid, status="ended", ended_at=start + 600, stop_reason="mic_released",
                      recording_started_at=start)
    cap = store.add_call_side(cid, app=app, process=f"{app.lower()}.exe", started_at=start, ended_at=start + 600,
                              utterances=[u for u in lines if u["who"] == "user"], lines=lines, final=True, kind=kind)
    return cid, cap


# -- the journal: every side read and labelled ----------------------------------------------------------------


def test_the_journal_reads_every_side_through_call_lines_and_labels_each(store):
    from yuki.memory.journal import build_sources, build_user_message, call_utterances

    cid, _cap = call_with(store, SCENE)
    [pending] = store.pending_captures()
    app, lines = call_utterances(pending)
    assert app == "Discord" and [u["who"] for u in lines] == ["others", "others", "user", "others", "user", "others"]
    sources = build_sources([pending])
    assert [s.users_line for s in sources] == [False, False, True, False, True, False]
    thread = store.thread_info(pending.thread_id)
    text = build_user_message(thread, sources, [], "NONCE")
    assert "[1] OTHERS SAID" in text and "the call app's own sound" in text
    assert "[3] THE USER SAID" in text and "mic open to: Discord," in text
    assert "[4] GAME VOICE SAID" in text and "VALORANT's voice chat" in text
    assert "mic open to: VALORANT, Discord" in text
    assert "the call app's own sound (the other people), a game's own voice chat; each line says who spoke" in text


def test_a_capture_from_before_both_sides_reads_as_the_users_own_lines(store):
    from yuki.memory.journal import build_sources

    cid = store.start_call(app="Discord", process="discord.exe", via="mic", at=T0, status="recording",
                           consent="yes", consent_by="card")
    store.add_call_side(cid, app="Discord", process="discord.exe", started_at=T0, ended_at=T0 + 60,
                        utterances=[{"at": T0 + 5, "end": T0 + 8, "text": "I'll push the fix tonight", "lang": "en"}],
                        final=True)
    sources = build_sources(store.pending_captures())
    assert [s.users_line for s in sources] == [True]


def test_a_commitment_citing_someone_elses_line_is_rejected_whatever_its_quote(store):
    from yuki.memory.journal import JournalWorker, build_sources

    call_with(store, SCENE)
    sources = build_sources(store.pending_captures())
    thread = SimpleNamespace(app="Discord")
    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", name="record_call_side", input={"facts": [], "commitments": [
            # the friend's own plan, its quote copied exactly from the friend's line: never the user's to-do
            {"text": "Send the trek photos tomorrow", "title": "", "quote": "I'll send you the photos",
             "due": "2026-10-04", "source": 1, "subject": "photos", "nature": "fact"},
            # the request the user agreed to: quoted from the user's own line
            {"text": "Send the hostel link that someone on the call asked for, after dinner", "title": "",
             "quote": "yeah sure, after dinner", "due": "2026-10-03", "source": 3, "subject": "hostel link",
             "nature": "fact"},
            # the user's words, but cited on the friend's request line: the quote is not in the cited line
            {"text": "Send the hostel link", "title": "", "quote": "yeah sure", "due": "", "source": 2,
             "subject": "hostel link", "nature": "fact"},
        ]})])
    worker = JournalWorker.__new__(JournalWorker)
    kept, rejected = worker._parse_commitments(response, thread, sources, "record_call_side")
    assert [c.quote for c in kept] == ["yeah sure, after dinner"]
    assert rejected == ["cites someone else's line, not the user's words"] * 2


def test_a_call_batch_keeps_the_users_commitment_drops_a_friends_and_asks_only_about_the_users_names(store, tmp_path):
    from yuki.memory.journal import JournalWorker
    from yuki.memory.provenance import describe

    call_with(store, SCENE)
    answer = {
        "facts": [
            {"text": "Someone on the Discord call said they would send the user the trek photos the next day.",
             "nature": "fact", "importance": 5, "source": 1},
            {"text": "In a Discord call, the user agreed to send the hostel link after dinner.", "nature": "fact",
             "importance": 7, "source": 3},
        ],
        "commitments": [
            {"text": "Send the trek photos", "title": "", "quote": "I'll send you the photos from the trek tomorrow",
             "due": "2026-10-04", "source": 1, "subject": "photos", "nature": "fact"},
            {"text": "Send the hostel link that someone on the call asked for, after dinner", "title": "",
             "quote": "yeah sure, after dinner", "due": "2026-10-03", "source": 3, "subject": "hostel link",
             "nature": "fact"},
        ],
        "questions": [
            {"subject": "Spike", "question": "Who is Spike to the user?", "kind": "person", "importance": 5,
             "source": 6, "options": [], "why": "named on the call"},
        ],
        "work": [],
    }
    client = ToolClient({"record_call_side": [answer]})
    worker = JournalWorker(store, client=client, embedder=StubEmbedder(), log_dir=tmp_path)
    worker.call_context = False
    [result] = worker.run_once()
    assert result.ok
    todos = [f for f in store.conversation_facts(("commitment",)) if f.origin == "call"]
    assert [f.quote for f in todos] == ["yeah sure, after dinner"] and todos[0].owner == "user"
    assert not store.questions()                                   # a name only the game said is never asked about
    log = [json.loads(x) for x in (tmp_path).glob("journal-*.jsonl").__next__().read_text("utf-8").splitlines()]
    call_log = next(x for x in log if x["type"] == "journal_call")
    assert call_log["commitments_rejected"] == ["cites someone else's line, not the user's words"]
    assert call_log["users_lines"] == 2 and call_log["utterances"] == 6
    sources = {j.fact: j.source for j in store.journal_between()}
    friend = sources["Someone on the Discord call said they would send the user the trek photos the next day."]
    mine = sources["In a Discord call, the user agreed to send the hostel link after dinner."]
    assert friend["who"] == "others" and friend["by"] == "someone on the call" and friend["kind"] == "call"
    assert "trek tomorrow" in friend["quote"] and "hostel" not in friend["quote"]   # never another speaker's words
    assert mine["by"] == "the user" and "who" not in mine
    assert "someone on the call said, not the user" in describe(friend)
    assert "the user's own words" in describe(mine)
    system = client.requests[0]["system"][0]["text"]
    assert "WHO SPOKE ON A CALL" in system and "ATTRIBUTION IS ABSOLUTE" in system and "GAMES." in system


def test_the_call_guidance_is_only_in_call_batches():
    from yuki.memory.journal import system_prompt

    call = system_prompt(["Ana"], questions=False, call_side=True)
    screen = system_prompt(["Ana"])
    assert "WHO SPOKE ON A CALL" in call and "WHO SPOKE ON A CALL. Every" not in screen
    assert "only the user's own microphone" not in call.lower() and "nothing they said is given" not in call


# -- the digest: every side, labelled, loops per track, the user's speech -------------------------------------------


def test_prepare_reads_every_side_marks_loops_per_track_and_yuki_only_on_the_users_lines():
    from yuki.memory.calldigest import line_label, prepare, speech_seconds

    lines = [
        line(T0 + 0, "kar do"), line(T0 + 5, "kar do", "others"),            # the same words on two tracks: no loop
        line(T0 + 9, "kar do"),                                               # the user's loop
        line(T0 + 30, "yuki what time is it"), line(T0 + 31, "it's late bro", "others"),
        line(T0 + 50, "Spike planted", "others", "VALORANT", "game", heard_by=None),
    ]
    caps = [{"id": 1, "data": {"app": "Discord", "kind": "call", "utterances": [u for u in lines if u["who"] == "user"],
                               "lines": lines}}]
    utts = prepare(caps, [(T0 + 28, T0 + 35, True)])
    assert [u.who for u in utts] == ["user", "others", "user", "user", "others", "others"]
    assert [u.repeat for u in utts] == [False, False, True, False, False, False]
    assert [u.to_yuki for u in utts] == ["", "", "", "yes", "", ""]       # Yuki hears only the user's microphone
    assert [line_label(u) for u in utts][-2:] == ["OTHERS on Discord", "GAME on VALORANT"]
    assert speech_seconds(utts) == 9.0                                       # the user's three lines only


def test_render_request_labels_who_spoke_and_the_mic_and_parse_blocks_keeps_it():
    from yuki.memory.calldigest import Piece, parse_blocks, prepare, render_request

    caps = [{"id": 1, "data": {"app": "Discord", "lines": SCENE}}]
    utts = prepare(caps)
    text, numbered = render_request([Piece(7, "Discord", 1, 1, utts)], {}, "NONCE")
    assert "[1] 21:00 OTHERS on Discord (en) I'll send you the photos" in text
    assert "[3] 21:00 USER (en, mic open to: Discord) yeah sure" in text
    assert "[4] 21:00 GAME on VALORANT (en) two on A" in text
    assert "mic open to: VALORANT, Discord" in text and "every side labelled with who spoke" in text
    blocks, gists = parse_blocks({"blocks": [{"first": 1, "last": 6, "topic": "hostel and a match", "lines": [
        {"at": 1, "text": "Someone on the call said they would send the trek photos the next day", "nature": "fact"},
        {"at": 3, "text": "Agreed to send the hostel link a friend asked for, after dinner", "nature": "fact"},
        {"at": 4, "text": "A teammate called two on A", "nature": "fact"}]}],
        "calls": [{"call": 7, "gist": "Hostel plans during a match."}]}, numbered)
    whos = [(ln["who"], ln["app"], ln["track_kind"]) for ln in blocks[0]["lines"]]
    assert whos == [("others", "Discord", "call"), ("user", "Discord", "call"), ("others", "VALORANT", "game")]
    assert gists == {7: "Hostel plans during a match."}
    game, _ = render_request([Piece(8, "VALORANT", 1, 1, utts[3:], kind="game")], {}, "N")
    assert "CALL 8 - VALORANT game voice chat (no call)" in game


def test_the_digest_worker_reads_both_sides_and_stores_whose_line_each_rests_on(store, tmp_path):
    from yuki.memory.calldigest import CallDigestWorker, system_prompt, yuki_spans_for

    cid, cap = call_with(store, SCENE)
    store.commit_call_batch(ModelCall(at=time.time(), thread_id=store.call_record(cid).thread_id, model="stub"),
                            [cap], [])
    assert yuki_spans_for(store, store.call_side_captures(cid)) == []       # others' lines present, no voice turns
    answer = {"blocks": [{"first": 1, "last": 6, "topic": "Hostel link", "lines": [
        {"at": 2, "text": "Someone on the call asked the user for the hostel link", "nature": "fact"},
        {"at": 3, "text": "Agreed to send it after dinner", "nature": "fact"}]}],
        "calls": [{"call": cid, "gist": "The user agreed to send a friend the hostel link.", "gist_shown": "",
                   "people": [], "where": "", "place": {"app": "", "kind": "", "channel": "", "server": ""}}]}
    client = ToolClient({"write_call_timeline": [answer]})
    summary = CallDigestWorker(store, client=client, log_dir=tmp_path, context=False).digest([cid], force=True)
    assert summary["ok"] == [cid] and summary["others"] == 4 and summary["speech_s"] == 6.0
    d = store.call_digest(cid)
    assert [(ln["who"], ln["text"][:9]) for ln in d["blocks"][0]["lines"]] == [("others", "Someone o"),
                                                                                ("user", "Agreed to")]
    assert d["speech_s"] == 6.0 and d["utterances"] == 6
    prompt = system_prompt(["Ana"])
    assert "ATTRIBUTION IS ABSOLUTE" in prompt and "GAME VOICE CHAT." in prompt and "{speakers}" not in prompt
    assert "Only the user's own microphone was ever recorded" not in prompt


def test_a_digest_line_rests_on_the_speakers_own_line_by_the_models_label():
    """The model said whose words a line reports; a line pinned to the line it answered moves to the speaker's own."""
    from yuki.memory.calldigest import Piece, parse_blocks, prepare, render_request

    utts = prepare([{"id": 1, "data": {"app": "Discord", "lines": SCENE}}])
    _text, numbered = render_request([Piece(7, "Discord", 1, 1, utts)], {}, "N")
    blocks, _ = parse_blocks({"blocks": [{"first": 1, "last": 6, "topic": "t", "lines": [
        {"at": 2, "text": "Agreed to send the hostel link a friend asked for", "nature": "fact", "speaker": "user"},
        {"at": 3, "text": "Someone on the call asked for the hostel link", "nature": "fact", "speaker": "others"},
        {"at": 4, "text": "Played a VALORANT match during the call", "nature": "fact", "speaker": "user"},
        {"at": 1, "text": "Someone on the call said they would send the photos", "nature": "fact",
         "speaker": "others"}]}], "calls": []}, numbered)
    got = [(numbered_at, ln["who"]) for ln in blocks[0]["lines"]
           for numbered_at in [next(n for n, (_c, u) in numbered.items() if u.at == ln["at"])]]
    # 2 -> 3 (the user's answer after the request), 3 -> 2 (the request before the answer), 4 -> 5 (the user's
    # callout after the teammate's), 1 stays (already the others' own line)
    assert sorted(got) == [(1, "others"), (2, "others"), (3, "user"), (5, "user")]


# -- the readers of digests: jokes, recap, events, related ----------------------------------------------------------


def test_a_joke_line_resting_on_someone_elses_words_never_ends_the_users_commitment(store):
    from yuki.memory.commitments import close_joke_commitments
    from yuki.memory.store import ConversationChange

    cid, _cap = call_with(store, SCENE)
    at = T0 + 24

    def digest(who: str) -> None:
        store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist="g", started_at=T0,
                               ended_at=T0 + 60, speech_s=6.0, utterances=6, kept=6, model="m",
                               blocks=[{"start": T0, "end": T0 + 60, "topic": "t", "lines": [
                                   {"at": at, "text": "Joked about the hostel", "nature": "humor", "who": who,
                                    "app": "Discord", "track_kind": "call"}]}])

    fid = store.add_conversation_fact("commitment", "Send the hostel link", quote="yeah sure, after dinner",
                                      subject="call (Discord): hostel link", origin="call", owner="user", at=at)
    digest("others")                                       # a friend's joke at the same second: not the user's words
    assert close_joke_commitments(store) == []
    digest("user")                                         # the user's own line marked a joke: the to-do goes
    assert close_joke_commitments(store) == [{"item": fid, "call": cid}]


def test_speaker_marks_and_the_related_source_say_whose_words():
    from yuki.memory.presence import speaker_mark

    assert speaker_mark({"who": "others", "track_kind": "game"}) == " [game]"
    assert speaker_mark({"who": "others", "track_kind": "call"}) == " [others]"
    assert speaker_mark({"who": "user"}) == "" and speaker_mark({"text": "old digest line"}) == ""


def test_a_game_voice_session_gets_no_recap_card(store):
    from yuki.memory.presence import PresenceWorker

    start = time.time() - 900.0      # the recap scan reads digests written lately, by the clock
    cid, _cap = call_with(store, [line(start + 5, "nice one", app="VALORANT", kind="game"),
                                  line(start + 8, "Spike planted", "others", "VALORANT", "game")],
                          kind="game", app="VALORANT", start=start)
    store.update_call(cid, speech_s=600.0)
    store.save_call_digest(cid, app="VALORANT", capture_ids=[], status="ok", gist="A match.", started_at=start,
                           ended_at=start + 600, speech_s=600.0, utterances=2, kept=2, model="m",
                           blocks=[{"start": start, "end": start + 60, "topic": "match", "lines": [
                               {"at": start + 5, "text": "Cheered a teammate", "nature": "fact", "who": "user",
                                "app": "VALORANT", "track_kind": "game"}]}])
    worker = PresenceWorker.__new__(PresenceWorker)
    worker.store = store
    worker._marks = {}
    worker._seen = {}
    marked: list[str] = []
    worker._looked = lambda key, now, within: False
    worker._mark = lambda key, now: marked.append(key)
    assert worker._recap(time.time(), None, None, []) is None
    assert marked == [f"recap:call:{cid}"]
