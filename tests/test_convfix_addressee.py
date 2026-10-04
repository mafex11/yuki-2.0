"""Report 77b gaps 2 and 3: call turns nobody judged, and why a quote was rejected.

Gap 2: in a call, a user turn whose next words came before Yuki said or did anything used to be stored as "said to
others" by its timing alone (never extracted, never searchable as Yuki's). It is now "unanswered": memory's extractor
judges it from its content and its judgement is stored; the last-hour view keeps words said to Yuki that got no spoken
reply. Gap 3: the rejected quotes were the item's old words or Yuki's words; the rejection now says which.

Temp databases, stub models and invented words only; nothing touches the desktop, a model or the real memory.
"""

from __future__ import annotations

import json
import tempfile
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.config import Settings
from yuki.memory.store import Store

T0 = datetime(2026, 10, 4, 23, 30).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


# -- the voice side ------------------------------------------------------------------------------------------------


class _Memory:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def log_turn(self, **kw: Any) -> None:
        self.turns.append(kw)


def _observer(memory: Any, call: list[bool], chat: list[dict]) -> Any:
    from yuki.voice.pipeline import TurnObserver

    obs = TurnObserver(settings=Settings(), log=None, memory=memory, session="t", speech_end=lambda: None,
                       on_state=lambda *a: None, tts_provider="deepgram", activity=lambda: None,
                       call_mode=lambda: call[0])
    obs.on_chat = chat.append
    return obs


def _say(obs: Any, text: str) -> None:
    from pipecat.frames.frames import TranscriptionFrame

    obs.on_frame(TranscriptionFrame(text=text, user_id="u", timestamp="t"))


def _quiet(obs: Any, n: int) -> None:
    from pipecat.frames.frames import FunctionCallInProgressFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame

    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(FunctionCallInProgressFrame(function_name="stay_quiet", tool_call_id=f"q{n}", arguments={}))
    obs.on_frame(LLMFullResponseEndFrame())


def _answer(obs: Any, words: str) -> None:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, BotStoppedSpeakingFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
        LLMTextFrame, TTSTextFrame,
    )

    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMTextFrame(text=words))
    obs.on_frame(BotStartedSpeakingFrame())
    output = object()
    obs.output_processor = output
    obs.on_played(types.SimpleNamespace(frame=TTSTextFrame(words, aggregated_by="sentence"), source=output))
    obs.on_frame(BotStoppedSpeakingFrame())
    obs.on_frame(LLMFullResponseEndFrame())


def test_a_call_turn_cut_off_by_the_next_words_is_unanswered_not_others() -> None:
    memory, chat = _Memory(), []
    obs = _observer(memory, [True], chat)
    _say(obs, "set a timer")                 # cut off by its own next words: nobody judged it
    _say(obs, "for ten minutes")
    _answer(obs, "Ten minutes, starting now.")
    _say(obs, "bro that round was rough")    # the talker's judgement: for the call
    _quiet(obs, 1)
    _say(obs, "lol")                         # the conversation ends before anything: still nobody's judgement
    obs.close()
    assert [(t["user_text"], t["addressed"]) for t in memory.turns] == [
        ("set a timer", "unanswered"), ("for ten minutes", "yuki"), ("bro that round was rough", "others"),
        ("lol", "unanswered")]
    assert [e["addressed"] for e in chat if e.get("type") == "turn_closed"] == [
        "unanswered", "yuki", "others", "unanswered"]
    assert obs.totals["for_others"] == 3 and obs.exchanges == 1   # the content-free row stats are unchanged


def test_outside_a_call_a_cut_off_turn_stays_said_to_yuki() -> None:
    memory, chat = _Memory(), []
    obs = _observer(memory, [False], chat)
    _say(obs, "open the")
    _say(obs, "open the invented notes app")
    _answer(obs, "Opening it.")
    obs.close()
    assert [t["addressed"] for t in memory.turns] == ["yuki", "yuki"]


