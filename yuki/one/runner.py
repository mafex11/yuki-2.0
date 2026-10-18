"""The ONE runner (phase 4 design §1, §2.2, §4; addendum must-fix 2, 3, 4, 6; oRun): one tool call of the ONE agent.

``Runner.run(name, args, turn, speak=None) -> OneResult`` runs one call the agent made, on the caller's worker thread
(blocking; :meth:`Runner.arun` offloads it from an event loop). In order:

1. **The verb**: the ONE tool resolves to its verb (``yuki.one.tools.SPECS``). An unknown tool, a typed-only tool on
   voice and a closed turn go to the gate and are refused there (fail closed).
2. **The gate at the verb's authorizer point** (``yuki.one.policy.gate``): the hands decide nothing themselves under
   a ONE call. The runner sets its :class:`CallContext` on the worker thread (``voice.hands.one_call``) and the verbs'
   own authorizer points ask it once their target is known (``Hands._check_hand`` after an app's windows or a tab's
   title are found; the click and typing checks once the control's role and name are read; the placement verbs; a
   skill's own check). The first of them is the agent's own call (counted on the turn budget); later targets of the
   same call, and the skill engine's steps (``SkillRunner._held``, ``chat._judged``), are the gate's inner steps.
   ``run_skill`` is decided before it starts (its target is its skill), so a send's countdown runs between the gate
   and the run. Nothing acts without an allowing decision (``Hands._act`` fails closed).
3. **The desk lease** (``actions.leases.input_lease``) held by the worker thread that runs the verb, so a voice act and
   a typed act never interleave (sound-only verbs and reads take none, as the hands' audio verbs never did). Typed:
   the lease wait is not counted on the budget.
4. **The act diff**: the desk is read right after the gate allowed (before the act) and again once the act's done line
   is said; ``perception.diff.compare`` with whose change is whose (``Hands._scope``: acted, touched) gives the After
   text (at most 500 characters) and its three flags. The act's own windows and the attention windows before it go to
   the turn (``Turn.flag``: what a tidy-up may move or give the front back to). New windows become actable by id.
5. **The result**: :class:`OneResult` (``for_model()`` is what the agent reads): status, verified, what it did, code's
   line said, the After diff, flags, ``then``, the evidence on a non-success (``yuki.one.reads.ending_facts``), the
   question of an ask, a refusal's line (said by code when final), and ``settle`` when code ends the turn.

**Code's verified line**: a verb verified by its own read-back (or by the effect read, ``reads.read_effect``, when its
report could not say) says its done line at once (``say_when_done``, else code's own), unless its ``then`` holds more
of the request: never a done line for half.

**The settle rule** (addendum must-fix 2, :func:`settles`, :class:`Reply`): code ends the turn (``run_llm`` false, the
lines already said) only when every call of the reply took (verified, already so, or a memory write kept), nothing was
flagged and every ``then`` is empty; a forced settle (the budget's hard stop or rounds, the second refusal) ends it with
code's one line. Otherwise the agent gets its next round with every result.

**The turn state** (:class:`RunState` on the gate's ``Turn``): every result, the act's own windows (tidy), the lines
said; ``private_read`` and the refusal count are the gate's and the budget's (``policy.Turn``, ``budget.TurnBudget``).

Logs: one content-free line per call (``one_run``: tool, verb, verdict, status, verified, ms, flags, sizes, the
budget's counts). Every helper fails closed: an error is a refusal or a failure, never an act.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from yuki.brain import consent as rule
from yuki.brain import util
from yuki.one import policy
from yuki.one import reads
from yuki.one import tools as T

# -- statuses (code words; :data:`STATUS_WORDS` is what the agent reads) ---------------------------------------------
DONE, ALREADY, NO_CHANGE, NOT_CONFIRMED, FAILED = "done", "already", "no_change", "not_confirmed", "failed"
REFUSED, ASKED, STOPPED, CHANGED, BUSY, STARTING = "refused", "asked", "stopped", "changed", "busy", "starting"
READ, WRITTEN, DRIVER, NOT_DONE = "read", "written", "driver", "not_done"
#: What the agent reads for each status.
STATUS_WORDS: dict[str, str] = {
    DONE: "done", ALREADY: "already so", NO_CHANGE: "nothing changed", NOT_CONFIRMED: "not confirmed",
    FAILED: "failed", REFUSED: "not done", ASKED: "needs their yes", STOPPED: "stopped by them",
    CHANGED: "not sent: they changed it", BUSY: "not done: the desk is busy", STARTING: "starting",
    READ: "read", WRITTEN: "kept", DRIVER: "allowed", NOT_DONE: "not done"}
#: The statuses a call may settle on (with nothing flagged and no then; ``already`` only once code's line for it was
#: heard: else the agent words it, as the talker does under ``all``).
CLEAN: frozenset[str] = frozenset({DONE, ALREADY, WRITTEN, STOPPED})
#: Tools the drivers run themselves after the gate allowed them (the conversation's controls, the voice's quick memory
#: read, the typed ask): the runner returns ``driver`` with the decision.
DRIVER_TOOLS: frozenset[str] = frozenset({"stay_quiet", "end_conversation", "go_quiet", "more_of_result",
                                          "quick_memory", "ask_user"})
#: The driver tools that end the agent's say (it chose quiet, or to end the conversation): their ``driver`` result
#: settles like a clean one (no model round after the agent chose to be quiet). The others' results are the agent's
#: to read (``more_of_result``, ``quick_memory``, ``ask_user``): never settling.
QUIET_TOOLS: frozenset[str] = frozenset({"stay_quiet", "end_conversation", "go_quiet"})
#: How long the desk lease is waited for (s) (``executor.LEASE_WAIT_S``).
LEASE_WAIT_S = 5.0
#: The After text's room (``voice.hands.AFTER_CHARS``) and the result text's (``hands``' report cap).
AFTER_CHARS = 500
RESULT_CHARS = 700
#: Code's fallback lines for a non-success (``voice.hands._FALLBACK_LINE``): the driver says one only when the agent
#: words nothing for it (the owed ledger).
OWED_LINES: dict[str, str] = {FAILED: "That didn't work.", NOT_CONFIRMED: "I couldn't confirm that worked.",
                              "blocked": "Something stopped that, so nothing was done."}
#: A send the user stopped in its countdown (``executor.LINE_STOPPED_SEND``).
LINE_STOPPED_SEND = "Stopped. Nothing was sent."
#: A send with no countdown host: never sent unseen (fail closed).
LINE_NO_COUNTDOWN = "Not sent: there's no countdown to stop it here, so nothing was sent."
#: Another send's countdown was open (never two at once): nothing was sent.
LINE_COUNTDOWN_BUSY = "Not sent: another message is counting down right now; nothing was sent."
#: The desk lease was held by another hand past the wait.
LINE_BUSY = "Not done: another hand of Yuki's is using the mouth and keyboard right now; nothing was done."
#: The runner was driven while the brain is not ``one`` (a wiring slip): nothing is done.
LINE_NOT_ONE = "Not done: Yuki's ONE brain is not switched on here, so nothing was done."
#: A drafted message whose chat Yuki couldn't see open (``executor.LINE_CHAT_UNCONFIRMED``).
LINE_CHAT_UNCONFIRMED = "I couldn't confirm the chat is open, so I didn't type anything."
#: The skill engine's outcomes after which a send did not go (``executor.NOT_SENT_OUTCOMES``).
NOT_SENT_OUTCOMES = frozenset({"asked", "handed_over", "refused", "held_game", "held_call", "failed"})


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


# ---------------------------------------------------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class OneResult:
    """One call's result.

    ``status``: a code word (:data:`STATUS_WORDS`). ``verified``: True only when a read showed the effect (its own
    read-back, or the effect read); ``None``: nothing could read it. ``result``: what it did or read, for the agent
    (capped; reads fenced). ``said``: code's line said for it (already heard: never repeated). ``owed``: code's
    fallback line for a non-success (the driver says it only if the agent words nothing). ``evidence``: on a
    non-success, what it did, where it stuck and why, what was looked for and seen. ``after``: the act's diff in plain
    words; ``flags`` / ``flagged``: the diff's three flags. ``then``: what of their request the call left for later.
    ``question``: an ask's question (the agent asks it; their yes is a code fact). ``line``: a refusal's own line (said
    by code when ``final``) or the settle line. ``settle``: code ends the turn (``run_llm`` false), ``silent``: with
    nothing said. ``heard``: code's line for this call (its done line, its refusal's or the settle line) was heard
    this turn, said for it now or the very same line earlier (``said`` is ``""`` then: never said twice).
    ``countdown``: a send's countdown state; ``swap``: a chip the user tapped. ``decision``: the gate's (RAM only).
    ``payload``: a driver tool's input, a read's payload (RAM only)."""

    name: str
    status: str
    verified: bool | None = None
    result: str = ""
    said: str = ""
    owed: str = ""
    evidence: dict[str, str] = field(default_factory=dict)
    after: str = ""
    flags: dict[str, bool] = field(default_factory=dict)
    flagged: bool = False
    then: str = ""
    question: str = ""
    line: str = ""
    final: bool = False
    settle: bool = False
    silent: bool = False
    heard: bool = False
    tidy: bool = False
    countdown: str = ""
    swap: Mapping[str, str] | None = None
    decision: Any = None
    payload: Any = None
    verb: str = ""
    ms: float = 0.0

    @property
    def refused(self) -> bool:
        """Nothing was done for it: the gate (or the verb, before acting) refused or asked."""
        return self.status in (REFUSED, ASKED)

    @property
    def clean(self) -> bool:
        """It took and leaves nothing: it may settle (:func:`settles`): done, already so (once code's line for it was
        heard: an "already" nobody heard is the agent's to word), a memory write kept, or a send they stopped; nothing
        flagged; no ``then``. The agent's own quiet or end of the conversation (:data:`QUIET_TOOLS`) settles too."""
        if self.status == DRIVER:
            return self.name in QUIET_TOOLS
        if self.status == ALREADY and not self.heard:
            return False
        return self.status in CLEAN and self.verified is not False and not self.flagged and not self.then

    def for_model(self) -> dict[str, Any]:
        """The tool result the agent reads (plain fields; ``said`` is already heard, never to be repeated)."""
        out: dict[str, Any] = {"status": STATUS_WORDS.get(self.status, self.status)}
        if self.verified is not None:
            out["verified"] = self.verified   # a read showed the effect (True) or showed it didn't take (False)
        if self.result:
            out["result"] = self.result
        if self.said:
            out["said"] = self.said
        if self.after:
            out["after"] = self.after
        if self.flagged:
            out["flags"] = [k for k, v in self.flags.items() if v]
        if self.evidence:
            out["evidence"] = reads.evidence_text(self.evidence)
        if self.question:
            out["question"] = self.question
        if self.line and self.line != self.said:
            out["line"] = self.line
        if self.then:
            out["then"] = self.then
        if self.swap:
            out["swap"] = dict(self.swap)
        return out

    def facts(self) -> dict[str, Any]:
        """Content-free fields for a log line."""
        decision = self.decision
        return {"tool": self.name, "verb": self.verb or None, "status": self.status, "verified": self.verified,
                "verdict": getattr(decision, "kind", None), "reason": getattr(decision, "reason", None),
                "row": getattr(decision, "row", None), "flagged": self.flagged,
                **{f"flag_{k}": bool(v) for k, v in self.flags.items()}, "then": bool(self.then),
                "said": bool(self.said), "heard": self.heard, "settle": self.settle, "silent": self.silent,
                "tidy": self.tidy,
                "countdown": self.countdown or None, "result_chars": len(self.result), "after_chars": len(self.after),
                "evidence": len(self.evidence), "ms": round(self.ms, 1)}


# ---------------------------------------------------------------------------------------------------------------------
# The settle rule
# ---------------------------------------------------------------------------------------------------------------------


def settles(results: Sequence[OneResult]) -> bool:
    """Whether code ends the turn on one reply's results (addendum must-fix 2): a forced settle among them (the budget,
    the second refusal), or every call clean (took, nothing flagged, no ``then``; an "already so" only once its line
    was heard; the agent's own ``stay_quiet`` / ``end_conversation`` / ``go_quiet``). No results: no."""
    got = [r for r in results or () if isinstance(r, OneResult)]
    if not got:
        return False
    if any(r.settle for r in got):
        return True
    return all(r.clean for r in got)


@dataclass(frozen=True)
class ReplyEnd:
    """How a reply ends: ``run_llm`` (the agent gets its next round), ``say``: code's settle line not said yet (``""``:
    nothing more to say), ``why``: a code word (``clean``, ``settled``, ``round``, ``waiting``)."""

    run_llm: bool
    say: str = ""
    why: str = ""


class Reply:
    """One model reply's calls, in order (the settle rule over the whole reply). ``calls``: how many the reply made (or
    the calls themselves). The driver adds each result as it comes; :meth:`run_llm` gives the ``run_llm`` of that
    call's result: false for every call but the reply's last, and for the last, false only when the reply settles."""

    def __init__(self, calls: int | Sequence[Any] = 0) -> None:
        self.expected = calls if isinstance(calls, int) else len(list(calls))
        self.results: list[OneResult] = []
        self._lock = threading.Lock()

    def add(self, result: OneResult) -> None:
        with self._lock:
            self.results.append(result)

    @property
    def complete(self) -> bool:
        return len(self.results) >= max(1, self.expected)

    def settles(self) -> bool:
        return self.complete and settles(self.results)

    def end(self) -> ReplyEnd:
        """The reply's end once its calls are in (:class:`ReplyEnd`)."""
        with self._lock:
            got = list(self.results)
        if len(got) < max(1, self.expected):
            return ReplyEnd(False, "", "waiting")
        if not settles(got):
            return ReplyEnd(True, "", "round")
        forced = next((r for r in got if r.settle), None)
        if forced is not None:
            # its line once: not when it was said for it, nor when the same line was heard earlier this turn (``heard``)
            line = "" if forced.silent or forced.heard or forced.said == forced.line else forced.line
            return ReplyEnd(False, line, "settled")
        return ReplyEnd(False, "", "clean")

    def run_llm(self, result: OneResult) -> bool:
        """Add ``result`` and give its ``run_llm``: only the reply's last result runs the agent again, and only when
        the reply does not settle."""
        self.add(result)
        return self.end().run_llm


# ---------------------------------------------------------------------------------------------------------------------
# The turn's runner state
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class RunState:
    """The runner's part of one turn (kept on the gate's ``Turn``): every call's result, the windows the turn's acts
    brought up or acted on (its own windows: a tidy-up's only targets), the code lines said (never twice), the nonce
    the turn's reads are fenced with."""

    results: list[OneResult] = field(default_factory=list)
    own: set[int] = field(default_factory=set)
    lines: set[str] = field(default_factory=set)
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))
    acted: bool = False
    #: The turn's call order on the driver's event loop (:meth:`Runner.arun`): ``(loop, asyncio.Lock)``.
    order: tuple[Any, Any] | None = None


