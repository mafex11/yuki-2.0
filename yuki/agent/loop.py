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

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Iterator

from yuki.agent.context import ContextManager
from yuki.agent.prompt import system_blocks
from yuki.agent.tools import (
    CONTROL_TOOLS,
    PERCEPTION_TOOLS,
    Backend,
    Dispatcher,
    ToolOutcome,
    resolve_tool_names,
    tool_params,
    unavailable_tool,
)
from yuki.config import Settings, resolve_effort, resolve_model
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
from yuki.log.requests import TOKEN_KEYS, append_request_row, usage_tokens


#: The API's limit on ``cache_control`` breakpoints in one request.
MAX_CACHE_BREAKPOINTS = 4


class Agent:
    """Yuki's brain: drives the model, runs tools, and talks to the caller.

    Several agents can share one process: each gets its own message history, its
    own tool subset and its own bit of role framing, while the frozen system
    prompt stays a byte-identical shared cached prefix for all of them.

    Args:
        settings: Runtime configuration. Held by reference, so mutating
            ``settings.model`` between requests switches model. Give each agent
            its own copy (``dataclasses.replace(settings)``) if they should not
            share a model and effort setting.
        logger: Session logger. One is created from ``settings`` if omitted.
        client: Anything exposing ``messages.create`` / ``messages.stream``.
            Defaults to a lazily-constructed :class:`anthropic.AnthropicBedrock`,
            so tests can stay offline.
        backend: Perception/action backend for the dispatcher.
        dispatcher: A fully built dispatcher, overriding ``backend``.
        tool_names: Restrict this agent to these tools. Only they are sent to the
            model, and a call to anything else comes back as "not available"
            without ever reaching the dispatcher. ``ask_user``, ``done`` and
            ``note_to_self`` are always included. Validated against the registry
            here, so a typo raises at construction rather than mid-task.
            ``None`` (the default) means every tool.
        extra_instructions: Role framing for *this* agent, sent as a second
            system block after the frozen prompt -- "a task is already running;
            you may look but not act", "you are the one who answers questions
            while the other Yuki works". Situational context about who this
            instance is, never a rule list: no keywords, no "if the user says X
            then Y", nothing that decides behaviour on the model's behalf. Behind
            its own cache breakpoint so wording differences between instances
            cannot cost the shared prompt its cache.
        prewarm: Do the two slow first-time things in a background thread at
            construction instead of in front of the user: open the persistent
            PowerShell session (2-4.7 s of cold start), and send one throwaway
            request that builds the Bedrock client and writes the prompt cache,
            so the first real request reads a warm cache. Harmless when there is
            no real desktop backend; pass ``False`` to keep construction from
            touching the machine or the network at all. Its tokens and cost go
            to a ``startup_cost`` record, never into a request's totals.
        lane: Which lane this agent is (``worker``, ``front_desk``, ``cli``),
            written into every ``request_summary`` and ``requests.csv`` line.
        record_requests: Append one line per finished request to
            ``settings.requests_csv_path``. The ``request_summary`` JSONL record
            is written either way.

    Raises:
        ValueError: If ``tool_names`` contains a name that is not a tool.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        logger: SessionLogger | None = None,
        *,
        client: Any | None = None,
        backend: Backend | None = None,
        dispatcher: Dispatcher | None = None,
        tool_names: Iterable[str] | None = None,
        extra_instructions: str | None = None,
        prewarm: bool = True,
        lane: str = "main",
        record_requests: bool = True,
    ) -> None:
        self.settings = settings or Settings()
        self.lane = lane
        self.record_requests = record_requests
        self.logger = logger or SessionLogger(self.settings.sessions_dir)
        self.dispatcher = dispatcher or Dispatcher(
            backend,
            screenshot_policy=self.settings.screenshot_policy,
            tool_timeout_s=self.settings.tool_timeout_s,
        )
        self.context = ContextManager(self.logger, keep_turns=self.settings.keep_perception_turns)
        self.tool_names = resolve_tool_names(tool_names)
        self.extra_instructions = (extra_instructions or "").strip() or None
        self._client = client
        self._client_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._pending_answer: str | None = None
        self._awaiting_answer = False
        self._prewarm_thread: threading.Thread | None = None
        # Per-request self-awareness facts (reset by run()): reported to the
        # model as plain facts, never used to steer the loop.
        self._request_started = time.monotonic()
        self._waited_s = 0.0
        self._model_calls = 0
        self._call_history: dict[str, list[tuple[str, str]]] = {}
        self._reset_accounting()
        #: The ``request_summary`` of the last request that ended (``None``
        #: while one is running). Read by the UI to label the finished card.
        self.last_summary: dict[str, Any] | None = None
        if self.tool_names is not None or self.extra_instructions:
            self.logger.log(
                "agent_scope",
                tools=list(self.tool_names) if self.tool_names is not None else None,
                extra_instructions=self.extra_instructions,
            )
        if prewarm:
            self._prewarm_thread = self._start_prewarm()

    # -- client ------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The Bedrock client, constructed on first use.

        Guarded by a lock because the pre-warm thread and the first real request
        can both reach for it at once, and importing ``anthropic`` plus resolving
        credentials is not something to do twice.
        """
        with self._client_lock:
            if self._client is None:
                import anthropic

                region = self.settings.aws_region or os.environ["AWS_REGION"]
                self._client = anthropic.AnthropicBedrock(aws_region=region)
            return self._client

    # -- warm-up -----------------------------------------------------------

    def _start_prewarm(self) -> threading.Thread:
        """Do both first-time warm-ups off the main thread.

        The model warm-up goes first because it is the one the user always pays
        for -- every request needs the model, only some need a shell.

        A daemon thread, so a process that exits before the warm-up finishes is
        not held open by it. Every failure is swallowed and logged: pre-warming is
        an optimisation, and a machine that cannot start PowerShell (or reach
        Bedrock) must still get a working Yuki that finds that out when it
        actually tries to use them.
        """

        def warm() -> None:
            self._warm_model()
            self._warm_shell()

        thread = threading.Thread(target=warm, name="yuki-prewarm", daemon=True)
        thread.start()
        return thread

    def _warm_shell(self) -> None:
        """Open the persistent PowerShell session now. Never raises."""
        started = time.perf_counter()
        try:
            live = self.dispatcher.prewarm_shell()
        except Exception as exc:
            self.logger.error(f"powershell prewarm failed: {type(exc).__name__}: {exc}", exc=exc)
            return
        self.logger.log(
            "shell_prewarm",
            live=live,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def _warm_model(self) -> None:
        """Build the client and write the prompt cache before the user asks.

        The first request of a process pays for three things nobody wants to wait
        for: importing ``anthropic``/``boto3``, resolving credentials, and having
        the API read a few thousand tokens of system prompt and tool schemas for
        the first time. This sends one deliberately pointless request with the
        *same* system blocks and tool definitions -- so the cached prefix is
        byte-identical -- and ``max_tokens`` of 1, so the model is cut off
        immediately and nothing is spent on output.

        Never raises, and never touches :attr:`~yuki.log.events.SessionLogger.usage`:
        this request is overhead, not part of any user request's accounting.
        """
        started = time.perf_counter()
        try:
            response = self.client.messages.create(**self._request_params(warmup=True))
        except Exception as exc:
            self.logger.error(f"model prewarm failed: {type(exc).__name__}: {exc}", exc=exc)
            return
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        model = self.settings.model
        usage = getattr(response, "usage", None)
        self.logger.log(
            "model_prewarm",
            model=model,
            elapsed_ms=elapsed_ms,
            stop_reason=getattr(response, "stop_reason", None),
            usage=usage,
        )
        tokens = usage_tokens(usage)
        cost = self.settings.estimate_cost(model, tokens)
        self.logger.log(
            "startup_cost",
            kind="model_prewarm",
            lane=self.lane,
            model=model,
            elapsed_s=round(elapsed_ms / 1000, 2),
            tokens=tokens,
            cost_usd=None if cost is None else round(cost, 6),
        )

    def wait_for_prewarm(self, timeout_s: float | None = None) -> bool:
        """Block until the pre-warm thread has finished (tests and diagnostics).

        Returns:
            True when no pre-warm is outstanding.
        """
        if self._prewarm_thread is None:
            return True
        self._prewarm_thread.join(timeout_s)
        return not self._prewarm_thread.is_alive()

    # -- caller controls ---------------------------------------------------

    def set_model(self, alias_or_id: str) -> str:
        """Switch model, effective on the next request.

        Safe from any thread: one attribute assignment, read once per request
        while building the parameters, so a switch never lands halfway through a
        call in flight.

        Args:
            alias_or_id: A :data:`yuki.config.MODEL_ALIASES` key such as
                ``opus``, or a full Bedrock model id (passed through untouched,
                so a model newer than this code still works).

        Returns:
            The resolved model id now in force.
        """
        previous = self.settings.model
        resolved = resolve_model(alias_or_id)
        self.settings.model = resolved
        self.logger.log(
            "model_switch", requested=alias_or_id, model=resolved, previous=previous
        )
        return resolved

    def set_effort(self, level: str) -> str:
        """Change how hard the model works, effective on the next request.

        Args:
            level: One of :data:`yuki.config.EFFORT_LEVELS`.

        Returns:
            The normalised level now in force.

        Raises:
            ValueError: On an unknown level, rather than letting the API reject
                the next request.
        """
        previous = self.settings.effort
        resolved = resolve_effort(level)
        self.settings.effort = resolved
        self.logger.log(
            "effort_switch", requested=level, effort=resolved, previous=previous
        )
        return resolved

    def answer(self, text: str) -> None:
        """Supply the answer to the question Yuki just asked.

        Call this after receiving an :class:`~yuki.log.events.AskUser` event and
        before resuming iteration.
        """
        if not self._awaiting_answer:
            raise RuntimeError("Agent is not waiting for an answer")
        self._pending_answer = text

    def cancel(self) -> None:
        """Ask the run to stop. Safe to call from any thread, at any time.

        The flag is a :class:`threading.Event`, so setting it needs no lock and
        is visible immediately to the thread driving the generator. That thread
        checks it at three points: before every model request, before dispatching
        every tool, and again after the ``tool_call`` event has been handed to the
        caller. The run then ends with ``error("Cancelled by the user.")``; any
        tool calls the model had already asked for are answered with a "not run"
        result, because the API insists on one result per call.

        A request or a tool already in flight is not interrupted -- there is no
        safe way to abort a keystroke halfway -- so cancelling lands at the next
        checkpoint rather than instantly.
        """
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        """Whether a cancellation is pending. Safe to read from any thread."""
        return self._cancelled.is_set()

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
        self._request_started = time.monotonic()
        self._waited_s = 0.0
        self._model_calls = 0
        self._call_history = {}
        self._reset_accounting()
        self.last_summary = None
        started_at = datetime.now()
        model, effort = self.settings.model, self.settings.effort
        self.logger.reset_usage()
        self.logger.begin_turn()
        self.logger.user_message(request)
        # How the request ended, for the summary. "abandoned" survives only when
        # the caller stopped iterating before a final or an error arrived.
        outcome = "abandoned"
        drive: Iterator[AgentEvent] | None = None
        try:
            overview, _ = self._capture_overview()
            self._close_dangling_tools(overview)
            self.context.add_request(request, overview)
            drive = self._drive()
            for event in drive:
                if isinstance(event, Final):
                    outcome = "final"
                elif isinstance(event, ErrorEvent):
                    outcome = "cancelled" if self._cancelled.is_set() else "error"
                yield event
        except KeyboardInterrupt:
            outcome = "cancelled"
            raise
        except Exception as exc:  # never let a crash escape into the REPL
            message = f"{type(exc).__name__}: {exc}"
            self.logger.error(message, exc=exc)
            outcome = "cancelled" if self._cancelled.is_set() else "error"
            yield ErrorEvent(message)
        finally:
            if drive is not None:
                drive.close()
            self.logger.usage_total()
            self._summarize(
                request, outcome=outcome, started_at=started_at, model=model, effort=effort
            )

    # -- per-request accounting --------------------------------------------

    def _reset_accounting(self) -> None:
        """Zero the time/token/cost accumulators for a new request."""
        self._model_s = 0.0
        self._tool_s = 0.0
        self._overview_s = 0.0
        self._tool_counts: Counter[str] = Counter()
        self._tokens: dict[str, int] = dict.fromkeys(TOKEN_KEYS, 0)
        self._cost_usd = 0.0
        self._unpriced: set[str] = set()
        self._models_used: list[str] = []

    def _account_response(self, model: str, usage: Any) -> None:
        """Fold one real (non-warm-up) response into this request's totals.

        Priced per call with the model that call actually used, so a model
        switch that lands mid-request is still costed correctly.
        """
        tokens = usage_tokens(usage)
        for key in TOKEN_KEYS:
            self._tokens[key] += tokens[key]
        if model not in self._models_used:
            self._models_used.append(model)
        cost = self.settings.estimate_cost(model, tokens)
        if cost is None:
            self._unpriced.add(model)
        else:
            self._cost_usd += cost

    def _summarize(
        self, request: str, *, outcome: str, started_at: datetime, model: str, effort: str
    ) -> None:
        """Write ``request_summary`` and the ``requests.csv`` line. Never raises.

        Wall time includes the time spent waiting for the user's answers, which
        is also reported on its own. Model time is every round trip (failed ones
        included); tool time is dispatching the tools the model called; the
        per-turn desktop overview is ``overview_s``. Whatever is left of the
        wall time is the loop's own overhead. Pre-warm calls are not in here:
        they are the separate ``startup_cost`` record.
        """
        try:
            wall_s = time.monotonic() - self._request_started
            models = self._models_used or [model]
            cost = None if self._unpriced else round(self._cost_usd, 6)
            summary: dict[str, Any] = {
                "request": request,
                "lane": self.lane,
                "outcome": outcome,
                "model": models[0] if len(models) == 1 else "+".join(models),
                "effort": effort,
                "started_at": started_at.isoformat(timespec="seconds"),
                "wall_s": round(wall_s, 2),
                "waiting_for_user_s": round(self._waited_s, 2),
                "model_s": round(self._model_s, 2),
                "tool_s": round(self._tool_s, 2),
                "overview_s": round(self._overview_s, 2),
                "model_calls": self._model_calls,
                "tool_calls": dict(self._tool_counts),
                "tokens": dict(self._tokens),
                "thinking_tokens": self.logger.usage.thinking_tokens,
                "cost_usd": cost,
                "unpriced_models": sorted(self._unpriced),
            }
            self.last_summary = summary
            self.logger.request_summary(summary)
            if self.record_requests:
                append_request_row(
                    self.settings.requests_csv_path,
                    {
                        "timestamp": summary["started_at"],
                        "session": self.logger.session_id,
                        "lane": self.lane,
                        "request": request,
                        "model": summary["model"],
                        "effort": effort,
                        "wall_s": summary["wall_s"],
                        "wait_s": summary["waiting_for_user_s"],
                        "model_s": summary["model_s"],
                        "tool_s": summary["tool_s"],
                        "calls": self._model_calls,
                        "tool_calls": summary["tool_calls"],
                        **self._tokens,
                        "cost_usd": cost,
                        "outcome": outcome,
                    },
                )
        except Exception as exc:  # accounting must never break a request
            self.logger.error(f"request summary failed: {type(exc).__name__}: {exc}", exc=exc)

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
                overview, facts = self._capture_overview()
                self.context.add_tool_results(
                    blocks, overview, tool_names=names, self_facts=facts
                )
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

        A response may carry ``done`` alongside real work -- "press mute, and with
        that I am finished" -- which is how a one-step request costs one round trip
        instead of two. ``done`` is therefore always executed last, whatever
        position the model put it in, and only if everything before it succeeded:
        a failure stops dispatch, so the ``done`` is dropped with an explanation
        and the model gets the results back to react to. It never gets to announce
        an outcome that did not happen.

        Args:
            tool_uses: The blocks from one assistant response, in model order.
            finished: Out-parameter; the ``done`` message is appended to it if the
                model ended its turn.

        Yields:
            Events for each call and result, and :class:`AskUser` when paused.

        Returns:
            ``(tool_result_blocks, tool_use_id -> name)``. The blocks are in the
            model's original order, because that is the order the API pairs them
            with the calls. Every block carries a result even when execution
            stopped early: the API requires one ``tool_result`` per ``tool_use``,
            so skipped tools get an error block explaining they never ran.
        """
        results: dict[str, dict[str, Any]] = {}
        names: dict[str, str] = {}
        stop_after = False
        order = [str(getattr(b, "id", "")) for b in tool_uses]
        work = [b for b in tool_uses if str(getattr(b, "name", "")) != "done"]
        closing = [b for b in tool_uses if str(getattr(b, "name", "")) == "done"]

        for block in work + closing:
            tool_id = str(getattr(block, "id", ""))
            name = str(getattr(block, "name", ""))
            tool_input = dict(getattr(block, "input", {}) or {})
            names[tool_id] = name

            if stop_after:
                results[tool_id] = self._skip(
                    tool_id, name, "Not run: an earlier tool in this turn failed."
                )
                continue

            if self._cancelled.is_set():
                results[tool_id] = self._skip(tool_id, name, "Not run: the user cancelled.")
                stop_after = True
                continue

            self.logger.tool_call(name, tool_input, tool_use_id=tool_id)
            yield ToolCall(name, tool_input)

            # The generator was suspended on that yield, which is exactly where a
            # cancel from the UI thread tends to land. The tool has not started,
            # so refuse it rather than touch the machine on the way out -- and
            # still emit a result, so a caller pairing calls with results does
            # not wait forever for one.
            if self._cancelled.is_set():
                text = "Not run: the user cancelled."
                results[tool_id] = self._skip(tool_id, name, text)
                yield ToolResult(name, False, text)
                stop_after = True
                continue

            dispatched = time.perf_counter()
            outcome = self._dispatch(name, tool_input)
            self._tool_s += time.perf_counter() - dispatched
            self._tool_counts[name] += 1

            if outcome.kind == "ask_user":
                question = outcome.control_input["question"]
                self.logger.ask_user(question)
                self._awaiting_answer = True
                self._pending_answer = None
                asked_at = time.monotonic()
                yield AskUser(question)  # generator pauses here
                self._waited_s += time.monotonic() - asked_at
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
                    results[tool_id] = self._result_block(
                        tool_id, [{"type": "text", "text": text}], is_error=True
                    )
                    stop_after = True
                    continue
                self.logger.user_answer(answer)
                self.logger.tool_result(
                    name, ok=True, summary=f"answered: {answer}", result={"answer": answer},
                    elapsed_ms=outcome.elapsed_ms, tool_use_id=tool_id,
                )
                yield ToolResult(name, True, f"answered: {answer}")
                results[tool_id] = self._result_block(
                    tool_id, [{"type": "text", "text": answer}]
                )
                continue

            if outcome.kind == "note":
                self.context.set_note(outcome.control_input["text"])

            if outcome.kind == "done":
                finished.append(outcome.control_input["message"])

            # Repetition facts for every real tool call the model made, looks
            # included (eleven identical look_at_window calls went unremarked
            # while only actions were counted). The per-turn automatic overview
            # never comes through here, so only explicit calls are counted.
            repeat_note = None
            if name not in CONTROL_TOOLS:
                repeats = self._record_call(name, tool_input, outcome.summary)
                repeat_note = _repeat_text(
                    name, repeats, perception=name in PERCEPTION_TOOLS, summary=outcome.summary
                )
                outcome.payload = (
                    {**outcome.payload, "self_facts": repeats}
                    if isinstance(outcome.payload, dict)
                    else {"result": outcome.payload, "self_facts": repeats}
                )

            self._log_outcome(outcome, tool_use_id=tool_id)
            yield ToolResult(outcome.name, outcome.ok, outcome.summary)

            content = list(outcome.content) or [
                {"type": "text", "text": outcome.summary or "Done."}
            ]
            if repeat_note:
                content.append({"type": "text", "text": repeat_note})
            results[tool_id] = self._result_block(tool_id, content, is_error=not outcome.ok)

            if not outcome.ok or outcome.kind == "done":
                stop_after = True

        return [results[tool_id] for tool_id in order], names

    def _dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        """Run one tool, refusing anything outside this agent's subset.

        The subset is enforced here rather than in the dispatcher because it
        belongs to the agent: two agents can share one dispatcher (and one
        PowerShell session) while showing the model different tools.
        """
        if self.tool_names is not None and name not in self.tool_names:
            return unavailable_tool(name, self.tool_names)
        return self.dispatcher.dispatch(name, tool_input)

    def _skip(self, tool_use_id: str, name: str, text: str) -> dict[str, Any]:
        """Answer a tool that never ran, and record that in the transcript.

        The API wants one ``tool_result`` per ``tool_use``, so a turn that stops
        early still has to say something about the calls it abandoned. Those
        answers are real content sent to the model, so they belong in the JSONL
        too -- otherwise a cancelled turn reads, in the log, as if the model was
        told nothing.

        Returns:
            The ``tool_result`` block to send for that call.
        """
        self.logger.tool_result(
            name,
            ok=False,
            summary=text,
            result={"skipped": True},
            elapsed_ms=0.0,
            tool_use_id=tool_use_id,
        )
        return self._result_block(tool_use_id, [{"type": "text", "text": text}], is_error=True)

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

    def _capture_overview(self) -> tuple[str, str | None]:
        """Grab the desktop overview handed to the model at the start of each turn.

        A failure here is reported to the model as text rather than raised: not
        being able to see the desktop is something Yuki should know about and work
        around, not a crash.

        Returns:
            ``(overview_text, self_facts_text)``. The second is the one-line
            request facts for the same situation block, ``None`` before the
            first model call of a request. Both are also written to the
            ``perception`` record (``payload.self_facts``).
        """
        facts = self._turn_facts()
        facts_text = _turn_facts_text(facts)
        looked = time.perf_counter()
        try:
            text, payload, elapsed = self.dispatcher.overview_text()
        except Exception as exc:
            self._overview_s += time.perf_counter() - looked
            message = f"(could not read the desktop: {type(exc).__name__}: {exc})"
            self.logger.error(message, exc=exc)
            self.logger.log("self_facts", **facts)
            return message, facts_text
        self._overview_s += time.perf_counter() - looked
        if isinstance(payload, dict):
            payload = {**payload, "self_facts": facts}
        self.logger.perception(
            "look_at_desktop", size_chars=len(text), elapsed_ms=elapsed, payload=payload
        )
        return text, facts_text

    # -- self-awareness facts ----------------------------------------------

    def _turn_facts(self) -> dict[str, Any]:
        """Facts about the running request itself: wall time and model calls."""
        return {
            "request_elapsed_s": round(time.monotonic() - self._request_started, 1),
            "waiting_for_user_s": round(self._waited_s, 1),
            "model_calls": self._model_calls,
        }

    def _record_call(self, name: str, tool_input: dict[str, Any], summary: str) -> dict[str, int]:
        """Record one explicit tool call this request and count its repeats.

        Actions and perception tools alike, keyed on tool name plus canonical
        input; control tools are not recorded.

        Returns:
            ``exact_calls``: calls of this tool with this exact input (canonical
            JSON) this request, this one included. ``exact_same_result``: how many
            of the earlier exact calls returned this same summary.
            ``other_input_same_result``: earlier calls of this tool with a
            different input that returned this same summary. ``tool_calls``:
            calls of this tool this request, this one included.
        """
        canonical = json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        history = self._call_history.setdefault(name, [])
        exact = [previous for key, previous in history if key == canonical]
        facts = {
            "exact_calls": len(exact) + 1,
            "exact_same_result": sum(previous == summary for previous in exact),
            "other_input_same_result": sum(
                key != canonical and previous == summary for key, previous in history
            ),
            "tool_calls": len(history) + 1,
        }
        history.append((canonical, summary))
        return facts

    # -- model request -----------------------------------------------------

    def _request_params(self, *, warmup: bool = False) -> dict[str, Any]:
        """Build the request parameters.

        Args:
            warmup: Build the throwaway pre-warm request instead of a real one:
                the same system blocks and tools (so the cached prefix matches
                byte for byte), one token of output, and a message that is never
                meant to be answered.

        Side effect (real requests only): the context places the moving
        conversation cache breakpoint, committed once the request succeeds.

        Returns:
            Keyword arguments for ``messages.create`` / ``messages.stream``.
        """
        system = system_blocks(extra=self.extra_instructions)
        tools = tool_params(
            names=self.tool_names,
            # The dispatcher's policy is the one that gates dispatch, so the
            # block the model sees must agree with it (never -> no
            # take_screenshot at all; the dispatcher's refusal is the backstop).
            screenshot_policy=getattr(
                self.dispatcher, "screenshot_policy", self.settings.screenshot_policy
            ),
        )
        if warmup:
            # No conversation breakpoint: the placeholder must never be cached.
            messages: list[dict[str, Any]] = [{"role": "user", "content": "warming up"}]
        else:
            # The moving conversation breakpoint takes whatever is left of the
            # API's 4 after the system blocks and the tools (see context.py).
            used = _count_breakpoints(system) + _count_breakpoints(tools)
            messages = self.context.request_messages(
                max_breakpoints=MAX_CACHE_BREAKPOINTS - used
            )
        return {
            "model": self.settings.model,
            "max_tokens": 1 if warmup else self.settings.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "adaptive", "display": self.settings.thinking_display},
            "output_config": {"effort": self.settings.effort},
        }

    def _request(self) -> Any:
        """Send one request and return the response, logging both sides in full."""
        params = self._request_params()
        self._model_calls += 1
        self.logger.log(
            "cache_breakpoints",
            system_tools=_count_breakpoints(params["system"])
            + _count_breakpoints(params["tools"]),
            messages=_count_breakpoints(params["messages"]),
            conversation=self.context.breakpoint_info(),
        )
        self.logger.llm_request(
            model=params["model"],
            system=params["system"],
            messages=params["messages"],
            tools=params["tools"],
            max_tokens=params["max_tokens"],
            thinking=params["thinking"],
            output_config=params["output_config"],
            stream=self.settings.stream,
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
            self._model_s += latency_ms / 1000
            self.logger.error(
                f"request failed after {latency_ms:.0f}ms: {type(exc).__name__}: {exc}", exc=exc
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        self._model_s += latency_ms / 1000
        self._account_response(params["model"], getattr(response, "usage", None))
        # The request went through, so its cache entry exists: the next prune
        # measures "before the breakpoint" against this one.
        self.context.commit_breakpoint()
        self.logger.llm_response(
            content=getattr(response, "content", []),
            stop_reason=getattr(response, "stop_reason", None),
            usage=getattr(response, "usage", None),
            latency_ms=latency_ms,
            stop_details=getattr(response, "stop_details", None),
        )
        return response


def _turn_facts_text(facts: dict[str, Any]) -> str | None:
    """One line for the situation block, or ``None`` before any model call."""
    calls = int(facts.get("model_calls") or 0)
    if calls <= 0:
        return None
    waited = float(facts.get("waiting_for_user_s") or 0.0)
    waiting = f" ({waited:.0f} s of it waiting for the user's answer)" if waited >= 1 else ""
    return (
        f"Request running {float(facts['request_elapsed_s']):.0f} s{waiting}, "
        f"{calls} model call{'' if calls == 1 else 's'} so far."
    )


def _repeat_text(
    name: str, facts: dict[str, int], *, perception: bool = False, summary: str = ""
) -> str | None:
    """The repetition fact appended to a tool result, or ``None`` when there is none.

    Args:
        name: Tool name.
        facts: From :meth:`Agent._record_call`.
        perception: The tool only looks. What is compared is the one-line summary
            (a tree's full text differs on every read, if only in its timing), so
            the fact names that summary rather than claiming identical content,
            and the "different input, same result" fact is left out: two
            different windows both having six elements says nothing.
        summary: This call's summary, quoted for perception tools.

    The text starts with a line break because the API joins it straight onto
    the end of the tool's own text block.
    """
    count = facts["exact_calls"]
    same = facts["exact_same_result"]
    others = 0 if perception else facts["other_input_same_result"]
    other_calls = f"{others} {name} call{'' if others == 1 else 's'} with different input"
    same_result = f'the same summary, "{summary}"' if perception and summary else "the same result"
    if count > 1:
        previous = count - 1
        if same == previous:
            tail = (
                f"the previous one returned {same_result}"
                if previous == 1
                else f"the previous {previous} returned {same_result}"
            )
        elif same == 0:
            tail = (
                "the previous one returned a different result"
                if previous == 1
                else f"none of the previous {previous} returned this result"
            )
        else:
            tail = f"{same} of the previous {previous} returned {same_result}"
        text = f"\n(this exact call has now been made {count} times this request; {tail}"
        if others:
            text += f"; {other_calls} also returned it"
        return text + ")"
    if others:
        return f"\n(earlier this request, {other_calls} returned this same result)"
    return None


def _count_breakpoints(value: Any) -> int:
    """Number of ``cache_control`` markers anywhere in a request part.

    Walks dicts and lists (system blocks, tool definitions, messages); SDK
    content objects in assistant turns never carry one, so they are skipped.
    """
    if isinstance(value, dict):
        own = 1 if value.get("cache_control") else 0
        return own + sum(
            _count_breakpoints(v) for k, v in value.items() if k != "cache_control"
        )
    if isinstance(value, (list, tuple)):
        return sum(_count_breakpoints(item) for item in value)
    return 0


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