def test_the_ui_greys_an_unanswered_turn_with_its_own_words() -> None:
    from yuki.ui.chat_model import ChatModel

    model = ChatModel()
    handler = getattr(model, "_on_voice_turn_closed")
    items = [SimpleNamespace(kind="user", turn="t-7", status="done"),
             SimpleNamespace(kind="user", turn="t-8", status="done")]
    model.items = items
    model._changed = lambda item: None
    handler({"turn": "t-7", "addressed": "unanswered"}, T0)
    handler({"turn": "t-8", "addressed": "yuki"}, T0)
    assert [i.status for i in items] == ["unanswered", "done"]


# -- memory: stored, extracted, judged --------------------------------------------------------------------------------


def test_the_store_keeps_unanswered_and_drops_unknown_marks(store):
    a = store.add_turn("s", "voice-s-1", T0, "set a timer", "", [], "voice", addressed="unanswered")
    b = store.add_turn("s", "voice-s-2", T0 + 1, "hi", "Hi.", [], "voice", addressed="maybe")
    store.flush_turns()
    got = {t.id: t.addressed for t in store.turns_by_ids([a, b])}
    assert got == {a: "unanswered", b: None}


def _worker(store: Store, answer: dict[str, Any], seen: list[str]):
    from yuki.memory.conversations import ConversationWorker

    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))

    def fake_call(purpose, system, user, tool, call, model=None):
        seen.append(user)
        call.cost_usd = 0.0
        call.stop_reason = "tool_use"
        return (answer if purpose == "extract" else {"knowhow": []}), SimpleNamespace(content=[]), {}

    worker._call = fake_call
    worker._embed = lambda texts: (None, "no embedder in tests")
    return worker


def test_a_batch_of_unanswered_turns_is_read_and_the_extractor_judgement_is_stored(store):
    ids = [store.add_turn("call-1", f"voice-call-1-{n}", T0 + n, words, "", [], "voice", addressed=mark)
           for n, (words, mark) in enumerate([("remind me to call the invented dentist", "unanswered"),
                                               ("haha no way", "unanswered"),
                                               ("bro you're cooked", "others")], start=1)]
    store.flush_turns()
    seen: list[str] = []
    worker = _worker(store, {"operations": [], "not_for_yuki": [2], "journal_facts": [], "questions": [],
                             "answers": [], "knowhow": []}, seen)
    result = worker.extract("call-1", store.turns_by_ids(ids))
    assert result.ok and seen, "a batch with unanswered turns is read by the model, never skipped"
    assert seen[0].count("SAID IN A CALL, NEVER ANSWERED") == 2 and "SAID TO SOMEONE ELSE" in seen[0]
    got = {t.id: (t.addressed, t.extracted) for t in store.turns_by_ids(ids)}
    assert got == {ids[0]: ("yuki", True), ids[1]: ("others", True), ids[2]: ("others", True)}


def test_a_batch_said_wholly_to_others_is_still_skipped_without_a_call(store):
    ids = [store.add_turn("call-2", f"voice-call-2-{n}", T0 + n, "gg", "", [], "voice", addressed="others")
           for n in (1, 2)]
    store.flush_turns()
    seen: list[str] = []
    result = _worker(store, {"operations": []}, seen).extract("call-2", store.turns_by_ids(ids))
    assert result.ok and seen == []


def test_the_extract_prompt_explains_the_unanswered_mark():
    from yuki.memory.conversations import EXTRACT_SYSTEM_PROMPT

    assert "SAID IN A CALL, NEVER ANSWERED" in EXTRACT_SYSTEM_PROMPT
    assert "when you cannot tell, it was said to the people on the call" in EXTRACT_SYSTEM_PROMPT


def test_search_does_not_learn_from_unjudged_call_turns_meanwhile():
    import inspect

    from yuki.memory import relevant

    assert '("others", "unanswered")' in inspect.getsource(relevant)


# -- the last hour (vfix4's view) -----------------------------------------------------------------------------------


