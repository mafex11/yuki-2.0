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
    assert "on the Discord call: the call app" not in text          # explained once in the header, not per line
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


def test_facts_on_someone_elses_line_never_describe_the_user_and_game_voice_is_never_a_fact_source(store):
    """Review 61 #2 and #4: the WHOSE WORDS check runs on call facts (``user_said``), and a fact citing a line
    that is not the user's is dropped when it is about the user themself (trait, story, humor), when it does
    not involve the user, or when the line is a game's voice chat - on the line's own metadata, never wording."""
    from yuki.memory.journal import JournalWorker, build_sources

    call_with(store, SCENE)
    sources = build_sources(store.pending_captures())
    thread = SimpleNamespace(id=1, app="Discord", host=None, scope="call:1", title="", url="")

    def fact(text, source, nature="fact", user_said=False, involves_user=True):
        return {"text": text, "nature": nature, "importance": 5, "source": source, "user_said": user_said,
                "involves_user": involves_user}

    facts = [
        fact("Someone on the Discord call said they would send the user the trek photos the next day.", 1),
        fact("The user is generous with photos.", 1, nature="trait"),                    # about the user, a friend's line
        fact("The user asked for the hostel link.", 2, user_said=True),                  # the user's words, a friend's line
        fact("A teammate called two enemies on A.", 4),                                  # game voice: context only
        fact("Someone on the call has a hostel booked.", 2, involves_user=False),        # not the user's business
        fact("The user played VALORANT during the Discord call.", 5, user_said=True),    # the user's own line
    ]
    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", name="record_call_side", input={"facts": facts, "commitments": []})])
    worker = JournalWorker.__new__(JournalWorker)
    dropped: list[int] = []
    kept = worker._parse_facts(response, thread, sources, "record_call_side", not_users_words=dropped)
    assert [f.fact for f in kept] == [facts[0]["text"], facts[5]["text"]]
    assert dropped == [1, 2, 4, 2]


def test_the_call_tool_asks_whose_words_and_whether_it_involves_the_user():
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL

    item = RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["facts"]["items"]
    assert {"user_said", "involves_user"} <= set(item["required"])
    assert item["properties"]["user_said"]["type"] == "boolean"


def test_no_todo_from_game_voice_chat(store):
    """Owner's decision 4 Oct 2026: a teammate's request the user agrees to is no to-do - a line recorded for a
    game's session with no call, or one only the game's voice chat was taking the microphone for."""
    from yuki.memory.journal import JournalWorker, build_sources, said_into_game

    scene = [
        line(T0 + 10, "add me after the match?", "others", "VALORANT", "game"),
        line(T0 + 12, "yeah I'll add you", heard_by=["VALORANT"]),                  # only the game heard it
        line(T0 + 20, "can you send me the deck?", "others"),
        line(T0 + 22, "yeah I'll send it tonight", heard_by=["VALORANT", "Discord"]),
    ]
    call_with(store, scene)
    sources = build_sources(store.pending_captures())
    assert [said_into_game(s, sources) for s in sources] == [False, True, False, False]
    game_only = SimpleNamespace(utterance={"who": "user", "track_kind": "game", "text": "x"})
    assert said_into_game(game_only, [])
    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
        type="tool_use", name="record_call_side", input={"facts": [], "commitments": [
            {"text": "Add the teammate after the match", "title": "", "quote": "yeah I'll add you", "due": "",
             "source": 2, "subject": "add", "nature": "fact"},
            {"text": "Send the deck tonight", "title": "", "quote": "yeah I'll send it tonight", "due": "",
             "source": 4, "subject": "deck", "nature": "fact"}]})])
    worker = JournalWorker.__new__(JournalWorker)
    kept, rejected = worker._parse_commitments(response, SimpleNamespace(app="Discord"), sources, "record_call_side")
    assert [c.quote for c in kept] == ["yeah I'll send it tonight"]
    assert rejected == ["said in a game's voice chat: no to-do"]


def test_a_friends_line_neither_starts_nor_ends_the_users_work_thread():
    from yuki.memory.workthreads import parse_work

    friend = SimpleNamespace(at=T0, capture=SimpleNamespace(by_yuki=False, trigger=None, at=T0),
                             utterance={"who": "others", "track_kind": "call", "text": "I finished the deploy"})
    mine = SimpleNamespace(at=T0 + 5, capture=SimpleNamespace(by_yuki=False, trigger=None, at=T0),
                           utterance={"who": "user", "track_kind": "call", "text": "I'll start the migration"})
    ops, rejected = parse_work([{"action": "starts", "thread": 0, "title": "Deploy", "sources": [1], "state": "done"}],
                               {}, [friend, mine], [], thread=SimpleNamespace(id=1, app="Discord", host=None))
    assert ops == [] and rejected == ["someone else's words on a call start or end no thread"]
    ops, rejected = parse_work([{"action": "starts", "thread": 0, "title": "Migration", "sources": [2],
                                 "state": "starting"}], {}, [friend, mine], [],
                               thread=SimpleNamespace(id=1, app="Discord", host=None))
    assert [o.title for o in ops] == ["Migration"] and not rejected

