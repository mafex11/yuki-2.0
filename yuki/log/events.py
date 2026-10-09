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
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from rich.console import Console
from rich.errors import MarkupError
from rich.markup import escape

from yuki.memory import secret_filter

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
    """The model asked for a tool. For an action, ``input`` also carries the agent's ``_about`` (what a person can read
    about the target: ``yuki.agent.tools.STEP_ABOUT_FIELD``) next to the brain's ``what``: the UI's step words."""

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
    #: The tool's structured result (for the UI to name what it worked on); not compared or printed.
    payload: Any = field(default=None, compare=False, repr=False)
    #: The step's words for the UI (revamp 38 §8c), the same as on its call: the brain's ``what`` and the agent's
    #: ``_about`` (the target's names, sends / changes); never the command, typed text or address.
    about: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


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
    """The agent's closing message for this request.

    ``outcome``: the request's one outcome verdict (``yuki.agent.finish.outcome_verdict``: level, what was seen, the
    send's confirmation, the line said by code below ``likely``), or ``None`` (a stand-in agent). The voice, the done
    card, the island and the window repeat its level and never say more than it."""

    text: str
    kind: Literal["final"] = "final"
    outcome: dict[str, Any] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ErrorEvent:
    """Something went wrong; the run is over."""

    text: str
    kind: Literal["error"] = "error"


@dataclass(frozen=True)
class Say:
    """Something the agent wants the user to hear while it keeps working (the ``say`` tool).

    The run does not pause: it is a progress line, not a question. Voice
    speaks it through the mouth; text mode shows it in the status strip.
    """

    text: str
    kind: Literal["say"] = "say"


@dataclass(frozen=True)
class Partial:
    """What is being written right now, for the chat (yuki.agent.stream_partial): ``what`` is ``reply`` (the
    reply so far) or ``thinking`` (the thinking summary so far). Not logged; the finished text still comes
    as :class:`Final`."""

    what: str
    text: str
    kind: Literal["partial"] = "partial"


@dataclass(frozen=True)
class SendPending:
    """A message is about to go to a person: the send countdown (the user's 3 Oct "Sending to X ... 3 s to stop").

    Yielded by the agent loop for a plain send that passed the situation check and the judge, only when its caller
    opted in (``Agent.send_countdown_s``). ``state``: ``counting`` (the card opens, ``seconds`` to stop or swap),
    ``going`` (the time ran out: the send runs now), ``stopped`` (cancelled in the window: nothing sent),
    ``swapped`` (a chip was tapped: the call is not run; the countdown starts again when the send is ready for the new
    person or place). ``typed``: the text is already in the box (a key or a click sends it), so a stop leaves it
    there unsent. Never logged with its text."""

    state: str
    who: str = ""
    where: str = ""
    app: str = ""
    text: str = ""
    seconds: float = 0.0
    who_alts: tuple[str, ...] = ()
    where_alts: tuple[str, ...] = ()
    typed: bool = False
    from_memory: bool = False
    #: The title of the window the text goes into (the chat it shows, as the app writes it; review F1); "" on a swap.
    window: str = ""
    kind: Literal["send_pending"] = "send_pending"


@dataclass(frozen=True)
class Paused:
    """appTakeover: the user took over the keyboard or mouse while the request acted, so it is paused (or goes on
    again). ``state``: ``paused`` (no new action starts; ``why``: ``keys`` | ``click`` | ``mouse``), ``resumed`` (it
    read the screen again and goes on; ``seconds``: how long it was paused), or ``ended`` (nobody resumed it within the
    limit: it stops quietly, the stop follows). Yuki never speaks for it; the island and the run card show it."""

    state: str
    why: str = ""
    seconds: float = 0.0
    kind: Literal["paused"] = "paused"