def test_the_last_hour_keeps_words_said_to_yuki_without_a_spoken_reply():
    from yuki.voice.recall import recent_view

    now = T0 + 600
    turns = [
        {"id": 1, "at": T0 + 10, "user": "pause the music", "reply": "", "actions": ["media_key (by voice)"],
         "addressed": "yuki"},
        {"id": 2, "at": T0 + 20, "user": "set a timer", "reply": "", "actions": [], "addressed": "yuki"},
        {"id": 3, "at": T0 + 25, "user": "for ten minutes", "reply": "Ten minutes, starting now.", "actions": [],
         "addressed": "yuki"},
        {"id": 4, "at": T0 + 30, "user": "bro that round was rough", "reply": "", "actions": [], "addressed": "others"},
        {"id": 5, "at": T0 + 40, "user": "lol", "reply": "", "actions": [], "addressed": "unanswered"},
        {"id": 6, "at": T0 + 50, "user": "typed with no reply", "reply": "", "actions": [], "addressed": None},
    ]
    view = recent_view(turns, now=now)
    assert view is not None
    assert "They: pause the music\n  You (no words): did media_key (by voice)" in view
    assert "They: set a timer\n" in view and "They: for ten minutes\n  You: Ten minutes, starting now." in view
    for absent in ("round was rough", "lol", "typed with no reply"):
        assert absent not in view


def test_the_same_words_logged_twice_show_once_with_the_reply():
    """A voice hand-off with no words of its own, then the worker's turn with the same words and the outcome."""
    from yuki.voice.recall import recent_view

    turns = [{"id": 1, "at": T0, "user": "send the invented deck", "reply": "", "actions": ["tell_yuki (by voice)"],
              "addressed": "yuki"},
             {"id": 2, "at": T0 + 30, "user": "send the invented deck", "reply": "Sent it.", "actions": []}]
    view = recent_view(turns, now=T0 + 60)
    assert view.count("send the invented deck") == 1 and "You: Sent it." in view and "tell_yuki" not in view


def test_an_hour_of_only_call_talk_is_still_empty():
    from yuki.voice.recall import recent_view

    turns = [{"id": 1, "at": T0, "user": "gg", "reply": "", "addressed": "others"},
             {"id": 2, "at": T0 + 5, "user": "ok", "reply": "", "addressed": "unanswered"}]
    assert recent_view(turns, now=T0 + 60) is None


def test_memory_hands_the_view_who_each_turn_was_said_to(store):
    from yuki.memory.api import MemoryClient
    from yuki.memory.checkins import user_day

    store.add_turn("s", "voice-s-1", T0, "set a timer", "", [], "voice", addressed="unanswered")
    store.flush_turns()
    client = MemoryClient(store)
    assert client.turns_on(user_day(T0))[0]["addressed"] == "unanswered"
    assert client.recent_turns()[0]["addressed"] == "unanswered"


# -- the backfill -----------------------------------------------------------------------------------------------------


