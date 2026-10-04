"""The agent loop.

:meth:`Agent.run` is a generator so the caller can stay in control: it yields
what happened as it happens, and when Yuki needs an answer it yields
:class:`~yuki.log.events.AskUser` and simply stops there. The caller supplies the
answer with :meth:`Agent.answer` and resumes iterating; the messages, the running
note and the turn counter all survive the pause because they live on the agent,
not in the generator's locals.

One :class:`Agent` spans the whole session: every new request appends to the same
conversation.

Two things keep the number and length of rounds down:

* Tools run while the response is still streaming. As soon as a ``tool_use``
  block is complete (and the stream has moved past it, so it was not cut off by
  the output limit) it is dispatched, provided it is an action or perception
  tool and everything before it in the response has already run and succeeded.
  ``ask_user`` waits for the end of the stream (and holds back everything after
  it), and ``done`` is still evaluated last. The stored assistant turn is always
  the SDK's complete final message.
* After a turn whose actions all succeeded, the window acted on is read once it
  has settled and attached to the next turn ("Window after your actions"), so
  the model does not spend a round calling ``look_at_window`` only to see what
  its action did.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Iterator

from yuki.agent import consult as second_look
from yuki.agent import finish as finish_gate
from yuki.agent import frame as task_frame
from yuki.agent import situation as situation_check
from yuki.agent.context import KEEP_FULL_REQUESTS, ContextManager, RequestRecord
from yuki.agent.failover import FailoverClient
from yuki.agent.judge import Authorizer, call_text as judge_call_text
from yuki.agent.lanes import READER_INSTRUCTIONS, READER_LANE_TOOLS, READER_TOOLS, is_reader
from yuki.agent.loopguard import EXEMPT as LOOP_EXEMPT, LoopBreaker, signature as call_signature
from yuki.agent.subagents import HelperTeam, report_text
from yuki.agent.untrusted import EXTERNAL_TEXT_TOOLS, new_nonce, source_for, wrap, wrap_blocks
from yuki.agent.memory import (
    MemoryAccess,
    default_memory,
    commitments_block_text,
    memory_block_text,
    memory_query_of,
    relevant_block_text,
    standing_commitments_text,
    standing_rules_text,
)
from yuki.agent.prompt import profile_text, rules_text, system_blocks
from yuki.agent.tools import (
    ABOUT_FIELD,
    STATES_FIELD,
    ALL_TOOL_NAMES,
    ACTION_TOOL_NAMES,
    AUTO_VIEW_TOOLS,
    CONTROL_TOOLS,
    FINISH_WORDS_FIELD,
    FINISHES_FIELD,
    MEMORY_READ_KINDS,
    MEMORY_TOOLS,
    NOW_FIELD,
    PLAYBOOK_FIELD,
    RISK_FIELD,
    STEP_ABOUT_FIELD,
    STEP_FIELD,
    WHAT_FIELD,
    PERCEPTION_TOOLS,
    Backend,
    Dispatcher,
    ToolOutcome,
    resolve_tool_names,
    call_traits,
    sends_for_certain,
    tool_params,
    tool_traits,
    unavailable_tool,
)
from yuki.config import Settings, resolve_effort, resolve_model
from yuki.log.events import (
    AgentEvent,
    AskUser,
    ErrorEvent,
    Final,
    Partial,
    Paused,
    Say,
    SendPending,
    SessionLogger,
    Thinking,
    ToolCall,
    ToolResult,
    _as_plain,
)
from yuki.log.requests import TOKEN_KEYS, append_request_row, usage_tokens
from yuki.agent.stream_partial import stream_partial


#: The API's limit on ``cache_control`` breakpoints in one request.
MAX_CACHE_BREAKPOINTS = 4

#: When the model warm-up could not reach Bedrock at all (the network not up yet
#: after logon or a wake), it is tried again after these waits (seconds), then left.
PREWARM_RETRY_S: tuple[float, ...] = (30.0, 90.0, 240.0)

#: Tools never dispatched while the response is still streaming. ``done`` is
#: evaluated last, after everything else in the turn; ``ask_user`` pauses the
#: generator for the user's answer, and whatever the model wrote after it must
#: not run before that answer exists.
_DEFERRED_TOOLS: frozenset[str] = frozenset({"done", "ask_user"})

#: Stop reasons after which a just-finished ``tool_use`` block may be cut off or
#: disowned, so it is not run early.
_UNSAFE_STOPS: frozenset[str] = frozenset({"max_tokens", "refusal"})

#: Tools that are not "actions taken" for the turn log and the collapsed form
#: of a request: looks change nothing, and ``done`` / ``note_to_self`` are the
#: reply and the working note. Plumbing for a summary line, not behaviour.
_NOT_ACTIONS: frozenset[str] = PERCEPTION_TOOLS | {"done", "note_to_self", "say"}

#: Longest one-line action summary kept for memory and the collapsed request.
_ACTION_LINE_CHARS = 160
#: A request with this many actions, or trouble then a success, is what practice memory's distiller learns from
#: (``yuki.memory.practice_learn.DISTILL_AFTER_ACTIONS``; here only for the content-free ``_learning`` counters).
LEARN_AFTER_ACTIONS = 3
#: Steps one request's run trace keeps (practice memory, Phase C): far more than any real request takes.
TRACE_STEPS = 80

#: How the loop breaker's rethink note reaches the model (:mod:`yuki.agent.loopguard`).
BREAKER_LABEL = "[Yuki's loop breaker, not the user]"

#: How the outcome check's note reaches the model (v3.14: an outcome its own calls do not back is sent back).
CHECK_LABEL = "[Yuki's check, not the user]"

#: How a correction the user added mid-task reaches the model (:meth:`Agent.steer`):
#: framed as theirs, so it reads as the user's words and not as a tool's output.
STEER_LABEL = "[The user added while you were working]"

#: The judge's words (review 34 H4, M3; ``Agent._note_words``): the kinds a request's own words are noted as, how long
#: an earlier request's words stay in view once a new request begins (a quick follow-up still reads them; this
#: morning's never), and the most heard turns of a spoken request given to it.
REQUEST_WORD_KINDS: frozenset[str] = frozenset({"typed", "heard", "reading", "card", "suggestion"})
USER_WORDS_WINDOW_S = 120.0
USER_WORDS_HEARD = 3
#: The opening of a request sent from a card's suggestion unchanged (``yuki.ui.nudges.SUGGESTION_MARK``, Yuki's own
#: format): its words are Yuki's offer, never typed by the user.
SUGGESTION_MARK = "[Yuki's own suggestion"

#: Why a run was stopped (:meth:`Agent.cancel`): the user stopped it, or it was withdrawn because
#: the user's newer request replaced it (voice: "no, close the tab instead"). A withdrawn run ends
#: quietly: its caller shows it as cancelled, never as an error, and nothing of it is spoken.
CANCELLED, WITHDRAWN = "cancelled", "withdrawn"
#: v3.14b: a request paused on a question nobody answered, set aside so its lane can go on (yuki.ui.runtime parks it,
#: and a later answer carries it on as a new request).
PARKED = "parked"

#: appTakeover (4 Oct): a request paused because the user took over the keyboard or mouse, and nobody resumed it within
#: ``Agent.pause_timeout_s``, ends with this line (quietly: never spoken). A stop while paused ends with it too.
PAUSE_STOP_LINE = "Stopped. Nothing else was done."
#: What an action the pause kept from running answers to the model.
PAUSED_SKIP = ("Not run: the user took over the keyboard or mouse before this ran, so you were paused here. The screen "
               "may have changed since you chose it.")
#: After a resume, the watch for the user taking over starts again this late (s): the hand that pressed Resume is still
#: moving away, and the screen is read again first.
RESUME_GRACE_S = 1.5
#: How the user took over, in plain words (the model's note and the log).
TAKEOVER_WORDS = {"keys": "typed on the keyboard", "click": "clicked", "mouse": "moved the mouse"}


def written_tool_call(text: str, tools: Iterable[str]) -> str | None:
    """The tool a reply named in the model's own tool-call markup written out as text, else ``None``.

    Seen offline 2026-09-27 (voice v3.9 replays, Sonnet on Bedrock): a response whose stop reason said tool_use
    held only a text block with ``<invoke name="click">`` and its parameters; the loop took that text for the
    final answer, and the next requests of the conversation copied it. Parsing the markup's fixed form (the tag
    and its ``name`` attribute), checked against this agent's real tool names; never a reading of the words.
    """
    start = text.find("<invoke")
    while start >= 0:
        head = text[start:start + 200]
        quote = head.find('name="')
        if quote >= 0:
            end = head.find('"', quote + 6)
            name = head[quote + 6:end] if end > quote else ""
            if name in tools:
                return name
        start = text.find("<invoke", start + 7)
    return None


def acting_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """A call's input without the brain's spoken step line (``say_now``): what the call does.

    Wherever calls are compared (the loop breaker, the repeat facts, a confirmed irreversible call), two calls that
    differ only in the words said as they start are the same call.
    """
    if STEP_FIELD not in (tool_input or {}):
        return tool_input
    return {k: v for k, v in tool_input.items() if k != STEP_FIELD}


#: appPT: the longest ``now`` note passed on to the island (one line; the UI cuts it to fit).
NOW_CHARS = 140
#: appPT (review F7): how alike (difflib's ratio, casefolded, whitespace squashed) a send's text and the user's answer
#: to a question must be for that answer to count as their go-ahead on the text (a fixed spelling or full stop aside).
ANSWERED_TEXT_SIMILAR = 0.85


def _now_line(value: Any) -> str:
    """The brain's ``now`` note as one line (whitespace squashed, capped): plumbing, never read for meaning."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= NOW_CHARS else text[: NOW_CHARS - 1].rstrip() + "…"


def _keeps_no_cache(client: Any) -> bool:
    """The agent's client goes to an AI that keeps no prompt cache (Settings → AI, report 67): no warm-up for it."""
    caching = getattr(client, "prompt_caching", None)
    return callable(caching) and caching() == "none"


def _routed(client: Any) -> bool:
    """The agent's client goes to another AI than Claude on Bedrock (Settings → AI routes Doing things elsewhere: its
    prompt cache is ``auto`` or ``none``, not Claude's ``explicit``). Never raises; False when it cannot tell."""
    caching = getattr(client, "prompt_caching", None)
    try:
        return callable(caching) and caching() != "explicit"
    except Exception:
        return False


def _acts_only(tool_input: Any) -> dict[str, Any]:
    """A kept step's input without the brain's notes about it (``what``, ``risk``...): what the call did."""
    from yuki.agent.loopguard import NOTE_FIELDS

    return {k: v for k, v in (tool_input or {}).items() if k not in NOTE_FIELDS} if isinstance(tool_input, dict) else {}


def _app_display(process: Any) -> str:
    """A window's app as people know it (``Discord.exe`` -> ``Discord``; Windows' own name when it has one), from the
    process name (``yuki.core.appnames``). ``""`` for none; never raises."""
    text = str(process or "").strip()
    if not text:
        return ""
    try:
        from yuki.core.appnames import app_key, prettify, resolver

        try:
            return resolver().name_for_key(app_key(text), prettify(text)) or prettify(text)
        except Exception:  # noqa: BLE001 - the tidied image name still reads
            return prettify(text)
    except Exception:  # noqa: BLE001
        return ""


#: The verdict reason of an action whose effect its report cannot show by the nature of its target, not because
#: anything went wrong: a field that exposes no text to read back (Discord's message box, a canvas editor). An
#: expected-unverifiable result (revamp A8, 01 S5): it never escalates the request's effort (on Sonnet 5 every switch
#: rewrites the cached prompt: a 53k-token rewrite and a 13 s call on 30 Sept for exactly this), and a done is not
#: held for it; a finish it marks is still held for a look. A field that does not hold the text, a failure and a
#: loop-breaker trip still escalate.
EXPECTED_UNVERIFIABLE = finish_gate.EXPECTED_UNVERIFIABLE

#: How the authorization judge's refusal reaches the model (revamp D, S1): framed as Yuki's own check, never the user.
JUDGE_LABEL = "[Yuki's authorization check, not the user]"
#: How a request stopped by too many denials ends (revamp D, S1: 3 in a row or 20 in all).
DENIALS_MESSAGE = ("I stopped before doing something you hadn't asked for, a few times over. Tell me exactly what you "
                   "want done and I'll do just that.")

#: Input-watch facts of a key or click report (yuki.actions.input): each says the input changed something the
#: user can see. Plumbing: which fields of the action's own report are its measured effects.
_EFFECT_FACTS = ("foreground_changed", "focus_changed", "foreground_title_changed", "page_changed",
                 "window_state_changed")   # v3.14b: normal / maximized / full screen, before -> after


#: appOpen review F6: how long a run trace waits for its openings' watches at most (the watch's own 180 s, and slack).
WATCH_JOIN_S = 200.0


def with_watch_steps(run: dict[str, Any], watching: list[tuple[int, dict[str, Any]]], *,
                     join_s: float = WATCH_JOIN_S) -> dict[str, Any]:
    """The run trace with each opening's watch (appOpen review F6) as a step right after the last launch_app it
    watched: verified when the app's own window came up, not when it did not, "could not tell" when the watch was
    stopped or did not end. Waits for the watches (bounded by ``join_s``); returns a new run dict."""
    deadline = time.monotonic() + max(0.0, float(join_s))
    last: dict[int, tuple[int, dict[str, Any]]] = {}
    for index, entry in watching:
        if id(entry) not in last or index > last[id(entry)][0]:
            last[id(entry)] = (index, entry)
    steps = list(run.get("steps") or [])
    ended = float(run.get("ended_at") or time.time())
    for index, entry in sorted(last.values(), key=lambda x: -x[0]):
        thread = entry.get("thread")
        if isinstance(thread, threading.Thread):
            thread.join(max(0.0, deadline - time.monotonic()))
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {
            "verified": None, "summary": "the watch did not end in time"}
        verdict = result.get("verified") if isinstance(result.get("verified"), bool) else None
        at = float(entry.get("ended") or time.time())
        ended = max(ended, at)
        base = steps[index] if 0 <= index < len(steps) else {}
        name = str(entry.get("name") or "")
        query = (base.get("input") or {}).get("query") if isinstance(base.get("input"), dict) else None
        steps.insert(index + 1, {
            "tool": "launch_app", "kind": "action",
            "app": str(result.get("process_name") or "") or None if verdict is True else None,
            "target": f"{base.get('target') or 'app ' + repr(name)}, watched",
            "input": {"query": query or name, "watch": True}, "ok": verdict is not False, "verdict": verdict,
            "why": ("its own window showed" if verdict is True else "its own window did not show"
                    if verdict is False else "the watch stopped before it could tell"),
            "expected": False, "result": " ".join(str(result.get("summary") or "").split())[:400],
            "ms": round((at - float(entry.get("began") or at)) * 1000.0, 1), "sig": None, "playbook": None,
            "at": at, "cost_usd": None})
    return {**run, "steps": steps, "ended_at": ended}


def action_verdict(name: str, outcome: ToolOutcome) -> tuple[bool | None, str]:
    """Whether an action's own report verifies that it took effect: ``(True | False | None, why)``.

    Read from the report the action layer measured (docs/VOICE.md v3.14), never from its words: a tab's selected
    state, a scroll position, a volume read back, a page at its address, a field read back after typing, a window
    in front, and for keys and clicks the focus, window and page watch (something visibly changed: True; watched
    and nothing changed: False). ``None``: the report cannot show it (a shell command, a key with nothing watched).
    A failed action is False.
    """
    if not outcome.ok:
        return False, "it failed"
    payload = outcome.payload if isinstance(outcome.payload, dict) else {}
    if name == "type_text" and isinstance(payload.get("type_text"), dict):
        # An element-targeted type_text (clicked to focus it first) nests the typing's own report (v3.15: live
        # 30 Sept its read-back "arrived" was missed here, read as "could not be read back" and escalated).
        payload = payload["type_text"]
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    own = details.get("verified")
    change = details.get("screen_change") if isinstance(details.get("screen_change"), dict) else None
    if isinstance(own, bool) and change is not None and change.get("verified") is own and change.get("why"):
        # report 58: the window read before and after the action, compared (yuki.perception.outcome)
        return own, str(change["why"])
    if name == "launch_app" and isinstance(own, bool):
        # appOpen F2: launch_app's own verdict is the app's own window in front (never a launcher's window)
        return own, ("the app's own window is in front" if own else
                     "its report says the app's own window is not in front")
    if isinstance(own, bool):
        return own, "its report says it was " + ("seen" if own else "not seen")
    if name == "launch_app" and "verified" in details and own is None:
        # appOpen F2: no window of the app itself yet (a game its launcher is still starting), or one nothing can
        # recognise as its: not done, and not a failure either
        if details.get("starting"):
            return None, "it is still starting through its launcher: no window of the app itself yet"
        return None, "no window it could recognise as the app's own came up in front"
    if name == "scroll" and details.get("unverifiable") == "page":
        # V6: a web page that takes no UI Automation scrolling never shows its scroll from here: expected, not trouble
        return None, finish_gate.PAGE_SCROLL_UNVERIFIABLE
    if name in ("scroll", "tab", "app_volume", "hover"):
        return None, "its report could not tell whether it took effect"
    if name == "open_url" or (name == "launch_app" and details.get("page_verified") is not None):
        page = details.get("page_verified")
        if page is True:
            return True, "the page is at the address"
        if page is False:
            return False, "the page is not at the address"
        if not details.get("page_targets") and details.get("hwnd"):
            return True, "its window came up in front"
        return None, "the page could not be checked"
    if name in ("launch_app", "focus_window"):
        return True, "the window is in front"
    if name in ("window_action", "write_clipboard"):
        # Their ok is the state read back after acting (the window's state from Windows; the clipboard's text).
        return True, "its report reads back the new state"
    if name == "type_text":
        seen = details.get("readback")
        if seen == "arrived":
            return True, "the field reads back the text"
        if seen == "unreadable":   # the field's own nature (no Value/Text pattern): expected, never a failure
            return None, EXPECTED_UNVERIFIABLE
        return (False, "the field does not hold the text") if seen == "missing" else (
            None, "the field could not be read back")
    if name in ("click", "hotkey", "press"):
        facts = [details.get(k) for k in _EFFECT_FACTS if k in details]
        if any(f is True for f in facts) or details.get("new_windows"):
            return True, "something visibly changed after it"
        if facts and all(f is False for f in facts):
            return False, "focus, window, its state and page were watched and nothing changed"
        return None, "nothing its report watched could show the effect"
    return None, "its report cannot show the effect"


class _StreamCancelled(Exception):
    """The run was cancelled while the model's response was streaming: the stream is dropped there."""


@dataclass
class _Turn:
    """The tool calls of one assistant response, shared by in-stream and later dispatch.

    Attributes:
        results: ``tool_use_id`` -> the ``tool_result`` block to send.
        names: ``tool_use_id`` -> tool name.
        handled: Ids that already have a result (ran, failed or skipped).
        stop_after: A failure (or ``done``) happened; everything later is skipped.
        failed: Some result in this turn is an error (a failure or a skip).
        finished: The ``done`` message, if the model ended the request.
        actions: ``(name, input, outcome)`` of every successful state-changing
            action, in dispatch order (:data:`AUTO_VIEW_TOOLS`).
        looked: hwnds read by an explicit successful ``look_at_window`` after
            the last of those actions.
        early: Tools dispatched while the response was still streaming.
        first_early_at: ``perf_counter`` of the first of them.
    """

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    handled: set[str] = field(default_factory=set)
    stop_after: bool = False
    failed: bool = False
    finished: list[str] = field(default_factory=list)
    actions: list[tuple[str, dict[str, Any], ToolOutcome]] = field(default_factory=list)
    looked: set[int] = field(default_factory=set)
    early: int = 0
    first_early_at: float | None = None
    #: ``finishes_with`` messages of actions in this response that succeeded (the brain's
    #: mark that the action completes the request), with that action's verdict and why
    #: (:func:`action_verdict`); the request ends with the last one once every call of the
    #: response has succeeded and that action's own report verified its effect (v3.14).
    finishing: list[tuple[str, bool | None, str, str]] = field(default_factory=list)
    #: Calls held for the user's confirmation (declared irreversible, not asked for): not failures
    #: for the loop breaker.
    gated: set[str] = field(default_factory=set)
    #: ``(tool, verdict, why)`` of every action this response ran (v3.14: the escalation signals).
    verdicts: list[tuple[str, bool | None, str]] = field(default_factory=list)
    #: vfix3 review F6: this response carries an action, so a ``remember_how`` in it describes a step not yet seen to
    #: work: it is held and kept only when the request ends well (:meth:`Agent._keep_held_how`).
    acting_reply: bool = False
    #: The windows the ``done`` of this response says the present state of (``about``); ``None``: no done.
    done_about: list[int] | None = None
    #: N3: the states the ``done`` of this response tells, each with the step it says showed it (``states``).
    done_states: list[dict[str, Any]] = field(default_factory=list)
    #: The user answered a question in this response (``ask_user`` or a risk confirmation).
    asked: bool = False
    #: ``(tool, why)`` of actions this response ran whose own report shows the effect did not happen (revamp D, S2):
    #: a measured signal for a second look.
    contradicted: list[tuple[str, str]] = field(default_factory=list)

    def record(self, tool_use_id: str, block: dict[str, Any]) -> None:
        """Store one result block, noting whether it is an error."""
        self.results[tool_use_id] = block
        self.handled.add(tool_use_id)
        if block.get("is_error"):
            self.failed = True


