"""leftVoice (report 71): a quick memory read is never silent, and a clean voice add needs no second talker run.

Report 65's open point: "what's on my list" by voice gave about 2.5-3 s of silence, because the talker's reply that
makes the quick_memory call said nothing and only the reply after the report spoke. Now the talker writes a few words
first in the same reply ("Let me look."), which the voice says while the call is written and the read runs; and a
clean add is confirmed by code in Yuki's voice ("Added it, it's on Home under Your list."), as a hand's done line is.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from yuki.voice import recall  # noqa: E402
from yuki.voice.recall import (  # noqa: E402
    ADDED_SAID_NOTE, NO_TIME_MARK, QUICK_MEMORY, QuickMemory, added_line,
)

# ---------------------------------------------------------------------------------------------------------------------
# the prompt: a few words before the look, every other call still alone
# ---------------------------------------------------------------------------------------------------------------------


def test_the_look_comes_after_a_few_words_and_every_other_call_stays_alone() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, SPEECH_CHANNEL_PROMPT

    assert "after a few words in the same reply" in QUICK_MEMORY["description"]
    assert "alone, no words before it" not in QUICK_MEMORY["description"]
    # the one exception to "every tool call alone", in the rule itself, and its worked example in the speech channel
    assert ("Every tool call is made alone, with no words written before it, except quick_memory: a few words first"
            in " ".join(MOUTH_PROMPT.split()))
    assert 'Good: "what\'s on my list?" -> the line say, "Let me look.", then quick_memory(action="todos")' in \
        SPEECH_CHANNEL_PROMPT
    # the pause example (words before a hand hold the music) is kept: the exception is the look only
    assert '"Yep, pausing it." + media_key' in MOUTH_PROMPT
    # what the Earlier block holds is answered from it, with no look
    assert "answer from the Earlier block when it holds it" in " ".join(MOUTH_PROMPT.split())
    # no say_first field (its tokens were the reason vfix4 left this open); one short optional field, then, for the
    # rest of a request an add is part of (second review LVV-1), paid for by shorter wording: still vfix4's 700
    assert list(QUICK_MEMORY["properties"]) == ["action", "items", "query", "then"]
    assert QUICK_MEMORY["required"] == ["action"]
    assert len(json.dumps(QUICK_MEMORY)) <= 700


# ---------------------------------------------------------------------------------------------------------------------
# the pipeline: the words before a look reach the voice before the call is written
# ---------------------------------------------------------------------------------------------------------------------


def test_words_before_a_look_reach_the_voice_before_the_call_and_count_as_said() -> None:
    from pipecat.services.anthropic.llm import AnthropicLLMService

    from test_voice_v316 import _events
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import LlmCall, _layered_service

    order: list[tuple[str, str]] = []
    acked: list[bool] = []

    class Hooks:
        def channel(self) -> str:
            return "marker"

        def marker_report(self, call: Any, channel: Any) -> None:
            pass

        def fallbacks(self) -> list:
            return []

        def sticky_index(self, size: int) -> int:
            return 0

        def model_ok(self, *a: Any) -> None:
            pass

        def acked(self) -> None:
            acked.append(True)

        def early_said(self, *a: Any) -> None:
            order.append(("early", ""))

    async def run() -> Any:
        llm = _layered_service(AnthropicLLMService, MouthMemory(), Hooks())(
            api_key="x", settings=AnthropicLLMService.Settings(model="m"))

        async def push(frame: Any, direction: Any = None) -> None:
            order.append(("push", type(frame).__name__))

        llm.push_frame = push
        events = _events(["say\nLet me ", "look."], tool="quick_memory", args='{"action": "todos"}')

        async def fake_open(api_call: Any, params: Any) -> tuple[Any, Any]:
            async def gen() -> Any:
                for e in events[1:]:
                    yield e
            return gen(), events[0]

        llm._yuki_open = fake_open
        llm._yuki_call = LlmCall(entered=time.monotonic(), user_turn=True)
        stream = await llm._create_message_stream(None, {})
        async for e in stream:
            kind = getattr(e, "type", "")
            if kind == "content_block_delta" and hasattr(e.delta, "text"):
                order.append(("text", e.delta.text))
            elif kind == "content_block_start" and getattr(e.content_block, "type", "") == "tool_use":
                order.append(("tool", e.content_block.name))
        return llm._yuki_call

    call = asyncio.run(run())
    said = "".join(t for k, t in order if k == "text")
    assert said.strip() == "Let me look."                       # the marker line is never spoken
    tool_at = order.index(("tool", "quick_memory"))
    assert all(order.index(("text", t)) < tool_at for k, t in order if k == "text")   # heard before the call
    assert ("push", "WordsDoneFrame") in order[:tool_at]        # the sentence waiting for its lookahead is said now
    assert call.first_text is not None and acked                # the reply's own words: no second acknowledgement
    assert ("early", "") not in order                           # quick_memory has no say_first to speak


# ---------------------------------------------------------------------------------------------------------------------
# a clean add is confirmed by code; anything else goes to the talker
# ---------------------------------------------------------------------------------------------------------------------


def test_the_added_line_is_said_only_for_a_clean_add() -> None:
    assert added_line({"added": 1, "items": ["Renew the car insurance"]}) == "Added it, it's on Home under Your list."
    assert added_line({"added": 7, "items": [f"item {i}" for i in range(7)]}) == \
        "Added 7, they're on Home under Your list."
    # what needs the talker's words: a due that could not be kept, an unsure write, unsaved items, a failure
    assert added_line({"added": 1, "items": ["Pay rent" + NO_TIME_MARK]}) is None
    assert added_line({"added": 1, "items": ["a"], "may_have_saved": 1}) is None
    assert added_line({"added": 1, "items": ["a"], "not_saved": 2}) is None
    assert added_line({"failed": "memory did not take it", "not_saved": 1}) is None
    assert added_line({"added": 0, "items": []}) is None
    assert added_line({"list": "Nothing open."}) is None


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **facts: Any) -> None:
        self.events.append((kind, facts))


class _Memory:
    """MemoryAccess as the tool calls it (a list in memory)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [{"id": "todo:1", "text": "Buy printer ink", "status": "open"}]

    def add_todo(self, text: str, due: Any = None, *, timeout_s: float = 4.0) -> str:
        if due == "bad":
            raise RuntimeError("memory call failed") from ValueError("bad due")
        self.rows.append({"id": f"todo:{len(self.rows) + 1}", "text": text, "status": "open"})
        return self.rows[-1]["id"]

    def todos(self, include_done: bool = False, *, timeout_s: float = 4.0) -> list[dict[str, Any]]:
        return list(self.rows)


