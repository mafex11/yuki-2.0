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
   (``yuki.one.runner.Runner``, lane oRun: budget -> gate -> desk -> act -> desk -> diff -> result), so every call
   passes the one policy gate (fail closed on an unknown tool). The model reads each call's ``OneResult.for_model()``
   as plain lines (:func:`model_text`). A gate's question (status ``asked``) is asked here (``AskUser``), the yes read
   by the check model and recorded as a code fact (``Runner.answer``), and the same call goes to the gate again (a yes
   allows it once); a skill's own question goes to the model with their answer. The tools the drivers run themselves
   (status ``driver``: ``ask_user``, ``quick_memory``) run here once the gate allowed them. A reply cut at its output
   ceiling is never kept: it is asked again once with more room (``Brain._after_cutoff``'s rule).
4. **Who ends it.** The model, by replying with no call (one22, A15: held once when the gate's turn says so,
   :func:`closing_hold`: the turn's one miss, nothing tried after it, time left; the model goes again with the hold's
   sentence, :data:`HOLD_HELD`). Code ends a clean reply as today (addendum decision 2,
   must-fix 2; ``one_settle = "code"``) by the runner's one settle rule (``yuki.one.runner.settles``: every call took,
   nothing flagged, every ``then`` empty): code's verified line is the reply, no closing call. A settle by the gate or
   the budget (the second refusal, the hard stop, the rounds) ends it with code's line (rule #149).
5. **The front desk** (``hands`` false) only looks: the first call that needs the worker (an act, ``remember``,
   ``ask_user``) ends its run with ``LINE_NEEDS_HANDS`` and ``needed_hands`` (the runtime hands the request to the
   worker, as before).

Logs are content-free: counts, ms, tokens, code words.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
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
    LINE_STUCK,
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
from yuki.log.events import ErrorEvent, Final, Partial, Say, Thinking, ToolCall, ToolResult
from yuki.log.requests import usage_tokens
from yuki.one import budget as B
from yuki.one import policy as P
from yuki.one import prompt as PR
from yuki.one import tools as T
from yuki.one.countdown import Countdown
from yuki.one.host import LINE_UNBUILT, fresh_desk, lane_tools, needs_worker, ready_for_request

#: Code's line for a verified act that said nothing of its own (the runner's ``said`` was empty).
LINE_DONE = "Done."
#: A reply written while the user added words is not their answer: the model goes again with their words.
REPLY_HELD = (f"{CHECK_LABEL} Your reply above was not sent: the user added this before it went out. Take it into "
              f"account, then reply again (or act).")
#: One22 (A15, contract C9 on typed): a closing reply of words the gate's turn holds once (``policy.Turn.closing_hold``:
#: the turn's one miss, nothing tried after it, the time for another way still open): not shown; the model goes again
#: with the hold's own sentence (``budget.LINE_HOLD_MISS``), as the voice driver's held round does.
HOLD_HELD = f"{CHECK_LABEL} Your reply above was not shown to them yet. {{why}}"
#: A call whose input was cut off (the reply hit its output ceiling) is never run on a cut-short value.
BAD_INPUT_TEXT = "Not run: its input was cut off. Send the call again, shorter."
#: The rest of a reply after code ended the request (a settle): nothing more of it runs.
LINE_NOT_RUN = "Not run: the request is over; nothing was done."
#: Their reply to a gate's question was not a yes: that step is not done (the model decides what is left).
LINE_NOT_YES = "Their reply was not a yes, so it was not done. Leave it, unless their words ask for something else."
#: A tool the runner gave back for its driver to run that this driver does not run (another channel's control).
LINE_NOT_DRIVEN = "Not run: {tool} is not a tool here; nothing was done."
#: A runner that broke on a call (it never should: it fails closed itself).
LINE_RUNNER_BROKE = "Not done: Yuki's runner failed on that call; nothing more is known."
# The runner's status words (``yuki.one.runner``, lane oRun: code words; the agent reads them in plain words).
ASKED, DRIVER = "asked", "driver"
#: Statuses after which something was done on the PC for the call, or it was found already so: the request's acts.
RAN: frozenset[str] = frozenset({"done", "already", "no_change", "not_confirmed", "failed", "starting"})
#: Statuses of an act that took (the request's level), unless a read showed it didn't (``verified`` False).
TOOK: frozenset[str] = frozenset({"done", "already"})
#: Statuses sent back as errors (``is_error``): nothing was done for it, or it failed.
NOT_OK: frozenset[str] = frozenset({"refused", "asked", "failed", "busy", "not_done", "declined", "error"})
#: The labels of a result's fields the model reads (``OneResult.for_model``; the prompt calls the act's diff "After").
FIELD_LABELS: dict[str, str] = {"after": "After", "said": "said by code (already shown to them)",
                                # one20 (S3): the measured tab-cover sentence code did not show: theirs to hear
                                "cover": "left behind your act (tell them in a few words)"}
