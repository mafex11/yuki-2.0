"""The Brain: one decision path for a request (brain rebuild rev 2, design 2, 6, 7, 10.1).

``Brain.run(request, *, user_words, context, channel, request_id, plan_seed)`` handles one request and yields the full
:data:`yuki.log.events.AgentEvent` union, ending with exactly one :class:`Final` (with the ``outcome`` keys the voice,
the done card, the island and the ledger read) or :class:`ErrorEvent`:

1. the request turn is read in parallel (window list and Now picture, "you right now", people named, profile and rules,
   menu; or a pack read ahead by :meth:`Brain.begin_early`);
2. one planner call (:mod:`yuki.brain.planner`): its ``plan`` streams and each validated step goes to the executor
   (``yuki.brain.executor``, bExec) as it closes; the executor runs the consent rule, the commit check, the steps and
   the one done-check, and reports back (:class:`yuki.brain.plan.ExecResult`);
3. done or unconfirmed -> the code line from the probe's evidence; a mismatch, a refusal or the user's correction ->
   a replan (at most 3 for an act plan, 5 model calls in all; an answer plan: 8 calls or 30 s); otherwise the code line
   "I got stuck at ...".

The executor runs push-style against :class:`yuki.brain.plan.ExecHost` (``emit``, a blocking ``ask``), so the request
runs on its own worker thread and this generator only relays: events come through one queue (no polling), and an
``ask`` becomes an :class:`AskUser` the caller answers with :meth:`Brain.answer` before resuming, as with
``loop.Agent``.

Ported by copy from ``yuki/agent/loop.py`` (which stays untouched): the failover client, cancel and withdraw and park,
the takeover pause (watch, pause, resume, close), the input gate's notices as ``Say`` events, the launch watch, cost
accounting and ``request_summary`` / ``requests.csv``, the send countdown (``SendPending`` and its swap chips), the
memory turn log. The same caller surface as ``loop.Agent`` (``answer``, ``steer``, ``cancel``, ``pause``,
``resume``, ``send_choice``, ``take_steers``, ``last_summary`` ...), so bWire's factory can hand a lane either.

The words never leave this process in a log: every record is counts, ids, ms and code words.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, fields as dc_fields, replace
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from yuki.brain.context import (
    CHECK_LABEL,
    Conversation,
    Exchange,
    Gathered,
    answer_report,
    cutoff_report,
    gather,
    held_report,
    invalid_report,
    load_catalog,
    one_line,
    read_report,
    request_turn,
    seed_block,
    sends_fn,
    step_report,
    steer_blocks,
    swap_blocks,
    tool_result,
)
from yuki.brain.plan import (
    ACT_VERBS,
    PLAN_TOOL,
    Checks,
    ExecResult,
    Plan,
    STEP_STATES,
    Step,
    StepStatus,
)
from yuki.brain.planner import CallCancelled, CallResult, ModelCall, PlanStream, build_params
from yuki.brain.prompt import ANSWER_PROMPT, ANSWER_TOOL, system_blocks
from yuki.brain.settings import BrainSettings
from yuki.brain.tools import PRIVATE_READS, ToolRun, run_fetch, run_look, run_memory, run_remember
from yuki.log.events import (
    AgentEvent,
    AskUser,
    ErrorEvent,
    Final,
    Paused,
    Say,
    SendPending,
    SessionLogger,
    ToolCall,
    ToolResult,
)
from yuki.log.requests import TOKEN_KEYS, append_request_row, usage_tokens

#: Why a run was stopped (``loop.CANCELLED`` / ``WITHDRAWN`` / ``PARKED``'s values: the runtime compares them).
CANCELLED, WITHDRAWN, PARKED = "cancelled", "withdrawn", "parked"
#: A request stopped while paused for the user's takeover ends with this line (never spoken).
PAUSE_STOP_LINE = "Stopped. Nothing else was done."
#: After a resume (or an answer, or a tap on the countdown), the takeover watch starts again this late (s).
RESUME_GRACE_S = 1.5

#: The outcome levels' words (``finish.LEVEL_LABELS``'s values: the done card and the island repeat them).
LEVEL_LABELS: dict[str, str] = {"verified": "Checked on screen", "likely": "Likely done",
                                "unverified": "Not confirmed", "failed": "Didn’t work", "answer": ""}
#: Lines said by code (design 2.3, 2.4): never a claim the probe did not see (a done comes with the executor's line
#: from the probe's evidence; without one it is said as not confirmed).
LINE_UNCONFIRMED = "I tried, but I couldn't confirm it worked."
LINE_STUCK = "I got stuck {where}{why}. Try another way?"
LINE_DECLINED = "Okay, I didn't do that."
LINE_DRAFTED = "It's typed in; send it when you're ready."
LINE_ANSWER_BUDGET = "I couldn't finish looking that up."
LINE_REFUSAL = "I can't help with that one."
LINE_NO_EXECUTOR = "I can't act on the screen in this build yet, so I didn't do anything."
LINE_NEEDS_HANDS = "Another task is using the mouse and keyboard, so this one comes right after it."
LINE_NO_REPLY = "I couldn't work that out."
LINE_CANCELLED = "Cancelled by the user."
LINE_WITHDRAWN = "Withdrawn: replaced by the user's newer request."
LINE_PARKED = "Parked: the question stays open until the user answers it."

#: A step's verb in plain words (the stuck line and the status line), never the step's values.
STEP_WORDS: dict[str, str] = {
    "launch_app": "opening the app", "focus_window": "bringing the window forward", "open_url": "opening the page",
    "window_action": "arranging the window", "tab": "switching the tab", "click": "clicking",
    "type_text": "typing", "hotkey": "pressing the shortcut", "press": "pressing the key", "scroll": "scrolling",
    "set_slider": "moving the slider", "app_volume": "changing the volume", "run_powershell": "running the command",
    "write_clipboard": "copying", "run_skill": "running your usual way", "open_yuki": "opening my window",
    "look": "looking", "memory": "checking memory", "fetch": "reading", "wait": "waiting",
}
#: The ``outcome`` dict's keys (``finish.outcome_verdict``'s, design M7).
OUTCOME_KEYS: tuple[str, ...] = ("level", "label", "acted", "seen", "send", "partial", "line", "step", "steps", "open")
#: How long the generator waits for the worker after the caller dropped it (s).
WORKER_JOIN_S = 5.0
#: How long a launch watch may hold a run row at most (s; the watch's own 180 s and slack).
WATCH_JOIN_S = 200.0
#: Executor results that end the request with their line (unless the user added something meanwhile: design 7).
ENDINGS: tuple[str, ...] = ("done", "unconfirmed", "drafted", "declined")
#: Step states that mean an act step did something on the PC (``ExecResult.acted`` is also read).
ACTED_STATES: tuple[str, ...] = ("ok", "mismatch", "drafted")
#: Cut-off responses (``max_tokens`` or an unreadable tool input) asked again in one request; the next ends stuck.
MAX_CUTOFFS = 1
#: A cut-off response is asked again with this much more room, up to the ceiling (streamed / plain: the SDK refuses a
#: plain call whose output could outlast its HTTP timeout).
CUTOFF_TOKENS_STREAMED, CUTOFF_TOKENS_PLAIN = 64000, 20000
#: A tool whose streamed input did not parse is answered with this (never run on a cut-short value).
BAD_INPUT_TEXT = "Not run: its input could not be read as one JSON object. Send the call again."

_END = object()


@dataclass
class _Ask:
    """One question from the worker, relayed as an :class:`AskUser` by the generator."""

    question: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


@dataclass
class _Request:
    request: str
    words: list[str]
    context: str
    channel: str
    request_id: str
    seed: dict[str, Any] | None
    number: int
    #: The window in front when the user called Yuki up (typed: the ask bar or the chat page is in front by the time
    #: the request runs). Code facts only: an hwnd number.
    origin_hwnd: int | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=datetime.now)
    epoch: float = field(default_factory=time.time)

    @property
    def shown(self) -> str:
        """The request as the summary, the ledger, the turn log and the follow-up history show it: the user's words, or
        the request text when none came (display only: never quotable)."""
        return "\n".join(self.words) or self.request


@dataclass
class _State:
    """One request's running facts (the budgets and the replan inputs)."""

    calls: int = 0
    plans: int = 0
    replans: int = 0
    invalid: int = 0
    kind: str = "answer"
    acted: bool = False
    current: Plan | None = None
    failed_step: Step | None = None
    repeat_rejected: bool = False
    last: ExecResult | None = None
    #: M9: the request already holds the user's private content (the people block's own messages, a memory, file or
    #: clipboard read), so an outbound fetch is a commit from here on.
    private_read: bool = False
    #: A plan's ending (line, outcome) held because the user added something before it was said (design 7).
    held: tuple[str, dict[str, Any]] | None = None
    #: Cut-off responses so far, and the output ceiling now in force (0: the setting's).
    cutoffs: int = 0
    max_tokens: int = 0
    verdict: bool | None = None
    probe: str = ""
    check_calls: int = 0
    header_ms: float | None = None
    step_ms: float | None = None
    steps: int = 0


class _NoticeSink:
    """``Dispatcher.notices``: the input gate's line ("waiting for you to stop typing") goes out at once as a
    :class:`Say` (the dispatcher calls ``put`` from the acting thread). ``get_nowait`` is there for a loop agent sharing
    the dispatcher: it finds nothing."""

    def __init__(self, brain: "Brain") -> None:
        self._brain = brain

    def put(self, text: str) -> None:
        self._brain.logger.log("input_yield", chars=len(str(text or "")))
        self._brain._push(Say(str(text or "")))

    def get_nowait(self) -> str:
        raise queue.Empty


class _MemoryProxy:
    """The executor's ``host.memory``: the real :class:`yuki.agent.memory.MemoryAccess`, except that a run row whose
    request started an app through a launcher still starting it (a game) waits, off the request, for that opening's
    watch, and gets its verdict as a step (``loop.with_watch_steps``'s rule, copied). Everything else passes through."""

    def __init__(self, memory: Any, brain: "Brain") -> None:
        self._memory = memory
        self._brain = brain

    def __getattr__(self, name: str) -> Any:
        return getattr(self._memory, name)

    def record_run(self, run: dict[str, Any], *, on_done: Callable[[dict[str, Any]], None] | None = None) -> bool:
        watches = self._brain._take_watches()
        record = getattr(self._memory, "record_run", None)
        if not callable(record):
            return False
        if not watches:
            return bool(record(run, on_done=on_done))

        def after() -> None:
            try:
                record(with_watch_steps(run, watches), on_done=on_done)
            except Exception as exc:  # noqa: BLE001 - memory never breaks a request
                self._brain.logger.log("run_trace_failed", error=type(exc).__name__)

        threading.Thread(target=after, name="yuki-brain-run-after-watch", daemon=True).start()
        self._brain.logger.log("run_trace_held", watches=len(watches))
        return True


def with_watch_steps(run: dict[str, Any], watches: Sequence[dict[str, Any]], *,
                     join_s: float = WATCH_JOIN_S) -> dict[str, Any]:
    """The run with each launch watch's verdict as a step right after the last ``launch_app`` step of that app
    (copied from ``loop.with_watch_steps``, keyed by the launcher's name instead of a trace index: the executor writes
    the steps). Waits for the watches (bounded); returns a new run dict."""
    deadline = time.monotonic() + max(0.0, float(join_s))
    steps = list(run.get("steps") or [])
    ended = float(run.get("ended_at") or time.time())
    for entry in watches:
        thread = entry.get("thread")
        if isinstance(thread, threading.Thread):
            thread.join(max(0.0, deadline - time.monotonic()))
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {
            "verified": None, "summary": "the watch did not end in time"}
        verdict = result.get("verified") if isinstance(result.get("verified"), bool) else None
        at = float(entry.get("ended") or time.time())
        ended = max(ended, at)
        name = str(entry.get("name") or "")
        launches = [i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("tool") == "launch_app"]
        same = [i for i in launches if str(((steps[i].get("input") or {}) if isinstance(steps[i].get("input"), dict)
                                            else {}).get("query") or "").casefold() == name.casefold()]
        index = (same or launches or [len(steps) - 1])[-1]
        steps.insert(index + 1, {
            "tool": "launch_app", "kind": "action",
            "app": str(result.get("process_name") or "") or None if verdict is True else None,
            "target": f"app {name!r}, watched", "input": {"query": name, "watch": True},
            "ok": verdict is not False, "verdict": verdict,
            "why": ("its own window showed" if verdict is True else "its own window did not show"
                    if verdict is False else "the watch stopped before it could tell"),
            "ms": round((at - float(entry.get("began") or at)) * 1000.0, 1), "at": at})
    return {**run, "steps": steps, "ended_at": ended}