def _params(args: dict[str, Any], *, llm: bool = True) -> tuple[Any, list[Any], list[tuple[Any, Any]]]:
    pushed: list[Any] = []
    settled: list[tuple[Any, Any]] = []

    async def push(frame: Any, direction: Any = None) -> None:
        pushed.append(frame)

    async def result_callback(result: Any, *, properties: Any = None) -> None:
        settled.append((result, properties))

    params = SimpleNamespace(arguments=args, tool_call_id="toolu_9", result_callback=result_callback,
                             llm=SimpleNamespace(push_frame=push) if llm else None)
    return params, pushed, settled


def test_a_clean_voice_add_is_said_by_code_with_no_second_talker_run() -> None:
    from pipecat.frames.frames import TTSSpeakFrame

    log = _Log()
    tool = QuickMemory(_Memory(), log=log, session="s1")
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Renew the car insurance"}]})
    asyncio.run(tool.handle(params))
    assert [type(f) for f in pushed] == [TTSSpeakFrame] and pushed[0].text == "Added it, it's on Home under Your list."
    result, properties = settled[0]
    assert properties is not None and properties.run_llm is False          # the talker is not run again
    assert result["said"] == pushed[0].text and result["how_to_handle_it"] == ADDED_SAID_NOTE
    assert result["added"] == 1 and result["where"] == recall.LIST_PLACE   # the report itself is kept for the record
    kind, facts = log.events[-1]
    assert kind == "quick_memory" and facts["said_by"] == "code" and facts["ok"] is True
    assert "insurance" not in repr(facts)                                   # content-free


