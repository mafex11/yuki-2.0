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
    # the schema is unchanged (no say_first field: its tokens were the reason vfix4 left this open)
    assert list(QUICK_MEMORY["properties"]) == ["action", "items", "query"] and QUICK_MEMORY["required"] == ["action"]
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
# the answer to a look is written while its lead plays (Pipecat would wait for the bot to stop speaking)
# ---------------------------------------------------------------------------------------------------------------------


class _Aggregator:
    """Pipecat 1.11's assistant aggregator, the parts answer_while_speaking wraps."""

    def __init__(self, speaking: bool, queued: bool = False) -> None:
        self._bot_speaking = speaking
        self._queued = queued
        self.pushed: list[Any] = []
        self.deferred = False

    def has_queued_frame(self, frame_type: Any) -> bool:
        return self._queued

    async def push_context_frame(self, direction: Any = None) -> None:
        self.pushed.append(direction)

    async def _maybe_push_context_after_function_result(self) -> None:   # Pipecat's own rule
        if self._queued:
            return
        if self._bot_speaking:
            self.deferred = True
            return
        await self.push_context_frame("UPSTREAM")

    async def _handle_function_call_result(self, frame: Any) -> None:
        await self._maybe_push_context_after_function_result()


@pytest.mark.parametrize("name,speaking,queued,runs_now,deferred", [
    ("quick_memory", True, False, True, False),    # the look's report while "Let me look." plays: answered at once
    ("quick_memory", False, False, True, False),   # nothing playing: as before
    ("quick_memory", True, True, False, False),    # more results queued: bundled, as Pipecat does
    ("check_progress", True, False, False, True),  # any other tool: Pipecat's wait for the bot to stop
])
def test_a_looks_answer_runs_while_its_lead_plays(name: str, speaking: bool, queued: bool, runs_now: bool,
                                                 deferred: bool) -> None:
    from pipecat.processors.frame_processor import FrameDirection

    from yuki.voice.pipeline import answer_while_speaking

    agg = _Aggregator(speaking, queued)
    answer_while_speaking(agg)
    asyncio.run(agg._handle_function_call_result(SimpleNamespace(function_name=name)))
    assert bool(agg.pushed) is runs_now and agg.deferred is deferred
    if runs_now and speaking:
        assert agg.pushed == [FrameDirection.UPSTREAM]
    # the wrap is per result: a later call outside a result handler keeps Pipecat's rule
    agg2 = _Aggregator(True)
    answer_while_speaking(agg2)
    asyncio.run(agg2._maybe_push_context_after_function_result())
    assert agg2.deferred and not agg2.pushed


def test_the_conversation_wraps_its_assistant_aggregator() -> None:
    import inspect

    from yuki.voice import pipeline

    source = inspect.getsource(pipeline.Conversation)
    assert "settle_only_when_newest(assistant_agg)" in source and "answer_while_speaking(assistant_agg)" in source
    assert pipeline.ANSWER_WHILE_SPEAKING == frozenset({"quick_memory"})