# ---------------------------------------------------------------------------------------------------------------------
# The executor's host
# ---------------------------------------------------------------------------------------------------------------------


class Host:
    """One request's :class:`yuki.brain.plan.ExecHost`, plus optional helpers the executor may use (``getattr``):
    ``hold_paused()``, ``countdown(...)``, ``ask_text(question)``, ``account(model, usage, ...)``, ``late_line(text)``,
    ``run_fields()``, ``private_read()`` (M9: consent row 5's "fetch after a private read"), ``swaps()`` (the
    countdown's tapped chips, for row 4's recipient check). Every method is safe to call from the executor's thread."""

    def __init__(self, brain: "Brain", req: _Request) -> None:
        self._brain = brain
        self._req = req
        self.request_id: str = req.request_id
        self.lane: str = brain.lane
        self.channel: str = req.channel
        #: The user's own words: the typed text or the heard turns, then their answers and additions mid-task (all
        #: from the runtime or the bridge; never a model's output, a card's text or the screen's).
        self.user_words: list[str] = list(req.words)
        self.memory: Any = _MemoryProxy(brain.memory, brain)
        self.dispatcher: Any = brain.dispatcher
        self.settings: Any = brain.settings
        # the executor's optional extras (read with getattr; merge of bCore and bExec)
        #: The session log (the executor passes every record through ``content_free``).
        self.log: Callable[..., None] = brain.logger.log
        #: The request's untrusted-data fence (set by the Brain before the executor is made).
        self.nonce: str = ""
        #: The user's own words from up to 3 earlier requests (never a card's text), for the commit check.
        self.earlier_words: list[str] = list(brain._earlier_words)[-3:]
        #: The window in front when the user called Yuki up (an hwnd, or ``None``): the executor's stand-in for the
        #: foreground while Yuki's own window is in front (a step that names no window acts there).
        self.origin_hwnd: int | None = req.origin_hwnd
        #: The executor's desk lease (set by the Brain once the executor is made): freed while a question waits.
        self.desk: Any = None

    @property
    def model_calls(self) -> int:
        """Model calls so far in this request (the planner's), for the run row."""
        state = self._brain._state
        return state.calls if state is not None else 0

    @property
    def cost_usd(self) -> float:
        """This request's cost so far without the commit check's (the executor adds its own checker's)."""
        return float(self._brain._cost_usd)

    # -- the pinned protocol ------------------------------------------------------------------------------------------

    def acting_begin(self) -> None:
        self._brain._acting_start(self._req)

    def acting_end(self) -> None:
        self._brain._acting_done()

    def cancelled(self) -> bool:
        return self._brain._cancelled.is_set()

    def paused(self) -> bool:
        return self._brain.paused

    def steer_pending(self) -> list[str] | None:
        return self._brain._peek_steers() or None

    def emit(self, event: AgentEvent) -> None:
        self._brain._observe(event)
        self._brain._push(event)

    def ask(self, question: str) -> bool:
        """Ask and read the reply as a go-ahead or not (a small model's yes/no over the question and the reply only;
        unclear, unanswered, cancelled or failed is no). The reply is the user's own words from here on."""
        answer = self.ask_text(question)
        if answer is None or self.cancelled():
            return False
        return self._brain._judge_answer(question, answer)

    # -- helpers ------------------------------------------------------------------------------------------------------

    def ask_text(self, question: str) -> str | None:
        """One question, the user's reply as text (``None``: no answer, or cancelled). The reply joins
        :attr:`user_words`. The desk lease is freed first (design 2.3, L4: the desk is free while a question waits; the
        next act step takes it again), whoever asks: the executor, an ask plan or the ``ask_user`` tool."""
        self.free_desk()
        answer = self._brain._ask(question)
        if answer is not None and answer.strip() and not self.cancelled():
            self.user_words.append(answer.strip())
        return answer

    def free_desk(self) -> None:
        """Free the executor's desk lease if it holds it (idempotent; never raises)."""
        lease = self.desk
        if lease is None or not getattr(lease, "held", False):
            return
        try:
            lease.release()
            self.log("brain_desk", state="freed", why="ask")
        except Exception:  # noqa: BLE001
            pass

    def hold_paused(self) -> bool:
        """When the user took over: say so (:class:`Paused`), wait for a resume, a stop or the limit; ``True`` when it
        goes on (read the window again first), ``False`` when it ended (a stop is this request's cancel)."""
        return self._brain._hold_paused()

    def countdown(self, *, who: str = "", where: str = "", app: str = "", text: str = "", typed: bool = False,
                  window: str = "", who_alts: Sequence[str] = (), where_alts: Sequence[str] = (),
                  from_memory: bool = False) -> str:
        """The send countdown (``SendPending``): ``going`` (send now), ``stopped`` (cancelled: nothing sent), or
        ``swapped`` (a chip was tapped: the choice joins the user's words and the plan is revised)."""
        return self._brain._send_countdown(who=who, where=where, app=app, text=text, typed=typed, window=window,
                                           who_alts=tuple(who_alts), where_alts=tuple(where_alts),
                                           from_memory=from_memory)

    def account(self, model: str, usage: Any, *, ms: float = 0.0, role: str = "check") -> None:
        """A model call the executor made (the commit check): its tokens and cost join the request's."""
        self._brain._account(model, usage, ms=ms, role=role)

    def late_line(self, text: str) -> None:
        """A correcting line after the request ended (the background re-check disagreed, design 2.4): handed to
        ``Brain.on_late`` (the voice and the island), never a new request."""
        self._brain._late_line(text)

    def run_fields(self) -> dict[str, Any]:
        """The request's own fields for a run row (``loop._record_run``'s header): source, ids, lane, words, timing,
        model calls and cost so far, the words added mid-run."""
        return self._brain._run_fields(self._req)

    def private_read(self) -> bool:
        """Whether this request already holds the user's private content (M9), whoever read it: the request turn's
        people block (the user's own last messages), a memory, file or clipboard read run outside the plan, or one the
        executor reported through :meth:`emit`. From then on a plan ``fetch`` of an address is a may-commit step
        (consent row 5)."""
        state = self._brain._state
        return bool(state is not None and state.private_read)

    def swaps(self) -> list[dict[str, str]]:
        """The chips the user tapped on the send countdown in this request, oldest first: ``{"kind": "who" | "where",
        "value": the tapped choice, "was": what it replaced}``. A tap is the user's own choice of person or place (from
        the runtime, never a model's output), but it is not words: nothing here is quotable. Consent row 4 may accept a
        revised plan's ``consent.to`` equal to a tapped ``who`` value."""
        return self._brain._swaps_seen()


# ---------------------------------------------------------------------------------------------------------------------
# The Brain
# ---------------------------------------------------------------------------------------------------------------------