def test_an_add_that_needs_words_and_every_read_still_go_to_the_talker() -> None:
    # a due memory could not read: saved without it, and the talker says so
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Pay rent", "due": "bad"}]})
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert not pushed and settled[0][1] is None and "without its time" in settled[0][0]["items"][0]
    # a read: the talker answers what they asked from the report (its words came before the call)
    params, pushed, settled = _params({"action": "todos"})
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert not pushed and settled[0][1] is None and "Buy printer ink" in settled[0][0]["list"]
    # nothing that can put words into the voice (a bare stand-in): the talker says the add
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Call the bank"}]}, llm=False)
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert settled[0][1] is None and "said" not in settled[0][0]


@pytest.mark.parametrize("n", [2, 5])
def test_several_items_are_counted_in_the_line(n: int) -> None:
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": f"Thing {i}"} for i in range(n)]})
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert pushed[0].text == f"Added {n}, they're on Home under Your list." and settled[0][1].run_llm is False


# ---------------------------------------------------------------------------------------------------------------------
# the answer to a look is written while its lead plays (Pipecat kept the report behind the lead's audio)
# ---------------------------------------------------------------------------------------------------------------------


def _tts() -> Any:
    """ClauseTTS over a stand-in Pipecat TTS: records whether a reply's end paused the voice's frames."""
    from yuki.voice.pipeline import _clause_tts

    class Base:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            from pipecat.utils.text.base_text_aggregator import AggregationType

            self._text_aggregation_mode = AggregationType.SENTENCE
            self.paused = 0

        async def _maybe_pause_frame_processing(self) -> None:
            self.paused += 1

        async def process_frame(self, frame: Any, direction: Any) -> None:
            from pipecat.frames.frames import LLMFullResponseEndFrame

            if isinstance(frame, LLMFullResponseEndFrame):
                await self._maybe_pause_frame_processing()   # Pipecat: "to avoid audio overlapping"

    return _clause_tts(Base)()


def _started(*names: str) -> Any:
    from pipecat.frames.frames import FunctionCallsStartedFrame

    return FunctionCallsStartedFrame(function_calls=[SimpleNamespace(function_name=n) for n in names])


@pytest.mark.parametrize("names,paused", [
    (("quick_memory",), 0),            # a reply ending on a look: its report goes on at once, behind nothing
    (("tell_yuki",), 1),               # any other call: Pipecat's pause, as before
    (("quick_memory", "media_key"), 1),
    ((), 1),                           # a reply with no call
])
def test_a_reply_ending_on_a_look_does_not_pause_the_voice(names: tuple[str, ...], paused: int) -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame

    tts = _tts()

    async def run() -> None:
        if names:
            await tts.process_frame(_started(*names), None)
        await tts.process_frame(LLMFullResponseEndFrame(), None)
        await tts.process_frame(LLMFullResponseEndFrame(), None)   # the next reply's end pauses again

    asyncio.run(run())
    assert tts.paused == paused + 1


def test_a_cut_reply_forgets_its_look() -> None:
    from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame

    tts = _tts()

    async def run() -> None:
        await tts.process_frame(_started("quick_memory"), None)
        await tts.process_frame(InterruptionFrame(), None)
        await tts.process_frame(LLMFullResponseEndFrame(), None)

    asyncio.run(run())
    assert tts.paused == 1


class _Context:
    def __init__(self, messages: list[Any]) -> None:
        from pipecat.utils.types import NOT_GIVEN

        self._messages = messages
        self.tools = NOT_GIVEN
        self.tool_choice = NOT_GIVEN

    def get_messages(self) -> list[Any]:
        return list(self._messages)


_HISTORY = [{"role": "user", "content": "What's on my list?"}]
_REPORT = {"list": "2 open: - Buy printer ink - Call the plumber", "how_to_handle_it": "Tell them."}


