"""Calls keep both sides (report 61c): the memory API's call reads and the agent's / MCP's call texts.

``MemoryClient.call_transcript`` pages every side's lines (``Store.call_lines``) labelled with who spoke, ``who``
filters them, old captures read as the user's, loops are judged per track and YUKI LISTENING only ever marks the
user's lines; ``calls_in`` / ``call_timeline`` report the session's kind, each side's speech and the apps whose own
sound was kept, and read a digest's per-line speaker fields generically; the agent's formatters and the MCP tools
mark whose words each line is and no text claims only the user's microphone is recorded.

Offline: temp databases, invented people, no model, no audio device.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store

T0 = datetime(2026, 10, 3, 21, 0).timestamp()
DAY = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _line(at: float, text: str, who: str = "user", app: str = "Discord", kind: str = "call", **kw) -> dict:
    return {"at": at, "end": at + 3.0, "text": text, "lang": "en", "who": who, "app": app, "track_kind": kind, **kw}


def _row(store: Store, app: str, start: float, end: float, *, kind: str = "call", mic: bool = True) -> int:
    cid = store.start_call(app=app, process=f"{app}.exe", via="game" if kind == "game" else "mic", at=start,
                           status="recording", consent="always", consent_by="pref", kind=kind)
    store.update_call(cid, status="ended", ended_at=end, stop_reason="call_ended",
                      recording_started_at=start if mic else None)
    return cid


def _both_sides_call(store: Store) -> int:
    """A Discord call with a VALORANT match inside it: the user, two friends on Discord (one track), teammates."""
    cid = _row(store, "Discord", T0, T0 + 900)
    lines = [
        _line(T0 + 10, "ok I'm in, give me a sec", heard_by=["Discord"]),
        _line(T0 + 14, "Kenji says send the deck tonight", who="others"),
        _line(T0 + 20, "yeah I'll send it after the match", heard_by=["Discord"]),
        _line(T0 + 30, "rotate B, they're stacking A", who="others", app="VALORANT", kind="game"),
        _line(T0 + 33, "on it, going B", heard_by=["Discord", "VALORANT"]),
        _line(T0 + 40, "nice one Mira", who="others"),
    ]
    store.add_call_side(cid, app="Discord", process="Discord.exe", started_at=T0 + 10, ended_at=T0 + 43,
                        utterances=[ln for ln in lines if ln["who"] == "user"], lines=lines, final=True)
    return cid


def _spool(store: Store, cid: int, seq: int, speech: float, side: str = "user", app: str | None = None,
           process: str | None = None, kind: str = "call", track_call_id: int | None = None) -> None:
    store.write_spool(cid, seq, start_at=T0 + seq, audio_s=speech + 1.0, speech_s=speech, payload=b"x" * 16,
                      codec="pcm16-zlib", side=side, track_app=app, track_process=process, track_kind=kind,
                      track_call_id=track_call_id)


# ---------------------------------------------------------------------------
# call_transcript
# ---------------------------------------------------------------------------


def test_call_transcript_labels_every_side_and_old_data_reads_as_the_users(store):
    cid = _both_sides_call(store)
    t = MemoryClient(store).call_transcript(cid)
    got = [(ln["who"], ln["app"], ln["track_kind"], ln["text"]) for ln in t["lines"]]
    assert got == [("user", "Discord", "call", "ok I'm in, give me a sec"),
                   ("others", "Discord", "call", "Kenji says send the deck tonight"),
                   ("user", "Discord", "call", "yeah I'll send it after the match"),
                   ("others", "VALORANT", "game", "rotate B, they're stacking A"),
                   ("user", "Discord", "call", "on it, going B"),
                   ("others", "Discord", "call", "nice one Mira")]
    assert t["lines"][4]["heard_by"] == ["Discord", "VALORANT"]          # who the mic was open to then
    assert all("heard_by" not in ln for ln in t["lines"] if ln["who"] == "others")
    assert t["sides"] == {"user": 3, "others": 3} and t["others_recorded"] and t["who"] == "all"
    assert t["kind"] == "call" and t["total"] == 6

    # a capture from before 3 Oct 2026: utterances only, no who - the user's own, on the capture's app
    old = _row(store, "Discord", T0 + 5000, T0 + 5600)
    store.add_call_side(old, app="Discord", process="Discord.exe", started_at=T0 + 5010, ended_at=T0 + 5100,
                        utterances=[{"at": T0 + 5010, "end": T0 + 5013, "text": "anyone up for a match", "lang": "en"},
                                    {"at": T0 + 5090, "end": T0 + 5093, "text": "ok later then", "lang": "en"}])
    o = MemoryClient(store).call_transcript(old)
    assert [(ln["who"], ln["app"], ln["track_kind"]) for ln in o["lines"]] == [("user", "Discord", "call")] * 2
    assert all("heard_by" not in ln for ln in o["lines"])
    assert o["others_recorded"] is False and o["sides"] == {"user": 2, "others": 0}


def test_call_transcript_who_keeps_one_side_counts_both_and_pages(store):
    cid = _both_sides_call(store)
    client = MemoryClient(store)
    mine = client.call_transcript(cid, who="user")
    theirs = client.call_transcript(cid, who="others")
    assert {ln["who"] for ln in mine["lines"]} == {"user"} and mine["total"] == 3
    assert {ln["who"] for ln in theirs["lines"]} == {"others"} and theirs["total"] == 3
    assert mine["sides"] == theirs["sides"] == {"user": 3, "others": 3}       # both counted whatever is kept
    assert client.call_transcript(cid, who="ALL")["total"] == 6
    with pytest.raises(ValueError):
        client.call_transcript(cid, who="friends")
    # paging and the window are as before
    many = _row(store, "Discord", T0 + 9000, T0 + 9900)
    lines = [_line(T0 + 9000 + 5 * i, f"line {i}", who="others" if i % 2 else "user") for i in range(25)]
    store.add_call_side(many, app="Discord", process="Discord.exe", started_at=T0 + 9000, ended_at=T0 + 9130,
                        utterances=[ln for ln in lines if ln["who"] == "user"], lines=lines, final=True)
    p2 = client.call_transcript(many, page=2, per_page=10)
    assert p2["pages"] == 3 and p2["page"] == 2 and [ln["text"] for ln in p2["lines"]][0] == "line 10"
    late = client.call_transcript(many, since=T0 + 9100, who="others")          # lines 20-24; the others' odd
    assert [ln["text"] for ln in late["lines"]] == ["line 21", "line 23"] and late["sides"] == {"user": 3, "others": 2}


def test_loops_are_judged_per_track_not_across_speakers(store):
    cid = _row(store, "Discord", T0, T0 + 600)
    lines = [_line(T0 + 10, "let's go"), _line(T0 + 12, "let's go", who="others"),     # two people, no loop
             _line(T0 + 20, "kar do", who="others"), _line(T0 + 24, "kar do", who="others"),  # a loop on Discord
             _line(T0 + 30, "kar do")]                                                   # the user's own, kept
    store.add_call_side(cid, app="Discord", process="Discord.exe", started_at=T0 + 10, ended_at=T0 + 33,
                        utterances=[ln for ln in lines if ln["who"] == "user"], lines=lines, final=True)
    t = MemoryClient(store).call_transcript(cid)
    assert [(ln["who"], ln["text"], ln["repeats"]) for ln in t["lines"]] == [
        ("user", "let's go", 0), ("others", "let's go", 0), ("others", "kar do", 1), ("user", "kar do", 0)]
    assert t["loops_left_out"] == 1


def test_yuki_listening_marks_only_the_users_lines(store):
    cid = _row(store, "Discord", T0, T0 + 600)
    lines = [_line(T0 + 60, "yuki open the deck"), _line(T0 + 61, "what deck?", who="others"),
             _line(T0 + 240, "hmm"), _line(T0 + 242, "you there?", who="others"),
             _line(T0 + 400, "ok bye", heard_by=["Discord"])]
    store.add_call_side(cid, app="Discord", process="Discord.exe", started_at=T0 + 60, ended_at=T0 + 403,
                        utterances=[ln for ln in lines if ln["who"] == "user"], lines=lines, final=True)
    store.add_turn("s", "worker-1", T0 + 68, "yuki open the deck", "ok", [], "voice", addressed="yuki")
    store.add_turn("s", "worker-2", T0 + 246, "hmm", "hm", [], "voice")            # addressee unknown
    store.flush_turns()
    marks = {ln["text"]: ln["to_yuki"] for ln in MemoryClient(store).call_transcript(cid)["lines"]}
    assert marks == {"yuki open the deck": "yes", "what deck?": "", "hmm": "maybe", "you there?": "", "ok bye": ""}


# ---------------------------------------------------------------------------
# calls_in / call_timeline
# ---------------------------------------------------------------------------


def test_calls_in_reports_kind_each_sides_speech_and_the_tracks_kept(store):
    call = _row(store, "Discord", T0, T0 + 900)
    _spool(store, call, 1, 40.0)                                                       # the user's microphone
    _spool(store, call, 2, 70.0, side="others", app="Discord", process="discord")      # the friends
    _spool(store, call, 3, 15.0, side="others", app="VALORANT", process="valorant-win64-shipping", kind="game")
    game = _row(store, "VALORANT", T0 + 1200, T0 + 2400, kind="game")                  # a match after the call
    _spool(store, game, 1, 20.0, kind="game")
    _spool(store, game, 2, 35.0, side="others", app="VALORANT", process="valorant-win64-shipping", kind="game")
    old = _row(store, "Discord", T0 + 4000, T0 + 4600)                                 # before 3 Oct: the mic only
    _spool(store, old, 1, 12.0)
    quiet = _row(store, "Discord", T0 + 6000, T0 + 6600, mic=False)                    # no headphones: no mic
    _spool(store, quiet, 1, 9.0, side="others", app="Discord", process="discord")
    day = datetime.fromtimestamp(T0).replace(hour=0, minute=0)
    by_id = {c["call_ids"][0]: c for c in MemoryClient(store).calls_in(day, day + timedelta(days=1))}

    c = by_id[call]
    assert c["kind"] == "call" and c["recorded"] and c["user_recorded"] and c["others_recorded"]
    assert c["others_speech_s"] == 85.0
    assert c["tracks"] == [{"app": "Discord", "track_kind": "call", "speech_s": 70.0},
                           {"app": "VALORANT", "track_kind": "game", "speech_s": 15.0}]
    g = by_id[game]
    assert g["kind"] == "game" and g["others_recorded"] and g["tracks"] == [
        {"app": "VALORANT", "track_kind": "game", "speech_s": 35.0}]
    o = by_id[old]
    assert o["user_recorded"] and not o["others_recorded"] and o["tracks"] == [] and o["others_speech_s"] == 0.0
    q = by_id[quiet]
    assert q["recorded"] and not q["user_recorded"] and q["others_recorded"]
    assert q["rows"][0]["state"] == "audio waiting to be transcribed"                  # not "not recorded"


def test_call_timeline_reads_the_digests_speaker_fields_and_old_lines_as_the_users(store):
    cid = _both_sides_call(store)
    blocks = [{"call_id": cid, "start": T0 + 10, "end": T0 + 43, "topic": "deck and the match", "lines": [
        {"at": T0 + 10, "text": "Joined the call", "nature": "fact"},                                 # old shape
        {"at": T0 + 14, "text": "Kenji asked, through a friend, for the deck tonight", "nature": "fact",
         "who": "others", "app": "Discord", "track_kind": "call"},
        {"at": T0 + 20, "text": "Agreed to send the deck after the match", "nature": "fact", "who": "user",
         "app": "Discord", "track_kind": "call"},
        {"at": T0 + 30, "text": "A teammate called a rotate to B", "nature": "fact", "who": "others",
         "app": "VALORANT", "track_kind": "game", "evidence": "line 4"}]}]                      # a field to keep
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist="Deck and a match", blocks=blocks,
                           started_at=T0 + 10, ended_at=T0 + 43, speech_s=9.0, utterances=6, kept=6, model="stub")
    t = MemoryClient(store).call_timeline(cid)
    lines = t["blocks"][0]["lines"]
    assert [(ln["who"], ln["app"], ln["track_kind"]) for ln in lines] == [
        ("user", "Discord", "call"), ("others", "Discord", "call"), ("user", "Discord", "call"),
        ("others", "VALORANT", "game")]
    assert lines[3]["evidence"] == "line 4" and lines[0]["at"].startswith("2026-10-03T21:00:10")
    assert t["stretches"][0]["lines"] == lines
    # no chunk rows here, but the digest rests on the others' lines: the head never says only the user's side
    from yuki.agent.memory import format_call_timeline

    assert t["others_recorded"] and "both sides kept" in format_call_timeline(t).splitlines()[0]


# ---------------------------------------------------------------------------
# the agent's and the MCP's call texts
# ---------------------------------------------------------------------------


def test_the_agent_texts_mark_whose_words_each_line_is(store):
    from yuki.agent.memory import CALL_SIDE_NOTE, GAME_SIDE_NOTE, MemoryAccess

    cid = _both_sides_call(store)
    _spool(store, cid, 1, 6.0)
    _spool(store, cid, 2, 7.5, side="others", app="Discord", process="discord")
    _spool(store, cid, 3, 2.5, side="others", app="VALORANT", process="valorant", kind="game")
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist="Deck and a match", blocks=[
        {"call_id": cid, "start": T0 + 10, "end": T0 + 43, "topic": "deck", "lines": [
            {"at": T0 + 14, "text": "Asked for the deck tonight", "nature": "fact", "who": "others",
             "app": "Discord", "track_kind": "call"},
            {"at": T0 + 20, "text": "Agreed to send the deck after the match", "nature": "fact", "who": "user"},
            {"at": T0 + 30, "text": "Called a rotate to B", "nature": "fact", "who": "others", "app": "VALORANT",
             "track_kind": "game"}]}],
        started_at=T0 + 10, ended_at=T0 + 43, speech_s=6.0, utterances=6, kept=3, model="stub")
    access = MemoryAccess(lambda: MemoryClient(store))

    listed = access.run_tool("calls", {"since": DAY, "until": DAY}).content[0]["text"]
    assert "both sides kept: the user spoke about" in listed and "(Discord, VALORANT game)" in listed
    assert CALL_SIDE_NOTE in listed and GAME_SIDE_NOTE in listed

    timeline = access.run_tool("call_timeline", {"call": f"call:{cid}"}).content[0]["text"]
    assert "21:00 [others on Discord] Asked for the deck tonight" in timeline
    assert "21:00 Agreed to send the deck after the match" in timeline              # the user's: unmarked
    assert "21:00 [game on VALORANT] Called a rotate to B" in timeline
    assert CALL_SIDE_NOTE in timeline and GAME_SIDE_NOTE in timeline

    words = access.run_tool("call_timeline", {"call": str(cid), "words": True}).content[0]["text"]
    assert "21:00:10 [user; mic open to Discord] ok I'm in, give me a sec" in words
    assert "21:00:14 [others on Discord] Kenji says send the deck tonight" in words
    assert "21:00:30 [game on VALORANT] rotate B, they're stacking A" in words
    assert "21:00:33 [user; mic open to Discord, VALORANT] on it, going B" in words
    assert "(3 the user's, 3 the others')" in words and "Only the user's side was kept" not in words


def test_an_old_call_says_only_the_users_side_was_kept_and_no_game_note(store):
    from yuki.agent.memory import GAME_SIDE_NOTE, format_call_transcript, format_calls

    old = _row(store, "Discord", T0, T0 + 600)
    _spool(store, old, 1, 30.0)
    store.add_call_side(old, app="Discord", process="Discord.exe", started_at=T0 + 10, ended_at=T0 + 13,
                        utterances=[{"at": T0 + 10, "end": T0 + 13, "text": "see you at nine", "lang": "en"}])
    client = MemoryClient(store)
    day = datetime.fromtimestamp(T0).replace(hour=0, minute=0)
    listed = format_calls(client.calls_in(day, day + timedelta(days=1)))
    assert "; only the user's side was kept)" in listed and GAME_SIDE_NOTE not in listed      # the call's head
    words = format_call_transcript(client.call_transcript(old))
    assert "21:00:10 [user] see you at nine" in words and "Only the user's side was kept on this call." in words


def test_a_game_session_reads_as_the_games_voice_chat(store):
    from yuki.agent.memory import GAME_SIDE_NOTE, format_call_transcript, format_calls

    game = _row(store, "VALORANT", T0, T0 + 1800, kind="game")
    _spool(store, game, 1, 10.0, kind="game")
    _spool(store, game, 2, 20.0, side="others", app="VALORANT", process="valorant", kind="game")
    lines = [_line(T0 + 5, "plant A", app="VALORANT", kind="game", heard_by=["VALORANT"]),
             _line(T0 + 8, "defuse is mine", who="others", app="VALORANT", kind="game")]
    store.add_call_side(game, app="VALORANT", process="VALORANT.exe", started_at=T0 + 5, ended_at=T0 + 11,
                        utterances=lines[:1], lines=lines, final=True, kind="game")
    client = MemoryClient(store)
    day = datetime.fromtimestamp(T0).replace(hour=0, minute=0)
    listed = format_calls(client.calls_in(day, day + timedelta(days=1)))
    assert f"[call:{game}] VALORANT game voice chat" in listed and "in the game 30m" in listed
    assert GAME_SIDE_NOTE in listed
    words = format_call_transcript(client.call_transcript(game))
    assert "on the VALORANT game voice chat" in words and "[game on VALORANT] defuse is mine" in words
    assert "[user; mic open to VALORANT] plant A" in words and GAME_SIDE_NOTE in words


def test_mcp_call_timeline_returns_labelled_lines_and_filters_by_who(store, tmp_path):
    from yuki.mcp.server import build_server

    cid = _both_sides_call(store)
    _spool(store, cid, 1, 6.0)
    _spool(store, cid, 2, 7.5, side="others", app="Discord", process="discord")
    server, _memory = build_server(store.path, tmp_path / "mcplogs")

    def text(result) -> str:
        assert not getattr(result, "is_error", False), result
        return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", None) or [])

    async def go():
        tools = {t.name: t for t in await server.list_tools()}
        listed = await server.call_tool("get_calls", {"since": DAY, "until": DAY})
        words = await server.call_tool("get_call_timeline", {"call": f"call:{cid}", "words": True})
        theirs = await server.call_tool("get_call_timeline", {"call": str(cid), "words": True, "who": "others"})
        return tools, text(listed), text(words), text(theirs)

    tools, listed, words, theirs = asyncio.run(go())
    assert "both sides kept" in listed
    assert "[others on Discord] Kenji says send the deck tonight" in words
    assert "[user; mic open to Discord] ok I'm in, give me a sec" in words
    assert "Kenji says send the deck tonight" in theirs and "ok I'm in" not in theirs
    assert "Only the others' lines here" in theirs
    assert tools["get_call_timeline"].annotations.read_only_hint is True       # still read-only
    assert "who" in tools["get_call_timeline"].input_schema["properties"]


def test_no_agent_or_mcp_text_claims_only_the_users_microphone_is_recorded():
    from yuki.agent import memory as agent_memory
    from yuki.agent.prompt import SYSTEM_PROMPT
    from yuki.agent.tools import TOOLS_BY_NAME
    from yuki.agent.untrusted import source_for
    from yuki.mcp import server as mcp_server

    texts = {
        "core prompt": SYSTEM_PROMPT,
        "memory tool": TOOLS_BY_NAME["memory"]["description"],
        "memory tool fields": str(TOOLS_BY_NAME["memory"]["input_schema"]),
        "call note": agent_memory.CALL_SIDE_NOTE + " " + agent_memory.GAME_SIDE_NOTE,
        "fences": source_for("calls", {}) + " " + source_for("call_timeline", {}),
        "mcp": mcp_server.INSTRUCTIONS,
    }
    with open(mcp_server.__file__, encoding="utf-8") as f:
        texts["mcp tools"] = f.read()
    stale = ("only the user's own microphone", "their side alone", "only their microphone is recorded",
             "never anyone else", "nobody else's words", "from the user's side only", "whether their side was",
             "their side was not recorded", "the user's exact words")
    for name, text in texts.items():
        low = text.lower()
        for claim in stale:
            assert claim not in low, (name, claim)
    assert "who spoke" in texts["core prompt"] and "who spoke" in texts["memory tool"]
    assert "who spoke" in texts["mcp"] and "teammates" in texts["call note"]
