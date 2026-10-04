"""vfix4 (4 Oct): the talker keeps the last hour across conversations, looks before it says it has nothing, and reads
and adds to the to-do list itself (yuki.voice.recall). Invented names and items throughout; no model call, no device.

The live case (voice log 4 Oct 09:40-09:48, content-free): conversation A, the user dictates a to-do list and asks for
it to be made; seven to-dos are added. A ends on its idle clock; the name wakes conversation B five minutes later;
"what were the to-dos I told you?" got "I don't have that on record" four times. The replay below is that exchange
told again on a real memory database in a temp folder.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yuki.voice import recall
from yuki.voice.recall import LIST_PLACE, QUICK_MEMORY, QuickMemory, fetch_recent, natural_when, recent_view

ITEMS = ["Buy printer ink", "Call the plumber about the kitchen leak", "Renew the gym pass",
         "Email Tara the slides", "Book the dentist for Friday", "Water the balcony plants", "Back up the laptop"]
DICTATED = ("okay so today I need to buy printer ink, call the plumber about the kitchen leak, renew the gym pass, "
            "email Tara the slides, book the dentist for Friday, water the balcony plants and back up the laptop")


def _turn(i: int, at: float, user: str, reply: str, session: str = "s1") -> dict[str, Any]:
    return {"id": i, "session_id": session, "request_id": i, "at": at, "user": user, "reply": reply,
            "actions": [], "outcome": "voice"}


# ---------------------------------------------------------------------------------------------------------------------
# the view of the last hour
# ---------------------------------------------------------------------------------------------------------------------


def test_the_view_holds_the_last_hours_exchanges_oldest_first_and_what_was_kept() -> None:
    now = datetime(2026, 10, 4, 9, 45, 47).timestamp()
    turns = [
        _turn(1, now - 5000, "what's the weather", "Sunny all day."),             # older than the hour: left out
        _turn(3, now - 200, "make a to do list of this", "On it."),
        _turn(2, now - 260, DICTATED, "Got all of that."),
        _turn(4, now - 190, "", "Added seven to-dos to your list."),               # a result said on its own
        _turn(5, now - 150, "so anyway he said", ""),                              # talk for others: no reply
        _turn(3, now - 200, "make a to do list of this", "On it.", session="s2"),  # the same row twice
    ]
    todos = [{"id": f"todo:{i}", "text": t, "source": "user", "status": "open",
              "since": datetime.fromtimestamp(now - 190).isoformat()} for i, t in enumerate(ITEMS)]
    todos.append({"id": "loop:9", "text": "Reply to the landlord", "source": "loop", "status": "open",
                  "since": datetime.fromtimestamp(now - 100).isoformat()})          # memory's own loop: not kept
    todos.append({"id": "todo:99", "text": "Old thing", "source": "user", "status": "open",
                  "since": datetime.fromtimestamp(now - 90000).isoformat()})        # added yesterday: not kept
    rules = [{"ref": "rule:1", "text": "Keep replies short.", "at": now - 300}]
    view = recent_view(turns, todos, rules, now=now)
    assert view is not None and view.startswith(recall.RECENT_LABEL) and view.endswith(recall.RECENT_END)
    assert "weather" not in view and "so anyway" not in view and "landlord" not in view and "Old thing" not in view
    assert view.index("Got all of that") < view.index("make a to do list") < view.index("Added seven")
    assert view.count("make a to do list") == 1
    assert "09:41 They: okay so today I need" in view and "\n  You: On it." in view and "09:42 You: Added" in view
    assert f"to-dos put on their list (7, {LIST_PLACE})" in view and all(item in view for item in ITEMS)
    assert "Rules they set: Keep replies short." in view
    assert len(view) <= recall.RECENT_CHARS


def test_the_view_is_capped_dropping_the_oldest_exchanges_whole_and_none_when_the_hour_is_empty() -> None:
    now = time.time()
    turns = [_turn(i, now - 3000 + i * 20, f"question number {i} " + "word " * 60, "answer " * 70) for i in range(40)]
    view = recent_view(turns, [], [], now=now)
    assert view is not None and len(view) <= recall.RECENT_CHARS
    assert "question number 39" in view and "question number 0 " not in view
    assert " …" in view   # long messages cut after whole words
    assert recent_view([_turn(1, now - 7200, "hi", "hey")], [], [], now=now) is None
    assert recent_view([], None, None, now=now) is None


def test_one_exchange_logged_by_the_voice_and_the_thinker_is_shown_once_and_promises_come_after_rules() -> None:
    now = time.time()
    turns = [_turn(1, now - 120, "Where is it?", "Let me check. It's on Home, under Your list, all seven of them."),
             _turn(2, now - 118, "where is it?", "It's on Home, under Your list, all seven of them.", session="w"),
             _turn(3, now - 60, "Where is it?", "Something else entirely.")]   # the same words, another answer
    todos = [{"text": "Email Tara the slides", "source": "user", "since": now - 100},
             {"text": "Yuki will stay quiet for an hour", "title": "Quiet for an hour", "source": "commitment",
              "since": now - 90},
             {"text": "Added after now", "source": "user", "since": now + 3600}]
    view = recent_view(turns, todos, [{"text": "No praise.", "at": now - 80}], now=now) or ""
    assert view.count("Where is it?") == 2 and "Let me check. It's on Home" in view
    assert "Something else entirely" in view and "Added after now" not in view
    assert view.index("Email Tara") < view.index("Rules they set: No praise.") < view.index(
        "Your promises to them: Quiet for an hour.")


def test_due_dates_are_said_naturally() -> None:
    assert natural_when("2026-10-05T17:00:00") == "Monday 5 October, 5 PM"
    assert natural_when("2026-10-05") == "Monday 5 October"
    assert natural_when("2026-10-05T09:30:00") == "Monday 5 October, 9:30 AM"
    assert natural_when("") == ""


# ---------------------------------------------------------------------------------------------------------------------
# a real memory database in a temp folder: the exact exchange, session A then session B
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def memory(tmp_path: Path):
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    client = MemoryClient.open(tmp_path / "memory.db")
    access = MemoryAccess(lambda: client, session_id="20261004-094000-aaaaaa")
    try:
        yield access, client
    finally:
        access.flush_turns(5.0)
        client.close()


def _session_a(client: Any, now: float, *, by_thinker: bool = True) -> None:
    """Conversation A as memory logged it: the dictation, the ask, and the hand-off's outcome."""
    sid = "20261004-062812-abcdef"
    client.log_turn(sid, "voice-a-1", now - 300, DICTATED, "Got all of that, sounds like a busy day.", [], "voice")
    client.log_turn(sid, "voice-a-2", now - 240, "make a to do list of this", "On it.", ["tell_yuki (by voice)"],
                    "voice")
    if by_thinker:
        for item in ITEMS:
            client.add_todo(item)
        client.log_turn(sid, 7, now - 238, "Make a to-do list of this", "Added 7 to-dos to your list.",
                        [f"todo add {i}" for i in range(7)], "final")
    client.store.flush_turns(5.0)