def state_of(turn: Any) -> RunState:
    """The runner's state of ``turn`` (made on first use)."""
    state = getattr(turn, "_one_run", None)
    if not isinstance(state, RunState):
        state = RunState()
        try:
            setattr(turn, "_one_run", state)
        except Exception:  # noqa: BLE001 - a turn that takes no attribute: a state per call
            pass
    return state


# ---------------------------------------------------------------------------------------------------------------------
# One call on its worker thread: the gate at the verbs' authorizer points
# ---------------------------------------------------------------------------------------------------------------------


def _element_parts(element: Any) -> tuple[str, str]:
    """``role "name"`` element words split (Yuki's own format: ``voice.hands._element_parts``)."""
    text = _words(element)
    head, sep, rest = text.partition(' "')
    if not sep or '"' not in rest or " " in head.strip():
        return "", ""
    role = head.strip()
    return ("" if role.casefold() in ("control", "field", "item") else role), rest[:rest.rindex('"')].strip()


def _verdict(decision: Any) -> SimpleNamespace:
    """A gate decision in the hands' ``authorize`` shape (its ``one`` is the decision itself)."""
    allowed = bool(getattr(decision, "allowed", False))
    return SimpleNamespace(allowed=allowed, decision="allow" if allowed else "ask_user",
                           question=str(getattr(decision, "question", "") or ""),
                           why=str(getattr(decision, "line", "") or getattr(decision, "reason", "") or ""),
                           source=f"row{getattr(decision, 'row', 0)}" if getattr(decision, "row", 0) else
                           str(getattr(decision, "reason", "") or ""),
                           handover=False, act_off=str(getattr(decision, "act_off", "") or ""),
                           row=getattr(decision, "row", 0), countdown=bool(getattr(decision, "countdown", False)),
                           one=decision)