class Brain:
    """Yuki's new brain (one decision path). One instance per lane, one request at a time.

    Args:
        settings: :class:`yuki.config.Settings` (held by reference; the brain's own values are read per request).
        logger: The session logger (content-free by default when made here).
        client: Anything with ``messages.stream`` / ``messages.create``; default the failover client.
        backend / dispatcher: The desktop (``yuki.agent.tools.Dispatcher``); default the real one, lazily.
        memory: :class:`yuki.agent.memory.MemoryAccess`; default the process-wide one.
        lane: Written into every summary and run row.
        record_requests: Append one ``requests.csv`` line per request.
    """

    def __init__(self, settings: Any = None, logger: SessionLogger | None = None, *, client: Any = None,
                 backend: Any = None, dispatcher: Any = None, memory: Any = None, lane: str = "main",
                 record_requests: bool = True) -> None:
        if settings is None:
            from yuki.config import Settings

            settings = Settings()
        self.settings = settings
        if memory is None:
            from yuki.agent.memory import default_memory

            memory = default_memory()
        self.memory = memory
        self.lane = lane
        self.record_requests = record_requests
        if logger is None:
            from yuki.log.events import content_kept

            logger = SessionLogger(settings.sessions_dir, content=content_kept(), quiet=True)
        self.logger = logger
        if dispatcher is None:
            from yuki.agent.tools import Dispatcher

            # screenshots never: the brain has no screenshot tool, and its dispatcher refuses one outright
            dispatcher = Dispatcher(backend, screenshot_policy="never",
                                    tool_timeout_s=float(getattr(settings, "tool_timeout_s", 20.0)))
        self.dispatcher = dispatcher
        self._enable_now()
        self._client = client
        self._client_lock = threading.Lock()
        self._q: queue.Queue[Any] = queue.Queue()
        self._cancelled = threading.Event()
        self.cancel_reason = CANCELLED
        self._steers: deque[str] = deque()
        self._steer_lock = threading.Lock()
        #: Countdown chips tapped this request: waiting for the next turn, and all of them (``Host.swaps``).
        self._swaps: list[dict[str, str]] = []
        self._swaps_all: list[dict[str, str]] = []
        self._pending_answer: str | None = None
        self._awaiting_answer = False
        self._ask_open: _Ask | None = None
        #: The send countdown (``SendPending``): seconds to stop or swap a send before it goes; 0 never waits (the
        #: desktop runtime turns it on, as for ``loop.Agent``).
        self.send_countdown_s = 0.0
        self._send_wake = threading.Event()
        self._send_lock = threading.Lock()
        self._send_open: dict[str, Any] | None = None
        self._send_choice: tuple[str, str] | None = None
        self._send_stopped: bool | None = None
        #: appTakeover (as ``loop.Agent``): the watch factory, the surfaces' callback, whether a pause may start now.
        self.takeover: Any = None
        self.on_pause: Any = None
        self.takeover_ok: Any = None
        #: A correcting line after a request ended (``on_late(text)``; the voice and the island), or ``None``.
        self.on_late: Callable[[str], None] | None = None
        #: ``False`` on a lane that only looks (the runtime's front desk while the worker holds the desk): an act plan
        #: runs nothing there; the request ends with :data:`LINE_NEEDS_HANDS` and :attr:`needed_hands` is set, so the
        #: runtime queues it for the lane that acts. Answer plans run as anywhere (they never take the desk lease).
        self.hands = True
        self.needed_hands = False
        self.pause_timeout_s = float(getattr(settings, "takeover_idle_stop_s", 600.0) or 600.0)
        self._paused = threading.Event()
        self._pause_wake = threading.Event()
        self._pause_lock = threading.Lock()
        self._pause_why = ""
        self._pause_at = 0.0
        self._pauses = 0
        self._paused_s = 0.0
        self._stopped_paused = False
        self._pause_end_logged = False
        self._pause_end_told = False
        self._pause_closing = False
        self._pause_quiet_until = 0.0
        self._resume_asked = False
        self._watch: Any = None
        self._acting_token: str | None = None
        self._acting_words = ""
        self._input_held = False
        self._setup_input_gate()
        #: The last two requests' words and Yuki's final answers (RAM only), for follow-ups (M8).
        self._history: deque[Exchange] = deque(maxlen=2)
        #: The user's own words of the last 3 requests that had any (never a card's text): the commit check's
        #: "earlier turns" (design 2.6).
        self._earlier_words: deque[str] = deque(maxlen=3)
        #: This request's commit check (bExec's ``CommitCheck``; its calls and cost join the request's).
        self._checker: Any = None
        self._checks_seen = 0
        #: The line the request ended with (the executor's run row carries it).
        self._final_text = ""
        self._requests = 0
        self._request_started = time.monotonic()
        self._running = False
        #: The voice's early start: the pack read ahead (context, the words it was read for, its nonce), the generation
        #: it belongs to (each request's start moves it on, so a pack that lands later is dropped), its reader.
        self._early: tuple[Gathered, str, str] | None = None
        self._early_gen = 0
        self._early_lock = threading.Lock()
        self._early_thread: threading.Thread | None = None
        self._early_thread_gen = -1
        self._watches: list[dict[str, Any]] = []
        self._watch_lock = threading.Lock()
        self._status_plan: Plan | None = None
        self._status_done = 0
        self._status_verb = ""
        self._app_named: str | None = None
        self._state: _State | None = None
        self._bs: BrainSettings | None = None
        self._req: _Request | None = None
        self._host_ref: Host | None = None
        self._menu_lines = ""
        self._last_exec_s = 0.0
        self._reset_accounting()
        #: The ``request_summary`` of the last request that ended (``None`` while one runs).
        self.last_summary: dict[str, Any] | None = None

    # -- the client ---------------------------------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The failover client (retries, failover, Settings -> AI routing), made on first use."""
        with self._client_lock:
            if self._client is None:
                from yuki.agent.failover import FailoverClient

                self._client = FailoverClient.from_settings(self.settings, log=self.logger.log)
            return self._client

    def _used_model(self, requested: str) -> str:
        used = getattr(self._client, "last_model", None)
        try:
            return (used() if callable(used) else None) or requested
        except Exception:  # noqa: BLE001
            return requested

    def _enable_now(self) -> None:
        """The Now picture in the overview, for the real desktop only (as ``loop.Agent``)."""
        try:
            from yuki.agent.tools import _LazyRealBackend

            enable = getattr(self.dispatcher, "enable_now", None)
            if callable(enable) and isinstance(getattr(self.dispatcher, "backend", None), _LazyRealBackend):
                enable(self.settings)
        except Exception:  # noqa: BLE001
            pass

    # -- caller controls (loop.Agent's surface) -----------------------------------------------------------------------

    def answer(self, text: str) -> None:
        """The answer to the :class:`AskUser` just yielded; call before resuming iteration."""
        if not self._awaiting_answer:
            raise RuntimeError("Brain is not waiting for an answer")
        self._pending_answer = text

    def steer(self, text: str) -> None:
        """Something the user said while this request runs (their exact words). Safe from any thread: it waits for the
        executor's next step boundary, where the plan is revised with it (design 7)."""
        text = (text or "").strip()
        if text:
            with self._steer_lock:
                self._steers.append(text)

    def _peek_steers(self) -> list[str]:
        with self._steer_lock:
            return list(self._steers)

    def take_steers(self) -> list[str]:
        """Everything in the steering inbox, emptying it."""
        with self._steer_lock:
            out = list(self._steers)
            self._steers.clear()
            return out

    def _take_swaps(self) -> list[dict[str, str]]:
        with self._steer_lock:
            out, self._swaps = self._swaps, []
            return out

    def _swaps_seen(self) -> list[dict[str, str]]:
        with self._steer_lock:
            return [dict(s) for s in self._swaps_all]

    def _steer_waiting(self) -> bool:
        """Something the user added (words, or a countdown chip) waits for the next turn."""
        with self._steer_lock:
            return bool(self._steers or self._swaps)

    def set_app_named(self, app: str | None) -> None:
        """Kept for the runtime's interface (``loop.Agent.set_app_named``). The new brain takes the user's exact
        words only (D10): a talker's reading of them is not used."""
        self._app_named = " ".join(str(app or "").split())[:80] or None

    def cancel(self, reason: str = CANCELLED) -> None:
        """Stop the run (``reason``: cancelled, withdrawn or parked). Safe from any thread. A step in flight finishes
        its own part; a stream with nothing run yet is dropped; a countdown, a pause or a question ends at once."""
        self.cancel_reason = reason if reason in (WITHDRAWN, PARKED) else CANCELLED
        self._cancelled.set()
        self._send_wake.set()
        self._pause_wake.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def awaiting_answer(self) -> bool:
        return self._awaiting_answer

    def send_choice(self, kind: str, value: str) -> bool:
        """A chip tapped on the send countdown (``who`` / ``where``, one of the alternatives shown)."""
        with self._send_lock:
            open_ = self._send_open
            if open_ is None or kind not in ("who", "where"):
                return False
            if str(value) not in (open_.get("who_alts") if kind == "who" else open_.get("where_alts") or ()):
                return False
            self._send_choice = (kind, str(value))
        self._send_wake.set()
        return True

    def warm_cache(self, effort: str | None = None, thinking: bool | None = None) -> bool | None:
        """No pre-warm (design 6, M11): the prefix is about 4k tokens; a warm is added only if the logs show it
        matters. Kept for the runtime's interface."""
        self.logger.log("model_prewarm", skipped="brain: no pre-warm")
        return None

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self._request_started

    def wait_for_prewarm(self, timeout_s: float | None = None) -> bool:
        return True

    # -- appTakeover (copied from loop.Agent) -------------------------------------------------------------------------

    def pause(self, why: str = "") -> bool:
        """The user took over the keyboard or mouse: no new step starts until :meth:`resume`. ``False`` when nothing
        is acting, it is stopping, a question or a countdown is open, or it is already paused."""
        with self._pause_lock:
            if (self._acting_token is None or self._cancelled.is_set() or self._paused.is_set()
                    or self._pause_closing):
                return False
            if self._awaiting_answer or self._send_open is not None or time.monotonic() < self._pause_quiet_until:
                return False
            allowed = self.takeover_ok
            if callable(allowed):
                try:
                    if not allowed():
                        return False
                except Exception:  # noqa: BLE001 - unknown: pause as usual
                    pass
            self._pause_why, self._pause_at = str(why or ""), time.monotonic()
            self._pauses += 1
            self._resume_asked = False
            self._pause_end_logged = self._pause_end_told = False
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
        """Go on after the user took over; ``words`` they typed or said with it join the request as theirs."""
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
        return self._paused.is_set() and not self._resume_asked

    def _watch_on(self, grace_s: float = 0.0) -> None:
        if self.takeover is None or self._watch is not None or self._paused.is_set() or self._cancelled.is_set():
            return
        try:
            self._watch = self.takeover(self.pause, grace_s=grace_s)
            self._watch.start()
        except Exception as exc:  # noqa: BLE001 - the request goes on without the watch
            self._watch = None
            self.logger.log("takeover", state="watch_failed", error=type(exc).__name__)

    def _watch_off(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            try:
                watch.stop()
            except Exception:  # noqa: BLE001
                pass

    def _hold_paused(self) -> bool:
        """Push-style ``loop.Agent._hold_paused``: a :class:`Paused` event, then wait (on the wake event, no polling)
        for a resume, a stop or the limit."""
        if not self._paused.is_set() or self._cancelled.is_set():
            return False
        self._watch_off()
        why, since = self._pause_why, self._pause_at or time.monotonic()
        self._push(Paused("paused", why))
        limit = float(self.pause_timeout_s or 600.0)
        timed_out = False
        while not self._resume_asked and not self._cancelled.is_set():
            left = limit - (time.monotonic() - since)
            if left <= 0:
                timed_out = True
                break
            self._pause_wake.wait(left)
            self._pause_wake.clear()
        took = time.monotonic() - since
        self._paused_s += took
        self._waited_s += took
        if timed_out:
            self.logger.log("takeover", state="ended", why=why, seconds=round(took, 1))
            self._stopped_paused = True
            self._pause_end_logged = self._pause_end_told = True
            self._tell_pause("ended", why, round(took, 1))
            self._push(Paused("ended", why, round(took, 1)))
            self.cancel()
            return False
        if self._cancelled.is_set():
            self._stopped_paused = True
            self._pause_end_logged = True
            self.logger.log("takeover", state="stopped", why=why, seconds=round(took, 1))
            return False
        with self._pause_lock:
            self._paused.clear()
            self._resume_asked = False
            self._pause_end_logged = self._pause_end_told = True
        self.logger.log("takeover", state="resumed", why=why, seconds=round(took, 1))
        self._tell_pause("resumed", why, round(took, 1))
        self._push(Paused("resumed", why, round(took, 1)))
        self._watch_on(grace_s=RESUME_GRACE_S)
        return True

    def _close_pause(self, how: str) -> None:
        """The request ends while still paused: the pause ends with it, everywhere (``loop.Agent._close_pause``)."""
        try:
            with self._pause_lock:
                self._pause_closing = True
                if not self._paused.is_set():
                    return
                self._paused.clear()
                self._resume_asked = False
                logged, told = self._pause_end_logged, self._pause_end_told
                self._pause_end_logged = self._pause_end_told = True
                why, since = self._pause_why, self._pause_at
            seconds = round(max(0.0, time.monotonic() - since), 1) if since else 0.0
            if not logged:
                self.logger.log("takeover", state="closed", how=how, why=why, seconds=seconds)
            if not told:
                self._tell_pause("closed", why, seconds)
        except Exception:  # noqa: BLE001
            pass

    # -- the acting marker (D18, H4) ----------------------------------------------------------------------------------

    def _acting_start(self, req: _Request) -> None:
        """Publish (or refresh) "Yuki is acting" for memory, with the request's own id, and start the takeover watch.
        The token is ``f"{lane}-{request_id}"``, exactly as ``loop._acting_start`` builds it."""
        refresh = self._acting_token is not None
        token = self._acting_token or f"{self.lane}-{req.request_id}"
        try:
            begin = getattr(self.memory, "acting_begin", None)
            published = bool(begin(token, self._acting_words, lane=self.lane, request_id=req.request_id)) \
                if callable(begin) else False
        except Exception:  # noqa: BLE001
            published = False
        self._acting_token = token
        if not refresh or not published:
            self.logger.log("memory_acting", state="refresh" if refresh else "begin", token=token,
                            published=published)
        self._watch_on()

    def _acting_done(self) -> None:
        """Withdraw the marker, if one was published (idempotent); the watch stops."""
        self._watch_off()
        token, self._acting_token = self._acting_token, None
        if token is None:
            return
        withdrawn = False
        end = getattr(self.memory, "acting_end", None)
        for _ in range(2):
            try:
                withdrawn = bool(end(token)) if callable(end) else False
            except Exception:  # noqa: BLE001
                withdrawn = False
            if withdrawn or not callable(end):
                break
        self.logger.log("memory_acting", state="end", token=token, withdrawn=withdrawn)

    # -- the input gate (copied from loop.Agent) ----------------------------------------------------------------------

    def _setup_input_gate(self) -> None:
        dispatcher = self.dispatcher
        try:
            if getattr(dispatcher, "notices", None) is None:
                dispatcher.notices = _NoticeSink(self)
            dispatcher.cancel_check = lambda: self._cancelled.is_set() or self._paused.is_set()
        except Exception:  # noqa: BLE001 - a dispatcher double without the attributes
            return
        try:
            from yuki.agent.tools import _LazyRealBackend

            if (getattr(self.settings, "input_yield", True) and getattr(dispatcher, "input_gate", None) is None
                    and isinstance(getattr(dispatcher, "backend", None), _LazyRealBackend)):
                from yuki.actions.leases import InputGate

                s = self.settings
                dispatcher.input_gate = InputGate(
                    recent_s=float(getattr(s, "input_yield_recent_s", 1.0)),
                    quiet_s=float(getattr(s, "input_yield_quiet_s", 0.8)),
                    max_s=float(getattr(s, "input_yield_max_s", 12.0)),
                    probe_s=float(getattr(s, "input_yield_probe_s", 0.25)),
                    notify_after_s=float(getattr(s, "input_yield_notify_s", 3.0)))
        except Exception:  # noqa: BLE001
            pass

    def _input_on(self) -> None:
        if self._input_held:
            return
        try:
            from yuki.agent.tools import _LazyRealBackend

            if not isinstance(getattr(self.dispatcher, "backend", None), _LazyRealBackend):
                return
            from yuki.actions import userinput

            self._input_held = True
            if not userinput.acquire():
                self.logger.log("user_input", state="unavailable")
        except Exception as exc:  # noqa: BLE001
            self.logger.log("user_input", state="failed", error=type(exc).__name__)

    def _input_off(self) -> None:
        if not self._input_held:
            return
        self._input_held = False
        try:
            from yuki.actions import userinput

            facts = userinput.shared().facts()
            userinput.release()
            self.logger.log("user_input", state="released", running=facts.get("running"), counts=facts.get("counts"))
        except Exception:  # noqa: BLE001
            pass

    # -- the send countdown (copied from loop.Agent, push-style) ------------------------------------------------------

    def _send_countdown(self, *, who: str, where: str, app: str, text: str, typed: bool, window: str,
                        who_alts: tuple[str, ...], where_alts: tuple[str, ...], from_memory: bool) -> str:
        seconds = float(self.send_countdown_s)
        info = {"who": who, "where": where, "app": app, "text": text, "typed": typed, "window": window,
                "who_alts": who_alts, "where_alts": where_alts, "from_memory": from_memory}
        if seconds <= 0:
            return "going"
        with self._send_lock:
            self._send_open, self._send_choice = info, None
        self._send_wake.clear()
        self.logger.log("send_countdown", state="counting", seconds=seconds, chars=len(text),
                        who_alts=len(who_alts), where_alts=len(where_alts), typed=typed)
        if self._req is not None and self._req.channel == "voice" and who:
            self._push(Say(f"Sending to {who}"))
        self._push(SendPending("counting", seconds=seconds, **info))
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
        self._pause_quiet_until = time.monotonic() + RESUME_GRACE_S
        self._waited_s += time.monotonic() - started
        event = dict(info)
        if state == "swapped" and choice is not None:
            kind, value = choice
            if kind == "who":
                swap = {"kind": "who", "value": value, "was": who}
                event.update(who=value, who_alts=tuple(x for x in (who, *who_alts) if x and x != value)[:2])
            else:
                swap = {"kind": "where", "value": value, "was": where or app}
                event.update(where=value, where_alts=tuple(x for x in (where, *where_alts) if x and x != value)[:2])
            event["window"] = ""
            # the tap is the user's choice but not their words (design 2.5(a)): the revision gets it as a code-written
            # fact, and the consent rule sees it through Host.swaps(); nothing composed here becomes quotable
            with self._steer_lock:
                self._swaps.append(swap)
                self._swaps_all.append(dict(swap))
            self.logger.log("send_countdown", state="swapped", swap=kind,
                            after_ms=round((time.monotonic() - started) * 1000))
            self._push(SendPending("swapped", seconds=seconds, **event))
            return "swapped"
        if state == "stopped":
            self._send_stopped = bool(typed)
        self.logger.log("send_countdown", state=state, after_ms=round((time.monotonic() - started) * 1000))
        self._push(SendPending(state, seconds=seconds, **event))
        return state

    # -- the launch watch (copied from loop.Agent) --------------------------------------------------------------------

    def _watch_launch(self, payload: Any) -> None:
        """A ``launch_app`` whose launcher is still starting the app (a game): the voice hand's light, bounded watch on
        its own thread; its verdict joins the run row (:class:`_MemoryProxy`)."""
        details = payload.get("details") if isinstance(payload, Mapping) else None
        launcher = details.get("launcher") if isinstance(details, Mapping) else None
        if not isinstance(launcher, Mapping) or not details.get("starting"):
            return
        backend = getattr(self.dispatcher, "backend", None)
        watch = getattr(backend, "watch_launch", None) if backend is not None else None
        if not callable(watch):
            return
        entry: dict[str, Any] = {"name": str(launcher.get("name") or ""), "began": time.time(), "ended": None,
                                 "result": None}
        logger = self.logger

        def run() -> None:
            try:
                result = watch(dict(launcher))
            except Exception as exc:  # noqa: BLE001
                result = {"verified": None, "summary": f"the watch failed: {type(exc).__name__}"}
            entry["result"] = result if isinstance(result, dict) else {"verified": None, "summary": str(result)}
            entry["ended"] = time.time()
            try:
                logger.log("launch_watch", verified=entry["result"].get("verified"),
                           waited_s=round(entry["ended"] - entry["began"], 1))
            except Exception:  # noqa: BLE001
                pass

        entry["thread"] = threading.Thread(target=run, name="yuki-brain-launch-watch", daemon=True)
        with self._watch_lock:
            self._watches.append(entry)
        entry["thread"].start()

    def _take_watches(self) -> list[dict[str, Any]]:
        with self._watch_lock:
            out, self._watches = self._watches, []
            return out

    # -- events -------------------------------------------------------------------------------------------------------

    def _push(self, item: Any) -> None:
        self._q.put(item)

    def _observe(self, event: AgentEvent) -> None:
        """What the executor's events say about the run: the status line, the tool counts, private reads (a later
        outbound fetch is a commit, M9), a launch still starting."""
        try:
            if isinstance(event, ToolCall):
                self._status_verb = event.name
                self._tool_counts[event.name] += 1
                if event.name in PRIVATE_READS and self._state is not None:
                    self._state.private_read = True
            elif isinstance(event, ToolResult):
                self._status_done += 1
                if event.name == "launch_app" and event.ok:
                    self._watch_launch(event.payload)
        except Exception:  # noqa: BLE001 - presentation only
            pass

    def _late_line(self, text: str) -> None:
        self.logger.log("late_line", chars=len(str(text or "")))
        tell = self.on_late
        if callable(tell):
            try:
                tell(str(text or ""))
            except Exception:  # noqa: BLE001
                pass

    # -- asking -------------------------------------------------------------------------------------------------------

    def _ask(self, question: str) -> str | None:
        """One question round trip through the generator (it yields :class:`AskUser`). Blocks the worker on an event
        until the caller answered or the request ended."""
        if self._cancelled.is_set():
            return None
        item = _Ask(" ".join(str(question or "").split()))
        self._ask_open = item
        self._push(item)
        item.done.wait()
        self._ask_open = None
        self._pause_quiet_until = time.monotonic() + RESUME_GRACE_S
        return None if self._cancelled.is_set() else item.answer

    def _judge_answer(self, question: str, answer: str) -> bool:
        """The user's reply to Yuki's yes/no question, read by the small check model as a go-ahead or not (no word list:
        design rule). Only the check model (``check_model``, else ``judge_model``: a Haiku), never the planner's: forced
        tool use is a 400 on the newest planners. Fail closed: no check model, an error, a timeout or an unclear reply
        is no. Content-free log."""
        started = time.perf_counter()
        bs = self._bs or BrainSettings.read(self.settings)
        model = bs.check_model
        if not model:
            self.logger.log("ask_judge", ok=False, error="no_check_model", ms=0.0)
            return False
        try:
            client = self.client
            claude = getattr(client, "claude_only", None)
            client = claude() if callable(claude) else client
            response = client.messages.create(
                model=model, max_tokens=64, system=ANSWER_PROMPT,
                messages=[{"role": "user", "content": f"Yuki asked: {question}\nThe user replied: {answer}"}],
                tools=[ANSWER_TOOL], tool_choice={"type": "tool", "name": "answer"},
                thinking={"type": "disabled"}, timeout=bs.ask_judge_s)
        except Exception as exc:  # noqa: BLE001 - fail closed
            self.logger.log("ask_judge", ok=False, error=type(exc).__name__,
                            ms=round((time.perf_counter() - started) * 1000, 1))
            return False
        agrees = False
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and isinstance(getattr(block, "input", None), Mapping):
                agrees = block.input.get("agrees") is True
        ms = (time.perf_counter() - started) * 1000
        self._account(self._used_model(model), getattr(response, "usage", None), ms=ms, role="ask")
        self.logger.log("ask_judge", ok=True, agrees=agrees, ms=round(ms, 1))
        return agrees

    # -- reading ahead (the voice's early start) ----------------------------------------------------------------------

    def begin_early(self, words: str | Sequence[str]) -> None:
        """The voice's early start (design 10.1): read the request turn's parts now, while the talker's ``hand_over``
        is still being written, so ``run`` finds them ready (the overview is used when at most 2 s old, the rest when at
        most ``pack_max_age_s``, and only by the next request, for the same words). Returns at once; never raises."""
        text = words if isinstance(words, str) else "\n".join(str(w) for w in words if str(w).strip())
        with self._early_lock:
            gen = self._early_gen
            if (self._early_thread is not None and self._early_thread.is_alive()
                    and self._early_thread_gen == gen):
                return
            bs = BrainSettings.read(self.settings)

            def build() -> None:
                try:
                    from yuki.agent.untrusted import new_nonce

                    nonce = new_nonce()
                    got = gather(dispatcher=self.dispatcher, memory=self.memory, settings=self.settings, words=text,
                                 names=list(getattr(self.settings, "user_names", []) or []), limits=bs, nonce=nonce)
                    with self._early_lock:
                        kept = self._early_gen == gen
                        if kept:
                            self._early = (got, text, nonce)
                    # a request started meanwhile took no pack: this one is for nobody now, never the next request
                    self.logger.log("brain_early", parts_ms=got.parts_ms, kept=kept)
                except Exception as exc:  # noqa: BLE001
                    self.logger.log("brain_early", error=type(exc).__name__)

            self._early_thread = threading.Thread(target=build, name="yuki-brain-early", daemon=True)
            self._early_thread_gen = gen
            self._early_thread.start()

    def _take_early(self) -> tuple[Gathered | None, str, str | None]:
        """The pack read ahead for this request, if one is ready (taken once); a reader still running belongs to no one
        from now on."""
        with self._early_lock:
            pack, self._early = self._early, None
            self._early_gen += 1
        if pack is None:
            return None, "", None
        return pack

    # -- status ("how's it going?") -----------------------------------------------------------------------------------

    def status_line(self) -> str | None:
        """The running request's status in plain words, by code (``bridge.work_line`` reads it): ``None`` when no
        request runs."""
        if not self._running:
            return None
        if self._awaiting_answer:
            return "Waiting for your answer."
        if self.paused:
            return "Paused while you use the mouse or keyboard."
        plan = self._status_plan
        if plan is None:
            return "Working out how to do it."
        if plan.kind != "act":
            return f"Looking into it: {plan.goal}."
        total = len(plan.steps) or max(self._status_done, 1)
        done = min(self._status_done, total)
        now = STEP_WORDS.get(self._status_verb, "")
        if done < len(plan.steps):
            try:   # the plan's step in the island's words (ui/step_words.py): "clicking Send in WhatsApp"
                from yuki.ui.step_words import describe_plan_step

                words = describe_plan_step(plan.steps[done], app=plan.target.app)
                now = words[:1].lower() + words[1:] if words else now
            except Exception:  # noqa: BLE001 - presentation only
                pass
        return f"{plan.goal}: {done} of {total} steps done" + (f", now {now}" if now and done < total else "") + "."

    # -- the request --------------------------------------------------------------------------------------------------

    def run(self, request: str, *, user_words: list[str], context: str = "", channel: str, request_id: str,
            plan_seed: dict | None = None, origin_hwnd: int | None = None) -> Iterator[AgentEvent]:
        """Handle one request (design 2). ``user_words``: the typed text or the heard turns, from the runtime or the
        bridge only; when none come, nothing is quotable (fail closed, M2) and ``request`` is shown as context only;
        ``context``: card / coach / where-typed text, shown labelled and never quotable; ``channel``: ``voice`` |
        ``typed``; ``request_id``: the id every log record of it carries; ``plan_seed``: a skill's hand-off (rung C);
        ``origin_hwnd``: the window in front when the user called Yuki up (a typed request runs with Yuki's own box in
        front): told to the planner as a code line (the hwnd only), and the executor's stand-in for the foreground.
        Yields events; pauses on :class:`AskUser`; ends with exactly one :class:`Final` or :class:`ErrorEvent`."""
        self._begin(request, user_words, channel)
        words = [str(w).strip() for w in (user_words or []) if str(w).strip()]
        context = str(context or "")
        asked = str(request or "").strip()
        if not words and asked:
            # the request may be composed with card text: shown as labelled context, never as the user's words
            context = f"The request as it came: {asked}" + (f"\n\n{context}" if context.strip() else "")
        try:
            origin = int(origin_hwnd) if origin_hwnd else None
        except (TypeError, ValueError):
            origin = None
        if origin:
            # code facts only (the hwnd number, never window text): what "this" and "here" point at
            line = (f"When they called Yuki up, window [{origin}] was in front; Yuki's own box may be in front now. A "
                    f"step that names no window then acts on [{origin}]; keys and clicks reach it only once it is in "
                    f"front again.")
            context = f"{context}\n\n{line}" if context.strip() else line
        self.logger.reset_usage()
        self.logger.begin_turn()
        # no id from the caller: the log's own (``<session id>-<n>``), as the old loop's acting marker carries it
        rid = self.logger.begin_request(str(request_id) if request_id else None)
        req = _Request(request=str(request or ""), words=words, context=context,
                       channel="voice" if channel == "voice" else "typed", request_id=str(rid or request_id or ""),
                       seed=dict(plan_seed) if isinstance(plan_seed, Mapping) else None, number=self._requests,
                       origin_hwnd=origin)
        self._req = req
        self._acting_words = req.shown
        if not words:
            self.logger.log("brain_no_user_words", request_chars=len(asked))
        outcome, reply = "abandoned", ""
        worker = self._worker = threading.Thread(target=self._work, args=(req,), name=f"yuki-brain-{self.lane}",
                                                 daemon=True)
        try:
            self._input_on()
            worker.start()
            while True:
                item = self._q.get()
                if item is _END:
                    break
                if isinstance(item, _Ask):
                    if self._cancelled.is_set():
                        item.done.set()
                        continue
                    self._awaiting_answer = True
                    self._pending_answer = None
                    asked_at = time.monotonic()
                    self.logger.log("ask_user", chars=len(item.question))
                    try:
                        yield AskUser(item.question)
                    finally:
                        self._waited_s += time.monotonic() - asked_at
                        self._awaiting_answer = False
                        item.answer, self._pending_answer = self._pending_answer, None
                        self.logger.log("user_answer", answered=item.answer is not None,
                                        chars=len(item.answer or ""))
                        item.done.set()
                    continue
                if isinstance(item, Final):
                    outcome, reply = "final", item.text
                elif isinstance(item, ErrorEvent):
                    outcome = self.cancel_reason if self._cancelled.is_set() else "error"
                    reply = item.text
                yield item
        except GeneratorExit:
            raise
        except Exception as exc:  # noqa: BLE001 - never a crash into the caller
            self.logger.log("error", where="brain_run", error=type(exc).__name__)
            outcome, reply = "error", f"{type(exc).__name__}"
            yield ErrorEvent(reply)
        finally:
            if worker.is_alive():
                if outcome == "abandoned":
                    self.cancel()
                ask = self._ask_open
                if ask is not None:
                    ask.done.set()
                worker.join(WORKER_JOIN_S)
            self._end(req, outcome=outcome, reply=reply)

    def _begin(self, request: str, user_words: Any, channel: str) -> None:
        """Reset the per-request state (``loop.Agent.run``'s opening). A worker of a request the caller dropped that is
        still finishing its step (it was cancelled) is waited for first, so none of its events reach this request."""
        old = getattr(self, "_worker", None)
        if isinstance(old, threading.Thread) and old.is_alive():
            old.join(WORKER_JOIN_S * 6)
            if old.is_alive():
                self.logger.log("error", where="brain_worker_still_running")
        self._cancelled.clear()
        self.cancel_reason = CANCELLED
        self._watch_off()
        self._paused.clear()
        self._pause_wake.clear()
        self._pauses, self._paused_s, self._stopped_paused, self._pause_why = 0, 0.0, False, ""
        self._pause_end_logged = self._pause_end_told = False
        self._pause_closing = False
        self._pause_quiet_until, self._resume_asked = 0.0, False
        stale = self.take_steers()
        if stale:
            self.logger.log("steer", count=len(stale), dropped="arrived between requests")
        with self._steer_lock:
            self._swaps, self._swaps_all = [], []
        self._q = queue.Queue()
        self._pending_answer = None
        self._awaiting_answer = False
        self._ask_open = None
        self._send_stopped = None
        self._request_started = time.monotonic()
        self._requests += 1
        self._running = True
        self._status_plan, self._status_done, self._status_verb = None, 0, ""
        self._state = None
        self._bs = None
        self._req = None
        self._host_ref = None
        self._checker = None
        self._checks_seen = 0
        self._final_text = ""
        self.needed_hands = False
        self._reset_accounting()
        self.last_summary = None

    # -- the worker ---------------------------------------------------------------------------------------------------

    def _work(self, req: _Request) -> None:
        try:
            self._drive(req)
        except CallCancelled:
            self._push(self._cancel_event())
        except Exception as exc:  # noqa: BLE001 - the request ends with an error event, never a crash
            self.logger.log("error", where="brain_drive", error=type(exc).__name__)
            self._push(ErrorEvent(f"{type(exc).__name__}: {exc}"))
        finally:
            self._push(_END)

    def _drive(self, req: _Request) -> None:
        bs = self._bs = BrainSettings.read(self.settings)
        host = self._host_ref = Host(self, req)
        state = self._state = _State()
        from yuki.agent.untrusted import new_nonce

        early, early_words, early_nonce = self._take_early()
        said = "\n".join(req.words)
        if early is not None and " ".join(early_words.split()) != " ".join(said.split()):
            # read ahead for other words (or for a request that never came): none of it is this request's
            self.logger.log("brain_early", dropped="other words")
            early = None
        nonce = early_nonce if early is not None and early_nonce else new_nonce()
        self._set_skill_context(req, host)
        started = time.perf_counter()
        gathered = gather(dispatcher=self.dispatcher, memory=self.memory, settings=self.settings, words=said,
                          names=list(getattr(self.settings, "user_names", []) or []), limits=bs, nonce=nonce,
                          reuse=early)
        # M9: the people block carries the user's own last messages (and the chat in front): from the first call, an
        # outbound fetch goes through a plan step, where the consent rule and the commit check see it
        state.private_read = bool(gathered.people)
        self._gather_ms = (time.perf_counter() - started) * 1000
        self._menu_lines = gathered.menu_lines
        catalog = load_catalog() if gathered.skill_keys is not None else None
        sends = sends_fn(catalog)
        conv = Conversation(request_turn(words=req.words, channel=req.channel, context=req.context,
                                         gathered=gathered, history=list(self._history), seed=req.seed, limits=bs,
                                         nonce=nonce))
        system = system_blocks(gathered.profile, gathered.menu)
        effort = bs.effort_for(req.channel)
        self._effort = effort
        self.logger.log("brain_request", channel=req.channel, effort=effort, model=bs.model, seeded=req.seed is not None,
                        early=early is not None, words=len(req.words), context_chars=len(req.context),
                        overview_chars=len(gathered.overview), you_chars=len(gathered.you),
                        people_chars=len(gathered.people), profile_chars=len(gathered.profile),
                        menu_chars=len(gathered.menu), skills=len(gathered.skill_keys or ()),
                        private_read=state.private_read, gather_ms=round(self._gather_ms, 1),
                        parts_ms=gathered.parts_ms)
        host.nonce = nonce
        executor = self._executor_for(host, req)
        host.desk = getattr(executor, "lease", None)
        try:
            self._loop(req, host, state, bs, conv, system, effort, gathered, sends, executor, nonce)
        finally:
            closer = getattr(executor, "close", None)
            if callable(closer):
                try:
                    ended = ("cancelled" if self._cancelled.is_set() else "final" if self._final_text else "error")
                    closer(reply=self._final_text, outcome=ended)
                except Exception:  # noqa: BLE001
                    pass
            host.acting_end()

    def _set_skill_context(self, req: _Request, host: Host) -> None:
        """What the Dispatcher's ``run_skill`` needs (``loop.Agent._set_skill_context``): memory, the user's own words,
        the request id, the log, the settings."""
        try:
            self.dispatcher.skill_context = {
                "memory": self.memory if getattr(self.memory, "installed", False) else None,
                "words": host.user_words, "request_id": req.request_id, "log": self.logger.log,
                "settings": self.settings}
        except Exception:  # noqa: BLE001
            pass

    def _budget_left(self, state: _State, bs: BrainSettings, req: _Request) -> bool:
        """Act plans: at most ``act_calls`` model calls; answers: ``answer_calls`` or ``answer_s`` of work."""
        if state.kind == "act":
            return state.calls < bs.act_calls
        work_s = time.monotonic() - req.started - self._waited_s
        return state.calls < bs.answer_calls and work_s < bs.answer_s

    def _loop(self, req: _Request, host: Host, state: _State, bs: BrainSettings, conv: Conversation,
              system: list[dict[str, Any]], effort: str, gathered: Gathered, sends: Any, executor: Any,
              nonce: str) -> None:
        while True:
            if self._cancelled.is_set():
                self._push(self._cancel_event())
                return
            if not self._budget_left(state, bs, req):
                self._finish_stuck(state, budget=True)
                return
            plan_id = f"p{state.plans + 1}"
            checks = Checks(plan_id=plan_id, channel=req.channel, seed=req.seed, skills=gathered.skill_keys,
                            sends=sends, failed_step=state.failed_step, repeat_rejected=state.repeat_rejected,
                            current_plan=state.current.id if state.current is not None else None)
            params = build_params(model=bs.model, max_tokens=state.max_tokens or bs.max_tokens, system=system,
                                  messages=conv.messages(), effort=effort, thinking_display=bs.thinking_display,
                                  stream=bs.stream, eager=bs.eager)
            call = ModelCall(self.client, params, checks=checks, emit=self._push, cancelled=self._cancelled.is_set,
                             log=self.logger.log, voice=req.channel == "voice", stream=bs.stream)
            state.calls += 1
            result: ExecResult | None = None
            try:
                call.open()
                tool = call.next_tool()
                if tool is not None and tool.name == PLAN_TOOL and call.plan is not None:
                    state.plans += 1
                    result = self._run_plan(call.plan, executor, host, state, req, nonce)
                resp = call.finish()
            except CallCancelled:
                call.close()
                self._push(self._cancel_event())
                return
            except Exception:
                call.close()
                raise
            state.repeat_rejected = checks.repeat_rejected
            self._record_call(resp, call, exec_s=self._last_exec_s if result is not None else 0.0)
            if result is not None and _acted(result):
                state.acted = True
            if resp.broken or resp.stop_reason == "max_tokens":
                if self._after_cutoff(call, result, resp, state, bs, conv, host):
                    return
                continue
            if resp.stop_reason == "refusal":
                self._final(LINE_REFUSAL, self._outcome(state, level="failed" if state.acted else "answer",
                                                        line=LINE_REFUSAL))
                return
            conv.add_assistant(resp.content)
            extra = self._extra_tools(resp)
            if resp.tool is None:
                if self._finish_text(resp.text, state, bs, conv, host):
                    return
                continue
            if resp.tool.name == PLAN_TOOL:
                done = self._after_plan(call.plan, result, resp, state, bs, conv, extra, host)
                if done:
                    return
                continue
            if resp.tool.bad_input:
                # eager streaming: the input did not parse strictly (the SDK may have cut it short): never run
                conv.add_user([tool_result(resp.tool.id, BAD_INPUT_TEXT, error=True), *extra,
                               *self._steer_turn(host)])
                continue
            run = self._run_tool(resp.tool.name, resp.tool.input, host, state, gathered, nonce)
            conv.add_user([tool_result(resp.tool.id, run.text, error=not run.ok), *extra,
                           *self._steer_turn(host)])

    def _after_cutoff(self, call: ModelCall, result: ExecResult | None, resp: CallResult, state: _State,
                      bs: BrainSettings, conv: Conversation, host: Host) -> bool:
        """A response cut off at ``max_tokens``, or with a tool input the stream could not read: it is never appended
        (its tool use has no whole input). It is asked again once, with more room. When steps of its plan were already
        handed on (only non-committing ones run before a plan is complete), the model is told which, in a code-written
        user turn of its own (append-only), so the re-plan does not repeat them. A second cut-off ends with the stuck
        line. ``True`` when the request ended."""
        state.cutoffs += 1
        source = call.plan
        taken = source.taken if source is not None else 0
        ran = taken > 0 or (result is not None and _acted(result))
        self.logger.log("brain_reissue", why="broken" if resp.broken else "max_tokens", acted=state.acted, ran=ran,
                        taken=taken, n=state.cutoffs)
        if ran:
            plan = source.plan if source is not None else None
            if plan is not None:
                state.current = plan
            if result is not None:
                state.last = result
        if state.cutoffs > MAX_CUTOFFS:
            self._finish_stuck(state)
            return True
        now = state.max_tokens or bs.max_tokens
        ceiling = CUTOFF_TOKENS_STREAMED if bs.stream else CUTOFF_TOKENS_PLAIN
        state.max_tokens = max(now, min(ceiling, now * 2))
        if ran:
            report = cutoff_report(source.plan if source is not None else None,
                                   result.statuses if result is not None else [], taken=taken)
            conv.add_user([{"type": "text", "text": report}, *self._steer_turn(host)])
        return False

    # -- one plan -----------------------------------------------------------------------------------------------------

    def _run_plan(self, source: PlanStream, executor: Any, host: Host, state: _State, req: _Request,
                  nonce: str) -> ExecResult:
        """Hand the streaming plan to the executor (or, without one, run an answer plan's reads here and refuse any
        act step: fail closed). Returns what it reported."""
        self._last_exec_s = 0.0
        header = source.wait_header()   # the plan's kind decides who runs it
        if header is None:
            source.complete()
            return ExecResult(status="invalid")
        self._status_plan = header
        if header.kind == "act" and not self.hands:
            # a lane that only looks: nothing acts here; the runtime hands the request to the lane that acts
            source.complete()
            self.needed_hands = True
            self.logger.log("brain_needs_hands", lane=self.lane)
            return ExecResult(status="refused", reason="needs_hands", line=LINE_NEEDS_HANDS, final=True)
        if header.kind == "act":
            state.kind = "act"
        if header.kind == "ask":
            plan = source.complete()
            if plan is None:
                return ExecResult(status="invalid")
            self._status_plan = plan
            answer = host.ask_text(plan.question or "")
            return ExecResult(status="read", reads=[answer_report(plan.question or "", answer)], reason="asked")
        if executor is None:
            return self._run_reads_here(source, host, state, nonce)
        started = time.perf_counter()
        try:
            got = self._run_executor(executor, source, host)
        except CallCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the seam failed: nothing more runs for this plan
            self.logger.log("error", where="executor", error=type(exc).__name__)
            got = ExecResult(status="error", reason="the executor failed")
        self._last_exec_s = time.perf_counter() - started
        self._tool_s += self._last_exec_s
        result = _as_result(got)
        plan = source.plan
        if plan is not None:
            self._status_plan = plan
        opened = source.opened_at
        if source.header_at is not None and state.header_ms is None and opened:
            state.header_ms = (source.header_at - opened) * 1000
        if source.first_step_at is not None and state.step_ms is None and opened:
            state.step_ms = (source.first_step_at - opened) * 1000
        state.steps += source.taken
        state.check_calls += int(result.check_calls or 0)
        if result.verdict is not None or result.probe:
            state.verdict, state.probe = result.verdict, result.probe or state.probe
        self.logger.log("brain_plan", status=result.status, step=result.step, acted=result.acted,
                        verdict=result.verdict, probe=result.probe or None, stop=source.stop,
                        taken=source.taken, errors=len(source.errors),
                        **({"facts": plan.facts()} if plan is not None else {}))
        return result

    def _executor_for(self, host: Host, req: _Request) -> Any:
        """bExec's executor for this request: ``Executor(host, checker=CommitCheck(...), plan_seed=seed)``, one per
        request, so the desk lease and the commit-check memo span replans. The commit check gets the failover client
        (made on first use) and the session log. ``None`` when it can't be made (acting then fails closed)."""
        try:
            from yuki.brain.check import CommitCheck
            from yuki.brain.executor import Executor
        except Exception as exc:  # noqa: BLE001 - a broken install: answers still run, acting fails closed
            self.logger.log("error", where="executor_import", error=type(exc).__name__)
            return None
        try:
            self._checker = CommitCheck(lambda: self.client, self.settings, self.logger.log)
            self._checks_seen = 0
            return Executor(host, checker=self._checker, plan_seed=req.seed)
        except Exception as exc:  # noqa: BLE001
            self.logger.log("error", where="executor_init", error=type(exc).__name__)
            return None

    def _run_executor(self, executor: Any, source: PlanStream, host: Host) -> ExecResult:
        """The one call into bExec's executor: its streaming intake on this plan's stream. ``begin(header)`` once the
        header is validated (``_run_plan`` waited for it), ``add_step`` for each step the moment it closes (a result
        back stops the intake: a steer, a cancel, a mismatch), then ``finish(plan, stop_reason=)`` when the block is
        complete. Its report comes back as an :class:`ExecResult`."""
        header = source.header()
        if header is None:
            source.complete()
            return ExecResult(status="invalid")
        # the user's own words that reached this request outside the executor (an answer to the planner's question, a
        # steer taken into a turn) count as theirs for the consent rule's spans and the commit check
        known = set(getattr(executor, "words", []) or [])
        executor.add_words([w for w in host.user_words if w not in known])
        executor.begin(header)
        early = None
        for step in source.steps():
            early = executor.add_step(step)
            if early is not None:
                break
        if early is not None:
            got = executor.finish()
        else:
            plan = source.complete() if source.stop is None else None
            if plan is not None:
                got = executor.finish(plan)
            elif source.stop in ("max_tokens", "refusal"):
                got = executor.finish(stop_reason=source.stop)   # nothing further runs
            elif source.stop == "cancelled":
                return ExecResult(status="stopped", reason="cancelled",
                                  statuses=[_step_status(s) for s in getattr(executor, "statuses", [])])
            else:   # invalid: the steps already run are reported, the rest never runs
                statuses = list(getattr(executor, "statuses", []) or [])
                return ExecResult(status="invalid", statuses=[_step_status(s) for s in statuses],
                                  acted=any(s.tool and s.verb in ACT_VERBS for s in statuses))
        return self._exec_result(got, host)

    def _exec_result(self, got: Any, host: Host) -> ExecResult:
        """bExec's ``RunResult`` as bCore's :class:`ExecResult` (statuses and code words mapped; the done-check's
        verdict, probe and line; the commit checks this plan made)."""
        calls = int(getattr(self._checker, "calls", 0) or 0)
        checks, self._checks_seen = max(0, calls - self._checks_seen), calls
        raw = list(getattr(got, "statuses", None) or [])
        reads = [str(r.get("text") or "") for r in getattr(got, "reads", None) or []
                 if isinstance(r, Mapping) and str(r.get("text") or "").strip()]
        found = getattr(got, "done", None)
        base: dict[str, Any] = {
            "statuses": [_step_status(s) for s in raw], "reads": reads,
            "acted": any(getattr(s, "tool", "") and getattr(s, "verb", "") in ACT_VERBS for s in raw),
            "verdict": getattr(found, "verdict", None) if found is not None else None,
            "probe": str(getattr(found, "probe", "") or "") if found is not None else "",
            "line": str(getattr(got, "line", "") or ""), "check_calls": checks,
            "outcome": getattr(got, "outcome", None), "final": bool(getattr(got, "final", False))}
        status = str(getattr(got, "status", "") or "error")
        if status == "done":
            # "done" only when the check saw it; any other reading is said as not confirmed, with the executor's line
            return ExecResult(status="done" if base["verdict"] is True else "unconfirmed", **base)
        if status == "answer":
            return ExecResult(status="read", **base)
        if status == "mismatch":
            m = getattr(got, "mismatch", None)
            if m is None:
                return ExecResult(status="mismatch", reason="mismatch", **base)
            return ExecResult(status="refused" if m.refused else "mismatch",
                              step=None if m.verb == "done" else m.step,   # the final check is no step to avoid
                              expected=m.expected, seen=m.seen, view=m.view, reason=m.why or "mismatch",
                              seed=dict(m.seed) if m.seed else None,
                              seen_fenced=bool(getattr(m, "seen_fenced", False)), **base)
        if status == "steer":
            return ExecResult(status="steered", reason="the user added words", **base)
        if status == "declined":
            return ExecResult(status="declined", **base)
        if status == "refused":
            return ExecResult(status="refused", reason="refused", **base)
        if status == "busy":
            return ExecResult(status="refused", reason="busy", **{**base, "final": True})
        if status in ("cancelled", "stopped"):
            return ExecResult(status="stopped", reason=status, **base)
        if status == "ask":   # the Brain asks an ask plan's question itself; kept for safety
            question = str(getattr(got, "line", "") or "")
            answer = host.ask_text(question) if question else None
            return ExecResult(status="read", reads=[answer_report(question, answer)], reason="asked",
                              statuses=base["statuses"], check_calls=checks)
        return ExecResult(status="error", reason=status, **base)

    def _run_reads_here(self, source: PlanStream, host: Host, state: _State, nonce: str) -> ExecResult:
        """Without an executor: an answer plan's reads, in parallel; an act step refuses the plan (nothing acts)."""
        steps: list[Step] = []
        for step in source.steps():
            if step.verb in ACT_VERBS:
                self.logger.log("brain_no_executor", verb=step.verb)
                return ExecResult(status="refused", reason="no_executor",
                                  statuses=[StepStatus(s.index, s.verb, "not_run") for s in steps]
                                  + [StepStatus(step.index, step.verb, "refused", "no_executor")])
            steps.append(step)
        plan = source.complete()
        if plan is None:
            return ExecResult(status="invalid", statuses=[StepStatus(s.index, s.verb, "not_run") for s in steps])
        runs: list[ToolRun | None] = [None] * len(plan.steps)

        def one(i: int, step: Step) -> None:
            runs[i] = self._read_step(step, host, state, nonce)

        threads = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(plan.steps)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60.0)
        statuses, reads = [], []
        for step, run in zip(plan.steps, runs):
            ok = run is not None and run.ok
            statuses.append(StepStatus(step.index, step.verb, "ok" if ok else "failed"))
            if run is not None:
                reads.append(run.text)
        return ExecResult(status="read", statuses=statuses, reads=reads)

    def _read_step(self, step: Step, host: Host, state: _State, nonce: str) -> ToolRun:
        """One read step run here (no executor): ``look`` / ``memory`` / ``fetch`` / ``wait``."""
        from yuki.brain.plan import _json_object

        words = "\n".join(host.user_words)
        if step.verb == "look":
            args = {"what": step.value, "window": step.hwnd}
            run = run_look(self.dispatcher, args, nonce=nonce)
        elif step.verb == "memory":
            run = run_memory(self.memory, _json_object(step.value) or {}, words=words, nonce=nonce,
                             menu=getattr(self, "_menu_lines", ""))
        elif step.verb == "fetch":
            value = (step.value or "").strip()
            web = value.lower().startswith(("http://", "https://"))
            if web and state.private_read:
                return ToolRun("fetch", "fetch_url", False, "Not run: an outbound read after a private one goes "
                                                            "through the executor's check.")
            run = run_fetch(self.dispatcher, {"url": value} if web else {"path": value}, nonce=nonce)
        else:
            seconds = min(10.0, max(0.0, float(step.value or 0)))
            self._cancelled.wait(seconds)
            run = ToolRun("wait", "wait", True, f"Waited {seconds:g} s.")
        self._emit_run(run)
        if run.private:
            state.private_read = True
        return run

    def _after_plan(self, source: PlanStream | None, result: ExecResult | None, resp: CallResult, state: _State,
                    bs: BrainSettings, conv: Conversation, extra: list[dict[str, Any]], host: Host) -> bool:
        """What a plan's report means (design 2.3); ``True`` when the request ended."""
        tool_id = resp.tool.id if resp.tool is not None else ""
        if source is None or result is None:
            conv.add_user([tool_result(tool_id, invalid_report(["the plan could not be read"]), error=True), *extra])
            return False
        if source.stop == "invalid" or result.status == "invalid":
            state.invalid += 1
            if state.invalid > 1:
                self._finish_stuck(state)
                return True
            conv.add_user([tool_result(tool_id, invalid_report(source.errors or ["invalid plan"], result.statuses),
                                       error=True), *extra])
            return False
        plan = source.plan
        if plan is not None:
            state.current = plan
        state.last = result
        status = result.status
        if status != "read":
            state.held = None   # an act plan ran after a held ending: its own result decides now (reads don't)
        if status == "done" and not result.line:
            # a done must carry the executor's line from the probe's evidence (design 2.4): never a generic claim
            self.logger.log("brain_done_without_line", probe=result.probe or None)
            given = dict(result.outcome) if isinstance(result.outcome, Mapping) else None
            if given is not None:
                given.update(level="unverified", label="", line=LINE_UNCONFIRMED)
            result = state.last = replace(result, status="unconfirmed", outcome=given)
            status = "unconfirmed"
        if status in ENDINGS:
            line = result.line or {"unconfirmed": LINE_UNCONFIRMED, "drafted": LINE_DRAFTED,
                                   "declined": LINE_DECLINED}[status]
            outcome = self._result_outcome(result, state, plan, line)
            if self._steer_waiting() and self._budget_left(state, bs, self._req):
                # the user added something before the line went out: it is held (loop.Agent's held final) and the
                # model takes one more step with their words; the held line stands if nothing changes
                state.held = (line, outcome)
                self.logger.log("steer", held_final=status)
                conv.add_user([tool_result(tool_id, held_report(result, plan, line)), *extra,
                               *self._steer_turn(host)])
                return False
            self._final(line, outcome)
            return True
        if result.final and result.line and status in ("refused", "stopped") and not self._cancelled.is_set():
            # the executor ends the run here with its own line (a game in front: "after the round?"; the desk busy):
            # no replan routes around it
            self._final(result.line, self._result_outcome(result, state, plan, result.line))
            return True
        if status == "stopped":
            self._push(self._cancel_event())
            return True
        if status == "read":
            text = result.reads[0] if result.reason == "asked" and result.reads else read_report(result)
            conv.add_user([tool_result(tool_id, text), *extra, *self._steer_turn(host)])
            return False
        if result.reason == "no_executor":
            self._final(LINE_NO_EXECUTOR, self._outcome(state, level="answer", line=LINE_NO_EXECUTOR))
            return True
        # mismatch, refused, steered, error: revise, within the budget. A revision with the user's own words is not a
        # replan after a mismatch (the executor's rule, design 2.3): only the call budget bounds it
        counted = status != "steered"
        if (counted and state.replans >= bs.max_replans) or not self._budget_left(state, bs, self._req):
            self._finish_stuck(state)
            return True
        if counted:
            state.replans += 1
        if isinstance(result.step, int) and plan is not None and 0 <= result.step < len(plan.steps) \
                and status in ("mismatch", "refused", "error"):
            state.failed_step = plan.steps[result.step]
        left = bs.max_replans - state.replans
        blocks: list[dict[str, Any]] = []
        if result.reads:
            # what the plan's reads returned before it stopped (each fenced by the executor), so the revision uses it
            blocks.append({"type": "text", "text": f"{CHECK_LABEL} What that plan's reads returned:\n\n"
                                                   + "\n\n".join(result.reads)})
        if result.seed and self._req is not None:
            # rung C: the skill handed over; the next plan is seeded with it and keeps the skill's done-check
            self._req.seed = dict(result.seed)
            seeded = seed_block(result.seed, limit=bs.seed_chars)
            if seeded:
                blocks.append({"type": "text", "text": seeded})
        conv.add_user([tool_result(tool_id, step_report(result, plan, limit=bs.result_chars, replans_left=left)),
                       *extra, *blocks, *self._steer_turn(host)])
        return False

    def _steer_turn(self, host: Host) -> list[dict[str, Any]]:
        """What the user added mid-task, taken now: their own words for the next call (and for the consent rule), and
        any countdown chip they tapped, as a code-written fact (never quotable words)."""
        texts = self.take_steers()
        swaps = self._take_swaps()
        if not texts and not swaps:
            return []
        if texts:
            self.logger.log("steer", count=len(texts))
            host.user_words.extend(texts)
        if swaps:
            self.logger.log("send_swap_told", count=len(swaps))
        return [*swap_blocks(swaps), *steer_blocks(texts)]

    def _extra_tools(self, resp: CallResult) -> list[dict[str, Any]]:
        """A second tool use in one response (asked for one): answered as an error, never run."""
        out = []
        first = resp.tool.id if resp.tool is not None else None
        for block in resp.content or []:
            if getattr(block, "type", None) == "tool_use" and str(getattr(block, "id", "")) != first:
                out.append(tool_result(str(block.id), "Not run: one tool per turn.", error=True))
        return out

    # -- standalone tools ---------------------------------------------------------------------------------------------

    def _run_tool(self, name: str, tool_input: Mapping[str, Any], host: Host, state: _State, gathered: Gathered,
                  nonce: str) -> ToolRun:
        """A tool called outside a plan (reads, a question, a memory write); act steps run only in a plan."""
        words = "\n".join(host.user_words)
        started = time.perf_counter()
        if name == "look":
            run = run_look(self.dispatcher, tool_input, nonce=nonce)
        elif name == "memory":
            run = run_memory(self.memory, tool_input, words=words, nonce=nonce, menu=gathered.menu_lines)
        elif name == "fetch":
            if tool_input.get("url") and state.private_read:
                # M9: an address after a file, clipboard or memory read can carry it out: only in a plan step, where the
                # executor's consent rule and commit check see it
                run = ToolRun("fetch", "fetch_url", False, "Not run: put this fetch in a plan step (after a private "
                                                           "read it goes through Yuki's check).")
            else:
                run = run_fetch(self.dispatcher, tool_input, nonce=nonce)
        elif name == "ask_user":
            question = " ".join(str(tool_input.get("question") or "").split())
            answer = host.ask_text(question) if question else None
            run = ToolRun("ask_user", "ask_user", answer is not None, answer_report(question, answer))
        elif name == "remember":
            run = run_remember(self.memory, tool_input, words=words)
        else:
            run = ToolRun(name, name, False, f"No tool {name!r}: use plan, look, memory, fetch, ask_user or remember.")
        if name != "ask_user":
            self._emit_run(run)
        if run.private:
            state.private_read = True
        self._tool_s += time.perf_counter() - started
        return run

    def _emit_run(self, run: ToolRun) -> None:
        about = {"what": STEP_WORDS.get(run.name, run.name)}
        self._tool_counts[run.ran] += 1
        self._push(ToolCall(run.ran, {**run.input, "_about": about}))
        summary = one_line(run.text, 120) if not run.ok else "ok"
        self._push(ToolResult(run.ran, run.ok, summary, payload=run.payload, about=about))
        self.logger.log("brain_tool", tool=run.ran, ok=run.ok, chars=len(run.text), ms=round(run.ms, 1))

    # -- endings ------------------------------------------------------------------------------------------------------

    def _final(self, text: str, outcome: dict[str, Any]) -> None:
        self._last_outcome = outcome
        self._final_text = str(text or "")
        self._push(Final(text, outcome=outcome))

    def _finish_text(self, text: str, state: _State, bs: BrainSettings, conv: Conversation, host: Host) -> bool:
        """A plain-text reply (already appended). If the user added something while it was written, it is not the last
        word: their words go in and the model takes one more step, within the budget (``loop.Agent``'s held reply).
        Otherwise: an answer when nothing acted; after an act plan, the model's words never claim a result: a held
        ending's code line stands, else code says where it stopped (design 2.2, 2.4). ``True`` when the request
        ended."""
        if self._steer_waiting() and self._budget_left(state, bs, self._req):
            self.logger.log("steer", held_final="reply")
            conv.add_user([{"type": "text", "text": f"{CHECK_LABEL} Your reply above was not sent: the user added this "
                                                    f"before it went out. Take it into account, then reply again (or "
                                                    f"plan, if it asks for something to be done)."},
                           *self._steer_turn(host)])
            return False
        text = (text or "").strip()
        last = state.last
        if state.held is not None:
            # a plan ended and its line was held for the user's addition; the model changed nothing: the line stands
            # when something acted (code's line, never the model's claim), else the model's answer to it
            if state.acted or last is None or last.status != "declined":
                self._final(*state.held)
                return True
        if last is not None and last.line and last.status in ("refused", "mismatch", "error"):
            # the executor's own line for why it stopped (a refusal's "after the round?") wins over the model's words
            self._final(last.line, self._outcome(state, level="failed" if state.acted else "answer", line=last.line))
            return True
        if state.acted:
            self._finish_stuck(state)
            return True
        self._final(text or LINE_NO_REPLY, self._outcome(state, level="answer"))
        return True

    def _finish_stuck(self, state: _State, *, budget: bool = False) -> None:
        """The code line for an act plan that could not get there, or an answer that ran out of budget. A held ending
        (no plan ran after it) is said as it was."""
        if state.held is not None:
            self._final(*state.held)
            return
        if state.kind != "act" and not state.acted:
            line = LINE_ANSWER_BUDGET if budget else LINE_NO_REPLY
            self._final(line, self._outcome(state, level="answer", line=line))
            return
        last, plan = state.last, state.current
        where, why = "", ""
        if last is not None and isinstance(last.step, int) and plan is not None and 0 <= last.step < len(plan.steps):
            where = STEP_WORDS.get(plan.steps[last.step].verb, "")
        if last is not None and last.reason:
            why = one_line(last.reason, 100)
        line = (last.line if last is not None and last.line and last.status in ("mismatch", "refused") else
                LINE_STUCK.format(where=where or "partway", why=f": {why}" if why else ""))
        self._final(line, self._outcome(state, level="failed" if state.acted else "answer", line=line,
                                        partial=state.acted))

    def _outcome(self, state: _State, *, level: str, line: str = "", partial: bool = False,
                 seen: str = "", send: str | None = None) -> dict[str, Any]:
        """The ``Final.outcome`` dict (``finish.outcome_verdict``'s keys)."""
        plan = state.current
        steps = len(plan.steps) if plan is not None and plan.kind == "act" else 0
        last = state.last
        not_ok = sum(1 for s in (last.statuses if last is not None else []) if s.state not in ("ok", "drafted"))
        return {"level": level, "label": LEVEL_LABELS.get(level, ""), "acted": bool(state.acted), "seen": seen,
                "send": send, "partial": bool(partial), "line": line,
                "step": STEP_WORDS.get(plan.steps[-1].verb, "") if steps else "", "steps": steps,
                "open": 0 if level in ("verified", "answer") else not_ok}

    def _result_outcome(self, result: ExecResult, state: _State, plan: Plan | None, line: str) -> dict[str, Any]:
        """The executor's outcome dict when it gave one (its keys filled in), else one from its status."""
        if isinstance(result.outcome, Mapping):
            given = {k: result.outcome.get(k) for k in OUTCOME_KEYS}
            out = {**self._outcome(state, level=str(given.get("level") or "unverified")),
                   **{k: v for k, v in given.items() if v is not None}}
            if not out.get("line") and out.get("level") != "verified":
                out["line"] = line
            out["label"] = out.get("label") or LEVEL_LABELS.get(str(out.get("level")), "")
            return {**dict(result.outcome), **out}
        status = result.status
        level = {"done": "verified", "unconfirmed": "unverified", "drafted": "likely", "declined": "answer"}.get(status)
        if level is None:
            level = "failed" if state.acted else "answer"
        send = None
        if plan is not None and plan.consent.commit == "send":
            send = "confirmed" if status == "done" else "unconfirmed" if state.acted else None
        return self._outcome(state, level=level, line="" if level == "verified" else line,
                             seen=result.probe or "", send=send)

    def _cancel_event(self) -> ErrorEvent:
        """The cancel line (``loop.Agent._cancel_event``'s words: the runtime and the voice read them)."""
        if self.cancel_reason == PARKED:
            self.logger.log("parked")
            return ErrorEvent(LINE_PARKED)
        if self.cancel_reason == WITHDRAWN:
            self.logger.log("withdrawn")
            return ErrorEvent(LINE_WITHDRAWN)
        if self._stopped_paused or self._paused.is_set():
            self.logger.log("error", where="paused_stop")
            return ErrorEvent(PAUSE_STOP_LINE)
        message = LINE_CANCELLED
        if self._send_stopped is not None:
            message += (" The message was not sent: it is still in the box." if self._send_stopped
                        else " The message was not sent.")
        self.logger.log("error", where="cancelled")
        return ErrorEvent(message)

    # -- accounting (copied from loop.Agent) --------------------------------------------------------------------------

    def _reset_accounting(self) -> None:
        self._model_s = 0.0
        self._tool_s = 0.0
        self._waited_s = 0.0
        self._gather_ms = 0.0
        self._tool_counts: Counter[str] = Counter()
        self._tokens: dict[str, int] = dict.fromkeys(TOKEN_KEYS, 0)
        self._cost_usd = 0.0
        self._unpriced: set[str] = set()
        self._models_used: list[str] = []
        self._call_stats: list[dict[str, Any]] = []
        self._extra_calls: Counter[str] = Counter()
        self._last_outcome: dict[str, Any] | None = None
        self._effort = ""

    def _account(self, model: str, usage: Any, *, ms: float = 0.0, role: str = "planner") -> None:
        tokens = usage_tokens(usage)
        for key in TOKEN_KEYS:
            self._tokens[key] += tokens[key]
        if model and model not in self._models_used:
            self._models_used.append(model)
        cost = None
        try:
            cost = self.settings.estimate_cost(model, tokens)
        except Exception:  # noqa: BLE001
            cost = None
        if cost is None:
            self._unpriced.add(model)
        else:
            self._cost_usd += cost
        try:
            self.logger.usage.add(usage, latency_ms=ms)
        except Exception:  # noqa: BLE001
            pass
        if role != "planner":
            self._extra_calls[role] += 1

    def _record_call(self, resp: CallResult, call: ModelCall, *, exec_s: float = 0.0) -> None:
        """One planner call's accounting (its time without the steps the executor ran while it streamed)."""
        model = self._used_model(resp.model)
        self._model_s += max(0.0, resp.ms / 1000 - exec_s)
        self._account(model, resp.usage, ms=resp.ms, role="planner")
        used = usage_tokens(resp.usage)
        self._call_stats.append({"out": used["output_tokens"], "in": used["input_tokens"],
                                 "cache_read": used["cache_read_tokens"], "cache_write": used["cache_write_tokens"],
                                 "tool": resp.tool.name if resp.tool is not None else None,
                                 "first_token_ms": None if resp.first_token_ms is None else round(resp.first_token_ms, 1),
                                 "ms": round(resp.ms, 1), "stop": resp.stop_reason})
        self.logger.log("brain_call", model=model, stop_reason=resp.stop_reason, broken=resp.broken,
                        tool=resp.tool.name if resp.tool is not None else None, ms=round(resp.ms, 1),
                        first_token_ms=None if resp.first_token_ms is None else round(resp.first_token_ms, 1),
                        tokens=used, text_chars=len(resp.text or ""))

    def _run_fields(self, req: _Request) -> dict[str, Any]:
        state = self._state
        return {"source": "brain", "session_id": getattr(self.memory, "session_id", None),
                "request_id": f"{self.lane}-{req.number}", "log_request_id": req.request_id,
                "lane": "voice" if req.channel == "voice" else self.lane, "at": req.epoch,
                "words": req.shown, "model_calls": state.calls if state is not None else 0,
                "cost_usd": None if self._unpriced else round(self._cost_usd, 6),
                "wall_s": round(time.monotonic() - req.started, 2),
                "added": self._host_words(req)[len(req.words):]}

    def _host_words(self, req: _Request) -> list[str]:
        host = getattr(self, "_host_ref", None)
        return list(getattr(host, "user_words", req.words)) if host is not None else list(req.words)

    # -- the end of a request -----------------------------------------------------------------------------------------

    def _end(self, req: _Request, *, outcome: str, reply: str) -> None:
        """Housekeeping after the last event (``loop.Agent.run``'s finally): the pause closes, the marker is withdrawn,
        the input monitor is given back, the summary and the ledger line are written, the exchange goes to memory's
        turn log and to the follow-up history. Never raises."""
        try:
            self._watch_off()
            self._close_pause("finished" if outcome == "final" else "cancelled" if self._cancelled.is_set()
                              else "ended")
            self._acting_done()
            self._input_off()
            self.logger.usage_total()
            self._summarize(req, outcome=outcome)
            self._log_turn(req, reply=reply, outcome=outcome)
            if outcome == "final" and reply:
                self._history.append(Exchange(words=req.shown, answer=reply, at=req.epoch))
            if req.words:   # only the user's own words (never a request composed with a card's text)
                self._earlier_words.append("\n".join(req.words))
        except Exception as exc:  # noqa: BLE001
            self.logger.log("error", where="brain_end", error=type(exc).__name__)
        finally:
            self._running = False
            self._status_plan = None

    def _summarize(self, req: _Request, *, outcome: str) -> None:
        """``request_summary`` and the ``requests.csv`` line (the plan's columns go at the end: bWire adds their
        names; until then the ledger ignores them)."""
        try:
            state = self._state or _State()
            bs = self._bs or BrainSettings.read(self.settings)
            wall_s = time.monotonic() - req.started
            models = self._models_used or [bs.model]
            # the commit check's calls are priced by its own checker (bExec): they join the request's cost
            check_cost = float(getattr(self._checker, "cost_usd", 0.0) or 0.0)
            cost = None if self._unpriced else round(self._cost_usd + check_cost, 6)
            verdict = (self._last_outcome or {}).get("level")
            plan_facts = {
                "kind": state.kind if state.plans else None, "plans": state.plans, "replans": state.replans,
                "steps": state.steps, "probe": state.probe or None, "verdict": verdict,
                "done_verdict": state.verdict, "ms_to_plan": None if state.header_ms is None else round(state.header_ms, 1),
                "ms_to_step": None if state.step_ms is None else round(state.step_ms, 1), "calls": state.calls,
                "check_calls": state.check_calls + self._extra_calls.get("check", 0),
                "ask_calls": self._extra_calls.get("ask", 0), "invalid": state.invalid,
                "gather_ms": round(self._gather_ms, 1), "channel": req.channel}
            summary: dict[str, Any] = {
                "request": req.shown, "lane": self.lane, "outcome": outcome,
                "model": models[0] if len(models) == 1 else "+".join(models), "effort": self._effort or None,
                "started_at": req.started_at.isoformat(timespec="seconds"), "wall_s": round(wall_s, 2),
                "waiting_for_user_s": round(self._waited_s, 2), "model_s": round(self._model_s, 2),
                "tool_s": round(self._tool_s, 2), "model_calls": state.calls, "tool_calls": dict(self._tool_counts),
                "tokens": dict(self._tokens), "thinking_tokens": self.logger.usage.thinking_tokens,
                "cost_usd": cost, "unpriced_models": sorted(m for m in self._unpriced if m),
                "calls_detail": list(self._call_stats), "plan": plan_facts, "verdict": verdict, "brain": True,
            }
            self.last_summary = summary
            record = self.logger.request_summary(summary)
            request_id = ((record.get("request_id") if isinstance(record, dict) else None)
                          or getattr(self.logger, "last_request_id", None) or req.request_id)
            if self.record_requests:
                append_request_row(self.settings.requests_csv_path, {
                    "timestamp": summary["started_at"], "request_id": request_id, "session": self.logger.session_id,
                    "lane": self.lane, "request": summary["request"], "model": summary["model"],
                    "effort": summary["effort"], "wall_s": summary["wall_s"], "wait_s": summary["waiting_for_user_s"],
                    "model_s": summary["model_s"], "tool_s": summary["tool_s"], "calls": state.calls,
                    "tool_calls": summary["tool_calls"], **self._tokens, "cost_usd": cost, "outcome": outcome,
                    # the plan's columns (bWire appends their names to REQUEST_CSV_FIELDS)
                    "plan_kind": plan_facts["kind"], "plan_steps": state.steps, "replans": state.replans,
                    "probe": state.probe or None, "verdict": verdict, "plan_ms": plan_facts["ms_to_plan"],
                    "plan_calls": state.calls, "check_calls": plan_facts["check_calls"],
                })
        except Exception as exc:  # noqa: BLE001 - accounting never breaks a request
            self.logger.log("error", where="brain_summary", error=type(exc).__name__)

    def _log_turn(self, req: _Request, *, reply: str, outcome: str) -> None:
        """The exchange to memory's turn log (its background writer; the user's words, never the card's context)."""
        log_turn = getattr(self.memory, "log_turn", None)
        if not callable(log_turn):
            return
        logger = self.logger

        def done(report: dict[str, Any]) -> None:
            logger.log("memory_turn_logged", **{k: v for k, v in report.items() if k in ("row", "error", "elapsed_ms")})

        try:
            state = self._state or _State()
            actions = []
            if state.current is not None and state.current.kind == "act":
                actions = [f"{s.verb}" for s in state.current.steps]
            queued = log_turn(request_id=f"{self.lane}-{req.number}", at=req.epoch, user_text=req.shown,
                              reply_text=reply, actions=actions, outcome=outcome, on_done=done)
            self.logger.log("memory_turn_queued", queued=bool(queued), outcome=outcome, reply_chars=len(reply))
        except Exception as exc:  # noqa: BLE001
            self.logger.log("error", where="memory_turn", error=type(exc).__name__)