AgentEvent = Thinking | ToolCall | ToolResult | AskUser | Say | Final | ErrorEvent | Partial | SendPending | Paused

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
        # voice: a progress line the agent said, and a correction the user added mid-task
        "say",
        "steer",
        # appTakeover: the user took over while a request acted (paused, resumed, ended)
        "takeover",
        # Yuki's own "acting" marker for memory (begin / refresh / end), stamped with the request id
        "memory_acting",
        # phase S's skills and phase F's guards (decision D12; defined here so content_free keeps their code words)
        "skill_menu",
        "skill_resolve",
        "route_try",
        "done_check",
        "done_recheck",
        "skill_run",
        "skill_handoff",
        "media_command",
        "input_refused_game",
        "yt_fetch",
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


#: Set to ``1`` to keep the words in the agents' session logs and the request ledger (a developer chasing a bug,
#: on their own PC). Unset (the default), they keep counts, ids and timings only (report 76).
LOG_CONTENT_ENV = "YUKI_LOG_CONTENT"


def content_kept() -> bool:
    """Whether the agents' session logs and ``requests.csv`` keep the words (:data:`LOG_CONTENT_ENV`)."""
    import os

    return os.environ.get(LOG_CONTENT_ENV, "").strip() == "1"


#: String fields a content-free log keeps as they are: ids, Yuki's own names (tools, models, lanes) and states, never
#: the user's or a page's words. Every other string becomes its length (``<field>_chars``).
ID_FIELDS: frozenset[str] = frozenset({
    "session", "session_id", "type", "path", "model", "name", "tool_use_id", "stop_reason", "lane", "outcome",
    "effort", "kind", "state", "started_at", "role", "mode", "via", "id", "request_id", "tool", "app", "op",
})


#: String fields a content-free log keeps when the value is a code word, not prose: levels, verdicts, outcome kinds,
#: reasons and states Yuki's own code names (hotInput, report 73: with ``outcome_verdict.level`` stored as its length,
#: the diagnostics could not see a single level or verdict). A value that is not a code word (:func:`_code_word`) is
#: still only its length, so a reason written as a sentence, or a name, never reaches the file. Never a field that
#: holds the user's own words (a todo's ``by``: "dropped: their words, exactly").
ENUM_FIELDS: frozenset[str] = frozenset({
    "level", "verdict", "verdict_class", "why", "reason", "status", "tone", "send", "step_level",
    "result_kind", "pattern", "patterns", "control_type", "button", "direction", "how", "source", "lease",
    "process", "process_name", "exe", "window_state", "trigger", "phase", "stage", "decision", "cause", "class",
    # phase F (fPrivacy): the structural words of skills, routes, checks and moments (decision D12)
    *("skill", "route", "check", "end_kind", "means", "slot_source", "moment", "evidence", "scope", "service_kind",
      "result", "done_check", "recheck", "verb", "rung", "label_source", "gate"),
})
#: The fields a content-free log keeps inside a tool's input, a tool's result and a perception payload: the model
#: writes a tool's input, so only fields whose values are a fixed vocabulary (a button, a direction, a pattern, a
#: role, a state); never ``name``, ``title``, ``text``, ``path`` or ``app``, and never the free-form ones (``why``,
#: ``reason``, ``source``, ``decision``, ``by``): there a single word can be a person's name or the user's words.
NESTED_FIELDS: frozenset[str] = frozenset({
    "level", "verdict", "verdict_class", "status", "send", "step_level", "result_kind", "pattern", "patterns",
    "control_type", "button", "direction", "how", "lease", "window_state", "phase", "stage", "class",
    "kind", "state", "mode", "via", "role", "roles", "hidden_roles", "flags", "op", "tool", "lane", "outcome",
    "type", "error_kind",
    # phase F (fPrivacy): the same structural words inside a nested value (a skill run's slots, a check's facts)
    *("skill", "route", "check", "end_kind", "means", "slot_source", "moment", "evidence", "scope", "service_kind",
      "result", "done_check", "recheck", "verb", "rung", "label_source", "gate"),
})
#: Phase F (fPrivacy): top-level fields whose value is a structure Yuki fills from the user's world (a skill's slots,
#: a check's observed facts): shaped like a tool's input (:func:`_shape`: only :data:`NESTED_FIELDS` code words,
#: numbers and flags stay; every other string, ``name`` and ``app`` included, is its length), never kept by
#: :data:`ID_FIELDS` at any depth.
SHAPED_FIELDS: frozenset[str] = frozenset({"slots", "observed", "slot_values"})
#: A code word is what Yuki's code writes: lower case, digits and ``_ . : + -`` (``read_back_on``, ``keys``,
#: ``verified``). A capitalised word is a name until shown otherwise, so it is kept only where the vocabulary is
#: known (:data:`_ROLE_WORDS` for roles and control types, a ``.exe`` for a process).
_CODE_WORD = re.compile(r"[a-z][a-z0-9_.:+\-]{0,47}")
#: Field names kept inside a nested value: plain lower-case names (a dict keyed by titles or people's names is left
#: out and counted, ``other_keys``).
_FIELD_NAME = re.compile(r"[a-z_][a-z0-9_]{0,47}")
_EXE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}\.exe", re.IGNORECASE)
#: Decision D20 (critique M7): a keyed digest in a log is its first :data:`DIGEST_CHARS` hex characters, through
#: :func:`log_digest` in every emitter; a field named ``*_digest`` keeps exactly that shape and is dropped otherwise
#: (a 64-hex full digest, a name someone put there). Not reversible: "the same playlist / tab / person as last time"
#: can be told apart without the content.
DIGEST_CHARS = 12
_DIGEST = re.compile(r"[0-9a-f]{12}")