def _conversation(spoke: set[str], report: dict[str, Any], *, by_code: bool = False, solo: set[str] | None = None,
                  user_speaking: bool = False, during: Any = None) -> Any:
    from yuki.voice.pipeline import Conversation

    queued: list[Any] = []

    async def queue_frame(frame: Any, direction: Any = None) -> None:
        queued.append(frame)

    class Tool:
        async def handle(self, params: Any) -> None:
            if during is not None:
                during(params)   # what happens while the look runs (the user goes on talking, ...)
            if by_code:
                from pipecat.frames.frames import FunctionCallResultProperties

                await params.result_callback(report, properties=FunctionCallResultProperties(run_llm=False))
            else:
                await params.result_callback(report)

    llm = SimpleNamespace(_yuki_spoke=spoke, _yuki_solo=set(spoke) if solo is None else solo, queue_frame=queue_frame)
    conv = SimpleNamespace(_llm=llm, quick_memory=Tool(), log=_Log(), session="s1",
                           observer=SimpleNamespace(bot_speaking=True, user_speaking=user_speaking), queued=queued)
    conv._answer_look_now = lambda params, result: Conversation._answer_look_now(conv, params, result)
    return conv


def _look_params(settled: list[Any], context: Any) -> Any:
    async def result_callback(result: Any, *, properties: Any = None) -> None:
        settled.append((result, properties))

    return SimpleNamespace(tool_call_id="toolu_7", function_name="quick_memory", arguments={"action": "todos"},
                           context=context, result_callback=result_callback)


def test_a_look_after_a_lead_is_answered_at_once_on_a_copy_that_holds_its_report() -> None:
    from pipecat.frames.frames import LLMContextFrame

    from yuki.voice.pipeline import Conversation

    report = {"list": "2 open: - Buy printer ink - Call the plumber", "how_to_handle_it": "Tell them."}
    history = [{"role": "user", "content": "What's on my list?"},
               # a placeholder the conversation may already hold for this call is replaced in the copy
               {"role": "assistant", "tool_calls": [{"id": "toolu_7", "type": "function",
                                                     "function": {"name": "quick_memory", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "toolu_7", "content": "IN_PROGRESS"}]
    conv = _conversation({"toolu_7"}, report)
    settled: list[Any] = []
    context = _Context(history)
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, context)))
    result, properties = settled[0]
    assert result == report and properties.run_llm is False     # in the conversation, with no run of its own
    (frame,) = conv.queued
    assert isinstance(frame, LLMContextFrame)                    # the answer's run, now
    messages = frame.context.get_messages()
    assert messages[0] == history[0] and len(messages) == 3
    assert messages[1]["tool_calls"][0]["id"] == "toolu_7"
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == {"action": "todos"}
    assert messages[2] == {"role": "tool", "tool_call_id": "toolu_7", "content": json.dumps(report)}
    assert context.get_messages() == history   # the shared one is untouched
    kind, facts = conv.log.events[-1]
    assert kind == "quick_memory" and facts["op"] == "answer_now" and "plumber" not in repr(facts)


def test_a_look_with_no_lead_and_a_clean_add_settle_as_before() -> None:
    from yuki.voice.pipeline import Conversation

    # no words before the call: nothing holds its report back, Pipecat runs the answer as usual
    conv = _conversation(set(), {"list": "Nothing open."})
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context([]))))
    assert settled[0][1] is None and not conv.queued
    # a report settled with no run at all stays so
    conv = _conversation({"toolu_7"}, {"added": 1, "said": "Added it."}, by_code=True)
    settled = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context([]))))
    assert settled[0][1].run_llm is False and not conv.queued


def test_the_conversation_registers_the_look_with_its_answer_now_handler() -> None:
    import inspect

    from yuki.voice import pipeline

    source = inspect.getsource(pipeline.Conversation)
    assert 'register_function(QUICK_MEMORY["name"], self._quick_memory_call' in source
    assert pipeline.ANSWER_WHILE_SPEAKING == frozenset({"quick_memory"})


# ---------------------------------------------------------------------------------------------------------------------
# review fixes: a look is answered at once only when it is alone in its reply and the user has not gone on (LV-1, LV-2)
# ---------------------------------------------------------------------------------------------------------------------