def _acted(result: ExecResult) -> bool:
    """Whether a plan's run did something on the PC: the executor says so, or an act step's status shows it ran (so a
    model's text after non-committing steps is never taken as the final word)."""
    return bool(result.acted) or any(s.verb in ACT_VERBS and s.state in ACTED_STATES for s in result.statuses)


def _step_status(s: Any) -> StepStatus:
    """One of bExec's step statuses as bCore's: ``pending`` -> ``not_run``; an ok draft -> ``drafted``; a failed act
    step that ran on the PC -> ``mismatch`` (it did something), one that never ran -> ``failed``; ``declined`` ->
    ``skipped`` (the user said no). The note is the executor's short reason (code words), never window text."""
    if isinstance(s, StepStatus):
        return s
    index = int(getattr(s, "index", 0) or 0)
    verb = str(getattr(s, "verb", "") or "")
    state = str(getattr(s, "state", "") or "pending")
    note = one_line(getattr(s, "why", "") or "", 200)
    if state == "pending":
        state = "not_run"
    elif state == "ok" and note == "drafted":
        state = "drafted"
    elif state == "failed" and getattr(s, "tool", "") and verb in ACT_VERBS:
        state = "mismatch"
    elif state == "declined":
        state, note = "skipped", "the user said no"
    elif state not in STEP_STATES:
        state = "failed"
    return StepStatus(index, verb, state, note)


