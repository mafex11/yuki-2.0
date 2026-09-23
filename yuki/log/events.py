"""Event schema, the session logger, and the agent's outward-facing events.

Two different notions of "event" live here on purpose:

* :class:`AgentEvent` and its subclasses are what :meth:`yuki.agent.loop.Agent.run`
  yields to its caller -- the small, UI-shaped stream.
* The methods on :class:`SessionLogger` write the audit trail: one JSON object
  per line, never truncated, with the full model request, the full response
  content, and every tool result.

Screenshot bytes are the single exception to "never truncated": PNG payloads are
written next to the JSONL as real files and referenced by path, per the
architecture contract.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from rich.console import Console

# ---------------------------------------------------------------------------
# Agent-facing events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thinking:
    """A summarised reasoning block from the model."""

    text: str
    kind: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class ToolCall:
    """The model asked for a tool."""

    name: str
    input: dict[str, Any]
    kind: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class ToolResult:
    """A tool finished (or failed)."""

    name: str
    ok: bool
    summary: str
    kind: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True)
class AskUser:
    """The agent needs an answer before it can continue.

    When this is yielded the generator is paused: call
    :meth:`yuki.agent.loop.Agent.answer` and resume iteration.
    """

    question: str
    kind: Literal["ask_user"] = "ask_user"


@dataclass(frozen=True)
class Final:
    """The agent's closing message for this request."""

    text: str
    kind: Literal["final"] = "final"


@dataclass(frozen=True)
class ErrorEvent:
    """Something went wrong; the run is over."""

    text: str
    kind: Literal["error"] = "error"


AgentEvent = Thinking | ToolCall | ToolResult | AskUser | Final | ErrorEvent

#: Every event type name that may appear in a session JSONL file. Anything
#: :meth:`SessionLogger.log` is called with should be in here, so a reader can
#: enumerate the schema instead of discovering it from real logs.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        # the conversation
        "session_start",
        "user_message",
        "llm_request",
        "llm_response",
        "thinking",
        "tool_call",
        "tool_result",
        "perception",
        "ask_user",
        "user_answer",
        "context_edit",
        "usage_total",
        # per-request time, tokens and estimated cost (also a line in requests.csv)
        "request_summary",
        "final",
        "error",
        # how this agent instance was set up and what it was told to be
        "agent_scope",
        # warm-ups done at construction, off the critical path
        "shell_prewarm",
        "model_prewarm",
        "startup_cost",
        # the caller changed a knob mid-session
        "model_switch",
        "effort_switch",
        # the desktop shell's own decisions (:class:`yuki.ui.uilog.UiLog`)
        "ui",
    }
)


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


