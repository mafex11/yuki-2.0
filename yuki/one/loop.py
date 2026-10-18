"""The ONE agent's typed driver (phase 4, oTyped; ``brain = "one"``): one Sonnet 5.5 agent owns the whole request.

:class:`OneAgent` ``.run(request, *, user_words, context, channel, request_id, ...)`` yields ``Brain.run``'s events
(:class:`Say`, :class:`AskUser`, :class:`SendPending`, ``ToolCall`` / ``ToolResult``, one :class:`Final` or
``ErrorEvent``), so ``ui/runtime.py`` relays it unchanged (``Lane._relay``). It is a NEW object beside the plan brain:
it subclasses :class:`yuki.brain.agent.Brain` for the caller surface and the request plumbing the design names (design
2.5: "the brain's Host for countdown, asks and pause": the worker thread and its event queue, the ``AskUser`` round
trip, cancel / withdraw / park, the takeover pause, the input gate, the acting marker, cost accounting,
``request_summary`` and ``requests.csv``, memory's turn log, the follow-up history) and replaces the request itself
(:meth:`OneAgent._drive`). ``Brain`` (the ``all`` brain) is not changed.

One request:

1. **The turn's facts, read once and frozen for the request** (``brain.context.gather``: the Situation per monitor,
   the you-card, "you right now", people named, profile and rules, the Skills menu), in one user turn
   (``brain.context.request_turn``) that rides once on the user's own message: the request stays append-only (the
   cache and the thinking blocks need it) and no harness text follows a tool result except the user's own added words.
2. **The ONE prompt and tools** (``one.prompt.typed_blocks``, the typed tail; ``one.tools`` typed set with ``keys``,
   ``shell``, ``fetch``, ``ask_user``; :func:`yuki.one.host.lane_tools` for the lane).
3. **Rounds.** Before **every** model call, ``turn.budget.round()`` (``one.budget``, the typed budget: asks, pauses
   and lease waits not counted); spent: code ends the request with its line and no model call. One streamed call
   (adaptive thinking at the typed effort, ``one_typed_thinking``; the failover chain stops after Sonnet 5): a reply
   with no call ends the request with the model's words; a reply's calls run **in order**, each through the runner
   (``one.runner``, lane oRun: budget -> gate -> desk -> act -> desk -> diff -> result), so every act passes the one
   policy gate. A gate's question is asked here (``AskUser``), the yes read by the check model and recorded as a code
   fact (``Turn.answer``), and the same call goes to the gate again (a yes allows it once).
4. **Who ends it.** The model, by replying with no call. Code ends a clean simple act as today (addendum decision 2,
   must-fix 2; ``one_settle = "code"``): every call of the reply acted and verified, nothing flagged, every ``then``
   empty: code's verified line is the reply, no closing call. A settle by the gate or the budget (the second refusal,
   the hard stop, the rounds) ends it with code's line (rule #149).
5. **The front desk** (``hands`` false) only looks: the first call that needs the worker (an act, ``remember``,
   ``ask_user``) ends its run with ``LINE_NEEDS_HANDS`` and ``needed_hands`` (the runtime hands the request to the
   worker, as before).

Logs are content-free: counts, ms, tokens, code words.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from yuki.brain.agent import (
    CANCELLED,
    CUTOFF_TOKENS_PLAIN,
    CUTOFF_TOKENS_STREAMED,
    EVIDENCE_CHARS,
    LEVEL_LABELS,
    LINE_NEEDS_HANDS,
    LINE_NO_REPLY,
    LINE_REFUSAL,
    STEP_WORDS,
    WARM_AFTER_S,
    WARM_TEXT,
    Brain,
    Host,
    _Request,
    _State,
)
from yuki.brain.context import (
    CHECK_LABEL,
    Conversation,
    answer_report,
    clip,
    gather,
    read_card,
    read_menu,
    read_profile_parts,
    request_turn,
    tool_result,
)
from yuki.brain.planner import CallCancelled
from yuki.brain.settings import BrainSettings, OneSettings
from yuki.log.events import Final, Partial, Say, Thinking, ToolCall, ToolResult
from yuki.log.requests import usage_tokens
from yuki.one import budget as B
from yuki.one import policy as P
from yuki.one import prompt as PR
from yuki.one import tools as T
from yuki.one.countdown import Countdown
from yuki.one.host import lane_tools, needs_worker

#: Code's line for a verified act that said nothing of its own (the runner's ``said`` was empty).
LINE_DONE = "Done."
#: A reply written while the user added words is not their answer: the model goes again with their words.
REPLY_HELD = (f"{CHECK_LABEL} Your reply above was not sent: the user added this before it went out. Take it into "
              f"account, then reply again (or act).")
#: A call whose input was cut off (the reply hit its output ceiling) is never run on a cut-short value.
BAD_INPUT_TEXT = "Not run: its input was cut off. Send the call again, shorter."
#: The rest of a reply after code ended the request (a settle): nothing more of it runs.
LINE_NOT_RUN = "Not run: the request is over; nothing was done."
#: Their reply to a gate's question was not a yes: that step is not done (the model decides what is left).
LINE_NOT_YES = "Their reply was not a yes, so it was not done. Leave it, unless their words ask for something else."
#: The status words of a result that did not act (the runner's or the stand-in's), for the outcome's facts.
NOT_ACTED: frozenset[str] = frozenset({"refused", "ask", "closed", "not_run", "cancelled", "changed", "declined"})
#: Result statuses sent back as errors (``is_error``): nothing was done, or it failed.
FAILED: frozenset[str] = frozenset({"refused", "failed", "error", "closed", "not_run", "cancelled"})
#: The fewest seconds between two ``Partial`` updates of the same kind (``planner.PARTIAL_GAP_S``).
PARTIAL_GAP_S = 0.12
#: The no-menu line of the ONE agent (``brain.prompt.NO_MENU`` speaks of the planner's routes).
NO_SKILLS = "Skills: none are loaded just now."
#: A round cut at its output ceiling is asked again once, with more room (``brain.agent.MAX_CUTOFFS``).
MAX_CUTOFFS = 1
CUTOFF_TEXT = f"{CHECK_LABEL} Your reply was cut off at its length limit; nothing of it was sent. Go on, more briefly."
#: A complete call in a reply that was cut off: not run either (the reply as a whole is asked again).
CUT_CALL_TEXT = "Not run: the reply was cut off before it ended. Send the call again."


# ---------------------------------------------------------------------------------------------------------------------
# One model call
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Use:
    """One ``tool_use`` block of a reply, in written order."""

    id: str
    name: str
    input: dict[str, Any]
    bad: bool = False


@dataclass
class Reply:
    """One round's complete response."""

    content: list[Any]
    text: str
    uses: list[Use]
    stop_reason: str | None
    usage: Any
    ms: float
    first_token_ms: float | None
    model: str
    hard: bool = False