def _as_result(got: Any) -> ExecResult:
    """The executor's report as an :class:`ExecResult` (it may return one, a dict, or an object with those fields)."""
    if isinstance(got, ExecResult):
        return got
    names = [f.name for f in dc_fields(ExecResult)]
    if isinstance(got, Mapping):
        data = {k: got.get(k) for k in names if k in got}
    else:
        data = {k: getattr(got, k) for k in names if hasattr(got, k)}
    data.setdefault("status", "error")
    statuses = []
    for s in data.get("statuses") or []:
        if isinstance(s, StepStatus):
            statuses.append(s)
        elif isinstance(s, Mapping):
            statuses.append(StepStatus(int(s.get("index", 0) or 0), str(s.get("verb") or ""),
                                       str(s.get("state") or "not_run"), str(s.get("note") or "")))
    data["statuses"] = statuses
    for key, default in (("seen", ""), ("view", ""), ("line", ""), ("probe", ""), ("reason", ""), ("expected", "")):
        data[key] = str(data.get(key) or default)
    data["reads"] = [str(r) for r in data.get("reads") or []]
    data["acted"] = bool(data.get("acted"))
    data["check_calls"] = int(data.get("check_calls") or 0)
    return ExecResult(**data)


__all__ = ["Brain", "Host", "CANCELLED", "WITHDRAWN", "PARKED", "PAUSE_STOP_LINE", "LEVEL_LABELS", "OUTCOME_KEYS",
           "STEP_WORDS", "with_watch_steps"]
