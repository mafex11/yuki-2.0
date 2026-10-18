"""The lane host of the ONE agent on typed requests (phase 4, oTyped; ``brain = "one"``).

``brain = "all"`` keeps its own host untouched (the old agent objects it depends on: ``loop.Agent`` on each lane, the
plan brain made from it). Under ``one`` each lane of :mod:`yuki.ui.runtime` gets a NEW object from here, made on its
first typed request: a :class:`yuki.one.loop.OneAgent` built for that lane. What a lane needs, and nothing of the old
thinker:

* **The front desk / worker split, as today.** The worker acts (the real desktop, one desk lease per act through the
  runner, the send countdown, the takeover pause). The front desk only looks: its Dispatcher is on the hands-blocked
  backend, and the first call that needs hands (:func:`needs_worker`: an act, or a worker-only tool) ends its run with
  ``LINE_NEEDS_HANDS`` and ``needed_hands``, so the runtime queues the request for the worker (the old hand-off,
  unchanged).
* **``remember`` and ``ask_user`` are worker-only** (:data:`WORKER_ONLY`, addendum decision 4): the front desk is
  never offered them, so a request the worker takes over never saves or asks twice.
* **Session logs per lane**: the agent writes to the lane's own session log (``<session>-worker`` /
  ``<session>-frontdesk``, the ones the lane's ``loop.Agent`` already writes), content-free.
* **The model chain**: Sonnet 5.5 with its failover chain cut after Sonnet 5 (:func:`one_client`; addendum must-fix 9:
  no Haiku drives an act). The commit check and the answer read keep their own Haiku chains (the same client: only the
  ONE model's chain is replaced, on this client alone).
* **The runner** (:func:`make_runner`): ``yuki.one.runner`` (lane oRun), imported guardedly. Until it is merged (or if
  it can't be made) a reads-only stand-in (:class:`ReadsRunner`) runs ``look`` / ``memory`` / ``fetch`` /
  ``remember`` through ``yuki.brain.tools`` behind the same gate, and refuses every act plainly.
* **A job that can't be built fails with a plain line** (:class:`Unbuilt`): never a fallback to another brain, and
  never a host without ``run()``.

Nothing here imports Qt or the runtime (the runtime passes its blocked backend in), and nothing runs at import.
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

from yuki.one import tools as T

#: Tools only the worker lane is offered (addendum decision 4): the front desk's request may move to the worker,
#: which would then save or ask again.
WORKER_ONLY: frozenset[str] = frozenset({"remember", "ask_user"})
#: Typed-only tools that a spoken request on a lane is never offered (addendum decision 5: shell, fetch and keys stay
#: off voice; the gate refuses them on the voice channel too).
TYPED_ONLY_ACTS: frozenset[str] = frozenset({"keys", "shell", "fetch"})
#: The line a job ends with when the ONE agent could not be made for its lane (a plain line, no fallback brain).
LINE_UNBUILT = "I couldn't start on that just now, so nothing was done. Please try again in a minute."
#: The runner stand-in's refusal of an act (until ``yuki.one.runner`` is merged).
LINE_NO_RUNNER = ("Not done: this build of Yuki can't act on the screen yet (its runner is missing), so nothing was "
                  "done. Say so in one line.")
#: The stand-in's answer to a read it does not run (``quick_memory``: the memory tool reads the same).
LINE_NOT_IN_BUILD = "Not run: {tool} is not in this build; use memory instead."


# ---------------------------------------------------------------------------------------------------------------------
# What each lane is offered
# ---------------------------------------------------------------------------------------------------------------------


def lane_tools(skill_keys: Sequence[str] = (), *, worker: bool = True, spoken: bool = False,
               volume_skill: bool | None = None) -> list[dict[str, Any]]:
    """The typed tool list (Anthropic shape) for one lane and request: ``one.tools.schemas("typed", keys)`` less
    :data:`WORKER_ONLY` on the front desk (``worker`` false) and less :data:`TYPED_ONLY_ACTS` for a spoken request
    (``spoken``). The order is the schema list's (it heads the cached prefix)."""
    drop = set()
    if not worker:
        drop |= WORKER_ONLY
    if spoken:
        drop |= TYPED_ONLY_ACTS
    tools = [t for t in T.schemas(T.TYPED, list(skill_keys or ()), volume_skill=volume_skill) if t["name"] not in drop]
    return T.anthropic(tools)