def test_the_replay_a_new_conversation_knows_the_to_dos_of_the_one_before(memory) -> None:
    from yuki.voice.mouth import fetch_memory, mouth_system_blocks

    access, client = memory
    now = time.time()
    _session_a(client, now)
    # conversation B starts (the name, five minutes later): the talker's layers are read as a conversation reads them
    mouth = fetch_memory(access)
    assert mouth.recent and "make a to do list of this" in mouth.recent and "Added 7 to-dos" in mouth.recent
    assert all(item in mouth.recent for item in ITEMS) and LIST_PLACE in mouth.recent
    assert mouth.facts()["recent_chars"] == len(mouth.recent)
    blocks = mouth_system_blocks(mouth)
    assert blocks[-1]["text"] == mouth.recent.strip() and "cache_control" not in blocks[-1]
    # "what were the to-dos I told you?": the talker's look at the list answers it in one turn
    report, facts = QuickMemory(access).run({"action": "todos"})
    assert facts == {"ok": True, "items": 7}
    assert report["list"].startswith(f"7 open, {LIST_PLACE}:") and all(f"- {item}" in report["list"] for item in ITEMS)
    assert "todo:" not in report["list"]   # never ids
    # "what did I tell you?": today's talk holds the dictation
    found, facts = QuickMemory(access).run({"action": "today"}, now=now + 5)
    assert facts["ok"] and "printer ink" in found["found"] and "today's chat" in found["how_to_handle_it"]