@dataclass
class RequestPack:
    """What a request needs before its first model call, built ahead (revamp D, S5; 07 §6 E2): the window list with
    the Now picture, the request origin, the window the request is about, and memory's part (the screen anchor, the
    task frame's inputs and memory's context). :meth:`Agent.build_pack` makes one (the voice can while the user is
    still speaking); ``Agent.run(pack=...)`` uses it instead of building. Raw texts: they are fenced with the request's
    own code when it runs.

    Attributes:
        built_at: ``time.monotonic()`` when it was made.
        overview: ``(text, payload, elapsed_ms)`` from ``Dispatcher.overview_text``.
        origin: The request-origin line.
        view: The window view read for the request (a ``WindowView``), or ``None``.
        memory: ``{"screen", "inputs": (facts, focus, clip), "fetched": MemoryContext}``, or ``None``.
    """

    built_at: float
    origin_hwnd: int | None = None
    view_hwnd: int | None = None
    overview: tuple[str, Any, float] | None = None
    origin: str | None = None
    view: Any = None
    memory: dict[str, Any] | None = None
    build_ms: float = 0.0
    parts_ms: dict[str, float] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.built_at


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
        memory: Yuki's memory (:class:`~yuki.agent.memory.MemoryAccess`): the
            portrait, standing rules and commitments and know-how attached to
            every request (plus where the last session left off, on this
            conversation's first request), what the memory tools run against,
            and where every finished exchange is logged, on a background
            thread, under the app run's ``memory.session_id``. Defaults to the
            process-wide one on the real memory database; offline checks pass
            one built on a stub client. Memory that is missing or failing
            never fails or delays a request.

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
        memory: MemoryAccess | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.memory = memory if memory is not None else default_memory()
        self.lane = lane
        self.record_requests = record_requests
        self.logger = logger or SessionLogger(self.settings.sessions_dir)
        self.dispatcher = dispatcher or Dispatcher(
            backend,
            screenshot_policy=self.settings.screenshot_policy,
            tool_timeout_s=self.settings.tool_timeout_s,
        )
        self._setup_input_gate()
        self.context = ContextManager(self.logger, keep_turns=self.settings.keep_perception_turns)
        # Phase 2 (the eyes builder's relay, 2026-09-29): the per-turn Now picture follows this agent's own settings,
        # for the brain on the real desktop only; a stub desk (a test, an offline replay) never gets the live screen's
        # picture from the memory service.
        try:
            from yuki.agent.tools import _LazyRealBackend

            enable = getattr(self.dispatcher, "enable_now", None)
            if callable(enable) and isinstance(getattr(self.dispatcher, "backend", None), _LazyRealBackend):
                enable(self.settings)
        except Exception:  # noqa: BLE001 - the brain works without it
            pass
        self.tool_names = resolve_tool_names(tool_names)
        self.extra_instructions = (extra_instructions or "").strip() or None
        #: The reader lane (revamp D: the front desk repurposed, yuki.agent.lanes): the read tools only (plus its
        #: hand_over, kept out of its own subset so a call to it counts as needing hands) and the reader's framing.
        self.reader = is_reader(lane)
        if self.reader:
            if self.tool_names is None or set(self.tool_names) - set(READER_TOOLS):
                self.tool_names = resolve_tool_names(READER_TOOLS)
            self.extra_instructions = READER_INSTRUCTIONS
        #: The authorization judge (revamp D, S1, yuki.agent.judge): its per-request memo, grants and denial counts.
        self.authorizer = Authorizer(lambda: self.client, self.settings, log=self.logger.log) \
            if getattr(self.settings, "judge", True) else None
        #: What was said in this conversation, oldest first (what the judge sees), each labelled by whose words it is:
        #: the user's (typed, heard, added mid-run, an answer) or Yuki's context (reading, card, suggestion).
        #: ``{"how", "text", "at", "request", "question"?}``; an earlier request's words leave after
        #: :data:`USER_WORDS_WINDOW_S` (``_note_words``).
        self._user_words: deque[dict[str, Any]] = deque(maxlen=24)
        self._client = client
        self._client_lock = threading.Lock()
        self._cancelled = threading.Event()
        #: Why the pending cancel was asked for (:data:`CANCELLED` or :data:`WITHDRAWN`).
        self.cancel_reason = CANCELLED
        #: Corrections and additions the user made while a request runs
        #: (:meth:`steer`), drained at the next step boundary.
        self._steers: queue.SimpleQueue[str] = queue.SimpleQueue()
        #: appPT: the send countdown (``SendPending``): seconds to stop or swap a plain send before it goes; 0 (the
        #: default: the CLI, evals, tests) never waits. The desktop runtime turns it on (yuki.ui.runtime).
        self.send_countdown_s = 0.0
        #: Wakes the countdown's wait: a cancel, or the user's chip tap (:meth:`send_choice`).
        self._send_wake = threading.Event()
        self._send_lock = threading.Lock()
        self._send_open: dict[str, Any] | None = None    # the send the countdown shows now (its alternatives)
        self._send_choice: tuple[str, str] | None = None
        #: appTakeover: makes the watch for the user taking over while this request acts (``takeover(on_takeover,
        #: grace_s=...)`` -> an object with ``start()`` / ``stop()``, ``yuki.actions.leases.TakeoverThread``); ``None``
        #: (the default: the CLI, evals, tests, a caller's agent) never pauses. The desktop runtime's worker sets it.
        self.takeover: Any = None
        #: Told at once of every pause state (``on_pause(state, why, seconds)``: ``paused`` from the watch's thread the
        #: moment the user takes over, ``resumed`` / ``ended`` from the request's thread), so the island shows it while
        #: the request is still in a model call. Never raises into the request.
        self.on_pause: Any = None
        #: Whether a takeover may pause the request now (``takeover_ok() -> bool``; ``None``: always). The desktop sets
        #: it to "no full screen in front": in a full-screen game or video the island is hidden, so a pause could not
        #: be seen; there the input gate's short wait before Yuki's own input stays the only yield (review F4).
        self.takeover_ok: Any = None
        #: A pause nobody resumes ends the request after this long (s; ``Settings.takeover_idle_stop_s``).
        self.pause_timeout_s = float(getattr(self.settings, "takeover_idle_stop_s", 600.0) or 600.0)
        self._paused = threading.Event()
        self._pause_wake = threading.Event()
        self._pause_lock = threading.Lock()
        self._pause_why = ""
        self._pause_at = 0.0
        self._paused_s = 0.0
        self._pauses = 0
        self._stopped_paused = False
        self._pause_quiet_until = 0.0
        self._resume_asked = False
        self._watch: Any = None
        self._pending_answer: str | None = None
        self._awaiting_answer = False
        self._prewarm_thread: threading.Thread | None = None
        # Per-request self-awareness facts (reset by run()): reported to the
        # model as plain facts, never used to steer the loop.
        self._request_started = time.monotonic()
        self._waited_s = 0.0
        self._model_calls = 0
        self._call_history: dict[str, list[tuple[str, str]]] = {}
        #: The request being handled, handed to ``remember_how`` as its source.
        self._request_text: str | None = None
        #: The effort of the request being run, when its caller chose one (``run(effort=...)``).
        self._request_effort: str | None = None
        #: This request's own model / thinking / slim context, when its caller chose them
        #: (``run(model=..., thinking=..., lean=...)``: a spoken request, by its source).
        self._request_model: str | None = None
        self._request_thinking: bool | None = None
        self._request_lean = False
        self._request_voice = False
        self._request_omit_effort = False
        #: ``(effort, thinking)`` this request moves to for its next model calls once a measured signal says the
        #: fast first pass needs judgement (``run(escalate=...)``, a spoken request: docs/VOICE.md v3.14), and why it
        #: did (``None`` until then).
        self._escalate_to: tuple[str | None, bool | None] | None = None
        self._escalation: dict[str, Any] | None = None
        #: What this request has seen of each window since anything was done to it (hwnd -> "looked" |
        #: "verified"): a ``done`` that names a window's present state needs one (v3.14).
        self._evidence: dict[int, str] = {}
        #: A ``done`` (or a finishing action) was sent back once this request for want of evidence.
        self._claim_rounds = 0
        #: The newest desktop overview as plain data (for the foreground app).
        self._overview_payload: Any = None
        #: ``(portrait text, turn)`` of the last portrait attached in full to
        #: this conversation; an identical one is not sent again.
        self._portrait_sent: tuple[str, int] | None = None
        #: What this request's reply was written from, for memory's personal-ness log (revamp 09 P4.1, ids only):
        #: the brief lines in the profile layer, the memories attached, the playbooks the frame showed.
        self._surfaced: dict[str, Any] = {"brief_ids": [], "brief_at": None, "fact_ids": []}
        #: Requests this conversation has started (1 on the first).
        self._requests = 0
        #: The previous request's words (fast memory also searches them: a follow-up names nothing itself).
        self._previous_request: str | None = None
        #: The words memory was searched with for the previous request and the running one (the user's own words,
        #: never Yuki's hand-off envelope: ``run(memory_query=)``, else ``memory_query_of``).
        self._previous_query: str | None = None
        self._this_query: str | None = None
        #: This conversation's exchanges as the task frame reads them for "the last person named"
        #: (``{"at", "text", "who": "user" | "yuki"}``: the user's own words and Yuki's replies, newest last). Old
        #: ones fade by their age in the frame (``yuki.agent.frame.SOURCES``).
        self._said: deque[dict[str, Any]] = deque(maxlen=12)
        #: The task frame of the request being handled (``yuki.agent.frame``), for its log.
        self._frame: task_frame.TaskFrame | None = None
        #: N8 (report 47): ``(at, {app: [lines]})`` of this session's troubled runs (:func:`task_frame.session_lessons`),
        #: shown in the frame of the next requests on those apps until practice memory's post-mortem learns them.
        self._session_lessons: deque[tuple[float, dict[str, list[str]]]] = deque(maxlen=12)
        #: The profile and rules layers of the system prompt for the request being handled
        #: (``prompt.system_blocks``), set by ``_memory_context``; ``None`` when memory had
        #: nothing (then the layer is left out).
        self._profile_layer: str | None = None
        self._rules_layer: str | None = None
        #: One line per thing done this request, for memory's turn log and
        #: the request's collapsed form.
        self._actions: list[str] = []
        #: Token of the "Yuki is acting" marker published for this request
        #: (from its first action tool until the request ends), else None.
        self._acting_token: str | None = None
        #: The loop breaker (yuki.agent.loopguard), reset every request.
        self._breaker = LoopBreaker(
            repeats=int(getattr(self.settings, "loop_breaker_repeats", 3)),
            failed_steps=int(getattr(self.settings, "loop_breaker_failed_steps", 4)),
            max_trips=int(getattr(self.settings, "loop_breaker_max_trips", 2)),
        )
        #: The breaker's hard-stop note has reached the model (the next action attempt ends the request).
        self._breaker_stop_sent = False
        #: This request's fence code for untrusted text (yuki.agent.untrusted).
        self._nonce = new_nonce()
        #: ``(tool, signature)`` of irreversible calls the user was asked about this request.
        self._confirmed: set[tuple[str, str]] = set()
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
                # Bedrock with retries and failover to the configured backups (yuki.agent.failover);
                # every retry and failover lands in this session's log.
                self._client = FailoverClient.from_settings(self.settings, log=self.logger.log)
            return self._client

    def _used_model(self, requested: str) -> str:
        """The model the last call on this thread actually used (a failover may have switched it)."""
        used = getattr(self._client, "last_model", None)
        try:
            return (used() if callable(used) else None) or requested
        except Exception:
            return requested

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
            self._warm_activity()
            warmed = self._warm_model()
            # revamp D: the judge's own cached prompt, before the first judged call. The judge always runs on Claude
            # (leftMem), so when Doing things is routed to another AI it is warmed whatever that AI's warm-up gave
            # (no cache kept, an outage, a bad key: review L2 and second review J1); on Claude, as on main, only after
            # the thinker's warm-up reached it.
            routed = _routed(self.client)
            judge = self.authorizer is not None and not self.reader
            judge_warm = bool(judge and (warmed or routed) and self.authorizer.warm() is not None)
            self._warm_memory()
            self._warm_shell()
            self._warm_apps()
            # Started at logon (or after a wake) the network may not be up yet: when Bedrock
            # could not be reached at all, try the warm-up again a few times, quietly (the judge too, until warm).
            for delay in PREWARM_RETRY_S:
                if warmed is not False:
                    break
                threading.Event().wait(delay)
                warmed = self._warm_model()
                if judge and not judge_warm and (warmed or routed):
                    judge_warm = self.authorizer.warm() is not None

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

    def _warm_apps(self) -> None:
        """Read the Start menu list now, so the first app launch does not pay for it. Never raises."""
        started = time.perf_counter()
        try:
            count = self.dispatcher.prewarm_apps()
        except Exception as exc:
            self.logger.error(f"start menu prewarm failed: {type(exc).__name__}: {exc}", exc=exc)
            return
        self.logger.log("apps_prewarm", apps=count, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    def _warm_activity(self) -> None:
        """Start the WinRT import behind the media facts (returns at once). Never raises."""
        try:
            started = self.dispatcher.warm_activity()
        except Exception as exc:
            self.logger.error(f"activity prewarm failed: {type(exc).__name__}: {exc}", exc=exc)
            return
        self.logger.log("activity_prewarm", started=started)

    def _warm_memory(self) -> None:
        """Open memory, cache the portrait and read the resume context before the first request. Never raises."""
        if not self.memory.installed:
            return
        result = self.memory.warm()
        self.logger.log(
            "memory_prewarm",
            source=result.source,
            portrait_chars=len(result.portrait or ""),
            resume_chars=len(result.resume or ""),
            memory_session=getattr(self.memory, "session_id", None),
            error=result.error,
            elapsed_ms=result.elapsed_ms,
        )

    def _warm_model(self, profile: tuple[str, bool, bool] | None = None) -> bool | None:
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

        Returns True when warmed, False when Bedrock could not be reached at
        all (no network yet: worth retrying), None on any other failure.
        """
        started = time.perf_counter()
        if _keeps_no_cache(self.client):   # Settings → AI (report 67)
            self.logger.log("model_prewarm", skipped="the chosen AI for doing things keeps no prompt cache")
            return None
        try:
            response = self.client.messages.create(**self._request_params(warmup=True, profile=profile))
        except Exception as exc:
            self.logger.error(f"model prewarm failed: {type(exc).__name__}: {exc}", exc=exc)
            try:
                import anthropic

                return False if isinstance(exc, anthropic.APIConnectionError) else None
            except Exception:
                return None
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
        return True

    def warm_cache(self, effort: str | None = None, thinking: bool | None = None) -> bool | None:
        """Write the prompt cache now, the way the start-up warm-up does (one token out). Never raises.

        For a caller that knows a request is likely soon after a quiet spell (voice, at the start of a
        conversation, when the cache has expired). ``effort`` / ``thinking``: the profile the coming request
        runs at (a spoken request's fast first pass, v3.14): on Sonnet 5 each effort is its own cached prefix,
        so the start-up warm-up (the typed profile) does not serve it. Returns what :meth:`_warm_model` returns.
        """
        self._last_warm = time.monotonic()
        if effort is None and thinking is None:
            return self._warm_model()
        return self._warm_model(profile=(resolve_effort(effort) if effort else self.settings.effort,
                                         thinking is not False, effort == ""))

    @property
    def idle_s(self) -> float:
        """Seconds since this agent last started a request or warmed its cache (the prompt cache lives ~5 min)."""
        return time.monotonic() - max(self._request_started, getattr(self, "_last_warm", 0.0))

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

    def steer(self, text: str) -> None:
        """Add something the user said while this request runs. Safe from any thread.

        It is not an answer to a question (that is :meth:`answer`) and it does
        not interrupt anything: the text waits in an inbox and goes into the
        next message to the model, after the results of the tools running now,
        as the user's words. A request about to finish takes one more step
        when the inbox is not empty, so a correction made during the last model
        call is not dropped. Logged as ``steer``.
        """
        text = (text or "").strip()
        if text:
            self._steers.put(text)

    def set_app_named(self, app: str | None) -> None:
        """V4: the app or route the user named for the request running now (a voice hand-off whose thinker started
        before the talker's call was complete gets it this way). The authorization judge compares a send's target app
        with it from the next call on. Safe from any thread (one attribute)."""
        self._request_app_named = " ".join(str(app or "").split())[:80] or None

    def take_steers(self) -> list[str]:
        """Everything in the steering inbox, emptying it (what arrived too late for a request)."""
        out: list[str] = []
        while True:
            try:
                out.append(self._steers.get_nowait())
            except queue.Empty:
                return out

    def _steer_blocks(self) -> list[dict[str, Any]]:
        """The inbox as text blocks for the next message (empty when nothing arrived)."""
        texts = self.take_steers()
        if not texts:
            return []
        self.logger.log("steer", texts=texts, count=len(texts))
        if isinstance(getattr(self, "_steer_texts", None), list):
            self._steer_texts.extend(texts)   # the user's own words mid-run: a post-mortem's evidence
        for text in texts:
            self._note_words(text, how="added")   # and the authorization judge's
        blocks = [{"type": "text", "text": f"{STEER_LABEL}: {text}"} for text in texts]
        left = self._tasks_left()
        if left:
            # N10 (report 47): what they add mid-run never drops the rest of their list silently
            blocks.append({"type": "text", "text": (
                f"{CHECK_LABEL}: your list still has, in order: {'; '.join(left)}. What they just added comes first "
                f"when it is quick (do it now, add it to tasks), then the list goes on; it replaces the list only "
                f"when their words say so (\"forget that\", \"instead\"), and then mark those items dropped with "
                f"their words and say so.")})
        return blocks

    def _tasks_left(self) -> list[str]:
        """N10: the kept list's items still to do, in order."""
        return [str(t.get("what") or "") for t in getattr(self, "_tasks", None) or []
                if t.get("state") in ("todo", "doing")]

    def _with_list_left(self, text: str) -> str:
        """N10 (review): the closing words, with what the kept list still has when the request ends anyway (after the
        two holds): nothing is dropped without the user hearing it. Fixed words over the list's own items; logged."""
        left = [" ".join(x.split()) for x in self._tasks_left() if x.strip()]
        if not left:
            return text
        self.logger.log("tasks_left_said", left=left)
        return f"{text.rstrip()} Still left from your list: {'; '.join(left)}.".strip()

    def cancel(self, reason: str = CANCELLED) -> None:
        """Ask the run to stop. Safe to call from any thread, at any time.

        ``reason`` is :data:`CANCELLED` (the user stopped it) or :data:`WITHDRAWN` (a newer
        request of the user's replaced it): the run ends the same way, the words differ, and the
        caller reads :attr:`cancel_reason` to show a withdrawn run quietly.

        The flag is a :class:`threading.Event`, so setting it needs no lock and
        is visible immediately to the thread driving the generator. That thread
        checks it at three points: before every model request, before dispatching
        every tool, and again after the ``tool_call`` event has been handed to the
        caller. The run then ends with ``error("Cancelled by the user.")``; any
        tool calls the model had already asked for are answered with a "not run"
        result, because the API insists on one result per call.

        A tool already in flight is not interrupted -- there is no safe way to
        abort a keystroke halfway. A model response that is streaming is dropped
        at its next event when none of its tools has run yet (the connection is
        closed, nothing of it is kept), so a withdrawn run frees the lane within
        a moment instead of after the whole response.
        """
        self.cancel_reason = reason if reason in (WITHDRAWN, PARKED) else CANCELLED
        self._cancelled.set()
        self._send_wake.set()   # a send countdown waiting now ends at once: nothing is sent
        self._pause_wake.set()  # appTakeover: a paused request ends at once (nothing else is done)

    # -- appTakeover: the user took over -------------------------------------------------------------------------

    def pause(self, why: str = "") -> bool:
        """The user took over the keyboard or mouse (``why``: ``keys`` | ``click`` | ``mouse``): no new action of this
        request starts until :meth:`resume` (an action in flight finishes its own part; one waiting for the user to
        stop typing is not sent). Safe from any thread. ``False`` when nothing is acting, it is stopping, or it is
        already paused."""
        with self._pause_lock:
            if self._acting_token is None or self._cancelled.is_set() or self._paused.is_set():
                return False
            if self._awaiting_answer or self._send_open is not None or time.monotonic() < self._pause_quiet_until:
                # Yuki asked something, or a send counts down on the island: the user's hand is on Yuki's card (the
                # answer, Stop, a chip), not taking over; and for a moment after, while that hand moves on
                return False
            allowed = self.takeover_ok
            if callable(allowed):
                try:
                    if not allowed():
                        return False   # a full screen in front: a pause could not be seen there (the watch goes on)
                except Exception:  # noqa: BLE001 - unknown: pause as usual
                    pass
            self._pause_why, self._pause_at = str(why or ""), time.monotonic()
            self._pauses += 1
            self._resume_asked = False
            self._paused.set()
            self._pause_wake.clear()
        self.logger.log("takeover", state="paused", why=self._pause_why, n=self._pauses)
        self._tell_pause("paused", self._pause_why)
        return True

    def _tell_pause(self, state: str, why: str, seconds: float = 0.0) -> None:
        tell = self.on_pause
        if tell is None:
            return
        try:
            tell(state, why, seconds)
        except Exception:  # noqa: BLE001 - presentation only
            pass

    def resume(self, words: str | None = None, *, by: str = "") -> bool:
        """Go on after the user took over (``by``: who asked: the island, the run card, voice, typing). ``words``: what
        they typed or said with it, added to the request as theirs (:meth:`steer`). The request reads the screen again
        before its next step. Safe from any thread. ``False`` when it is not paused.

        The pause itself ends where the request waits (:meth:`_hold_paused`): a resume asked before the request got
        there (Resume pressed while a model call was still out) still reads the screen again first, and the island is
        told "resumed" from there, never left saying paused."""
        with self._pause_lock:
            if not self._paused.is_set() or self._resume_asked:
                return False
            if words and str(words).strip():
                self.steer(str(words))
            self._resume_asked = True
            self._pause_wake.set()
        self.logger.log("takeover", state="resume_asked", by=by or None, words=bool(words and str(words).strip()))
        return True

    @property
    def paused(self) -> bool:
        """Whether the request is paused because the user took over (and nobody asked it to go on yet). Safe to read
        from any thread."""
        return self._paused.is_set() and not self._resume_asked

    def _watch_on(self, grace_s: float = 0.0) -> None:
        """Start watching for the user taking over (once per acting stretch; never while paused). Never raises."""
        if self.takeover is None or self._watch is not None or self._paused.is_set() or self._cancelled.is_set():
            return
        try:
            self._watch = self.takeover(self._on_takeover, grace_s=grace_s)
            self._watch.start()
        except Exception as exc:  # noqa: BLE001 - the request goes on without the watch
            self._watch = None
            self.logger.log("takeover", state="watch_failed", error=f"{type(exc).__name__}: {exc}")

    def _watch_off(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            try:
                watch.stop()
            except Exception:  # noqa: BLE001
                pass

    def _on_takeover(self, kind: str) -> bool:
        """The watch saw the user take over (its own thread). ``False``: not taken as a pause now (Yuki is asking, a
        send counts down): the watch goes on."""
        return self.pause(kind)

    def _hold_paused(self) -> Iterator[AgentEvent]:
        """When the user took over: say so (:class:`Paused`), wait until they resume, stop, or the limit passes, and
        return whether it goes on (``False``: not paused, or it ended; a stop is then this request's cancel)."""
        if not self._paused.is_set() or self._cancelled.is_set():
            return False
        self._watch_off()
        why, since = self._pause_why, self._pause_at or time.monotonic()
        yield Paused("paused", why)
        limit = float(self.pause_timeout_s or 600.0)
        timed_out = False
        while not self._resume_asked and not self._cancelled.is_set():
            left = limit - (time.monotonic() - since)
            if left <= 0:
                timed_out = True
                break
            self._pause_wake.wait(min(0.5, left))
            self._pause_wake.clear()
        took = time.monotonic() - since
        self._paused_s += took
        self._waited_s += took    # the user's time, not the work's
        if timed_out:
            self.logger.log("takeover", state="ended", why=why, seconds=round(took, 1))
            self._stopped_paused = True
            self._tell_pause("ended", why, round(took, 1))
            yield Paused("ended", why, round(took, 1))
            self.cancel()
            return False
        if self._cancelled.is_set():
            self._stopped_paused = True
            self.logger.log("takeover", state="stopped", why=why, seconds=round(took, 1))
            return False
        with self._pause_lock:
            self._paused.clear()
            self._resume_asked = False
        self.logger.log("takeover", state="resumed", why=why, seconds=round(took, 1))
        self._tell_pause("resumed", why, round(took, 1))
        yield Paused("resumed", why, round(took, 1))
        self._watch_on(grace_s=RESUME_GRACE_S)
        return True

    def _resume_note(self, seconds: float) -> str:
        """What the model is told when the request goes on after the user took over."""
        how = TAKEOVER_WORDS.get(self._pause_why, "took over the keyboard or mouse")
        return (f"{CHECK_LABEL}: while you worked the user {how}, so you were paused for {round(seconds)} s; they have "
                f"now let you go on. They may have changed things: the screen below was read again just now. Check "
                f"what your earlier steps left and go on from what it shows, not from what you saw before; a step "
                f"marked not run did not happen.")

    def _resume_view(self) -> tuple[int, str] | None:
        """The window in front, read again in full as the request goes on after a pause (the user may have changed
        it): ``(hwnd, text)`` for the next message, or ``None``. Yuki's own window in front (they typed to Yuki to go
        on) is not read. Never raises."""
        started = time.perf_counter()
        try:
            hwnd = self.dispatcher.foreground_hwnd()
            if hwnd is None:
                return None
            from yuki.agent.tools import _is_own_window

            if _is_own_window(hwnd):
                self.logger.log("takeover", state="resume_view_skipped", why="Yuki's own window is in front")
                return None
            view = self.dispatcher.window_view(int(hwnd), settle=False)
        except Exception as exc:  # noqa: BLE001 - the overview still goes with the note
            self.logger.log("takeover", state="resume_view_failed", error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            self._auto_view_s += time.perf_counter() - started
        text = (f"The window in front now, read again after the pause ({view.elapsed_ms:.0f} ms): its elements are "
                f"ready to act on.\n" + wrap(view.text, source=f"the window {hwnd} (its on-screen text)",
                                             nonce=self._nonce))
        self.logger.perception("resume_view", size_chars=len(text), elapsed_ms=(time.perf_counter() - started) * 1000,
                               payload={"hwnd": hwnd, "summary": getattr(view, "summary", None)})
        self._evidence[int(hwnd)] = "looked"
        self._last_view_text = view.text
        return int(hwnd), text

    def send_choice(self, kind: str, value: str) -> bool:
        """appPT: the user tapped a chip on the send countdown: ``kind`` ``who`` or ``where``, ``value`` one of the
        alternatives the countdown showed. The pending send is not run; the swap goes to the brain as the user's words
        and the countdown starts again when the send is ready for the new person or place. Safe from any thread.
        ``False`` when no countdown is open or the value was not offered."""
        with self._send_lock:
            open_ = self._send_open
            if open_ is None or kind not in ("who", "where"):
                return False
            if str(value) not in (open_.get("who_alts") if kind == "who" else open_.get("where_alts") or ()):
                return False
            self._send_choice = (kind, str(value))
        self._send_wake.set()
        return True

    @property
    def cancelled(self) -> bool:
        """Whether a cancellation is pending. Safe to read from any thread."""
        return self._cancelled.is_set()

    @property
    def awaiting_answer(self) -> bool:
        """Whether the agent is paused on a question."""
        return self._awaiting_answer

    # -- main entry point --------------------------------------------------

    def run(
        self, request: str, *, origin_hwnd: int | None = None, effort: str | None = None,
        model: str | None = None, thinking: bool | None = None, lean: bool = False,
        view_hwnd: int | None = None, escalate: tuple[str | None, bool | None] | None = None,
        voice: bool = False, memory_query: str | None = None, previous_query: str | None = None,
        memory_apps: Iterable[str] | None = None, pack: "RequestPack | None" = None,
        user_words: Iterable[str] | str | None = None, app_named: str | None = None,
    ) -> Iterator[AgentEvent]:
        """Handle one user request, yielding events as work happens.

        Args:
            request: What the user typed.
            origin_hwnd: The window that was in front when the user summoned
                Yuki, when the caller knows it (a UI whose own window takes the
                keyboard to be typed into). Without it, the request origin
                reports whatever is in front now, and when that is Yuki's own
                window, the top window beneath it.
            effort: ``output_config.effort`` for this request only (``""``: none sent,
                for a model that takes no effort parameter; the caller's
                setting for where the request came from, e.g.
                ``Settings.voice_brain_effort`` for a spoken one); ``None``: the
                agent's own :attr:`Settings.effort`.
            model: Model id (or alias) for this request only; ``None``: the agent's.
            thinking: ``False`` turns thinking off for this request (``None``: the
                agent's adaptive thinking).
            lean: A slim memory block for this request: the profile and rules
                layers stay (the system prompt), the rest of the portrait and the
                resume are left out (to-dos, know-how and commitments stay). For a
                spoken request, chosen by its source, never its words
                (docs/VOICE.md §7).
            view_hwnd: Read this window now and attach it to the request, element ids
                and all, the way the view after actions is attached: for a request
                about something inside that window (the caller's decision; voice: the
                talker's ``screen`` mark), so the first model call can click it
                instead of spending a whole round trip on ``look_at_window``.
            escalate: ``(effort, thinking)`` for this request's model calls from the
                moment a measured signal says its fast first pass needs judgement:
                an action failed or its own report does not verify its effect, the
                loop breaker tripped, the user was asked something, a finish or a
                claim about the screen had no evidence, or a tool call came back
                written as text (docs/VOICE.md v3.14). It holds for the rest of the
                request (every switch of effort rewrites the whole prompt cache on
                Sonnet 5, measured). ``None``: fixed for the whole request.
            memory_query: The user's own words, what memory is searched with (the
                related memories and know-how). A spoken request's hand-off carries
                ~1.8k characters of Yuki's own notes and the embedder reads ~128
                tokens (revamp 02 D1). ``None``: the request's own words, Yuki's
                bracketed notes left out (:func:`yuki.agent.memory.memory_query_of`).
            previous_query: The previous request's words, for a follow-up; ``None``:
                the memory query of this conversation's previous request.
            memory_apps: Apps the caller says the request names (their know-how
                is attached at a lower relevance floor, like the app in front's).
            pack: A request pack built ahead (:meth:`build_pack`, e.g. while the
                user was still speaking): its overview, origin, window view and
                memory are used instead of being built now. A pack older than
                ``Settings.pack_max_age_s`` has its screen half (overview, origin,
                view) read again; its memory half is kept.
            user_words: A spoken request's heard turns (speech to text, oldest first): what the authorization judge
                reads as the user's own words, while the talker's ``request`` is shown to it as Yuki's reading, which
                never authorises a send (review 34 H4). ``None``: the heard turns in the hand-off's own notes; a typed
                request's own text is the user's words.
            app_named: The app or route the user named for this request in their own words (V4, 3 Oct: the voice
                talker's ``app_named``, taken from the heard words). It wins over the usual route: the task frame says
                so above the person's routes, and the authorization judge compares a send's target app with it.

        Yields:
            :class:`~yuki.log.events.AgentEvent` values. The generator pauses on
            :class:`~yuki.log.events.AskUser` and finishes after exactly one
            :class:`~yuki.log.events.Final` or :class:`~yuki.log.events.ErrorEvent`.
        """
        self._cancelled.clear()
        self.cancel_reason = CANCELLED
        self._watch_off()
        self._paused.clear()
        self._pause_wake.clear()
        self._pauses, self._paused_s, self._stopped_paused, self._pause_why = 0, 0.0, False, ""
        self._pause_quiet_until, self._resume_asked = 0.0, False
        stale = self.take_steers()
        self._pending_answer = None
        self._awaiting_answer = False
        self._request_started = time.monotonic()
        self._waited_s = 0.0
        self._model_calls = 0
        self._call_history = {}
        self._breaker.reset()
        self._breaker_stop_sent = False
        self._written_calls = 0
        self._nonce = new_nonce()
        self._confirmed = set()
        self._request_text = request
        #: V4: the app or route the user named for this request (the voice's structured field), or None.
        self._request_app_named = " ".join(str(app_named or "").split())[:80] or None
        self._requests += 1
        self._actions = []
        #: What this request's actions measured (v3.15 counters; the done's optional learned note is a hint for practice
        #: memory's distiller, which reads the run trace).
        self._learning: dict[str, Any] = {"actions": 0, "trouble_at": None, "told": False, "asked": False,
                                          "saved": False, "targets": Counter(), "note": None, "worked_at": None}
        #: This request's run trace (practice memory, Phase C: ``yuki.memory.practice.record_run``): one dict per tool
        #: call - tool, target app and control, input, verdict and why, result, duration, the model call it came from
        #: (its cost is shared out when the request ends) - plus the window ids, element ids and coordinates it
        #: touched (never meaning: no lesson may carry them), the words the user added mid-run, the playbooks shown.
        self._trace: list[dict[str, Any]] = []
        self._trace_numbers: set[int] = set()
        #: appOpen review F6: the openings of this request still starting through a launcher (trace index, watch).
        self._launch_watch_steps: list[tuple[int, dict[str, Any]]] = []
        self._call_costs: dict[int, float] = {}
        self._steer_texts: list[str] = []
        #: vfix3 review F6: know-how sent with the action it describes, kept when the request ends well.
        self._held_how: list[dict[str, Any]] = []
        self._practice_shown: list[int] = []
        # Revamp D: the judge's per-request state, the consults taken, the sends and their confirmation, the text
        # typed into each window (an Enter later sends it), the trace steps an independent confirmation may still back,
        # the window view last attached (the consult's summary), and a stop for too many denials.
        if self.authorizer is not None:
            self.authorizer.begin_request()
        self._consults: list[dict[str, Any]] = []
        self._send_consulted = False
        self._close_call_consulted = False
        self._sends: list[dict[str, Any]] = []
        #: N0 (report 47): the situation checks made in this request (a consequential step needs one it had read).
        self._situation_checks: list[situation_check.CheckRecord] = []
        #: appPT: the questions the user answered in this request (``ask_user``: question, answer). A send whose exact
        #: text they wrote in an answer needs no countdown (that was their go-ahead on it); an answer about who or
        #: where ("which Sam?", "Discord or WhatsApp?") is no go-ahead on the text (review F7).
        self._answered: list[tuple[str, str]] = []
        #: appPT: the send stopped in its countdown this request: ``None`` (none), else whether its text was already in
        #: the box (the run's cancel line then says plainly what did not happen, for the screen and the voice).
        self._send_stopped: bool | None = None
        #: N10: the request's kept list (``tasks``), and how often a done was held for what is still on it.
        self._tasks: list[dict[str, Any]] = []
        self._tasks_held = 0
        self._typed: dict[int, str] = {}
        self._files_read = False
        self._unconfirmed: list[tuple[int, int | None]] = []
        self._last_view_text: str | None = None
        self._denial_stop: str | None = None
        self._pending_notes: list[dict[str, Any]] = []
        self._reset_accounting()
        self.last_summary = None
        started_at = datetime.now()
        started_epoch = time.time()
        self._request_effort = resolve_effort(effort) if effort else None
        #: ``effort=""``: send no effort at all (a model that takes none, e.g. Haiku 4.5).
        self._request_omit_effort = effort == ""
        self._request_model = resolve_model(model) if model else None
        self._request_thinking = thinking
        self._request_lean = bool(lean)
        #: A spoken request: its step tools take a required say_now (v3.14b, ``tools.with_step_lines``).
        self._request_voice = bool(voice)
        self._escalate_to = escalate if escalate and (escalate[0] or escalate[1] is not None) else None
        self._escalation = None
        self._evidence = {}
        self._claim_rounds = 0
        model, effort = self._request_model or self.settings.model, self._request_effort or self.settings.effort
        self.logger.reset_usage()
        self.logger.begin_turn()
        self.logger.user_message(request)
        if stale:
            # Added after the previous request's last step: the caller hears of
            # it through take_steers() at the end of a request, so this is rare.
            self.logger.log("steer", texts=stale, count=len(stale), dropped="arrived between requests")
        # How the request ended, for the summary. "abandoned" survives only when
        # the caller stopped iterating before a final or an error arrived.
        outcome = "abandoned"
        reply = ""
        drive: Iterator[AgentEvent] | None = None
        try:
            query = " ".join(str(memory_query or "").split()) or memory_query_of(request)
            self._note_request_words(request, query, user_words)   # whose words each is, for the judge
            fresh = pack is not None and pack.age_s <= float(getattr(self.settings, "pack_max_age_s", 20.0))
            if pack is not None:
                self.logger.log("request_pack", age_s=round(pack.age_s, 2), fresh=fresh, build_ms=pack.build_ms,
                                has_view=pack.view is not None, has_memory=pack.memory is not None)
            if origin_hwnd is None and pack is not None:
                origin_hwnd = pack.origin_hwnd
            if view_hwnd is None and pack is not None:
                view_hwnd = pack.view_hwnd
            overview, _ = self._capture_overview(prebuilt=pack.overview if fresh else None)
            origin = pack.origin if fresh else self._request_origin(origin_hwnd)
            self._close_dangling_tools(overview)
            # The window the request is about, read now (v3.9). With thinking off (spoken requests) it goes in
            # as a look_at_window call already made and its result, the shape the model knows; pasted into the
            # request instead, Sonnet wrote its first tool call out as text in several offline replays.
            view = self._request_view(int(view_hwnd), prebuilt=pack.view if fresh else None) if view_hwnd else None
            if view is not None:
                self._evidence[int(view_hwnd)] = "looked"
                self._last_view_text = view[1]
            as_call = view is not None and self._request_thinking is False
            if view is not None and not as_call:
                overview = f"{overview}\n\n{view[0]}"
            memory_text, commitments_text = self._memory_context(
                request, first=self._requests == 1, lean=self._request_lean, query=query,
                previous=" ".join(str(previous_query or "").split()) or self._previous_query,
                apps=list(memory_apps or ()), origin_hwnd=origin_hwnd,
                prefetched=pack.memory if pack is not None and pack.memory is not None else None,
            )
            self._this_query = query
            self.context.add_request(
                request,
                overview,
                memory_text=memory_text,
                origin_text=origin,
                standing_text=commitments_text,
            )
            if as_call:
                self._add_view_call(int(view_hwnd), view[1], overview)
            drive = self._drive()
            for event in drive:
                if isinstance(event, Final):
                    outcome, reply = "final", event.text
                elif isinstance(event, ErrorEvent):
                    outcome = self.cancel_reason if self._cancelled.is_set() else "error"
                    reply = event.text
                yield event
        except KeyboardInterrupt:
            outcome = "cancelled"
            reply = reply or "Interrupted."
            raise
        except Exception as exc:  # never let a crash escape into the REPL
            message = f"{type(exc).__name__}: {exc}"
            self.logger.error(message, exc=exc)
            outcome = self.cancel_reason if self._cancelled.is_set() else "error"
            reply = message
            yield ErrorEvent(message)
        finally:
            if drive is not None:
                drive.close()
            self._acting_done()
            self.logger.usage_total()
            self._summarize(
                request, outcome=outcome, started_at=started_at, model=model, effort=effort
            )
            # After the reply is out (the caller already has the final or error
            # event): hand the exchange to memory's background writer, then
            # shrink the conversation for the next request.
            self._log_turn(
                request, reply=reply, outcome=outcome, at=started_epoch
            )
            self._record_run(request, reply=reply, outcome=outcome, at=started_epoch)
            self._keep_lessons(outcome=outcome, at=started_epoch)
            self._keep_held_how(outcome=outcome)
            self._record_surfaced(reply=reply, at=started_epoch)
            words = self._this_query or memory_query_of(request)
            if words:
                self._said.append({"at": started_epoch, "text": words, "who": "user"})
            if reply and outcome == "final":
                self._said.append({"at": time.time(), "text": reply, "who": "yuki"})
            self._previous_request = request
            self._previous_query = self._this_query or memory_query_of(request) or None
            self._this_query = None
            self._end_request(request, reply=reply, outcome=outcome, at=started_at)

    # -- per-request accounting --------------------------------------------

    def _reset_accounting(self) -> None:
        """Zero the time/token/cost accumulators for a new request."""
        self._model_s = 0.0
        self._tool_s = 0.0
        self._overview_s = 0.0
        self._auto_view_s = 0.0
        self._tool_counts: Counter[str] = Counter()
        self._tokens: dict[str, int] = dict.fromkeys(TOKEN_KEYS, 0)
        self._cost_usd = 0.0
        self._unpriced: set[str] = set()
        self._models_used: list[str] = []
        #: What the memory block of this request carried (``request_summary``).
        self._memory_info: dict[str, Any] = {}
        #: Per model call: output, input and cache tokens, the tools it called, its first token (revamp D measures).
        self._call_stats: list[dict[str, Any]] = []
        self._first_token_ms: list[float | None] = []

    def _judge_cost(self) -> float:
        """What the authorization judge cost this request so far (revamp D; an early allow's cost lands a moment later,
        from the judge's own thread)."""
        authorizer = getattr(self, "authorizer", None)
        return float(getattr(authorizer, "cost_usd", 0.0) or 0.0) if authorizer is not None else 0.0

    def _all_unpriced(self) -> set[str]:
        """The models with no known price this request: the agent's own calls, the second look's and the judge's."""
        authorizer = getattr(self, "authorizer", None)
        judged = getattr(authorizer, "unpriced", None) if authorizer is not None else None
        return set(self._unpriced) | (set(judged) if isinstance(judged, set) else set())

    def _account_response(self, model: str, usage: Any) -> float | None:
        """Fold one real (non-warm-up) response into this request's totals; returns its cost (``None``: unpriced).

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
        return cost

    def _summarize(
        self, request: str, *, outcome: str, started_at: datetime, model: str, effort: str
    ) -> None:
        """Write ``request_summary`` and the ``requests.csv`` line. Never raises.

        Wall time includes the time spent waiting for the user's answers, which
        is also reported on its own. Model time is every round trip (failed ones
        included) minus the time tools ran while its response was still
        streaming; tool time is dispatching the tools the model called, in the
        stream or after it; the per-turn desktop overview (plus the request
        origin's activity facts) is ``overview_s`` and
        the window view attached after actions ``auto_view_s``. Whatever is left of the
        wall time is the loop's own overhead. Pre-warm calls are not in here:
        they are the separate ``startup_cost`` record.
        """
        try:
            wall_s = time.monotonic() - self._request_started
            models = self._models_used or [model]
            unpriced = self._all_unpriced()
            cost = None if unpriced else round(self._cost_usd + self._judge_cost(), 6)
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
                "auto_view_s": round(self._auto_view_s, 2),
                "model_calls": self._model_calls,
                "escalated": dict(self._escalation) if self._escalation else None,
                "tool_calls": dict(self._tool_counts),
                "tokens": dict(self._tokens),
                "thinking_tokens": self.logger.usage.thinking_tokens,
                "cost_usd": cost,
                "unpriced_models": sorted(unpriced),
                "memory": {
                    **self._memory_info,
                    **{
                        f"{name}_calls": self._tool_counts.get(name, 0)
                        for name in sorted(MEMORY_TOOLS)
                    },
                },
                # revamp D: per-call facts, the judge, the second looks and the sends of this request
                "calls_detail": list(getattr(self, "_call_stats", [])),
                "judge": ({"calls": self.authorizer.calls, "denials": self.authorizer.denials,
                           "ms": list(self.authorizer.ms), "cost_usd": round(self.authorizer.cost_usd, 6)}
                          if getattr(self, "authorizer", None) is not None else None),
                "consults": list(getattr(self, "_consults", [])),
                "sends": list(getattr(self, "_sends", [])),
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

    # -- after the request: the turn log and compaction -----------------------

    def _log_turn(self, request: str, *, reply: str, outcome: str, at: float) -> None:
        """Queue this exchange for memory's turn log. Returns at once; never raises.

        Logged as ``memory_turn_queued`` now and ``memory_turn_logged`` (row
        id or error) when the background writer is done with it.
        """
        request_id = f"{self.lane}-{self._requests}"
        actions = list(self._actions)
        logger = self.logger

        def done(report: dict[str, Any]) -> None:
            logger.log("memory_turn_logged", **report)

        try:
            queued = self.memory.log_turn(
                request_id=request_id,
                at=at,
                user_text=request,
                reply_text=reply,
                actions=actions,
                outcome=outcome,
                on_done=done,
            )
            self.logger.log(
                "memory_turn_queued",
                queued=queued,
                session_id=getattr(self.memory, "session_id", None),
                request_id=request_id,
                outcome=outcome,
                reply_chars=len(reply),
                actions=actions,
            )
        except Exception as exc:  # memory must never break a request
            self.logger.error(f"memory turn log failed: {type(exc).__name__}: {exc}", exc=exc)

    def _end_request(self, request: str, *, reply: str, outcome: str, at: datetime) -> None:
        """Record the request's outcome and collapse old requests. Never raises.

        When the request that carried the portrait in full is collapsed, the
        portrait is no longer in the conversation, so the next request attaches
        it again instead of pointing back at it.
        """
        try:
            self.context.end_request(
                RequestRecord(
                    text=request, reply=reply, actions=list(self._actions), at=at,
                    outcome=outcome,
                )
            )
            info = self.context.compact(keep=KEEP_FULL_REQUESTS)
            if (
                info is not None
                and self._portrait_sent is not None
                and self._portrait_sent[1] in info["turns"]
            ):
                self._portrait_sent = None
        except Exception as exc:  # housekeeping must never break a request
            self.logger.error(f"context compaction failed: {type(exc).__name__}: {exc}", exc=exc)

    def _acting_start(self) -> None:
        """Before the request's first action on the desktop: tell memory that what
        appears on screen from now until the request ends is Yuki's doing, at
        the user's request (:meth:`MemoryAccess.acting_begin`). Plumbing: it
        changes nothing about what the agent does; never raises. Every later action of
        the request refreshes the marker (memory ignores one not refreshed for
        ``yuki.memory.acting.MAX_AGE_S``, so an end that fails can never hold it long)."""
        refresh = self._acting_token is not None
        token = self._acting_token or f"{self.lane}-{self.logger.session_id}-{self._requests}"
        try:
            begin = getattr(self.memory, "acting_begin", None)
            published = bool(begin(token, self._request_text or "", lane=self.lane)) if callable(begin) else False
        except Exception:
            published = False
        self._acting_token = token
        if not refresh or not published:
            self.logger.log("memory_acting", state="refresh" if refresh else "begin", token=token,
                            published=published)
        self._watch_on()   # appTakeover: from the first action on, the user's taking over pauses the request

    def _acting_done(self) -> None:
        """The request ended: withdraw the marker, if one was published. Never raises."""
        self._watch_off()
        self._paused.clear()
        self._resume_asked = False
        token, self._acting_token = self._acting_token, None
        if token is None:
            return
        withdrawn = False
        for _ in range(2):   # the marker file retries sharing violations itself; one more try on top
            try:
                end = getattr(self.memory, "acting_end", None)
                withdrawn = bool(end(token)) if callable(end) else False
            except Exception:
                withdrawn = False
            if withdrawn or not callable(getattr(self.memory, "acting_end", None)):
                break
        self.logger.log("memory_acting", state="end", token=token, withdrawn=withdrawn,
                        **({} if withdrawn else {"leak": "memory drops it once it is stale"}))

    def _learn_step(self, target: int | None, worked: bool) -> None:
        """One action of this request, for its learned note: counted, and the first that did not work (failed or
        not verified) marked (v3.15)."""
        learning = getattr(self, "_learning", None)
        if learning is None:
            return
        learning["actions"] += 1
        if not worked and learning["trouble_at"] is None:
            learning["trouble_at"] = learning["actions"]
        if worked:
            learning["worked_at"] = learning["actions"]
        if target is not None:
            learning["targets"][int(target)] += 1

    def _learn_due(self) -> bool:
        """Whether this request's done must carry ``learned`` (v3.15): a measured trigger, never the words. It took
        :data:`LEARN_AFTER_ACTIONS` or more actions, or an action failed or was not verified and a later one worked;
        something worked (a verified action: nothing is learned from a job where nothing did), and nothing was kept
        with remember_how in it already."""
        learning = getattr(self, "_learning", None)
        if learning is None or learning["saved"] or learning.get("worked_at") is None:
            return False
        trouble = learning["trouble_at"]
        return learning["actions"] >= LEARN_AFTER_ACTIONS or (trouble is not None and learning["worked_at"] > trouble)

    def _window_of(self, hwnd: int | None) -> dict[str, Any] | None:
        """The desktop overview's entry for ``hwnd`` (process name, title), or ``None``."""
        if hwnd is None or not isinstance(self._overview_payload, dict):
            return None
        return next((w for w in self._overview_payload.get("windows") or []
                     if isinstance(w, dict) and w.get("hwnd") == hwnd), None)

    def _learned_control(self, name: str, target: dict[str, Any]) -> int:
        """How many earlier unverifiable steps of this control were confirmed afterwards (practice memory's run traces),
        when that makes it expected-unverifiable: at least ``LEARNED_MIN_CONFIRMED`` and none contradicted; else 0."""
        fetch = getattr(self.memory, "call_method", None)
        if not callable(fetch) or not getattr(self.memory, "installed", False):
            return 0
        try:
            record = fetch("control_record", target.get("app"), name, target.get("control"), timeout_s=0.3) or {}
        except Exception:  # noqa: BLE001 - memory missing: nothing learned
            return 0
        confirmed = int(record.get("unverifiable_confirmed") or 0)
        if confirmed >= finish_gate.LEARNED_MIN_CONFIRMED and not int(record.get("contradicted") or 0):
            return confirmed
        return 0

    def _mark_confirmations(self, hwnd: int | None, verdict: tuple[bool | None, str]) -> None:
        """An unverifiable step stays open until something independent backs it in this request: a later verified step
        in the same window (or a send seen in its chat) marks it confirmed in the run trace (practice memory then
        learns the control as expected-unverifiable)."""
        trace = getattr(self, "_trace", None)
        if trace is None or not hwnd:
            return
        if verdict[0] is True:
            keep = []
            for index, window in self._unconfirmed:
                if window == hwnd and index < len(trace):
                    trace[index]["confirmed"] = True
                else:
                    keep.append((index, window))
            self._unconfirmed = keep
        elif verdict[0] is None and trace:
            self._unconfirmed.append((len(trace) - 1, hwnd))

    def _trace_step(self, name: str, tool_input: dict[str, Any], *, ok: bool | None, verdict: bool | None = None,
                    why: str = "", result: str = "", ms: float = 0.0, outcome: ToolOutcome | None = None,
                    kind: str | None = None, control: str | None = None) -> None:
        """One step of this request's run trace (practice memory, Phase C): the tool, the app it acted in and the
        control's role and name and the window's title (never a window number as meaning: the ids and coordinates it
        touched go to the run's numbers), its input, the verdict and why, what it reported, how long it took and the
        model call it came from. Plumbing; never raises, never changes what the request does."""
        trace = getattr(self, "_trace", None)
        if trace is None or len(trace) >= TRACE_STEPS:
            return
        try:
            payload = outcome.payload if outcome is not None and isinstance(outcome.payload, dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            if kind is None:
                kind = ("action" if name in ACTION_TOOL_NAMES else "memory" if name in MEMORY_TOOLS
                        else "ask" if name == "ask_user" else "read" if name in PERCEPTION_TOOLS else "control")
            hwnd = (_view_target(name, tool_input, details)[0] if kind == "action"
                    else _as_hwnd(tool_input.get("hwnd")))
            window = self._window_of(hwnd)
            app = str((window or {}).get("process_name") or "") or None
            if app is None and kind == "action":
                app = _foreground_app(self._overview_payload)
            parts = []
            describe = getattr(self.dispatcher, "element_info", None)
            element = describe(tool_input) if callable(describe) else None
            if element:
                parts.append(f"{element.get('role') or 'element'} \"{element.get('name') or ''}\"")
            if window and window.get("title"):
                parts.append(f"window \"{str(window['title'])[:80]}\"")
            numbers = [hwnd] if hwnd else []
            for key in ("hwnd", "expect_hwnd", "element", "read", "x", "y"):
                v = tool_input.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    numbers.append(int(v))
            for key in ("hwnd", "foreground_hwnd"):
                v = details.get(key)
                if isinstance(v, int) and not isinstance(v, bool):
                    numbers.append(v)
            self._trace_numbers.update(n for n in numbers if n)
            acting = {k: v for k, v in acting_input(tool_input).items()
                      if k not in (RISK_FIELD, FINISHES_FIELD, FINISH_WORDS_FIELD, PLAYBOOK_FIELD)}
            # (``what``, the brain's plain words for the step, stays in the kept input: the ledger shows it; the
            # signature leaves it out, as every note field)
            sig = hashlib.sha1(call_signature(acting).encode("utf-8", "replace")).hexdigest()[:12]
            step = {"tool": name, "kind": kind, "app": app, "target": ", ".join(parts), "input": acting,
                    "ok": ok, "verdict": verdict, "why": why, "expected": finish_gate.is_expected(why),
                    "result": " ".join(str(result or "").split())[:400], "ms": round(float(ms or 0.0), 1),
                    "call": self._model_calls, "sig": sig, "playbook": tool_input.get(PLAYBOOK_FIELD),
                    "at": time.time()}
            if control:
                step["control"] = control
            launcher = details.get("launcher") if name == "launch_app" else None
            if isinstance(launcher, dict):
                # appOpen review F1: which launcher took it and when, so practice memory tells a game this launch
                # started from the user's own doing; review F6: a game still starting gets the voice hand's light watch
                step["input"] = {**acting, **{k: launcher[k] for k in ("handed_to", "handed_at") if launcher.get(k)}}
                if details.get("starting"):
                    self._watch_launch(launcher, len(trace))
            trace.append(step)
        except Exception as exc:  # noqa: BLE001 - the trace never breaks a request
            self.logger.log("trace_step_failed", tool=name, error=f"{type(exc).__name__}: {exc}")

    def _watch_launch(self, launcher: dict[str, Any], index: int) -> None:
        """appOpen review F6: a launch_app the launcher is still starting (a game) gets the same light, bounded watch
        as the voice hand's (``backend.watch_launch``: one window list a second, at most 180 s, never acting), on its
        own thread; its verdict joins this request's run trace as a step after the opening (:func:`with_watch_steps`),
        so the thinker's opening gets a real verdict too. One watch per entry: a second launch_app of the same game
        while it starts shares it. A backend without a watch (tests, stubs) keeps the opening as it was."""
        backend = getattr(getattr(self, "dispatcher", None), "backend", None)
        watch = getattr(backend, "watch_launch", None) if backend is not None else None
        if not callable(watch):
            return
        key = str(launcher.get("appid") or launcher.get("name") or "").lower()
        watches: dict[str, dict[str, Any]] = self.__dict__.setdefault("_launch_watches", {})
        entry = watches.get(key)
        if entry is None or entry.get("ended") is not None:
            entry = {"name": str(launcher.get("name") or ""), "began": time.time(), "ended": None, "result": None}
            logger = getattr(self, "logger", None)

            def run() -> None:
                try:
                    result = watch(launcher)
                except Exception as exc:  # noqa: BLE001 - a watch that failed says so
                    result = {"verified": None, "summary": f"the watch failed: {type(exc).__name__}"}
                entry["result"] = result if isinstance(result, dict) else {"verified": None, "summary": str(result)}
                entry["ended"] = time.time()
                if logger is not None:
                    try:
                        logger.log("launch_watch", verified=entry["result"].get("verified"),
                                   waited_s=round(entry["ended"] - entry["began"], 1),
                                   in_front=bool(entry["result"].get("in_front")),
                                   fullscreen=bool(entry["result"].get("fullscreen")))
                    except Exception:  # noqa: BLE001 - the log never breaks a watch
                        pass

            entry["thread"] = threading.Thread(target=run, name="yuki-launch-watch", daemon=True)
            watches[key] = entry
            entry["thread"].start()
        steps = getattr(self, "_launch_watch_steps", None)
        if steps is None:
            steps = self._launch_watch_steps = []
        steps.append((index, entry))

    def _record_surfaced(self, *, reply: str, at: float) -> None:
        """After the reply is out: which brief lines and memories this reply was written from, to memory's personal-ness
        log (``MemoryClient.record_surfaced``, revamp 09 P4.1; the nightly judge reads a sample). Ids only; a request
        with no reply records nothing. Returns at once; never raises."""
        record = getattr(self.memory, "record_surfaced", None)
        if not (reply or "").strip() or not callable(record) or not getattr(self.memory, "installed", False):
            return
        try:
            ctx = getattr(self, "_surfaced", None) or {}
            session = getattr(self.memory, "session_id", None)
            record(kind="reply", ref=f"turn:{session}:{self.lane}-{self._requests}",
                   brief_ids=list(ctx.get("brief_ids") or []), fact_ids=list(ctx.get("fact_ids") or []), at=at,
                   brief_at=ctx.get("brief_at"))
        except Exception as exc:  # noqa: BLE001 - the log never breaks a request
            self.logger.log("surfaced_failed", error=f"{type(exc).__name__}: {exc}")

    def _record_run(self, request: str, *, reply: str, outcome: str, at: float) -> None:
        """After the reply is out: this request's run trace to memory's background writer (practice memory, Phase C;
        ``MemoryClient.record_run``), with each step's share of its model call's cost, the words the user added
        mid-run, the playbooks the frame showed, the loop breaker's trips and the done's optional learned note (a hint
        for the distiller, never evidence). Returns at once; never raises."""
        trace = getattr(self, "_trace", None)
        record = getattr(self.memory, "record_run", None)
        if trace is None or not callable(record) or not getattr(self.memory, "installed", False):
            return
        try:
            per_call = Counter(s.get("call") for s in trace)
            steps = []
            for s in trace:
                cost = self._call_costs.get(s.get("call"))
                share = per_call.get(s.get("call")) or 0
                steps.append({**{k: v for k, v in s.items() if k != "call"},
                              "cost_usd": round(cost / share, 6) if cost is not None and share else None})
            numbers = set(self._trace_numbers)
            if isinstance(self._overview_payload, dict):   # every window number on screen is never meaning either
                numbers.update(int(w["hwnd"]) for w in self._overview_payload.get("windows") or []
                               if isinstance(w, dict) and isinstance(w.get("hwnd"), int))
            learning = getattr(self, "_learning", None) or {}
            run = {
                "source": "agent", "session_id": getattr(self.memory, "session_id", None),
                "request_id": f"{self.lane}-{self._requests}", "lane": "voice" if self._request_voice else self.lane,
                "at": at, "ended_at": time.time(), "words": self._this_query or memory_query_of(request),
                "reply": reply, "outcome": outcome, "steps": steps,
                "cost_usd": None if self._all_unpriced() else round(self._cost_usd + self._judge_cost(), 6),
                "model_calls": self._model_calls,
                "wall_s": round(time.monotonic() - self._request_started, 2), "note": learning.get("note"),
                "steers": list(self._steer_texts), "breaker": len(self._breaker.trips),
                "numbers": sorted(numbers), "injected": list(self._practice_shown),
                "app": _foreground_app(self._overview_payload),
            }
            logger = self.logger

            def done(report: dict[str, Any]) -> None:
                logger.log("run_trace_logged", **{k: v for k, v in report.items() if k != "result"},
                           learn=(report.get("result") or {}).get("learn"))

            watching = list(getattr(self, "_launch_watch_steps", None) or [])
            if watching:
                # appOpen review F6: an opening still starting is kept with its watch's verdict (at most ~3 min later,
                # off the request: the reply is out already)
                self._launch_watch_steps = []

                def after_watch() -> None:
                    try:
                        record(with_watch_steps(run, watching), on_done=done)
                    except Exception as exc:  # noqa: BLE001 - memory never breaks a request
                        logger.log("run_trace_failed", error=f"{type(exc).__name__}: {exc}")

                threading.Thread(target=after_watch, name="yuki-run-after-watch", daemon=True).start()
                self.logger.log("run_trace_held", watches=len(watching), steps=len(steps))
                return
            queued = record(run, on_done=done)
            self.logger.log("run_trace_queued", queued=queued, steps=len(steps), injected=run["injected"],
                            cited=sorted({str(s.get("playbook")) for s in steps if s.get("playbook")}))
        except Exception as exc:  # noqa: BLE001 - memory never breaks a request
            self.logger.error(f"run trace failed: {type(exc).__name__}: {exc}", exc=exc)

    #: N8: how long a session lesson is shown (s): practice memory's post-mortem learns a run within minutes when its
    #: budget allows (live 3 Oct its daily budget was spent by 19:09 and the troubled runs after it were held).
    LESSON_TTL_S = 4 * 3600.0

    def _keep_lessons(self, *, outcome: str, at: float) -> None:
        """N8 (report 47): this run's lessons for the rest of the session (a troubled run only). Never raises."""
        try:
            lessons = task_frame.session_lessons(getattr(self, "_trace", None) or [], outcome=outcome,
                                                 steers=list(getattr(self, "_steer_texts", None) or []), at=at)
            if lessons:
                self._session_lessons.append((time.time(), lessons))
                self.logger.log("session_lessons", apps=sorted(lessons), lines=sum(len(v) for v in lessons.values()))
                self._keep_pitfalls(outcome=outcome, at=at, lessons=lessons)
            self._keep_slider_way(outcome=outcome, at=at)
        except Exception as exc:  # noqa: BLE001 - a lesson never breaks a request
            self.logger.log("session_lessons", error=f"{type(exc).__name__}: {exc}"[:200])

    def _hold_how(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """vfix3 review F6: ``remember_how`` sent in the same response as the action it describes ("'my Japanese
        playlist' means <name>" with the play click) is held, not saved: what it says is learned only after it worked
        (:meth:`_keep_held_how`). Its text and app are checked now, so a bad call still fails at once."""
        text = " ".join(str(tool_input.get("text") or "").split())
        if not text:
            return ToolOutcome(name="remember_how", ok=False, summary="give the know-how as text",
                               content=[{"type": "text", "text": "Not saved: give the know-how as text."}])
        self._held_how.append({"input": dict(tool_input), "call": self._model_calls,
                               "steers": len(task_frame.users_steers(self._steer_texts))})
        return ToolOutcome(name="remember_how", ok=True, summary="held until the step it describes has worked",
                           content=[{"type": "text", "text": "Held: it is kept once this request ends with that step "
                                                             "done, and dropped if it fails or they correct you."}],
                           payload={"held": True})

    def _keep_held_how(self, *, outcome: str) -> None:
        """At the end of a request: the know-how :meth:`_hold_how` held is kept when the request ended with its
        answer (``final``), every action from the response that sent it on succeeded with no contradicted effect,
        and the user added no words after it (a correction); else dropped. Learned after one success, by the run's
        own facts (outcome, ok, verdicts). Saved on a background thread; never raises."""
        held, self._held_how = list(getattr(self, "_held_how", None) or []), []
        if not held:
            return
        trace = list(getattr(self, "_trace", None) or [])
        steers = len(task_frame.users_steers(getattr(self, "_steer_texts", None) or []))
        keep: list[dict[str, Any]] = []
        for h in held:
            steps = [s for s in trace if s.get("kind") == "action" and int(s.get("call") or 0) >= h["call"]]
            why = ("the request did not end with its answer" if outcome != "final" else
                   "no action ran after it" if not steps else
                   "a step after it failed or did not have its effect"
                   if any(not s.get("ok") or s.get("verdict") is False for s in steps) else
                   "they added words after it" if steers > h["steers"] else "")
            self.logger.log("held_how", kept=not why, why=why or "it worked", app=h["input"].get("app"))
            if not why:
                keep.append(h["input"])
        if not keep or not getattr(self.memory, "installed", False):
            return
        memory, request, logger = self.memory, self._request_text, self.logger

        def save() -> None:
            for tool_input in keep:
                try:
                    out = memory.run_tool("remember_how", tool_input, request=request)
                    logger.log("held_how_saved", ok=bool(out.ok), summary=str(out.summary)[:120])
                except Exception as exc:  # noqa: BLE001 - memory never breaks a request
                    logger.log("held_how_saved", ok=False, error=f"{type(exc).__name__}: {exc}"[:200])

        threading.Thread(target=save, name="yuki-held-how", daemon=True).start()

    def _keep_slider_way(self, *, outcome: str, at: float) -> None:
        """N2 (report 47, review): after one success, the way a slider of that app was moved (which control of the read
        holds it, set by its value or its own keys and read back) as a practice procedure for that app, so the next
        request goes straight to it. Practice memory's distiller learns only runs of three actions or more; this is
        one. Learned from the run's verified step, never hard-coded per app."""
        if outcome != "final" or not getattr(self.memory, "installed", False):
            return
        objects: dict[str, dict[str, Any]] = {}
        for s in getattr(self, "_trace", None) or []:
            if (s.get("tool") == "set_slider" and s.get("kind") == "action" and s.get("ok") is True
                    and s.get("verdict") is True and s.get("app")):
                control = str(s.get("target") or "").split(", window ", 1)[0].strip()
                if not control or control.startswith("window "):
                    continue
                keys = (s.get("input") or {}).get("keys") if isinstance(s.get("input"), dict) else None
                # vfix3: a shortcut is kept only when this very step verified it moved the level (its report:
                # "pressed the app's own shortcut ..."); a shortcut that did nothing here is not a way
                shortcut = ("+".join(str(k) for k in keys)[:30]
                            if isinstance(keys, list) and keys and "pressed the app's own shortcut" in str(
                                s.get("result") or "") else "")
                how = (f"with keys {shortcut} (its own shortcut, verified: the level moved)" if shortcut else
                       "with to (percent) or by (points); it reads the value back")
                objects[str(s["app"])] = {"kind": "procedure", "body": {"steps": [   # each step at most 160 chars
                    "Read the window",
                    f"set_slider on {control[:60]} {how}",
                    "Never drag the slider or script the mouse; the mixer is another control"]}}
        if objects:
            self._write_practice(objects, origin="slider", outcome=outcome, at=at)

    #: How long the background write of a run's pitfalls may take (s; off the request's path).
    PITFALL_WRITE_S = 8.0

    def _keep_pitfalls(self, *, outcome: str, at: float, lessons: dict[str, list[str]] | None = None) -> None:
        """N8 (report 47, review): a failed or stopped run's pitfalls and the way that worked after them
        (:func:`task_frame.session_pitfalls`) into practice memory, so every lane and the next session read them for that
        app (practice memory shows a pitfall of an app in play to every request). Through the existing practice store
        (``practice.add_playbook``, a ``pitfall`` with one ``observed`` event of this run: a tentative object the ledger
        weighs like any other; a near-identical earlier one of Yuki's own is superseded, not piled up). No migration.
        Practice memory's own post-mortem may refine it when its budget allows. On a daemon thread; never raises."""
        trace = getattr(self, "_trace", None) or []
        if not getattr(self.memory, "installed", False) or not callable(getattr(self.memory, "call", None)):
            return
        pits = task_frame.session_pitfalls(trace, outcome=outcome,
                                           steers=list(getattr(self, "_steer_texts", None) or []))
        if not pits:
            return
        objects = {app: {"kind": "pitfall", "body": {"text": pit["text"], "instead": pit["instead"] or None}}
                   for app, pit in pits.items()}

        def kept(_ids: list[int]) -> None:
            for app in pits:   # practice memory shows it now: the session copy would only repeat it
                if lessons is not None:
                    lessons.pop(app, None)

        self._write_practice(objects, origin="session", outcome=outcome, at=at, on_done=kept)

    def _write_practice(self, objects: dict[str, dict[str, Any]], *, origin: str, outcome: str, at: float,
                        on_done: Any = None) -> None:
        """Write practice objects learned by code from this run (``{app: {"kind", "body"}}``; the intent is the
        request's words through memory's secret filter) with one ``observed`` event of this run, through the existing
        practice store (``practice.add_playbook``; no migration). A near-identical live object of the same kind, app and
        ``origin`` is superseded, not piled up. On a daemon thread, off the request's path; never raises."""
        intent = " ".join(str(self._this_query or "").split())[:120] or "a task"
        key = f"agent:{getattr(self.memory, 'session_id', None) or ''}:{self.lane}-{self._requests}"
        logger = self.logger
        memory = self.memory

        def write(client: Any) -> list[int]:
            from yuki.memory import practice as practice_store

            ids = []
            what = practice_store._redact(intent) or "a task"   # memory's secret filter, as for every trace
            embed = getattr(client, "_embed", None)
            vec = embed(practice_store.vector_text(client.store, what)) if callable(embed) else None
            for app, obj in objects.items():
                supersedes = None
                if vec is not None:
                    near = practice_store.near(client.store, vec, kinds=[obj["kind"]], limit=4)
                    same = [o for o in near if o.origin == origin and practice_store.app_key(app) in o.apps
                            and (o.score or 0.0) >= practice_store.SUPERSEDE_COSINE]
                    supersedes = same[0].id if same else None
                ids.append(practice_store.add_playbook(
                    client.store, {"kind": obj["kind"], "intent": what, "apps": [app], "origin": origin,
                                   "body": obj["body"], "sources": [key]},
                    events=[{"type": "observed", "run_key": key, "refs": {"outcome": outcome}, "at": at}],
                    vector=vec, embed_model=getattr(getattr(client, "_embedder", None), "model_name", None),
                    supersedes=supersedes))
            return ids

        def run() -> None:
            try:
                ids = memory.call(f"practice_{origin}", write, timeout_s=self.PITFALL_WRITE_S)
                logger.log("practice_written", origin=origin, ok=True, apps=sorted(objects), ids=list(ids or []))
                if on_done is not None:
                    on_done(list(ids or []))
            except Exception as exc:  # noqa: BLE001 - memory never breaks a request
                logger.log("practice_written", origin=origin, ok=False, error=f"{type(exc).__name__}: {exc}"[:200])

        threading.Thread(target=run, name=f"yuki-practice-{origin}", daemon=True).start()

    #: N9: how long the people-lately block is reused (s), and how long a request waits for it the first time (s; a
    #: slower answer is kept for the next request when it lands).
    LATELY_TTL_S = 300.0
    LATELY_WAIT_S = 0.6

    def _people_lately(self) -> list[str]:
        """N9 (report 47, review): who the user deals with lately, for resolving a misheard name in a spoken request
        (:func:`task_frame.people_lately`: closeness, recency, the usual chat, recent calls), from memory's
        ``people_list`` and ``calls_in``. Reused for :data:`LATELY_TTL_S`; waits at most :data:`LATELY_WAIT_S` (a late
        answer is kept for the next request). Never raises (``[]``)."""
        cached = getattr(self, "_lately_cache", None)
        if cached is not None and time.monotonic() - cached[0] <= self.LATELY_TTL_S:
            return list(cached[1])
        call = getattr(self.memory, "call", None)
        if not getattr(self.memory, "installed", False) or not callable(call):
            return []

        def read(client: Any) -> list[str]:
            listing = getattr(client, "people_list", None)
            rows = listing(limit=task_frame.MAX_LATELY) if callable(listing) else []
            calls_in = getattr(client, "calls_in", None)
            calls = calls_in(since=time.time() - task_frame.LATELY_CALL_HOURS * 3600) if callable(calls_in) else []
            lines = task_frame.people_lately(rows, calls)
            self._lately_cache = (time.monotonic(), lines)   # also when this request stopped waiting
            return lines

        try:
            return list(call("people_lately", read, timeout_s=self.LATELY_WAIT_S) or [])
        except Exception as exc:  # noqa: BLE001 - memory never breaks a request
            self.logger.log("people_lately", ok=False, error=f"{type(exc).__name__}: {exc}"[:160])
            return []

    def _lessons_for(self, overview: Any) -> list[str]:
        """N8: the session lessons of the apps in play (an open window's app, or the app the user named), newest
        first."""
        now = time.time()
        keys = {_app_key(w.get("process_name")) for w in ((overview or {}).get("windows") or [])
                if isinstance(w, dict)} if isinstance(overview, dict) else set()
        named = _app_key(getattr(self, "_request_app_named", None))
        if named:
            keys.add(named)
        keys.discard("")
        out: list[str] = []
        for at, lessons in reversed(list(getattr(self, "_session_lessons", ()) or ())):
            if now - at > self.LESSON_TTL_S:
                continue
            for app, lines in list(lessons.items()):   # a background write may drop an app it kept in memory
                key = _app_key(app)
                if key and any(key == k or (len(k) >= 4 and (k in key or key in k)) for k in keys):
                    out += [f"[{app}] {line}" for line in lines]
        return out[:task_frame.MAX_LESSONS]

    def _note_action(self, name: str, ok: bool, summary: str) -> None:
        """Add one line to this request's actions (not for looks, done or the note)."""
        if name in _NOT_ACTIONS:
            return
        summary = " ".join(str(summary or "").split())
        line = f"{name}: {summary}" if ok else f"{name} failed: {summary}"
        if len(line) > _ACTION_LINE_CHARS:
            line = line[: _ACTION_LINE_CHARS - 1] + "…"
        self._actions.append(line)

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
            if self._paused.is_set() and not self._cancelled.is_set():
                # appTakeover: the user took over after the last results went in (while the screen was being read):
                # wait for them here, then the screen is read again before the next call
                since = self._paused_s
                if (yield from self._hold_paused()):
                    overview, _facts = self._capture_overview()
                    view = self._resume_view()
                    self.context.add_note("\n\n".join(
                        [self._resume_note(self._paused_s - since), overview] + ([view[1]] if view else [])))
            if self._cancelled.is_set():
                yield self._cancel_event()
                return
            if step > 1:
                self.logger.begin_turn()

            self.context.prune()
            # Thinking and the tools that can run early are yielded while the
            # response streams; what comes back is the complete final message.
            turn = _Turn()
            try:
                response = yield from self._request(turn)
            except _StreamCancelled:
                # Cancelled (or withdrawn) while the response streamed and before any of its tools ran:
                # the response was dropped unread, so nothing of it goes into the transcript.
                yield self._cancel_event()
                return
            self.context.add_assistant(response.content)

            stop_reason = getattr(response, "stop_reason", None)
            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

            # Cancelled while the model was answering: a reply with no tool call
            # would otherwise be delivered as the final word after a stop.
            # (Tool calls are refused one by one in _run_one and the loop ends
            # at the next step's check.)
            if self._cancelled.is_set() and not tool_uses:
                yield self._cancel_event()
                return

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                message = "The model declined this request"
                category = getattr(details, "category", None)
                if category:
                    message += f" ({category})"
                if turn.handled:
                    # Some tools already ran while the response streamed: close
                    # the turn with what they did, so the transcript matches the
                    # machine, and run nothing more.
                    blocks, names = yield from self._run_tools(
                        tool_uses, turn, skip_text="Not run: the model stopped this response."
                    )
                    overview, facts = self._capture_overview()
                    self.context.add_tool_results(
                        blocks, overview, tool_names=names, self_facts=facts
                    )
                self.logger.error(message)
                yield ErrorEvent(message)
                return

            if stop_reason == "max_tokens" and not tool_uses:
                message = "The model hit the output limit before finishing."
                self.logger.error(message)
                yield ErrorEvent(message)
                return

            if tool_uses:
                if stop_reason == "max_tokens":
                    self._skip_truncated(response, turn)
                blocks, names = yield from self._run_tools(tool_uses, turn)
                if self._denial_stop:
                    # Revamp D (S1): too many denials in a row or in all: the request stops, honestly.
                    overview, facts = self._capture_overview()
                    self.context.add_tool_results(blocks, overview, tool_names=names, self_facts=facts)
                    self.logger.log("judge_stop", why=self._denial_stop)
                    self.logger.error(DENIALS_MESSAGE)
                    yield ErrorEvent(DENIALS_MESSAGE)
                    return
                breaker_blocks, stop_now = self._breaker_step(turn)
                if stop_now:
                    # The hard stop was already given and the brain still reached for an action.
                    overview, facts = self._capture_overview()
                    self.context.add_tool_results(blocks, overview, tool_names=names, self_facts=facts)
                    message = ("I stopped: the same thing kept failing and I could not find another way. "
                               "Tell me how you want to go on.")
                    self.logger.log("loop_breaker", op="ended_request", trips=len(self._breaker.trips))
                    self.logger.error(message)
                    yield ErrorEvent(message)
                    return
                held = self._hold_unverified(turn)
                if not turn.finished and turn.finishing and not turn.failed and not turn.stop_after and not held:
                    # The brain marked an action as finishing the request, every call of the response
                    # succeeded and that action's own report verified its effect: that is its done, with no
                    # extra round trip.
                    turn.finished.append(turn.finishing[-1][0])
                    self.logger.log("finished_by_action", message=turn.finishing[-1][0])
                reason = self._step_signal(turn, breaker_blocks)
                if reason is None and held and not (turn.finishing and finish_gate.is_expected(turn.finishing[-1][2])):
                    reason = held[0][1]   # a finish held for an expected-unverifiable report is no trouble signal
                if getattr(self.settings, "consult", True):
                    # Revamp D (S3): the actor's profile stays fixed for the whole request; a contradicted action or a
                    # breaker trip gets one second look instead, its advice a labelled note in the next message.
                    signal = (("contradicted", f"{turn.contradicted[0][0]}: {turn.contradicted[0][1]}")
                              if turn.contradicted else ("breaker", "the loop breaker tripped") if breaker_blocks
                              else None)
                    if signal is not None and not turn.finished:
                        advice = self._consult(*signal)
                        if advice is not None and advice.advice and not advice.error:
                            held = held + [({"type": "text", "text": advice.note()}, f"second look: {signal[0]}")]
                elif reason and self._escalate(reason) and not held and not breaker_blocks:
                    # The fast first pass did not show its effect: the next call thinks again from the screen, not
                    # from its own unchecked words before the call (v3.14; measured on the Warp stub: without this,
                    # the escalated call repeated the first pass's "it has no full screen mode" 2 of 4 times).
                    held = [({"type": "text", "text": (
                        f"{CHECK_LABEL}: {reason}. Think again from what the window shows now, not from what you "
                        f"wrote before that call (it was not checked), and try the usual way for what they asked "
                        f"before telling them it cannot be done.")}, reason)]
                breaker_blocks = breaker_blocks + [note for note, _why in held]
                resumed_after: float | None = None
                if not turn.finished and self._paused.is_set() and not self._cancelled.is_set():
                    # appTakeover: the user took over during this turn: wait for them; on resume the screen is read
                    # again (the overview below, and the window in front in full) before the next call
                    since = self._paused_s
                    if (yield from self._hold_paused()):
                        resumed_after = self._paused_s - since
                        breaker_blocks = breaker_blocks + [{"type": "text", "text": self._resume_note(resumed_after)}]
                steers = self._steer_blocks()
                if turn.finished and steers:
                    # The user added something before the closing message went
                    # out: it is not sent, and the model takes one more step.
                    steers.insert(0, {"type": "text", "text": (
                        "Your done message was not sent: the user added this before it went "
                        "out. Take it into account, then finish with a new done."
                    )})
                    self.logger.log("steer", held_final=turn.finished[0])
                    turn.finished.clear()
                if resumed_after is not None:
                    view = self._resume_view()
                else:
                    view = None if turn.finished else self._auto_view(turn)
                overview, facts = self._capture_overview()
                self.context.add_tool_results(
                    blocks + breaker_blocks + steers, overview, tool_names=names, self_facts=facts,
                    window_view=view,
                )
                if turn.finished:
                    final = self._with_list_left(turn.finished[0])
                    self.logger.final(final)
                    yield Final(final)
                    return
                continue

            if stop_reason == "pause_turn":
                continue

            # end_turn (or anything else) with no tool call: the model answered in
            # plain text without calling done. Treat that text as the final word.
            text = "\n".join(
                getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
            ).strip()
            steers = self._steer_blocks()
            if steers:
                # Same as a held done: the user added something while this reply
                # was being written, so it is not the last word yet.
                self.logger.log("steer", held_final=text)
                self.context.add_note(
                    "Your reply above was not sent: the user added this before it went out. "
                    "Take it into account, then finish with done.\n"
                    + "\n".join(block["text"] for block in steers)
                )
                continue
            named = written_tool_call(text, self.tool_names or ALL_TOOL_NAMES) if text else None
            if named and self._written_calls < 2:
                # A tool call written out as text: nothing ran, and it is no answer. Said so, and one more step
                # (with thinking on, if the request can escalate: the glitch comes with thinking off).
                self._escalate("a tool call came back written as text")
                self._written_calls += 1
                self.logger.log("written_tool_call", tool=named, stop_reason=stop_reason, n=self._written_calls)
                self.context.add_note(
                    f"Your last reply wrote a {named} call out as text, so nothing ran and nothing was said to the "
                    f"user. Make it as a real tool call now (or finish with done if nothing is left to do)."
                )
                continue
            if not text and 0 < self._written_calls < 3:
                # An empty reply right after it was told its call was written as text: asked once more.
                self._written_calls += 1
                self.logger.log("written_tool_call", tool=None, stop_reason=stop_reason, n=self._written_calls)
                self.context.add_note("Your reply was empty and nothing ran. Make the tool call now as a real tool "
                                      "call, or finish with done.")
                continue
            left = self._tasks_left()
            if text and left and self._tasks_held < 2:
                # N10 (review): a reply in plain text ends the request like done, so the kept list holds it the same way
                self._tasks_held += 1
                self.logger.log("tasks_held", left=left, message=text)
                self.context.add_note(
                    f"{CHECK_LABEL}: your reply above was not sent and the request did not end: your list still has, "
                    f"in order: {'; '.join(left)}. Do it now. Only when their own words dropped an item, mark it "
                    f"dropped in tasks with those words and say so in done; when all is done, mark it done in tasks "
                    f"alongside done.")
                continue
            if text:
                text = self._with_list_left(text)
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

    #: How many times a request's ``done`` is sent back for a claim with no evidence (then it is accepted: the
    #: check asks once, it never loops).
    CLAIM_ROUNDS = 1

    def _hold_unverified(self, turn: _Turn) -> list[tuple[dict[str, Any], str]]:
        """Hold an outcome this response announced that its own calls do not back (v3.14). Never reads the words.

        * A ``finishes_with`` whose action's report does not verify the effect is not the outcome: the brain gets
          the report and the window after it, and finishes after checking.
        * A ``done`` sent with an action whose report does not verify its effect, or naming (``about``) a window
          this request has neither read nor acted on with a verified result, is sent back once: look, then finish.

        Returns ``(note block, why)`` pairs for the next message (empty: nothing held); ``turn.finished`` is cleared
        when a ``done`` was held.
        """
        held: list[tuple[dict[str, Any], str]] = []
        left = self._tasks_left()
        ending = bool(turn.finished) or (bool(turn.finishing) and not turn.failed and not turn.stop_after)
        if ending and not turn.failed and left and self._tasks_held < 2:
            # N10 (report 47): the request does not end while its kept list still has items to do (live 3 Oct the
            # last part of a list, a message to the user's friend, was forgotten after an interjection)
            self._tasks_held += 1
            self.logger.log("tasks_held", left=left, message=turn.finished[0] if turn.finished else None)
            turn.finished.clear()
            turn.finishing.clear()
            held.append(({"type": "text", "text": (
                f"{CHECK_LABEL}: the request did not end: your list still has, in order: {'; '.join(left)}. Do it "
                f"now. Only when their own words dropped an item, mark it dropped in tasks with those words and say "
                f"so in done; when all is done, mark it done in tasks alongside done.")},
                "the kept list still had items to do"))
            return held
        if not turn.finished and turn.finishing and not turn.failed and not turn.stop_after:
            _message, verified, why, tool = turn.finishing[-1]
            if verified is not True:
                self.logger.log("finish_held", tool=tool, verified=verified, why=why)
                held.append(({"type": "text", "text": (
                    f"{CHECK_LABEL}: your finishes_with on {tool} was not said: "
                    f"{why}, so nothing shows yet that it did what they asked. Check what the window shows now "
                    f"(attached below when there is one) and finish with done, or act again.")},
                    f"the finishing {tool} was not verified ({why})"))
            return held
        if not turn.finished or turn.failed or self._claim_rounds >= self.CLAIM_ROUNDS:
            if turn.finished and self._claim_rounds >= self.CLAIM_ROUNDS and self._claim_gaps(turn):
                self.logger.log("claim_accepted", why="asked once already this request", about=turn.done_about)
            return held
        gaps = self._claim_gaps(turn)
        if not gaps:
            return held
        self._claim_rounds += 1
        self.logger.log("claim_held", gaps=gaps, about=turn.done_about, message=turn.finished[0])
        turn.finished.clear()
        held.append(({"type": "text", "text": (
            f"{CHECK_LABEL}: your done message was not sent: {gaps[0]}. Look first "
            f"(look_at_window, read_tabs or read_page), then finish with done saying what it shows.")},
            "a claim about the screen had no evidence"))
        return held

    def _claim_gaps(self, turn: _Turn) -> list[str]:
        """What this response's ``done`` asserts without evidence from this request's calls, in words for the brain."""
        gaps = [f"{tool}'s report does not show that it took effect ({why})"
                for tool, verified, why in turn.verdicts if verified is not True and not finish_gate.is_expected(why)]
        gaps += [f"it says how window {hwnd} is now, but this request has neither read it nor acted on it with a "
                 f"verified result" for hwnd in (turn.done_about or []) if hwnd not in self._evidence]
        gaps += self._state_gaps(turn.done_states)
        return gaps

    def _state_gaps(self, states: list[dict[str, Any]]) -> list[str]:
        """N3 (report 47): the states a done tells (``states``) that no step of this request showed: the step it names
        did not run in this request, failed, or (an action) did not verify its effect; a window it names was neither
        read nor acted on with a verified result. Facts of the run trace only, never the words."""
        out: list[str] = []
        trace = getattr(self, "_trace", None) or []
        for item in states or []:
            says = " ".join(str(item.get("says") or "").split())[:120]
            tool = " ".join(str(item.get("seen_in") or "").split())
            hwnd = _as_hwnd(item.get("hwnd"))
            steps = [s for s in trace if s.get("tool") == tool and s.get("ok")
                     and (s.get("kind") != "action" or s.get("verdict") is True or s.get("expected"))]
            seen_window = hwnd is not None and hwnd in self._evidence   # read (the view after actions too) or verified
            if not steps and not seen_window:
                out.append(f"it says \"{says}\", but no {tool or 'step'} in this request showed that: you have not "
                           f"read that")
            elif hwnd is not None and hwnd not in self._evidence and tool in ("look_at_window", "read_page",
                                                                              "read_tabs"):
                out.append(f"it says \"{says}\" of window {hwnd}, which this request has not read: you have not read "
                           f"that")
        return out

    def _step_signal(self, turn: _Turn, breaker_blocks: list[dict[str, Any]]) -> str | None:
        """The measured reason this request's next model call needs judgement, or ``None`` (v3.14).

        An action (or any real call) failed, an action's own report does not verify its effect (an
        expected-unverifiable one, :data:`EXPECTED_UNVERIFIABLE`, is not that), the loop breaker tripped, or the user
        was just asked something. Signals only: which calls ran, their results and verdicts.
        """
        for tool_id, block in turn.results.items():
            name = turn.names.get(tool_id, "")
            if block.get("is_error") and tool_id not in turn.gated and name not in LOOP_EXEMPT:
                return f"{name} failed"
        for tool, verified, why in turn.verdicts:
            if verified is not True and not finish_gate.is_expected(why):
                return f"{tool} was not verified ({why})"
        if breaker_blocks:
            return "the loop breaker tripped"
        if turn.asked:
            return "the user was asked"
        return None

    def _escalate(self, reason: str) -> bool:
        """Move this request's next model calls to its escalated effort and thinking (once; logged).

        Returns whether it escalated now (``False``: nothing to escalate to, or already escalated). Revamp D (S3): with
        ``Settings.consult`` on (the default) the profile never switches (a switch rewrote the whole cached prompt);
        the measured signals get a second look instead (:meth:`_consult`)."""
        if getattr(self.settings, "consult", True):
            if self._escalate_to is not None and self._escalation is None:
                self.logger.log("effort_kept", reason=reason, after_calls=self._model_calls)
            return False
        if self._escalate_to is None or self._escalation is not None:
            return False
        effort, thinking = self._escalate_to
        self._escalation = {"reason": reason, "after_calls": self._model_calls, "effort": effort,
                            "thinking": thinking}
        self.logger.log("effort_escalated", **self._escalation)
        return True

    def _step_config(self) -> tuple[str | None, bool, bool]:
        """``(effort or None, thinking on, send no effort)`` for the next model call of this request."""
        effort = self._request_effort or self.settings.effort
        omit = self._request_omit_effort
        thinking = self._request_thinking is not False
        if self._escalation is not None and self._escalate_to is not None:
            to_effort, to_thinking = self._escalate_to
            if to_effort:
                effort, omit = resolve_effort(to_effort), False
            if to_thinking is not None:
                thinking = bool(to_thinking)
        return effort, thinking, omit

    def _breaker_step(self, turn: _Turn) -> tuple[list[dict[str, Any]], bool]:
        """Count this step for the loop breaker; ``(note blocks for the next message, end the request now)``.

        Calls held for the user's confirmation are not counted. A trip adds the breaker's note
        (framed as Yuki's, not the user's); once the hard-stop note has been sent, a step that
        still reached for an action ends the request.
        """
        ids = [i for i, n in turn.names.items() if n not in LOOP_EXEMPT and i not in turn.gated]
        failures = sum(1 for i in ids if (turn.results.get(i) or {}).get("is_error"))
        if self._breaker_stop_sent and ids:
            return [], True
        trip = self._breaker.end_step(len(ids), failures)
        if trip is not None:
            self.logger.log("loop_breaker", op="trip", reason=trip.reason, tool=trip.tool, summary=trip.summary,
                            count=trip.count, trips=len(self._breaker.trips))
        note = self._breaker.take_note()
        if not note:
            return [], False
        if self._breaker.exhausted:
            self._breaker_stop_sent = True
        self.logger.log("loop_breaker", op="note", text=note, exhausted=self._breaker.exhausted)
        return [{"type": "text", "text": f"{BREAKER_LABEL}: {note}"}], False

    def _risk_check(self, name: str, tool_input: dict[str, Any]) -> tuple[str, str | None]:
        """``("run", None)``, ``("confirm", question)`` or ``("refuse", why)`` for one action.

        The brain declares the risk (``risk``: reversible, user_asked, effect); the code only
        enforces the declaration: irreversible and not asked for -> the user confirms first, once
        per exact call per request. run_powershell must carry a declaration.
        """
        if name not in ACTION_TOOL_NAMES:
            return "run", None
        risk = tool_input.get(RISK_FIELD)
        if risk is None:
            if name == "run_powershell":
                return "refuse", ("Not run: run_powershell needs its risk declared (reversible, user_asked; "
                                  "effect when not reversible). Call it again with that field.")
            return "run", None
        if not isinstance(risk, dict) or not isinstance(risk.get("reversible"), bool):
            return "refuse", ("Not run: risk must be an object with reversible (true/false) and user_asked (effect "
                              "only when not reversible).")
        if risk["reversible"] or risk.get("user_asked") is True:
            return "run", None
        if (name, call_signature(acting_input(tool_input))) in self._confirmed:
            return "run", None
        effect = " ".join(str(risk.get("effect") or "").split()) or f"this {name} call"
        return "confirm", f"This can't be undone: {effect}. Should I go ahead?"

    def _run_helpers(self, tool_input: dict[str, Any]) -> Any:
        """Run the read-only helper team (``spawn_helpers``), yielding a :class:`HelperStep` per step.

        Returns (``yield from``) the tool's outcome: the helpers' reports. Their model calls are
        priced into this request (``requests.csv``) and each helper's whole run is logged as
        ``helper_run``. Cancelling the request stops the helpers at their next step.
        """
        started = time.perf_counter()
        tasks = [" ".join(str(t).split()) for t in (tool_input.get("tasks") or []) if str(t).strip()]
        if not tasks:
            return ToolOutcome(name="spawn_helpers", ok=False, summary="no tasks given",
                               content=[{"type": "text", "text": "tasks must list 1-4 instructions."}])
        team = HelperTeam(client=self.client, backend=getattr(self.dispatcher, "backend", None),
                          memory=self.memory, settings=self.settings, nonce=self._nonce)
        team.start(tasks)
        limit = float(getattr(self.settings, "helpers_timeout_s", 45.0)) + 30.0
        end = time.monotonic() + limit
        while True:
            if self._cancelled.is_set():
                team.cancelled.set()
            try:
                step = team.events.get(timeout=0.1)
            except queue.Empty:
                if not team.alive() or time.monotonic() >= end:
                    break
                continue
            yield step
        if team.alive():
            team.cancelled.set()
            for r in team.results:
                if r.stopped == "running":
                    r.stopped, r.error = "time", "did not finish in time"
        results = list(team.results)
        for r in results:
            for model, usage in r.usage:
                self._account_response(model, usage)
            self.logger.log("helper_run", index=r.index, task=r.task, ok=r.ok, stopped=r.stopped, error=r.error,
                            calls=r.calls, tools=r.tools, input_tokens=r.input_tokens, elapsed_s=r.elapsed_s,
                            usage=[{"model": m, "usage": _as_plain(u)} for m, u in r.usage],
                            transcript=r.transcript, report=r.report)
        done = sum(1 for r in results if r.ok)
        elapsed = time.perf_counter() - started
        return ToolOutcome(
            name="spawn_helpers", ok=done > 0,
            summary=f"{done} of {len(results)} helpers reported in {elapsed:.1f} s",
            content=[{"type": "text", "text": report_text(results)}],
            payload={"helpers": [{"index": r.index, "task": r.task, "ok": r.ok, "stopped": r.stopped,
                                  "calls": r.calls, "tools": r.tools, "elapsed_s": r.elapsed_s} for r in results]},
            elapsed_ms=elapsed * 1000,
        )

    def _cancel_event(self) -> ErrorEvent:
        """Record a cancellation (or a withdrawal) in the transcript and build the event for it."""
        if self.cancel_reason == PARKED:
            message = "Parked: the question stays open until the user answers it."
            self.logger.log("parked", message=message)
            self.context.add_note("That request is paused on your question to the user, who has not answered yet; "
                                  "it goes on only when they answer. Do not bring it up otherwise.")
            return ErrorEvent(message)
        if self.cancel_reason == WITHDRAWN:
            message = "Withdrawn: replaced by the user's newer request."
            self.logger.log("withdrawn", message=message)
            self.context.add_note("The user moved on before that request finished: it was withdrawn in favour of "
                                  "their newer request. Do not continue it or bring it up.")
            return ErrorEvent(message)
        if self._stopped_paused or self._paused.is_set():
            # appTakeover: stopped while paused because the user took over (or nobody resumed it in time): plainly
            message = PAUSE_STOP_LINE
            self.logger.error(message)
            self.context.add_note("The user took over and that request was stopped while paused. Do not continue it "
                                  "or bring it up unless they ask.")
            return ErrorEvent(message)
        message = "Cancelled by the user."
        stopped = getattr(self, "_send_stopped", None)
        if stopped is not None:
            # appPT: stopped in the send countdown: what did not happen, plainly (the screen's end line and the words
            # the voice gets for a stop it owns)
            message += (" The message was not sent: it is still in the box." if stopped
                        else " The message was not sent.")
        self.logger.error(message)
        self.context.add_note("The user cancelled that request. Stop and await new instructions.")
        return ErrorEvent(message)

    # -- tools -------------------------------------------------------------

    def _run_tools(
        self, tool_uses: list[Any], turn: _Turn, *, skip_text: str | None = None
    ) -> Any:
        """Execute every ``tool_use`` block not yet handled, stopping at the first failure.

        A response may carry ``done`` alongside real work -- "press mute, and with
        that I am finished" -- which is how a one-step request costs one round trip
        instead of two. ``done`` is therefore always executed last, whatever
        position the model put it in, and only if everything before it succeeded:
        a failure stops dispatch, so the ``done`` is dropped with an explanation
        and the model gets the results back to react to. It never gets to announce
        an outcome that did not happen.

        Blocks already run while the response streamed (see :meth:`_request`)
        keep their results; this runs the rest in the same order and under the
        same stop-on-first-failure rule.

        Args:
            tool_uses: The blocks from one assistant response, in model order.
            turn: This response's tool state; ``turn.finished`` gets the ``done``
                message if the model ended its turn.
            skip_text: Run nothing more: answer every unhandled call with this.

        Yields:
            Events for each call and result, and :class:`AskUser` when paused.

        Returns:
            ``(tool_result_blocks, tool_use_id -> name)``. The blocks are in the
            model's original order, because that is the order the API pairs them
            with the calls. Every block carries a result even when execution
            stopped early: the API requires one ``tool_result`` per ``tool_use``,
            so skipped tools get an error block explaining they never ran.
        """
        order = [str(getattr(b, "id", "")) for b in tool_uses]
        work = [b for b in tool_uses if str(getattr(b, "name", "")) not in ("done", "remember_how")]
        # vfix3 review F6: know-how written in the same response as the action it describes runs after that action
        # (and is held until the request ends well); done stays last
        learning = [b for b in tool_uses if str(getattr(b, "name", "")) == "remember_how"]
        closing = [b for b in tool_uses if str(getattr(b, "name", "")) == "done"]
        turn.acting_reply = any(str(getattr(b, "name", "")) in ACTION_TOOL_NAMES for b in tool_uses)

        for block in work + learning + closing:
            tool_id = str(getattr(block, "id", ""))
            if tool_id in turn.handled:
                continue
            if skip_text is not None:
                name = str(getattr(block, "name", ""))
                turn.names[tool_id] = name
                turn.record(tool_id, self._skip(tool_id, name, skip_text))
                continue
            yield from self._run_one(turn, block)

        return [turn.results[tool_id] for tool_id in order], dict(turn.names)

    def _run_one(self, turn: _Turn, block: Any, *, early: bool = False) -> Iterator[AgentEvent]:
        """Run one ``tool_use`` block and record its result in ``turn``.

        Args:
            turn: This response's tool state.
            block: The ``tool_use`` block.
            early: The response is still streaming (counted for ``first_tool_ms``).

        Yields:
            ``tool_call`` / ``tool_result`` events, and :class:`AskUser` when paused.
        """
        tool_id = str(getattr(block, "id", ""))
        name = str(getattr(block, "name", ""))
        tool_input = dict(getattr(block, "input", {}) or {})
        turn.names[tool_id] = name

        # done is honoured only when everything before it succeeded -- which
        # includes a call skipped for being cut off at the output limit.
        if turn.stop_after and self._paused.is_set():
            # appTakeover: the rest of a response the pause caught is not run either, and is no failure of its own
            turn.gated.add(tool_id)
            turn.record(tool_id, self._skip(tool_id, name, PAUSED_SKIP))
            return
        if turn.stop_after or (name == "done" and turn.failed):
            turn.record(
                tool_id, self._skip(tool_id, name, "Not run: an earlier tool in this turn failed.")
            )
            return

        if self._cancelled.is_set():
            turn.record(tool_id, self._skip(tool_id, name, "Not run: the user cancelled."))
            turn.stop_after = True
            return

        if self._paused.is_set() and (name in ACTION_TOOL_NAMES or name == "say"):
            # appTakeover (review F1, F2): the user took over before this call began: nothing of it runs or shows: no
            # step on the island, no "can't be undone" question, no judge or second look, no send countdown and no
            # spoken line (Yuki says nothing while paused). The rest of the response is skipped the same way.
            turn.gated.add(tool_id)
            turn.record(tool_id, self._skip(tool_id, name, PAUSED_SKIP))
            if name in ACTION_TOOL_NAMES:
                self._note_action(name, False, "not run: the user took over (paused)")
                self._trace_step(name, tool_input, ok=False, kind="control", why="paused: the user took over",
                                 result=PAUSED_SKIP)
            self.logger.log("takeover", state="skipped", tool=name, tool_use_id=tool_id)
            turn.stop_after = True
            return

        # (v3.15 held a long job's done once to ask for a learned note: a whole extra model call per long job. Phase C
        # learns from the run trace instead - the distiller reads the steps themselves - so done goes out at once.)
        acts = name in ACTION_TOOL_NAMES
        target = self._call_target(name, tool_input) if acts else {}
        # Revamp D (S4): finishes_with is a boolean; the line the request ends with is written by code from the call and
        # its target (finish_words: the brain's own wording). It travels on the tool_call event in the finishes_with
        # field as a string, the shape voice v3.17 reads to say a verified result by code.
        finishes, own_words = finish_gate.finish_request(tool_input) if acts else (False, None)
        finish_line = (own_words or finish_gate.finish_line(name, tool_input, process=target.get("app"),
                                                             tab_name=target.get("tab_name"))) if finishes else None
        event_input = tool_input if finish_line is None else {**tool_input, FINISHES_FIELD: finish_line}
        about = self._step_about(name, tool_input, target) if acts else {}
        # the step's words travel on its result too (the brain's ``what`` and the target's names)
        step_words = {**({WHAT_FIELD: tool_input[WHAT_FIELD]} if isinstance(tool_input.get(WHAT_FIELD), str) else {}),
                      **({STEP_ABOUT_FIELD: about} if about else {})}
        if about:   # the UI's step words (yuki.ui.step_words): names, never the command, text or address
            event_input = {**event_input, STEP_ABOUT_FIELD: about}
        self.logger.tool_call(name, event_input, tool_use_id=tool_id)
        yield ToolCall(name, event_input)
        now = _now_line(tool_input.get(NOW_FIELD))
        if now:   # appPT: the brain's own note of what it checks or does and why, live on the island (after its step)
            yield Partial("now", now)

        # The generator was suspended on that yield, which is exactly where a
        # cancel from the UI thread tends to land. The tool has not started,
        # so refuse it rather than touch the machine on the way out -- and
        # still emit a result, so a caller pairing calls with results does
        # not wait forever for one.
        if self._cancelled.is_set():
            text = "Not run: the user cancelled."
            turn.record(tool_id, self._skip(tool_id, name, text))
            yield ToolResult(name, False, text, about=step_words)
            turn.stop_after = True
            return
        if acts and self._paused.is_set():
            # appTakeover (review F1): the user took over while this step was being shown: none of the gates below run
            # (no question, no judge or second look, no send countdown, nothing said)
            yield from self._paused_skip(turn, tool_id, name, tool_input, step_words)
            return

        # The loop breaker refuses a call that already failed the same way too often, and every
        # action once it has stopped the request's actions (yuki.agent.loopguard). Calls are compared
        # without the words said as they start (v3.14).
        acting = acting_input(tool_input)
        refused = self._breaker.refuse(name, acting)
        if refused is None:
            decision, detail = self._risk_check(name, tool_input)
            if decision == "refuse":
                refused = detail
        else:
            decision, detail = "refuse", refused
            self.logger.log("loop_breaker", op="refused", tool=name, input=tool_input)
        if refused is not None:
            turn.record(tool_id, self._skip(tool_id, name, refused))
            self._note_action(name, False, refused)
            self._trace_step(name, tool_input, ok=False, why="refused before it ran", result=refused)
            yield ToolResult(name, False, refused, about=step_words)
            turn.stop_after = True
            return
        if decision == "confirm" and self._paused.is_set():
            yield from self._paused_skip(turn, tool_id, name, tool_input, step_words)   # appTakeover: never asked paused
            return
        if decision == "confirm":
            # Declared irreversible and not asked for: the user confirms first (the ask_user card).
            question = str(detail)
            turn.gated.add(tool_id)
            self.logger.log("risk_gate", op="ask", tool=name, input=tool_input, question=question)
            self.logger.ask_user(question)
            self._awaiting_answer = True
            self._pending_answer = None
            asked_at = time.monotonic()
            yield AskUser(question)  # generator pauses here
            self._waited_s += time.monotonic() - asked_at
            self._awaiting_answer = False
            self._pause_quiet_until = time.monotonic() + RESUME_GRACE_S   # appTakeover: the answering hand moves on
            answer, self._pending_answer = self._pending_answer, None
            if answer is None:
                text = "Not run: it can't be undone and the user did not answer whether to go ahead."
            else:
                self.logger.user_answer(answer)
                self._note_words(answer, how="answer", question=question)
                self._confirmed.add((name, call_signature(acting)))
                turn.asked = True
                text = (f"Not run yet: you declared it irreversible and not asked for, so the user was asked "
                        f"'{question}' and answered: '{answer}'. If that is a yes, make this exact call again "
                        f"and it will run; if not, do not run it and go on as they said.")
            self.logger.log("risk_gate", op="answered" if answer is not None else "no_answer", tool=name,
                            answer=answer)
            self.logger.tool_result(name, ok=False, summary=text, result={"held": True, "answer": answer},
                                    elapsed_ms=0.0, tool_use_id=tool_id)
            self._note_action(name, False, f"held for confirmation: {question} -> {answer}")
            self._trace_step(name, tool_input, ok=answer is not None, kind="ask",
                             result=f"held for the user's confirmation: \"{question}\" -> \"{answer}\"")
            yield ToolResult(name, False, text, about=step_words)
            turn.record(tool_id, self._result_block(tool_id, [{"type": "text", "text": text}], is_error=True))
            turn.stop_after = True
            return

        judged = None
        sends = confirmed = False
        if (acts and not self._paused.is_set()) or (not acts and self._outbound_read(name)):
            # (appTakeover: an action the pause caught is not judged: it is skipped below)
            # Revamp D (S1): calls whose metadata can destroy or reach someone, and kinds the user's policy holds back,
            # pass the authorization judge, which sees only the user's own words and this call (and a fetch_url after
            # a read_file: review 34 M2).
            judged = self._authorize(name, tool_input, target)
            held_text = self._denial_text(judged) if judged is not None and not judged.allowed else None
            held_why = "not run: held by Yuki's check"
            if held_text is None:
                # N0 (report 47): a step with a lasting effect (a send, a window closed, one declared irreversible) runs
                # only after a situation check this request has read; otherwise the brain is sent to look first.
                # a send is the judge's mark or, whatever the judge says (or with no judge), the call's own traits:
                # text typed with Enter, a click while typed text waits (review F6)
                sends = bool(judged is not None and judged.sends) or sends_for_certain(call_traits(
                    name, tool_input, role=target.get("role"), typed_pending=bool(target.get("typed_pending"))))
                need = situation_check.consequence(name, tool_input, sends=sends)
                checks = getattr(self, "_situation_checks", None)
                confirmed = (name, call_signature(acting)) in getattr(self, "_confirmed", set())   # they said yes
                if need and checks is not None and not confirmed and not situation_check.covered(
                        checks, target.get("hwnd"), self._model_calls):
                    read_before = [c for c in checks if c.call < self._model_calls]
                    held_text = (situation_check.REFUSAL_OTHER if read_before else situation_check.REFUSAL).format(
                        label=CHECK_LABEL, what=need)
                    held_why = "not run: no situation check before a step that lasts"
                    self.logger.log("situation_gate", tool=name, what=need, checks=len(checks),
                                    hwnd=target.get("hwnd"))
            if held_text is None:
                # Revamp D (S3): a pending send to a person, or the request's first action on a close call, gets one
                # second look before it runs; its verdict (the model's) can hold the call.
                signal = None
                if judged is not None and judged.sends:
                    signal = ("send", "a message to a person is about to be sent")
                elif self._learning.get("actions", 0) == 0 and _close_call(self._frame):
                    signal = ("close_call", "the task frame calls two candidates a close call")
                if signal is not None and not self._paused.is_set() and self._consult_due(signal[0]):
                    advice = self._consult(signal[0], signal[1],
                                           pending=judge_call_text(name, tool_input, target=target))
                    if (advice is not None and advice.blocking and advice.verdict in ("revise", "ask_user")
                            and advice.advice):
                        # held only on the second look's own mark that running it now would be a real mistake
                        held_text = (advice.note() + " This step was not run. If it is right after all, make the "
                                                     "same call again.")
            if held_text is not None:
                turn.gated.add(tool_id)
                self.logger.tool_result(name, ok=False, summary=held_text,
                                        result={"held": True, "judge": judged.facts() if judged else None},
                                        elapsed_ms=judged.ms if judged else 0.0, tool_use_id=tool_id)
                self._note_action(name, False, held_why)
                self._trace_step(name, tool_input, ok=False, kind="control", why=held_why, result=held_text)
                yield ToolResult(name, False, held_text, about=step_words)
                turn.record(tool_id, self._result_block(tool_id, [{"type": "text", "text": held_text}], is_error=True))
                turn.stop_after = True
                return

        if (acts and sends and not confirmed and float(self.send_countdown_s or 0) > 0 and not self._paused.is_set()
                and not self._text_approved(self._send_text(name, tool_input, target)[0])):
            # appPT: a plain send that passed the situation check and the judge shows who / where / the text for a few
            # seconds to stop or swap (the user's 3 Oct "Sending to X ... 3 s to stop"). Never when the user already
            # gave the go-ahead on this text (they wrote it in an answer to Yuki's question, or confirmed this very
            # call), never for a draft (nothing is sent).
            went = yield from self._send_countdown(name, tool_input, target)
            if went != "going":
                if went == "stopped":
                    text = "Not run: the user stopped the send in its countdown; nothing was sent."
                else:
                    text = went   # the swap's note to the brain
                turn.gated.add(tool_id)
                self.logger.tool_result(name, ok=False, summary=text, result={"held": True, "countdown": "stopped"
                                        if went == "stopped" else "swapped"}, elapsed_ms=0.0, tool_use_id=tool_id)
                self._note_action(name, False, "not sent: stopped in the countdown" if went == "stopped"
                                  else "not sent: the user swapped who or where in the countdown")
                self._trace_step(name, tool_input, ok=False, kind="control", why="send countdown", result=text)
                yield ToolResult(name, False, text, about=step_words)
                turn.record(tool_id, self._result_block(tool_id, [{"type": "text", "text": text}], is_error=True))
                turn.stop_after = True
                return

        if acts and self._paused.is_set():
            # appTakeover: the user took over while the gates ran: no new action starts (the request waits for them
            # after this turn)
            yield from self._paused_skip(turn, tool_id, name, tool_input, step_words)
            return
        if acts:
            self._acting_start()
        dispatched = time.perf_counter()
        dispatched_at = time.time()
        if early:
            turn.early += 1
            if turn.first_early_at is None:
                turn.first_early_at = dispatched
        if name == "hand_over":
            # The reader lane's hand-off (yuki.agent.lanes): not in its own subset, so the runtime counts the call as
            # needing hands and queues the request for the worker when this one ends.
            outcome = ToolOutcome(name=name, ok=True, summary="queued for the task lane", content=[{
                "type": "text", "text": "Queued: the task lane takes it the moment you finish. Say in one line that it "
                                        "comes right after what is running, then call done."}],
                payload={"why": str(tool_input.get("why") or "")})
        elif (name == "remember_how" and turn.acting_reply and getattr(self.memory, "installed", False)
              and (self.tool_names is None or name in self.tool_names)):
            outcome = self._hold_how(tool_input)
        elif name == "spawn_helpers" and (self.tool_names is None or name in self.tool_names):
            outcome = yield from self._run_helpers(tool_input)
        elif name == situation_check.TOOL_NAME and (self.tool_names is None or name in self.tool_names):
            # N0: the situation check is gathered here (the window read again, memory's recent history); a read with
            # no side effects, so a cancel stops the wait for it
            outcome = self._abortable(name, lambda: self._situation(tool_input))
            record = outcome.payload.pop("record", None) if isinstance(outcome.payload, dict) else None
            if outcome.ok and isinstance(record, situation_check.CheckRecord):
                # recorded here, on the loop's own thread, only for a check whose result goes back to the model: a read
                # left behind by the user's words or a cancel never covers a later send (review F1)
                record.call = self._model_calls
                self._situation_checks.append(record)
            shown = outcome.payload.get("shown") if isinstance(outcome.payload, dict) else None
            if outcome.ok and isinstance(shown, str) and shown:
                yield Partial("now", shown)   # appPT: what the check looked at, in plain words, on the island
        elif name in PERCEPTION_TOOLS:
            # A read has no side effects: a cancel or the user's correction stops the wait for it (v3.14).
            outcome = self._abortable(name, lambda: self._dispatch(name, tool_input))
        elif acts and tool_traits(name, tool_input).needs == "input":
            # Global input may wait for the user to stop typing (S7): its one line is said as it waits.
            outcome = yield from self._dispatch_watching(name, tool_input)
        else:
            outcome = self._dispatch(name, tool_input)
        self._tool_s += time.perf_counter() - dispatched
        self._tool_counts[name] += 1
        if name == "read_file" or (name == "spawn_helpers" and isinstance(outcome.payload, dict) and any(
                "read_file" in (h.get("tools") or []) for h in outcome.payload.get("helpers") or []
                if isinstance(h, dict))):
            self._files_read = True   # a later fetch_url is judged (review 34 M2)
        # Text written outside Yuki (pages, windows, files, command output, memory) is fenced as
        # untrusted data with this request's code (yuki.agent.untrusted).
        if name in EXTERNAL_TEXT_TOOLS and (outcome.ok or name == "run_powershell"):
            outcome.content = wrap_blocks(list(outcome.content), source=source_for(name, tool_input),
                                          nonce=self._nonce)
        abandoned = isinstance(outcome.payload, dict) and bool(outcome.payload.get("abandoned"))
        if acts and not outcome.ok and self._paused.is_set():
            # appTakeover: the user took over while it waited to send its input (nothing was sent): not a failure of
            # the call for the loop breaker or the escalation signals
            abandoned = True
            turn.gated.add(tool_id)
        if name not in CONTROL_TOOLS and not abandoned:
            trip = self._breaker.observe(name, acting, outcome.ok, outcome.summary,
                                         perception=name in PERCEPTION_TOOLS)
            if trip is not None:
                self.logger.log("loop_breaker", op="trip", reason=trip.reason, tool=trip.tool,
                                summary=trip.summary, count=trip.count, trips=len(self._breaker.trips))
                # Into this exchange's action lines too, so memory's conversation extractor can learn what did not
                # work on this PC (know-how; the memory builder's request, 2026-09-29).
                line = " ".join(f"loop breaker: {trip.reason} ({trip.tool}, {trip.count} times)".split())
                self._actions.append(line if len(line) <= _ACTION_LINE_CHARS else line[:_ACTION_LINE_CHARS - 1] + "…")

        if outcome.kind == "ask_user":
            question = outcome.control_input["question"]
            self.logger.ask_user(question)
            self._awaiting_answer = True
            self._pending_answer = None
            asked_at = time.monotonic()
            yield AskUser(question)  # generator pauses here
            self._waited_s += time.monotonic() - asked_at
            self._awaiting_answer = False
            self._pause_quiet_until = time.monotonic() + RESUME_GRACE_S   # appTakeover: the answering hand moves on
            answer = self._pending_answer
            self._pending_answer = None
            self._trace_step(name, tool_input, ok=answer is not None, kind="ask",
                             result=f"asked \"{question}\" -> " + (f"\"{answer}\"" if answer is not None
                                                                     else "no answer"))
            if answer is None:
                text = "The user did not answer."
                self._note_action("asked the user", False, f"{question} (no answer)")
                self.logger.tool_result(
                    name, ok=False, summary=text, result={"answer": None},
                    elapsed_ms=outcome.elapsed_ms, tool_use_id=tool_id,
                )
                yield ToolResult(name, False, text, about=step_words)
                turn.record(
                    tool_id,
                    self._result_block(tool_id, [{"type": "text", "text": text}], is_error=True),
                )
                turn.stop_after = True
                return
            self.logger.user_answer(answer)
            self._note_words(answer, how="answer", question=question)
            turn.asked = True
            # appPT: kept for the send countdown: a send whose text they wrote here goes without one; an answer about
            # who or where does not count (review F7)
            self._answered.append((str(question or ""), str(answer or "")))
            self._note_action("asked the user", True, f"{question} → {answer}")
            self.logger.tool_result(
                name, ok=True, summary=f"answered: {answer}", result={"answer": answer},
                elapsed_ms=outcome.elapsed_ms, tool_use_id=tool_id,
            )
            yield ToolResult(name, True, f"answered: {answer}")
            turn.record(tool_id, self._result_block(tool_id, [{"type": "text", "text": answer}]))
            return

        if outcome.kind == "note":
            self.context.set_note(outcome.control_input["text"])

        if outcome.kind == "tasks":
            # N10: the kept list, in the run trace and the log; a steer carries what is left, done is held for it
            self._tasks = [dict(i) for i in outcome.control_input.get("items") or []]
            self.logger.log("tasks", items=self._tasks, left=len(self._tasks_left()))
            self._trace_step("tasks", {"items": self._tasks}, ok=True, kind="control", result=outcome.summary)

        if outcome.kind == "done":
            turn.finished.append(outcome.control_input["message"])
            about = tool_input.get(ABOUT_FIELD)
            turn.done_about = [h for h in (_as_hwnd(v) for v in about) if h] if isinstance(about, list) else []
            states = tool_input.get(STATES_FIELD)
            turn.done_states = [s for s in states if isinstance(s, dict)][:8] if isinstance(states, list) else []
            learned = " ".join(str(tool_input.get("learned") or "").split())
            if learned and hasattr(self, "_learning"):
                self._learning["note"] = learned
        if (name == "remember_how" and outcome.ok and hasattr(self, "_learning")
                and not (isinstance(outcome.payload, dict) and outcome.payload.get("held"))):
            self._learning["saved"] = True   # it already kept what it learned

        verdict: tuple[bool | None, str] | None = None
        send_note: str | None = None
        if name in ACTION_TOOL_NAMES:
            # What the action's own report verifies (v3.14): an outcome is said only on a verified effect.
            verdict = action_verdict(name, outcome)
            hwnd_acted = target.get("hwnd")
            if (verdict[0] is None and outcome.ok and not finish_gate.is_expected(verdict[1])
                    and target.get("control")):
                # Revamp D (S2): a control practice memory learned never shows its effect is expected-unverifiable.
                learned = self._learned_control(name, target)
                if learned:
                    verdict = (None, finish_gate.LEARNED_UNVERIFIABLE.format(n=learned))
            if outcome.ok and judged is not None and judged.sends:
                # Revamp D (S2): a send is confirmed from memory's own capture of the chat, independently of the
                # action's report.
                text = (str(tool_input.get("text") or "") if name == "type_text"
                        else self._typed.get(int(hwnd_acted or 0), ""))
                if text.strip():
                    conf = self._confirm_send(target, text, dispatched_at)
                    self._sends.append({"tool": name, "confirmed": conf["confirmed"], "waited_ms": conf["waited_ms"],
                                        "similarity": conf["similarity"]})
                    if conf["confirmed"]:
                        verdict = (True, f"memory's capture of the chat shows the message arrived "
                                         f"({conf['waited_ms']} ms after it was sent)")
                        send_note = verdict[1]
                    else:
                        verdict = (None, f"the message did not show up in memory's capture of the chat within "
                                         f"{float(getattr(self.settings, 'send_confirm_s', 4.0)):g} s: not confirmed")
                        send_note = verdict[1]
                else:
                    self._sends.append({"tool": name, "confirmed": None, "why": "no text known to look for"})
                self._typed.pop(int(hwnd_acted or 0), None)
            elif outcome.ok and name == "type_text" and hwnd_acted:
                held = "" if tool_input.get("clear") else self._typed.get(int(hwnd_acted), "")
                self._typed[int(hwnd_acted)] = (held + str(tool_input.get("text") or ""))[-2000:]
            turn.verdicts.append((name, verdict[0], verdict[1]))
            if outcome.ok and finish_gate.classify(True, verdict[0], verdict[1]) == finish_gate.CONTRADICTED:
                turn.contradicted.append((name, verdict[1]))
            if isinstance(outcome.payload, dict):
                outcome.payload = {**outcome.payload, "verified": verdict[0],
                                   "verdict_class": finish_gate.classify(outcome.ok, verdict[0], verdict[1])}
            details = outcome.payload.get("details") if isinstance(outcome.payload, dict) else None
            target_hwnd = _view_target(name, tool_input, details if isinstance(details, dict) else {})[0]
            if target_hwnd is not None:
                if verdict[0] is True:
                    self._evidence[target_hwnd] = "verified"
                else:
                    self._evidence.pop(target_hwnd, None)
            elif name in ("run_powershell", "hotkey", "press"):
                self._evidence.clear()   # aimed at no window it names: any window may have changed
            self.logger.log("action_verdict", tool=name, verified=verdict[0], why=verdict[1], target=target_hwnd,
                            verdict_class=finish_gate.classify(outcome.ok, verdict[0], verdict[1]))
            self._learn_step(target_hwnd, bool(outcome.ok and verdict[0] is True))
        elif outcome.ok and name in ("look_at_window", "read_tabs", "read_page"):
            looked = _as_hwnd(tool_input.get("hwnd"))
            if looked is not None:
                self._evidence[looked] = "looked"

        if outcome.kind == "say":
            # A progress line for the user; the run does not pause for it.
            self.logger.log("say", text=outcome.control_input["text"], tool_use_id=tool_id,
                            **({"held": "paused"} if self._paused.is_set() else {}))
            if not self._paused.is_set():   # appTakeover (review F2): Yuki says nothing while the user has taken over
                yield Say(outcome.control_input["text"])

        # What the view after this turn's actions should show, and whether an
        # explicit read after the last action already shows it.
        if outcome.ok and name in AUTO_VIEW_TOOLS:
            turn.actions.append((name, tool_input, outcome))
            turn.looked.clear()
        elif outcome.ok and name == "look_at_window" and turn.actions:
            hwnd = _as_hwnd(tool_input.get("hwnd"))
            if hwnd is not None:
                turn.looked.add(hwnd)

        # Repetition facts for every real tool call the model made, looks
        # included (eleven identical look_at_window calls went unremarked
        # while only actions were counted). The per-turn automatic overview
        # and the view after actions never come through here, so only
        # explicit calls are counted.
        repeat_note = None
        if name not in CONTROL_TOOLS:
            repeats = self._record_call(name, acting, outcome.summary)
            repeat_note = _repeat_text(
                name, repeats, perception=name in PERCEPTION_TOOLS, summary=outcome.summary
            )
            outcome.payload = (
                {**outcome.payload, "self_facts": repeats}
                if isinstance(outcome.payload, dict)
                else {"result": outcome.payload, "self_facts": repeats}
            )

        self._log_outcome(outcome, tool_use_id=tool_id)
        self._note_action(name, outcome.ok, outcome.summary)
        if name not in CONTROL_TOOLS:
            self._trace_step(name, tool_input, ok=outcome.ok, verdict=verdict[0] if verdict else None,
                             why=verdict[1] if verdict else "", result=outcome.summary, ms=outcome.elapsed_ms,
                             outcome=outcome, control=target.get("control") if acts else None)
            if acts and verdict is not None and outcome.ok:
                self._mark_confirmations(target.get("hwnd"), verdict)
        yield ToolResult(outcome.name, outcome.ok, outcome.summary, payload=outcome.payload, about=step_words)
        if outcome.ok and finish_line:
            # N1: an action whose report says nothing moved finishes with that, never with the line written before it
            # ran ("Turned Spotify up." at 100%)
            moved_details = outcome.payload.get("details") if isinstance(outcome.payload, dict) else None
            noop = finish_gate.noop_line(name, moved_details, process=target.get("app"))
            turn.finishing.append((noop or finish_line, verdict[0] if verdict else None,
                                   verdict[1] if verdict else "", name))

        content = list(outcome.content) or [
            {"type": "text", "text": outcome.summary or "Done."}
        ]
        if repeat_note:
            content.append({"type": "text", "text": repeat_note})
        if verdict is not None and outcome.ok:
            # The action's own verdict, as a fact next to its report (v3.14): whether what it measured shows the
            # effect, so a claim about it rests on something checked (revamp D: or memory's capture of a send).
            if verdict[0] is True:
                line = f"\n(Verified: {verdict[1]}.)" if send_note else f"\n(Verified from its own report: {verdict[1]}.)"
            elif finish_gate.is_expected(verdict[1]):
                line = f"\n(Expected: {verdict[1]}.)"
            elif verdict[0] is False:
                line = f"\n(Contradicted: {verdict[1]}. What happened is only what the window shows now.)"
            else:
                line = f"\n(Not verified: {verdict[1]}. What happened is only what the window shows now.)"
            content.append({"type": "text", "text": line})
        turn.record(tool_id, self._result_block(tool_id, content, is_error=not outcome.ok))

        if not outcome.ok or outcome.kind == "done":
            turn.stop_after = True

    def _can_run_early(self, turn: _Turn, block: Any, seen: list[Any]) -> bool:
        """Whether a just-completed block may run while the response still streams.

        Only action and perception tools (and ``note_to_self``, which only
        replaces the note), only while nothing in this response has failed,
        and only when every earlier call in the response has already run --
        so an ``ask_user`` holds back everything the model wrote after it, and
        model order is kept exactly as :meth:`_run_tools` would keep it.
        ``done`` never counts as earlier: it is evaluated last anyway.
        """
        name = str(getattr(block, "name", ""))
        if name in _DEFERRED_TOOLS or name in ("spawn_helpers", "remember_how"):
            return False   # remember_how: vfix3 review F6, it waits for the response's actions (_run_tools)
        if self._risk_check(name, dict(getattr(block, "input", {}) or {}))[0] != "run":
            return False   # a confirmation or a refusal waits for the end of the stream
        if turn.stop_after or turn.failed or self._cancelled.is_set():
            return False
        for earlier in seen:
            if earlier is block:
                break
            if str(getattr(earlier, "name", "")) == "done":
                continue
            if str(getattr(earlier, "id", "")) not in turn.handled:
                return False
        return True

    def _skip_truncated(self, response: Any, turn: _Turn) -> None:
        """Refuse the call the output limit cut off, so its partial input never runs.

        At ``max_tokens`` the last content block, if it is a ``tool_use``, was
        still being written; the SDK parses whatever arrived, which can be a
        plausible-looking half of the input. Everything before it ran (or runs)
        as usual; ``done`` is then dropped like after any failure.
        """
        content = list(getattr(response, "content", []) or [])
        if not content or getattr(content[-1], "type", None) != "tool_use":
            return
        block = content[-1]
        tool_id = str(getattr(block, "id", ""))
        if tool_id in turn.handled:
            return
        name = str(getattr(block, "name", ""))
        turn.names[tool_id] = name
        turn.record(
            tool_id,
            self._skip(
                tool_id,
                name,
                "Not run: your response reached the output limit while this call was "
                "being written, so its input may be cut off.",
            ),
        )

    def _note_orphaned_tools(self, turn: _Turn) -> None:
        """Tell the model which tools ran before its response broke off.

        When a stream fails after some tools already ran, there is no final
        message to store, so the transcript would otherwise not show that
        anything happened on the machine.
        """
        ran = [
            f"{turn.names.get(tool_id, '?')}: "
            + " ".join(
                str(part.get("text") or "")
                for part in block.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()[:300]
            for tool_id, block in turn.results.items()
        ]
        self.logger.log("context_edit", reason="orphaned_tool_results", tools=ran)
        self.context.add_note(
            "Your last response broke off with an error after these tools had already "
            "run:\n" + "\n".join(f"- {line}" for line in ran)
        )

    #: How often a wait on a read looks at the cancel flag and the steering inbox (s).
    ABORT_POLL_S = 0.05

    def _abortable(self, name: str, run: Any) -> ToolOutcome:
        """Run a read-only tool so that a cancel or the user's correction stops the wait for it (v3.14).

        The read goes on, unwatched, on its own thread and its result is dropped: it changes nothing on the machine,
        so nothing is left half done. An action is never run this way (it always finishes). Logged ``read_abandoned``.
        """
        started = time.perf_counter()
        value, why = self._race(name, run)
        if why is None:
            return value
        elapsed = (time.perf_counter() - started) * 1000
        text = f"Not finished: {why} while it was reading, so it was left; nothing on the PC was changed."
        return ToolOutcome(name=name, ok=False, summary=text, content=[{"type": "text", "text": text}],
                           payload={"abandoned": why}, elapsed_ms=elapsed)

    def _race(self, name: str, run: Any) -> tuple[Any, str | None]:
        """``(run()'s value, None)``, or ``(None, why)`` when a cancel or the user's words came first (then ``run``
        goes on unwatched: only ever a read). Logged ``read_abandoned``."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["out"] = run()
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
                box["exc"] = exc
            finally:
                done.set()

        started = time.perf_counter()
        threading.Thread(target=work, name=f"yuki-read-{name}", daemon=True).start()
        while not done.wait(self.ABORT_POLL_S):
            why = ("the user stopped the request" if self._cancelled.is_set() else
                   "the user said something more" if not self._steers.empty() else None)
            if why is not None:
                self.logger.log("read_abandoned", tool=name, why=why,
                                after_ms=round((time.perf_counter() - started) * 1000))
                return None, why
        if "exc" in box:
            raise box["exc"]
        return box["out"], None

    def _dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        """Run one tool, refusing anything outside this agent's subset.

        The subset is enforced here rather than in the dispatcher because it
        belongs to the agent: two agents can share one dispatcher (and one
        PowerShell session) while showing the model different tools.
        """
        if name in MEMORY_READ_KINDS and (self.tool_names is None or "memory" in self.tool_names):
            # revamp D: the seven memory reads are one tool; a call by an old name runs as that kind
            name, tool_input = "memory", {**tool_input, "kind": name}
        if self.tool_names is not None and name not in self.tool_names:
            return unavailable_tool(name, self.tool_names)
        if name in MEMORY_TOOLS:
            if not self.memory.installed:
                return unavailable_tool(name, self.tool_names or ())
            learned = None
            if name == "about" and str(tool_input.get("heard_as") or "").strip():
                learned = self._learn_heard(tool_input)
                tool_input = {k: v for k, v in tool_input.items() if k != "heard_as"}
            outcome = self.memory.run_tool(name, tool_input, request=self._request_text)
            if learned:
                outcome.summary = f"{learned} {outcome.summary}".strip()
                outcome.content = [{"type": "text", "text": learned}, *list(outcome.content or [])]
            return outcome
        return self.dispatcher.dispatch(name, tool_input)

    def _learn_heard(self, tool_input: dict[str, Any]) -> str:
        """N9 (report 47): keep how speech recognition heard a person's name (``about``'s ``heard_as``), confirmed or
        corrected by the user, as that person's misheard form in memory's name aliases (the store's existing
        ``add_alias``; memory's people index resolves it from then on, and recognition keyterms never include a heard
        form). Only for a name memory's people index knows; the user's own words this request must hold the heard
        form (a span of what they said, so a guess of the model's is never kept), and they must have said something
        after it: an answer to a question, words added mid-run, or a later request (review F4: a first guess, before
        any question or answer, is never kept; whether those words confirm it is the model's judgement, which is why
        it passes ``heard_as`` only then). Returns one line for the result."""
        heard = " ".join(str(tool_input.get("heard_as") or "").split())[:60]
        name = " ".join(str(tool_input.get("name") or "").split())[:80]
        own = [w for w in self._user_words
               if isinstance(w, dict) and w.get("how") in ("typed", "heard", "added", "answer")]
        if not heard or not name or heard.casefold() == name.casefold():
            return ""
        needle = " ".join(heard.casefold().split())
        first = next((i for i, w in enumerate(own) if needle in " ".join(str(w.get("text") or "").casefold().split())),
                     None)
        if first is None:
            return (f"Not kept as a misheard form: \"{heard}\" is not in what they said in this request (keep only the "
                    f"words as heard).")
        if not any(w.get("how") in ("answer", "added") or (w.get("request") or 0) > (own[first].get("request") or 0)
                   for w in own[first + 1:]):
            return (f"Not kept as a misheard form yet: they have not confirmed that \"{heard}\" meant {name} (pass "
                    f"heard_as only after their answer or correction says so).")
        call = getattr(self.memory, "call", None)
        if not callable(call):
            return ""

        def keep(client: Any) -> Any:
            found = client.contact(name)
            if not isinstance(found, dict) or not found.get("found"):
                return None
            return client.store.add_alias(heard, str(found.get("name") or name),
                                          why=f"speech recognition heard it so; the user confirmed it was "
                                              f"{found.get('name') or name} (voice, {time.strftime('%Y-%m-%d')})")

        try:
            row = call("add_alias", keep, timeout_s=2.0)
        except Exception as exc:  # noqa: BLE001 - memory never breaks a request
            self.logger.log("heard_alias", ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
            return "The misheard form could not be kept (memory did not answer)."
        self.logger.log("heard_alias", ok=bool(row), chars=len(heard))
        return (f"Kept: \"{heard}\" is how speech recognition hears {name}; it resolves to them from now on."
                if row else f"Not kept: memory's people index has no one called {name}.")

    def _paused_skip(self, turn: _Turn, tool_id: str, name: str, tool_input: dict[str, Any],
                     step_words: dict[str, Any]) -> Iterator[AgentEvent]:
        """appTakeover: an action whose step was already shown, kept from running because the user took over (the
        request waits for them after this turn). Not a failure of the call: the loop breaker and the escalation signals
        leave it out."""
        turn.gated.add(tool_id)
        turn.record(tool_id, self._skip(tool_id, name, PAUSED_SKIP))
        self._note_action(name, False, "not run: the user took over (paused)")
        self._trace_step(name, tool_input, ok=False, kind="control", why="paused: the user took over",
                         result=PAUSED_SKIP)
        yield ToolResult(name, False, PAUSED_SKIP, about=step_words)
        turn.stop_after = True

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

    def _capture_overview(self, prebuilt: tuple[str, Any, float] | None = None) -> tuple[str, str | None]:
        """Grab the desktop overview handed to the model at the start of each turn.

        A failure here is reported to the model as text rather than raised: not
        being able to see the desktop is something Yuki should know about and work
        around, not a crash. ``prebuilt``: ``(text, payload, elapsed_ms)`` read ahead
        for a request pack (:meth:`build_pack`), fenced here with this request's code.

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
            text, payload, elapsed = prebuilt if prebuilt is not None else self.dispatcher.overview_text()
        except Exception as exc:
            self._overview_s += time.perf_counter() - looked
            message = f"(could not read the desktop: {type(exc).__name__}: {exc})"
            self.logger.error(message, exc=exc)
            self.logger.log("self_facts", **facts)
            return message, facts_text
        self._overview_s += time.perf_counter() - looked
        self._overview_payload = payload
        # window titles come from apps and web pages: fenced like any other outside text
        text = wrap(text, source="the window list and what the windows show (text written by apps and web pages)",
                    nonce=self._nonce)
        if isinstance(payload, dict):
            payload = {**payload, "self_facts": facts}
        self.logger.perception(
            "look_at_desktop", size_chars=len(text), elapsed_ms=elapsed, payload=payload
        )
        return text, facts_text

    def _request_origin(self, origin_hwnd: int | None) -> str | None:
        """Where the user was when they asked, as one line of facts, or ``None``.

        The window in front (from the overview just captured, with
        ``origin_hwnd`` taking precedence) plus :meth:`Dispatcher.activity_facts`:
        media sessions and microphone/camera use. Attached to the request's
        first message and never stubbed, so it is still there when the model
        finishes. What to make of it is the model's call. Logged as
        ``request_origin``; never raises.
        """
        started = time.perf_counter()
        try:
            facts = self.dispatcher.activity_facts()
        except Exception as exc:  # the origin must never break a request
            self.logger.error(f"activity facts failed: {type(exc).__name__}: {exc}", exc=exc)
            facts = {"errors": {"activity": f"{type(exc).__name__}: {exc}"}}
        calls: list[dict[str, Any]] = []
        if facts.get("microphone"):
            # a call memory knows is going on (merged across its restarts): "on a Discord call since
            # 13:20", not Windows' microphone start time (reset by a reboot or a rejoin)
            try:
                fetch = getattr(self.memory, "call_method", None)
                found = fetch("current_calls", timeout_s=1.0) if callable(fetch) else None
                calls = [c for c in (found or []) if isinstance(c, dict)]
            except Exception:
                calls = []
        try:
            text = _origin_text(self._overview_payload, facts, origin_hwnd=origin_hwnd, calls=calls)
        except Exception as exc:
            self.logger.error(f"request origin failed: {type(exc).__name__}: {exc}", exc=exc)
            text = None
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        self._overview_s += elapsed_ms / 1000
        self.logger.log(
            "request_origin",
            text=text,
            origin_hwnd=origin_hwnd,
            facts=facts,
            elapsed_ms=elapsed_ms,
        )
        return text

    # -- memory ------------------------------------------------------------

    def _live_screen(self, origin_hwnd: int | None = None, *, board: Any = None,
                     payload: Any = None) -> dict[str, Any] | None:
        """The window the request came from as the Now board read it this turn (the dispatcher's last picture), as
        the whole-memory ranking's screen anchor; ``None`` without one. The service's own last capture lags a fresh
        window switch by its settle time (revamp 02 §3.3: "On their screen" named the wrong window twice).
        ``board`` / ``payload``: a request pack's own picture and window list (:meth:`build_pack`)."""
        board = board if board is not None else getattr(self.dispatcher, "last_now_board", None)
        if not isinstance(board, dict):
            return None
        payload = payload if isinstance(payload, dict) else (
            self._overview_payload if isinstance(self._overview_payload, dict) else {})
        want = origin_hwnd or payload.get("foreground_hwnd") or board.get("foreground")
        window = next((w for w in board.get("windows") or [] if isinstance(w, dict) and w.get("hwnd") == want), None)
        if window is None or window.get("blocked"):
            return None
        title = window.get("live_title") or window.get("title")
        if not (title or window.get("text")):
            return None
        url = window.get("url") or None
        host = None
        if url:
            try:
                from urllib.parse import urlparse

                host = urlparse(str(url)).hostname
            except ValueError:
                host = None
        return {"app": window.get("app"), "process": window.get("process"), "title": title, "url": url,
                "scope": window.get("scope"), "host": host, "kind": window.get("kind"),
                "text": str(window.get("text") or ""), "at": window.get("read_at"), "by_yuki": bool(window.get("by_yuki")),
                "source": "now"}

    def _frame_inputs(self, words: str | None, screen: dict[str, Any] | None,
                      apps: list[str]) -> tuple[dict[str, Any] | None, Any, dict[str, Any] | None]:
        """What the task frame is built from: memory's ``frame_facts`` (people named in the words and earlier in
        this conversation, the chat in front, the chats in front lately, the call, routes, pitfalls; bounded), this
        turn's focus read (the dispatcher's, no second read) and the clipboard's metadata (a few ms; private content
        never read). Each is ``None`` when it cannot be had; never raises."""
        facts = None
        fetch = getattr(self.memory, "frame_facts", None)
        if callable(fetch):
            try:
                facts = fetch(words or "", said=list(self._said), screen=screen, apps=apps)
            except Exception:
                facts = None
        focus = getattr(self.dispatcher, "last_focus", None)
        clip = None
        backend = getattr(self.dispatcher, "backend", None)
        if backend is not None:
            try:
                clip = task_frame.clipboard_meta(backend)
            except Exception:
                clip = None
        return (facts if isinstance(facts, dict) else None), focus, clip

    def _memory_context(
        self, request: str, *, first: bool = False, lean: bool = False, query: str | None = None,
        previous: str | None = None, apps: list[str] | None = None, origin_hwnd: int | None = None,
        prefetched: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        """Split what memory knows into the system layers and this request's message blocks.

        ``prefetched``: what a request pack fetched ahead (:meth:`build_pack`: the screen anchor, the frame's
        inputs and memory's context), used instead of fetching now.

        System layers (``prompt.system_blocks``, cached, stored on the agent for
        ``_request_params``): the **profile** (the user's names from settings and the
        portrait's Personality section) and the **rules** (the rules and preferences part
        of the standing context, verbatim, fetched fresh every request so a rule saved a
        moment ago shows).

        Returned for the request's first message: the memory block (background: the rest
        of the portrait, the open to-dos, know-how for the foreground app and the request,
        and on this conversation's first request where the last session left off) and the
        open-commitments block (the volatile half of the standing context), placed right
        before the user's words by ``ContextManager.add_request``. Either is ``None`` when
        there is nothing to say. A portrait identical to one already attached in full
        earlier in this conversation -- still there, never stubbed or collapsed -- is named
        rather than sent again. Logs ``memory_context`` every time, whatever happened;
        never raises.

        Args:
            request: The request text.
            first: This is the conversation's first request: attach the
                resume context too.
            lean: Leave the rest of the portrait and the resume out of the block
                (the profile and rules layers are unchanged).
            query: The user's own words (what memory is searched with); ``None``:
                :func:`yuki.agent.memory.memory_query_of` the request.
            previous: The previous request's words.
            apps: Apps the caller says the request names.
            origin_hwnd: The window the request came from (the screen anchor).

        The rules layer holds the user's rules and preferences only (or the "no rules" text); their
        to-dos and Yuki's commitments go in the commitments block (revamp 09 P0.1). To-dos, know-how,
        threads and recent screens come only as far as they bear on the words (revamp 02 P2).
        """
        started = time.perf_counter()
        app = _foreground_app(self._overview_payload)
        query = query if query is not None else memory_query_of(request)
        self._profile_layer = profile_text(self.settings.user_names, None)
        self._rules_layer = None
        self._frame = None
        self._surfaced = {"brief_ids": [], "brief_at": None, "fact_ids": []}
        try:
            if prefetched is not None:
                screen = prefetched.get("screen")
                facts, focus, clip = prefetched.get("inputs") or (None, None, None)
            else:
                screen = self._live_screen(origin_hwnd)
                # the task frame (revamp Phase B): who and what the request may refer to, built in code from the Now
                # board, this conversation, the focus, the clipboard and memory's people index; a request whose
                # frame has a person, a route or a referent in play (or a spoken hand-off) is an action request, for
                # which Yuki's own past exchanges from other days weigh less in the whole-memory ranking
                facts, focus, clip = self._frame_inputs(query, screen, list(apps or ()))
            frame = task_frame.build(facts, focus=focus, clip=clip, overview=self._overview_payload)
            action = bool(self._request_voice or frame.action)
            if prefetched is not None and prefetched.get("fetched") is not None:
                fetched = prefetched["fetched"]
            else:
                fetched = self._fetch_memory(app=app, query=query, first=first, lean=lean, previous=previous,
                                             apps=apps or (), screen=screen, action=action,
                                             payload=self._overview_payload)
            frame = task_frame.build(facts, focus=focus, clip=clip, knowhow=fetched.knowhow,
                                     overview=self._overview_payload, practice=getattr(fetched, "practice", None),
                                     named_app=getattr(self, "_request_app_named", None),
                                     lessons=self._lessons_for(self._overview_payload),
                                     lately=self._people_lately() if self._request_voice else ())
            self._frame = frame
            self._practice_shown = list(frame.playbooks)
            rest, personality = _split_personality(fetched.portrait)
            rules = standing_rules_text(fetched)
            commitments = standing_commitments_text(fetched)
            brief = getattr(fetched, "brief", None)
            # the personal brief (revamp 09 P1) is the thinker's "who they are and how to be with them": it takes the
            # place of the Personality section and the persona, typed and voice alike; without one, as before
            self._profile_layer = profile_text(self.settings.user_names, None if brief else personality,
                                               None if brief else fetched.persona, getattr(fetched, "people", None),
                                               brief=brief)
            self._surfaced = {"brief_ids": list(getattr(fetched, "brief_ids", None) or []) if brief else [],
                              "brief_at": getattr(fetched, "brief_at", None) if brief else None,
                              "fact_ids": [f"{r.get('kind')}:{r.get('id')}" for r in fetched.relevant
                                           if isinstance(r, dict) and r.get("id") is not None]
                              + [f"pb:{i}" for i in frame.playbooks]}
            if fetched.source != "unavailable" and rules is not None:
                self._rules_layer = rules_text(rules)
            background = replace(fetched, portrait=None if lean else ((rest or "").strip() or None),
                                 resume=None if lean else fetched.resume,
                                 knowhow=list(fetched.knowhow)[frame.procedures:])   # the frame shows the first
            repeated = None
            if (
                background.portrait
                and self._portrait_sent is not None
                and self._portrait_sent[0] == background.portrait
            ):
                repeated = self._portrait_sent[1]
            text = memory_block_text(background, repeated_turn=repeated)
            standing = commitments_block_text(commitments)
            # fast memory: the few memories bearing on these words, right before them (a volatile block in
            # the request's messages, like the commitments: the cached system layers never change for it)
            relevant = relevant_block_text(fetched.relevant)
            if relevant:
                standing = f"{relevant}\n\n{standing}" if standing else relevant
            if frame.text:   # the frame first: who and what, then what memory connects to it
                standing = f"{frame.text}\n\n{standing}" if standing else frame.text
        except Exception as exc:  # memory must never break a request
            self.logger.error(f"memory context failed: {type(exc).__name__}: {exc}", exc=exc)
            self._memory_info = {
                "source": "error", "context_chars": 0, "portrait_chars": 0,
                "standing_chars": 0, "resume_chars": 0,
            }
            return None, None
        portrait_chars = len(background.portrait or "") if text and repeated is None else 0
        standing_chars = len(fetched.standing or "")
        resume_chars = len(fetched.resume or "") if text else 0
        if text and background.portrait and repeated is None:
            self._portrait_sent = (background.portrait, self.logger.turn)
            self._surfaced["fact_ids"].append("portrait")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        self._memory_info = {
            "source": fetched.source,
            "context_chars": len(text or ""),
            "portrait_chars": portrait_chars,
            "portrait_repeated_from_turn": repeated,
            "standing_chars": standing_chars,
            "resume_chars": resume_chars,
            "resume_requested": first,
            "lean": lean,
            "truncated": list(fetched.truncated),
            "knowhow_lines": len(fetched.knowhow) if text else 0,
            "todo_lines": len(fetched.todos) if text else 0,
            "todos_held": fetched.todos_held,
            "commitments_chars": len(commitments or ""),
            "rules_layer_chars": len(self._rules_layer or ""),
            "connected_chars": len(relevant or ""),
            "self_echo": int((fetched.related or {}).get("self_echo") or 0),
            "query_chars": len(query or ""),
            "relevant_items": len(fetched.relevant),
            "relevant_ms": fetched.relevant_ms,
            "persona_chars": len(fetched.persona or ""),
            "people_chars": len(getattr(fetched, "people", None) or ""),
            "brief_chars": len(getattr(fetched, "brief", None) or ""),
            "brief_ids": list(getattr(fetched, "brief_ids", None) or []),
            "action": action,
            "own_discounted": int((fetched.related or {}).get("own_discounted") or 0),
            "frame": frame.info(),
            "practice": _practice_info(getattr(fetched, "practice", None)),
            "lookup_ms": elapsed_ms,
        }
        self.logger.log(
            "memory_context",
            chars=len(text or ""),
            source=fetched.source,
            attached=text is not None,
            portrait_chars=portrait_chars,
            portrait_repeated_from_turn=repeated,
            standing_chars=standing_chars,
            commitments_attached=standing is not None,
            profile_layer_chars=len(self._profile_layer or ""),
            personality_chars=len(personality or ""),
            rules_layer_chars=len(self._rules_layer or ""),
            commitments_chars=len(commitments or ""),
            resume_chars=resume_chars,
            resume_requested=first,
            truncated=list(fetched.truncated),
            knowhow_lines=len(fetched.knowhow),
            todo_lines=len(fetched.todos),
            todos_held=fetched.todos_held,
            connected_chars=len(relevant or ""),
            self_echo=int((fetched.related or {}).get("self_echo") or 0),
            query_chars=len(query or ""),
            screen_source=("now" if screen else None),
            relevant=[{"kind": r.get("kind"), "id": r.get("id"), "cosine": r.get("cosine"), "score": r.get("score")}
                      for r in fetched.relevant],
            relevant_ms=fetched.relevant_ms,
            persona_chars=len(fetched.persona or ""),
            people_chars=len(getattr(fetched, "people", None) or ""),
            brief_chars=len(getattr(fetched, "brief", None) or ""),
            brief_ids=list(getattr(fetched, "brief_ids", None) or []),
            action=action,
            own_discounted=int((fetched.related or {}).get("own_discounted") or 0),
            frame=frame.info(),
            practice=_practice_info(getattr(fetched, "practice", None)),
            frame_facts_ms=(facts or {}).get("elapsed_ms") if isinstance(facts, dict) else None,
            memory_session=getattr(self.memory, "session_id", None),
            app=app,
            error=fetched.error,
            elapsed_ms=elapsed_ms,
        )
        return text, standing

    def _fetch_memory(self, *, app: str | None, query: str, first: bool, lean: bool, previous: str | None,
                      apps: Any, screen: Any, action: bool, payload: Any) -> Any:
        """Memory's context for one request (the open windows' processes are the apps in play for practice memory)."""
        open_apps = sorted({str(w.get("process_name")) for w in (payload or {}).get("windows") or []
                            if isinstance(w, dict) and w.get("process_name")}) if isinstance(payload, dict) else []
        return self.memory.context(app=app, query=query, resume=first and not lean, previous=previous,
                                   apps=apps or (), screen=screen, action=action, open_apps=open_apps)

    # -- the request pack (revamp D, S5) ---------------------------------------

    def build_pack(self, request: str = "", *, origin_hwnd: int | None = None, view_hwnd: int | None = None,
                   memory_query: str | None = None, previous_query: str | None = None,
                   memory_apps: Iterable[str] | None = None, lean: bool = False, voice: bool = False,
                   with_memory: bool = True) -> RequestPack:
        """Build what ``run`` needs before its first model call, now, so the request does not wait for it.

        For a caller that knows a request is coming (the voice, while the user is still speaking: 07 §6 E2): the
        window list with the Now picture, the request origin, the window the request is about (``view_hwnd``) and
        memory's part. Touches nothing of a running request (call it while this agent is idle, or from the thread
        that will run it). Never raises: a part that fails is left out and ``run`` builds it then. Logged as
        ``request_pack_built``."""
        started = time.perf_counter()
        pack = RequestPack(built_at=time.monotonic(), origin_hwnd=origin_hwnd, view_hwnd=view_hwnd)
        try:
            t0 = time.perf_counter()
            pack.overview = self.dispatcher.overview_text()
            pack.parts_ms["overview"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001 - run reads it then
            self.logger.log("request_pack_part", part="overview", error=f"{type(exc).__name__}: {exc}")
        payload = pack.overview[1] if pack.overview else None
        try:
            t0 = time.perf_counter()
            facts = self.dispatcher.activity_facts()
            calls: list[dict[str, Any]] = []
            if facts.get("microphone"):
                fetch = getattr(self.memory, "call_method", None)
                found = fetch("current_calls", timeout_s=1.0) if callable(fetch) else None
                calls = [c for c in (found or []) if isinstance(c, dict)]
            pack.origin = _origin_text(payload, facts, origin_hwnd=origin_hwnd, calls=calls)
            pack.parts_ms["origin"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001
            self.logger.log("request_pack_part", part="origin", error=f"{type(exc).__name__}: {exc}")
        if view_hwnd:
            try:
                t0 = time.perf_counter()
                pack.view = self.dispatcher.window_view(int(view_hwnd), settle=False)
                pack.parts_ms["view"] = round((time.perf_counter() - t0) * 1000, 1)
            except Exception as exc:  # noqa: BLE001
                self.logger.log("request_pack_part", part="view", error=f"{type(exc).__name__}: {exc}")
        if with_memory and getattr(self.memory, "installed", False):
            try:
                t0 = time.perf_counter()
                query = " ".join(str(memory_query or "").split()) or memory_query_of(request)
                board = getattr(self.dispatcher, "last_now_board", None)
                screen = self._live_screen(origin_hwnd, board=board, payload=payload)
                inputs = self._frame_inputs(query, screen, list(memory_apps or ()))
                frame = task_frame.build(inputs[0], focus=inputs[1], clip=inputs[2], overview=payload)
                fetched = self._fetch_memory(
                    app=_foreground_app(payload), query=query, first=self._requests == 0, lean=lean,
                    previous=" ".join(str(previous_query or "").split()) or self._previous_query,
                    apps=list(memory_apps or ()), screen=screen, action=bool(voice or frame.action), payload=payload)
                pack.memory = {"screen": screen, "inputs": inputs, "fetched": fetched, "query": query}
                pack.parts_ms["memory"] = round((time.perf_counter() - t0) * 1000, 1)
            except Exception as exc:  # noqa: BLE001
                self.logger.log("request_pack_part", part="memory", error=f"{type(exc).__name__}: {exc}")
        pack.build_ms = round((time.perf_counter() - started) * 1000, 1)
        self.logger.log("request_pack_built", build_ms=pack.build_ms, parts_ms=dict(pack.parts_ms),
                        view=pack.view is not None, memory=pack.memory is not None)
        return pack

    # -- revamp D: the user's words, authorization, the second look, send confirmation -----------------------

    def _setup_input_gate(self) -> None:
        """Quieter hands (S7): on the real desktop, the dispatcher waits for the user to stop typing or using the mouse
        before global input; its notices become this request's ``Say`` events. Stub desks get none."""
        dispatcher = self.dispatcher
        if getattr(dispatcher, "notices", None) is None:
            try:
                dispatcher.notices = queue.SimpleQueue()
            except Exception:  # noqa: BLE001 - a dispatcher double without the attribute
                return
        # appTakeover: a pause (the user took over) also ends a wait to send input: nothing is sent
        dispatcher.cancel_check = lambda: self._cancelled.is_set() or self._paused.is_set()
        try:
            from yuki.agent.tools import _LazyRealBackend

            if (getattr(self.settings, "input_yield", True) and getattr(dispatcher, "input_gate", None) is None
                    and isinstance(getattr(dispatcher, "backend", None), _LazyRealBackend)):
                from yuki.actions.leases import InputGate

                dispatcher.input_gate = InputGate(
                    recent_s=float(getattr(self.settings, "input_yield_recent_s", 1.0)),
                    quiet_s=float(getattr(self.settings, "input_yield_quiet_s", 0.8)),
                    max_s=float(getattr(self.settings, "input_yield_max_s", 12.0)),
                    probe_s=float(getattr(self.settings, "input_yield_probe_s", 0.25)),
                    notify_after_s=float(getattr(self.settings, "input_yield_notify_s", 3.0)))
        except Exception:  # noqa: BLE001 - the hands work without it
            pass

    def _note_words(self, text: str | None, *, how: str, question: str | None = None) -> None:
        """One piece of what was said, for the judge (``yuki.agent.judge.words_text`` labels it): the user's own words
        (``typed``, ``heard``, ``added`` mid-run, an ``answer``) or context that is never theirs (``reading``: the
        voice talker's rendering; ``card`` / ``suggestion``: Yuki's own card text the request carried). Each entry
        keeps its request number and time; when a new request's words arrive, words of earlier requests older than
        :data:`USER_WORDS_WINDOW_S` before it are dropped (review 34 M3: this morning's "tell sam I'm late" must not
        authorise a send in a later request)."""
        text = " ".join(str(text or "").split())
        if text:
            now = time.time()
            if how in REQUEST_WORD_KINDS and not any(w.get("request") == self._requests for w in self._user_words):
                keep = [w for w in self._user_words if now - float(w.get("at") or 0.0) <= USER_WORDS_WINDOW_S]
                self._user_words.clear()
                self._user_words.extend(keep)
            entry: dict[str, Any] = {"how": how, "text": text, "at": now, "request": self._requests}
            if question:
                entry["question"] = question
            self._user_words.append(entry)
            if how in ("answer", "added") and getattr(self, "authorizer", None) is not None:
                self.authorizer.forget_denials()   # their new words: a held call is judged again, not replayed

    def _note_request_words(self, request: str, query: str, user_words: Any = None) -> None:
        """A new request's words for the judge, each labelled by whose they are (review 34 H4, M3). A card the request
        opens with (Yuki's own bracketed card: a reply to it, or its suggestion sent unchanged) is noted as Yuki's,
        never the user's words. Typed: the rest is the user's words. Spoken: the heard turns (``user_words``, the
        speech-to-text the voice passes; else the heard words in the hand-off's own notes) are the user's words, and
        the talker's rendering is noted as Yuki's reading of it, which never authorises a send."""
        from yuki.agent.memory import VOICE_HEARD_MARK

        text = (request or "").strip()
        suggestion = text.startswith(SUGGESTION_MARK)
        while text.startswith("[") and "]\n" in text:   # Yuki's own card lines first (its own request format)
            card, text = text[1:].split("]\n", 1)
            if not suggestion:
                self._note_words(card, how="card")
            text = text.strip()
        cut = text.find("\n\n[")
        head, notes = (text[:cut], text[cut:]) if cut >= 0 else (text, "")
        if suggestion:   # the card's own words, sent unchanged: Yuki's offer, never typed by the user
            self._note_words(head or query, how="suggestion")
            return
        if not self._request_voice:
            self._note_words(head or query, how="typed")
            return
        heard = [user_words] if isinstance(user_words, str) else list(user_words or [])
        # V4 (3 Oct): the voice passed each heard turn as {"how", "text"}; the runtime turned each into its Python repr,
        # so the judge read "{'how': 'voice', 'text': ...}". A dict entry is taken by its text (our own shape).
        heard = [h.get("text") if isinstance(h, dict) else h for h in heard]
        heard = [" ".join(str(h).split()) for h in heard if h and str(h).strip()]
        if not heard and VOICE_HEARD_MARK in notes:   # the hand-off's own notes carry the heard turns
            block = notes[notes.find(VOICE_HEARD_MARK):]
            block = block[: block.find("]\n")] if "]\n" in block else block
            colon = block.find("): ")
            if colon >= 0:
                quoted = block[colon + 3:].strip().rstrip("]").strip()
                heard = [t.strip().strip('"') for t in quoted.split('" / "') if t.strip().strip('"')]
        for h in heard[-USER_WORDS_HEARD:]:
            self._note_words(h, how="heard")
        self._note_words(head or query, how="reading")

    def _call_target(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """The window a call acts on (from its own arguments, the element's window, else the window in front) and the
        element's role and name, as the judge and the trace name them."""
        hwnd = _as_hwnd(tool_input.get("expect_hwnd")) or (
            _as_hwnd(tool_input.get("hwnd")) if name in ("window_action", "tab", "focus_window") else None)
        if hwnd is None and tool_input.get("element") is not None:
            hwnd = _as_hwnd(getattr(self.dispatcher, "_latest_tree_hwnd", None))
        if hwnd is None and isinstance(self._overview_payload, dict) and name in (
                "type_text", "hotkey", "press", "click", "scroll", "hover"):
            hwnd = _as_hwnd(self._overview_payload.get("foreground_hwnd"))
        window = self._window_of(hwnd) or {}
        target: dict[str, Any] = {"hwnd": hwnd, "app": window.get("process_name"), "title": window.get("title")}
        describe = getattr(self.dispatcher, "element_info", None)
        element = describe(tool_input) if callable(describe) else None
        if element:
            target["element"] = f"{element.get('role') or 'element'} \"{element.get('name') or ''}\""
            target["element_name"] = element.get("name") or ""
            # V6: a scroll's control is its role in that app (a page's name is its title, different on every page),
            # so what practice memory learns about scrolling there is learned per app
            target["control"] = (f"{element.get('role') or ''}|" if name == "scroll"
                                 else f"{element.get('role') or ''}|{element.get('name') or ''}")
            target["role"] = element.get("role") or ""
        elif name == "click":
            # Review 34 H2: a click at raw coordinates is gated by the element under the point in the latest read.
            pointed = getattr(self.dispatcher, "pointed_element", None)
            under = pointed(tool_input) if callable(pointed) else None
            if under:
                target["element"] = f"{under.get('role') or 'element'} \"{under.get('name') or ''}\" (under the point)"
                target["element_name"] = under.get("name") or ""
                target["role"] = under.get("role") or ""
        if name == "click":
            # text Yuki typed into this window and has not sent yet: a click there can send it (review 34 H2)
            target["typed_pending"] = bool(str(getattr(self, "_typed", {}).get(int(hwnd or 0), "")).strip())
        if name == "tab":
            names = (getattr(self.dispatcher, "tab_names", {}) or {}).get(hwnd or 0) or {}
            tab = names.get(str(tool_input.get("tab") or "").strip().strip("[]"))
            if tab:
                target["element"] = f"tab \"{tab}\""
                target["tab_name"] = tab
        return target

    def _step_about(self, name: str, tool_input: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        """What a person can read about an action's target, for the UI's step words (``_about`` on the ``tool_call``
        event, revamp 38 §8c): the control's role and visible name and the tab's name from the latest read, the app's
        name, whether the call sends or changes something for certain (its fixed traits, never words) and the brain's
        declared effect. Never the command, typed text, address or a window or element number. Never raises."""
        about: dict[str, Any] = {}
        try:
            if target.get("element_name"):
                about["element"] = str(target["element_name"])[:80]
            if target.get("role"):
                about["role"] = str(target["role"])
            if target.get("tab_name"):
                about["tab"] = str(target["tab_name"])[:80]
            app = _app_display(target.get("app"))
            if app:
                about["app"] = app
            traits = call_traits(name, tool_input, role=target.get("role"),
                                 typed_pending=bool(target.get("typed_pending")))
            if sends_for_certain(traits):
                about["sends"] = True
            fixed = tool_traits(name, tool_input)
            if (fixed.destroys or fixed.reaches) and fixed.needs != "input":
                about["changes"] = True   # a shell, a close: an effect for certain (keys and clicks only may be)
            if fixed.needs == "focus" and (fixed.destroys or fixed.reaches):
                about["local"] = True     # open_url of a file or another program's handler
            risk = tool_input.get(RISK_FIELD)
            effect = " ".join(str(risk.get("effect") or "").split()) if isinstance(risk, dict) else ""
            if effect:
                about["effect"] = effect[:160]
        except Exception as exc:  # noqa: BLE001 - presentation only, never in the way of the call
            self.logger.log("step_about_failed", tool=name, error=f"{type(exc).__name__}: {exc}")
        return about

    def _outbound_read(self, name: str) -> bool:
        """A ``fetch_url`` after a ``read_file`` in this request (the brain's own or a helper's): the address could
        carry what the file says out, so it is judged (review 34 M2). The tool sequence only."""
        return name == "fetch_url" and bool(getattr(self, "_files_read", False))

    def _authorize(self, name: str, tool_input: dict[str, Any], target: dict[str, Any]) -> Any:
        """The judge's verdict for one action, or ``None`` when its metadata and the policy do not send it there."""
        outbound = self._outbound_read(name)
        if self.authorizer is None or (name not in ACTION_TOOL_NAMES and not outbound):
            return None
        # Yuki's own earlier calls of this request (the calls, never what came back: auto mode's classifier sees the
        # agent's tool calls too), so a message is judged whole (a mention typed by the call before is part of it).
        earlier = [f"{s.get('tool')} {json.dumps(_acts_only(s.get('input')), ensure_ascii=False, default=str)}"
                   for s in getattr(self, "_trace", []) if s.get("kind") == "action" and s.get("ok")]
        if outbound:
            # the file reads it follows (the calls only), so the judge sees what the address may carry
            earlier += [f"{s.get('tool')} {json.dumps(s.get('input') or {}, ensure_ascii=False, default=str)}"
                        for s in getattr(self, "_trace", []) if s.get("tool") == "read_file"]
        verdict = self.authorizer.check(name, tool_input, words=list(self._user_words), target=target,
                                        signature=call_signature(acting_input(tool_input)), earlier=earlier,
                                        files_read=outbound,
                                        **({"named_app": self._request_app_named}
                                           if getattr(self, "_request_app_named", None) else {}))
        if self.authorizer.stopped and verdict is not None and not verdict.allowed:
            self._denial_stop = self.authorizer.stopped
        return verdict

    def _denial_text(self, verdict: Any) -> str:
        """What the brain is told when a call is not run (framed as Yuki's own check, never the user)."""
        if verdict.source == "policy":
            return (f"{JUDGE_LABEL}: not run: {verdict.why}. Tell them what you would do and let them say go, or do "
                    f"it themselves.")
        if verdict.source == "app_off":
            return (f"{JUDGE_LABEL}: not run, and it will not run: {verdict.why} Do not click, type or press keys in "
                    f"that app in this request; tell them plainly, or do what they asked another way.")
        if verdict.source == "error":
            return (f"{JUDGE_LABEL}: not run: {verdict.why} ({verdict.error}). Ask the user one plain question before "
                    f"this step.")
        ask = f' (for example: "{verdict.question}")' if verdict.question else ""
        return (f"{JUDGE_LABEL}: not run: the user did not ask for this ({verdict.why}). Do what they asked another "
                f"way, or ask them one plain question with ask_user{ask}. Do not make this call again unless they "
                f"say yes.")

    def _consult_due(self, signal: str) -> bool:
        if not getattr(self.settings, "consult", True):
            return False
        if len(self._consults) >= int(getattr(self.settings, "consult_max", 2)):
            return False
        if signal == "send" and (self._send_consulted or not getattr(self.settings, "consult_before_send", True)):
            return False
        if signal == "close_call" and self._close_call_consulted:
            return False
        return True

    def _consult(self, signal: str, detail: str, *, pending: str | None = None) -> Any:
        """One side call (revamp D, S3) over a summary written by code; the actor's own profile is untouched.
        Returns the :class:`yuki.agent.consult.Advice` (``None`` when not due). Its cost joins this request's."""
        if not self._consult_due(signal):
            return None
        if signal == "send":
            self._send_consulted = True
        if signal == "close_call":
            self._close_call_consulted = True
        windows = [w for w in (self._overview_payload or {}).get("windows") or []
                   if isinstance(w, dict)] if isinstance(self._overview_payload, dict) else []
        summary = second_look.summary_text(
            words=self._this_query or memory_query_of(self._request_text or ""), signal=signal, detail=detail,
            frame=self._frame.text if self._frame is not None else None, steps=self._trace, windows=windows,
            view=self._last_view_text, pending=pending)
        model = str(getattr(self.settings, "consult_model", "") or self.settings.model)
        params = second_look.consult_request(summary, model=model,
                                             effort=str(getattr(self.settings, "consult_effort", "medium") or ""))
        try:
            client = self.client
        except Exception as exc:  # noqa: BLE001
            advice = second_look.Advice(signal=signal, error=f"{type(exc).__name__}: {exc}")
        else:
            advice = second_look.run_consult(client, params, signal=signal,
                                             timeout_s=float(getattr(self.settings, "consult_timeout_s", 30.0)))
        if advice.usage:
            # the model that answered (its returned amount when it gave one); an unknown price leaves the total unknown
            cost = advice.returned_usd if advice.returned_usd is not None else \
                self.settings.estimate_cost(advice.model or model, advice.usage)
            if cost is not None:
                advice.cost_usd = round(float(cost), 6)
                self._cost_usd += float(cost)
            elif advice.model is not None:   # routed, price not known: the request's total is unknown, never smaller
                self._unpriced.add(advice.model)
            for key in TOKEN_KEYS:
                self._tokens[key] += int(advice.usage.get(key) or 0)
        info = {**advice.facts(), "detail": detail[:200], "at_call": self._model_calls}
        self._consults.append(info)
        self.logger.log("consult", **info, advice=advice.advice)
        return advice

    def _confirm_send(self, target: dict[str, Any], text: str, since: float) -> dict[str, Any]:
        """Wait (about ``Settings.send_confirm_s``) for memory's capture of the chat to show the message just sent
        (revamp D, S2). Without memory the send is "not confirmed" (said plainly), never assumed."""
        app = target.get("app")
        hwnd = target.get("hwnd")

        def lookup(t0: float) -> list[dict[str, Any]]:
            fetch = getattr(self.memory, "call_method", None)
            if not callable(fetch) or not getattr(self.memory, "installed", False):
                return []
            return fetch("sent_messages", app, t0 - 1.0, timeout_s=0.5) or []

        def refresh() -> None:
            try:
                from yuki.memory.now import shared_client

                if isinstance(getattr(self.dispatcher, "backend", None), _real_backend_type()):
                    client = shared_client()
                    if client is not None:
                        client.refresh(hwnd, wait_s=0.05)
            except Exception:  # noqa: BLE001
                pass

        result = finish_gate.confirm_send(lookup, text, since=since,
                                          within_s=float(getattr(self.settings, "send_confirm_s", 4.0)),
                                          refresh=refresh, cancelled=self._cancelled.is_set)
        self.logger.log("send_confirmation", app=app, hwnd=hwnd, chars=len(text or ""), **result)
        return result

    def _situation(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """N0 (report 47): one situation check (:mod:`yuki.agent.situation`): the target window read again with a larger
        budget for its latest content, memory's recent history of the person or thing, the user's own recent messages in
        that app most like what is about to be sent; the evidence fenced as data read from outside, then the question a
        good assistant asks itself. Its record (``payload["record"]``) is kept for the gate by the caller, on the loop's
        thread, only when the result reaches the model (this may run on an abandoned reader thread). Never raises."""
        started = time.perf_counter()
        backend = getattr(self.dispatcher, "backend", None)
        windows = {int(w["hwnd"]): w for w in (self._overview_payload or {}).get("windows") or []
                   if isinstance(w, dict) and isinstance(w.get("hwnd"), int)} \
            if isinstance(self._overview_payload, dict) else {}

        def read_tree(hwnd: int) -> Any:
            get = getattr(backend, "get_window_tree", None)
            if not callable(get):
                return None
            from yuki.agent.tools import _plain

            return _plain(get(int(hwnd), max_elements=situation_check.READ_CAP,
                              timeout_s=situation_check.READ_TIMEOUT_S))

        def process_of(hwnd: int) -> tuple[str, str]:
            w = windows.get(int(hwnd)) or self._window_of(int(hwnd)) or {}
            return str(w.get("process_name") or ""), str(w.get("title") or "")

        installed = bool(getattr(self.memory, "installed", False))
        fetch = getattr(self.memory, "call_method", None) if installed else None

        def recall(query: str, since_epoch: float) -> str:
            call = getattr(self.memory, "call", None)
            if not callable(call):
                return ""
            from yuki.agent.memory import format_recall

            since = datetime.fromtimestamp(since_epoch)
            rows = call("situation_recall", lambda c: c.recall(query, since=since, limit=8),
                        timeout_s=situation_check.MEMORY_TIMEOUT_S)
            facts = [r for r in (rows or []) if isinstance(r, dict)]
            return format_recall(facts, query=query, since=since) if facts else ""

        sit = situation_check.gather(tool_input, read_tree=read_tree, process_of=process_of,
                                     memory_call=fetch if callable(fetch) else None,
                                     recall=recall if installed else None)
        record = situation_check.CheckRecord(hwnd=sit.hwnd, at=time.time(), about=sit.about)
        self.logger.log("situation_check", **sit.facts_dict(), call=self._model_calls)
        evidence = wrap(sit.evidence_text(), source="the target window and memory (Yuki's situation check)",
                        nonce=self._nonce)
        text = f"{situation_check.CHECK_LABEL}: {sit.summary()}.\n{evidence}\n\n{situation_check.QUESTION}"
        return ToolOutcome(name=situation_check.TOOL_NAME, ok=True, summary=sit.summary(),
                           content=[{"type": "text", "text": text}],
                           payload={"situation": sit.facts_dict(), "record": record,
                                    "shown": sit.shown(_app_display(sit.process))},
                           elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    def _pending_send(self, name: str, tool_input: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        """appPT: what the send countdown shows for a send about to run: who (the person this request's situation check
        was about, else the task frame's person), where (their route in the target's app, else the app), the exact
        text (the call's own, else what Yuki typed into that window and has not sent), and the alternatives the
        resolver had (the frame's other people; the person's other routes). Facts of the request only; never raises."""
        hwnd = target.get("hwnd")
        text, typed = self._send_text(name, tool_input, target)
        # review F1: the chat the target window itself shows (its title, as the app writes it, read again now), so a
        # wrong chat in front is on the card whatever the brain believes it is sending to
        info: dict[str, Any] = {"who": "", "where": "", "app": _app_display(target.get("app")), "text": text,
                                "typed": typed, "who_alts": (), "where_alts": (), "from_memory": False,
                                "window": self._live_title(hwnd, target.get("title")), "route_apps": {}}
        try:
            checks = [c for c in getattr(self, "_situation_checks", None) or [] if c.about]
            same = [c for c in checks if hwnd is not None and c.hwnd == hwnd] or checks
            who = " ".join(str(same[-1].about).split()) if same else ""
            frame = getattr(self, "_frame", None)
            persons = [p for p in (getattr(frame, "persons", None) or []) if isinstance(p, dict) and p.get("name")]
            person = next((p for p in persons if who and str(p["name"]).casefold() == who.casefold()), None)
            if person is None and not who and persons:
                person = persons[0]
            if person is not None:
                who = str(person["name"])
            info["who"] = who
            info["from_memory"] = person is not None
            routes = [r for r in (person or {}).get("routes") or [] if isinstance(r, dict) and r.get("label")]
            key = _app_key(target.get("app"))
            mine = next((r for r in routes if key and _app_key(r.get("app")) == key), None)
            info["where"] = str(mine["label"]) if mine else info["app"]
            info["where_alts"] = tuple(dict.fromkeys(str(r["label"]) for r in routes if r is not mine
                                                     and str(r["label"]) != info["where"]))[:2]
            # each route's own app, so a "where" swap names the app it switches to (review F6)
            info["route_apps"] = {str(r["label"]): _app_display(r.get("app")) or str(r["label"]) for r in routes}
            others = [str(p["name"]) for p in persons if p is not person]
            others += [str(getattr(c, "label", "") or "") for c in (getattr(frame, "people", None) or [])]
            info["who_alts"] = tuple(dict.fromkeys(o for o in others if o and o.casefold() != who.casefold()))[:2]
        except Exception as exc:  # noqa: BLE001 - the countdown still shows the text and the app
            self.logger.log("send_countdown_info_failed", error=f"{type(exc).__name__}: {exc}")
        return info

    def _send_text(self, name: str, tool_input: dict[str, Any], target: dict[str, Any]) -> tuple[str, bool]:
        """appPT: the exact text a send call puts out, and whether it is already in the box: the call's own text after
        what Yuki typed into that window and has not sent (unless the call clears the box first), or, for a key or a
        click, what Yuki typed there. Plumbing on the request's own record; never raises."""
        hwnd = target.get("hwnd")
        held = str(getattr(self, "_typed", {}).get(int(hwnd or 0), "") or "")
        if name == "type_text":
            typed = bool(held.strip()) and not tool_input.get("clear")
            return ((held if typed else "") + str(tool_input.get("text") or "")).strip(), typed
        return held.strip(), bool(held.strip())

    def _text_approved(self, text: str) -> bool:
        """appPT (review F7): whether the user already gave the go-ahead on this exact text in this request: they wrote
        it in an answer to Yuki's question ("message Sam" -> "what should I say?" -> the text). Measured on characters
        (casefolded, whitespace squashed: the whole text inside their answer, or :data:`ANSWERED_TEXT_SIMILAR` alike),
        never on words; an answer that only picked who or where is no go-ahead on the text."""
        import difflib

        def squash(s: str) -> str:
            return " ".join(str(s or "").split()).casefold()

        x = squash(text)
        if not x:
            return False
        for _question, answer in getattr(self, "_answered", None) or []:
            y = squash(answer)
            if y and (x in y or difflib.SequenceMatcher(None, x, y).ratio() >= ANSWERED_TEXT_SIMILAR):
                return True
        return False

    def _live_title(self, hwnd: Any, fallback: Any = "") -> str:
        """The target window's title read again now (on the real desktop), else the overview's: the chat it shows, as
        the app writes it. Whitespace squashed; never raises."""
        title = str(fallback or "")
        try:
            if hwnd and isinstance(getattr(self.dispatcher, "backend", None), _real_backend_type()):
                from yuki.perception.windows import window_info

                info = window_info(int(hwnd))
                if info is not None and info.title:
                    title = str(info.title)
        except Exception:  # noqa: BLE001 - the overview's title stands
            pass
        return " ".join(title.split())

    def _send_countdown(self, name: str, tool_input: dict[str, Any], target: dict[str, Any]) -> Iterator[AgentEvent]:
        """appPT: the send countdown. Yields :class:`SendPending` ``counting`` (and, for a spoken request, one short
        line said in Yuki's voice: "Sending to Sam"), waits up to :attr:`send_countdown_s` for a cancel (Stop, Esc, the
        stop hotkey, "stop" said aloud) or a chip tap (:meth:`send_choice`), then yields how it ended. Returns
        (``yield from``) ``going``, ``stopped``, or the swap's note for the brain (the swap also goes in as the user's
        words, so the judge reads it). Waiting here is waiting for the user: it is counted as such."""
        info = self._pending_send(name, tool_input, target)
        seconds = float(self.send_countdown_s)
        with self._send_lock:
            self._send_open, self._send_choice = info, None
        self._send_wake.clear()
        event = {k: info[k] for k in ("who", "where", "app", "text", "who_alts", "where_alts", "typed", "from_memory",
                                      "window")}
        self.logger.log("send_countdown", state="counting", tool=name, seconds=seconds, chars=len(info["text"]),
                        who_alts=len(info["who_alts"]), where_alts=len(info["where_alts"]), typed=info["typed"])
        if getattr(self, "_request_voice", False) and info["who"]:
            yield Say(f"Sending to {info['who']}")
        yield SendPending("counting", seconds=seconds, **event)
        started = time.monotonic()
        deadline = started + seconds
        choice: tuple[str, str] | None = None
        while True:
            if self._cancelled.is_set():
                state = "stopped"
                break
            with self._send_lock:
                choice, self._send_choice = self._send_choice, None
            if choice is not None:
                state = "swapped"
                break
            left = deadline - time.monotonic()
            if left <= 0:
                state = "going"
                break
            self._send_wake.wait(left)
            self._send_wake.clear()
        with self._send_lock:
            self._send_open = None
        self._pause_quiet_until = time.monotonic() + RESUME_GRACE_S   # appTakeover: a hand on the card moves on
        self._waited_s += time.monotonic() - started
        if state == "swapped" and choice is not None:
            kind, value = choice
            who, where, app = info["who"] or "them", info["where"] or info["app"], info["app"]
            if kind == "who":
                # the new person in the same app, never on the old person's own chat (review F2: "Discord DM @sam_99"
                # is Sam's route, not Maya's)
                words = f"Not {who}: send it to {value} instead" + (f", on {app}" if app else "") + "."
                avoid = f"Do not send it to {who}."
                event = {**event, "who": value, "who_alts": tuple(x for x in (who, *info["who_alts"])
                                                                   if x and x != value)[:2]}
            else:
                words = f"Send it to {who} on {value} instead of {where}."
                avoid = f"Do not send it on {where}."
                # the app the chosen route is in, so the switching card names it (review F6)
                event = {**event, "where": value, "app": (info.get("route_apps") or {}).get(value) or value,
                         "where_alts": tuple(x for x in (where, *info["where_alts"]) if x and x != value)[:2]}
            event["window"] = ""   # the window it was going into is not where it goes now
            left = ""
            hwnd = target.get("hwnd")
            if info["typed"] and hwnd is not None:
                # review F3: the text sat in the first chat's box; that box is no longer the target, so it is not
                # the next send's text (a click to the new chat is no send), and the brain hears it is still there
                getattr(self, "_typed", {}).pop(int(hwnd), None)
                left = (" The text you typed is still in the message box of the chat you were in, not sent: clear "
                        "that box before you leave that chat (it must not stay there or go to them), then type the "
                        "text in the new chat.")
            self.steer(f"{words} (tapped on the send countdown)")
            note = (f"{CHECK_LABEL}: not run: in the send countdown the user tapped to change "
                    f"{'who it goes to' if kind == 'who' else 'where it goes'}: {words} Their words come next. Send "
                    f"the same text there: go to that chat first (another window needs its own check), then send. "
                    f"{avoid}{left}")
            self.logger.log("send_countdown", state="swapped", swap=kind, after_ms=round((time.monotonic() - started)
                                                                                          * 1000))
            yield SendPending("swapped", seconds=seconds, **event)
            return note
        if state == "stopped":
            self._send_stopped = bool(info["typed"])   # the run's cancel line says plainly what did not happen
        self.logger.log("send_countdown", state=state, after_ms=round((time.monotonic() - started) * 1000))
        yield SendPending(state, seconds=seconds, **event)
        return state

    def _dispatch_watching(self, name: str, tool_input: dict[str, Any]) -> Iterator[AgentEvent]:
        """Run an action whose hands may wait for the user (S7) on its own thread, saying the gate's line as a ``Say``
        the moment it waits. The action always finishes (never abandoned). Returns (``yield from``) its outcome."""
        notices = getattr(self.dispatcher, "notices", None)
        if notices is None or getattr(self.dispatcher, "input_gate", None) is None:
            return self._dispatch(name, tool_input)
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["out"] = self._dispatch(name, tool_input)
            except BaseException as exc:  # noqa: BLE001 - handed back below
                box["exc"] = exc
            finally:
                done.set()

        threading.Thread(target=work, name=f"yuki-act-{name}", daemon=True).start()
        while not done.wait(self.ABORT_POLL_S):
            yield from self._drain_notices()
        yield from self._drain_notices()
        if "exc" in box:
            raise box["exc"]
        return box["out"]

    def _drain_notices(self) -> Iterator[AgentEvent]:
        notices = getattr(self.dispatcher, "notices", None)
        while notices is not None:
            try:
                text = notices.get_nowait()
            except queue.Empty:
                return
            self.logger.log("input_yield", text=text)
            yield Say(text)

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

    def _request_params(self, *, warmup: bool = False,
                        profile: tuple[str, bool, bool] | None = None) -> dict[str, Any]:
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
        # The prompt in layers, most stable first (prompt.system_blocks): core (+ this lane's
        # framing), profile, rules. The warm-up sends the core alone: its breakpoint sits at
        # the same position, so the entry it writes is the one real requests read.
        # A spoken request (or the warm-up of the voice profile) carries the voice layer after the core (revamp D, S6).
        voice = (profile is not None) if warmup else bool(getattr(self, "_request_voice", False))
        system = system_blocks(
            extra=self.extra_instructions,
            profile=None if warmup else self._profile_layer,
            rules=None if warmup else self._rules_layer,
            voice=voice,
        )
        tools = tool_params(
            # No breakpoint of its own: tools come first in the cached prefix, so the core
            # layer's breakpoint already covers them (and frees a slot for the rules layer).
            cacheable=False,
            names=self.tool_names,
            # The dispatcher's policy is the one that gates dispatch, so the
            # block the model sees must agree with it (never -> no
            # take_screenshot at all; the dispatcher's refusal is the backstop).
            screenshot_policy=getattr(
                self.dispatcher, "screenshot_policy", self.settings.screenshot_policy
            ),
            memory=self.memory.installed,
            lane_tools=READER_LANE_TOOLS if getattr(self, "reader", False) else (),
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
        # A spoken request's step tools carry say_now (v3.14b; optional since revamp D). The warm-up with a profile is
        # the voice profile's (warm_cache(effort=, thinking=) is only called for it), so it writes the same tools.
        if voice:
            from yuki.agent.tools import with_step_lines

            tools = with_step_lines(tools)
        # Effort and thinking per step (v3.14): a spoken request's fast first pass until a measured signal escalates
        # it (``_escalate``). The warm-up writes the entry of the profile it is asked for (``warm_cache``): on
        # Sonnet 5 an effort change is a different cached prefix from the tools on (measured 2026-09-29).
        if warmup:
            effort, thinking, omit = profile or (self.settings.effort, True, False)
        else:
            effort, thinking, omit = self._step_config()
        params = {
            "model": (None if warmup else self._request_model) or self.settings.model,
            "max_tokens": 1 if warmup else self.settings.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "thinking": ({"type": "disabled"} if not thinking
                         else {"type": "adaptive", "display": self.settings.thinking_display}),
            "output_config": {"effort": effort},
        }
        if omit:
            del params["output_config"]
        return params

    def _request(self, turn: _Turn) -> Iterator[AgentEvent]:
        """Send one request, running tools as their blocks complete; return the response.

        A generator: it yields :class:`Thinking` as each thinking block
        completes and the ``tool_call``/``tool_result`` events of every tool it
        runs early, and returns the complete final message (``yield from``).

        With ``settings.stream`` (the default) the request goes through
        ``messages.stream``. A ``tool_use`` block whose input is complete is run
        as soon as the stream shows it was not cut off -- the next block
        starting, or the stop reason arriving and not being ``max_tokens`` or
        ``refusal`` -- if :meth:`_can_run_early` allows it; one token of delay
        buys never running a truncated input. While a tool runs the stream is
        simply not read; the rest of the response waits in the connection.
        The stored response is the SDK's ``get_final_message()``, the same
        accumulated message a non-streamed call returns, thinking blocks and
        signatures untouched. Without ``stream``, ``messages.create`` is used
        and every tool runs after the response, as before.

        Logs the request and the response in full. ``llm_response`` also has
        ``first_tool_ms`` (request start to the first tool dispatched while
        streaming; ``None`` when none was), ``tools_in_stream`` and
        ``in_stream_tool_ms``; its ``latency_ms`` is the whole request,
        in-stream tool time included.
        """
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
            output_config=params.get("output_config"),
            stream=self.settings.stream,
        )
        started = time.perf_counter()
        in_stream_s = 0.0
        first_token_at: float | None = None
        try:
            if self.settings.stream:
                with self.client.messages.stream(**params) as stream:
                    seen: list[Any] = []
                    pending: Any = None
                    live: dict[str, Any] = {}   # the reply / thinking so far, for the chat (stream_partial)
                    for event in stream:
                        if self._cancelled.is_set() and not turn.handled and pending is None:
                            raise _StreamCancelled()
                        kind = getattr(event, "type", None)
                        if first_token_at is None and kind in ("content_block_start", "content_block_delta"):
                            first_token_at = time.perf_counter()   # revamp D: the first token, measured
                        part = stream_partial(event, live)
                        if part is not None and part.what == "now":
                            part = Partial("now", _now_line(part.text)) if _now_line(part.text) else None
                        if part is not None:
                            yield part
                        if pending is not None and kind in (
                            "content_block_start",
                            "message_delta",
                            "message_stop",
                        ):
                            block, pending = pending, None
                            if _block_confirmed(event) and self._can_run_early(turn, block, seen):
                                ran = time.perf_counter()
                                yield from self._run_one(turn, block, early=True)
                                in_stream_s += time.perf_counter() - ran
                        if kind == "content_block_stop":
                            block = getattr(event, "content_block", None)
                            block_type = getattr(block, "type", None)
                            if block_type == "thinking":
                                yield from self._emit_thinking(block)
                            elif block_type == "tool_use":
                                seen.append(block)
                                pending = block
                    response = stream.get_final_message()
            else:
                response = self.client.messages.create(**params)
                for block in getattr(response, "content", []) or []:
                    if getattr(block, "type", None) == "thinking":
                        yield from self._emit_thinking(block)
        except _StreamCancelled:
            latency_ms = (time.perf_counter() - started) * 1000
            self._model_s += max(latency_ms / 1000 - in_stream_s, 0.0)
            self.logger.log("stream_cancelled", reason=self.cancel_reason, after_ms=round(latency_ms, 1))
            raise
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            self._model_s += max(latency_ms / 1000 - in_stream_s, 0.0)
            self.logger.error(
                f"request failed after {latency_ms:.0f}ms: {type(exc).__name__}: {exc}", exc=exc
            )
            if turn.results:
                self._note_orphaned_tools(turn)
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        self._model_s += max(latency_ms / 1000 - in_stream_s, 0.0)
        used_model = self._used_model(params["model"])
        if used_model != params["model"]:
            self.logger.log("model_used", requested=params["model"], used=used_model)
        cost = self._account_response(used_model, getattr(response, "usage", None))
        if cost is not None and isinstance(getattr(self, "_call_costs", None), dict):
            self._call_costs[self._model_calls] = cost   # shared out over this call's steps in the run trace
        try:   # revamp D: per-call facts for the request summary (output tokens per action call, the prefix, TTFT)
            used = usage_tokens(getattr(response, "usage", None))
            self._call_stats.append({
                "out": used["output_tokens"], "in": used["input_tokens"], "cache_read": used["cache_read_tokens"],
                "cache_write": used["cache_write_tokens"],
                "tools": [str(getattr(b, "name", "")) for b in getattr(response, "content", []) or []
                          if getattr(b, "type", None) == "tool_use"],
                "first_token_ms": None if first_token_at is None else round((first_token_at - started) * 1000, 1),
                "ms": round(latency_ms, 1)})
        except Exception:  # noqa: BLE001 - accounting never breaks a request
            pass
        # The request went through, so its cache entry exists: the next prune
        # measures "before the breakpoint" against this one.
        self.context.commit_breakpoint()
        self._log_llm_response(
            response,
            latency_ms=latency_ms,
            first_tool_ms=(
                None
                if turn.first_early_at is None
                else round((turn.first_early_at - started) * 1000, 1)
            ),
            tools_in_stream=turn.early,
            in_stream_tool_ms=round(in_stream_s * 1000, 1),
            first_token_ms=None if first_token_at is None else round((first_token_at - started) * 1000, 1),
        )
        self._first_token_ms.append(None if first_token_at is None else round((first_token_at - started) * 1000, 1))
        return response

    def _emit_thinking(self, block: Any) -> Iterator[AgentEvent]:
        """Log and yield one thinking block, if it has any text."""
        text = getattr(block, "thinking", "") or ""
        if text.strip():
            self.logger.thinking(text)
            yield Thinking(text)

    def _log_llm_response(self, response: Any, *, latency_ms: float, **extra: Any) -> None:
        """Write the ``llm_response`` record, with this loop's extra timing fields.

        The same record :meth:`SessionLogger.llm_response` writes (full content,
        stop reason and details, usage, latency; usage folded into the running
        total; one console line), plus ``extra`` -- which that method has no
        parameter for.
        """
        log = self.logger
        usage = getattr(response, "usage", None)
        stop_reason = getattr(response, "stop_reason", None)
        log.usage.add(usage, latency_ms=latency_ms)
        dereference = getattr(log, "_dereference_images", None)
        content = _as_plain(getattr(response, "content", []))
        log.log(
            "llm_response",
            content=dereference(content) if callable(dereference) else content,
            stop_reason=stop_reason,
            stop_details=_as_plain(getattr(response, "stop_details", None)),
            usage=_as_plain(usage),
            latency_ms=round(latency_ms, 1),
            **extra,
        )
        plain = _as_plain(usage)
        if not isinstance(plain, dict):
            plain = {}
        printer = getattr(log, "_print", None)
        if callable(printer):
            first = extra.get("first_tool_ms")
            printer(
                f"[dim]<- {stop_reason} {latency_ms / 1000:.1f}s "
                f"in={plain.get('input_tokens', 0)} "
                f"cached={plain.get('cache_read_input_tokens', 0)} "
                f"out={plain.get('output_tokens', 0)}"
                + (f" first_tool={first / 1000:.1f}s" if first is not None else "")
                + "[/dim]"
            )

    def _request_view(self, hwnd: int, prebuilt: Any = None) -> tuple[str, str] | None:
        """The window ``hwnd`` read as the request comes in (one read, element ids usable), or ``None``.

        ``(framed text for the request, the fenced tree alone)``: the first is placed with the request, the
        second is a ``look_at_window`` result (:meth:`_add_view_call`). Logged as a ``perception`` record of
        kind ``request_view``; its time counts as ``auto_view_s``. ``prebuilt``: the read a request pack made
        ahead (the dispatcher already holds its element ids). Never raises.
        """
        started = time.perf_counter()
        try:
            view = prebuilt if prebuilt is not None else self.dispatcher.window_view(hwnd, settle=False)
        except Exception as exc:  # the window may have gone: the request goes on without it
            self.logger.log("request_view_skipped", hwnd=hwnd, error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            self._auto_view_s += time.perf_counter() - started
        fenced = wrap(view.text, source=f"the window {hwnd} (its on-screen text)", nonce=self._nonce)
        text = (
            f"The window in front when they asked, read for you as the request came in "
            f"({view.elapsed_ms:.0f} ms): its elements are ready to act on, so there is no need to "
            f"look at it first.\n" + fenced
        )
        self.logger.perception(
            "request_view", size_chars=len(text), elapsed_ms=(time.perf_counter() - started) * 1000,
            payload={"hwnd": hwnd, "summary": view.summary},
        )
        return text, fenced

    def _add_view_call(self, hwnd: int, fenced: str, overview: str) -> None:
        """Put the window read for the request in as a ``look_at_window`` call already made, with its result."""
        tool_id = f"toolu_yukiview{self._requests}"
        self.context.add_assistant([{"type": "tool_use", "id": tool_id, "name": "look_at_window",
                                     "input": {"hwnd": hwnd}}])
        self.context.add_tool_results(
            [self._result_block(tool_id, [{"type": "text", "text": (
                "Read for you as the request came in (the window in front): its elements are ready to act on.\n"
                + fenced)}])],
            overview, tool_names={tool_id: "look_at_window"})
        self.logger.log("request_view_call", hwnd=hwnd, tool_use_id=tool_id)

    # -- the view after actions ----------------------------------------------

    def _auto_view(self, turn: _Turn) -> tuple[int, str] | tuple[int, str, bool] | None:
        """Read the window this turn's actions acted on, for the next turn.

        Attached only when at least one state-changing action succeeded and
        nothing in the turn failed or was skipped (a failure's own text is what
        the model needs then), and not when an explicit ``look_at_window`` of
        that same window already ran after the last action. The window is the
        last action's ``expect_hwnd``, else the window it reported (the one it
        focused, launched or opened, or the foreground after the input), else
        the foreground window. It is read once it has settled
        (:meth:`Dispatcher.window_view`), or read once when ``launch_app`` /
        ``open_url`` already reported its content ready.

        Returns:
            ``(hwnd, text)`` for :meth:`ContextManager.add_tool_results`, or
            ``None``. Every attempt is logged: a ``perception`` record of kind
            ``auto_view`` when attached, ``auto_view_skipped`` otherwise.
        """
        if not turn.actions:
            return None
        if turn.failed or self._cancelled.is_set():
            self.logger.log(
                "auto_view_skipped",
                reason="cancelled" if self._cancelled.is_set() else "a tool in this turn failed",
            )
            return None
        name, tool_input, outcome = turn.actions[-1]
        details = outcome.payload.get("details") if isinstance(outcome.payload, dict) else None
        details = details if isinstance(details, dict) else {}
        hwnd, source = _view_target(name, tool_input, details)
        started = time.perf_counter()
        try:
            if hwnd is None:
                hwnd, source = self.dispatcher.foreground_hwnd(), "the foreground window"
            if hwnd is None:
                self.logger.log("auto_view_skipped", reason="no window to read", after=name)
                return None
            if hwnd in turn.looked:
                self.logger.log(
                    "auto_view_skipped",
                    reason="look_at_window of this window already ran after the action",
                    hwnd=hwnd,
                )
                return None
            content_ready = name in ("launch_app", "open_url") and details.get("content_ready") is True
            try:
                # the settle poll is a read: a cancel or the user's words stop the wait (v3.14)
                # Only what changed since the window's last read, when it fits (v3.15): the whole tree stays
                # one look_at_window away, and every id the model holds from that read still holds.
                near = tool_input.get("element")
                near = near if isinstance(near, int) and not isinstance(near, bool) else None
                view, abandoned = self._race("auto_view", lambda: self.dispatcher.window_view(
                    hwnd, settle=not content_ready, changes=True, near=near))
                if abandoned is not None:
                    return None
            except Exception as exc:
                # The window acted on may have closed (Escape on a dialog, say):
                # show what is in front instead, and say so.
                foreground = self.dispatcher.foreground_hwnd()
                if foreground is None or foreground == hwnd:
                    raise
                source = (
                    f"the foreground window; hwnd {hwnd}, the window acted on, could not be "
                    f"read ({type(exc).__name__}: {exc})"
                )
                hwnd = foreground
                view = self.dispatcher.window_view(hwnd)
        except Exception as exc:
            self.logger.error(f"auto view failed: {type(exc).__name__}: {exc}", exc=exc)
            return None
        finally:
            self._auto_view_s += time.perf_counter() - started
        elapsed_ms = (time.perf_counter() - started) * 1000
        reads = f"{view.reads} read{'' if view.reads == 1 else 's'}"
        changes = bool(getattr(view, "changes", False))
        text = (
            f"Window after your actions: {source}, read automatically after {name} "
            f"({reads} in {view.elapsed_ms:.0f} ms; {view.reason})"
            + ("; what changed on it" if changes else "") + ".\n"
            + wrap(view.text, source=f"the window {hwnd} (its on-screen text)", nonce=self._nonce)
        )
        self.logger.perception(
            "auto_view",
            size_chars=len(text),
            elapsed_ms=elapsed_ms,
            payload={
                **view.payload,
                "auto_view": {
                    "hwnd": hwnd,
                    "source": source,
                    "after_tool": name,
                    "reads": view.reads,
                    "settled": view.settled,
                    "reason": view.reason,
                    "content_ready_reported": name in ("launch_app", "open_url")
                    and details.get("content_ready") is True,
                    "summary": view.summary,
                    "changes_only": changes,
                    "full_chars": getattr(view, "full_chars", 0),
                },
            },
        )
        self._evidence[hwnd] = "looked"   # the next call sees it read after the actions (v3.14)
        self._last_view_text = view.text   # revamp D: the window acted on, for a second look's summary
        return (hwnd, text, True) if changes else (hwnd, text)


def _close_call(frame: Any) -> bool:
    """Whether the task frame measured a close call (two people, two things or two routes within their margin)."""
    if frame is None:
        return False
    if getattr(frame, "close_people", False) or getattr(frame, "close_things", False):
        return True
    return any(bool(p.get("close")) and len(p.get("routes") or []) > 1
               for p in getattr(frame, "persons", None) or [] if isinstance(p, dict))


def _real_backend_type() -> type:
    from yuki.agent.tools import _LazyRealBackend

    return _LazyRealBackend


def _practice_info(practice: Any) -> dict[str, Any] | None:
    """Content-free facts of what practice memory gave a request (ids, cosines, the apps in play, how long it took)."""
    if not isinstance(practice, dict):
        return None
    return {"ways": [(w.get("id"), w.get("cosine")) for w in practice.get("ways") or [] if isinstance(w, dict)],
            "pitfalls": [(p.get("id"), p.get("cosine")) for p in practice.get("pitfalls") or [] if isinstance(p, dict)],
            "apps": list(practice.get("apps") or []), "named": list(practice.get("named") or []),
            "measured": practice.get("measured"), "ms": practice.get("ms")}


def _split_personality(portrait: str | None) -> tuple[str, str | None]:
    """The portrait without its Personality section, and the section (memory is optional)."""
    try:
        from yuki.memory.personality import split_personality
    except Exception:  # noqa: BLE001 - no memory package: the whole page stays background
        return portrait or "", None
    return split_personality(portrait)


def _app_key(name: Any) -> str:
    """An app's name as a comparable key ("Spotify.exe", "spotify" -> "spotify"): plumbing."""
    text = str(name or "").casefold().strip()
    if text.endswith(".exe"):
        text = text[:-4]
    return "".join(ch for ch in text if ch.isalnum())


def _as_hwnd(value: Any) -> int | None:
    """A window handle from a tool input or result field, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        hwnd = int(value)
    except (TypeError, ValueError):
        return None
    return hwnd or None


def _view_target(
    name: str, tool_input: dict[str, Any], details: dict[str, Any]
) -> tuple[int | None, str]:
    """``(hwnd, how it was chosen)`` for the view after an action.

    The guard the model put on the action, else the window the action says it
    acted on, else ``None`` (the caller then reads the foreground window).
    """
    hwnd = _as_hwnd(tool_input.get("expect_hwnd"))
    if hwnd is not None:
        return hwnd, "the window the action was aimed at (expect_hwnd)"
    if name in ("focus_window", "window_action", "tab"):
        hwnd = _as_hwnd(tool_input.get("hwnd"))
        if hwnd is not None:
            return hwnd, "the window you focused" if name == "focus_window" else "the window the action named"
    hwnd = _as_hwnd(details.get("hwnd"))
    if hwnd is not None:
        return hwnd, "the window the action reported"
    hwnd = _as_hwnd(details.get("foreground_hwnd"))
    if hwnd is not None:
        return hwnd, "the window in front after the action"
    return None, "the foreground window"


def _foreground_app(overview: Any) -> str | None:
    """Process name of the app the user is in, from a desktop overview's plain data.

    The foreground window when it is a listed window; otherwise (Yuki's own
    overlay is in front, and it is never listed) the listed window at the top
    of the Z-order that is not minimised.
    """
    if not isinstance(overview, dict):
        return None
    windows = [w for w in overview.get("windows") or [] if isinstance(w, dict)]
    foreground = overview.get("foreground_hwnd")
    chosen = next((w for w in windows if foreground and w.get("hwnd") == foreground), None)
    if chosen is None:
        chosen = next((w for w in windows if not w.get("is_minimized")), None)
    name = str((chosen or {}).get("process_name") or "").strip()
    return name or None


def _origin_text(
    overview: Any, facts: dict[str, Any], *, origin_hwnd: int | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> str:
    """The request-origin line: what was in front, media, microphone and camera.

    Facts only, in plain words; nothing here decides what the model does with
    them. The window comes from the overview's plain data (the hwnd the model
    can pass to ``focus_window``); ``facts`` is
    :func:`yuki.perception.system.activity_facts` (or a stub's equivalent).
    ``calls``: memory's calls going on now (``MemoryClient.current_calls``); a
    microphone user that is one of them reads "on a Discord call since 13:20
    (8h16m)" (identity: the holder's image name against the call's process).
    """
    overview = overview if isinstance(overview, dict) else {}
    windows = [w for w in overview.get("windows") or [] if isinstance(w, dict)]
    by_hwnd = {_as_hwnd(w.get("hwnd")): w for w in windows}
    foreground = facts.get("foreground") if isinstance(facts.get("foreground"), dict) else {}
    errors = facts.get("errors") if isinstance(facts.get("errors"), dict) else {}

    def window(w: dict[str, Any]) -> str:
        text = f'[{w.get("hwnd")}] {w.get("process_name") or "?"} "{w.get("title") or ""}"'
        return text + (" (minimised)" if w.get("is_minimized") else "")

    front_hwnd = _as_hwnd(overview.get("foreground_hwnd")) or _as_hwnd(foreground.get("hwnd"))
    hint = _as_hwnd(origin_hwnd)
    if hint is not None and hint in by_hwnd:
        front = f"in front, before they called you up, was {window(by_hwnd[hint])}"
    elif hint is not None:
        front = f"in front, before they called you up, was no app window (hwnd {hint})"
    elif front_hwnd is not None and front_hwnd in by_hwnd:
        front = f"in front was {window(by_hwnd[front_hwnd])}"
    elif foreground.get("own_process"):
        beneath = next((w for w in windows if not w.get("is_minimized")), None)
        front = "your own window was in front (they were typing to you)"
        if beneath is not None:
            front += f"; the top window beneath it was {window(beneath)}"
    elif front_hwnd is not None:
        details = ", ".join(
            part
            for part in (
                str(foreground.get("process_name") or ""),
                f'class {foreground["class_name"]}' if foreground.get("class_name") else "",
                f'"{foreground.get("title") or overview.get("foreground_title") or ""}"',
            )
            if part
        )
        front = f"in front was no app window (hwnd {front_hwnd}: {details})"
    else:
        front = "nothing was in front"
    parts = [front]

    if "media" in errors:
        parts.append(f"media: unknown ({errors['media']})")
    else:
        sessions = []
        for session in facts.get("media") or []:
            if not isinstance(session, dict):
                continue
            title = str(session.get("title") or "")
            artist = str(session.get("artist") or "")
            what = f'"{title}"' if title else "untitled media"
            if artist:
                what += f" by {artist}"
            sessions.append(
                f"{what} {session.get('status') or 'unknown'} in {session.get('app_id') or '?'}"
            )
        parts.append("media: " + ("; ".join(sessions) if sessions else "none"))

    own_image = os.path.basename(sys.executable).lower()
    open_calls: dict[str, dict[str, Any]] = {}
    if calls:
        from yuki.memory.store import app_key   # memory's own process identity ("Discord.exe" -> "discord")

        open_calls = {str(c.get("process")): c for c in calls if c.get("process")}
    for fact, label in (("microphone", "microphone"), ("camera", "camera")):
        if fact in errors:
            parts.append(f"{label}: unknown ({errors[fact]})")
            continue
        users = []
        for user in facts.get(fact) or []:
            if not isinstance(user, dict):
                continue
            app = str(user.get("app") or "?")
            since = user.get("since")
            text = app
            call = open_calls.get(app_key(app) or "") if fact == "microphone" and open_calls else None
            if call is not None:
                text = f"{app}, on a {call.get('app') or app} call"
                started = _call_since(call.get("since"))
                if started is not None:
                    seconds = call.get("seconds")
                    elapsed = (float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                               else max(0.0, time.time() - started.timestamp()))
                    text += (f" since {started.strftime('%H:%M')} "
                             f"({int(elapsed // 3600)}h{int(elapsed % 3600 // 60):02d}m)")
            elif isinstance(since, (int, float)) and since > 0:
                text += f" since {datetime.fromtimestamp(since).strftime('%H:%M')}"
            if app.lower() == own_image:
                text += " (the program you run as)"
            users.append(text)
        parts.append(f"{label} in use by {', '.join(users)}" if users else f"{label} not in use")
    return "When the user asked: " + "; ".join(parts) + "."


def _call_since(value: Any) -> datetime | None:
    """A call's start from ``current_calls`` (ISO local time, or epoch seconds), naive local; else ``None``."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value))
        if isinstance(value, str) and value.strip():
            stamp = datetime.fromisoformat(value.strip())
            return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _block_confirmed(event: Any) -> bool:
    """Whether the stream event after a finished ``tool_use`` block shows it complete.

    A new block starting means generation went on past it. A stop reason of
    ``max_tokens`` or ``refusal`` means it may have been cut off or disowned.
    """
    kind = getattr(event, "type", None)
    if kind == "content_block_start":
        return True
    if kind == "message_delta":
        reason = getattr(getattr(event, "delta", None), "stop_reason", None)
    else:  # message_stop carries the accumulated message
        reason = getattr(getattr(event, "message", None), "stop_reason", None)
    return reason not in _UNSAFE_STOPS


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