def needs_worker(name: str, args: Mapping[str, Any] | None = None) -> bool:
    """Whether a call needs the worker's lane (the front desk hands the request over at it): an acting tool, a
    worker-only tool, ``keep_usual``, or a ``quick_memory`` write (``add_todos``). Tool facts only; an unknown name is
    not (the gate refuses it plainly)."""
    spec = T.spec_of(str(name or ""))
    if spec is None:
        return False
    if spec.acts or name in WORKER_ONLY or name == "keep_usual":
        return True
    return name == "quick_memory" and str(dict(args or {}).get("action") or "") == "add_todos"


# ---------------------------------------------------------------------------------------------------------------------
# The model client
# ---------------------------------------------------------------------------------------------------------------------


def one_client(settings: Any, *, log: Callable[..., Any] | None = None, factory: Callable[[str], Any] | None = None
               ) -> Any:
    """The ONE agent's failover client: ``FailoverClient.from_settings`` (retries, failover, Settings -> AI routing as
    the brain's), with the ONE model's chain replaced by :func:`yuki.brain.settings.one_chain` of it (Sonnet 5.5 from
    us-east-1, then Sonnet 5; no Haiku). The chains of every other model (the commit check's, the answer read's) are
    kept; the settings' own dict is never changed (a new dict on this client only)."""
    from yuki.agent.failover import FailoverClient
    from yuki.brain.settings import OneSettings, one_chain

    client = FailoverClient.from_settings(settings, log=log, **({"factory": factory} if factory is not None else {}))
    model = OneSettings.read(settings).model
    chains = dict(getattr(client, "fallbacks", None) or {})
    chains[model] = one_chain(chains.get(model))
    client.fallbacks = chains
    return client


# ---------------------------------------------------------------------------------------------------------------------
# The runner (lane oRun), guarded
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Result:
    """A runner result in the shape the typed driver reads (``yuki.one.runner.OneResult``'s fields, design 6: status,
    said, evidence, after, flags, verified; plus ``text``, the result for the model, ``ok``, ``question`` for a gate's
    ask, ``settle`` / ``silent`` / ``line`` for a code settle, ``payload`` for the UI's step words)."""

    status: str
    said: str = ""
    evidence: str = ""
    after: str = ""
    flags: tuple[str, ...] = ()
    verified: bool = False
    text: str = ""
    ok: bool = True
    question: str = ""
    settle: bool = False
    silent: bool = False
    line: str = ""
    acted: bool = False
    payload: Any = None


class ReadsRunner:
    """The runner stand-in until ``yuki.one.runner`` is merged (or when it can't be made): an act is refused at once
    with :data:`LINE_NO_RUNNER` (nothing done, no commit check spent); every other call passes the gate
    (``policy.gate``: the budget, unknown tools, the channel, M9), then ``look``, ``memory``, ``fetch`` and
    ``remember`` run through ``yuki.brain.tools`` (fenced reads, the user's own words for memory's quote checks).
    Content-free log line per call."""

    kind = "reads_only"

    def __init__(self, *, dispatcher: Any, memory: Any, log: Callable[..., Any] | None = None,
                 menu: Callable[[], str] | None = None) -> None:
        self.dispatcher = dispatcher
        self.memory = memory
        self._log = log
        self._menu = menu

    def run(self, name: str, args: Mapping[str, Any], turn: Any, speak: Callable[[str], None] | None = None
            ) -> Result:
        from yuki.one import policy

        started = time.perf_counter()
        spec = T.spec_of(name)
        if spec is not None and spec.acts:
            # nothing can act without the runner: refused before the gate, so no commit check is spent on it
            return self._done(Result("refused", ok=False, text=LINE_NO_RUNNER), name, started)
        decision = policy.gate(name, args, {}, turn)
        if decision.kind == policy.ASK:
            return self._done(Result("ask", question=decision.question, ok=False, text=decision.question), name,
                              started)
        if not decision.allowed:
            if decision.final and decision.line and speak is not None:
                speak(decision.line)
            return self._done(Result("refused", ok=False, text=decision.line or "Not done.", settle=decision.settle,
                                     silent=decision.silent, line=decision.line), name, started)
        run = self._read(name, dict(args or {}), turn)
        if run is None:
            return self._done(Result("refused", ok=False, text=LINE_NOT_IN_BUILD.format(tool=name)), name, started)
        if getattr(run, "private", False):
            turn.note_read("memory" if name == "memory" else "file")
        return self._done(Result("ok" if run.ok else "failed", ok=bool(run.ok), text=str(run.text or ""),
                                 payload=getattr(run, "payload", None)), name, started)

    def _read(self, name: str, args: dict[str, Any], turn: Any) -> Any:
        from yuki.agent.untrusted import new_nonce
        from yuki.brain import tools as BT

        words = "\n".join(getattr(turn, "words", []) or [])
        nonce = new_nonce()
        if name == "look":
            return BT.run_look(self.dispatcher, args, nonce=nonce)
        if name == "memory":
            menu = self._menu() if callable(self._menu) else ""
            return BT.run_memory(self.memory, args, words=words, nonce=nonce, menu=menu)
        if name == "fetch":
            return BT.run_fetch(self.dispatcher, args, nonce=nonce)
        if name == "remember":
            return BT.run_remember(self.memory, args, words=words)
        return None

    def _done(self, result: Result, name: str, started: float) -> Result:
        if self._log is not None:
            try:
                self._log("one_run", tool=name, status=result.status, runner=self.kind,
                          ms=round((time.perf_counter() - started) * 1000, 1), chars=len(result.text))
            except Exception:  # noqa: BLE001 - a log never breaks a call
                pass
        return result