def log_digest(digest: Any) -> str | None:
    """The log form of a keyed digest (``FieldCipher.digest``'s 64 hex characters): its first 12, lower case;
    ``None`` for anything that is not hex (decision D20). Every emitter of a ``*_digest`` field calls this."""
    text = str(digest or "").strip().lower()[:DIGEST_CHARS]
    return text if _DIGEST.fullmatch(text) else None


def _digest_field(key: str) -> bool:
    return key.endswith("_digest")

#: The UIA control-type names (yuki/perception/tree.py ``_ROLE_NAMES``) and Yuki's own role words built on them.
_ROLE_WORDS: frozenset[str] = frozenset({
    "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List", "Menu",
    "MenuBar", "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab",
    "TabItem", "Text", "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid", "DataItem",
    "Document", "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar", "Separator",
    "SemanticZoom", "AppBar", "Link",
})
_ROLE_FIELDS = frozenset({"role", "roles", "hidden_roles", "control_type"})
_EXE_FIELDS = frozenset({"process", "process_name", "exe"})
#: Inside a nested value, lists longer than this keep only their length, and nesting deeper than this its size.
NESTED_LIST_MAX = 20
NESTED_DEPTH_MAX = 4


def _code_word(value: str, key: str = "") -> bool:
    """Whether a string is one code word (``verified``, ``read_back_off``), not prose or a name; under a role field a
    UIA role name (``Slider``), under a process field an exe name (``Spotify.exe``)."""
    if _CODE_WORD.fullmatch(value):
        return True
    if key in _ROLE_FIELDS:
        return value in _ROLE_WORDS
    if key in _EXE_FIELDS:
        return bool(_EXE.fullmatch(value))
    return False


def _code_words(value: Any, key: str = "") -> bool:
    """Whether ``value`` is a short list of code words (a control's patterns, the roles left hidden)."""
    return (isinstance(value, (list, tuple)) and len(value) <= NESTED_LIST_MAX
            and all(isinstance(v, str) and _code_word(v, key) for v in value))


