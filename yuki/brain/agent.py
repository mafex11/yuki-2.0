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
   "I got stuck at ...";
4. P1.3 (18 Oct free-thinker plan): a done whose acts took something the user had (the executor's after-look: a
   window that was in front, playing or holding the keyboard covered, the front taken, a pop-up left) gets one
   tidy-up turn AFTER its line: the line goes out at once (a :class:`Say`), then one planner call with the "After:"
   block and one principle, for at most two undoable window steps on what the request's own acts changed (place it,
   give the front back); the :class:`Final` is the line and what the tidy-up did (``outcome["said_first"]``: the part
   already said). Nothing flagged, or the user's tidy dial off: no turn, no cost.

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
    answer_part_report,
    answer_report,
    cutoff_report,
    gather,
    held_report,
    invalid_report,
    load_catalog,
    look_report,
    one_line,
    part_rest_report,
    read_card,
    read_menu,
    read_profile_parts,
    read_report,
    REST_DONE_WORD,
    request_turn,
    SEED_MEMORY_WAIT_S,
    seed_block,
    seed_memory,
    sends_fn,
    step_report,
    steer_blocks,
    swap_blocks,
    text_back_report,
    tidy_report,
    tool_result,
    unconfirmed_report,
)
from yuki.brain.done import with_parts
from yuki.brain.plan import (
    ACT_VERBS,
    PLAN_TOOL,
    Checks,
    ExecResult,
    Plan,
    STEP_STATES,
    Step,
    StepStatus,
    error_codes,
    seed_sends,
    seed_skill,
    skill_family,
)
from yuki.brain.planner import CallCancelled, CallResult, ModelCall, PlanStream, build_params
from yuki.brain.prompt import ANSWER_PROMPT, ANSWER_TOOL, profile_text, system_blocks
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
#: ``where``: " " and the step in plain words (" partway" when that step is unknown), or "" when nothing ran.
LINE_STUCK = "I got stuck{where}{why}. Try another way?"
#: A part's line, held while the planner did the rest of the request, when the budget or the replans ran out before
#: the planner said it was all done: what the part's check saw, and that the rest isn't.
LINE_REST_UNDONE = "{line}; I couldn't get the rest done."
#: The executor's own code words a stuck line may say, in plain words (x10). A consent refusal's code word is said only
#: in the consent rule's own plain words (``consent.plain_why``, x11), and any other reason is left out: a reason is a
#: code word (or an executor's note) for the logs and the planner, never speech (tonight's "...your usual way:
#: handed_over" and "...: quote").
REASON_WORDS: dict[str, str] = {
    "handed_over": "it didn't get there",
    "asked": "it needed a detail I didn't have",
    "chat_not_confirmed": "the chat didn't open",
}
#: aBrain (phase 2.3; 18 Oct live A: "I did it, but I couldn't confirm it worked." x5, every time the evidence said
#: more): a request that didn't end done says what the evidence holds (:func:`ending_line`, from the executor's
#: ``ending_facts``): what ran, what the check looked for and what it saw. ``{did}``: the last act step that ran, as
#: done; ``{claim}``: what the check looked for; ``{seen}``: what it saw; ``{stuck}``: the step that stopped it.
LINE_DID_UNCHECKED = "{did}, but I couldn't check it worked."
LINE_DID_UNREAD = "{did}, but I couldn't check that {claim}."
LINE_UNREAD = "I couldn't check that {claim}."
LINE_DID_NOT_SO = "{did}, but I couldn't confirm {claim}: {seen}."
LINE_STUCK_AFTER = "{did}, then got stuck {stuck}{why}. Try another way?"
#: The evidence the voice's talker words a non-success ending from (``outcome["evidence"]``), at most this long.
EVIDENCE_CHARS = 420
#: The outcome levels whose ending carries the evidence (with any ``partial`` one).
EVIDENCE_LEVELS = frozenset({"unverified", "failed"})
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
#: The ``outcome`` dict's keys (``finish.outcome_verdict``'s, design M7). A request that had a tidy-up turn adds
#: ``said_first``: the start of its Final already put out as a :class:`Say` (P1.3, :meth:`Brain._tidy_ending`).
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
#: sBrainFix (F9): how many act plans that only looked go back to the planner with what they read in one request (the
#: next one ends with its line, within the call budget either way).
LOOK_BACKS_MAX = 2
#: sBrainFix (F8): the outcome levels after which a seeded request's hand-off check is read once as an observer
#: (review r1 L6: also ``unverified`` when an act step of the request ran: the driver's own finish gate judges it).
OBSERVE_LEVELS: frozenset[str] = frozenset({"verified", "likely"})
OBSERVE_ACTED_LEVELS: frozenset[str] = frozenset({"unverified"})
#: pBrain (17 Oct e2e2 B14 / bRound2 B28 r0: [ctrl+t, open_url youtube.com, look] with done "YouTube's home page",
#: ended "Youtube's home page is open in Arc; I couldn't check the rest." and nothing put on): a steps plan that ended
#: on a look (the core prompt's own sign of a plan doing a part) and whose done read true while covering only part of
#: the target (level likely) goes back to the planner once with what the look read, its line held (``part_rest``);
#: each such turn adds PART_REST_CALLS to the act budget, as code's own question. (kBrain, 18 Oct: a skill route no
#: longer gets such a turn: a request of several parts is one plan with a part per skill, each checked by its own
#: skill, so a skill route is only ever a whole request, and its done line is said at once.)
PART_RESTS_MAX = 1
PART_REST_CALLS = 2
#: pBrain (17 Oct e2e2 B14 r2: the done read "another page is showing" and the planner replied "Tell me what you want
#: to watch and I'll put it on."): an act request not confirmed done whose planner answers in plain text gets one
#: more planner turn before it ends (:func:`text_back`), adding TEXT_BACK_CALLS to the act budget (code's question).
TEXT_BACKS_MAX = 1
TEXT_BACK_CALLS = 2
#: fBrain (18 Oct live A, "Pause the song bro, also what time's my meeting tomorrow": one act plan, a memory read and
#: the pause, ended on the pause's line and never answered the question): an act plan that ended and read memory or
#: fetched something in its steps gets one more planner turn with what those reads returned, its line held, to answer
#: what their words asked (:func:`answer_due`); the line is then said with the answer after it. Each such turn adds
#: ANSWER_PART_CALLS to the act budget (code's question).
ANSWER_PARTS_MAX = 1
ANSWER_PART_CALLS = 1
#: P1.3 (18 Oct free-thinker plan): a done act request whose acts took something the user had (the executor's
#: after-look) gets one tidy-up turn after its line is said (one per request: the turn's own acts are never looked at
#: again). The turn is exactly one model call (whatever comes back ends it); it adds TIDY_CALLS to the act budget
#: (code's question). No flag: no turn, no cost.
TIDY_CALLS = 1
#: The read verbs whose results are content for the user (a look reads the screen to check or to find a control).
ANSWER_READ_VERBS: frozenset[str] = frozenset({"memory", "fetch"})
#: pBrain (B35, bRound2 "Sending to हेमंत"): the bound on the people-index read that gives a spoken line memory's own
#: name for a send's recipient (:meth:`Brain._said_name`).
SAID_NAME_TIMEOUT_S = 0.8
#: bRound2 (B28): the user's own turns of this conversation the executor may find a step's app value in
#: (:meth:`Host.said_before`): memory's turn log of the last ``SAID_BEFORE_S`` (the 10 Oct YouTube-in-Arc task ran 17
#: minutes; "in arc not in Chrome" stood 4 minutes before "Open the fucking YouTube tab"), at most
#: ``SAID_BEFORE_TURNS`` turns, read within ``SAID_BEFORE_WAIT_S``. Only the turns the voice pipeline marked as said
#: to Yuki (``addressed`` ``yuki``; the 10 Oct copy: 37 such turns that evening, the rest Yuki's own lines with no
#: words): never a turn said to others in a call, and never a Brain request's own logged row (its text is the request
#: as it came, which a card may have composed; the Brain's own requests are its earlier words already).
SAID_BEFORE_S = 600.0
SAID_BEFORE_TURNS = 16
SAID_BEFORE_WAIT_S = 0.4
SAID_BEFORE_TO: tuple[str | None, ...] = ("yuki",)