#: The fewest seconds between two ``Partial`` updates of the same kind (``planner.PARTIAL_GAP_S``).
PARTIAL_GAP_S = 0.12
#: The no-menu line of the ONE agent (``brain.prompt.NO_MENU`` speaks of the planner's routes).
NO_SKILLS = "Skills: none are loaded just now."
#: A round cut at its output ceiling is asked again once, with more room (``brain.agent.MAX_CUTOFFS``); the cut reply
#: is never kept (a thinking block cut short has no signature, a call cut short no whole input).
MAX_CUTOFFS = 1


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


def model_text(fields: Mapping[str, Any]) -> str:
    """A result's fields as plain lines for the model (``OneResult.for_model()``, a driver's report): ``label: value``
    in their order, a value of several lines under its label (a fenced read stays whole), a flag as yes / no, a list
    joined, a mapping (a chip they tapped) as ``key: value`` pairs; an empty field, or one that repeats an earlier one
    word for word (a refusal's line is its result too), left out."""
    lines: list[str] = []
    seen: set[str] = set()
    for key, value in dict(fields or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, Mapping):
            value = "; ".join(f"{k}: {v}" for k, v in value.items())
        elif isinstance(value, (list, tuple, set, frozenset)):
            value = ", ".join(str(v) for v in value)
        text = str(value).strip()
        if not text or (len(text) > 3 and text in seen):
            continue
        seen.add(text)
        label = FIELD_LABELS.get(str(key), str(key).replace("_", " "))
        lines.append(f"{label}:\n{text}" if "\n" in text else f"{label}: {text}")
    return "\n".join(lines) or "ok"


@dataclass
class Got:
    """One call's result as the driver reads it: the tool result's text for the model, whether it goes back as an error
    (``ok`` false), and the request's facts from code: ``ran`` (something was done on the PC for it, or it was found
    already so), ``took`` (done or already so, not read back as failed), ``failed``, a settle (``settle``, ``silent``,
    ``line``), the evidence of a non-success (plain text), a send's state for the outcome (``send``: ``confirmed`` /
    ``unconfirmed``), the UI's payload of a read. ``one``: the runner's own result (``OneResult``; the settle rule
    reads it), ``None`` for a result the driver made itself."""

    status: str
    text: str
    ok: bool
    ran: bool = False
    took: bool = False
    failed: bool = False
    settle: bool = False
    silent: bool = False
    line: str = ""
    evidence: str = ""
    send: str | None = None
    payload: Any = None
    one: Any = None


def read_result(r: Any, limit: int) -> Got:
    """The runner's ``OneResult`` (lane oRun) as the driver reads it: the model's text is its ``for_model()`` in plain
    lines (:func:`model_text`), cut at ``limit``; ``ok`` / ``ran`` / ``took`` from its status, ``refused`` and
    ``verified`` (never from a field it lacks: a result that says nothing ran nothing). A :class:`Got` is returned as
    it is."""
    if isinstance(r, Got):
        return r
    status = str(getattr(r, "status", "") or "").strip().lower() or "error"
    for_model = getattr(r, "for_model", None)
    try:
        fields = for_model() if callable(for_model) else {"status": status}
    except Exception:  # noqa: BLE001 - its fields can't be read: its status alone
        fields = {"status": status}
    verified = getattr(r, "verified", None)
    refused = bool(getattr(r, "refused", False)) or status in ("refused", ASKED)
    ran = status in RAN
    took = ran and status in TOOK and verified is not False
    send = None
    if str(getattr(r, "countdown", "") or "") == "sent":
        send = "confirmed" if status == "done" and verified is True else "unconfirmed"
    return Got(status, clip(model_text(fields), limit), ok=not refused and status not in NOT_OK and verified is not False,
               ran=ran, took=took, failed=ran and (status == "failed" or verified is False),
               settle=bool(getattr(r, "settle", False)), silent=bool(getattr(r, "silent", False)),
               line=" ".join(str(getattr(r, "line", "") or "").split()),
               evidence="" if took else str(fields.get("evidence") or "") if isinstance(fields, Mapping) else "",
               send=send, payload=getattr(r, "payload", None) if status == "read" else None, one=r)