def test_backfill_plans_then_applies_from_the_voice_log_only(store, tmp_path):
    from yuki.memory import addressee_backfill as bf

    old_cut = store.add_turn("20261004-230000-aa", "voice-20261004-230000-aa-3", T0, "set a", "", [], "voice",
                             addressed="others")
    quiet = store.add_turn("20261004-230000-aa", "voice-20261004-230000-aa-4", T0 + 1, "gg", "", [], "voice",
                           addressed="others")
    store.flush_turns()
    store._write(lambda c: c.execute("UPDATE conversation_turns SET extracted=1"))
    logs = tmp_path / "voice"
    logs.mkdir()
    rows = [
        {"type": "turn", "session": "20261004-230000-aa", "turn": 3, "trigger": "user", "call_mode": True,
         "addressed": "unanswered", "for_others_why": "next_words_first"},
        {"type": "turn", "session": "20261004-230000-aa", "turn": 4, "trigger": "user", "call_mode": True,
         "addressed": "others", "for_others_why": "talker_stay_quiet"},
        {"type": "turn", "session": "20261004-230000-aa", "turn": 9, "trigger": "user", "call_mode": True,
         "addressed": "unanswered"},   # no words were stored for it
        {"type": "session", "session": "20261004-230000-aa", "state": "start"},
    ]
    (logs / "voice-20261004.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n",
                                               encoding="utf-8")
    ids = bf.unanswered_turns(bf.read_voice_logs(logs))
    assert ids == ["voice-20261004-230000-aa-3", "voice-20261004-230000-aa-9"]
    p = bf.plan(store, ids, requeue=True)
    assert p.as_dict() | {"relabel_ids": None} == {"logged_unanswered": 2, "in_memory": 1, "relabel": 1,
                                                   "relabel_ids": None, "already": 0, "missing": 1, "requeue": 1}
    # the dry run (main's default) writes nothing
    db = store.path
    assert bf.main(["--db", str(db), "--voice-logs", str(logs)]) == 0
    assert store.turns_by_ids([old_cut])[0].addressed == "others"
    assert bf.apply(store, p) == 1
    got = {t.id: (t.addressed, t.extracted) for t in store.turns_by_ids([old_cut, quiet])}
    assert got == {old_cut: ("unanswered", False), quiet: ("others", True)}
    assert [t.id for t in store.turns_pending()] == [old_cut]
    # run again: nothing left to do
    assert bf.plan(store, ids).as_dict()["relabel"] == 0


# -- gap 3: why a quote was rejected ---------------------------------------------------------------------------------


def _validate(store: Store, ops: list[dict[str, Any]], turn_ids: list[int]):
    from yuki.memory.conversations import ConversationWorker

    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids(turn_ids)
    return worker.validate(ops, turns, store.conversation_facts(), worker.cards_for(turns))


def _rule_op(op: str, item: int, quote: str, n: int = 1) -> dict[str, Any]:
    return {"op": op, "item": item, "kind": "rule", "subject": "answer length", "text": "Keep answers to one line.",
            "quote": quote, "due": "", "end_as": "", "card": 0, "exchanges": [n], "reason": "", "owner": ""}


def test_an_update_sending_the_items_old_quote_is_told_so(store):
    old = store.add_turn("s", 1, T0, "keep your answers to one line from now on", "Got it.", [], "final")
    store.flush_turns()
    rule = store.add_conversation_fact("rule", "Keep answers to one line.", quote="keep your answers to one line",
                                       source_turn_ids=[old], at=T0, origin="model")
    new = store.add_turn("s", 2, T0 + 600, "yes", "Want me to keep that for voice too?", [], "final")
    store.flush_turns()
    changes, records = _validate(store, [_rule_op("UPDATE", rule, "keep your answers to one line")], [new])
    assert changes == [] and records[0].status == (
        "rejected: quote is the item's old words, not said in the cited exchanges (an UPDATE quotes the new words)")


def test_a_rule_quoting_yukis_reply_is_told_so(store):
    t = store.add_turn("s", 3, T0, "", "I'll always ask before sending invented messages.", [], "voice")
    store.flush_turns()
    changes, records = _validate(store, [_rule_op("ADD", 0, "always ask before sending")], [t])
    assert changes == [] and records[0].status == "rejected: quote is Yuki's words, not the user's"


def test_a_real_quote_still_passes_and_a_made_up_one_still_fails(store):
    t = store.add_turn("s", 4, T0, "Yuki, from now on, keep your answers short, okay?", "Okay.", [], "final")
    store.flush_turns()
    changes, _ = _validate(store, [_rule_op("ADD", 0, "keep your answers short")], [t])
    assert len(changes) == 1 and changes[0].quote == "keep your answers short"
    changes, records = _validate(store, [_rule_op("ADD", 0, "never use long answers")], [t])
    assert changes == [] and records[0].status == "rejected: quote is not the user's own words in the cited exchanges"


def test_the_update_instruction_names_the_new_words():
    from yuki.memory.conversations import EXTRACT_SYSTEM_PROMPT

    assert "never the item's old quote" in EXTRACT_SYSTEM_PROMPT