#: kBrain: the planner's stable layers are kept an hour (``prompt.CACHE_CONTROL``): a warm-up is worth its call only
#: when no planner call or warm-up ran for this long (s), or none yet in this app run.
WARM_AFTER_S = 3000.0
#: The warm-up's one user turn (code's words; one token comes back and is dropped).
WARM_TEXT = "(Yuki is starting up: no request yet. Reply with one word.)"

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
    #: kBrain (contract C3): the voice talker's reading of the words, a seed shown labelled (never an order, never
    #: quotable); ``None`` for typed requests.
    understanding: dict[str, Any] | None = None
    #: kMind merge note (review L5): the runtime's late understanding for a request the voice submitted before it was
    #: complete (``AgentRuntime.seed``), taken once as the planner's first turn is written; ``None``: none can come.
    late_understanding: Callable[[], Any] | None = field(default=None, repr=False, compare=False)
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
    #: bRound2 (F1c): a send's text with too few of their words was sent back once (:func:`plan.send_text_error`).
    text_bounced: bool = False
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
    #: x10: skills that ran in this request and couldn't confirm, or handed over (code ids): never run again here.
    spent: set[str] = field(default_factory=set)
    #: x10: the one replan by steps after a skill's unconfirmed result was taken.
    retried_unconfirmed: bool = False
    #: sBrainFix (F9): act plans that only looked and were sent back to the planner with what they read.
    look_backs: int = 0
    #: The held ending a part's turn put up (:attr:`held` while no other ending replaced it).
    rest_held: tuple[str, dict[str, Any]] | None = None
    #: pBrain: steps plans that ended on a look with a done that read only part of the target (:func:`part_rest`),
    #: sent back to the planner for the rest; and the plain-text replies to an unconfirmed act request sent back
    #: (:func:`text_back`).
    part_rests: int = 0
    text_backs: int = 0
    #: fBrain: act plans whose memory or fetch reads went back to the planner for the answer to their question
    #: (:func:`answer_due`), and the held ending that turn put up (:attr:`held` while no other ending replaced it).
    answer_parts: int = 0
    answer_held: tuple[str, dict[str, Any]] | None = None
    #: P1.3: the tidy-up turn was given (once per request), the ending it holds (its line, already said), and the
    #: executor's list of what its steps did (their words, said after that line).
    tidied: bool = False
    tidy_held: tuple[str, dict[str, Any]] | None = None
    tidy_words: list[str] = field(default_factory=list)
    #: pBrain review r1 (L1): how many of the user's words (``Host.user_words``: their turns, steers, answers to a
    #: question) there were when the last plan began; words added since mean the user spoke after it (a steer folded
    #: into its report, an answer), and :func:`text_back` then leaves their reply to the planner's reading of them.
    plan_words: int = 0


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
        #: bRound2: the conversation's turns from memory's turn log, read on first use (:meth:`said_before`).
        self._said_before: list[str] | None = None

    def said_before(self) -> list[str]:
        """bRound2 (B28): the user's own turns of this conversation, oldest first: memory's turn log (what the user
        said to Yuki, exactly as heard, including the turns only the voice talker answered, which never reached this
        Brain as a request) of the last :data:`SAID_BEFORE_S`, marked as said to Yuki (:data:`SAID_BEFORE_TO`), at most
        :data:`SAID_BEFORE_TURNS`, each once, none of this request's own. Never a card's text, a model's words or
        Yuki's replies. Read once per request on first use (the executor asks only for a step's app value no request
        of the Brain's says); ``[]`` when memory can't say. RAM only; the log has the count."""
        if self._said_before is not None:
            return list(self._said_before)
        got: list[str] = []
        memory = self.memory
        call = getattr(memory, "call", None)
        if callable(call):
            now = time.time()

            def go(client: Any) -> list[str]:
                store = getattr(client, "store", client)
                turns = store.turns_between(now - SAID_BEFORE_S, now + 1.0)
                return [str(t.user_text or "") for t in turns
                        if getattr(t, "addressed", None) in SAID_BEFORE_TO and str(t.user_text or "").strip()]

            try:
                got = list(call("brain_said_before", go, timeout_s=SAID_BEFORE_WAIT_S) or [])
            except Exception:  # noqa: BLE001 - a help only
                got = []
        mine = {" ".join(str(w).split()).casefold() for w in self.user_words}
        out: list[str] = []
        seen: set[str] = set()
        for text in got:
            key = " ".join(text.split()).casefold()
            if key and key not in mine and key not in seen:
                seen.add(key)
                out.append(" ".join(text.split()))
        self._said_before = out[-SAID_BEFORE_TURNS:]
        try:
            self.log("brain_said_before_read", turns=len(self._said_before), read=len(got))
        except Exception:  # noqa: BLE001
            pass
        return list(self._said_before)

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
        #: sBrainFix (F8): this request's hand-off check was already read as an observer.
        self._seed_observed = False
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
        #: kBrain: when the planner's prefix was last written or read (a planner call or a warm-up; monotonic, 0:
        #: never in this app run), for :meth:`warm_cache`.
        self._warm_at = 0.0
        self._warming = False
        self._warm_lock = threading.Lock()
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
        """kBrain: write the planner's cached prefix (tools, core, profile and rules, menu) and open its connection
        ahead of a request: one call with the same tools, system blocks, effort and streaming as a typed request, one
        token out. The logs said it matters (18 Oct, Bedrock, Sonnet 5.5): a process's first planner call reached its
        first token in about 4 s, a cold cache write in 13.4 s (10 Oct: 12.4 s); after a warm-up, 0.96 s. Only when
        no planner call or warm-up ran in the last :data:`WARM_AFTER_S`, and never while a request runs. Runs on the
        caller's thread (callers use a background one); ``effort`` / ``thinking`` are the old loop's interface and are
        not used (the planner's own effort is). ``True`` when it ran, ``False`` when skipped, ``None`` when the call
        failed (the next call may try again). Its spend is a ``startup_cost`` record, which the day's spend counts
        (``yuki.core.cost``), never a request's. Content-free log; never raises."""
        with self._warm_lock:
            if self._running or self._warming or (
                    self._warm_at and time.monotonic() - self._warm_at < WARM_AFTER_S):
                self.logger.log("model_prewarm", skipped="brain: warm or busy")
                return False
            self._warming = True
        started = time.perf_counter()
        try:
            return self._warm()
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            self.logger.log("model_prewarm", ok=False, error=type(exc).__name__,
                            ms=round((time.perf_counter() - started) * 1000, 1))
            return None
        finally:
            self._warming = False

    def _warm(self) -> bool:
        """:meth:`warm_cache`'s call and its accounting (raises on a failed call)."""
        started = time.perf_counter()
        bs = BrainSettings.read(self.settings)
        names = list(getattr(self.settings, "user_names", []) or [])
        card = read_card(self.memory)
        brief, rules = read_profile_parts(self.memory)
        # the same bytes as the request's breakpoint 2 and 3 (context.gather composes them the same way)
        profile = profile_text(names=names, brief=None if card else brief, rules=rules, limit=bs.profile_chars)
        menu, _lines, _keys = read_menu(self.memory, self.settings, limit=bs.menu_chars)
        params = build_params(model=bs.model, max_tokens=1, system=system_blocks(profile, menu),
                              messages=[{"role": "user", "content": [{"type": "text", "text": WARM_TEXT}]}],
                              effort=bs.effort_of("typed")[0], thinking_display=bs.thinking_display, stream=bs.stream,
                              eager=bs.eager)
        if bs.stream:
            with self.client.messages.stream(**params) as stream:
                for _event in stream:
                    pass
                message = stream.get_final_message()
        else:
            message = self.client.messages.create(**params)
        self._warm_at = time.monotonic()   # review L2: only a warm-up that ran holds the next one off
        ms = round((time.perf_counter() - started) * 1000, 1)
        model = self._used_model(bs.model)
        tokens = usage_tokens(getattr(message, "usage", None))
        try:
            cost = self.settings.estimate_cost(model, tokens)
        except Exception:  # noqa: BLE001
            cost = None
        self.logger.log("model_prewarm", ok=True, model=model, ms=ms, tokens=tokens)
        # review L2: the day's spend reads warm-ups from ``startup_cost`` records (``yuki.core.cost``), as the old
        # loop's; a request's own totals never hold it
        self.logger.log("startup_cost", kind="model_prewarm", lane=self.lane, model=model,
                        elapsed_s=round(ms / 1000, 2), tokens=tokens,
                        cost_usd=None if cost is None else round(cost, 6))
        return True

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

    def _said_name(self, who: str) -> str:
        """pBrain (B35, bRound2: "Sending to हेमंत" when the planner wrote ``consent.to`` in Devanagari): the name the
        countdown says and shows for a send's recipient: memory's own name for the one person ``who`` names
        (:func:`yuki.brain.consent.spoken_name` over memory's people index, ``contacts.named_in``: as written, a
        learned heard form, a transliteration), else ``who`` as given. One bounded memory read
        (:data:`SAID_NAME_TIMEOUT_S`); whom the message goes to is the skill's, never changed here."""
        given = " ".join(str(who or "").split())
        host = self._host_ref
        memory = getattr(host, "memory", None) if host is not None else None
        call = getattr(memory if memory is not None else self.memory, "call", None)
        if not given or not callable(call):
            return given

        def people(text: str) -> list[list[str]] | None:
            def go(client: Any) -> list[list[str]]:
                from yuki.memory.contacts import named_in

                return [list(c.names) for c, _matched in named_in(getattr(client, "store", client), text)]

            try:
                got = call("brain_said_name", go, timeout_s=SAID_NAME_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - memory can't say: the name as given
                return None
            return got if isinstance(got, list) else None

        from yuki.brain.consent import spoken_name

        said = spoken_name(given, people)
        self.logger.log("brain_said_name", changed=said != given)
        return said

    def _send_countdown(self, *, who: str, where: str, app: str, text: str, typed: bool, window: str,
                        who_alts: tuple[str, ...], where_alts: tuple[str, ...], from_memory: bool) -> str:
        seconds = float(self.send_countdown_s)
        if seconds > 0:
            who = self._said_name(who)   # pBrain: said and shown by memory's own name for them, never as written
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
            plan_seed: dict | None = None, origin_hwnd: int | None = None,
            understanding: Mapping[str, Any] | None = None,
            late_understanding: Callable[[], Any] | None = None) -> Iterator[AgentEvent]:
        """Handle one request (design 2). ``user_words``: the typed text or the heard turns, from the runtime or the
        bridge only; when none come, nothing is quotable (fail closed, M2) and ``request`` is shown as context only;
        ``context``: card / coach / where-typed text, shown labelled and never quotable; ``channel``: ``voice`` |
        ``typed``; ``request_id``: the id every log record of it carries; ``plan_seed``: a skill's hand-off (rung C);
        ``origin_hwnd``: the window in front when the user called Yuki up (a typed request runs with Yuki's own box in
        front): told to the planner as a code line (the hwnd only), and the executor's stand-in for the foreground.
        ``understanding``: the voice talker's reading of the words (contract C3: goal, parts, choose, context), shown
        to the planner labelled as a seed: their exact words win. ``late_understanding``: the runtime's reader of one
        that came after the request was submitted (``AgentRuntime.seed``), asked once when there was none, as the
        planner's first turn is written. Yields events; pauses on :class:`AskUser`; ends with exactly one
        :class:`Final` or :class:`ErrorEvent`."""
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
                       origin_hwnd=origin,
                       understanding=dict(understanding) if isinstance(understanding, Mapping) and understanding
                       else None, late_understanding=late_understanding if callable(late_understanding) else None)
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
        self._seed_observed = False
        self.needed_hands = False
        self._reset_accounting()
        self.last_summary = None

    # -- the seed's memory hint (bRound2, B08) -----------------------------------------------------------------------

    def _seed_hint(self, seed: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """What memory ties to a hand-off's missing value (:func:`yuki.brain.context.seed_memory`: the pairing the
        driver learned, else the names read that memory's facts about the words name), for the seed block. Read only
        for a seed with a ``missing`` value and names read; logged as code words and a count. Never raises."""
        if not isinstance(seed, Mapping) or not seed.get("missing") or not seed.get("seen"):
            return None
        try:
            hint = seed_memory(self.memory, seed)
        except Exception:  # noqa: BLE001 - a help only
            hint = None
        try:
            self.logger.log("brain_seed_memory", found=(hint or {}).get("from") or "none",
                            names=len((hint or {}).get("names") or ()))
        except Exception:  # noqa: BLE001
            pass
        return hint

    def _seed_hint_job(self, seed: Mapping[str, Any] | None, box: list[Any]) -> threading.Thread | None:
        """:meth:`_seed_hint` on its own thread, alongside the request turn's reads (its result lands in ``box``);
        ``None`` when the seed has nothing to look up."""
        if not isinstance(seed, Mapping) or not seed.get("missing") or not seed.get("seen"):
            return None

        def run() -> None:
            got = self._seed_hint(seed)
            if got:
                box.append(got)

        job = threading.Thread(target=run, name="yuki-brain-seed-memory", daemon=True)
        job.start()
        return job

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
        if req.seed is not None:
            # a skill handed this request over: something is to be done, so it is budgeted as an act request from
            # the first call (an invalid first plan is no answer: x10), and that skill is not run again here
            state.kind = "act"
            handed = seed_skill(req.seed)
            if handed:
                state.spent.add(handed)
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
        hint_box: list[Any] = []
        hint_job = self._seed_hint_job(req.seed, hint_box)
        gathered = gather(dispatcher=self.dispatcher, memory=self.memory, settings=self.settings, words=said,
                          names=list(getattr(self.settings, "user_names", []) or []), limits=bs, nonce=nonce,
                          reuse=early)
        if hint_job is not None:
            hint_job.join(SEED_MEMORY_WAIT_S)
        # M9: the people block carries the user's own last messages (and the chat in front): from the first call, an
        # outbound fetch goes through a plan step, where the consent rule and the commit check see it
        state.private_read = bool(gathered.people)
        self._gather_ms = (time.perf_counter() - started) * 1000
        self._menu_lines = gathered.menu_lines
        catalog = load_catalog() if gathered.skill_keys is not None else None
        sends = sends_fn(catalog)
        if req.understanding is None and req.late_understanding is not None:
            # kMind merge note (review L5): an understanding that came after the request started, while its parts were
            # read; one that comes after this point is dropped (their exact words stand alone, as with none)
            try:
                late = req.late_understanding()
            except Exception:  # noqa: BLE001 - none: as before
                late = None
            if isinstance(late, Mapping) and late:
                req.understanding = dict(late)
                self.logger.log("brain_late_seed", taken=True)
        conv = Conversation(request_turn(words=req.words, channel=req.channel, context=req.context,
                                         gathered=gathered, history=list(self._history), seed=req.seed, limits=bs,
                                         nonce=nonce, seed_hint=hint_box[0] if hint_box else None,
                                         understanding=req.understanding))
        system = system_blocks(gathered.profile, gathered.menu)
        # aBrain (phase 2.5): pinned for the whole request, from its structure (the talker's understanding: its parts
        # and the skills they name; a hand-off), never its words; the rule is a setting so a test can compare
        effort, effort_why = bs.effort_of(req.channel, req.understanding, seeded=req.seed is not None,
                                          skills=gathered.skill_keys)
        self._effort = effort
        self.logger.log("brain_request", channel=req.channel, effort=effort, effort_why=effort_why,
                        effort_rule=bs.effort_rule, model=bs.model, seeded=req.seed is not None,
                        early=early is not None, words=len(req.words), context_chars=len(req.context),
                        overview_chars=len(gathered.overview), you_chars=len(gathered.you),
                        people_chars=len(gathered.people), profile_chars=len(gathered.profile),
                        menu_chars=len(gathered.menu), skills=len(gathered.skill_keys or ()),
                        card_chars=len(gathered.card), understanding=req.understanding is not None,
                        private_read=state.private_read, gather_ms=round(self._gather_ms, 1),
                        parts_ms=gathered.parts_ms)
        host.nonce = nonce
        executor = self._executor_for(host, req)
        self._executor = executor
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
        """Act plans: at most ``act_calls`` model calls, plus :data:`PART_REST_CALLS` for each turn code itself asked
        for after a steps plan's part was done (pBrain: ``part_rests``), and :data:`TEXT_BACK_CALLS` for each plain
        reply sent back (pBrain: ``text_backs``); code's questions, never the planner's spending. Answers:
        ``answer_calls`` or ``answer_s`` of work."""
        if state.kind == "act":
            return state.calls < (bs.act_calls + state.part_rests * PART_REST_CALLS
                                  + state.text_backs * TEXT_BACK_CALLS + state.answer_parts * ANSWER_PART_CALLS
                                  + int(state.tidied) * TIDY_CALLS)
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
                            current_plan=state.current.id if state.current is not None else None,
                            spent_skills=frozenset(state.spent),
                            words=tuple(host.user_words), text_bounced=state.text_bounced)
            params = build_params(model=bs.model, max_tokens=state.max_tokens or bs.max_tokens, system=system,
                                  messages=conv.messages(), effort=effort, thinking_display=bs.thinking_display,
                                  stream=bs.stream, eager=bs.eager)
            # no step is said as it streams (aBrain, phase 2.3: the executor says a slow one in the goal's words)
            call = ModelCall(self.client, params, checks=checks, emit=self._push, cancelled=self._cancelled.is_set,
                             log=self.logger.log, stream=bs.stream)
            state.calls += 1
            result: ExecResult | None = None
            try:
                call.open()
                tool = call.next_tool()
                if tool is not None and tool.name == PLAN_TOOL and call.plan is not None:
                    state.plans += 1
                    state.plan_words = len(host.user_words)
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
            state.text_bounced = checks.text_bounced
            self._record_call(resp, call, exec_s=self._last_exec_s if result is not None else 0.0)
            if result is not None and _acted(result):
                state.acted = True
            if self._tidying(state):
                # P1.3 (review M2): the tidy-up turn is one model call, plan-only: whatever came back ends it here (a
                # plan's steps already ran under the executor's tidy gate; any other tool is never run)
                if self._after_tidy(call, resp, result, state, bs, conv, host):
                    return
                continue
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
        elif self._tidying(state):
            # the tidy-up turn only acts (undoable window steps) or says nothing: never a question or a read plan
            source.complete()
            return ExecResult(status="refused", reason="tidy_only")
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
        if result.status == "unconfirmed":
            try:
                skill = self._unconfirmed_skill(executor, plan, result)
            except Exception:  # noqa: BLE001 - never breaks a plan's report: it ends as before
                skill = None
            if skill:
                result = replace(result, unconfirmed_skill=skill)
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
        self._seed_executor(executor, header)
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

    def _seed_executor(self, executor: Any, header: Plan) -> None:
        """The hand-off seed the executor holds for this plan (x10). Its done-check reads the seed's fixed check for a
        seeded plan whose own ``done`` took it (``seed_check``: the planner wrote none) and for a skill route, as
        before; a seeded plan by steps that picked a probe of its own (or sends, or joins) is decided by that probe or
        its class, so the executor gets the seed without its check (its onset baseline and key stay). The request's
        seed is the source: a hand-off within a plan updates it (``_after_plan``)."""
        if not hasattr(executor, "seed"):
            return
        seed = self._req.seed if self._req is not None else None
        if not seed:
            executor.seed = None
        elif header.seed_check or header.skill:
            executor.seed = dict(seed)
        else:
            executor.seed = {k: v for k, v in seed.items() if k not in ("done", "done_words")}

    def _unconfirmed_skill(self, executor: Any, plan: Plan | None, result: ExecResult) -> str | None:
        """The skill whose own check left this plan unconfirmed, when finishing it by steps after a look is safe
        (x10, principle 1), else ``None``. Safe means: the plan is a skill route (its ``run_skill`` step decides it)
        and declares no send or join; that step acted and couldn't confirm (engine outcome ``unverified``); its skill
        sends nothing and joins nothing (a message capability, even as a draft, is never redone: rule 8, no double
        send or double text); it has no ``points`` slot (a relative change, "a bit quieter": done again, it changes
        twice; which values the resolver filled is not in the report, so any such skill stops); and the route that
        acted may be followed after acting (its data's ``retry_after_acting``: a tab pick or a join is not).
        Unreadable data is not safe. Code data only, never the words.

        An engine that hands over every unconfirmed end that may be finished by steps itself (x5Fallback: rung C, why
        ``unverified``; it exports ``WHY_UNVERIFIED``) sends those here as a hand-off, which the seed path replans by
        steps. An ``unverified`` it still returns is one it chose to stop at (a send, a relative change, a state that
        was already there, a result on another device), so it is never redone here: ``None``."""
        if result.status != "unconfirmed" or plan is None or not plan.skill or plan.consent.commit in ("send", "join"):
            return None
        try:
            from yuki.skills import engine as skill_engine

            if getattr(skill_engine, "WHY_UNVERIFIED", None):
                return None
        except Exception:  # noqa: BLE001 - no engine to ask: not safe to redo by steps
            return None

        def ran_key(run: Any) -> str:
            payload = getattr(getattr(run, "outcome", None), "payload", None)
            return str(payload.get("skill") or "") if isinstance(payload, Mapping) else ""

        # this plan's runs only (the executor keeps them per plan); the route's own skill decides it
        runs = list(getattr(executor, "skill_outcomes", None) or [])
        pick = next((r for r in reversed(runs) if plan.skill in (ran_key(r), str(getattr(r, "key", "") or ""))), None)
        payload = getattr(getattr(pick, "outcome", None), "payload", None)
        if pick is None or not isinstance(payload, Mapping) or str(payload.get("outcome") or "") != "unverified":
            return None
        if getattr(pick, "drafted", False):
            return None
        key = ran_key(pick) or str(getattr(pick, "key", "") or "")
        try:
            from yuki.brain import util
            from yuki.brain.consent import skill_joins, skill_sends

            own = util.skill(key)
            family = util.skill(skill_family(key)) if skill_family(key) != key else None
            known = [s for s in (own, family) if s is not None]
            if not known:
                return None
            slots = dict(getattr(pick, "slots", {}) or {})
            if any(getattr(s, "sends", False) or getattr(s, "sends_when", ()) or skill_sends(s, slots, partial=True)
                   or skill_joins(s) for s in known):
                return None
            if any(getattr(slot, "type", "") == "points" for s in known for slot in getattr(s, "slots", ()) or ()):
                return None   # a relative change ("a bit quieter") may be in it: done again, it would change twice
            # a learned skill runs its capability's rungs (it has none of its own)
            routes = list(getattr(own, "routes", ()) or ()) or list(getattr(family, "routes", ()) or ())
            route = next((r for r in routes if getattr(r, "key", None) == payload.get("route")), None)
            if route is not None:
                safe = bool(getattr(route, "retry_after_acting", False))
            else:
                safe = bool(routes) and all(bool(getattr(r, "retry_after_acting", False)) for r in routes)
        except Exception:  # noqa: BLE001 - unreadable data: not safe to redo by steps
            return None
        return key if safe else None

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
            "outcome": getattr(got, "outcome", None), "final": bool(getattr(got, "final", False)),
            "parts": [str(p) for p in getattr(got, "parts", None) or [] if str(p or "").strip()],
            "after": str(getattr(got, "after", "") or "")}
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
            # x10: an invalid plan counts against the replans (brain_max_replans, 3), as a mismatch does; it no
            # longer ends the request at the second one. Its errors are logged as the validator's code words
            # (content-free), so why a plan failed can be read from the log.
            state.invalid += 1
            errors = list(source.errors or ["invalid plan"])
            raw_kind = resp.tool.input.get("kind") if resp.tool is not None and isinstance(resp.tool.input, Mapping) \
                else None
            if raw_kind == "act":
                state.kind = "act"   # it meant to act: the act budget, never an answer's 30 s
            self.logger.log("brain_plan_invalid", n=state.invalid, errors=len(errors), why=error_codes(errors)[:8],
                            seeded=self._req is not None and self._req.seed is not None, replans=state.replans)
            if state.replans >= bs.max_replans or not self._budget_left(state, bs, self._req):
                self._finish_stuck(state)
                return True
            state.replans += 1
            conv.add_user([tool_result(tool_id, invalid_report(errors, result.statuses,
                                                               replans_left=bs.max_replans - state.replans),
                                       error=True), *extra, *self._steer_turn(host)])
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
            if status == "done" and result.probe == "field_holds" and typed_draft(
                    plan, self._req.seed if self._req is not None else None):
                # review r2 L3: a message skill's hand-off finished by steps is a draft its own probe saw typed in:
                # said as one ("send it when you're ready"), never a generic "the box holds it"
                line = LINE_DRAFTED
            # a send's or a join's line is consent's own, word for word (the send rule is unchanged)
            sent = isinstance(result.outcome, Mapping) and bool(result.outcome.get("send"))
            said = ending_line(line, self._ending_facts()) if (status == "unconfirmed" and not sent
                                                               and not self._consent_lined(plan)) else line
            outcome = self._result_outcome(result, state, plan, said)
            if said != line:
                # aBrain (phase 2.3): the generic line gave way to the evidence's; the outcome's code line is the same
                line = said
                if outcome.get("line"):
                    outcome["line"] = line
            # review M2: its evidence from this plan, now, before any held turn or tidy-up plan resets the executor
            outcome = self._with_evidence(line, outcome)
            if (status == "unconfirmed" and look_only(plan, result) and state.look_backs < LOOK_BACKS_MAX
                    and self._budget_left(state, bs, self._req)):
                # sBrainFix (F9): an act plan that only looked did nothing, so its check reading nothing is no ending
                # (16 Oct simulation B08: "I did it, but I couldn't confirm it worked." after one look at Spotify):
                # what it read goes back to the planner for the plan that does it. Not a replan after a failure (the
                # call budget bounds it, and LOOK_BACKS_MAX). Its line is held only when something acted earlier in
                # the request (it stands if the model then changes nothing; the model's words never claim a result
                # after an act); with nothing acted, the model's own reply is the answer, as for any plain reply.
                state.look_backs += 1
                state.held = (line, outcome) if state.acted else None
                self.logger.log("brain_look_only", n=state.look_backs, reads=len(result.reads),
                                probe=result.probe or None, acted=state.acted)
                conv.add_user([tool_result(tool_id, look_report(result, plan)), *extra, *self._steer_turn(host)])
                return False
            if (status == "unconfirmed" and result.unconfirmed_skill and not state.retried_unconfirmed
                    and state.replans < bs.max_replans and self._budget_left(state, bs, self._req)):
                # x10 (principle 1): a skill that acted and couldn't confirm is not the end while replans remain:
                # one replan by steps after a look, that skill never run again. Its line is held: it stands if the
                # replan changes nothing (a plain reply, an invalid plan), and a plan that runs decides instead.
                state.retried_unconfirmed = True
                state.replans += 1
                state.spent.add(result.unconfirmed_skill)
                state.held = (line, outcome)
                self.logger.log("brain_unconfirmed_replan", skill=skill_family(result.unconfirmed_skill),
                                replans=state.replans)
                conv.add_user([tool_result(tool_id, unconfirmed_report(
                    result, plan, result.unconfirmed_skill, replans_left=bs.max_replans - state.replans)), *extra,
                    *self._steer_turn(host)])
                return False
            if self._steer_waiting() and self._budget_left(state, bs, self._req):
                # the user added something before the line went out: it is held (loop.Agent's held final) and the
                # model takes one more step with their words; the held line stands if nothing changes
                state.held = (line, outcome)
                self.logger.log("steer", held_final=status)
                conv.add_user([tool_result(tool_id, held_report(result, plan, line)), *extra,
                               *self._steer_turn(host)])
                return False
            if (status == "done" and line != LINE_DRAFTED and part_rest(plan, result, outcome)
                    and state.part_rests < PART_RESTS_MAX):
                # pBrain (17 Oct e2e2 B14 r0, bRound2 B28 r0): the plan ended on a look, which the core prompt asks of
                # steps doing a part, and its done read true while covering only part of the target (likely): a done
                # that reads the first part ("YouTube's home page" for "...and put something on from the front
                # page"). Whether more is left is the planner's to read from their words, never code's: its line is
                # held and the planner gets one turn with what the look read, to plan the rest by steps or give the
                # one-word reply that all is done (the held line then stands; any other reply, or the budget running
                # out, says the rest isn't done). PART_RESTS_MAX bounds it, and the turn adds PART_REST_CALLS to the
                # act budget. Never after a send or a join (part_rest).
                state.part_rests += 1
                state.held = state.rest_held = (line, outcome)
                self.logger.log("brain_done_part", n=state.part_rests, reads=len(result.reads),
                                probe=result.probe or None)
                conv.add_user([tool_result(tool_id, part_rest_report(result, plan, line)), *extra,
                               *self._steer_turn(host)])
                return False
            if (answer_due(plan, result, status) and state.answer_parts < ANSWER_PARTS_MAX
                    and not self._cancelled.is_set()):
                # fBrain: the plan read memory (or fetched) beside what it did, and nothing ever answers from those
                # reads after an act plan ends: its line is held and the planner answers from them in plain text
                # (or the one word that nothing more was asked); code says its line, then that answer
                state.answer_parts += 1
                state.held = state.answer_held = (line, outcome)
                self.logger.log("brain_answer_part", n=state.answer_parts, reads=len(result.reads), status=status)
                conv.add_user([tool_result(tool_id, answer_part_report(result, plan, line)), *extra,
                               *self._steer_turn(host)])
                return False
            if status != "declined" and self._tidy_turn(result, plan, line, outcome, state, bs, conv, extra, host,
                                                        tool_id):
                return False
            self._final(line, outcome)
            return True
        if result.final and result.line and status in ("refused", "stopped") and not self._cancelled.is_set():
            # the executor ends the run here with its own line (a game in front: "after the round?"; the desk busy):
            # no replan routes around it; the parts already done are said first (review M2)
            line = with_parts(result.parts, result.line)
            self._final(line, self._with_evidence(line, self._result_outcome(result, state, plan, line)))
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
            # rung C: the skill handed over; the next plan is seeded with it (by steps, or with the consent a send or
            # a join needs: x10), and the skill is not run again in this request
            self._req.seed = dict(result.seed)
            handed = seed_skill(result.seed)
            if handed:
                state.spent.add(handed)
            seeded = seed_block(result.seed, limit=bs.seed_chars, nonce=str(getattr(host, "nonce", "") or ""),
                                hint=self._seed_hint(result.seed))
            if seeded:
                blocks.append({"type": "text", "text": seeded})
        conv.add_user([tool_result(tool_id, step_report(result, plan, limit=bs.result_chars, replans_left=left)),
                       *extra, *blocks, *self._steer_turn(host)])
        return False

    # -- the look after acting (P1.3) ---------------------------------------------------------------------------------

    @staticmethod
    def _tidying(state: _State) -> bool:
        """The tidy-up turn is running: its held ending is the one up."""
        return state.tidy_held is not None and state.held is state.tidy_held

    def _tidy_turn(self, result: ExecResult, plan: Plan | None, line: str, outcome: dict[str, Any], state: _State,
                   bs: BrainSettings, conv: Conversation, extra: list[dict[str, Any]], host: Host,
                   tool_id: str) -> bool:
        """One tidy-up turn when the executor's after-look flagged what the request's acts did (``result.after``) and
        the user's tidy dial is not off. The request's line goes out first (review H1: a :class:`Say`, at once: the
        voice says it, the strip shows it; nothing waits on the turn), then the planner gets the "After:" block and
        the principle (:func:`tidy_report`) for exactly one call, and the executor takes only undoable window steps on
        what the request's own acts changed. ``True`` when the turn was given."""
        executor = getattr(self, "_executor", None)
        if (not result.after or state.tidied or executor is None or not hasattr(executor, "tidy_up")
                or self._cancelled.is_set()):
            return False
        from yuki.brain.util import TIDY_ASK, TIDY_OFF, tidy_mode

        mode = tidy_mode()
        if mode == TIDY_OFF:
            self.logger.log("brain_tidy", given=False, why="off")
            return False
        state.tidied = True   # counted before the budget is read: the turn's own call is in it
        if not self._budget_left(state, bs, self._req):
            self.logger.log("brain_tidy", given=False, why="budget")
            return False
        from yuki.brain.executor import TIDY_STEPS

        executor.tidy_up(mode)
        state.tidy_words = executor.tidy["words"]
        state.held = state.tidy_held = (line, outcome)
        self._push(Say(line))
        self.logger.log("brain_tidy", given=True, mode=mode, status=result.status, said_first=True)
        report = tidy_report(plan, line, result.after, nonce=str(getattr(host, "nonce", "") or ""),
                             ask=mode == TIDY_ASK, steps=TIDY_STEPS)
        conv.add_user([tool_result(tool_id, report), *extra, *self._steer_turn(host)])
        return True

    def _after_tidy(self, call: ModelCall, resp: CallResult, result: ExecResult | None, state: _State,
                    bs: BrainSettings, conv: Conversation, host: Host) -> bool:
        """The tidy-up turn's one call came back (a plan that ran, a plain reply, any other tool, a cut-off, a
        refusal): the turn ends (review M2: nothing else runs in it). Its ending (:meth:`_tidy_ending`) is the
        request's Final; when the user added words meanwhile (review L5), that ending is held instead and their words
        are a normal turn (the ending stands if nothing more needs doing). ``True`` when the request ended."""
        text, outcome = self._tidy_ending(state, result)
        whole = not resp.broken and resp.stop_reason not in ("max_tokens", "refusal")
        if (whole and self._steer_waiting() and not self._cancelled.is_set()
                and self._budget_left(state, bs, self._req)):
            state.held = (text, outcome)
            self.logger.log("steer", held_final="tidy")
            conv.add_assistant(resp.content)
            source = call.plan
            report = held_report(result if result is not None else {"status": "done"},
                                 source.plan if source is not None else None, text)
            blocks = ([tool_result(resp.tool.id, report), *self._extra_tools(resp)] if resp.tool is not None
                      else [{"type": "text", "text": report}])
            conv.add_user([*blocks, *self._steer_turn(host)])
            return False
        self._final(text, outcome)
        return True

    def _tidy_ending(self, state: _State, result: ExecResult | None) -> tuple[str, dict[str, Any]]:
        """The tidy-up turn is over: ``(text, outcome)``, the request's line (said already) then the words of each tidy
        step whose own read-back saw it change something (the executor's list; none: the line alone, "otherwise stay
        quiet"). The text is the whole record (the chat, memory); ``outcome["said_first"]`` is the part the
        :class:`Say` already put out, so the voice need not say it again. Ends the turn's state."""
        held_line, held_outcome = state.tidy_held or ("", {})
        said = " ".join(" ".join(str(w).split()) for w in state.tidy_words if str(w).strip())
        text = f"{held_line.rstrip()} {said}".strip() if said else held_line
        # review M2: the line went out as code said it (a Say), so no talker re-words it ("" weighed: nothing to add)
        outcome = {**dict(held_outcome or {}), "said_first": held_line, "evidence": "",
                   **({"line": text} if said else {})}
        self.logger.log("brain_tidy", ended=True, acts=len(state.tidy_words),
                        status=result.status if result is not None else None)
        state.tidy_held = None
        executor = getattr(self, "_executor", None)
        if executor is not None and hasattr(executor, "tidy"):
            executor.tidy = None
        return text, outcome

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

    def _with_evidence(self, text: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """aBrain (phase 2.3): ``outcome`` with what a non-success ending's line stands on (or a done that read only
        part of what they asked), ``outcome["evidence"]``, for the voice's talker to say in its own words (typed shows
        the line); content: never logged. Built where the ending's line is made, from the plan that made it (review M2:
        a later tidy-up or read plan resets the executor), so a held ending keeps its own; ``""`` marks one already
        weighed with nothing to add, and a send's or a join's (consent's own words, said word for word). Never
        raises."""
        if not isinstance(outcome, dict) or "evidence" in outcome:
            return outcome
        level = str(outcome.get("level") or "")
        if level not in EVIDENCE_LEVELS | {"likely"} and not outcome.get("partial"):
            return outcome
        state = self._state
        plan = state.current if state is not None else None
        if outcome.get("send") or self._consent_lined(plan):
            return {**outcome, "evidence": ""}
        facts = self._ending_facts()
        evidence = ""
        if level in EVIDENCE_LEVELS or outcome.get("partial") or facts.get("unchecked"):
            last = state.last if state is not None else None
            evidence = evidence_text(facts, goal=plan.goal if plan is not None else "",
                                     parts=last.parts if last is not None else (), line=text)
        return {**outcome, "evidence": evidence}

    def _consent_lined(self, plan: Plan | None) -> bool:
        """The ending is a send's or a join's: the plan declared one, or a capability it ran joins (consent's lines
        stand word for word; the send rule is unchanged). Code facts only; ``True`` when it can't tell."""
        try:
            if plan is not None and plan.consent.commit in ("send", "join"):
                return True
            from yuki.brain import util as _util
            from yuki.brain.consent import skill_joins

            runs = getattr(getattr(self, "_executor", None), "skill_outcomes", None) or ()
            return any(skill_joins(_util.skill(getattr(run, "key", ""))) for run in runs)
        except Exception:  # noqa: BLE001 - can't tell: as consent's
            return True

    def _final(self, text: str, outcome: dict[str, Any]) -> None:
        # an ending made before this (held, a tidy-up's) carries its own evidence; one made now is weighed now
        outcome = self._with_evidence(text, outcome)
        if isinstance(outcome, dict) and "evidence" in outcome and not outcome["evidence"]:
            outcome = {k: v for k, v in outcome.items() if k != "evidence"}
        self._last_outcome = outcome
        self._final_text = str(text or "")
        self._push(Final(text, outcome=outcome))
        self._observe_seed(outcome)

    def _ending_facts(self) -> dict[str, str]:
        """The executor's evidence for an ending (:meth:`yuki.brain.executor.Executor.ending_facts`), ``{}`` when there
        is no executor or it can't say. Never raises."""
        executor = getattr(self, "_executor", None)
        reader = getattr(executor, "ending_facts", None)
        try:
            got = reader() if callable(reader) else {}
        except Exception:  # noqa: BLE001 - no facts: the line stands
            got = {}
        return dict(got) if isinstance(got, Mapping) else {}

    def _observe_seed(self, outcome: Mapping[str, Any] | None) -> None:
        """sBrainFix (F8): a request a skill handed over that ends "verified" or "likely" by a plan's own check, or
        "unverified" after an act step ran (review r1 L6: a play its probe couldn't read), has the hand-off's own check
        read once as an observer, after the line went out, on its own thread (``done.observe_seed``; its verdict is
        ignored: the driver's own finish gate decides what it learns). A plan the seed's check decided
        (``seed_check``: the executor read it already) needs none. Once per request; never raises."""
        try:
            req, state = self._req, self._state
            if req is None or not req.seed or self._seed_observed or not isinstance(outcome, Mapping):
                return
            level = str(outcome.get("level") or "")
            if level not in OBSERVE_LEVELS and not (level in OBSERVE_ACTED_LEVELS and state is not None
                                                    and state.acted):
                return
            plan = state.current if state is not None else None
            if plan is not None and plan.seed_check:
                return
            self._seed_observed = True
            from yuki.brain import done as _done

            host = self._host_ref
            seed = dict(req.seed)
            env = _done.ProbeEnv(since=float(req.epoch), dispatcher=self.dispatcher,
                                 memory=getattr(host, "memory", None) if host is not None else self.memory,
                                 baseline=dict(seed.get("baseline") or {}) if isinstance(seed.get("baseline"), Mapping)
                                 else {})
            _done.observe_seed(seed, env, log=self.logger.log)
        except Exception as exc:  # noqa: BLE001 - an observer never breaks a request's ending
            self.logger.log("error", where="brain_seed_observer", error=type(exc).__name__)

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
        where = text_back(state, self._req.seed if self._req is not None else None, words=len(host.user_words))
        if where is not None and not self._cancelled.is_set():
            # pBrain (17 Oct e2e2 B14 r2: the done read "another page is showing" and the planner's reply, "Tell me
            # what you want to watch and I'll put it on.", ended the request on code's "I couldn't confirm ..."): an act
            # request that is not confirmed done is never ended by the planner's words the first time: one more turn,
            # with where it stands in code's line, to do the rest by steps (a pick left to it is its to make) or ask
            # one question as a plan. Its next plain reply ends it as before (code's line). TEXT_BACKS_MAX bounds it,
            # and the turn adds TEXT_BACK_CALLS to the act budget (code's question). Never on a send or a join.
            state.text_backs += 1
            self.logger.log("brain_text_back", n=state.text_backs, status=last.status if last is not None else None,
                            held=state.held is not None, acted=state.acted, chars=len(text))
            conv.add_user([{"type": "text", "text": text_back_report(state.current, where)},
                           *self._steer_turn(host)])
            return False
        if state.held is not None and state.held is state.answer_held:
            # fBrain: the act's own line, then the planner's answer from what the plan read (the one word: no question)
            held_line, held_outcome = state.held
            answer = "" if rest_done_reply(text) else " ".join(text.split())
            line = f"{held_line.rstrip()} {answer}".strip() if answer else held_line
            self._final(line, {**dict(held_outcome or {}), "line": line} if answer else dict(held_outcome or {}))
            return True
        if state.held is not None:
            if state.held is state.rest_held and not rest_done_reply(text):
                # after a part was done (part_rest), the planner neither planned the rest nor gave the one-word reply
                # that all is done (context.REST_DONE_WORD): the part's line, and that the rest isn't
                self._finish_stuck(state)
                return True
            # a plan ended and its line was held for the user's addition; the model changed nothing: the line stands
            # when something acted (code's line, never the model's claim), else the model's answer to it
            if state.acted or last is None or last.status != "declined":
                self._final(*state.held)
                return True
        if last is not None and last.line and last.status in ("refused", "mismatch", "error"):
            # the executor's own line for why it stopped (a refusal's "after the round?") wins over the model's words;
            # the parts already done are said first (review M2)
            line = with_parts(last.parts, last.line)
            self._final(line, self._outcome(state, level="failed" if state.acted else "answer", line=line,
                                            partial=bool(last.parts)))
            return True
        if state.acted or (state.kind == "act" and last is not None
                           and last.status in ("refused", "mismatch", "error")):
            # sBrainFix (review r1 runs): a plan that tried to act and was stopped, even with nothing acted, leaves the
            # model's words no answer to give: in the 16 Oct B31 run its send was refused (text_mismatch) and its reply
            # "Sent Hemanth a DM asking when he's joining the VC." would have been said. Code says where it stopped.
            self._finish_stuck(state)
            return True
        self._final(text or LINE_NO_REPLY, self._outcome(state, level="answer"))
        return True

    def _finish_stuck(self, state: _State, *, budget: bool = False) -> None:
        """The code line for an act plan that could not get there, or an answer that ran out of budget. A held ending
        (no plan ran after it) is said as it was; a part's, held for the rest of the request (pBrain), says that the
        rest isn't done (:data:`LINE_REST_UNDONE`, level failed, partial): the planner never said it was all done."""
        if state.held is not None and state.held is state.rest_held:
            held_line, held_outcome = state.held
            base = " ".join(str(held_line or "").split())
            from yuki.brain.done import LINE_REST

            # pBrain: a part's held line is done's own "<what was verified>; I couldn't check the rest." (part_rest
            # holds only a likely ending): said once, as what was seen and that the rest isn't done
            tail = LINE_REST.format(line="")
            if base.endswith(tail):
                base = base[:-len(tail)]
            line = LINE_REST_UNDONE.format(line=base.rstrip(". ") or "I did a part")
            self._final(line, {**dict(held_outcome or {}), "level": "failed", "label": LEVEL_LABELS["failed"],
                               "partial": True, "line": line,
                               "open": max(1, int(dict(held_outcome or {}).get("open") or 0))})
            return
        if state.held is not None:
            self._final(*state.held)
            return
        if state.kind != "act" and not state.acted:
            line = LINE_ANSWER_BUDGET if budget else LINE_NO_REPLY
            self._final(line, self._outcome(state, level="answer", line=line))
            return
        last, plan = state.last, state.current
        where, why = "", ""
        facts = self._ending_facts()
        if last is not None and isinstance(last.step, int) and plan is not None and 0 <= last.step < len(plan.steps):
            # aBrain (phase 2.3): the step in the island's words ("clicking Continue"), else its verb's
            where = facts.get("stuck") or STEP_WORDS.get(plan.steps[last.step].verb, "")
        if not where and state.acted:
            where = "partway"   # something ran, the step that stopped it is not known
        if last is not None and last.reason:
            # x10: a reason is a code word (or an executor's note) for the planner and the log: only the few with
            # plain words of their own are said; any other is left out
            why = plain_reason(last.reason, refused=last.status == "refused")
        if last is not None and last.line and last.status in ("mismatch", "refused"):
            line = last.line
        elif facts.get("did") and where and where != "partway":
            line = LINE_STUCK_AFTER.format(did=facts["did"], stuck=where, why=f": {why}" if why else "")
        else:
            line = LINE_STUCK.format(where=f" {where}" if where else "", why=f": {why}" if why else "")
        # review M2: the parts already done (their own skill's check saw them) are said before where it stopped
        line = with_parts(last.parts if last is not None else (), line)
        self._final(line, self._with_evidence(line, self._outcome(state, level="failed" if state.acted else "answer",
                                                                  line=line, partial=state.acted)))

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
        self._warm_at = time.monotonic()   # the planner's prefix was just read (or written): warm for an hour
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


_GENERIC: frozenset[str] | None = None


def generic_lines() -> frozenset[str]:
    """The code lines that say nothing of what happened (the brain's, the done-check's and the skill engine's "couldn't
    confirm it worked", "That didn't work."), whitespace-normalised: :func:`ending_line` replaces them when the evidence
    says more. Code's own constants; never a reading of words."""
    global _GENERIC
    if _GENERIC is None:
        from yuki.brain import util as _util

        lines = {LINE_UNCONFIRMED, _util.LINE_UNVERIFIED, _util.LINE_FAILED, _util.LINE_PARTIAL}
        try:
            from yuki.brain.executor import LINE_SKILL_UNSURE
            from yuki.skills.engine import LINE_UNVERIFIED as ENGINE_UNVERIFIED

            lines |= {LINE_SKILL_UNSURE, ENGINE_UNVERIFIED}
        except Exception:  # noqa: BLE001 - those modules not importable: the brain's own lines only
            pass
        _GENERIC = frozenset(" ".join(line.split()) for line in lines)
    return _GENERIC


def ending_line(line: str, facts: Mapping[str, str] | None) -> str:
    """aBrain (phase 2.3): a non-success ending's line from the evidence when ``line`` is one of the generic ones
    (:func:`generic_lines`) and ``facts`` (the executor's ``ending_facts``) say more; else ``line`` as it is.

    What the check looked for and saw: "Opened a page in Arc, but I couldn't confirm Excalidraw is open in Arc: another
    page is showing."; what it couldn't read: "..., but I couldn't check that Excalidraw is open in Arc."; only what
    ran: "Opened a page in Arc, but I couldn't check it worked." Facts are code's words, never the planner's."""
    text = " ".join(str(line or "").split())
    if not facts or text not in generic_lines():
        return line
    did, claim, seen = (" ".join(str(facts.get(k) or "").split()) for k in ("did", "claim", "seen"))
    if claim and seen and not facts.get("unread"):
        if did:
            return LINE_DID_NOT_SO.format(did=did, claim=claim, seen=seen.rstrip(". "))
        from yuki.brain.done import LINE_NOT_CONFIRMED

        return LINE_NOT_CONFIRMED.format(what=claim, seen=seen.rstrip(". "))
    if claim:
        return LINE_DID_UNREAD.format(did=did, claim=claim) if did else LINE_UNREAD.format(claim=claim)
    if did:
        return LINE_DID_UNCHECKED.format(did=did)
    return line


def evidence_text(facts: Mapping[str, str] | None, *, goal: str = "", parts: Sequence[str] = (),
                  line: str = "") -> str:
    """aBrain (phase 2.3): what a non-success ending stands on, in a few labelled plain facts for the voice's talker
    to word in one short line (``outcome["evidence"]``): the plan's goal, the parts done, the last step that ran, the
    step that stopped it and why, what the check looked for and what it saw. Content (titles, names): never logged.
    ``""`` when there is nothing beyond ``line``. At most :data:`EVIDENCE_CHARS`."""
    facts = dict(facts or {})
    bits: list[str] = []
    goal = " ".join(str(goal or "").split())
    if goal:
        bits.append(f"Goal: {goal.rstrip('. ')}")
    done_parts = [" ".join(str(p).split()).rstrip(". ") for p in parts or () if str(p or "").strip()]
    if done_parts:
        bits.append("Done: " + "; ".join(done_parts))
    if facts.get("did"):
        bits.append(f"Last step that ran: {facts['did']}")
    if facts.get("stuck"):
        bits.append(f"Stopped at: {facts['stuck']}" + (f" ({facts['why']})" if facts.get("why") else ""))
    if facts.get("claim"):
        bits.append(f"Checked for: {facts['claim']}; " + ("couldn't read it" if facts.get("unread") or not
                                                          facts.get("seen") else f"saw: {facts['seen']}"))
    elif facts.get("seen") and not facts.get("unread"):
        bits.append(f"The check saw: {facts['seen']}")
    if facts.get("unchecked"):
        bits.append(f"The check read only part of it; not read: {facts['unchecked']}")
    if not bits or (len(bits) == 1 and goal):
        return ""
    text = ". ".join(bits) + "."
    return text if len(text) <= EVIDENCE_CHARS else text[:EVIDENCE_CHARS - 1].rstrip() + "…"


def plain_reason(reason: Any, *, refused: bool = False) -> str:
    """A stuck plan's reason in plain words for the line the user hears (x10, C9), or ``""``: the executor's own code
    words (:data:`REASON_WORDS`), else, only for a refusal (``refused``: the plan's status is ``refused``, so the code
    word is the consent rule's), that rule's plain words for it (``consent.plain_why``, x11Consent; absent before it
    merges). Anything else, a code word with no plain words or an executor's note, is left out: it is never spoken (an
    executor code that shares a name with a consent code, ``error`` for one, is never said in consent's meaning)."""
    code = " ".join(str(reason or "").split())
    if not code:
        return ""
    if code in REASON_WORDS:
        return REASON_WORDS[code]
    if not refused:
        return ""
    try:
        from yuki.brain import consent as _consent

        plain = getattr(_consent, "plain_why", None)
        said = plain(code) if callable(plain) else ""
    except Exception:  # noqa: BLE001 - no plain words: left out
        said = ""
    return " ".join(str(said or "").split())


def typed_draft(plan: Plan | None, seed: Mapping[str, Any] | None) -> bool:
    """A plan by steps that finished a sending skill's hand-off (``seed_sends``) as a draft: it declares no send, and
    its own done is ``field_holds`` of the message box with the text, so a True there is the draft typed in (said as
    :data:`LINE_DRAFTED`)."""
    if plan is None or plan.kind != "act" or not plan.seeded or plan.skill is not None:
        return False
    if plan.consent.commit == "send" or not seed_sends(seed):
        return False
    return plan.done.name == "field_holds" and bool(" ".join(str(plan.done.args.get("text") or "").split()))


def rest_done_reply(text: Any) -> bool:
    """The planner's reply after a part was done is the one word code asked for when all is done
    (``context.REST_DONE_WORD``, :func:`yuki.brain.context.part_rest_report`): an exact reply format code set, never a
    reading of the model's words (any other reply, "I can't pick one" or a sentence, is not it: the safe side)."""
    return " ".join(str(text or "").split()).strip(" .!").casefold() == REST_DONE_WORD


def _same_check(a: Any, b: Any) -> bool:
    """Two probes read the same thing: the same probe with the same arguments, the process aside (a page check's
    browser, which the executor fills from the request either way) and a page check's site normalised the way the
    probe reads it (``done._site_key``: "youtube.com" is "youtube"). Code facts of the plan only."""
    from yuki.brain.done import _site_key

    if a is None or b is None or str(getattr(a, "name", "")) != str(getattr(b, "name", "")):
        return False

    def norm(probe: Any) -> dict[str, str]:
        args = dict(getattr(probe, "args", None) or {})
        out = {str(k): " ".join(str(v).split()).casefold() for k, v in args.items() if k != "process"}
        if "site" in out:
            out["site"] = _site_key(out["site"])
        return out

    return norm(a) == norm(b)


def part_rest(plan: Plan | None, result: ExecResult, outcome: Mapping[str, Any] | None) -> bool:
    """pBrain: a steps plan (no skill route, no send or join, not decided by a hand-off's own check) that ended on a
    look, whose done is the very check one of its own act steps already expected (it reads what that step did, a
    part) and read true while covering only part of the target (``likely``: a target field its probe doesn't read),
    did only the first part of the request by its own plan's shape: it goes back to the planner for the rest. All three
    17 Oct cases were [ctrl+t, open_url (expect YouTube's home page), look] with the done "YouTube's home page"; a plan
    whose done reads something no step expected ([click a video, wait, look] with the done "a video page") ends as
    before. Code facts only (the route, the declared commit, the last step's verb, the probes, the check's level),
    never a reading of their words."""
    if plan is None or plan.kind != "act" or plan.skill or plan.seed_check or not plan.steps:
        return False
    if plan.consent.commit in ("send", "join") or plan.steps[-1].verb != "look" or result.verdict is not True:
        return False
    if not any(s.verb in ACT_VERBS and _same_check(s.expect, plan.done) for s in plan.steps):
        return False
    return isinstance(outcome, Mapping) and str(outcome.get("level") or "") == "likely"


def text_back(state: _State, seed: Mapping[str, Any] | None, *, words: int | None = None) -> str | None:
    """pBrain: code's line for where an act request stands when the planner just replied in plain text and the request
    is not confirmed done, so the reply goes back once (:data:`TEXT_BACKS_MAX`); ``None`` when it ends as before. Not
    confirmed: its last plan stopped (a step or the done read otherwise: ``mismatch``, an ``error``), and the report
    the planner got asked for a revised plan. Never with a held line (a look-back's, an unconfirmed skill's, a skill
    route's or a part's turn already was code's one more turn, with a plain reply as one of its answers; the user's
    addition), after a refusal (consent's line stands), an ending the executor called final (a game in front), or on a
    send or a join (rule 8: nothing more is planned on a send's back, nor for a sending skill's hand-off), nor when the
    user spoke since the last plan began (``words``: how many of their words there are now, against
    :attr:`_State.plan_words`; review r1 L1: a "leave it" folded into the step report, or "no, forget it" to a
    question, and the planner's "Okay." is their word, never pushed back). Code facts only."""
    if state.kind != "act" or state.text_backs >= TEXT_BACKS_MAX or state.held is not None:
        return None
    if words is not None and words > state.plan_words:
        return None
    plan, last = state.current, state.last
    if plan is not None and plan.consent.commit in ("send", "join"):
        return None
    if seed_sends(seed):
        return None
    if last is None or last.final or last.status not in ("mismatch", "error"):
        return None
    return " ".join(str(last.line or "").split()) or LINE_UNCONFIRMED


def answer_due(plan: Plan | None, result: ExecResult, status: str) -> bool:
    """fBrain: an act plan that ended (done, not confirmed, drafted) with a memory read or a fetch among its steps
    (:data:`ANSWER_READ_VERBS`) whose results came back: what it read is content for the user, and the plan's ending says
    only what it did, so the reads go back to the planner once for the answer (:func:`answer_part_report`). Never on a
    send or a join (consent's own line stands alone), a skill route, or a plan that only looked (``look_only`` sends
    its reads back already). Code facts only (the plan's verbs, its status, whether reads came back)."""
    if plan is None or plan.kind != "act" or plan.skill or status not in ("done", "unconfirmed", "drafted"):
        return False
    if plan.consent.commit in ("send", "join") or not result.reads:
        return False
    return any(s.verb in ANSWER_READ_VERBS for s in plan.steps) and any(s.verb in ACT_VERBS for s in plan.steps)


def look_only(plan: Plan | None, result: ExecResult) -> bool:
    """An act plan whose every step only reads (look, memory, fetch, wait), so nothing of it acted (sBrainFix, F9): its
    check reading nothing is not the end of the request. Code facts only (the closed verb lists, the step states)."""
    if plan is None or plan.kind != "act" or not plan.steps or plan.skill:
        return False
    return all(s.verb not in ACT_VERBS for s in plan.steps) and not _acted(result)


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
    data["parts"] = [str(p) for p in data.get("parts") or [] if str(p or "").strip()]
    data["acted"] = bool(data.get("acted"))
    data["check_calls"] = int(data.get("check_calls") or 0)
    return ExecResult(**data)


__all__ = ["Brain", "Host", "CANCELLED", "WITHDRAWN", "PARKED", "PAUSE_STOP_LINE", "LEVEL_LABELS", "OUTCOME_KEYS",
           "STEP_WORDS", "REASON_WORDS", "plain_reason", "typed_draft", "with_watch_steps", "look_only",
           "LOOK_BACKS_MAX", "OBSERVE_LEVELS", "OBSERVE_ACTED_LEVELS", "PART_RESTS_MAX", "PART_REST_CALLS",
           "rest_done_reply", "LINE_REST_UNDONE", "answer_due", "ANSWER_PARTS_MAX", "ANSWER_PART_CALLS"]