def test_the_replay_by_voice_adds_the_list_and_says_how_many_and_where(memory) -> None:
    from yuki.voice.mouth import fetch_memory

    access, client = memory
    now = time.time()
    _session_a(client, now, by_thinker=False)
    report, facts = QuickMemory(access).run({"action": "add_todos", "items": [
        {"text": t} for t in ITEMS[:6]] + [{"text": ITEMS[6], "due": "2026-10-09 18:00"}]})
    assert facts == {"ok": True, "items": 7, "asked": 7}
    assert report["added"] == 7 and report["where"] == LIST_PLACE and "Added 7" in report["how_to_handle_it"]
    rows = client.todos()
    assert sorted(r["text"] for r in rows if r["source"] == "user") == sorted(ITEMS)
    assert any(r["due"] for r in rows if r["text"] == ITEMS[6])
    assert all(item in (fetch_memory(access).recent or "") for item in ITEMS)   # the next conversation has them


def test_an_add_with_a_time_memory_cannot_read_is_kept_without_it_and_says_so(memory) -> None:
    access, client = memory
    report, facts = QuickMemory(access).run({"action": "add_todos", "items": [{"text": "Pay rent", "due": "someday"}]})
    assert facts["items"] == 1 and "without its time" in report["items"][0]
    assert [r["text"] for r in client.todos() if r["source"] == "user"] == ["Pay rent"]


def test_an_empty_list_and_an_empty_search_say_where_they_looked(memory) -> None:
    access, _client = memory
    report, facts = QuickMemory(access).run({"action": "todos"})
    assert facts == {"ok": True, "items": 0} and "empty" in report["how_to_handle_it"]
    report, facts = QuickMemory(access).run({"action": "search", "query": "the venue for the wedding"})
    assert report["found"] == "" and "a memory search" in report["how_to_handle_it"]
    assert "never that they did not tell you" in report["how_to_handle_it"]
    report, _facts = QuickMemory(access).run({"action": "add_todos", "items": []})
    assert "failed" in report and "Nothing was added" in report["how_to_handle_it"]


def test_the_view_spans_the_day_start(memory) -> None:
    """At 04:30 the last hour reaches into the user's day before (memory's days start at 04:00)."""
    access, client = memory
    now = datetime.now().replace(hour=4, minute=30, second=0, microsecond=0).timestamp()
    client.log_turn("s", 1, now - 2400, "remind me about the parcel", "Sure, noted.", [], "voice")
    client.log_turn("s", 2, now - 600, "and the bins", "Got it.", [], "voice")
    client.store.flush_turns(5.0)
    view = fetch_recent(access, now=now)
    assert view and "parcel" in view and "the bins" in view


def test_without_memory_or_when_it_fails_there_is_no_view() -> None:
    class Broken:
        def call(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    assert fetch_recent(None) is None and fetch_recent(object()) is None and fetch_recent(Broken()) is None


# ---------------------------------------------------------------------------------------------------------------------
# the tool in the conversation
# ---------------------------------------------------------------------------------------------------------------------


class _Params:
    def __init__(self, args: dict[str, Any]) -> None:
        self.arguments, self.tool_call_id, self.function_name = args, "t1", "quick_memory"
        self.results: list[Any] = []

    async def result_callback(self, payload: Any, *, properties: Any = None) -> None:
        self.results.append(payload)


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **facts: Any) -> None:
        self.events.append((kind, facts))


class _Rows:
    """MemoryAccess as the tool calls it, holding a list."""

    def __init__(self) -> None:
        self.rows = [{"id": "todo:1", "text": "Email Tara the slides about the launch plan", "title": "Email Tara slides",
                      "source": "user", "status": "open", "due": "2026-10-06"},
                     {"id": "commit:2", "text": "Send the photos", "source": "commitment", "status": "open"}]

    def todos(self, *, include_done: bool = False, timeout_s: float = 0.0) -> list[dict[str, Any]]:
        return list(self.rows)


def test_the_handler_reports_to_the_talker_and_logs_no_content() -> None:
    log = _Log()
    params = _Params({"action": "todos"})
    asyncio.run(QuickMemory(_Rows(), log=log, session="s").handle(params))
    payload = params.results[0]
    assert "- Email Tara slides (due Tuesday 6 October)" in payload["list"]   # the title, the due said naturally
    assert "- Send the photos (your promise to them)" in payload["list"]
    kind, facts = log.events[0]
    assert kind == "quick_memory" and facts["op"] == "todos" and facts["items"] == 2 and facts["ok"] is True
    assert "Tara" not in repr(log.events)