def test_a_long_call_is_cut_at_its_longest_silence_not_between_a_request_and_its_answer():
    from yuki.memory.journal import JournalWorker

    def lines(pause_at: int | None) -> list:
        out, t = [], T0
        for i in range(60):                               # steady talk, 2 s apart
            t += 40.0 if i == pause_at else 2.0
            u = line(t, f"talking about the trip plans part {i} " * 3, "others" if i % 2 else "user")
            out.append(SimpleNamespace(at=t, utterance=u, call_app="Discord"))
        return out

    worker = JournalWorker.__new__(JournalWorker)
    worker.batch_chars = 4000
    plain = worker._call_parts(lines(None))
    first_cut = len(plain[0])                             # where a part would end with no silence to prefer
    pause = first_cut - 5                                 # a 40 s pause a few lines before it
    parts = worker._call_parts(lines(pause))
    assert sum(len(p) for p in parts) == 60
    assert len(parts[0]) == pause                         # the cut moved back onto the pause
    assert parts[1][0].utterance["text"].startswith(f"talking about the trip plans part {pause} ")


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
    # a line resting on a game's voice chat is dropped: game talk is context only (review 61 #4)
    assert whos == [("others", "Discord", "call"), ("user", "Discord", "call")]
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



def test_a_digest_line_moves_by_time_and_side_never_back_onto_an_earlier_user_line():
    """Review 61 #3: game callouts between a friend's request and the user's answer never stop the move (by time,
    the wanted side only); a line the model says is the user's moves only onto the user's reply after it, never onto
    something the user said before; the model's label and the move are stored beside the transcript's ``who``."""
    from yuki.memory.calldigest import Piece, parse_blocks, prepare, render_request

    scene = [
        line(T0 + 5, "nice", heard_by=["Discord"]),                                   # 1 the user, before
        line(T0 + 10, "I'll send you the photos tomorrow", "others"),                 # 2 a friend's plan
        line(T0 + 20, "can you send me the hostel link?", "others"),                  # 3 a friend's request
        *[line(T0 + 21 + i, f"callout {i}", "others", "VALORANT", "game") for i in range(8)],   # 4-11 game
        line(T0 + 35, "yeah sure, after dinner", heard_by=["Discord"]),               # 12 the user's answer
    ]
    utts = prepare([{"id": 1, "data": {"app": "Discord", "lines": scene}}])
    _text, numbered = render_request([Piece(7, "Discord", 1, 1, utts)], {}, "N")
    num = {u.text: n for n, (_c, u) in numbered.items()}
    blocks, _ = parse_blocks({"blocks": [{"first": 1, "last": 12, "topic": "t", "lines": [
        {"at": num["can you send me the hostel link?"], "text": "Agreed to send the hostel link", "nature": "fact",
         "speaker": "user"},
        # mislabelled: a friend's plan said to be the user's; the next user line is the reply to the request, and
        # it must not be pulled back onto the user's earlier "nice"
        {"at": num["I'll send you the photos tomorrow"], "text": "Will send photos", "nature": "fact",
         "speaker": "user"}]}], "calls": []}, numbered)
    by_text = {ln["text"]: ln for ln in blocks[0]["lines"]}
    agreed = by_text["Agreed to send the hostel link"]
    assert agreed["who"] == "user" and agreed["at"] == T0 + 35 and agreed["moved"] is True
    assert agreed["speaker"] == "user"
    photos = by_text["Will send photos"]
    assert photos["at"] != T0 + 5                         # never back onto the user's earlier line


def test_a_moved_joke_line_never_ends_the_users_commitment(store):
    from yuki.memory.commitments import close_joke_commitments

    cid, _cap = call_with(store, SCENE)
    at = T0 + 24
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist="g", started_at=T0,
                           ended_at=T0 + 60, speech_s=6.0, utterances=6, kept=6, model="m",
                           blocks=[{"start": T0, "end": T0 + 60, "topic": "t", "lines": [
                               {"at": at, "text": "Joked about the hostel", "nature": "humor", "who": "user",
                                "app": "Discord", "track_kind": "call", "speaker": "user", "moved": True}]}])
    store.add_conversation_fact("commitment", "Send the hostel link", quote="yeah sure, after dinner",
                                subject="call (Discord): hostel link", origin="call", owner="user", at=at)
    assert close_joke_commitments(store) == []

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
    # kProactive (18 Oct): the recap card is the card judge's candidates.gen_recap now; a game-only session is skipped
    # each time (no mark is written), so only the outcome is checked.
    from yuki.memory.candidates import Context, Seen, gen_recap

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
    assert gen_recap(Context(store, None, time.time(), None, Seen(store))) == []
    # the same session as a call does get one: the game's own voice chat is what keeps it off
    store._write(lambda conn: conn.execute("UPDATE calls SET kind='call' WHERE id=?", (cid,)))
    [card] = gen_recap(Context(store, None, time.time(), None, Seen(store)))
    assert card.type == "recap" and card.key == f"recap:call:{cid}"
