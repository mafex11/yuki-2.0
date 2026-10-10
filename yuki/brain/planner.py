"""The planner call: one streamed Sonnet call per plan, its steps handed on as they are written (design 2.2, 2.3, 8).

One :class:`ModelCall` per model round trip:

* the request: the six tools (:mod:`yuki.brain.tools`), the three cached system blocks, the request's append-only
  messages; adaptive thinking; effort pinned per channel (``output_config.effort``) and never changed mid-request;
  ``tool_choice`` ``auto`` (forced tool use with thinking is not allowed on Sonnet 5 and is a 400 on Sonnet 5.5 /
  Opus 5.5: the core prompt says "your first call is plan") with ``disable_parallel_tool_use`` (one tool per response:
  reads run in parallel inside a plan instead); streamed, with ``eager_input_streaming`` on the tools of a streamed
  call (dropped for the rest of the app run if the endpoint refuses it with a 400);
* the stream is read through ``yuki.agent.failover`` (retries, failover, Settings -> AI routing), never directly;
* the ``plan`` tool's input is scanned as it streams (:class:`PlanScanner`, a JSON tokenizer over the fixed machine
  format): once ``steps`` begins, the fields before it are validated as the header; each step object is validated
  the moment it closes and handed to the executor through :class:`PlanStream` (the stream is read only while the
  executor asks for the next step, so a running step holds the rest of the response in the connection);
* ``stop_reason`` ``max_tokens`` or ``refusal`` stops every further step (the steps already run committed nothing:
  only non-committing steps run before the plan is complete, the executor's rule);
* events: :class:`Thinking` per thinking block, ``Partial`` for the thinking and a plain-text reply as they stream,
  ``Partial("now", goal)`` once the header is valid (the island's line), ``Partial("step", say)`` for a voice step's
  words as it is handed on.

Logs are content-free (counts, ms, tokens, stop reasons, code words).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from yuki.brain.plan import (
    PLAN_TOOL,
    Checks,
    Plan,
    PlanError,
    Step,
    finish_plan,
    parse_header,
    parse_step,
    probe_errors,
)
from yuki.brain.tools import tool_params
from yuki.log.events import AgentEvent, Partial, Thinking

#: The plan's fields before ``steps`` (all required by the schema): the header is validated early only when every one
#: came before ``steps``.
HEADER_KEYS: tuple[str, ...] = ("goal", "kind", "revises", "target", "route", "consent", "done", "question")
#: The fewest seconds between two ``Partial`` updates of the same kind (a handful of UI updates a second).
PARTIAL_GAP_S = 0.12

#: Set once the endpoint refused ``eager_input_streaming`` (a 400 on a call that carried it): the rest of this app run
#: sends no such field (the tool block then stays one byte form for the cache).
_eager_refused = threading.Event()


class CallCancelled(Exception):
    """The request was cancelled while the response streamed and nothing of it had run: the stream is dropped."""


# ---------------------------------------------------------------------------------------------------------------------
# The plan's JSON as it streams
# ---------------------------------------------------------------------------------------------------------------------


class PlanScanner:
    """A tokenizer over the ``plan`` input's JSON as it arrives (a fixed machine format, never words).

    :meth:`feed` returns what closed in the new text: ``("header", text)`` once, when the ``steps`` array opens (the
    object up to it, closed, for :func:`parse_header`), and ``("step", text)`` for each step object as its closing
    brace arrives. Strings and escapes are tracked, so braces inside a value never count."""

    def __init__(self) -> None:
        self.text = ""
        self._i = 0
        self._depth = 0
        self._in_str = False
        self._esc = False
        self._key_next = False
        self._key_at = -1
        self._key: str | None = None
        self._steps_key_at = -1
        self._in_steps = False
        self._elem_at = -1

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self.text += chunk or ""
        t, out = self.text, []
        while self._i < len(t):
            c = t[self._i]
            if self._in_str:
                if self._esc:
                    self._esc = False
                elif c == "\\":
                    self._esc = True
                elif c == '"':
                    self._in_str = False
                    if self._key_at >= 0:
                        try:
                            self._key = json.loads(t[self._key_at:self._i + 1])
                        except ValueError:
                            self._key = None
                        if self._key == "steps":
                            self._steps_key_at = self._key_at
                        self._key_at = -1
            elif c == '"':
                self._in_str = True
                if self._depth == 1 and self._key_next:
                    self._key_at, self._key_next = self._i, False
            elif c in "{[":
                self._depth += 1
                if self._depth == 1 and c == "{":
                    self._key_next = True
                elif self._depth == 2 and c == "[" and self._key == "steps" and not self._in_steps:
                    self._in_steps = True
                    head = t[:self._steps_key_at].rstrip().rstrip(",")
                    out.append(("header", head + "}"))
                elif self._depth == 3 and c == "{" and self._in_steps:
                    self._elem_at = self._i
            elif c in "}]":
                if self._depth == 3 and c == "}" and self._in_steps and self._elem_at >= 0:
                    out.append(("step", t[self._elem_at:self._i + 1]))
                    self._elem_at = -1
                elif self._depth == 2 and c == "]" and self._in_steps:
                    self._in_steps = False
                self._depth -= 1
            elif c == "," and self._depth == 1:
                self._key_next = True
            self._i += 1
        return out


# ---------------------------------------------------------------------------------------------------------------------
# The steps of one plan, as the executor takes them
# ---------------------------------------------------------------------------------------------------------------------


class PlanStream:
    """One plan's steps, validated as they close (``yuki.brain.plan.StepSource``).

    Built by :class:`ModelCall` when the ``plan`` tool's block starts. ``steps()`` pulls the stream only while the
    executor asks for the next step. A step that fails validation stops the stream's steps there (``stop`` =
    ``invalid``, ``errors`` say why); the whole plan is validated again at its end (:meth:`complete`)."""

    def __init__(self, call: "ModelCall", checks: Checks, *, on_header: Callable[[Plan], None] | None = None,
                 on_step: Callable[[Step], None] | None = None) -> None:
        self._call = call
        self.checks = checks
        self.plan_id = checks.plan_id
        self.stop: str | None = None
        self.errors: list[str] = []
        self._header: Plan | None = None
        self._head_text: str | None = None
        self._raw_steps: list[str] = []
        self._valid: list[Step] = []
        self._ready: deque[Step] = deque()
        self._taken = 0
        self._closed = False
        self._final: Plan | None = None
        self._final_done = False
        self._on_header = on_header
        self._on_step = on_step
        #: When the header and the first step were handed on (perf_counter), for the log.
        self.header_at: float | None = None
        self.first_step_at: float | None = None

    # -- fed by the call ----------------------------------------------------------------------------------------------

    def _feed(self, kind: str, text: str) -> None:
        if self.stop is not None:
            return
        if kind == "header":
            self._head_text = text
            self._try_header()
        elif kind == "step":
            self._raw_steps.append(text)
            self._try_steps()

    def _try_header(self) -> None:
        if self._header is not None or self._head_text is None:
            return
        try:
            raw = json.loads(self._head_text)
        except ValueError:
            return   # the fields were not all before steps: validated whole at the end
        if not isinstance(raw, Mapping) or any(k not in raw for k in HEADER_KEYS):
            return   # a field came after steps: the header is validated whole at the end
        try:
            self._header = parse_header(raw, self.checks)
        except PlanError as exc:
            self._fail(exc.errors)
            return
        self.header_at = time.perf_counter()
        if self._on_header is not None:
            self._on_header(self._header)
        self._try_steps()

    def _try_steps(self) -> None:
        if self._header is None:
            return
        while len(self._valid) < len(self._raw_steps) and self.stop is None:
            index = len(self._valid)
            try:
                raw = json.loads(self._raw_steps[index])
                step = parse_step(raw, index, self._header, self.checks, self._valid)
            except ValueError:
                self._fail([f"step {index + 1}: its JSON could not be read"])
                return
            except PlanError as exc:
                self._fail(exc.errors)
                return
            self._valid.append(step)
            self._ready.append(step)

    def _closed_input(self, text: str) -> None:
        """The plan block is complete: validate the whole input (also what streamed as steps). An invalid plan's report
        also carries every other probe problem in it (bWire merge: the streamed check stops at the first bad field,
        and one retry should fix them all)."""
        self._validate_whole(text)
        if self.stop == "invalid":
            try:
                raw = json.loads(text or "{}")
            except ValueError:
                raw = None
            for error in probe_errors(raw):
                if error not in self.errors:
                    self.errors.append(error)

    def _validate_whole(self, text: str) -> None:
        self._closed = True
        if self.stop is not None:
            return
        try:
            raw = json.loads(text or "{}")
        except ValueError:
            self._fail(["the plan's JSON could not be read"])
            return
        if not isinstance(raw, Mapping):
            self._fail(["the plan must be a JSON object"])
            return
        if self._header is None:
            try:
                self._header = parse_header(raw, self.checks)
            except PlanError as exc:
                self._fail(exc.errors)
                return
            self.header_at = time.perf_counter()
            if self._on_header is not None:
                self._on_header(self._header)
        items = raw.get("steps")
        if not isinstance(items, list):
            self._fail(["steps must be a list"])
            return
        # steps the stream did not hand on as objects (none in practice): validated now
        for index in range(len(self._valid), len(items)):
            try:
                step = parse_step(items[index], index, self._header, self.checks, self._valid)
            except PlanError as exc:
                self._fail(exc.errors)
                return
            self._valid.append(step)
            self._ready.append(step)
        try:
            plan = finish_plan(self._header, self._valid, self.checks)
        except PlanError as exc:
            self._fail(exc.errors)
            return
        if len(plan.steps) > len(self._valid):   # a skill route's own run_skill step, made by code
            synth = plan.steps[len(self._valid):]
            self._valid.extend(synth)
            self._ready.extend(synth)
        self._final = plan

    def _fail(self, errors: list[str]) -> None:
        self.stop = self.stop or "invalid"
        self.errors.extend(errors)
        self._ready.clear()

    def _stopped(self, why: str) -> None:
        """A stop from the stream (``max_tokens``, ``refusal``, ``cancelled``): no further step is handed on."""
        if self.stop is None:
            self.stop = why
        self._ready.clear()

    # -- the executor's side (StepSource) -----------------------------------------------------------------------------

    def header(self) -> Plan | None:
        return self._header

    def wait_header(self) -> Plan | None:
        """Read until the header is validated (or the plan stops): the Brain needs the plan's kind before it decides
        who runs it. Steps that close meanwhile wait, validated, for the executor."""
        while self._header is None and self.stop is None and self._pump():
            pass
        if self._header is None and self.stop is None:
            self._finish_stream()
        return self._header

    def steps(self) -> Iterator[Step]:
        while True:
            if self.stop is not None:
                return
            if self._call.cancelled():
                self._stopped("cancelled")
                return
            if self._ready:
                step = self._ready.popleft()
                self._taken += 1
                if self.first_step_at is None:
                    self.first_step_at = time.perf_counter()
                if self._on_step is not None:
                    self._on_step(step)
                yield step
                continue
            if self._final_done or (self._closed and not self._ready):
                return
            if not self._pump():
                self._finish_stream()
                if not self._ready:
                    return

    def complete(self) -> Plan | None:
        """Read to the end of the response (the plan block, then its stop reason: a refusal or a cut-off is known before
        any committing step runs); the whole validated plan, or ``None``."""
        while self.stop is None and self._pump():
            pass
        self._finish_stream()
        return self._final if self.stop is None else None

    def _pump(self) -> bool:
        """One event of the stream; a cancel ends this plan's steps (``stop`` = ``cancelled``), never raised to the
        executor."""
        try:
            return self._call.pump()
        except CallCancelled:
            self._stopped("cancelled")
            return False

    def _finish_stream(self) -> None:
        if self._final_done:
            return
        if not self._closed and self.stop is None:
            # the stream ended without the plan block closing (cut off)
            self._stopped(self._call.stop_reason if self._call.stop_reason in ("max_tokens", "refusal")
                          else "invalid")
            if self.stop == "invalid":
                self.errors.append("the plan was cut off")
        if self._call.stop_reason in ("max_tokens", "refusal"):
            self._stopped(self._call.stop_reason)
        self._final_done = True

    # -- for the Brain ------------------------------------------------------------------------------------------------

    @property
    def taken(self) -> int:
        """Steps handed to the executor so far."""
        return self._taken

    @property
    def opened_at(self) -> float:
        """When the model call was sent (perf_counter): the header's and first step's times are measured from it."""
        return float(getattr(self._call, "_started", 0.0) or 0.0)

    @property
    def plan(self) -> Plan | None:
        """The whole plan when it validated, else the header with the steps validated so far."""
        if self._final is not None:
            return self._final
        return self._header.with_steps(self._valid) if self._header is not None else None


# ---------------------------------------------------------------------------------------------------------------------
# One model call
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class ToolStart:
    """A ``tool_use`` block as it begins (``input`` is filled once the response is complete). ``bad_input``: a
    streamed input that did not parse as one JSON object (eager streaming hands on what the model wrote unchecked, and
    the SDK's tolerant parser may return it cut short): the tool is not run, it is answered as an error."""

    name: str
    id: str
    index: int
    input: dict[str, Any] = field(default_factory=dict)
    bad_input: bool = False


@dataclass
class CallResult:
    """The complete response of one call."""

    content: Any
    stop_reason: str | None
    tool: ToolStart | None
    text: str
    usage: Any
    ms: float
    first_token_ms: float | None
    model: str
    broken: bool = False


class ModelCall:
    """One planner round trip, streamed (or plain when ``stream`` is off).

    Args:
        client: The failover client (``messages.stream`` / ``messages.create``).
        params: The request parameters (built by :func:`build_params`).
        checks: Validation facts for a plan this call may hold (its plan id is reserved).
        emit: Pushes events to the caller (thread-safe).
        cancelled: Whether the request was cancelled.
        log: ``log(type, **fields)`` (content-free fields only).
        voice: Hand a voice step's words on as ``Partial("step", ...)``.
    """

    def __init__(self, client: Any, params: dict[str, Any], *, checks: Checks, emit: Callable[[AgentEvent], None],
                 cancelled: Callable[[], bool], log: Callable[..., Any], voice: bool = False,
                 stream: bool = True) -> None:
        self._client = client
        self.params = params
        self.checks = checks
        self._emit = emit
        self.cancelled = cancelled
        self._log = log
        self._voice = voice
        self._stream_mode = stream
        self._mgr: Any = None
        self._stream: Any = None
        self._iter: Iterator[Any] | None = None
        self._ended = False
        self._started = 0.0
        self.first_token_at: float | None = None
        self.stop_reason: str | None = None
        self.tool: ToolStart | None = None
        self.plan: PlanStream | None = None
        self._scanner: PlanScanner | None = None
        #: A non-plan tool's input as it streamed (re-parsed strictly at the end, as the plan is).
        self._tool_json: list[str] | None = None
        self._blocks: dict[int, dict[str, Any]] = {}
        self._text_parts: list[str] = []
        self._partial_at: dict[str, float] = {}
        self._final: Any = None
        self.broken = False

    # -- opening ------------------------------------------------------------------------------------------------------

    def open(self) -> None:
        """Send the request (the stream opens with its first event read by the failover client)."""
        self._started = time.perf_counter()
        if not self._stream_mode:
            self._final = self._client.messages.create(**self.params)
            self._replay_final()
            return
        try:
            self._open_stream()
        except Exception as exc:
            if not self._eager_in_params() or getattr(exc, "status_code", None) != 400:
                raise
            # the endpoint refused the field (an older serving stack): once without it, and never again this run
            _eager_refused.set()
            self.params = {**self.params, "tools": [{k: v for k, v in t.items() if k != "eager_input_streaming"}
                                                    for t in self.params.get("tools") or []]}
            self._log("brain_eager_refused", status=400)
            self._open_stream()

    def _eager_in_params(self) -> bool:
        return any(t.get("eager_input_streaming") for t in self.params.get("tools") or [] if isinstance(t, Mapping))

    def _open_stream(self) -> None:
        self._mgr = self._client.messages.stream(**self.params)
        self._stream = self._mgr.__enter__()
        self._iter = iter(self._stream)

    def close(self) -> None:
        """Drop the connection (after the final message was read, or on a cancel). Never raises."""
        mgr, self._mgr = self._mgr, None
        if mgr is not None:
            try:
                mgr.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # -- reading ------------------------------------------------------------------------------------------------------

    def pump(self) -> bool:
        """Read and handle one stream event; ``False`` once the response has ended."""
        if self._ended:
            return False
        if self._iter is None:
            self._ended = True
            return False
        if self.cancelled():
            # each request has its own message list, so a cancelled one needs nothing more of this response
            raise CallCancelled()
        try:
            event = next(self._iter)
        except StopIteration:
            self._ended = True
            return False
        except ValueError:
            # eager streaming: the SDK could not parse a tool input at all (no tool_use id to answer: re-issued)
            self.broken = True
            self._ended = True
            if self.plan is not None:
                self.plan._fail(["the plan's JSON could not be read"])
            self._log("brain_stream_broken", reason="tool_input_json")
            return False
        self._handle(event)
        return True

    def next_tool(self) -> ToolStart | None:
        """Read until a ``tool_use`` block begins (returned) or the response ends (``None``)."""
        while self.tool is None and self.pump():
            pass
        return self.tool

    def _partial(self, what: str, text: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._partial_at.get(what, 0.0) < PARTIAL_GAP_S:
            return
        self._partial_at[what] = now
        self._emit(Partial(what, text))

    def _handle(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        if self.first_token_at is None and kind in ("content_block_start", "content_block_delta"):
            self.first_token_at = time.perf_counter()
        if kind == "content_block_start":
            index = int(getattr(event, "index", 0) or 0)
            block = getattr(event, "content_block", None)
            btype = getattr(block, "type", None)
            self._blocks[index] = {"type": btype, "text": []}
            if btype == "tool_use" and self.tool is None:
                self.tool = ToolStart(name=str(getattr(block, "name", "") or ""), id=str(getattr(block, "id", "") or ""),
                                      index=index)
                if self.tool.name == PLAN_TOOL:
                    self._start_plan()
        elif kind == "content_block_delta":
            index = int(getattr(event, "index", 0) or 0)
            delta = getattr(event, "delta", None)
            dtype = getattr(delta, "type", None)
            slot = self._blocks.setdefault(index, {"type": None, "text": []})
            if dtype == "thinking_delta":
                slot["text"].append(str(getattr(delta, "thinking", "") or ""))
                self._partial("thinking", "".join(slot["text"]))
            elif dtype == "text_delta":
                piece = str(getattr(delta, "text", "") or "")
                slot["text"].append(piece)
                self._text_parts.append(piece)
                if self.tool is None:
                    self._partial("reply", "".join(self._text_parts))
            elif dtype == "input_json_delta" and self.tool is not None and index == self.tool.index:
                piece = str(getattr(delta, "partial_json", "") or "")
                if self._scanner is not None:
                    for found, text in self._scanner.feed(piece):
                        if self.plan is not None:
                            self.plan._feed(found, text)
                else:
                    if self._tool_json is None:
                        self._tool_json = []
                    self._tool_json.append(piece)
        elif kind == "content_block_stop":
            index = int(getattr(event, "index", 0) or 0)
            slot = self._blocks.get(index) or {}
            if slot.get("type") == "thinking":
                text = "".join(slot.get("text") or [])
                if text.strip():
                    self._emit(Thinking(text))
            elif slot.get("type") == "tool_use" and self.tool is not None and index == self.tool.index \
                    and self.plan is not None and self._scanner is not None:
                self.plan._closed_input(self._scanner.text)
        elif kind == "message_delta":
            reason = getattr(getattr(event, "delta", None), "stop_reason", None)
            if reason:
                self.stop_reason = str(reason)
                if self.plan is not None and self.stop_reason in ("max_tokens", "refusal"):
                    self.plan._stopped(self.stop_reason)
        elif kind == "message_stop":
            self._ended = True

    def _start_plan(self) -> None:
        self._scanner = PlanScanner()
        self.plan = PlanStream(self, self.checks, on_header=self._header_ready, on_step=self._step_out)

    def _header_ready(self, plan: Plan) -> None:
        self._partial("now", plan.goal, force=True)

    def _step_out(self, step: Step) -> None:
        if self._voice and step.say:
            self._partial("step", step.say, force=True)

    def _strict_tool_input(self) -> None:
        """A non-plan tool's streamed input, parsed strictly (the plan's own blocks are, by :class:`PlanStream`): what
        does not parse as one JSON object marks the tool ``bad_input`` instead of running on a cut-short value."""
        tool, parts = self.tool, self._tool_json
        if tool is None or tool.name == PLAN_TOOL or parts is None:
            return
        text = "".join(parts).strip()
        if not text:
            return   # nothing streamed for it: the SDK's input stands (an empty object)
        try:
            strict = json.loads(text)
        except ValueError:
            strict = None
        if isinstance(strict, dict):
            tool.input = strict
            return
        tool.bad_input = True
        self._log("brain_tool_input_invalid", tool=tool.name, chars=len(text))

    def _replay_final(self) -> None:
        """Plain (non-streamed) mode: the whole response at once, handled as if streamed."""
        final = self._final
        self.first_token_at = time.perf_counter()
        self.stop_reason = getattr(final, "stop_reason", None)
        for index, block in enumerate(getattr(final, "content", None) or []):
            btype = getattr(block, "type", None)
            if btype == "thinking" and str(getattr(block, "thinking", "") or "").strip():
                self._emit(Thinking(str(block.thinking)))
            elif btype == "text":
                self._text_parts.append(str(getattr(block, "text", "") or ""))
            elif btype == "tool_use" and self.tool is None:
                self.tool = ToolStart(name=str(block.name), id=str(block.id), index=index,
                                      input=dict(getattr(block, "input", None) or {}))
                if self.tool.name == PLAN_TOOL:
                    self._start_plan()
                    text = json.dumps(self.tool.input, ensure_ascii=False)
                    for found, part in self._scanner.feed(text):   # type: ignore[union-attr]
                        self.plan._feed(found, part)                   # type: ignore[union-attr]
                    self.plan._closed_input(text)                      # type: ignore[union-attr]
                    if self.stop_reason in ("max_tokens", "refusal"):
                        self.plan._stopped(self.stop_reason)           # type: ignore[union-attr]
        self._ended = True

    def finish(self) -> CallResult:
        """Read the rest of the response and return it whole (the conversation appends ``content`` unchanged)."""
        while self.pump():
            pass
        final = self._final
        if final is None and self._stream is not None and not self.broken:
            try:
                final = self._stream.get_final_message()
            except ValueError:
                self.broken = True
                final = None
            self._final = final
        self.close()
        if self.plan is not None:
            self.plan._finish_stream()
        content = getattr(final, "content", None) or []
        if self.tool is not None and not self.tool.input:
            for block in content:
                if getattr(block, "type", None) == "tool_use" and str(getattr(block, "id", "")) == self.tool.id:
                    raw = getattr(block, "input", None)
                    self.tool.input = dict(raw) if isinstance(raw, Mapping) else {}
        self._strict_tool_input()
        text = "".join(str(getattr(b, "text", "") or "") for b in content if getattr(b, "type", None) == "text")
        if self.stop_reason is None:
            self.stop_reason = getattr(final, "stop_reason", None)
        return CallResult(content=content, stop_reason=self.stop_reason, tool=self.tool,
                          text=text or "".join(self._text_parts), usage=getattr(final, "usage", None),
                          ms=(time.perf_counter() - self._started) * 1000,
                          first_token_ms=None if self.first_token_at is None
                          else (self.first_token_at - self._started) * 1000,
                          model=str(self.params.get("model") or ""), broken=self.broken or final is None)


def build_params(*, model: str, max_tokens: int, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 effort: str, thinking_display: str, stream: bool, eager: bool) -> dict[str, Any]:
    """The planner request (design 2.2, 6): six tools, three cached system blocks, the request's messages, adaptive
    thinking, the channel's pinned effort, ``tool_choice`` auto with one tool per response."""
    return {
        "model": model,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": messages,
        "tools": tool_params(streamed=stream, eager=eager and not _eager_refused.is_set()),
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "thinking": {"type": "adaptive", "display": thinking_display},
        "output_config": {"effort": effort},
    }


__all__ = ["PlanScanner", "PlanStream", "ToolStart", "CallResult", "ModelCall", "CallCancelled", "build_params",
           "PARTIAL_GAP_S"]