def _accepted(fn: Callable[..., Any], offer: Mapping[str, Any]) -> dict[str, Any]:
    """The part of ``offer`` that ``fn`` (a function or a class) takes by keyword: everything when it takes
    ``**kwargs``. Raises ``TypeError`` when a required parameter is not offered."""
    sig = inspect.signature(fn)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(offer)
    out = {k: v for k, v in offer.items() if k in params}
    for p in params.values():
        if p.name in ("self", "cls") or p.kind in (inspect.Parameter.VAR_POSITIONAL,):
            continue
        if p.default is inspect.Parameter.empty and p.name not in out:
            raise TypeError(f"the runner needs {p.name!r}, which the typed lane does not offer")
    return out


def make_runner(agent: Any) -> Any:
    """The runner for one ONE agent (one per lane, long-lived; the turn and ``speak`` come with each call). Built from
    ``yuki.one.runner`` (lane oRun) when it is importable: its ``make_runner``, else its ``Runner``, given by keyword
    what it takes of the lane's offer (``dispatcher``, ``settings``, ``memory``, ``log``, ``logger``, ``lane``,
    ``channel`` = ``typed``, ``countdown``: the lane's one countdown host, ``client``: the agent's failover client
    (the ONE chain; the commit check runs on its default Claude path), ``hands``: whether the lane acts, ``agent``: the
    :class:`yuki.one.loop.OneAgent` itself). Otherwise, or when it can't be made, :class:`ReadsRunner`. Logged once
    (content-free)."""
    log = getattr(getattr(agent, "logger", None), "log", None)

    def note(**fields: Any) -> None:
        if callable(log):
            try:
                log("one_runner", lane=getattr(agent, "lane", ""), **fields)
            except Exception:  # noqa: BLE001
                pass

    offer = {"dispatcher": agent.dispatcher, "settings": agent.settings, "memory": agent.memory, "log": log,
             "logger": getattr(agent, "logger", None), "lane": getattr(agent, "lane", ""), "channel": T.TYPED,
             "countdown": getattr(agent, "one_countdown", None), "client": agent.client,
             "hands": bool(getattr(agent, "hands", True)), "agent": agent}
    try:
        from yuki.one import runner as mod   # lane oRun; guarded until it is merged
    except Exception as exc:  # noqa: BLE001 - not merged yet, or broken: reads only
        note(state="reads_only", why=type(exc).__name__)
        return ReadsRunner(dispatcher=agent.dispatcher, memory=agent.memory, log=log,
                           menu=lambda: getattr(agent, "_menu_lines", ""))
    for factory_name in ("make_runner", "Runner"):
        factory = getattr(mod, factory_name, None)
        if not callable(factory):
            continue
        try:
            runner = factory(**_accepted(factory, offer))
        except Exception as exc:  # noqa: BLE001 - can't be made: reads only, said in the log
            note(state="reads_only", why=type(exc).__name__, via=factory_name)
            break
        note(state="ready", via=factory_name, kind=type(runner).__name__)
        return runner
    return ReadsRunner(dispatcher=agent.dispatcher, memory=agent.memory, log=log,
                       menu=lambda: getattr(agent, "_menu_lines", ""))