def test_a_look_beside_another_call_in_its_reply_settles_as_before() -> None:
    from yuki.voice.pipeline import Conversation

    # LV-1: "What's on my list, and what did I say about Tara?" -> two looks in one reply after "Let me look.":
    # Pipecat answers once, after the last report, with both; neither starts a run of its own on a half copy
    conv = _conversation({"toolu_7", "toolu_8"}, _REPORT, solo=set())
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context(list(_HISTORY)))))
    assert settled[0][1] is None and not conv.queued


def test_a_look_whose_report_comes_back_while_the_user_speaks_settles_as_before() -> None:
    from yuki.voice.pipeline import Conversation

    # LV-2: the user goes on ("...the work one") before the report is back: no run over them; their turn answers
    conv = _conversation({"toolu_7"}, _REPORT, user_speaking=True)
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context(list(_HISTORY)))))
    assert settled[0][1] is None and not conv.queued
    kind, facts = conv.log.events[-1]
    assert kind == "quick_memory" and facts["op"] == "answer_later" and facts["user_speaking"] is True


@pytest.mark.parametrize("role", ["user", "developer"])
def test_a_look_after_which_the_user_said_more_settles_as_before(role: str) -> None:
    from yuki.voice.pipeline import Conversation

    history = list(_HISTORY)
    context = _Context(history)
    # the user finished a new turn while the look ran: the copy would be built before their words
    conv = _conversation({"toolu_7"}, _REPORT,
                         during=lambda params: history.append({"role": role, "content": "The work one."}))
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, context)))
    assert settled[0][1] is None and not conv.queued
    assert conv.log.events[-1][1]["newer"] is True


def test_yukis_own_words_after_the_call_do_not_count_as_the_user_saying_more() -> None:
    from yuki.voice.pipeline import Conversation

    history = list(_HISTORY)
    conv = _conversation({"toolu_7"}, _REPORT,
                         during=lambda params: history.append({"role": "assistant", "content": "Let me look."}))
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context(history))))
    assert settled[0][1].run_llm is False and len(conv.queued) == 1


def test_the_talker_marks_only_a_lone_call_as_alone_in_its_reply() -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    class Gate:
        is_speculating = False

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            self._speculation_gate = Gate()

        async def run_function_calls(self, calls: Any) -> None:
            pass

    hooks = SimpleNamespace(speculation=lambda *a: None, would_hold=lambda name: False, calls_made=lambda ids: None)
    llm = _layered_service(Base, MouthMemory(), hooks)()

    def call(cid: str) -> Any:
        return SimpleNamespace(function_name="quick_memory", tool_call_id=cid)

    async def main() -> None:
        await llm.run_function_calls([call("toolu_1")])
        await llm.run_function_calls([call("toolu_2"), call("toolu_3")])

    asyncio.run(main())
    assert llm._yuki_solo == {"toolu_1"}


def test_a_look_flagged_after_its_replys_end_does_not_unpause_the_next_reply() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame

    tts = _tts()

    async def run() -> None:
        # a speculative reply: the gate releases its end, then its held look is started from a task
        await tts.process_frame(LLMFullResponseStartFrame(), None)
        await tts.process_frame(LLMFullResponseEndFrame(), None)
        await tts.process_frame(_started("quick_memory"), None)
        # the next reply (the look's answer) pauses as usual
        await tts.process_frame(LLMFullResponseStartFrame(), None)
        await tts.process_frame(LLMFullResponseEndFrame(), None)

    asyncio.run(run())
    assert tts.paused == 2


# ---------------------------------------------------------------------------------------------------------------------
# second review: the live conversation, not the run's frozen copy, says whether the user went on (LVC-1); the answer
# stays in the turn its lead belongs to (LVC-2); an add ends the turn only when it is the whole request (LVV-1)
# ---------------------------------------------------------------------------------------------------------------------


def _live() -> Any:
    from pipecat.processors.aggregators.llm_context import LLMContext

    return LLMContext(messages=[{"role": "user", "content": "Yuki."}, {"role": "assistant", "content": "Yeah?"}])


