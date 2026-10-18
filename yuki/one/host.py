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
* **The runner** (:func:`make_runner`): ``yuki.one.runner.Runner`` (lane oRun) over the lane's own
  ``voice.hands.Hands`` (``Verbs``), made with the agent (:func:`build_agent`): every call of the agent goes through
  it, so through the one gate at the verbs' authorizer points.
* **A job that can't be built fails with a plain line** (:class:`Unbuilt`): a runner that can't be made included;
  never a fallback to reads only or to another brain, and never a host without ``run()``.

Nothing here imports Qt or the runtime (the runtime passes its blocked backend in), and nothing runs at import.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterator, Mapping, Sequence

from yuki.one import tools as T

#: Tools only the worker lane is offered (addendum decision 4): the front desk's request may move to the worker,
#: which would then save or ask again.
WORKER_ONLY: frozenset[str] = frozenset({"remember", "ask_user"})
#: Typed-only tools that a spoken request on a lane is never offered (addendum decision 5: shell, fetch and keys stay
#: off voice; the gate refuses them on the voice channel too).
TYPED_ONLY_ACTS: frozenset[str] = frozenset({"keys", "shell", "fetch"})
#: The line a job ends with when the ONE agent (or its runner) could not be made for its lane (a plain line, no
#: fallback brain).
LINE_UNBUILT = "I couldn't start on that just now, so nothing was done. Please try again in a minute."


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
# The runner (lane oRun)
# ---------------------------------------------------------------------------------------------------------------------


class _HandsLog:
    """The hands' log (``Hands.log.event(...)``, the voice log's shape) onto the lane's session log (content-free, as
    the hands' own events are)."""

    def __init__(self, log: Callable[..., Any] | None) -> None:
        self._log = log

    def event(self, event: str, **fields: Any) -> None:
        if callable(self._log):
            try:
                self._log(event, **fields)
            except Exception:  # noqa: BLE001 - a log never breaks a hand
                pass


#: The hooks the lane's agent set on its own Dispatcher (``Brain._setup_input_gate``) that the lane's hands share on
#: theirs: the wait for the user to stop typing before global input (typed requests have it under ``all``), the lines
#: it wants said, and the cancel check it polls (Stop reaches an act still waiting).
SHARED_HOOKS: tuple[str, ...] = ("input_gate", "notices", "cancel_check")


def make_runner(agent: Any) -> Any:
    """The runner of one ONE agent (one per lane, long-lived; the turn and ``speak`` come with each call):
    ``yuki.one.runner.Runner`` (lane oRun) over the lane's own hands, as its contract gives a typed driver:

    * ``Hands(with_app_volume(backend))`` over the lane's backend (the worker's real desktop; the front desk's
      hands-blocked one: it only looks, and hands the request over before any act), their log the lane's session log,
      their memory and the skill engine's the agent's, the skill settings the agent's, their words the request's
      (``hands.words`` / ``exchange``: the gate's ``Turn.words`` as they are now), their user turn the request's
      number;
    * the agent's Dispatcher hooks (:data:`SHARED_HOOKS`) shared with the hands' own Dispatcher;
    * ``Runner(Verbs(hands), channel="typed", settings, countdown = the lane's one countdown host, memory, log)``. Its
      reads go through the hands' own Dispatcher (no ``dispatcher=``), so a control's ``[id]`` from ``look window`` is
      the one ``click`` / ``type`` find (``Verbs.element_facts`` reads that same Dispatcher's latest read).

    Raises when it can't be made (``yuki.one.runner`` missing, a contract that changed): the lane then fails its jobs
    with a plain line (:class:`Unbuilt`), never on reads only or on another brain. Logged once (content-free)."""
    log = getattr(getattr(agent, "logger", None), "log", None)

    def note(**fields: Any) -> None:
        if callable(log):
            try:
                log("one_runner", lane=getattr(agent, "lane", ""), **fields)
            except Exception:  # noqa: BLE001
                pass

    try:
        from yuki.one.runner import Runner   # lane oRun
        from yuki.voice.hands import Hands, Verbs, with_app_volume

        dispatcher = agent.dispatcher
        backend = getattr(dispatcher, "backend", None)
        if backend is None:
            raise RuntimeError("the lane's dispatcher has no backend")
        hands = Hands(with_app_volume(backend), log=_HandsLog(log))
        hands.memory = hands.skill_memory = agent.memory
        hands.skill_settings = agent.settings
        hands.words = lambda: list(getattr(getattr(agent, "_turn", None), "words", None) or [])
        hands.exchange = hands.words
        hands.user_turn = lambda: getattr(agent, "_requests", 0)
        own = getattr(hands, "_dispatcher", None)
        for hook in SHARED_HOOKS:
            value = getattr(dispatcher, hook, None)
            if own is not None and value is not None and getattr(own, hook, None) is None:
                setattr(own, hook, value)
        runner = Runner(Verbs(hands), channel=T.TYPED, settings=agent.settings,
                        countdown=getattr(agent, "one_countdown", None), memory=agent.memory, log=log)
    except Exception as exc:
        note(state="unbuilt", why=type(exc).__name__)
        raise
    note(state="ready", kind=type(runner).__name__, hands=bool(getattr(agent, "hands", True)))
    return runner


