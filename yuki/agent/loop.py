"""The agent loop.

:meth:`Agent.run` is a generator so the caller can stay in control: it yields
what happened as it happens, and when Yuki needs an answer it yields
:class:`~yuki.log.events.AskUser` and simply stops there. The caller supplies the
answer with :meth:`Agent.answer` and resumes iterating; the messages, the running
note and the turn counter all survive the pause because they live on the agent,
not in the generator's locals.

One :class:`Agent` spans the whole session: every new request appends to the same
conversation.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterator

from yuki.agent.context import ContextManager
from yuki.agent.prompt import system_blocks
from yuki.agent.tools import Backend, Dispatcher, ToolOutcome, tool_params
from yuki.config import Settings
from yuki.log.events import (
    AgentEvent,
    AskUser,
    ErrorEvent,
    Final,
    SessionLogger,
    Thinking,
    ToolCall,
    ToolResult,
)


class Agent:
    """Yuki's brain: drives the model, runs tools, and talks to the caller.

    Args:
        settings: Runtime configuration. Held by reference, so mutating
            ``settings.model`` between requests switches model.
        logger: Session logger. One is created from ``settings`` if omitted.
        client: Anything exposing ``messages.create`` / ``messages.stream``.
            Defaults to a lazily-constructed :class:`anthropic.AnthropicBedrock`,
            so tests can stay offline.
        backend: Perception/action backend for the dispatcher.
        dispatcher: A fully built dispatcher, overriding ``backend``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        logger: SessionLogger | None = None,
        *,
        client: Any | None = None,
        backend: Backend | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.logger = logger or SessionLogger(self.settings.sessions_dir)
        self.dispatcher = dispatcher or Dispatcher(
            backend,
            screenshot_policy=self.settings.screenshot_policy,
            tool_timeout_s=self.settings.tool_timeout_s,
        )
        self.context = ContextManager(self.logger, keep_turns=self.settings.keep_perception_turns)
        self._client = client
        self._cancelled = threading.Event()
        self._pending_answer: str | None = None
        self._awaiting_answer = False

    # -- client ------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The Bedrock client, constructed on first use."""
        if self._client is None:
            import anthropic

            region = self.settings.aws_region or os.environ["AWS_REGION"]
            self._client = anthropic.AnthropicBedrock(aws_region=region)
        return self._client

    # -- caller controls ---------------------------------------------------

    def answer(self, text: str) -> None:
        """Supply the answer to the question Yuki just asked.

        Call this after receiving an :class:`~yuki.log.events.AskUser` event and
        before resuming iteration.
        """
        if not self._awaiting_answer:
            raise RuntimeError("Agent is not waiting for an answer")
        self._pending_answer = text

    def cancel(self) -> None:
        """Ask the run to stop.

        Takes effect at the next checkpoint -- before the next model request or
        the next tool -- so an in-flight API call or tool still completes.
        """
        self._cancelled.set()

    @property
    def awaiting_answer(self) -> bool:
        """Whether the agent is paused on a question."""
        return self._awaiting_answer

    # -- main entry point --------------------------------------------------

    def run(self, request: str) -> Iterator[AgentEvent]:
        """Handle one user request, yielding events as work happens.

        Args:
            request: What the user typed.

        Yields:
            :class:`~yuki.log.events.AgentEvent` values. The generator pauses on
            :class:`~yuki.log.events.AskUser` and finishes after exactly one
            :class:`~yuki.log.events.Final` or :class:`~yuki.log.events.ErrorEvent`.
        """
        self._cancelled.clear()
        self._pending_answer = None
        self._awaiting_answer = False
        self.logger.reset_usage()
        self.logger.begin_turn()
        self.logger.user_message(request)
        try:
            overview = self._capture_overview()
            self._close_dangling_tools(overview)
            self.context.add_request(request, overview)
            yield from self._drive()
        except Exception as exc:  # never let a crash escape into the REPL
            message = f"{type(exc).__name__}: {exc}"
            self.logger.error(message, exc=exc)
            yield ErrorEvent(message)
        finally:
            self.logger.usage_total()

    def _close_dangling_tools(self, overview: str) -> None:
        """Answer tool calls left hanging by a cancelled or interrupted run.

        Without this the next request would carry a ``tool_use`` block with no
        matching ``tool_result`` and the API would reject it.
        """
        dangling = self.context.dangling_tool_uses()
        if not dangling:
            return
        blocks = [
            self._result_block(
                tool_id,
                [{"type": "text", "text": "Not run: the previous request was interrupted."}],
                is_error=True,
            )
            for tool_id, _ in dangling
        ]
        self.logger.log(
            "context_edit",
            reason="closed_dangling_tool_uses",
            tools=[name for _, name in dangling],
        )
        self.context.add_tool_results(blocks, overview, tool_names=dict(dangling))

    # -- the loop ----------------------------------------------------------

    def _drive(self) -> Iterator[AgentEvent]:
        """Round-trip with the model until it finishes, asks, or fails."""
        for step in range(1, self.settings.max_steps + 1):
            if self._cancelled.is_set():
                yield self._cancel_event()
                return
            if step > 1:
                self.logger.begin_turn()

            self.context.prune()
            response = self._request()
            self.context.add_assistant(response.content)

            for block in response.content:
                if getattr(block, "type", None) == "thinking":
                    text = getattr(block, "thinking", "") or ""
                    if text.strip():
                        self.logger.thinking(text)
                        yield Thinking(text)

            stop_reason = getattr(response, "stop_reason", None)

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                message = "The model declined this request"
                category = getattr(details, "category", None)
                if category:
                    message += f" ({category})"
                self.logger.error(message)
                yield ErrorEvent(message)
                return

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

            if stop_reason == "max_tokens" and not tool_uses:
                message = "The model hit the output limit before finishing."
                self.logger.error(message)
                yield ErrorEvent(message)
                return

            if tool_uses:
                finished: list[str] = []
                blocks, names = yield from self._run_tools(tool_uses, finished)
                self.context.add_tool_results(blocks, self._capture_overview(), tool_names=names)
                if finished:
                    self.logger.final(finished[0])
                    yield Final(finished[0])
                    return
                continue

            if stop_reason == "pause_turn":
                continue

            # end_turn (or anything else) with no tool call: the model answered in
            # plain text without calling done. Treat that text as the final word.
            text = "\n".join(
                getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
            ).strip()
            if text:
                self.logger.final(text)
                yield Final(text)
            else:
                message = f"The model stopped with nothing to say (stop_reason={stop_reason})."
                self.logger.error(message)
                yield ErrorEvent(message)
            return

        message = f"Gave up after {self.settings.max_steps} steps without finishing."
        self.logger.error(message)
        yield ErrorEvent(message)

    def _cancel_event(self) -> ErrorEvent:
        """Record a cancellation in the transcript and build the event for it."""
        message = "Cancelled by the user."
        self.logger.error(message)
        self.context.add_note("The user cancelled that request. Stop and await new instructions.")
        return ErrorEvent(message)

    # -- tools -------------------------------------------------------------

    def _run_tools(
        self, tool_uses: list[Any], finished: list[str]
    ) -> Any:
        """Execute every ``tool_use`` block in order, stopping at the first failure.

        Args:
            tool_uses: The blocks from one assistant response, in model order.
            finished: Out-parameter; the ``done`` message is appended to it if the
                model ended its turn.

        Yields:
            Events for each call and result, and :class:`AskUser` when paused.

        Returns:
            ``(tool_result_blocks, tool_use_id -> name)``. Every block carries a
            result even when execution stopped early: the API requires one
            ``tool_result`` per ``tool_use``, so skipped tools get an error block
            explaining they never ran.
        """
        blocks: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        stop_after = False

        for block in tool_uses:
            tool_id = str(getattr(block, "id", ""))
            name = str(getattr(block, "name", ""))
            tool_input = dict(getattr(block, "input", {}) or {})
            names[tool_id] = name

            if stop_after:
                blocks.append(
                    self._result_block(
                        tool_id,
                        [
                            {
                                "type": "text",
                                "text": "Not run: an earlier tool in this turn failed.",
                            }
                        ],
                        is_error=True,
                    )
                )
                continue

            if self._cancelled.is_set():
                blocks.append(
                    self._result_block(
                        tool_id,
                        [{"type": "text", "text": "Not run: the user cancelled."}],
                        is_error=True,
                    )
                )
                stop_after = True
                continue

            self.logger.tool_call(name, tool_input, tool_use_id=tool_id)
            yield ToolCall(name, tool_input)

            outcome = self.dispatcher.dispatch(name, tool_input)

            if outcome.kind == "ask_user":
                question = outcome.control_input["question"]
                self.logger.ask_user(question)
                self._awaiting_answer = True
                self._pending_answer = None
                yield AskUser(question)  # generator pauses here
                self._awaiting_answer = False
                answer = self._pending_answer
                self._pending_answer = None
                if answer is None:
                    text = "The user did not answer."
                    self.logger.tool_result(
                        name, ok=False, summary=text, result={"answer": None},
                        elapsed_ms=outcome.elapsed_ms, tool_use_id=tool_id,
                    )
                    yield ToolResult(name, False, text)
                    blocks.append(
                        self._result_block(tool_id, [{"type": "text", "text": text}], is_error=True)
                    )
                    stop_after = True
                    continue
                self.logger.user_answer(answer)
                self.logger.tool_result(
                    name, ok=True, summary=f"answered: {answer}", result={"answer": answer},
                    elapsed_ms=outcome.elapsed_ms, tool_use_id=tool_id,
                )
                yield ToolResult(name, True, f"answered: {answer}")
                blocks.append(self._result_block(tool_id, [{"type": "text", "text": answer}]))
                continue

            if outcome.kind == "note":
                self.context.set_note(outcome.control_input["text"])

            if outcome.kind == "done":
                finished.append(outcome.control_input["message"])

            self._log_outcome(outcome, tool_use_id=tool_id)
            yield ToolResult(outcome.name, outcome.ok, outcome.summary)

            content = outcome.content or [
                {"type": "text", "text": outcome.summary or "Done."}
            ]
            blocks.append(self._result_block(tool_id, content, is_error=not outcome.ok))

            if not outcome.ok:
                stop_after = True
            if outcome.kind == "done":
                stop_after = True

        return blocks, names

    def _log_outcome(self, outcome: ToolOutcome, *, tool_use_id: str) -> None:
        """Write the screenshot file, the perception record, and the tool result."""
        if outcome.screenshot_png is not None:
            path = self.logger.save_screenshot(outcome.screenshot_png, outcome.screenshot_b64)
            outcome.payload = {**outcome.payload, "file": str(path)}
        if outcome.is_perception and outcome.ok:
            self.logger.perception(
                outcome.name,
                size_chars=_content_chars(outcome.content),
                elapsed_ms=outcome.elapsed_ms,
                payload=outcome.payload,
            )
        self.logger.tool_result(
            outcome.name,
            ok=outcome.ok,
            summary=outcome.summary,
            result=outcome.payload,
            elapsed_ms=outcome.elapsed_ms,
            tool_use_id=tool_use_id,
        )

    @staticmethod
    def _result_block(
        tool_use_id: str, content: list[dict[str, Any]], *, is_error: bool = False
    ) -> dict[str, Any]:
        """Build one ``tool_result`` block."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    # -- perception --------------------------------------------------------

    def _capture_overview(self) -> str:
        """Grab the desktop overview handed to the model at the start of each turn.

        A failure here is reported to the model as text rather than raised: not
        being able to see the desktop is something Yuki should know about and work
        around, not a crash.
        """
        try:
            text, payload, elapsed = self.dispatcher.overview_text()
        except Exception as exc:
            message = f"(could not read the desktop: {type(exc).__name__}: {exc})"
            self.logger.error(message, exc=exc)
            return message
        self.logger.perception(
            "look_at_desktop", size_chars=len(text), elapsed_ms=elapsed, payload=payload
        )
        return text

    # -- model request -----------------------------------------------------

    def _request(self) -> Any:
        """Send one request and return the response, logging both sides in full."""
        params: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": system_blocks(),
            "messages": self.context.messages,
            "tools": tool_params(),
            "thinking": {"type": "adaptive", "display": self.settings.thinking_display},
        }
        self.logger.llm_request(
            model=params["model"],
            system=params["system"],
            messages=params["messages"],
            tools=params["tools"],
        )
        started = time.perf_counter()
        try:
            if self.settings.stream:
                with self.client.messages.stream(**params) as stream:
                    response = stream.get_final_message()
            else:
                response = self.client.messages.create(**params)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            self.logger.error(
                f"request failed after {latency_ms:.0f}ms: {type(exc).__name__}: {exc}", exc=exc
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        self.logger.llm_response(
            content=getattr(response, "content", []),
            stop_reason=getattr(response, "stop_reason", None),
            usage=getattr(response, "usage", None),
            latency_ms=latency_ms,
            stop_details=getattr(response, "stop_details", None),
        )
        return response


def _content_chars(content: list[dict[str, Any]]) -> int:
    """Character size of a tool result's content blocks."""
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            total += len(block.get("text") or "")
        elif block.get("type") == "image":
            source = block.get("source") or {}
            total += len(str(source.get("data") or ""))
    return total