@dataclass
class UsageTotals:
    """Running token/latency totals for one user request."""

    requests: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    wall_ms: float = 0.0

    def add(self, usage: Any, *, latency_ms: float) -> None:
        """Fold one response's ``usage`` object (or dict) into the totals."""
        data = _as_plain(usage)
        if not isinstance(data, dict):
            data = {}
        self.requests += 1
        self.input_tokens += int(data.get("input_tokens") or 0)
        self.cache_read_tokens += int(data.get("cache_read_input_tokens") or 0)
        self.cache_write_tokens += int(data.get("cache_creation_input_tokens") or 0)
        self.output_tokens += int(data.get("output_tokens") or 0)
        details = data.get("output_tokens_details") or {}
        if isinstance(details, dict):
            self.thinking_tokens += int(details.get("thinking_tokens") or 0)
        self.wall_ms += latency_ms

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for logging."""
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# JSON coercion
# ---------------------------------------------------------------------------


def _as_plain(value: Any) -> Any:
    """Best-effort conversion of SDK/dataclass objects to plain JSON types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _as_plain(v) for k, v in dataclasses.asdict(value).items()}
    if hasattr(value, "model_dump"):  # pydantic (anthropic SDK blocks)
        try:
            return _as_plain(value.model_dump())
        except Exception:  # pragma: no cover - defensive
            pass
    if isinstance(value, dict):
        return {str(k): _as_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_as_plain(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes__": len(value)}
    if hasattr(value, "__dict__"):
        return {k: _as_plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)


class SessionLogger:
    """Writes one JSONL session file and mirrors a readable stream to the console.

    The logger owns the turn counter: :meth:`begin_turn` is called once per model
    round-trip so every line can be traced back to a step in the conversation.
    """

    def __init__(
        self,
        sessions_dir: Path,
        *,
        console: Console | None = None,
        session_id: str | None = None,
        quiet: bool = False,
    ) -> None:
        """Open a new session file.

        Args:
            sessions_dir: Directory for ``<id>.jsonl`` and ``<id>/`` screenshots.
            console: Rich console to print to; a default one is made if omitted.
            session_id: Override the timestamp-based id (tests use this).
            quiet: Suppress console output but still write JSONL.
        """
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.sessions_dir / f"{self.session_id}.jsonl"
        self.shot_dir = self.sessions_dir / self.session_id
        self.turn = 0
        self.usage = UsageTotals()
        self.quiet = quiet
        self.console = console or Console(soft_wrap=True)
        self._fh: TextIO = self.path.open("a", encoding="utf-8")
        self._shot_count = 0
        # sha1(base64 png) -> saved path, so llm_request logging can swap the
        # inline payload for a file reference.
        self._shot_index: dict[str, str] = {}
        self.log("session_start", session_id=self.session_id, path=str(self.path))

    # -- lifecycle ---------------------------------------------------------

    def begin_turn(self) -> int:
        """Advance to the next turn and return its number."""
        self.turn += 1
        return self.turn

    def reset_usage(self) -> None:
        """Start a fresh per-request usage total."""
        self.usage = UsageTotals()

    def close(self) -> None:
        """Flush and close the JSONL file."""
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw writer --------------------------------------------------------

    def log(self, type: str, **fields: Any) -> dict[str, Any]:
        """Write one JSONL record and return it."""
        record: dict[str, Any] = {
            "ts": time.time(),
            "session": self.session_id,
            "turn": self.turn,
            "type": type,
        }
        record.update({k: _as_plain(v) for k, v in fields.items()})
        self._fh.write(json.dumps(record, ensure_ascii=False, default=_as_plain) + "\n")
        self._fh.flush()
        return record

    # -- screenshots -------------------------------------------------------

    def save_screenshot(self, png: bytes, b64: str | None = None) -> Path:
        """Write PNG bytes to the session's screenshot folder.

        Args:
            png: Raw PNG bytes.
            b64: The base64 form that will be sent to the model, if any. Given
                it, later ``llm_request`` logs replace the inline payload with
                this file's path instead of writing megabytes of base64.

        Returns:
            Path of the written file.
        """
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._shot_count += 1
        path = self.shot_dir / f"shot-{self._shot_count}.png"
        path.write_bytes(png)
        if b64:
            self._shot_index[hashlib.sha1(b64.encode()).hexdigest()] = str(path)
        return path

    def _dereference_images(self, value: Any) -> Any:
        """Recursively swap inline base64 image data for the saved file path."""
        if isinstance(value, dict):
            source = value.get("source")
            if value.get("type") == "image" and isinstance(source, dict) and "data" in source:
                data = str(source.get("data") or "")
                digest = hashlib.sha1(data.encode()).hexdigest()
                ref = self._shot_index.get(digest, f"<png {len(data)} b64 chars>")
                stripped = {k: v for k, v in source.items() if k != "data"}
                stripped["file"] = ref
                rest = {k: self._dereference_images(v) for k, v in value.items() if k != "source"}
                return {**rest, "source": stripped}
            return {k: self._dereference_images(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._dereference_images(v) for v in value]
        return value

    # -- typed events ------------------------------------------------------

    def user_message(self, text: str) -> None:
        """Log (and show) a new request from the user."""
        self.log("user_message", text=text)

    def llm_request(
        self,
        *,
        model: str,
        system: Any,
        messages: Any,
        tools: Any,
        max_tokens: int | None = None,
        thinking: Any = None,
        output_config: Any = None,
        stream: bool | None = None,
    ) -> None:
        """Log the complete outgoing request: the bulk *and* every knob.

        One record per request, so a reader never has to join two lines to know
        what was actually sent. Screenshot payloads inside ``messages`` are
        swapped for the file they were written to.

        Args:
            model: Model id.
            system: The ``system`` blocks.
            messages: The ``messages`` list.
            tools: The tool definitions.
            max_tokens: Output ceiling, when the caller sets one.
            thinking: The ``thinking`` parameter.
            output_config: The ``output_config`` parameter (effort, format).
            stream: Whether the streaming endpoint was used.
        """
        self.log(
            "llm_request",
            model=model,
            max_tokens=max_tokens,
            thinking=_as_plain(thinking),
            output_config=_as_plain(output_config),
            stream=stream,
            system=_as_plain(system),
            messages=self._dereference_images(_as_plain(messages)),
            tools=_as_plain(tools),
        )
        self._print(f"[dim]-> {model} ({_count_messages(messages)} messages)[/dim]")

    def llm_response(
        self,
        *,
        content: Any,
        stop_reason: str | None,
        usage: Any,
        latency_ms: float,
        stop_details: Any = None,
    ) -> None:
        """Log the complete response and fold its usage into the running total."""
        self.usage.add(usage, latency_ms=latency_ms)
        self.log(
            "llm_response",
            content=self._dereference_images(_as_plain(content)),
            stop_reason=stop_reason,
            stop_details=_as_plain(stop_details),
            usage=_as_plain(usage),
            latency_ms=round(latency_ms, 1),
        )
        plain = _as_plain(usage)
        if not isinstance(plain, dict):
            plain = {}
        self._print(
            f"[dim]<- {stop_reason} {latency_ms / 1000:.1f}s "
            f"in={plain.get('input_tokens', 0)} "
            f"cached={plain.get('cache_read_input_tokens', 0)} "
            f"out={plain.get('output_tokens', 0)}[/dim]"
        )

    def thinking(self, text: str) -> None:
        """Log and show a reasoning block (first 200 chars on the console)."""
        self.log("thinking", text=text)
        shown = " ".join(text.split())
        if len(shown) > 200:
            shown = shown[:200] + "..."
        if shown:
            self._print(f"[cyan]. {shown}[/cyan]")

    def tool_call(
        self, name: str, tool_input: dict[str, Any], *, tool_use_id: str | None = None
    ) -> None:
        """Log a tool invocation."""
        self.log("tool_call", name=name, input=tool_input, tool_use_id=tool_use_id)
        self._print(f"[yellow]-> {name}[/yellow] [dim]{_brief(tool_input)}[/dim]")

    def tool_result(
        self,
        name: str,
        *,
        ok: bool,
        summary: str,
        result: Any,
        elapsed_ms: float,
        tool_use_id: str | None = None,
    ) -> None:
        """Log the full result of a tool invocation."""
        self.log(
            "tool_result",
            name=name,
            ok=ok,
            summary=summary,
            result=self._dereference_images(_as_plain(result)),
            elapsed_ms=round(elapsed_ms, 1),
            tool_use_id=tool_use_id,
        )
        colour = "green" if ok else "red"
        mark = "ok" if ok else "!!"
        self._print(f"[{colour}]{mark} {name}[/] {summary} [dim]{elapsed_ms:.0f}ms[/dim]")

    def perception(self, kind: str, *, size_chars: int, elapsed_ms: float, payload: Any) -> None:
        """Log a perception snapshot in full, plus its size on the console."""
        self.log(
            "perception",
            kind=kind,
            size_chars=size_chars,
            elapsed_ms=round(elapsed_ms, 1),
            payload=self._dereference_images(_as_plain(payload)),
        )
        self._print(f"[dim]   {kind}: {size_chars} chars, {elapsed_ms:.0f}ms[/dim]")

    def ask_user(self, question: str) -> None:
        """Log a question put to the user."""
        self.log("ask_user", question=question)
        self._print(f"[bold magenta]? {question}[/bold magenta]")

    def user_answer(self, text: str) -> None:
        """Log the user's reply to a question."""
        self.log("user_answer", text=text)

    def context_edit(
        self,
        *,
        tool_use_id: str,
        name: str,
        from_turn: int,
        original_chars: int,
        stub: str,
    ) -> None:
        """Log one stale perception payload being replaced by a stub."""
        self.log(
            "context_edit",
            tool_use_id=tool_use_id,
            name=name,
            from_turn=from_turn,
            original_chars=original_chars,
            stub=stub,
        )
        self._print(
            f"[dim]   context: stubbed {name} from turn {from_turn} "
            f"({original_chars} chars)[/dim]"
        )

    def final(self, text: str) -> None:
        """Log and show the agent's closing message."""
        self.log("final", text=text)
        self._print(f"[bold green]yuki[/bold green] [bold]{text}[/bold]")

    def error(self, text: str, *, exc: BaseException | None = None) -> None:
        """Log an error, with a traceback when an exception is supplied."""
        tb = "".join(traceback.format_exception(exc)) if exc is not None else None
        self.log("error", text=text, traceback=tb)
        self._print(f"[bold red]error[/bold red] {text}")

    def usage_total(self) -> UsageTotals:
        """Log and print the per-request totals; returns them."""
        totals = self.usage
        self.log("usage_total", **totals.as_dict())
        self._print(
            f"[dim]-- {totals.requests} request(s) - in {totals.input_tokens} "
            f"- cached-read {totals.cache_read_tokens} "
            f"- cache-write {totals.cache_write_tokens} "
            f"- out {totals.output_tokens} (thinking {totals.thinking_tokens}) "
            f"- {totals.wall_ms / 1000:.1f}s[/dim]"
        )
        return totals

    def request_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Log the end-of-request time/token/cost record and one console line.

        Args:
            summary: Built by :meth:`yuki.agent.loop.Agent._summarize`.

        Returns:
            The written record.
        """
        record = self.log("request_summary", **summary)
        cost = summary.get("cost_usd")
        cost_text = "cost unknown" if cost is None else f"${cost:.4f}"
        waited = float(summary.get("waiting_for_user_s") or 0.0)
        waiting = f" (waited {waited:.1f}s for user)" if waited >= 0.05 else ""
        self._print(
            f"[dim]== {summary.get('outcome')} - {float(summary.get('wall_s') or 0):.1f}s{waiting} "
            f"- model {float(summary.get('model_s') or 0):.1f}s "
            f"- tools {float(summary.get('tool_s') or 0):.1f}s "
            f"- {summary.get('model_calls', 0)} call(s) - {cost_text}[/dim]"
        )
        return record

    # -- console -----------------------------------------------------------

    def _print(self, markup: str) -> None:
        if not self.quiet:
            self.console.print(markup, highlight=False)


def _count_messages(messages: Any) -> int:
    """Length of the message list, or ``-1`` if it has none."""
    try:
        return len(messages)
    except TypeError:  # pragma: no cover - defensive
        return -1


def _brief(value: Any, limit: int = 120) -> str:
    """One-line rendering of a tool input for the console."""
    try:
        text = json.dumps(_as_plain(value), ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        text = repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."