def _provisional(shared: Any, text: str) -> Any:
    """The context an eager end of turn runs on, made by Pipecat's own ``_run_speculative_inference``."""
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

    pushed: list[Any] = []

    async def push_frame(frame: Any, direction: Any = None) -> None:
        pushed.append(frame)

    agg = SimpleNamespace(_context=shared, role="user", push_frame=push_frame)
    asyncio.run(LLMUserAggregator._run_speculative_inference(agg, SimpleNamespace(text=text)))
    (frame,) = pushed
    assert frame.context is not shared and frame.speculation is True
    return frame.context


def _eager_conversation(shared: Any, report: dict[str, Any], *, during: Any = None) -> Any:
    conv = _conversation({"toolu_7"}, report, user_speaking=False, during=during)
    conv._llm_context = shared
    return conv


def test_on_an_eager_turn_the_user_going_on_is_seen_in_the_live_conversation() -> None:
    from yuki.voice.pipeline import Conversation

    shared = _live()
    provisional = _provisional(shared, "What's on my list?")

    def goes_on(params: Any) -> None:
        # the eager turn is committed, then the user finishes a new one, while the read is slow
        shared.add_message({"role": "user", "content": "What's on my list?"})
        shared.add_message({"role": "user", "content": "Actually, the work one."})

    conv = _eager_conversation(shared, _REPORT, during=goes_on)
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, provisional)))
    assert settled[0][1] is None and not conv.queued          # main's way: their own turn answers, once
    assert conv.log.events[-1][1]["op"] == "answer_later" and conv.log.events[-1][1]["newer"] is True


def test_an_eager_turns_own_commit_is_not_the_user_going_on() -> None:
    from yuki.voice.pipeline import Conversation

    shared = _live()
    provisional = _provisional(shared, "What's on my list?")
    # the confirmed turn is written to the conversation just after its held call starts: still the same turn
    conv = _eager_conversation(shared, _REPORT,
                               during=lambda params: shared.add_message({"role": "user", "content": "What's on my list?"}))
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, provisional)))
    assert settled[0][1].run_llm is False and len(conv.queued) == 1
    assert conv.queued[0].context.get_messages()[2] == {"role": "user", "content": "What's on my list?"}


def test_a_look_made_by_the_answer_now_run_sees_the_user_going_on() -> None:
    from yuki.voice.pipeline import Conversation

    shared = _live()
    shared.add_message({"role": "user", "content": "What's on my list?"})
    # the answer-now run's own copy (frozen): it says "Let me search." and looks again
    copy = _Context(shared.get_messages() + [
        {"role": "assistant", "tool_calls": [{"id": "toolu_6", "type": "function",
                                              "function": {"name": "quick_memory", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "toolu_6", "content": "{}"}])
    conv = _eager_conversation(shared, _REPORT,
                               during=lambda params: shared.add_message({"role": "user", "content": "Never mind."}))
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, copy)))
    assert settled[0][1] is None and not conv.queued
    # with nothing said since, a chained look on a fresh copy is answered at once as before
    fresh = _live()
    fresh.add_message({"role": "user", "content": "What's on my list?"})
    conv = _eager_conversation(fresh, _REPORT)
    settled = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, copy)))
    assert settled[0][1].run_llm is False and len(conv.queued) == 1


def _observer(tmp_path: Path) -> Any:
    from yuki.config import Settings
    from yuki.voice.pipeline import TurnObserver

    return TurnObserver(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), log=None,
                        memory=None, session="s1", speech_end=lambda: None, on_state=lambda *a: None,
                        tts_provider="deepgram", activity=lambda: None)