def hands_of(runner: Any) -> Any:
    """The lane's hands behind ``runner`` (``runner.verbs.hands``), else ``None`` (a fake in checks)."""
    return getattr(getattr(runner, "verbs", None), "hands", None)


def ready_for_request(runner: Any, *, skill_keys: Sequence[str] = (), menu: str = "") -> None:
    """At a request's start: the runner's Skills menu text (``memory`` kind ``skills``) and the hands' Skills keys
    (``run_skill``'s skill path is on with a menu, as on the voice) are this request's. Never raises."""
    try:
        if hasattr(runner, "menu"):
            runner.menu = str(menu or "")
        hands = hands_of(runner)
        if hands is not None:
            hands.skill_keys = tuple(str(k) for k in skill_keys or () if str(k or "").strip())
    except Exception:  # noqa: BLE001
        pass


def fresh_desk(runner: Any) -> None:
    """Right before an act: the hands' summary of the desk (``Verbs.desk``: the last one while it is fresh and nothing
    was done since, else read now, a few ms), so the verbs act on the windows the agent was shown (they refuse a window
    they were never shown). Never raises."""
    desk = getattr(getattr(runner, "verbs", None), "desk", None)
    if callable(desk):
        try:
            desk()
        except Exception:  # noqa: BLE001 - the verbs say plainly what they can't find
            pass


# ---------------------------------------------------------------------------------------------------------------------
# Building a lane's agent
# ---------------------------------------------------------------------------------------------------------------------


def build_agent(*, lane: str, settings: Any, logger: Any = None, memory: Any = None, dispatcher: Any = None,
                hands: bool = True, loop_agent: Any = None, client: Any = None,
                runner_factory: Callable[[Any], Any] | None = None) -> Any:
    """The ONE agent of one lane (:class:`yuki.one.loop.OneAgent`). ``dispatcher``: the front desk's Dispatcher on the
    hands-blocked backend (``None`` on the worker: the agent makes its own on the real desktop, as the plan brain
    does). ``loop_agent``: the lane's ``loop.Agent`` (``all``'s host, untouched): its session log is the lane's, and
    the worker takes its send countdown, takeover watch and ``takeover_ok`` (as ``Lane._make_brain`` does). Its runner
    is made here (:func:`make_runner`), so a runner that can't be made is a build failure. Raises on a build failure:
    the runtime then fails the job with :data:`LINE_UNBUILT` (:class:`Unbuilt`)."""
    from yuki.one.loop import OneAgent

    agent = OneAgent(settings, logger if logger is not None else getattr(loop_agent, "logger", None), client=client,
                     dispatcher=dispatcher, memory=memory, lane=lane, hands=hands, runner_factory=runner_factory)
    if hands and loop_agent is not None:
        agent.send_countdown_s = float(getattr(loop_agent, "send_countdown_s", 0.0) or 0.0)
        agent.takeover = getattr(loop_agent, "takeover", None)
        agent.takeover_ok = getattr(loop_agent, "takeover_ok", None)
    agent.runner()   # raises when it can't be made: the job fails plainly, never on reads only
    return agent


class Unbuilt:
    """What a lane drives when its ONE agent could not be made: the job ends at once with :data:`LINE_UNBUILT` (an
    :class:`yuki.log.events.ErrorEvent`, shown as a failure), never on another brain. It has the caller surface the
    lane and the runtime read (``run``, ``cancel``, ``steer``, ``answer``, ``last_summary`` ...). ``why``: a code word
    for the log."""

    hands = False
    needed_hands = False
    #: A ONE lane's agent (the runtime reads it: no front-desk tool list applies, the agent hands over itself).
    one_agent = True
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


__all__ = ["LINE_UNBUILT", "SHARED_HOOKS", "TYPED_ONLY_ACTS", "Unbuilt", "WORKER_ONLY", "build_agent", "fresh_desk",
           "hands_of", "lane_tools", "make_runner", "needs_worker", "one_client", "ready_for_request"]