def runner_settles(results: Sequence[Any]) -> bool:
    """The runner's one settle rule over one reply's results (``yuki.one.runner.settles``, addendum must-fix 2: every
    call took, nothing flagged, every ``then`` empty; a forced settle is the driver's own path). ``False`` when it
    can't be read: the model then has its round (never a settle on a guess)."""
    if not results:
        return False
    try:
        from yuki.one.runner import settles   # lane oRun

        return bool(settles(list(results)))
    except Exception:  # noqa: BLE001
        return False


def tidy_debt(runner: Any, turn: Any) -> dict[str, Any] | None:
    """The runner's tidy debt for a reply code would settle (``yuki.one.runner.Runner.tidy_debt``: the net After and
    flags of an earlier flagged act whose cover is still there, once a turn), else ``None``. Never raises."""
    measure = getattr(runner, "tidy_debt", None)
    if not callable(measure):
        return None
    try:
        got = measure(turn)
    except Exception:  # noqa: BLE001 - no measure: code settles as before
        return None
    return dict(got) if isinstance(got, Mapping) and got else None


def closing_hold(turn: Any) -> str | None:
    """One22 (A15, contract C9): why the request's closing reply of words is held once, else ``None``: the gate's turn
    (``policy.Turn.closing_hold``: the budget's one-miss reading), else the turn's budget's own
    (``budget.TurnBudget.closing_hold``), read through ``getattr`` as the voice driver does. Pure. Never raises."""
    for owner in (turn, getattr(turn, "budget", None)):
        fn = getattr(owner, "closing_hold", None)
        if not callable(fn):
            continue
        try:
            got = fn()
        except Exception:  # noqa: BLE001 - no reading: no hold
            return None
        text = " ".join(str(got or "").split())
        return text or None
    return None


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
    """What one reply's calls came to. ``results``: the runner's own results (the settle rule reads them:
    ``yuki.one.runner.settles``); ``own``: calls the driver answered itself (a cut input, an ask, ``ask_user``,
    ``quick_memory``, a call not run): a reply with one never ends in code. ``settle``: code's line when the gate or
    the budget ended the request (``""``: silent)."""

    results: list[Any] = field(default_factory=list)
    own: int = 0
    settle: str | None = None
    handoff: bool = False


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

    #: A ONE lane's agent (the runtime reads it: no front-desk tool list applies; the agent hands over itself).
    one_agent = True

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
        #: This request's acts (:class:`Got`: ran, took, failed), the last non-success's evidence and the last send's
        #: state (the outcome's facts, from code).
        self._ran = 0
        self._took = 0
        self._failed = 0
        self._evidence = ""
        self._send: str | None = None

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
        """This lane's runner, made once (``host.make_runner``: ``yuki.one.runner.Runner`` over the lane's hands;
        raises when it can't be made)."""
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
        try:
            runner = self.runner()
        except Exception as exc:  # noqa: BLE001 - no runner: the job fails plainly, nothing runs, no other brain
            self._ended_by = "unbuilt"
            self.logger.log("one_unbuilt", why=type(exc).__name__, lane=self.lane)
            self._push(ErrorEvent(LINE_UNBUILT))
            return
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
        ready_for_request(runner, skill_keys=keys, menu=menu)
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
        held = False   # one22 (A15): the closing hold, once a request
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
            if reply.stop_reason == "max_tokens":
                # cut off at its output ceiling (thinking included): never kept (a thinking block cut short has no
                # signature, a call cut short no whole input) and nothing of it runs; asked again once with more room,
                # the conversation as it was (Brain._after_cutoff's rule); a second cut ends it with code's line
                cutoffs += 1
                self.logger.log("one_cutoff", n=cutoffs, tools=len(reply.uses))
                if cutoffs > MAX_CUTOFFS:
                    self._settle(state, LINE_STUCK.format(where=" partway" if state.acted else "", why=""), shown,
                                 why="cutoff")
                    return
                ceiling = CUTOFF_TOKENS_STREAMED if bs.stream else CUTOFF_TOKENS_PLAIN
                os_ = replace(os_, max_tokens=max(os_.max_tokens, min(ceiling, os_.max_tokens * 2)))
                continue
            conv.add_assistant(reply.content)   # as it came: thinking blocks and their signatures kept in-request
            if not reply.uses:
                if reply.stop_reason == "refusal":
                    self._ended_by = "refusal"
                    self._final(LINE_REFUSAL, self._outcome_one(state, level="answer", line=LINE_REFUSAL))
                    return
                if self._steer_waiting() and not turn.budget.closed:
                    self.logger.log("steer", held_final="reply")
                    conv.add_user([{"type": "text", "text": REPLY_HELD}, *self._added(host, turn)])
                    continue
                why = None if held or turn.budget.closed else closing_hold(turn)
                if why:
                    # one22 (A15): the turn's one miss with nothing tried after it and time left: held once, the
                    # model gets one more round with the hold's sentence (the voice driver's C9 hold, typed)
                    held = True
                    self.logger.log("one_hold", closing="words", round=state.calls, why_chars=len(why))
                    conv.add_user([{"type": "text", "text": HOLD_HELD.format(why=why)}, *self._added(host, turn)])
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
                ending = " ".join(facts.settle.split())
                for line in lines:   # an earlier call's line of this reply (its done line) is shown before the end
                    if line != ending:
                        self._push(Say(line))
                        shown.append(line)
                self._settle(state, ending, shown, why="gate")
                return
            if self._cancelled.is_set():
                self._ended_by = "cancelled"
                self._push(self._cancel_event())
                return
            if os_.settle == "code" and not facts.own and runner_settles(facts.results):
                # final-B (one-desk D3/F6): an earlier flagged act's cover still there with no tidy step taken: the
                # net After goes on this reply's last result and the model gets one round (once a turn)
                debt = tidy_debt(runner, turn)
                # integration T5: under "do" the debt's own step is code's, and its line went to ``_speak`` after the
                # reply's lines were taken: it is part of this reply (never cleared unseen by the next request)
                lines += self._take_lines()
                if debt is None:
                    # addendum decision 2 / must-fix 2: a clean reply ends in code, as today, by the runner's one
                    # settle rule: code's verified line (said at verification) is the reply, no closing model call
                    self._ended_by = "code"
                    text = " ".join(lines) or LINE_DONE
                    self._final(text, self._outcome_one(state, level=self._level()))
                    return
                if results and isinstance(results[-1], dict):
                    last = results[-1]
                    old = "".join(str(b.get("text") or "") for b in last.get("content") or [] if isinstance(b, dict))
                    results[-1] = tool_result(str(last.get("tool_use_id") or ""),
                                              f"{old}\n{model_text(debt)}".strip(), error=bool(last.get("is_error")))
            for line in lines:   # said at verification: shown now as progress; the model reads them in its results
                self._push(Say(line))
                shown.append(line)
            conv.add_user([*results, *self._added(host, turn)])

    # -- one reply's calls --------------------------------------------------------------------------------------------

    def _run_calls(self, uses: list[Use], host: Host, state: _State, os_: OneSettings, turn: P.Turn,
                   runner: Any) -> tuple[list[dict[str, Any]], _ReplyFacts]:
        """Every call of one reply, in written order (a later one waits for the one before), each through the runner
        (the one gate decides it, fail closed). Each has a result (the API's rule: every tool_use answered), the ones
        after a settle or a cancel "not run"."""
        results: list[dict[str, Any]] = []
        facts = _ReplyFacts()
        if not self.hands:
            needs = next((u for u in uses if not u.bad and needs_worker(u.name, u.input)), None)
            if needs is not None:
                # the front desk only looks: a reply with a call that needs the worker goes there as it came, before
                # any of its calls runs here (the worker reads again what it needs)
                self.needed_hands = True
                facts.handoff = True
                self.logger.log("one_handoff", tool=needs.name, lane=self.lane)
                return results, facts
        for use in uses:
            if facts.settle is not None or self._cancelled.is_set():
                facts.own += 1
                results.append(tool_result(use.id, LINE_NOT_RUN, error=True))
                continue
            if use.bad:
                facts.own += 1
                results.append(tool_result(use.id, BAD_INPUT_TEXT, error=True))
                continue
            if not self._hold_if_paused(turn):
                facts.own += 1
                results.append(tool_result(use.id, LINE_NOT_RUN, error=True))
                continue
            spec = T.spec_of(use.name)
            acts = spec is not None and spec.acts
            if acts and self.hands:
                host.acting_begin()   # "Yuki is acting" for memory, and the takeover watch
                fresh_desk(runner)    # the verbs act only on windows they were shown
            verb = self._emit_call(use.name, use.input)
            got = self._run_one(runner, use.name, use.input, turn, host, os_.result_chars)
            self._emit_result(verb, got)
            results.append(tool_result(use.id, got.text, error=not got.ok))
            if got.one is not None:
                facts.results.append(got.one)
            else:
                facts.own += 1
            if acts:
                state.kind = "act"
                state.steps += 1
            self._tally(state, got)
            if got.settle:
                facts.settle = "" if got.silent else got.line
        return results, facts

    def _tally(self, state: _State, got: Got) -> None:
        """One call's facts into the request's (its level, the evidence of the last non-success, a send's state)."""
        if got.ran:
            state.acted = True
            self._ran += 1
            self._took += 1 if got.took else 0
            self._failed += 1 if got.failed else 0
            if not got.took and got.evidence:
                self._evidence = got.evidence
        if got.send:
            self._send = got.send

    def _run_one(self, runner: Any, name: str, args: Mapping[str, Any], turn: P.Turn, host: Host,
                 limit: int) -> Got:
        """One call through the runner. A question (status ``asked``) is asked here, the wait not counted
        (:meth:`_asked`); a tool the runner gives back to its driver (status ``driver``) runs here
        (:meth:`_driver`)."""
        started = time.perf_counter()
        got = read_result(self._through(runner, name, args, turn), limit)
        if got.status == ASKED:
            got = self._asked(runner, name, args, turn, host, got, limit)
        elif got.status == DRIVER:
            got = self._driver(name, args, turn, host, limit)
        self._tool_s += time.perf_counter() - started
        return got

    def _asked(self, runner: Any, name: str, args: Mapping[str, Any], turn: P.Turn, host: Host, got: Got,
               limit: int) -> Got:
        """A call that came back asked. The gate's question (its decision asked, or the turn holds that question open):
        asked here, the reply read by the check model (fail closed), recorded as a code fact (``Runner.answer``), and on
        a yes the same call goes to the gate again (allowed that once). A skill's own question (which one, who to):
        asked here, their answer goes to the model with the result (it runs the skill again with it). No question, or
        the request was stopped: the model reads the result as it is."""
        question = " ".join(str(getattr(got.one, "question", "") or "").split())
        if not question or self._cancelled.is_set():
            return got
        decision = getattr(got.one, "decision", None)
        gate = getattr(decision, "kind", None) == P.ASK or any(
            " ".join(str(q).split()) == question for q, _row in list(getattr(turn, "asks", {}).values()))
        with turn.budget.excluded():
            answer = host.ask_text(question)
            yes = gate and bool(answer and answer.strip()) and not self._cancelled.is_set() \
                and self._judge_answer(question, answer)
        if gate:
            answered = getattr(runner, "answer", None)
            if callable(answered):
                answered(turn, yes)
            else:
                turn.answer(yes)
        self._sync_words(turn, host)
        self.logger.log("one_ask", tool=name, gate=gate, answered=answer is not None, yes=yes)
        if not gate:
            return Got("answered", clip(f"{got.text}\n{answer_report(question, answer)}", limit), True)
        if yes and not self._cancelled.is_set():
            return read_result(self._through(runner, name, args, turn), limit)
        return Got("declined", f"{answer_report(question, answer)}\n{LINE_NOT_YES}", False)

    def _driver(self, name: str, args: Mapping[str, Any], turn: P.Turn, host: Host, limit: int) -> Got:
        """A tool the gate allowed that the driver runs itself (``yuki.one.runner.DRIVER_TOOLS``): ``ask_user`` (the
        worker's: one question, their reply as their own words) and ``quick_memory`` (the voice's quick memory read or
        to-do add, :meth:`_quick_memory`). Any other is not a typed tool: not run."""
        if name == "ask_user":
            return Got("answered", self._ask_user(args, host, turn), True)
        if name == "quick_memory":
            return self._quick_memory(args, limit)
        return Got("not_run", LINE_NOT_DRIVEN.format(tool=name), False)

    def _quick_memory(self, args: Mapping[str, Any], limit: int) -> Got:
        """``quick_memory`` (``voice.recall.QuickMemory.run``, blocking on this worker thread): its report as plain
        lines, what memory holds fenced as data (its handling note, Yuki's own words, after the fence). Content-free
        log line."""
        started = time.perf_counter()
        action = str(dict(args or {}).get("action") or "")
        try:
            from yuki.voice.recall import QuickMemory

            payload, facts = QuickMemory(self.memory).run(dict(args or {}))
        except Exception as exc:  # noqa: BLE001 - a failed read is said as one
            payload, facts = {"failed": f"memory could not be read ({type(exc).__name__})"}, {"ok": False}
        report = dict(payload) if isinstance(payload, Mapping) else {"found": str(payload or "")}
        note = str(report.pop("how_to_handle_it", "") or "").strip()
        ok = bool(dict(facts or {}).get("ok"))
        from yuki.agent.untrusted import new_nonce, wrap

        nonce = str(getattr(self._host_ref, "nonce", "") or "") or new_nonce()
        text = wrap(model_text(report), source="their memory", nonce=nonce)
        self.logger.log("one_quick", action=action, ok=ok, items=dict(facts or {}).get("items"),
                        ms=round((time.perf_counter() - started) * 1000, 1))
        return Got("read" if ok else "failed", clip(f"{text}\n{note}" if note else text, limit), ok)

    def _through(self, runner: Any, name: str, args: Mapping[str, Any], turn: P.Turn) -> Any:
        try:
            return runner.run(name, dict(args or {}), turn, self._speak)
        except Exception as exc:  # noqa: BLE001 - a runner error is a failed call, never a crash
            self.logger.log("error", where="one_runner", tool=name, error=type(exc).__name__)
            return Got("error", LINE_RUNNER_BROKE, False)

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

    def _emit_result(self, verb: str, got: Got) -> None:
        """The step's result on the island and the window: ``ok``, or its own line (else its status) in a few words."""
        summary = "ok" if got.ok else (clip(" ".join((got.line or got.text).split()), 120) or got.status or "not done")
        self._push(ToolResult(verb, got.ok, summary, payload=got.payload, about={"what": STEP_WORDS.get(verb, verb)}))

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
        self._final(line, self._outcome_one(state, level=self._level() if state.kind == "act" else "answer", line=line))

    def _level(self) -> str:
        """The request's level from code's facts of its acts: none ran (refused, asked, stopped): ``answer``; every one
        took: ``verified``; every one failed: ``failed``; else ``unverified``."""
        if not self._ran:
            return "answer"
        if self._took >= self._ran:
            return "verified"
        return "failed" if self._failed >= self._ran else "unverified"

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
            "send": self._send, "partial": bool(partial), "line": "" if level == "verified" else line,
            "step": STEP_WORDS.get(self._verbs_run[-1], "") if self._verbs_run and state.kind == "act" else "",
            "steps": int(state.steps),
            "open": 0 if level in ("verified", "answer") else max(0, self._ran - self._took)}
        if level in ("unverified", "failed") and self._evidence:
            out["evidence"] = clip(self._evidence, EVIDENCE_CHARS)
        else:
            out["evidence"] = ""   # weighed here: nothing to add (Brain._final drops an empty one)
        return out

    def _begin(self, request: str, user_words: Any, channel: str) -> None:
        super()._begin(request, user_words, channel)
        self._ran = self._took = self._failed = 0
        self._evidence = ""
        self._send = None

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


__all__ = ["BAD_INPUT_TEXT", "FIELD_LABELS", "Got", "HOLD_HELD", "LINE_DONE", "LINE_NOT_RUN", "LINE_NOT_YES",
           "NO_SKILLS", "NOT_OK", "OneAgent", "RAN", "REPLY_HELD", "Reply", "TOOK", "Use", "closing_hold", "model_text",
           "read_result", "reply_uses", "runner_settles", "tidy_debt", "typed_menu"]