def test_the_answer_now_run_stays_in_the_users_turn_until_its_lead_is_heard(tmp_path: Path) -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame

    from yuki.voice.pipeline import Conversation

    obs = _observer(tmp_path)
    user_turn = obs._open("user")
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMFullResponseEndFrame())   # "Let me look." + the look: the lead is still on its way to the speaker
    conv = _conversation({"toolu_7"}, _REPORT)
    conv.observer = obs
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context(list(_HISTORY)))))
    assert len(conv.queued) == 1
    obs.on_frame(LLMFullResponseStartFrame())  # the answer's run starts before the lead's first audio
    assert obs.turn is user_turn and user_turn.trigger == "user"
    # a later run of its own (a result, a progress line) is still a turn of its own
    obs.on_frame(LLMFullResponseEndFrame())
    obs.on_frame(LLMFullResponseStartFrame())
    assert obs.turn is not user_turn and obs.turn.trigger == "result"


def test_an_add_with_more_asked_in_the_same_words_goes_to_the_talker() -> None:
    from yuki.voice.recall import ADDED_THEN_NOTE

    # LVV-1 (a): "Add call mom to my list, and what time is it?" -> one add whose then holds the rest: no code line
    # (it ended the turn, and the time was never given); the talker says the add and does the rest
    log = _Log()
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Call mom"}],
                                       "then": "  what time is it "})
    asyncio.run(QuickMemory(_Memory(), log=log).handle(params))
    assert not pushed and settled[0][1] is None
    report = settled[0][0]
    assert report["then"] == "what time is it" and "said" not in report
    assert report["how_to_handle_it"].endswith(ADDED_THEN_NOTE.format(then="what time is it"))
    assert log.events[-1][1]["then"] is True and "said_by" not in log.events[-1][1]
    assert "mom" not in repr(log.events) and "time is it" not in repr(log.events)   # content-free
    # an empty then is a whole request: said by code as before
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Call mom"}], "then": ""})
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert pushed and settled[0][1].run_llm is False
    # a read's then is ignored (it is for adds)
    params, pushed, settled = _params({"action": "todos", "then": "what time is it"})
    asyncio.run(QuickMemory(_Memory()).handle(params))
    assert "then" not in settled[0][0]


def test_an_add_beside_another_call_in_its_reply_goes_to_the_talker() -> None:
    # LVV-1 (b): an add with any other call in its reply is answered once, after the last report, by Pipecat
    alone: list[str] = []
    tool = QuickMemory(_Memory(), alone=lambda cid: (alone.append(cid), False)[1])
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Call mom"}]})
    asyncio.run(tool.handle(params))
    assert alone == ["toolu_9"] and not pushed and settled[0][1] is None and "said" not in settled[0][0]
    tool = QuickMemory(_Memory(), alone=lambda cid: True)
    params, pushed, settled = _params({"action": "add_todos", "items": [{"text": "Call mom"}]})
    asyncio.run(tool.handle(params))
    assert pushed and settled[0][1].run_llm is False


def test_the_conversation_tells_the_tool_which_calls_were_alone_in_their_reply() -> None:
    import inspect

    from yuki.voice import pipeline

    source = inspect.getsource(pipeline.Conversation)
    assert 'alone=lambda call_id: call_id in getattr(self._llm, "_yuki_solo", ())' in source
    assert "then" in QUICK_MEMORY["properties"] and QUICK_MEMORY["required"] == ["action"]