def _shape(value: Any, depth: int = 0) -> Any:
    """A tool's input or result, or a perception payload, without its words: numbers, booleans and ``None`` as they
    are, code words in :data:`NESTED_FIELDS` as they are, every other string its length (``<field>_chars``), field
    names that are not plain names (a dict keyed by titles) left out and counted, long lists their length."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return len(value)
    if depth >= NESTED_DEPTH_MAX:
        return {"chars": len(json.dumps(value, ensure_ascii=False, default=str))}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        dropped = 0
        for key, v in value.items():
            key = str(key)
            if not _FIELD_NAME.fullmatch(key):
                dropped += 1
                continue
            if _digest_field(key):
                if isinstance(v, str) and _DIGEST.fullmatch(v):
                    out[key] = v        # D20: exactly 12 hex characters (log_digest); anything else is dropped
                continue
            if isinstance(v, str):
                if key in NESTED_FIELDS and _code_word(v, key):
                    out[key] = v
                else:
                    out[f"{key}_chars"] = len(v)
            elif key in NESTED_FIELDS and _code_words(v, key):
                out[key] = list(v)
            else:
                out[key] = _shape(v, depth + 1)
        if dropped:
            out["other_keys"] = dropped
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > NESTED_LIST_MAX:
            return {"count": len(value)}
        return [_shape(v, depth + 1) for v in value]
    return {"chars": len(json.dumps(value, ensure_ascii=False, default=str))}


def _error_kind(text: str) -> str:
    """``"ReadTimeout"`` from ``"ReadTimeout: The read operation timed out"``: the exception's type, not its message."""
    head = text.split(":", 1)[0].strip()
    return head if head.replace(".", "_").isidentifier() else ""


def _trace_frames(text: str) -> str:
    """A traceback without its messages: the code locations and the exception types (content-free)."""
    out = []
    for line in str(text).splitlines():
        if line.startswith(("Traceback", "  File ")):
            out.append(line)
        elif line and not line.startswith(" "):
            kind = _error_kind(line)
            if kind:
                out.append(kind)
    return "\n".join(out)


def _counts_only(value: Any) -> Any:
    """``value`` with every string that is not an id (:data:`ID_FIELDS`) or a code word in :data:`ENUM_FIELDS`
    replaced by its length."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, v in value.items():
            if _digest_field(str(key)):
                if isinstance(v, str) and _DIGEST.fullmatch(v):
                    out[key] = v        # D20: exactly 12 hex characters (log_digest); anything else is dropped
                continue
            if isinstance(v, str) and key not in ID_FIELDS and not (key in ENUM_FIELDS and _code_word(v, key)):
                if key == "traceback":
                    out[key] = _trace_frames(v)
                elif key == "error":
                    out["error_kind"] = _error_kind(v)
                    out["error_chars"] = len(v)
                else:
                    out[f"{key}_chars"] = len(v)
            elif key in ENUM_FIELDS and _code_words(v, key):
                out[key] = list(v)
            else:
                out[key] = _counts_only(v)
        return out
    if isinstance(value, list):
        return [len(v) if isinstance(v, str) else _counts_only(v) for v in value]
    return value


def _block_shape(block: Any) -> Any:
    """One content block as counts: its type, a tool's name, and how long its text or input is."""
    if not isinstance(block, dict):
        return {"chars": len(json.dumps(block, ensure_ascii=False, default=str))}
    shape: dict[str, Any] = {"type": block.get("type")}
    if block.get("name"):
        shape["name"] = block.get("name")
    if block.get("id"):
        shape["id"] = block.get("id")
    rest = {k: v for k, v in block.items() if k not in ("type", "name", "id", "signature")}
    shape["chars"] = len(json.dumps(rest, ensure_ascii=False, default=str))
    return shape