# ---------------------------------------------------------------------------------------------------------------------
# Building a lane's agent
# ---------------------------------------------------------------------------------------------------------------------


def build_agent(*, lane: str, settings: Any, logger: Any = None, memory: Any = None, dispatcher: Any = None,
                hands: bool = True, loop_agent: Any = None, client: Any = None,
                runner_factory: Callable[[Any], Any] | None = None) -> Any:
    """The ONE agent of one lane (:class:`yuki.one.loop.OneAgent`). ``dispatcher``: the front desk's Dispatcher on the
    hands-blocked backend (``None`` on the worker: the agent makes its own on the real desktop, as the plan brain
    does). ``loop_agent``: the lane's ``loop.Agent`` (``all``'s host, untouched): its session log is the lane's, and
    the worker takes its send countdown, takeover watch and ``takeover_ok`` (as ``Lane._make_brain`` does). Raises on
    a build failure: the runtime then fails the job with :data:`LINE_UNBUILT` (:class:`Unbuilt`)."""
    from yuki.one.loop import OneAgent

    agent = OneAgent(settings, logger if logger is not None else getattr(loop_agent, "logger", None), client=client,
                     dispatcher=dispatcher, memory=memory, lane=lane, hands=hands, runner_factory=runner_factory)
    if hands and loop_agent is not None:
        agent.send_countdown_s = float(getattr(loop_agent, "send_countdown_s", 0.0) or 0.0)
        agent.takeover = getattr(loop_agent, "takeover", None)
        agent.takeover_ok = getattr(loop_agent, "takeover_ok", None)
    return agent


class Unbuilt:
    """What a lane drives when its ONE agent could not be made: the job ends at once with :data:`LINE_UNBUILT` (an
    :class:`yuki.log.events.ErrorEvent`, shown as a failure), never on another brain. It has the caller surface the
    lane and the runtime read (``run``, ``cancel``, ``steer``, ``answer``, ``last_summary`` ...). ``why``: a code word
    for the log."""

    hands = False
    needed_hands = False
    paused = False
    awaiting_answer = False
    last_summary: dict[str, Any] | None = None
    send_countdown_s = 0.0

    def __init__(self, why: str = "", logger: Any = None, *, line: str = LINE_UNBUILT) -> None:
        self.why = str(why or "")
        self.logger = logger
        self.line = line
        self.cancel_reason = "cancelled"
        self._cancelled = threading.Event()
        self._steers: list[str] = []
        self.on_pause: Any = None
        self.on_late: Any = None
        self.takeover: Any = None
        self.takeover_ok: Any = None

    def run(self, request: str = "", **_kw: Any) -> Iterator[Any]:
        from yuki.log.events import ErrorEvent

        log = getattr(self.logger, "log", None)
        if callable(log):
            try:
                log("one_unbuilt", why=self.why)
            except Exception:  # noqa: BLE001
                pass
        yield ErrorEvent(self.line)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        self.cancel_reason = reason
        self._cancelled.set()

    def steer(self, text: str) -> None:
        if str(text or "").strip():
            self._steers.append(str(text).strip())

    def take_steers(self) -> list[str]:
        out, self._steers = self._steers, []
        return out

    def answer(self, text: str) -> None:
        del text

    def resume(self, words: str | None = None, *, by: str = "") -> bool:
        del words, by
        return False

    def send_choice(self, kind: str, value: str) -> bool:
        del kind, value
        return False

    def set_app_named(self, app: str | None) -> None:
        del app

    def warm_cache(self, *_a: Any, **_k: Any) -> bool:
        return False

    def status_line(self) -> str | None:
        return None


__all__ = ["LINE_NOT_IN_BUILD", "LINE_NO_RUNNER", "LINE_UNBUILT", "ReadsRunner", "Result", "TYPED_ONLY_ACTS", "Unbuilt", "WORKER_ONLY",
           "build_agent", "lane_tools", "make_runner", "needs_worker", "one_client"]