@pytest.mark.parametrize("add_first", [False, True])
def test_an_add_and_a_read_in_one_reply_still_get_one_answer(add_first: bool) -> None:
    # LVV-1 (b): "add call mom, and then read me the list" -> add_todos and todos in one reply. Pipecat 1.11 runs the
    # talker once, after the last report of the group; the add settled with run_llm=False starved that run when the
    # read came back first (a write is usually slower)
    from pipecat.frames.frames import (
        FunctionCallFromLLM, FunctionCallInProgressFrame, FunctionCallResultFrame, FunctionCallsStartedFrame,
    )
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator

    agg = LLMAssistantAggregator(LLMContext(messages=[{"role": "user", "content": "Add call mom, then read the list."}]))
    runs: list[Any] = []

    async def push_context_frame(direction: Any = None) -> None:
        runs.append(direction)

    agg.push_context_frame = push_context_frame
    agg.has_queued_frame = lambda cls: False
    calls = {"add": {"action": "add_todos", "items": [{"text": "Call mom"}]}, "read": {"action": "todos"}}
    reports: dict[str, tuple[Any, Any]] = {}
    for cid, args in calls.items():
        params, _pushed, settled = _params(args)
        params.tool_call_id = cid
        asyncio.run(QuickMemory(_Memory(), alone=lambda call_id: False).handle(params))   # two calls in one reply
        reports[cid] = settled[0]
    assert "said" not in reports["add"][0] and reports["add"][1] is None

    async def main() -> None:
        await agg._handle_function_calls_started(FunctionCallsStartedFrame(function_calls=[
            FunctionCallFromLLM(function_name="quick_memory", tool_call_id=cid, arguments=args, context=None)
            for cid, args in calls.items()]))
        for cid, args in calls.items():
            await agg._handle_function_call_in_progress(FunctionCallInProgressFrame(
                function_name="quick_memory", tool_call_id=cid, arguments=args, cancel_on_interruption=False,
                group_id="g1"))
        for cid in (["add", "read"] if add_first else ["read", "add"]):
            result, properties = reports[cid]
            await agg._handle_function_call_result(FunctionCallResultFrame(
                function_name="quick_memory", tool_call_id=cid, arguments=calls[cid], result=result,
                properties=properties))

    asyncio.run(main())
    assert len(runs) == 1


def test_the_answer_now_copy_holds_the_replys_own_words_before_the_call() -> None:
    # offline 4 Oct (second review): "Add call mom to my list, and what time is it?" -> "It's 22:51. Adding call mom to
    # your list now." + the add with then; the answer's copy lacked those words and gave the time a second time
    from yuki.voice.pipeline import Conversation

    conv = _conversation({"toolu_7"}, _REPORT)
    # the turn's words so far: an earlier run's, then this reply's (the newest run starts at reply_from)
    conv.observer.turn = SimpleNamespace(reply=["Hm.", "It's 22:51.", " Adding call mom  now."], reply_from=1)
    settled: list[Any] = []
    asyncio.run(Conversation._quick_memory_call(conv, _look_params(settled, _Context(list(_HISTORY)))))
    messages = conv.queued[0].context.get_messages()
    assert messages[1] == {"role": "assistant", "content": "It's 22:51. Adding call mom now."}
    assert messages[2]["tool_calls"][0]["id"] == "toolu_7" and messages[3]["role"] == "tool" and len(messages) == 4
    assert conv.observer.look_now_turn is conv.observer.turn
    # words the conversation already holds since the user spoke are not written twice
    conv = _conversation({"toolu_7"}, _REPORT)
    conv.observer.turn = SimpleNamespace(reply=["Let me look."], reply_from=0)
    held = [*_HISTORY, {"role": "assistant", "content": "Let me look."}]
    asyncio.run(Conversation._quick_memory_call(conv, _look_params([], _Context(held))))
    assert [m.get("content") for m in conv.queued[0].context.get_messages()].count("Let me look.") == 1
    # the same words in an earlier exchange do not count
    conv = _conversation({"toolu_7"}, _REPORT)
    conv.observer.turn = SimpleNamespace(reply=["Let me look."], reply_from=0)
    old = [{"role": "user", "content": "Tara?"}, {"role": "assistant", "content": "Let me look."}, *_HISTORY]
    asyncio.run(Conversation._quick_memory_call(conv, _look_params([], _Context(old))))
    assert [m.get("content") for m in conv.queued[0].context.get_messages()].count("Let me look.") == 2


def test_each_mouth_run_marks_where_its_words_start_in_the_turn(tmp_path: Path) -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame

    obs = _observer(tmp_path)
    turn = obs._open("user")
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMTextFrame(text="Let me look."))
    obs.on_frame(LLMFullResponseEndFrame())
    assert turn.reply_from == 0
    obs.look_now_turn = turn
    obs.on_frame(LLMFullResponseStartFrame())   # the answer-now run, in the same turn
    obs.on_frame(LLMTextFrame(text="Two things."))
    assert obs.turn is turn and turn.reply_from == 1 and "".join(turn.reply[turn.reply_from:]) == "Two things."