def content_free(record: dict[str, Any]) -> dict[str, Any]:
    """A log record with counts, ids and timings only (report 76: the worker logs kept the user's messages, screen
    summaries and replies in plain text, outside the encrypted database). The request's bulk becomes its size: the
    messages their count and length, the tools their names, a response's blocks their type, name and length."""
    rec = dict(record)
    heavy: dict[str, Any] = {}
    if rec.get("type") == "llm_request":
        messages = rec.pop("messages", None)
        system = rec.pop("system", None)
        tools = rec.pop("tools", None)
        heavy["messages"] = {"count": len(messages) if isinstance(messages, list) else 0,
                             "chars": len(json.dumps(messages, ensure_ascii=False, default=str))}
        heavy["system_chars"] = len(json.dumps(system, ensure_ascii=False, default=str))
        heavy["tools"] = [t.get("name") for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
    elif rec.get("type") == "llm_response" and isinstance(rec.get("content"), list):
        heavy["content"] = [_block_shape(b) for b in rec.pop("content")]
    for key in SHAPED_FIELDS:
        if isinstance(rec.get(key), (dict, list, tuple)):
            heavy[key] = _shape(rec.pop(key))
    for key in ("input", "result", "payload"):
        if key == "result" and isinstance(rec.get(key), str) and _code_word(rec[key], key):
            continue    # phase F: a skill run's or a check's result code word ("done", "unverified") stays
        if key in rec and not isinstance(rec[key], (int, float, bool)) and rec[key] is not None:
            value = rec.pop(key)
            heavy[f"{key}_chars"] = len(json.dumps(value, ensure_ascii=False, default=str))
            if key == "input" and isinstance(value, dict):
                heavy["input_keys"] = sorted(str(k) for k in value if _FIELD_NAME.fullmatch(str(k)))
            # hotInput: the non-word facts inside (verified, flags, counts, hidden roles, a level) stay readable
            heavy[f"{key}_shape"] = _shape(value)
    out = _counts_only(rec)
    out.update(heavy)
    return out


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
        content: bool = True,
    ) -> None:
        """Open a new session file.

        Args:
            sessions_dir: Directory for ``<id>.jsonl`` and ``<id>/`` screenshots.
            console: Rich console to print to; a default one is made if omitted.
            session_id: Override the timestamp-based id (tests use this).
            quiet: Suppress console output but still write JSONL.
            content: False: the file keeps counts, ids and timings only (:func:`content_free`; the desktop's
                agents, report 76), and the console shows no words either. The caller still gets each record whole.
        """
        self.content = content
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
        #: Phase F (fPrivacy): the request in progress (:meth:`begin_request`): every record from its start on
        #: carries this ``request_id``, which the acting marker, the memory log and ``requests.csv`` repeat, so
        #: ``python -m yuki.log.follow <request_id>`` can put one request together across the logs.
        self.request_id: str | None = None
        self._requests = 0
        self._request_begun = False
        self.log("session_start", session_id=self.session_id, path=str(self.path))

    # -- lifecycle ---------------------------------------------------------

    def begin_turn(self) -> int:
        """Advance to the next turn and return its number."""
        self.turn += 1
        return self.turn

    def reset_usage(self) -> None:
        """Start a fresh per-request usage total."""
        self.usage = UsageTotals()

    def begin_request(self, request_id: str | None = None) -> str:
        """Start a request: from now on every record carries its ``request_id`` (a record that names its own keeps
        it). ``request_id``: one the caller already has (a card run, a hand-off); default
        ``<session id>-<n>``, the n-th request of this log: the same id the agent's acting marker carries
        (its token is ``<lane>-<session id>-<n>``, :func:`yuki.memory.acting.request_id_of`). Returns the id. The
        next :meth:`user_message` does not start another one."""
        self._requests += 1
        self.request_id = str(request_id) if request_id else f"{self.session_id}-{self._requests}"
        self._request_begun = True
        return self.request_id

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
        if self.request_id is not None and "request_id" not in record:
            record["request_id"] = self.request_id
        if not self.content:
            line = json.dumps(content_free(record), ensure_ascii=False, default=_as_plain)
            self._fh.write(line + "\n")
            self._fh.flush()
            return record
        line = json.dumps(record, ensure_ascii=False, default=_as_plain)
        if secret_filter.find(line):
            # A password or key in a request, a tool input or captured screen text: the log keeps the
            # record with the secret as "[password hidden]" (docs/MEMORY.md, "Secret filter"). The
            # serialized line is checked in one pass; only a hit walks the record's strings. The caller
            # (the agent) still gets the record as it was.
            hidden, _ = secret_filter.redact_json(json.loads(line))
            line = json.dumps(hidden, ensure_ascii=False)
        self._fh.write(line + "\n")
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
        """Log (and show) a new request from the user: it starts the request's ``request_id``
        (:meth:`begin_request`), unless the caller began it already."""
        if not self._request_begun:
            self.begin_request()
        self._request_begun = False
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
        self._print(f"[dim]-> {_x(model)} ({_count_messages(messages)} messages)[/dim]")

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
            f"[dim]<- {_x(stop_reason)} {latency_ms / 1000:.1f}s "
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
            self._print(f"[cyan]. {_x(shown)}[/cyan]")

    def tool_call(
        self, name: str, tool_input: dict[str, Any], *, tool_use_id: str | None = None
    ) -> None:
        """Log a tool invocation."""
        self.log("tool_call", name=name, input=tool_input, tool_use_id=tool_use_id)
        self._print(f"[yellow]-> {_x(name)}[/yellow] [dim]{_x(_brief(tool_input))}[/dim]")

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
        self._print(f"[{colour}]{mark} {_x(name)}[/] {_x(summary)} [dim]{elapsed_ms:.0f}ms[/dim]")

    def perception(self, kind: str, *, size_chars: int, elapsed_ms: float, payload: Any) -> None:
        """Log a perception snapshot in full, plus its size on the console."""
        self.log(
            "perception",
            kind=kind,
            size_chars=size_chars,
            elapsed_ms=round(elapsed_ms, 1),
            payload=self._dereference_images(_as_plain(payload)),
        )
        self._print(f"[dim]   {_x(kind)}: {size_chars} chars, {elapsed_ms:.0f}ms[/dim]")

    def ask_user(self, question: str) -> None:
        """Log a question put to the user."""
        self.log("ask_user", question=question)
        self._print(f"[bold magenta]? {_x(question)}[/bold magenta]")

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
            f"[dim]   context: stubbed {_x(name)} from turn {from_turn} "
            f"({original_chars} chars)[/dim]"
        )

    def final(self, text: str) -> None:
        """Log and show the agent's closing message."""
        self.log("final", text=text)
        self._print(f"[bold green]yuki[/bold green] [bold]{_x(text)}[/bold]")

    def error(self, text: str, *, exc: BaseException | None = None) -> None:
        """Log an error, with a traceback when an exception is supplied."""
        tb = "".join(traceback.format_exception(exc)) if exc is not None else None
        self.log("error", text=text, traceback=tb)
        self._print(f"[bold red]error[/bold red] {_x(text)}")

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
            f"[dim]== {_x(summary.get('outcome'))} - {float(summary.get('wall_s') or 0):.1f}s{waiting} "
            f"- model {float(summary.get('model_s') or 0):.1f}s "
            f"- tools {float(summary.get('tool_s') or 0):.1f}s "
            f"- {summary.get('model_calls', 0)} call(s) - {cost_text}[/dim]"
        )
        return record

    # -- console -----------------------------------------------------------

    def _print(self, markup: str) -> None:
        """One console line. Every piece of text from outside (the model, tools, the user) is escaped where the line
        is built (``_x``); a line that still does not parse is printed as plain text, never raised (2026-09-29: a
        stray "[/cyan]" in the model's reasoning raised rich's MarkupError out of the agent loop). A content-free
        logger prints nothing (its lines carry the words)."""
        if self.quiet or not self.content:
            return
        try:
            self.console.print(markup, highlight=False)
        except MarkupError:
            self.console.print(markup, highlight=False, markup=False)


def _count_messages(messages: Any) -> int:
    """Length of the message list, or ``-1`` if it has none."""
    try:
        return len(messages)
    except TypeError:  # pragma: no cover - defensive
        return -1


def _x(value: Any) -> str:
    """Text from outside (model, tools, user, pages) made literal for rich's console markup."""
    return escape(str(value))


def _brief(value: Any, limit: int = 120) -> str:
    """One-line rendering of a tool input for the console."""
    try:
        text = json.dumps(_as_plain(value), ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        text = repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."