def test_the_tool_schema_is_lean_and_the_prompt_stays_in_its_budget() -> None:
    import json

    from yuki.voice.mouth import HANDS_PROMPT, MOUTH_PROMPT, SPEECH_CHANNEL_PROMPT, VOICE_MARKS_PROMPT

    # measured 4 Oct: the talker's prompt sat just under a step near 18k tokens (+0.3-0.4 s per reply past it); the
    # tool is kept small and the marks section (useless with a voice that strips marks) is sent only with Gemini
    assert QUICK_MEMORY["required"] == ["action"] and len(json.dumps(QUICK_MEMORY)) <= 700
    assert len(MOUTH_PROMPT) + len(SPEECH_CHANNEL_PROMPT) + len(HANDS_PROMPT) + len(VOICE_MARKS_PROMPT) <= 41_800
    assert len(MOUTH_PROMPT) + len(SPEECH_CHANNEL_PROMPT) + len(HANDS_PROMPT) <= 39_900
    # the judgement is the talker's, with a worked example of the live case; no word list decides it
    assert 'quick_memory(action="todos") alone' in MOUTH_PROMPT and "(no look)" in MOUTH_PROMPT
    assert "the Earlier block" in MOUTH_PROMPT


def test_the_view_rides_after_the_cached_layers_which_stay_byte_identical() -> None:
    from yuki.voice.mouth import MouthMemory, mouth_system_blocks

    base = MouthMemory(source="memory", names=["Dev"], standing="- Keep replies short.")
    with_view = MouthMemory(source="memory", names=["Dev"], standing="- Keep replies short.",
                            recent=f"{recall.RECENT_LABEL}\n09:40 They: hi\n  You: hey\n{recall.RECENT_END}")
    plain, viewed = mouth_system_blocks(base), mouth_system_blocks(with_view)
    assert viewed[:-1] == plain and viewed[-1]["text"].startswith(recall.RECENT_LABEL)
    assert "cache_control" not in viewed[-1] and "cache_control" in viewed[-2]


def test_the_marks_section_rides_only_with_a_voice_that_performs_marks() -> None:
    from yuki.voice.mouth import VOICE_MARKS_PROMPT, MouthMemory, mouth_system_blocks
    from yuki.voice.pipeline import _Hooks

    memory = MouthMemory(source="memory", names=["Dev"])
    assert VOICE_MARKS_PROMPT in mouth_system_blocks(memory, channel="marker")[0]["text"]   # the default: as before
    plain = mouth_system_blocks(memory, channel="marker", marks=False)[0]["text"]
    assert VOICE_MARKS_PROMPT not in plain and "[laugh]" not in plain and "# Your voice" in plain

    class Conv:
        tts_provider = "gemini"

    conv = Conv()
    assert _Hooks(conv).marks() is True
    conv.tts_provider = "deepgram"
    assert _Hooks(conv).marks() is False


def test_a_woken_conversation_reads_the_hour_again_and_its_first_call_waits_for_it() -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation, _Hooks

    class Access:
        def __init__(self) -> None:
            self.turns = [_turn(1, time.time() - 120, "make a to do list of this", "Added 7 to-dos to your list.")]

        def call(self, what: str, fn: Any, *, timeout_s: float) -> Any:
            time.sleep(0.05)
            return fn(self)

        def turns_on(self, day: str, limit: int = 200) -> list[dict[str, Any]]:
            return list(self.turns)

    conv = Conversation.__new__(Conversation)
    conv.memory, conv.mouth_memory, conv.log, conv.session = Access(), MouthMemory(recent=None), _Log(), "s"
    conv._recent_job = None
    conv._refresh_recent()
    job = conv._recent_job
    assert job is not None

    class Settings:
        voice_hands = False

    conv.settings, conv.bridge, conv.grounder, conv.practice = Settings(), object(), None, None
    conv._shown_ids = []

    class Call:
        user_turn = True

    asyncio.run(_Hooks(conv).before_call(type("Ctx", (), {"get_messages": lambda self: [
        {"role": "user", "content": "what were the to-dos I told you?"}], "messages": [
        {"role": "user", "content": "what were the to-dos I told you?"}]})(), Call()))
    assert job.done() and conv._recent_job is None
    assert conv.mouth_memory.recent and "Added 7 to-dos" in conv.mouth_memory.recent
    kind, facts = conv.log.events[0]
    assert kind == "recent" and facts["op"] == "woken" and facts["changed"] is True and "to-dos" not in repr(facts)