class CallContext:
    """One call of the ONE agent on its worker thread (``voice.hands.one_call``): what the verbs' authorizer points
    ask. :meth:`check` is the call's own step at its point (the first: the agent's own call, counted; later targets of
    the same call: inner steps by their Dispatcher verb); :meth:`authorize` is ``Hands.authorize`` under ONE (a click's
    or a typing's own control: the call's own step; the skill engine's steps: the gate's inner steps). Thread-safe."""

    def __init__(self, runner: "Runner | None", turn: policy.Turn, name: str, args: Mapping[str, Any], *,
                 settings: Any = None, desk_read: Callable[[], Any] | None = None, bracket: bool = False) -> None:
        self.runner = runner
        self.turn = turn
        self.name = str(name)
        self.args = dict(args or {})
        self.settings = settings
        self._desk_read = desk_read
        #: Read the desk right after the first allowing decision (the act's before).
        self.bracket = bool(bracket)
        self.first: Any = None
        #: The input the first decision was made on (the budget's step key: the miss is keyed the same way).
        self.first_args: dict[str, Any] = {}
        self.facts: dict[str, Any] = {}
        self.own: list[Any] = []
        self.inner: list[Any] = []
        self.resolved: dict[str, Any] | None = None
        self.before: Any = None
        #: ``run_skill``: the skill's own app (its Act switch is read on every step of the run).
        self.skill_app = ""
        #: ``run_skill`` on the hands' own skill path (not the desk hands): the windows this run's own steps typed
        #: into, hwnd -> the text the desk's registry holds there that is this run's alone (:func:`skill_step`).
        self.run_typed: dict[int, str] = {}
        #: When the call began (wall clock: an effect read looks at what happened since).
        self.started_wall = time.time()
        self._lock = threading.RLock()

    @property
    def decided(self) -> bool:
        return self.first is not None

    @property
    def allowed(self) -> bool:
        """The call's own steps were all allowed (and there was at least one)."""
        with self._lock:
            return self.first is not None and all(getattr(d, "allowed", False) for d in self.own)

    def decision(self) -> Any:
        """The decision the result reads: the first of the call's own steps that was not allowed, else the first."""
        with self._lock:
            return next((d for d in self.own if not getattr(d, "allowed", False)), self.first)

    def check(self, name: str, args: Mapping[str, Any], target: Mapping[str, Any] | None = None) -> Any:
        """The call's own step at an authorizer point, its target known (``hwnd``, ``app``, ``title``, ``role``,
        ``name``; a tab close's ``tabs``). Never raises (an error is a refusal)."""
        target = dict(target or {})
        args = dict(args or {})
        try:
            with self._lock:
                if self.first is None and str(name) == self.name:
                    decision = policy.gate(self.name, args, target, self.turn)
                    self.first, self.facts, self.first_args = decision, dict(target), dict(args)
                else:
                    tabs = target.get("tabs") if isinstance(target.get("tabs"), list) else None
                    mapped = T.as_verb(str(name), args, tab_names=tabs)
                    decision = policy.gate(mapped[0], mapped[1], target, self.turn, inner=True) if mapped is not None \
                        else policy.Decision(policy.REFUSE, "unknown_tool", line=policy.LINE_UNKNOWN)
                    if self.first is None:
                        self.first, self.facts = decision, dict(target)
                self.own.append(decision)
        except Exception:  # noqa: BLE001 - fail closed
            decision = policy.Decision(policy.REFUSE, "error", line=policy.LINE_ERROR)
            with self._lock:
                self.own.append(decision)
                if self.first is None:
                    self.first = decision
        if getattr(decision, "allowed", False) and self.bracket and self.before is None and self._desk_read:
            try:
                self.before = self._desk_read()   # right after the gate, right before the act
            except Exception:  # noqa: BLE001 - no read: no diff for this act
                self.before = None
        return decision

    def authorize(self, tool: str, tool_input: Mapping[str, Any], target: Mapping[str, Any] | None = None,
                  earlier: Sequence[str] | None = None, *, force: bool = False) -> SimpleNamespace:
        """``Hands.authorize`` under ONE (and the skill steps' ``SkillRunner._held`` / ``chat._judged``)."""
        facts = dict(target or {})
        own_verb = {"click": "click", "type": "type_text"}.get(self.name)
        if self.first is None and own_verb is not None and str(tool) == own_verb:
            if not facts.get("role") and not facts.get("name"):
                role, name = _element_parts(facts.get("element"))
                if role or name:
                    facts.update(role=role, name=name)
            if tool == "click" and force and not facts.get("role"):
                facts["role"] = "Button"
            return _verdict(self.check(self.name, self.args, facts))
        try:
            verdict = policy.authorize(self.turn, str(tool), dict(tool_input or {}), facts, earlier, force=force)
        except Exception:  # noqa: BLE001 - fail closed
            verdict = SimpleNamespace(allowed=False, decision="ask_user", question="", why=policy.LINE_ERROR,
                                      source="error", handover=False, act_off="", row=0, countdown=False)
        with self._lock:
            self.inner.append(verdict)
        return verdict

    def act_off(self, app: Any) -> str:
        """The name Settings switched "Act" off for, when ``app`` (a step's window's app) or this run's skill's own app
        is switched off; ``""`` when neither is (the turn's policy reader, else the shared one)."""
        reader = getattr(self.turn, "policy", None)
        try:
            reader = reader or util._shared_reader()
            for name in (app, self.skill_app):
                text = _words(name)
                off = reader.app_off(text) if text else None
                if off:
                    return str(off)
        except Exception:  # noqa: BLE001 - an unreadable policy never blocks (rows 1-2 read it at the gate)
            return ""
        return ""


def current() -> CallContext | None:
    """The ONE call running on this thread while the brain is ``one`` (``voice.hands.one_now``), else ``None``."""
    try:
        from yuki.voice.hands import one_now

        got = one_now()
        return got if isinstance(got, CallContext) else None
    except Exception:  # noqa: BLE001
        return None


def skill_step(hands: Any, tool: str, tool_input: Mapping[str, Any], target: Mapping[str, Any] | None,
               earlier: Sequence[str] | None = None, *, force: bool = False,
               ctx: CallContext | None = None) -> Any:
    """One skill-engine step under a ONE call (``SkillRunner._held``, ``chat._judged``): the verdict when it is held,
    else ``None`` (allowed). The Act switch of the step's app and of the run's skill's own app first; then the
    request's desk hands (``uia_route.DeskHands``: the capability's own steps and the gate's grant) with the gate on
    their lasting steps (the send key once for exactly the counted text; the join press on their plain words), or for
    any other hands the gate itself. Typing a send's own text marks it as Yuki's unsent text in that window (the gate
    reads it at the send key). Fails closed.

    The hands' own skill path (no desk hands: a capability that can neither send nor join, its learned ways): the
    run's own typing (a search box, a quick switcher) is the run's. While the desk's registry holds that text alone,
    the run's own next key or click in that window is judged by the gate's commit check with that text known (as the
    hands judge it under ``all``: ``typed_pending``), not refused as a send by code; held, the text is put back.
    Text that waited there before the run, or that joined the run's own, keeps the registry's rule."""
    ctx = ctx if ctx is not None else current()
    if ctx is None:
        return SimpleNamespace(allowed=False, question="", source="no_check", why="no ONE call on this thread",
                               handover=False, act_off="", row=0)
    facts = dict(target or {})
    off = ctx.act_off(facts.get("app"))
    if off:
        return SimpleNamespace(allowed=False, question="", source="act_off", act_off=off, handover=False, row=2,
                               why=f"Act is switched off for {off} in Yuki's settings")
    try:
        from yuki.skills.uia_route import DeskHands
    except Exception:  # noqa: BLE001
        DeskHands = ()   # type: ignore[assignment]
    if DeskHands and isinstance(hands, DeskHands):
        try:
            verdict = hands.authorize(tool, dict(tool_input or {}), facts, list(earlier or []), force=force)
        except Exception:  # noqa: BLE001 - fail closed
            return SimpleNamespace(allowed=False, question="", source="error", why=policy.LINE_ERROR,
                                   handover=False, act_off="", row=0)
        if not getattr(verdict, "allowed", False):
            return verdict
        own = str(getattr(verdict, "why", "") or "") if str(getattr(verdict, "source", "")) == "capability" else ""
        hwnd = facts.get("hwnd")
        if own == "text" and tool == "type_text" and hwnd is not None:
            policy.mark_typed(hwnd, str((tool_input or {}).get("text") or ""))
        if own in ("send", "join"):
            gated = ctx.authorize(tool, tool_input, facts, earlier, force=force)
            if not getattr(gated, "allowed", False):
                return gated
        return None
    return _own_run_step(ctx, tool, tool_input, facts, earlier, force=force)