def reply_uses(content: Sequence[Any], stop_reason: str | None) -> list[Use]:
    """Every ``tool_use`` block of a reply, in written order. A reply cut at its output ceiling (``max_tokens``) has
    its last block's input marked bad when that block is a tool_use (never run on a cut-short value)."""
    out: list[Use] = []
    blocks = list(content or [])
    for i, block in enumerate(blocks):
        get = (lambda k, d=None, b=block: b.get(k, d)) if isinstance(block, Mapping) else \
            (lambda k, d=None, b=block: getattr(b, k, d))
        if get("type") != "tool_use":
            continue
        raw = get("input")
        bad = not isinstance(raw, Mapping) or (stop_reason == "max_tokens" and i == len(blocks) - 1)
        out.append(Use(str(get("id") or ""), str(get("name") or ""), dict(raw) if isinstance(raw, Mapping) else {},
                       bad=bad))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------------------------------


def _as_result(r: Any) -> Any:
    return SimpleNamespace(**r) if isinstance(r, Mapping) else r


def _field(r: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(r, name, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def result_status(r: Any) -> str:
    return str(_field(r, "status", "") or "").strip().lower()


def result_ok(r: Any) -> bool:
    """Whether a runner result goes back as a success (``ok`` when it says, else by its status)."""
    ok = _field(r, "ok", None)
    if isinstance(ok, bool):
        return ok
    return result_status(r) not in FAILED


def result_acted(r: Any) -> bool:
    """Whether the call did something on the PC (``acted`` when the result says, else its status is not one of
    :data:`NOT_ACTED`)."""
    acted = _field(r, "acted", None)
    if isinstance(acted, bool):
        return acted
    return result_status(r) not in NOT_ACTED and bool(_field(r, "verified", False) or result_ok(r))


def result_flagged(r: Any) -> bool:
    """Whether the act's diff came back flagged (``flags``: the names of the flags set, or a mapping of them)."""
    flags = _field(r, "flags", ())
    if isinstance(flags, Mapping):
        return any(bool(v) for v in flags.values())
    if isinstance(flags, (list, tuple, set, frozenset)):
        return any(bool(f) for f in flags)
    return bool(flags)


def result_text(r: Any, limit: int) -> str:
    """The tool result the model reads: the runner's own ``text`` when it gives one (a method or a string), else its
    fields in order (status, what code said, the evidence, the After diff, the flags, a line), cut at ``limit``."""
    text = _field(r, "text", "")
    if callable(text):
        try:
            text = text()
        except Exception:  # noqa: BLE001
            text = ""
    if isinstance(text, str) and text.strip():
        return clip(text, limit)
    parts: list[str] = []
    status = result_status(r)
    if status:
        parts.append(f"status: {status}")
    said = str(_field(r, "said", "") or "").strip()
    if said:
        parts.append(f"code already told them: {said}")
    for name, label in (("evidence", ""), ("after", "After: "), ("line", "")):
        value = str(_field(r, name, "") or "").strip()
        if value and value != said:
            parts.append(f"{label}{value}")
    flags = _field(r, "flags", ()) or ()
    if isinstance(flags, Mapping):
        flags = [k for k, v in flags.items() if v]
    if isinstance(flags, (list, tuple, set, frozenset)) and flags:
        parts.append("flags: " + ", ".join(str(f) for f in flags))
    return clip("\n".join(parts) or "ok", limit)


def typed_menu(menu_lines: str) -> tuple[str, list[str]]:
    """The ONE typed agent's Skills block and keys from the menu lines the request read (``gather``'s, the planner's
    heading first): the talker's heading over the same lines (``run_skill`` with the key: the ONE tool's words, not the
    planner's routes), and their keys (``voice.mouth.menu_keys``, the run_skill enum)."""
    from yuki.skills.menu import PLANNER_HEAD, TALKER_HEAD
    from yuki.voice.mouth import menu_keys

    lines = str(menu_lines or "").strip()
    if lines.startswith(PLANNER_HEAD):
        lines = lines[len(PLANNER_HEAD):].strip()
    keys = menu_keys(lines)
    if not keys:
        return NO_SKILLS, []
    return f"{TALKER_HEAD}\n{lines}", keys


@dataclass
class _ReplyFacts:
    """What one reply's calls came to (the settle rule's facts, addendum must-fix 2)."""

    acts: int = 0
    other: int = 0
    unverified: int = 0
    flagged: bool = False
    then_left: bool = False
    settle: str | None = None
    handoff: bool = False

    @property
    def clean(self) -> bool:
        """Every call of the reply acted and verified, nothing flagged, every ``then`` empty."""
        return self.acts > 0 and not self.other and not self.unverified and not self.flagged and not self.then_left


# ---------------------------------------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------------------------------------


class OneAgent(Brain):
    """The ONE agent on one runtime lane (typed requests; a spoken one the runtime hands it runs the same way on the
    voice channel's gate and budget). Made by :func:`yuki.one.host.build_agent`.

    Args (beyond :class:`yuki.brain.agent.Brain`'s):
        hands: ``False`` on the front desk: it only looks; the first call that needs the worker hands the request over.
        runner: The runner (a fake in checks); ``None``: ``runner_factory(self)`` on first use.
        runner_factory: Builds the runner for this agent (``yuki.one.host.make_runner`` by default).
    """

    def __init__(self, settings: Any = None, logger: Any = None, *, client: Any = None, backend: Any = None,
                 dispatcher: Any = None, memory: Any = None, lane: str = "worker", record_requests: bool = True,
                 hands: bool = True, runner: Any = None, runner_factory: Callable[[Any], Any] | None = None) -> None:
        super().__init__(settings, logger, client=client, backend=backend, dispatcher=dispatcher, memory=memory,
                         lane=lane, record_requests=record_requests)
        self.hands = bool(hands)
        self._runner = runner
        self._runner_factory = runner_factory
        self._runner_lock = threading.Lock()
        #: The one countdown host (``one.countdown``): ``SendPending`` to the island through this agent's events; Stop
        #: (``cancel``) and the chips (``send_choice``) reach it here. Its seconds follow ``send_countdown_s`` (the
        #: runtime sets it on the worker) at each request's start.
        self.one_countdown = Countdown(publish=self._push, seconds=0.0, said_name=self._said_name,
                                       log=self.logger.log)
        #: The previous request's gate state (a question nobody answered, the sends that went: ``Turn.carry``).
        self._prev_turn: P.Turn | None = None
        self._turn: P.Turn | None = None
        self._os: OneSettings | None = None
        #: This request's calls, by the Dispatcher verb they ran as (memory's turn log; never their values).
        self._verbs_run: list[str] = []
        self._lines_lock = threading.Lock()
        self._lines: list[str] = []
        self._ended_by = ""
        #: This request's acts that did not verify, and the last one's evidence (the outcome's, for a non-success).
        self._unverified = 0
        self._evidence = ""

    # -- the client: the ONE chain ------------------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The failover client (``host.one_client``: the ONE model's chain stops after Sonnet 5), made on first use; a
        client given at construction is used as it is."""
        with self._client_lock:
            if self._client is None:
                from yuki.one.host import one_client

                self._client = one_client(self.settings, log=self.logger.log)
            return self._client

    # -- the caller surface the ONE agent changes ---------------------------------------------------------------------

    def cancel(self, reason: str = CANCELLED) -> None:
        """Stop the run (``Brain.cancel``) and an open send countdown with it (Stop / Esc on the island)."""
        super().cancel(reason)
        try:
            if self.one_countdown.cancel():
                self._send_stopped = False
        except Exception:  # noqa: BLE001
            pass

    def send_choice(self, kind: str, value: str) -> bool:
        """A chip tapped on the open send countdown: the countdown ends ``changed`` with the swap (the runner tells the
        model, which sends again to the chosen one or not)."""
        try:
            return bool(self.one_countdown.swap(kind, value))
        except Exception:  # noqa: BLE001
            return False

    def pause(self, why: str = "") -> bool:
        """The user took over (``Brain.pause``), except while a send's countdown is open: their hand on the mouse then
        is for the island's Stop or a chip, and a pause would stop the send's own steps halfway once it goes."""
        if self.one_countdown.counting:
            return False
        return super().pause(why)

    def status_line(self) -> str | None:
        """The running request's status in plain words, by code (``None`` when none runs)."""
        if not self._running:
            return None
        if self._awaiting_answer:
            return "Waiting for your answer."
        if self.paused:
            return "Paused while you use the mouse or keyboard."
        now = STEP_WORDS.get(self._status_verb, "")
        return f"Working on it, now {now}." if now else "Working on it."

    def warm_cache(self, effort: str | None = None, thinking: bool | None = None) -> bool | None:
        """Write the ONE typed prefix's cache (the lane's tools and the three system blocks) and open the connection,
        one token out, when nothing warmed it in the last :data:`WARM_AFTER_S` and no request runs. The worker only
        (the front desk's tools differ). ``True`` ran, ``False`` skipped, ``None`` failed. Its spend is a
        ``startup_cost`` record. Content-free log; never raises."""
        del effort, thinking
        if not self.hands:
            return False
        with self._warm_lock:
            if self._running or self._warming or (self._warm_at and time.monotonic() - self._warm_at < WARM_AFTER_S):
                self.logger.log("model_prewarm", skipped="one: warm or busy")
                return False
            self._warming = True
        started = time.perf_counter()
        try:
            return self._warm_one()
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            self.logger.log("model_prewarm", ok=False, error=type(exc).__name__,
                            ms=round((time.perf_counter() - started) * 1000, 1))
            return None
        finally:
            self._warming = False

    def _warm_one(self) -> bool:
        from yuki.brain.prompt import profile_text

        started = time.perf_counter()
        os_, bs = OneSettings.read(self.settings), BrainSettings.read(self.settings)
        names = list(getattr(self.settings, "user_names", []) or [])
        card = read_card(self.memory)
        brief, rules = read_profile_parts(self.memory)
        profile = profile_text(names=names, brief=None if card else brief, rules=rules, limit=bs.profile_chars)
        _menu, lines, _ids = read_menu(self.memory, self.settings, limit=bs.menu_chars)
        menu, keys = typed_menu(lines)
        effort = bs.effort_of("typed")[0]
        params = self._params(os_, bs, PR.typed_blocks(profile, menu, skill_keys=keys),
                              [{"role": "user", "content": [{"type": "text", "text": WARM_TEXT}]}],
                              lane_tools(keys, worker=True), effort)
        params["max_tokens"] = 1
        message = self.client.messages.create(**params)
        self._warm_at = time.monotonic()
        ms = round((time.perf_counter() - started) * 1000, 1)
        model = self._used_model(os_.model)
        tokens = usage_tokens(getattr(message, "usage", None))
        try:
            cost = self.settings.estimate_cost(model, tokens)
        except Exception:  # noqa: BLE001
            cost = None
        self.logger.log("model_prewarm", ok=True, model=model, ms=ms, tokens=tokens, one=True)
        self.logger.log("startup_cost", kind="model_prewarm", lane=self.lane, model=model,
                        elapsed_s=round(ms / 1000, 2), tokens=tokens, cost_usd=None if cost is None else round(cost, 6))
        return True

    # -- the runner ---------------------------------------------------------------------------------------------------

    def runner(self) -> Any:
        """This lane's runner, made once (``host.make_runner``: ``yuki.one.runner`` when merged, else reads only)."""
        with self._runner_lock:
            if self._runner is None:
                factory = self._runner_factory
                if factory is None:
                    from yuki.one.host import make_runner

                    factory = make_runner
                self._runner = factory(self)
            return self._runner

    # -- memory reads for the gate (``brain.executor``'s, copied) -----------------------------------------------------

    def _people_in(self, text: str) -> list[list[str]] | None:
        """Memory's people index: each person named in ``text`` with every name they go by (``None``: can't say)."""
        call = getattr(self.memory, "call", None)
        if not callable(call):
            return None

        def go(client: Any) -> list[list[str]]:
            from yuki.memory.contacts import named_in

            return [list(c.names) for c, _matched in named_in(getattr(client, "store", client), text)]

        try:
            got = call("one_people", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return None
        return got if isinstance(got, list) else None

    def _recent_to(self, to: str) -> list[dict[str, Any]]:
        """The user's last (at most 3) dated messages to the person ``to`` names, newest first (``[]``: can't say)."""
        call = getattr(self.memory, "call", None)
        if not callable(call) or not str(to or "").strip():
            return []

        def go(client: Any) -> list[dict[str, Any]]:
            from yuki.memory.contacts import resolve, samples

            store = getattr(client, "store", client)
            rows: list[dict[str, Any]] = []
            for person in resolve(store, to)[:1]:
                for route in person.routes:
                    rows += samples(store, route, 3)
            rows.sort(key=lambda r: -float(r.get("at") or 0.0))
            return rows[:3]

        try:
            got = call("one_recent", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return []
        return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []

    # -- the request --------------------------------------------------------------------------------------------------

    def _drive(self, req: _Request) -> None:
        os_ = self._os = OneSettings.read(self.settings)
        bs = self._bs = BrainSettings.read(self.settings)
        host = self._host_ref = Host(self, req)
        state = self._state = _State()
        self._verbs_run, self._ended_by = [], ""
        with self._lines_lock:
            self._lines = []
        self.one_countdown.seconds = float(self.send_countdown_s or 0.0)
        from yuki.agent.untrusted import new_nonce

        early, early_words, early_nonce = self._take_early()
        said = "\n".join(req.words)
        if early is not None and " ".join(early_words.split()) != " ".join(said.split()):
            self.logger.log("brain_early", dropped="other words")
            early = None
        nonce = early_nonce if early is not None and early_nonce else new_nonce()
        host.nonce = nonce
        self._set_skill_context(req, host)
        started = time.perf_counter()
        gathered = gather(dispatcher=self.dispatcher, memory=self.memory, settings=self.settings, words=said,
                          names=list(getattr(self.settings, "user_names", []) or []), limits=bs, nonce=nonce,
                          reuse=early)
        self._gather_ms = (time.perf_counter() - started) * 1000
        self._menu_lines = gathered.menu_lines
        menu, keys = typed_menu(gathered.menu_lines)
        spoken = req.channel == "voice"
        tools = lane_tools(keys, worker=self.hands, spoken=spoken)
        system = PR.typed_blocks(gathered.profile, menu, skill_keys=keys)
        conv = Conversation(request_turn(words=req.words, channel=req.channel, context=req.context, gathered=gathered,
                                         history=list(self._history), seed=None, limits=bs, nonce=nonce,
                                         understanding=req.understanding))
        effort, effort_why = bs.effort_of(req.channel, req.understanding, skills=gathered.skill_keys)
        self._effort = effort if os_.typed_thinking == "adaptive" else "off"
        from yuki.brain.check import CommitCheck

        turn = self._turn = P.Turn(
            req.channel, list(host.user_words), list(self._earlier_words)[-3:], request_id=req.request_id,
            checker=CommitCheck(lambda: self.client, self.settings, self.logger.log), people_in=self._people_in,
            recent=self._recent_to, budget=B.TurnBudget(req.channel, B.limits_for(req.channel, self.settings)),
            private_read=bool(gathered.people), log=self.logger.log).carry(self._prev_turn)
        self._checker = turn.checker()   # its calls and cost join the request's (Brain._summarize reads it)
        #: For the runner: the window in front when the user called Yuki up (Yuki's own box is in front during a typed
        #: request), a code fact (an hwnd), ``None`` when unknown.
        turn.origin_hwnd = req.origin_hwnd
        state.private_read = turn.private_read
        runner = self.runner()
        self.logger.log("one_request", channel=req.channel, lane=self.lane, hands=self.hands, model=os_.model,
                        effort=self._effort, effort_why=effort_why, thinking=os_.typed_thinking, settle=os_.settle,
                        words=len(req.words), context_chars=len(req.context), tools=len(tools), skills=len(keys),
                        overview_chars=len(gathered.overview), card_chars=len(gathered.card),
                        people_chars=len(gathered.people), private_read=turn.private_read,
                        early=early is not None, runner=getattr(runner, "kind", type(runner).__name__),
                        gather_ms=round(self._gather_ms, 1), parts_ms=gathered.parts_ms)
        try:
            self._rounds(req, host, state, os_, bs, conv, system, tools, effort, turn, runner)
        finally:
            self._prev_turn = turn
            host.acting_end()
            try:   # content-free: counts and code words; the budget's own counts under "budget"
                self.logger.log("one_turn", ended_by=self._ended_by or "error", calls=state.calls,
                                act_calls=state.steps, acted=state.acted, verbs=len(self._verbs_run),
                                level=(self._last_outcome or {}).get("level"), budget=turn.budget.facts())
            except Exception:  # noqa: BLE001 - a log never ends a request twice
                pass

    def _rounds(self, req: _Request, host: Host, state: _State, os_: OneSettings, bs: BrainSettings,
                conv: Conversation, system: list[dict[str, Any]], tools: list[dict[str, Any]], effort: str,
                turn: P.Turn, runner: Any) -> None:
        shown: list[str] = []   # code's lines already shown as progress (Say)
        cutoffs = 0
        while True:
            if self._cancelled.is_set() or not self._hold_if_paused(turn):
                self._ended_by = "cancelled"
                self._push(self._cancel_event())
                return
            spend = turn.budget.round()
            if not spend.ok:
                self._settle(state, "" if spend.silent else spend.line, shown, why=f"budget_{spend.why}")
                return
            state.calls += 1
            reply = self._call(self._params(os_, bs, system, conv.messages(), tools, effort), turn, bs)
            if reply.hard:
                turn.budget.close("hard")
                self._settle(state, B.LINE_REST_LEFT, shown, why="budget_hard")
                return
            self._record(reply, os_)
            conv.add_assistant(reply.content)   # as it came: thinking blocks and their signatures kept in-request
            if reply.stop_reason == "max_tokens" and cutoffs < MAX_CUTOFFS:
                # cut off (thinking included): once more with more room; a cut call's input is never run
                cutoffs += 1
                os_ = replace(os_, max_tokens=max(os_.max_tokens,
                                                  CUTOFF_TOKENS_STREAMED if bs.stream else CUTOFF_TOKENS_PLAIN))
                self.logger.log("one_cutoff", n=cutoffs, tools=len(reply.uses))
                conv.add_user([*(tool_result(u.id, BAD_INPUT_TEXT if u.bad else CUT_CALL_TEXT, error=True)
                                 for u in reply.uses), {"type": "text", "text": CUTOFF_TEXT}])
                continue
            if not reply.uses:
                if reply.stop_reason == "refusal":
                    self._ended_by = "refusal"
                    self._final(LINE_REFUSAL, self._outcome_one(state, level="answer", line=LINE_REFUSAL))
                    return
                if self._steer_waiting() and not turn.budget.closed:
                    self.logger.log("steer", held_final="reply")
                    conv.add_user([{"type": "text", "text": REPLY_HELD}, *self._added(host, turn)])
                    continue
                self._end_with_text(reply.text, state, shown)
                return
            results, facts = self._run_calls(reply.uses, host, state, os_, turn, runner)
            if facts.handoff:
                self._ended_by = "handoff"
                self._final(LINE_NEEDS_HANDS, self._outcome_one(state, level="answer", line=LINE_NEEDS_HANDS))
                return
            lines = self._take_lines()
            if facts.settle is not None:
                self._settle(state, facts.settle, shown + lines, why="gate")
                return
            if self._cancelled.is_set():
                self._ended_by = "cancelled"
                self._push(self._cancel_event())
                return
            if os_.settle == "code" and facts.clean:
                # addendum decision 2 / must-fix 2: a clean simple act ends in code, as today: code's verified line
                self._ended_by = "code"
                text = " ".join(lines) or LINE_DONE
                self._final(text, self._outcome_one(state, level="verified"))
                return
            for line in lines:   # said at verification: shown now as progress; the model reads them in its results
                self._push(Say(line))
                shown.append(line)
            conv.add_user([*results, *self._added(host, turn)])

    # -- one reply's calls --------------------------------------------------------------------------------------------

    def _run_calls(self, uses: list[Use], host: Host, state: _State, os_: OneSettings, turn: P.Turn,
                   runner: Any) -> tuple[list[dict[str, Any]], _ReplyFacts]:
        """Every call of one reply, in written order (a later one waits for the one before). Each has a result (the
        API's rule: every tool_use answered), the ones after a settle or a cancel "not run"."""
        results: list[dict[str, Any]] = []
        facts = _ReplyFacts()
        for use in uses:
            if facts.settle is not None or self._cancelled.is_set():
                results.append(tool_result(use.id, LINE_NOT_RUN, error=True))
                continue
            if use.bad:
                facts.other += 1
                results.append(tool_result(use.id, BAD_INPUT_TEXT, error=True))
                continue
            if not self.hands and needs_worker(use.name, use.input):
                # the front desk only looks: the request goes to the worker as it came (its reads are read again there)
                self.needed_hands = True
                facts.handoff = True
                self.logger.log("one_handoff", tool=use.name, lane=self.lane)
                return results, facts
            if use.name == "ask_user":
                facts.other += 1
                results.append(tool_result(use.id, self._ask_user(use.input, host, turn)))
                continue
            if not self._hold_if_paused(turn):
                results.append(tool_result(use.id, LINE_NOT_RUN, error=True))
                continue
            spec = T.spec_of(use.name)
            acts = spec is not None and spec.acts
            if acts and self.hands:
                host.acting_begin()   # "Yuki is acting" for memory, and the takeover watch
            verb = self._emit_call(use.name, use.input)
            r = self._run_one(runner, use.name, use.input, turn, host)
            text = result_text(r, os_.result_chars)
            ok = result_ok(r)
            self._emit_result(verb, r, ok, text)
            results.append(tool_result(use.id, text, error=not ok))
            if acts:
                facts.acts += 1
                state.kind = "act"
                state.steps += 1
                ran = result_acted(r)
                if ran:
                    state.acted = True
                if not bool(_field(r, "verified", False)):
                    facts.unverified += 1   # the reply is not clean: the model gets its next round
                    if ran:   # it ran and was not seen to work (a refused act did nothing: not counted here)
                        self._unverified += 1
                        evidence = str(_field(r, "evidence", "") or "").strip()
                        if evidence:
                            self._evidence = evidence
                if result_flagged(r):
                    facts.flagged = True
                if T.then_of(use.input):
                    facts.then_left = True
            else:
                facts.other += 1
            if bool(_field(r, "settle", False)):
                facts.settle = "" if bool(_field(r, "silent", False)) else str(_field(r, "line", "") or "")
        return results, facts

    def _run_one(self, runner: Any, name: str, args: Mapping[str, Any], turn: P.Turn, host: Host) -> Any:
        """One call through the runner; a gate's question is asked here (budget not counting the wait), the reply read
        by the check model (fail closed), recorded as a code fact (``Turn.answer``), and on a yes the same call goes to
        the gate again (allowed that once)."""
        started = time.perf_counter()
        r = self._through(runner, name, args, turn)
        if result_status(r) == "ask" and str(_field(r, "question", "") or "").strip():
            question = " ".join(str(_field(r, "question", "")).split())
            with turn.budget.excluded():
                answer = host.ask_text(question)
                yes = bool(answer and answer.strip()) and not self._cancelled.is_set() \
                    and self._judge_answer(question, answer)
            turn.answer(yes)
            self._sync_words(turn, host)
            self.logger.log("one_ask", tool=name, answered=answer is not None, yes=yes)
            if yes and not self._cancelled.is_set():
                r = self._through(runner, name, args, turn)
            else:
                r = SimpleNamespace(status="declined", ok=False, acted=False, verified=False,
                                    text=f"{answer_report(question, answer)}\n{LINE_NOT_YES}")
        self._tool_s += time.perf_counter() - started
        return r

    def _through(self, runner: Any, name: str, args: Mapping[str, Any], turn: P.Turn) -> Any:
        try:
            got = runner.run(name, dict(args or {}), turn, self._speak)
        except Exception as exc:  # noqa: BLE001 - a runner error is a failed call, never a crash
            self.logger.log("error", where="one_runner", tool=name, error=type(exc).__name__)
            return SimpleNamespace(status="error", ok=False, verified=False,
                                   text="Not done: Yuki's runner failed on that call; nothing more is known.")
        return _as_result(got)

    def _speak(self, text: str) -> None:
        """The runner's ``speak``: code's line at verification (typed: kept, then shown as progress or as the reply)."""
        line = " ".join(str(text or "").split())
        if line:
            with self._lines_lock:
                self._lines.append(line)

    def _take_lines(self) -> list[str]:
        with self._lines_lock:
            out, self._lines = self._lines, []
        return out

    def _ask_user(self, args: Mapping[str, Any], host: Host, turn: P.Turn) -> str:
        """``ask_user`` (the worker's): one question, the reply as the user's own words; the wait is not counted."""
        question = " ".join(str(dict(args or {}).get("question") or "").split())
        if not question:
            return "ask_user needs question."
        with turn.budget.excluded():
            answer = host.ask_text(question)
        self._sync_words(turn, host)
        return answer_report(question, answer)

    def _hold_if_paused(self, turn: P.Turn) -> bool:
        """The user took over: wait here (not counted) until they resume (``True``) or it ends (``False``)."""
        if not self.paused:
            return True
        with turn.budget.excluded():
            return self._hold_paused()

    @staticmethod
    def _sync_words(turn: P.Turn, host: Host) -> None:
        """The gate reads the user's words as they are now (their answers and additions joined them)."""
        turn.words = [" ".join(str(w).split()) for w in host.user_words if str(w or "").strip()]

    def _added(self, host: Host, turn: P.Turn) -> list[dict[str, Any]]:
        """What the user added mid-request (their words; a chip they tapped), as text blocks after the last tool result:
        their own words, never harness text."""
        blocks = self._steer_turn(host)
        self._sync_words(turn, host)
        return blocks

    # -- events -------------------------------------------------------------------------------------------------------

    def _emit_call(self, name: str, args: Mapping[str, Any]) -> str:
        """The call as the island and the window show a step: the Dispatcher verb it runs as, with its input (the words
        for the user and ``then`` left out) and the step's words. Returns the verb."""
        mapped = T.as_verb(name, args)
        verb, vinput = mapped if mapped is not None else (name, dict(args or {}))
        self._status_verb = verb
        self._tool_counts[verb] += 1
        self._verbs_run.append(verb)
        self._push(ToolCall(verb, {**dict(vinput or {}), "_about": {"what": STEP_WORDS.get(verb, verb)}}))
        return verb

    def _emit_result(self, verb: str, r: Any, ok: bool, text: str) -> None:
        summary = "ok" if ok else (clip(" ".join(text.split()), 120) or result_status(r) or "not done")
        self._push(ToolResult(verb, ok, summary, payload=_field(r, "payload", None),
                              about={"what": STEP_WORDS.get(verb, verb)}))

    # -- the model call -----------------------------------------------------------------------------------------------

    def _params(self, os_: OneSettings, bs: BrainSettings, system: list[dict[str, Any]],
                messages: list[dict[str, Any]], tools: list[dict[str, Any]], effort: str) -> dict[str, Any]:
        """One round's request: the ONE model, the lane's tools, the three cached system blocks, the request's
        append-only messages; typed thinking adaptive at the request's effort (pinned for the request), or off
        (``fast``). ``tool_choice`` auto: a reply may hold several calls (run in order)."""
        params: dict[str, Any] = {"model": os_.model, "max_tokens": int(os_.max_tokens), "system": system,
                                  "messages": messages, "tools": tools}
        if os_.typed_thinking == "fast":
            from yuki.config import thinking_off

            params["thinking"] = thinking_off(os_.model)
        else:
            params["thinking"] = {"type": "adaptive", "display": bs.thinking_display}
            params["output_config"] = {"effort": effort}
        return params

    def _call(self, params: dict[str, Any], turn: P.Turn, bs: BrainSettings) -> Reply:
        """One round, streamed through the failover client (plain when ``stream`` is off): thinking and the reply as
        ``Partial`` while they come, each thinking block as :class:`Thinking`. A cancel drops the stream
        (``CallCancelled``); the hard stop drops it too (``Reply.hard``)."""
        started = time.perf_counter()
        model = str(params.get("model") or "")
        if not bs.stream:
            final = self.client.messages.create(**params)
            content = list(getattr(final, "content", None) or [])
            for block in content:
                if getattr(block, "type", None) == "thinking" and str(getattr(block, "thinking", "") or "").strip():
                    self._push(Thinking(str(block.thinking)))
            stop = getattr(final, "stop_reason", None)
            text = "".join(str(getattr(b, "text", "") or "") for b in content if getattr(b, "type", None) == "text")
            ms = (time.perf_counter() - started) * 1000
            return Reply(content, text, reply_uses(content, stop), stop, getattr(final, "usage", None), ms, ms, model)
        first: float | None = None
        blocks: dict[int, dict[str, Any]] = {}
        texts: list[str] = []
        partial_at: dict[str, float] = {}
        tool_started = False
        hard = False
        final: Any = None

        def partial(what: str, text: str) -> None:
            now = time.monotonic()
            if now - partial_at.get(what, 0.0) >= PARTIAL_GAP_S:
                partial_at[what] = now
                self._push(Partial(what, text))

        mgr = self.client.messages.stream(**params)
        stream = mgr.__enter__()
        try:
            for event in stream:
                if self._cancelled.is_set():
                    self._ended_by = "cancelled"
                    raise CallCancelled()
                if turn.budget.hard_due():
                    hard = True
                    break
                kind = getattr(event, "type", None)
                if first is None and kind in ("content_block_start", "content_block_delta"):
                    first = time.perf_counter()
                index = int(getattr(event, "index", 0) or 0)
                if kind == "content_block_start":
                    btype = getattr(getattr(event, "content_block", None), "type", None)
                    blocks[index] = {"type": btype, "text": []}
                    if btype == "tool_use":
                        tool_started = True
                elif kind == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    slot = blocks.setdefault(index, {"type": None, "text": []})
                    if dtype == "thinking_delta":
                        slot["text"].append(str(getattr(delta, "thinking", "") or ""))
                        partial("thinking", "".join(slot["text"]))
                    elif dtype == "text_delta":
                        piece = str(getattr(delta, "text", "") or "")
                        slot["text"].append(piece)
                        texts.append(piece)
                        if not tool_started:
                            partial("reply", "".join(texts))
                elif kind == "content_block_stop":
                    slot = blocks.get(index) or {}
                    if slot.get("type") == "thinking":
                        text = "".join(slot.get("text") or [])
                        if text.strip():
                            self._push(Thinking(text))
            if not hard:
                final = stream.get_final_message()
        finally:
            try:
                mgr.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        ms = (time.perf_counter() - started) * 1000
        first_ms = None if first is None else (first - started) * 1000
        if hard:
            return Reply([], "", [], "hard_stop", None, ms, first_ms, model, hard=True)
        content = list(getattr(final, "content", None) or [])
        stop = getattr(final, "stop_reason", None)
        text = "".join(str(getattr(b, "text", "") or "") for b in content if getattr(b, "type", None) == "text")
        return Reply(content, text or "".join(texts), reply_uses(content, stop), stop, getattr(final, "usage", None),
                     ms, first_ms, model)

    def _record(self, reply: Reply, os_: OneSettings) -> None:
        """One round's accounting (as the planner's) and its content-free log line."""
        model = self._used_model(reply.model)
        self._warm_at = time.monotonic()   # the prefix was just read (or written): warm for an hour
        self._model_s += reply.ms / 1000
        self._account(model, reply.usage, ms=reply.ms, role="planner")
        used = usage_tokens(reply.usage)
        self._call_stats.append({"out": used["output_tokens"], "in": used["input_tokens"],
                                 "cache_read": used["cache_read_tokens"], "cache_write": used["cache_write_tokens"],
                                 "tools": len(reply.uses), "ms": round(reply.ms, 1), "stop": reply.stop_reason,
                                 "first_token_ms": None if reply.first_token_ms is None
                                 else round(reply.first_token_ms, 1)})
        self.logger.log("one_call", model=model, stop_reason=reply.stop_reason, tools=len(reply.uses),
                        bad=sum(1 for u in reply.uses if u.bad), ms=round(reply.ms, 1),
                        first_token_ms=None if reply.first_token_ms is None else round(reply.first_token_ms, 1),
                        tokens=used, text_chars=len(reply.text or ""), thinking=os_.typed_thinking)

    # -- endings ------------------------------------------------------------------------------------------------------

    def _end_with_text(self, text: str, state: _State, shown: list[str]) -> None:
        """The model ended the request with a reply and no call: its words are the answer; the level is code's facts
        (every act verified, some not, or no act)."""
        self._ended_by = "model"
        line = str(text or "").strip() or " ".join(shown) or LINE_NO_REPLY
        if state.kind == "act":
            level = "verified" if not self._unverified and state.acted else ("unverified" if state.acted else "failed")
        else:
            level = "answer"
        self._final(line, self._outcome_one(state, level=level, line=line))

    def _settle(self, state: _State, line: str, shown: list[str], *, why: str) -> None:
        """Code ends the request (a budget limit, the gate's second refusal, the hard stop): its line, no model call.
        A silent settle shows what code already said (or rule #149's line)."""
        self._ended_by = why
        text = " ".join(str(line or "").split()) or " ".join(shown) or B.LINE_REST_LEFT
        # the request did not get to its end: what acted is a part (Brain._finish_stuck's levels)
        self._final(text, self._outcome_one(state, level="failed" if state.acted else "answer", line=text,
                                            partial=state.acted))

    def _outcome_one(self, state: _State, *, level: str, line: str = "", partial: bool = False) -> dict[str, Any]:
        """The ``Final.outcome`` dict (``finish.outcome_verdict``'s keys), from code facts only. A non-success ending
        carries its evidence (the last unverified act's, at most :data:`EVIDENCE_CHARS`) for the voice's own words."""
        out: dict[str, Any] = {
            "level": level, "label": LEVEL_LABELS.get(level, ""), "acted": bool(state.acted), "seen": "",
            "send": None, "partial": bool(partial), "line": "" if level == "verified" else line,
            "step": STEP_WORDS.get(self._verbs_run[-1], "") if self._verbs_run and state.kind == "act" else "",
            "steps": int(state.steps), "open": 0 if level in ("verified", "answer") else int(self._unverified)}
        if level in ("unverified", "failed") and self._evidence:
            out["evidence"] = clip(self._evidence, EVIDENCE_CHARS)
        else:
            out["evidence"] = ""   # weighed here: nothing to add (Brain._final drops an empty one)
        return out

    def _begin(self, request: str, user_words: Any, channel: str) -> None:
        super()._begin(request, user_words, channel)
        self._unverified = 0
        self._evidence = ""

    def _observe_seed(self, outcome: Mapping[str, Any] | None) -> None:
        """No hand-off check is read as an observer here: the ONE agent takes no skill hand-off (a job's
        ``plan_seed`` is not used, and nothing reads the screen after the request ended)."""
        del outcome

    def _log_turn(self, req: _Request, *, reply: str, outcome: str) -> None:
        """The exchange to memory's turn log (the user's words, the reply, the verbs that ran: never their values)."""
        log_turn = getattr(self.memory, "log_turn", None)
        if not callable(log_turn):
            return
        logger = self.logger

        def done(report: dict[str, Any]) -> None:
            logger.log("memory_turn_logged", **{k: v for k, v in report.items() if k in ("row", "error", "elapsed_ms")})

        try:
            queued = log_turn(request_id=f"{self.lane}-{req.number}", at=req.epoch, user_text=req.shown,
                              reply_text=reply, actions=list(self._verbs_run), outcome=outcome, on_done=done)
            self.logger.log("memory_turn_queued", queued=bool(queued), outcome=outcome, reply_chars=len(reply))
        except Exception as exc:  # noqa: BLE001
            self.logger.log("error", where="memory_turn", error=type(exc).__name__)


__all__ = ["BAD_INPUT_TEXT", "LINE_DONE", "LINE_NOT_RUN", "LINE_NOT_YES", "NO_SKILLS", "OneAgent", "REPLY_HELD",
           "Reply", "Use", "reply_uses", "result_acted", "result_ok", "result_status", "result_text", "typed_menu"]