def _hwnd_of(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out or None


def _submits(tool: str, tool_input: Mapping[str, Any]) -> bool:
    """The gate's own reading of a step that sends what waits in its window (``policy._submits``: any click, Enter,
    typing with Enter); unsure: not (the registry's rule then stands)."""
    try:
        return bool(policy._submits(str(tool), dict(tool_input or {})))
    except Exception:  # noqa: BLE001
        return False


def _own_run_step(ctx: CallContext, tool: str, tool_input: Mapping[str, Any], facts: dict[str, Any],
                  earlier: Sequence[str] | None, *, force: bool) -> Any:
    """One step of a run on the hands' own skill path through the gate (inner), the run's own typing kept as the run's
    (:func:`skill_step`'s docstring). ``None`` allowed, else the verdict."""
    hwnd = _hwnd_of(facts.get("hwnd") if facts.get("hwnd") is not None else (tool_input or {}).get("hwnd"))
    released: str | None = None
    if hwnd is not None and hwnd in ctx.run_typed and _submits(tool, tool_input):
        waiting = policy.typed_text(hwnd)
        if waiting is not None and waiting == ctx.run_typed[hwnd]:
            # only this run's own typing waits there: its own key or click is the commit check's, with the text known
            policy.clear_typed(hwnd)
            released = waiting
            facts["typed_pending"], facts["typed_text"] = True, waiting
        else:
            ctx.run_typed.pop(hwnd, None)   # other text joined it, or it expired: the registry's rule stands
    before = policy.typed_text(hwnd) if hwnd is not None and tool == "type_text" and released is None else None
    try:
        verdict = ctx.authorize(tool, tool_input, facts, earlier, force=force)
    except Exception:  # noqa: BLE001 - fail closed
        verdict = SimpleNamespace(allowed=False, question="", source="error", why=policy.LINE_ERROR,
                                  handover=False, act_off="", row=0)
    allowed = bool(getattr(verdict, "allowed", False))
    if released is not None:
        ctx.run_typed.pop(hwnd, None)
        if not allowed:
            if policy.typed_text(hwnd) is None:
                policy.mark_typed(hwnd, released)   # held: it still waits there, Yuki's, unsent
        elif tool == "type_text" and policy.typed_text(hwnd):
            ctx.run_typed[hwnd] = str(policy.typed_text(hwnd))   # what the gate marked after it: the run's alone
    elif tool == "type_text" and hwnd is not None and allowed:
        now = policy.typed_text(hwnd)
        if now and (before is None or before == ctx.run_typed.get(hwnd)):
            ctx.run_typed[hwnd] = now   # the registry's text there is this run's alone
        else:
            ctx.run_typed.pop(hwnd, None)
    return None if allowed else verdict


# ---------------------------------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------------------------------


class _Lease:
    """The desk lease for one act on this thread (``input_lease``, re-entrant per thread); typed: its wait is not
    counted on the budget. ``got`` false: another hand held it past the wait."""

    def __init__(self, turn: Any, *, wanted: bool, wait_s: float) -> None:
        self.turn, self.wanted, self.wait_s = turn, wanted, wait_s
        self.stack = ExitStack()
        self.got = True

    def __enter__(self) -> "_Lease":
        if not self.wanted:
            return self
        try:
            from yuki.actions.leases import LeaseBusy, input_lease
        except Exception:  # noqa: BLE001 - no lease module: the act goes as it would on the hands
            return self
        budget = getattr(self.turn, "budget", None)
        wait = budget.excluded() if budget is not None and getattr(self.turn, "channel", "") == "typed" \
            else nullcontext()
        try:
            with wait:
                self.stack.enter_context(input_lease(timeout_s=self.wait_s))
        except LeaseBusy:
            self.got = False
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stack.close()


class Runner:
    """Runs the ONE agent's tool calls (module docstring).

    Args:
        verbs: The hand verbs (``voice.hands.Verbs`` over the conversation's or the lane's ``Hands``; a stand-in with
            the same surface in checks).
        channel: ``voice`` | ``typed`` (the turns it runs are of that channel).
        settings: The settings the brain mode is read from (``yuki.one.modes``; ``None``: the process's mode).
        countdown: The one send countdown host (``yuki.one.countdown.Countdown``); ``None``: a send is refused (never
            sent unseen).
        memory: The memory the reads, writes and skills use (``None``: the verbs').
        dispatcher: The Dispatcher reads, ``look`` ids and a send's skill run go through (``None``: the verbs'). It must
            be the verbs' own (``Verbs.dispatcher``, the hands' ``_dispatcher``) when given: a ``click`` / ``type`` /
            ``slider`` by ``[id]`` reads the ids from the verbs' dispatcher's latest window read, so ``look window``
            through any other dispatcher leaves ids no verb finds. Leave it ``None`` unless a check needs a stand-in.
        log: ``log(event, **fields)``, content-free.
        speak: ``speak(text)``: code's lines (the verified done line, a final refusal's line, the settle line), said
            the moment they are known; called on the worker thread (voice: a thread-safe push, :func:`speaker`).
            ``None``: nothing is said by the runner (the driver reads ``said``/``line``).
        menu: The Skills menu text (``memory`` kind ``skills``).
        lease: Take the desk lease for acts (checks may turn it off).
        effect_budget_s: How long an act whose own report could not verify it has its effect read.
    """

    def __init__(self, verbs: Any, *, channel: str = "voice", settings: Any = None, countdown: Any = None,
                 memory: Any = None, dispatcher: Any = None, log: Callable[..., None] | None = None,
                 speak: Callable[[str], Any] | None = None, menu: str = "", lease: bool = True,
                 lease_wait_s: float = LEASE_WAIT_S, effect_budget_s: float = reads.EFFECT_BUDGET_S) -> None:
        self.verbs = verbs
        self.channel = "voice" if channel == "voice" else "typed"
        self.settings = settings if settings is not None else getattr(verbs, "settings", None)
        self.countdown = countdown
        self._memory = memory
        self._dispatcher = dispatcher
        self.log = log
        self.speak = speak
        self.menu = menu
        self.lease = bool(lease)
        self.lease_wait_s = float(lease_wait_s)
        self.effect_budget_s = float(effect_budget_s)

    @property
    def memory(self) -> Any:
        return self._memory if self._memory is not None else getattr(self.verbs, "memory", None)

    @property
    def dispatcher(self) -> Any:
        return self._dispatcher if self._dispatcher is not None else getattr(self.verbs, "dispatcher", None)

    # -- the entry points ------------------------------------------------------------------------------------------

    def run(self, name: str, args: Mapping[str, Any] | None, turn: policy.Turn,
            speak: Callable[[str], Any] | None = None) -> OneResult:
        """One call, blocking (the caller's worker thread: the verb runs here, holding the desk lease here). Never
        raises: an error is a failure, nothing more done."""
        started = time.perf_counter()
        name = str(name or "")
        args = dict(args) if isinstance(args, Mapping) else {}
        say = speak if speak is not None else self.speak
        state = state_of(turn)
        try:
            result = self._run(name, args, turn, say, state)
        except Exception as exc:  # noqa: BLE001 - fail closed
            result = OneResult(name, FAILED, verified=False, result=f"{type(exc).__name__}",
                               evidence={"why": "Yuki's runner broke on this call"}, owed=OWED_LINES[FAILED])
            self._emit("one_run_error", tool=name, error=type(exc).__name__)
        result.ms = (time.perf_counter() - started) * 1000.0
        budget = getattr(turn, "budget", None)
        if budget is not None and budget.late() and not result.settle:
            # the turn was closed while this ran (the hard stop): its result settles silently (code said the owed line)
            result.settle, result.silent = True, True
        state.results.append(result)
        self._emit("one_run", **result.facts(), **(budget.facts() if budget is not None else {}))
        return result

    async def arun(self, name: str, args: Mapping[str, Any] | None, turn: policy.Turn,
                   speak: Callable[[str], Any] | None = None) -> OneResult:
        """:meth:`run` on a worker thread (never on the event loop), the turn's calls one after another in the order
        they arrived (Pipecat starts a reply's calls side by side, in the order written; ``asyncio.Lock`` wakes its
        waiters first come, first served: the hands' order gate, ``Hands._enter_order``)."""
        state = state_of(turn)
        loop = asyncio.get_running_loop()
        if state.order is None or state.order[0] is not loop:
            state.order = (loop, asyncio.Lock())
        async with state.order[1]:
            return await asyncio.to_thread(self.run, name, args, turn, speak)

    @staticmethod
    def answer(turn: policy.Turn, yes: bool) -> None:
        """The user answered the gate's newest question (the driver read their reply): a yes is a code fact for that
        very step (allowed once, not asked again); a no drops it."""
        turn.answer(bool(yes))

    # -- one call ----------------------------------------------------------------------------------------------------

    def _run(self, name: str, args: dict[str, Any], turn: policy.Turn, say: Any, state: RunState) -> OneResult:
        from yuki.one.modes import is_one

        if not is_one(self.settings):
            # the hands listen to the runner only while the brain is one: a runner driven under another brain (a
            # wiring slip, e.g. settings that coerced "one" away) refuses everything rather than act on another
            # brain's checks
            self._emit("one_run_mode", tool=name, mode="not_one")
            return OneResult(name, REFUSED, result=LINE_NOT_ONE, line=LINE_NOT_ONE, verb=name)
        spec = T.spec_of(name)
        budget = getattr(turn, "budget", None)
        if spec is None or turn.channel not in spec.channels or (budget is not None and budget.closed):
            return self._refused(name, policy.gate(name, args, {}, turn), say, state, turn)
        if name == "scroll" and args.get("keep") == "stop":
            # stopping Yuki's own scrolling always works (``hands._stop_scrolling``): a stop, not an act
            ok, report, facts = self.verbs.run(name, args)
            return OneResult(name, DONE if ok else FAILED, verified=bool(ok), result=report[:RESULT_CHARS],
                             verb="scroll")
        if spec.kind == T.CONTROL or name in DRIVER_TOOLS:
            decision = policy.gate(name, args, {}, turn)
            if not decision.allowed:
                return self._refused(name, decision, say, state, turn)
            return OneResult(name, DRIVER, decision=decision, payload=dict(args), verb=decision.verb)
        if spec.kind == T.READ:
            return self._read(name, args, turn, state, say)
        if spec.kind == T.WRITE:
            return self._write(name, args, turn, state, say)
        if name == "run_skill":
            return self._skill(args, turn, say, state)
        return self._act(name, args, turn, spec, say, state)

    def _refused(self, name: str, decision: Any, say: Any, state: RunState,
                 turn: policy.Turn | None = None) -> OneResult:
        """A call the gate did not allow: refused (its line; said by code when final or when it settles the turn) or
        asked (its question, for the agent to ask)."""
        kind = getattr(decision, "kind", policy.REFUSE)
        status = ASKED if kind == policy.ASK else REFUSED
        line = str(getattr(decision, "line", "") or "")
        settle = bool(getattr(decision, "settle", False))
        silent = bool(getattr(decision, "silent", False))
        final = bool(getattr(decision, "final", False))
        said, heard = "", False
        if line and not silent and (final or settle):
            said, heard = self._speak(line, say, state)
        focus = getattr(decision, "focus", None)
        if focus and getattr(decision, "kind", "") == policy.REFUSE and turn is not None:
            self._bring_forward(int(focus), turn)
        return OneResult(name, status, verified=None, result=line if not final else "",
                         question=str(getattr(decision, "question", "") or ""), line=line, final=final,
                         settle=settle, silent=silent, said=said, heard=heard, decision=decision,
                         verb=str(getattr(decision, "verb", "") or ""), tidy=bool(getattr(decision, "tidy", False)))

    def _bring_forward(self, hwnd: int, turn: policy.Turn) -> None:
        """A refused join's window brought forward for the user's own click (``Executor._bring_forward``): never with a
        game in front (read again), inside the desk lease and the acting marker. Never raises."""
        try:
            if rule.game_holds(rule.Live(game=rule.game_now(turn.board_now()))):
                return
            hands = getattr(self.verbs, "hands", None)
            acting = hands._acting("window") if hands is not None and hasattr(hands, "_acting") else nullcontext()
            with _Lease(turn, wanted=self.lease, wait_s=self.lease_wait_s) as lease:
                if lease.got:
                    with acting:
                        self.dispatcher.dispatch("focus_window", {"hwnd": int(hwnd)})
                    self._emit("one_forward", ok=True)
        except Exception as exc:  # noqa: BLE001 - the refusal's line still says it
            self._emit("one_forward", ok=False, error=type(exc).__name__)

    def _say(self, line: str, say: Any, state: RunState) -> str:
        """Code's line, said once a turn (``""``: nothing said now)."""
        return self._speak(line, say, state)[0]

    @staticmethod
    def _speak(line: str, say: Any, state: RunState) -> tuple[str, bool]:
        """Code's line, said once a turn: ``(said now, heard)``; ``heard`` is also true when the very same line was
        said earlier this turn (not said again). No speaker, or one that failed: neither (the driver reads ``line``)."""
        text = _words(line)
        if not text or say is None:
            return "", False
        key = text.casefold()
        if key in state.lines:
            return "", True
        try:
            say(text)
        except Exception:  # noqa: BLE001 - not said: the driver reads ``line``
            return "", False
        state.lines.add(key)
        return text, True

    # -- reads and writes ----------------------------------------------------------------------------------------------

    def _read(self, name: str, args: dict[str, Any], turn: policy.Turn, state: RunState, say: Any) -> OneResult:
        facts = self._facts_of(args.get("window"))
        decision = policy.gate(name, args, facts, turn)
        if not decision.allowed:
            return self._refused(name, decision, say, state, turn)
        got = reads.read(name, args, dispatcher=self.dispatcher, memory=self.memory, words=" / ".join(turn.words),
                         nonce=state.nonce, channel=turn.channel, menu=self.menu)
        if got.ok and got.tool == "look_at_desktop" and isinstance(got.payload, Mapping):
            self._learn_payload(got.payload)
        return OneResult(name, READ if got.ok else FAILED, verified=None, result=got.text, decision=decision,
                         payload=got.payload, verb=got.tool)

    def _write(self, name: str, args: dict[str, Any], turn: policy.Turn, state: RunState, say: Any) -> OneResult:
        decision = policy.gate(name, args, {}, turn)
        if not decision.allowed:
            return self._refused(name, decision, say, state, turn)
        if name == "keep_usual":
            ok, line, facts = self.verbs.run("keep_usual", args)
            said, heard = self._speak(line, say, state) if ok and line else ("", False)
            return OneResult(name, WRITTEN if ok else FAILED, verified=True if ok else None, result=line,
                             said=said, heard=heard, decision=decision, verb="keep_usual")
        try:
            from yuki.brain.tools import run_remember

            run = run_remember(self.memory, args, words=" / ".join(turn.words))
            ok, text = bool(run.ok), str(run.text or "")
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"Not saved ({type(exc).__name__})."
        return OneResult(name, WRITTEN if ok else FAILED, verified=True if ok else None, result=text[:RESULT_CHARS],
                         decision=decision, verb="remember")

    # -- acts ----------------------------------------------------------------------------------------------------------

    def _ctx(self, turn: policy.Turn, name: str, args: dict[str, Any], *, bracket: bool) -> CallContext:
        return CallContext(self, turn, name, args, settings=self.settings, desk_read=self._desk_read,
                           bracket=bracket)

    def _act(self, name: str, args: dict[str, Any], turn: policy.Turn, spec: T.Spec, say: Any,
             state: RunState) -> OneResult:
        from yuki.voice.hands import one_call

        ctx = self._ctx(turn, name, args, bracket=spec.kind == T.ACT)
        with _Lease(turn, wanted=self.lease and spec.kind == T.ACT, wait_s=self.lease_wait_s) as lease:
            if not lease.got:
                return OneResult(name, BUSY, verified=None, result=LINE_BUSY, evidence={"why": LINE_BUSY},
                                 verb=spec.verb)
            with one_call(ctx):
                ok, report, facts = self.verbs.run(name, args)
        return self._finish(name, args, turn, ctx, ok, report, dict(facts or {}), say, state)

    def _finish(self, name: str, args: dict[str, Any], turn: policy.Turn, ctx: CallContext, ok: bool, report: str,
                facts: dict[str, Any], say: Any, state: RunState, *, done_line: str | None = None,
                scope: Mapping[str, Any] | None = None, lasting: bool = False) -> OneResult:
        """An act's result: its status from what it read back (and the effect read when it couldn't), code's line,
        the diff and the turn's tidy facts, the budget's miss, the action log."""
        decision = ctx.decision()
        verb = str(getattr(decision, "verb", "") or "")
        budget = getattr(turn, "budget", None)
        if decision is not None and not getattr(decision, "allowed", False):
            if decision is not ctx.first and budget is not None and decision.kind == policy.REFUSE \
                    and not getattr(decision, "settle", False):
                # a later target of the call refused (an inner decision): the call's refusal all the same (the gate
                # counts only the agent's own first decision); the second refusal of the turn settles it
                spend = budget.refused(decision.line if decision.final else "")
                if spend.settle:
                    decision = replace(decision, settle=True, line=spend.line)
            return self._refused(name, decision, say, state, turn)
        resolved = dict(ctx.resolved or args)
        then = T.then_of(args)
        verified = facts.get("verified")
        # a skill's own question (its resolver's: who to, which one): theirs to answer, never a miss of the step
        asked = isinstance(facts.get("_asked"), str)
        status = ASKED if asked else self._status(ok, verified, facts)
        probe_note: dict[str, Any] = {}
        if status == NOT_CONFIRMED and ok and verified is None and not facts.get("refused"):
            status, verified, probe_note = self._effect(name, resolved, facts, ctx)
        if decision is None and status not in (DONE, ALREADY):
            # the verb refused before any gate (its own shape, a window not shown, a stale read): nothing was done
            status = REFUSED if facts.get("refused") or not facts.get("sent_at") else status
        said, heard = "", False
        line = ""
        late = budget is not None and budget.late()   # closed by the hard stop while it ran: nothing more is said
        if status == DONE and not then and not late:
            given = args.get("say_when_done") if name != "run_skill" else None
            line = (done_line if done_line is not None else
                    (_words(given) if given is not None else self.verbs.code_line(name, resolved, facts)))
            if not line and done_line is None:
                # a window or tab step the hands said their own step line for (what it left showing: "Arc is showing
                # ..."), as they do under ``all``: never a silent focus
                line = _words(facts.get("step_line"))
            said, heard = self._speak(line, say, state) if line else ("", False)
        elif status == ALREADY and not then and not late:
            # already so: only the hand's own fixed line for it (else the agent words it: it does not settle unheard)
            line = _words(facts.get("say"))
            said, heard = self._speak(line, say, state) if line else ("", False)
        # a read's text was capped before its fence (a shell's output): never cut again, so the fence stays whole
        text = str(report or "") if facts.get("read") else str(report or "")[:RESULT_CHARS]
        result = OneResult(name, status, verified=verified, result=text, said=said, heard=heard, then=then,
                           decision=decision, verb=verb, tidy=bool(getattr(decision, "tidy", False)))
        if asked:
            result.question = str(facts.get("_asked") or "")
        elif status not in CLEAN and status not in (NO_CHANGE, STARTING, READ):
            result.evidence = self._evidence(name, resolved, ok, status, report, facts, probe_note)
            result.owed = str(facts.get("say") or "") or OWED_LINES.get(
                "blocked" if facts.get("refused") == "blocked" else status, "")
            if facts.get("refused") == "game" and status == REFUSED:
                # a game in front held it (the verb's backstop, or the skill's own hold): said as it is, by code
                result.final, result.line = True, _words(facts.get("say")) or policy.LINE_GAME
                result.said, result.heard = self._speak(result.line, say, state)
            if facts.get("held") and facts.get("question"):
                result.status, result.question = ASKED, str(facts.get("question") or "")
        acted = facts.get("sent_at") is not None or status in (DONE, NOT_CONFIRMED, FAILED)
        if ctx.before is not None:
            self._diff(name, resolved, facts, ctx, result, turn, state, scope=scope)
        if acted:
            state.acted = True
            if budget is not None and decision is not None and result.status != ASKED:
                # an ask (the skill's own question, a hold with a question) is theirs to answer, not a miss: no retry
                # window opened, the step not barred, a lasting act not stopped for it
                budget.after_call(name, ctx.first_args or args, acts=True,
                                  ok=status in (DONE, ALREADY, NO_CHANGE, STARTING), lasting=lasting)
            try:
                self.verbs.record(name, resolved, report, ok=bool(ok), verified=verified, facts=facts)
            except Exception:  # noqa: BLE001 - a record never breaks an act
                pass
        return result

    @staticmethod
    def _status(ok: bool, verified: Any, facts: Mapping[str, Any]) -> str:
        if facts.get("held"):
            return ASKED if facts.get("question") else REFUSED
        if facts.get("act_off"):
            return REFUSED
        if ok and facts.get("read"):
            return READ   # a command's effect is what it printed: the agent reads it
        if verified is True and ok:
            return DONE
        if ok and facts.get("expected"):
            return DONE   # done, and its effect can't be read back by the nature of the target (a page's scroll)
        if ok and facts.get("starting"):
            return STARTING
        if ok and facts.get("noop"):
            return NO_CHANGE
        if facts.get("refused") or facts.get("halted"):
            return REFUSED
        if ok and facts.get("settled"):
            return ALREADY
        if isinstance(facts.get("choose"), Mapping):
            return NOT_DONE
        if not ok:
            return FAILED if facts.get("sent_at") is not None else REFUSED
        return FAILED if verified is False else NOT_CONFIRMED

    def _effect(self, name: str, resolved: dict[str, Any], facts: dict[str, Any], ctx: CallContext
                ) -> tuple[str, bool | None, dict[str, Any]]:
        """An act whose own report could not verify it: its effect read (``reads.effect_probe``), bounded."""
        mapped = T.as_verb(name, resolved)
        if mapped is None:
            return NOT_CONFIRMED, None, {}
        verb, vinput = mapped
        role, cname = _element_parts(facts.get("target"))
        probe_facts = {"hwnd": facts.get("live_hwnd") or resolved.get("window"), "app": facts.get("app") or "",
                       "role": role, "name": cname or _words(facts.get("shown"))}
        got = reads.effect_probe(verb, vinput, probe_facts)
        if got is None:
            return NOT_CONFIRMED, None, {}
        probe, pargs = got
        verdict, evidence = reads.read_effect(probe, pargs, dispatcher=self.dispatcher, memory=self.memory,
                                              since=ctx.started_wall, last_verdict=facts.get("verified"),
                                              hwnd=probe_facts["hwnd"], budget_s=self.effect_budget_s)
        note = {"probe": probe, "args": pargs, "verdict": verdict, "evidence": evidence}
        if verdict is True:
            return DONE, True, note
        return (FAILED if verdict is False else NOT_CONFIRMED), verdict, note

    def _evidence(self, name: str, resolved: dict[str, Any], ok: bool, status: str, report: str,
                  facts: Mapping[str, Any], probe: Mapping[str, Any]) -> dict[str, str]:
        did = ""
        if facts.get("sent_at") is not None and ok:
            try:
                from yuki.voice.hands import hand_words

                did = hand_words(name, resolved, facts.get("app"))
            except Exception:  # noqa: BLE001
                did = ""
        first = _words(str(report or "").split(";")[0].split(". ")[0])
        skill = facts.get("evidence") if isinstance(facts.get("evidence"), Mapping) else None
        return reads.ending_facts(did=did, stuck=STATUS_WORDS.get(status, status) if status != DONE else "",
                                  why=first, probe=str(probe.get("probe") or ""), probe_args=probe.get("args"),
                                  verdict=probe.get("verdict"), evidence=probe.get("evidence"), skill=skill)

    def _diff(self, name: str, resolved: dict[str, Any], facts: dict[str, Any], ctx: CallContext,
              result: OneResult, turn: policy.Turn, state: RunState, *, scope: Mapping[str, Any] | None) -> None:
        """The act's diff: the desk read again, compared with its before (whose change is whose: ``Hands._scope``),
        the After text and its flags; the act's own windows and the attention windows before it to the turn (a
        tidy-up's only targets); new windows actable by id."""
        try:
            after = self._desk_read()
            if after is None:
                return
            from yuki.perception import diff as desk_diff

            try:
                self.verbs.learn(after)
            except Exception:  # noqa: BLE001
                pass
            scoped = dict(facts)
            if scope is not None:
                scoped["_desk_scope"] = dict(scope)
            acted, touched = self.verbs.scope(name, resolved, scoped, ctx.before, after)
            d = desk_diff.compare(ctx.before, after, acted=acted, touched=touched)
            own = {int(h) for h in self.verbs.act_windows(after, d, acted, touched) if h}
            state.own |= own
            result.flags = {k: bool(v) for k, v in dict(d.flags).items()}
            result.flagged = bool(d.any_flag)
            if not d.empty:
                result.after = desk_diff.render(d, max_chars=AFTER_CHARS)
            if result.flagged and not result.tidy:
                # a new flagged diff opens a tidy-up on its windows; a tidy step's own flagged diff never renews it
                # (its two steps per flagged diff stay counted)
                attention = [h for h in [*list(d.attention or []), d.focus_from] if h]
                turn.flag(own=sorted(own), attention=attention)
            self._emit("one_diff", tool=name, **d.facts())
        except Exception as exc:  # noqa: BLE001 - no diff: the result stands
            self._emit("one_diff_error", tool=name, error=type(exc).__name__)

    def _desk_read(self) -> Any:
        try:
            return self.verbs.desk_read()
        except Exception:  # noqa: BLE001
            return None

    def _facts_of(self, hwnd: Any) -> dict[str, Any]:
        if hwnd is None:
            return {}
        try:
            return dict(self.verbs.facts_of(hwnd) or {})
        except Exception:  # noqa: BLE001
            return {}

    def _learn_payload(self, payload: Mapping[str, Any]) -> None:
        """A desk look's windows made actable by id."""
        try:
            windows = [SimpleNamespace(hwnd=int(w["hwnd"]), app=str(w.get("process_name") or w.get("app") or ""),
                                       title=str(w.get("title") or ""))
                       for w in payload.get("windows") or [] if isinstance(w, Mapping) and w.get("hwnd")]
            self.verbs.learn(SimpleNamespace(windows=windows))
        except Exception:  # noqa: BLE001
            pass

    # -- run_skill -----------------------------------------------------------------------------------------------------

    def _skill(self, args: dict[str, Any], turn: policy.Turn, say: Any, state: RunState) -> OneResult:
        """``run_skill``: decided by the gate before it starts (its target is its skill: the countdown, a draft's
        rewrite, a join's grant), then run holding the desk lease: a send, a draft or a join through the request's
        desk hands with the gate's grant (``uia_route.DeskHands``, the brain's path: DM only, exactly the text), any
        other skill through the hands' own skill verb. Its done line is the skill's own, said when its check saw it."""
        from yuki.voice.hands import one_call

        key, slots = rule.skill_input(rule.Call("run_skill", dict(args)))
        sk = util.skill(key)
        app = str(getattr(sk, "app", "") or "")
        ctx = self._ctx(turn, "run_skill", args, bracket=False)
        ctx.skill_app = app
        decision = ctx.check("run_skill", args, {"app": app})
        if not decision.allowed:
            return self._refused("run_skill", decision, say, state, turn)
        run_args = {k: v for k, v in dict(decision.rewrite or args).items() if k != "consent"}
        drafted = decision.rewrite is not None
        counted = False
        if decision.countdown:
            if self.countdown is None:
                return OneResult("run_skill", REFUSED, result=LINE_NO_COUNTDOWN, line=LINE_NO_COUNTDOWN,
                                 decision=decision, verb="run_skill")
            got = self._countdown(turn, args, sk, slots)
            if got.state != "sent":
                return self._not_sent(got, decision, say, state)
            counted = bool(got.counted)
        run_slots = dict(run_args.get("slots") or {}) if isinstance(run_args.get("slots"), Mapping) else {}
        # a capability that can send or join runs on the desk hands whatever its slots (a draft, an "open my chat with
        # Hemanth"): its own steps (the quick switcher's keys, the chat's name, the row's pick) are its data's there;
        # on the hands' own path every step is the gate's, and the run's own typing would hold its own Enter
        can_send = sk is not None and (bool(getattr(sk, "sends", False)) or rule.skill_joins(sk)
                                       or rule.skill_sends(sk, run_slots, partial=True))
        desk_path = bool(decision.countdown or drafted or decision.join or can_send) \
            or not getattr(self.verbs, "skills_on", False)
        scope = None
        with _Lease(turn, wanted=self.lease, wait_s=self.lease_wait_s) as lease:
            if not lease.got:
                return OneResult("run_skill", BUSY, result=LINE_BUSY, evidence={"why": LINE_BUSY}, decision=decision,
                                 verb="run_skill")
            with one_call(ctx):
                ctx.before = self._desk_read()   # after the gate and the countdown, right before the run
                turn.begin_skill(decision, run_args, counted=counted)
                try:
                    if desk_path:
                        declared = args.get("consent") if isinstance(args.get("consent"), Mapping) else {}
                        ok, report, facts = self._desk_skill(run_args, turn, decision, counted, key, sk,
                                                             to=_words(declared.get("to")))
                    else:
                        ok, report, facts = self.verbs.run("run_skill", run_args)
                finally:
                    turn.end_skill()
                if drafted and decision.type_after and ok:
                    ok, report, facts = self._type_after(turn, decision, key, app, ok, report, facts)
        facts = dict(facts or {})
        prefs = facts.pop("_prefs", None)
        if decision.countdown and str(facts.get("outcome") or "") not in NOT_SENT_OUTCOMES:
            turn.mark_sent(args)   # it may have gone: never twice this request, asked first in the next turn
        if prefs is not None:
            result_obj = prefs[3] if isinstance(prefs, (list, tuple)) and len(prefs) > 3 else None
            try:
                scope = self.verbs.skill_scope(key, result_obj)
            except Exception:  # noqa: BLE001
                scope = None
        elif ctx.before is not None:
            scope = {"apps": [a for a in (app,) if a], "focus": True,
                     "open": str(getattr(sk, "kind", "") or "") == "open", "left": drafted}
        line = _words(facts.get("done_line")) if facts.get("verified") is True else ""
        if drafted and facts.get("verified") is True and not line:
            line = rule.LINE_DRAFTED   # a draft is never "sent": consent's own words
        if str(facts.get("outcome") or "") == "asked":
            # the skill's own question (its resolver's: which one, who to): theirs to answer, the agent asks it; not a
            # failed step (``_finish`` books no miss for it)
            facts["_asked"] = _words(facts.get("ask")) or _words(str(report or "").split("\n", 1)[0])
        result = self._finish("run_skill", args, turn, ctx, ok, report, facts, say, state, done_line=line,
                              scope=scope, lasting=bool(decision.countdown or decision.join))
        if counted:
            result.countdown = "sent"
        if prefs is not None:
            self._after_skill(prefs)
        return result

    def _countdown(self, turn: policy.Turn, args: Mapping[str, Any], sk: Any, slots: Mapping[str, Any]) -> Any:
        """The send's countdown on this thread (``one.countdown.Countdown.run``), its time not counted on the budget,
        the game and the Act switch read again when it runs out (``policy.recheck_send``)."""
        declared = args.get("consent") if isinstance(args.get("consent"), Mapping) else {}
        pname = rule.person_slot(sk) if sk is not None else ""
        tname = rule.text_slot(sk) if sk is not None else ""
        app_slot = next((sl.name for sl in getattr(sk, "slots", ()) or () if getattr(sl, "type", "") == "app"), "")
        app = (_words(slots.get(app_slot)) if app_slot else "") or str(getattr(sk, "app", "") or "")
        where = util.app_name(app) if app else ""
        who = _words(declared.get("to")) or (_words(slots.get(pname)) if pname else "")
        text = _words(declared.get("text")) or (_words(slots.get(tname)) if tname else "")
        budget = getattr(turn, "budget", None)
        with (budget.excluded() if budget is not None else nullcontext()):
            return self.countdown.run(who=who, where=where, app=where, text=text, typed=turn.channel == "typed",
                                      recheck=lambda: policy.recheck_send(turn, args))

    def _not_sent(self, got: Any, decision: Any, say: Any, state: RunState) -> OneResult:
        """A send whose countdown did not run out to a send: stopped (their Stop: said by code, settles), changed
        (they spoke, or a chip: the agent reads their words next), or held after it (a game, Act off: said by code)."""
        why = str(getattr(got, "why", "") or "")
        if got.state == "cancelled" and why == "busy":
            return OneResult("run_skill", REFUSED, result=LINE_COUNTDOWN_BUSY, line=LINE_COUNTDOWN_BUSY,
                             countdown="busy", decision=decision, verb="run_skill",
                             evidence={"why": "another send was counting down; nothing was sent"})
        if got.state == "cancelled":
            said, heard = self._speak(LINE_STOPPED_SEND, say, state)
            return OneResult("run_skill", STOPPED, verified=True, result=LINE_STOPPED_SEND, said=said, heard=heard,
                             line=LINE_STOPPED_SEND, countdown="cancelled", decision=decision, verb="run_skill")
        if getattr(got, "line", ""):
            said, heard = self._speak(got.line, say, state)
            return OneResult("run_skill", REFUSED, result="", line=got.line, final=True, said=said, heard=heard,
                             countdown="changed", decision=decision, verb="run_skill",
                             evidence={"why": "held when the countdown ran out", "stuck": why})
        swap = dict(got.swap) if getattr(got, "swap", None) else None
        note = ("they tapped another choice during the countdown; nothing was sent" if swap else
                "they spoke during the countdown; nothing was sent: their words decide")
        return OneResult("run_skill", CHANGED, result=note, countdown="changed", swap=swap, decision=decision,
                         verb="run_skill", evidence={"why": note})

    def _grant_person(self, run_args: Mapping[str, Any], decision: Any, sk: Any, to: str) -> int | None:
        """The one person a send's grant goes to (``uia_route.grant_person`` through memory), else ``None``."""
        if sk is None or not decision.countdown:
            return None
        pname = rule.person_slot(sk)
        slots = run_args.get("slots") if isinstance(run_args.get("slots"), Mapping) else {}
        person = _words(slots.get(pname)) if pname else ""
        memory = self.memory
        call = getattr(memory, "call", None) if memory is not None else None
        if not person or not callable(call):
            return None

        def go(client: Any) -> int | None:
            from yuki.skills.uia_route import grant_person

            return grant_person(getattr(client, "store", client), person, to)

        try:
            got = call("one_grant_person", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return None
        return got if isinstance(got, int) and not isinstance(got, bool) else None

    def _desk_skill(self, run_args: dict[str, Any], turn: policy.Turn, decision: Any, counted: bool, key: str,
                    sk: Any, *, to: str = "") -> tuple[bool, str, dict[str, Any]]:
        """A send, a draft or a join: the Dispatcher's ``run_skill`` with the request's desk hands holding the gate's
        grant for this run only (``Executor._dispatch_act``'s shape). The skill's own typed draft joins the desk's
        unsent text afterwards (``policy.adopt_typed``)."""
        from yuki.skills.uia_route import DeskHands

        dispatcher = self.dispatcher
        if dispatcher is None:
            return False, "no dispatcher to run the skill through; nothing was done", {"refused": True}
        desk = DeskHands(dispatcher, memory=self.memory, words=list(turn.words), earlier=list(turn.earlier),
                         checker=turn.checker(), log=self.log)
        try:
            from yuki.voice.hands import _AppVolumeBackend

            if isinstance(desk.backend, _AppVolumeBackend):
                # the voice hands' backend is the real desk's with the mixer added: the desk hands read the real one
                # (its Now board: the popups a skill's search sees; their own steps' dispatcher over it)
                desk.backend = desk.backend._inner
        except Exception:  # noqa: BLE001 - the wrapped backend still acts; only the popups go unseen
            pass
        grant = policy.skill_grant(turn, run_args, decision, counted=counted,
                                   person_id=self._grant_person(run_args, decision, sk, to))
        desk.grant = dict(grant)
        previous = getattr(dispatcher, "skill_context", None)
        context: dict[str, Any] = {"memory": self.memory, "words": list(turn.words), "earlier": list(turn.earlier),
                                   "request_id": turn.request_id, "log": self.log, "settings": self.settings,
                                   "hands": desk}
        if grant.get("send"):
            context["grant"] = dict(grant)
        started = time.monotonic()
        try:
            dispatcher.skill_context = context
            outcome = dispatcher.dispatch("run_skill", {k: run_args[k] for k in ("skill", "slots", "again",
                                                                                   "corrects", "place")
                                                        if k in run_args})
        finally:
            desk.grant = {}
            try:
                dispatcher.skill_context = previous if previous is not None else {}
            except Exception:  # noqa: BLE001
                pass
            policy.adopt_typed(getattr(desk, "_typed", None))
        payload = outcome.payload if isinstance(getattr(outcome, "payload", None), Mapping) else {}
        details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
        verified = details.get("verified")
        refused = details.get("refused")
        facts: dict[str, Any] = {"verified": verified if isinstance(verified, bool) else None,
                                 "outcome": str(payload.get("outcome") or ""),
                                 "done_line": str(payload.get("done_line") or ""), "skill": key,
                                 "sent_at": started if payload.get("outcome") not in ("refused", None) else None}
        if isinstance(payload.get("evidence"), Mapping):
            facts["evidence"] = dict(payload["evidence"])
        if refused:
            facts["refused"] = "game" if refused == "game" else refused
            facts["settled"] = True
        text = "\n".join(str(b.get("text") or "") for b in getattr(outcome, "content", None) or []
                         if isinstance(b, Mapping) and b.get("type") == "text") or str(outcome.summary or "")
        return bool(getattr(outcome, "ok", False)), text, facts

    def _type_after(self, turn: policy.Turn, decision: Any, key: str, app: str, ok: bool, report: str,
                    facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """A draft's own wording (``Decision.type_after``) typed with no Enter once the chat is open and in front:
        through the gate (an inner step; it marks the text as Yuki's unsent text), never around it."""
        if facts.get("verified") is not True:
            return False, LINE_CHAT_UNCONFIRMED, {**facts, "verified": False, "type_after": "not_open"}
        front = None
        try:
            front = self.verbs.foreground()
        except Exception:  # noqa: BLE001
            front = None
        target = self._facts_of(front) if front else {}
        from yuki.voice.hands import _app_id

        same = bool(front and app) and (_app_id(target.get("app")) == _app_id(app) or _app_id(
            util.app_name(target.get("app") or "")) == _app_id(util.app_name(app)))
        if not same:
            return False, LINE_CHAT_UNCONFIRMED, {**facts, "verified": False, "type_after": "not_in_front"}
        text = str(decision.type_after or "")
        gated = policy.gate("type_text", {"hwnd": int(front), "text": text}, target, turn, inner=True)
        if not gated.allowed:
            return False, gated.line or "I opened the chat but didn't type the text in.", \
                {**facts, "verified": False, "type_after": "refused"}
        outcome = self.dispatcher.dispatch("type_text", {"text": text, "press_enter": False,
                                                         "expect_hwnd": int(front)})
        verified, _why = util.action_verdict("type_text", outcome)
        return bool(outcome.ok), f"{report}; typed Yuki's wording in, not sent", \
            {**facts, "verified": verified is not False and bool(outcome.ok), "type_after": "typed"}

    def _after_skill(self, prefs: Any) -> None:
        """The preference side of a voice skill run (``Hands._learn_from_run``: a correction, a "still your usual?"
        card), off the result's path on a thread of its own. Never raises."""
        hands = getattr(self.verbs, "hands", None)
        learn = getattr(hands, "_learn_from_run", None)
        if not callable(learn):
            return

        def go() -> None:
            try:
                learn(*prefs)
            except Exception:  # noqa: BLE001 - the preference side never breaks a run
                pass

        threading.Thread(target=go, name="yuki-one-prefs", daemon=True).start()

    # -- plumbing ------------------------------------------------------------------------------------------------------

    def _emit(self, event: str, **fields: Any) -> None:
        if self.log is None:
            return
        try:
            self.log(event, **{k: v for k, v in fields.items() if v is not None})
        except Exception:  # noqa: BLE001 - a log never breaks a call
            pass


def speaker(loop: asyncio.AbstractEventLoop, say: Callable[[str], Any], *, wait_s: float = 1.0
            ) -> Callable[[str], None]:
    """A ``speak`` for :class:`Runner` on voice: ``say(text)`` (a coroutine function, e.g. pushing a
    ``TTSSpeakFrame``) run on the voice loop from the worker thread, waited for at most ``wait_s`` so the line is
    queued before the act's after-read. Never raises."""

    def speak(text: str) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(say(text), loop)
            future.result(timeout=wait_s)
        except Exception:  # noqa: BLE001 - not queued in time: it still goes
            pass

    return speak


__all__ = ["ALREADY", "ASKED", "BUSY", "CHANGED", "CLEAN", "CallContext", "DONE", "DRIVER", "DRIVER_TOOLS", "FAILED",
           "NOT_CONFIRMED", "NOT_DONE", "NO_CHANGE", "OWED_LINES", "OneResult", "READ", "REFUSED", "Reply", "ReplyEnd",
           "RunState", "Runner", "STARTING", "STATUS_WORDS", "STOPPED", "WRITTEN", "current", "settles", "skill_step",
           "speaker", "state_of"]
