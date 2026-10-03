"""Two agents, two threads, one mouse.

The **worker** lane is Yuki proper: one :class:`~yuki.agent.loop.Agent` with the
whole tool set, working through a FIFO queue of requests, and the only lane allowed
to touch the mouse and keyboard.

The **front desk** lane exists so the user is never told "busy, wait". While the
worker is mid-task, a second :class:`Agent` takes new requests, answers whatever it
can answer by looking at the machine, and hands anything that needs hands to the
worker's queue.

Both lanes drive their generator on their own :class:`QThread` and speak to the GUI
exclusively through the signals on :class:`AgentRuntime`, so nothing in the GUI
thread ever blocks on the model.

Keeping the front desk out of the mouse
---------------------------------------
Two things, and they are deliberately redundant:

* The front-desk agent is constructed with ``tool_names`` covering only the tools
  that look, and with ``extra_instructions`` telling it the situation it is in. The
  action tools are therefore never offered to the model, and a call to one comes
  back as "not available" without reaching the dispatcher.
* Its dispatcher is built on :class:`HandsBlockedBackend` anyway, so even if that
  subset were ever widened by accident the lane still physically cannot move the
  mouse: the nine action functions are replaced by one that touches nothing.

Hand-off is read from the same fact, never from the model's prose: if the front
desk asked for a tool it does not have, the request needs hands, so the runtime
queues the original text for the worker.
"""

from __future__ import annotations

import itertools
import queue
import time
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from PySide6.QtCore import QObject, QThread, Signal

from yuki.agent.loop import CANCELLED, PARKED, WITHDRAWN, Agent
from yuki.agent.memory import MemoryAccess, default_memory
from yuki.agent.subagents import HelperStep
from yuki.agent.tools import (
    ACTION_TOOL_NAMES,
    ALL_TOOL_NAMES,
    Backend,
    Dispatcher,
    default_backend,
)
from yuki.config import Settings
from yuki.log.events import (
    AskUser,
    ErrorEvent,
    Final,
    Say,
    SessionLogger,
    Thinking,
    Partial,
    ToolCall,
    ToolResult,
)
from yuki.ui.uilog import UiLog

#: The lane names. Every signal carries one, so the UI knows who is talking.
WORKER = "worker"
FRONT_DESK = "front_desk"
#: A question nobody answers sets its request aside after this long (s), freeing the lane (v3.14b: the six answered
#: questions in the live logs of 27-29 Sept took 3-43 s; the one never answered held the worker until the app quit).
ASK_PARK_S = 120.0
_PARK = object()

#: The tools the front desk may use: everything that only looks.
LOOK_ONLY_TOOLS: tuple[str, ...] = tuple(
    name for name in ALL_TOOL_NAMES if name not in ACTION_TOOL_NAMES
)

#: Who the front-desk instance is. Situational framing for that agent, passed as
#: its ``extra_instructions``; it decides nothing on the model's behalf.
FRONT_DESK_INSTRUCTIONS = (
    "A task is already running on this PC and owns the mouse and keyboard, so for "
    "now you are the one who talks to the user while the other you works. Answer "
    "this if you can without acting on the desktop -- you can still look at the "
    "desktop, read windows and query system facts. If it needs "
    "hands, call the tool that would do it: it does not run now, and the caller "
    "queues the request for the task lane the moment you finish. Then say in one "
    "line that it comes right after the running task, and call done. Never say it "
    "is being done or is done."
)

#: What the blocked backend tells the model if an action ever reaches it.
BLOCKED_SUMMARY = (
    "Another task owns the mouse and keyboard right now; tell the user this will be queued."
)


@dataclass
class BlockedAction:
    """An :class:`~yuki.actions.ActionResult`-shaped refusal.

    Defined here rather than imported so refusing an action never has to import
    :mod:`yuki.actions`, and with it the whole input stack.
    """

    ok: bool = False
    summary: str = BLOCKED_SUMMARY
    details: dict[str, Any] = field(default_factory=lambda: {"blocked": "worker_owns_input"})
    elapsed_ms: float = 0.0


class HandsBlockedBackend:
    """A backend that can look but not touch.

    Perception passes straight through to the real backend. Every function named in
    :data:`yuki.agent.tools.ACTION_TOOL_NAMES` is replaced by a no-op refusal, and
    the attempt is remembered in :attr:`attempted`.

    Args:
        inner: The real backend; :func:`yuki.agent.tools.default_backend` by default.
    """

    def __init__(self, inner: Backend | None = None) -> None:
        self.inner: Any = inner if inner is not None else default_backend()
        self.attempted: list[str] = []

    def reset(self) -> None:
        """Forget attempts from the previous request."""
        self.attempted = []

    def __getattr__(self, name: str) -> Any:
        if name in ACTION_TOOL_NAMES:

            def blocked(*args: Any, **kwargs: Any) -> BlockedAction:
                del args, kwargs
                self.attempted.append(name)
                return BlockedAction()

            blocked.__name__ = name
            return blocked
        return getattr(self.inner, name)


@dataclass
class Job:
    """One request handed to a lane."""

    id: int
    request: str
    #: The window that was in front before the overlay took the keyboard, so the
    #: agent knows where the user was (the overlay itself is in front by then).
    origin_hwnd: int | None = None
    #: Where the request came from: ``text`` (typed) or ``voice``. Logged only.
    source: str = "text"
    #: A window to read and attach to the request as it starts (``Agent.run(view_hwnd=)``): the caller
    #: said the request is about something inside it (voice: the talker's ``screen`` mark).
    view_hwnd: int | None = None
    #: The user's own words, what memory is searched with (``Agent.run(memory_query=)``; voice: the request and
    #: the words heard, without the hand-off's notes), and the previous request's; ``None``: the agent's own.
    memory_query: str | None = None
    previous_query: str | None = None
    #: What the user actually said or typed, for the authorization judge (``Agent.run(user_words=)``; voice: the heard
    #: turns, never the talker's own reading of them); ``None``: the agent takes the request's own words.
    user_words: tuple[str, ...] | None = None
    #: The thinker's request pack built while the user spoke (``Agent.run(pack=)``, revamp D S5; voice only, worker only).
    pack: Any = None
    #: V4: the app or route the user named for this request in their own words (``Agent.run(app_named=)``; voice: the
    #: talker's ``app_named``); ``None``: none named.
    app_named: str | None = None


def _words_tuple(words: "Iterable[str] | str | None") -> tuple[str, ...] | None:
    """The user's words as a tuple of non-empty strings (``None`` when there are none)."""
    if words is None:
        return None
    items = [words] if isinstance(words, str) else list(words)
    # V4 (3 Oct): an entry given as {"how", "text"} (the voice's shape before this fix) is its text, never its repr
    items = [w.get("text") if isinstance(w, dict) else w for w in items]
    out = tuple(str(w).strip() for w in items if str(w or "").strip())
    return out or None


class Lane(QThread):
    """One :class:`Agent` on one thread, fed by a queue.

    The thread runs no Qt event loop: it blocks on its job queue, and while a
    request is paused on a question it blocks on its answer queue. Everything it
    has to say leaves as a signal on the owning :class:`AgentRuntime`.

    Args:
        name: :data:`WORKER` or :data:`FRONT_DESK`.
        agent: The agent to drive.
        runtime: Owner whose signals this lane emits.
        blocked: The hands-blocking backend wrapper, when this lane has one.
    """

    def __init__(
        self,
        name: str,
        agent: Agent,
        runtime: "AgentRuntime",
        *,
        blocked: HandsBlockedBackend | None = None,
    ) -> None:
        super().__init__(runtime)
        self.name = name
        self.agent = agent
        self.runtime = runtime
        self.blocked = blocked
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._answers: queue.Queue[str | None] = queue.Queue()
        #: The job being driven now, and the ids still waiting in the queue
        #: (in order), for per-job cancel and steering. Guarded by ``_state``:
        #: the lane thread and the GUI thread both touch them.
        self._state = threading.Lock()
        self._current: Job | None = None
        self._waiting: list[int] = []
        #: Queued ids cancelled (or withdrawn) before they started, with why: skipped when dequeued.
        self._skip: dict[int, str] = {}
        #: V4: the app named for the running job after it became current (:meth:`name_app_job`), given to the agent
        #: again once its run has begun, so the run's own start cannot overwrite it (review F9).
        self._named_late: dict[int, str] = {}
        #: v3.14b: the request this lane set aside while it waited for an answer that did not come, as
        #: ``(job, question)``; a later answer carries it on as a new request (:meth:`AgentRuntime.answer`).
        self._parked: tuple[Job, str] | None = None
        #: How long a question may hold the lane before it is set aside (s; ``Settings.ask_park_s``).
        self.park_after_s = float(getattr(getattr(runtime, "settings", None), "ask_park_s", ASK_PARK_S))

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        """The tool subset this lane's agent was built with, if any."""
        return self.agent.tool_names

    # -- called from the GUI thread ----------------------------------------

    def enqueue(self, job: Job) -> int:
        """Add a job to this lane's FIFO and return how many are now waiting."""
        with self._state:
            self._waiting.append(job.id)
        self._jobs.put(job)
        return self._jobs.qsize()

    @property
    def current_id(self) -> int | None:
        """The id of the job being driven now, or ``None``."""
        with self._state:
            return self._current.id if self._current is not None else None

    def waiting_ids(self) -> list[int]:
        """Ids waiting in this lane's queue, oldest first (cancelled ones left out)."""
        with self._state:
            return [i for i in self._waiting if i not in self._skip]

    def cancel_job(self, job_id: int, reason: str = CANCELLED) -> str:
        """Cancel one job: the running one, or a queued one before it starts.

        ``reason`` is :data:`~yuki.agent.loop.CANCELLED` or :data:`~yuki.agent.loop.WITHDRAWN` (a newer
        request replaced it): a withdrawn job ends with ``withdrawn`` instead of ``failed``.

        Returns:
            ``running`` (cancel sent to the agent), ``queued`` (it will be
            skipped when its turn comes), or ``unknown`` (not on this lane).
        """
        with self._state:
            running = self._current is not None and self._current.id == job_id
            queued = job_id in self._waiting and job_id not in self._skip
            if queued and not running:
                self._skip[job_id] = reason
            parked = self._parked is not None and self._parked[0].id == job_id
            if parked:
                self._parked = None   # set aside on its question, and now dropped: an answer later starts nothing
        if running:
            self.cancel(reason)
            return "running"
        if parked:
            return "parked"
        return "queued" if queued else "unknown"

    def steer_job(self, job_id: int, text: str) -> bool:
        """Hand ``text`` to the running job's agent (:meth:`Agent.steer`); ``False`` if it is not running."""
        with self._state:
            running = self._current is not None and self._current.id == job_id
        if running:
            self.agent.steer(text)
        return running

    def name_app_job(self, job_id: int, app: str) -> bool:
        """V4: the app the user named, for the running job's agent (:meth:`Agent.set_app_named`); ``False`` if that
        job is not running here. Also kept for :meth:`_drive` to give again once the run has begun: a job is current
        a moment before ``Agent.run``'s body starts, and that start sets the request's named app from its own argument
        (review F9)."""
        with self._state:
            running = self._current is not None and self._current.id == job_id
            if running:
                self._named_late[job_id] = app
        setter = getattr(self.agent, "set_app_named", None)
        if running and callable(setter):
            setter(app)
            return True
        return False

    def answer(self, text: str) -> None:
        """Hand the user's answer to a paused request."""
        self._answers.put(text)

    def take_parked(self) -> tuple[Job, str] | None:
        """The request set aside on an unanswered question, if any (it is then no longer parked)."""
        with self._state:
            parked, self._parked = self._parked, None
        return parked

    def _wait_answer(self, job: Job) -> Any:
        """The user's answer to a question, ``None`` (cancelled), or :data:`_PARK`: set it aside (v3.14b).

        Before, the lane waited for the answer with no end: live 2026-09-28 21:06 a question nobody answered held the
        worker until the app quit, and every later request waited behind it. Now the question is set aside when a new
        request is waiting on this lane, or when it has gone unanswered for :attr:`park_after_s` (measured: the six
        answered questions in the live logs of 27-29 Sept took 3-43 s). Plumbing on the queue and the clock only.
        """
        asked_at = time.monotonic()
        while True:
            try:
                return self._answers.get(timeout=0.25)
            except queue.Empty:
                pass
            with self._state:
                waiting = [i for i in self._waiting if i not in self._skip]
            if waiting or time.monotonic() - asked_at >= self.park_after_s:
                return _PARK

    def cancel(self, reason: str = CANCELLED) -> None:
        """Cancel the request currently running on this lane.

        Takes effect at the agent's next checkpoint (a streaming response is dropped at its next
        event). A request paused on a question is released straight away with a cancellation answer.
        """
        self.agent.cancel(reason)
        if self.agent.awaiting_answer:
            self._answers.put(None)

    def shutdown(self) -> None:
        """Cancel, release any pause, and let the thread's loop end."""
        self.agent.cancel()
        self._answers.put(None)
        self._jobs.put(None)

    # -- the thread --------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - QThread entry point
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._state:
                if job.id in self._waiting:
                    self._waiting.remove(job.id)
                skipped = self._skip.pop(job.id, None)
                self._current = None if skipped else job
            if skipped:
                # Cancelled while it waited: it never reaches the agent, so a
                # cancel meant for it cannot land on the next request instead.
                if skipped == WITHDRAWN:
                    self.runtime.withdrawn.emit(self.name, job.id,
                                                "Withdrawn before it started: replaced by a newer request.")
                else:
                    self.runtime.failed.emit(self.name, job.id, "Cancelled before it started.")
                self.runtime.lane_done.emit(self.name, job.id, job.request, False, [])
                continue
            reached_for_hands: list[str] = []
            try:
                reached_for_hands = self._drive(job)
                # The agent writes its summary in the generator's finally, which
                # has run by now: the final/failed signal went out first, so the
                # UI can label the message that is already on screen.
                summary = self.agent.last_summary
                if summary is not None:
                    self.runtime.summarized.emit(self.name, job.id, dict(summary))
                late = self.agent.take_steers()
                if late:
                    # Said after the request's last step: the caller decides what
                    # to do with it (voice passes it on as a new request).
                    self.runtime.steer_unread.emit(self.name, job.id, late)
            finally:
                with self._state:
                    self._current = None
                    self._named_late.pop(job.id, None)
                self.runtime.lane_done.emit(
                    self.name, job.id, job.request, bool(reached_for_hands), reached_for_hands
                )

    def _drive(self, job: Job) -> list[str]:
        """Run one job to completion, turning agent events into signals.

        Returns:
            The tools this lane asked for but is not allowed to use. A non-empty
            list means the request needs the worker's hands.
        """
        if self.blocked is not None:
            self.blocked.reset()
        allowed = self.allowed_tools
        refused: list[str] = []
        runtime = self.runtime
        runtime.started.emit(self.name, job.id, job.request)

        # A spoken request runs at its own effort setting (heard live, and mostly one quick
        # action); a typed one at the agent's. Chosen by where it came from, never by its words.
        # Chosen by where it came from, never by its words: a spoken request also runs with
        # thinking off, a slim memory block and, when set, the voice thinker's own model
        # (docs/VOICE.md §7, measured there).
        settings = self.runtime.settings
        voice = job.source == "voice"
        effort = getattr(settings, "voice_brain_effort", None) if voice else None
        # v3.14: a spoken request starts fast (low effort, thinking off) and the same request moves to the
        # escalated profile for its next calls only on a measured signal (docs/VOICE.md v3.14, Agent.run).
        escalate = ((getattr(settings, "voice_brain_escalate_effort", "") or None,
                     getattr(settings, "voice_brain_escalate_thinking", None)) if voice else None)
        framed = False
        events = self.agent.run(
            job.request, origin_hwnd=job.origin_hwnd, effort=effort if voice else None,
            model=(getattr(settings, "voice_brain_model", "") or None) if voice else None,
            thinking=None if not voice or getattr(settings, "voice_brain_thinking", True) else False,
            lean=voice and not getattr(settings, "voice_brain_portrait", True),
            **({"view_hwnd": job.view_hwnd} if job.view_hwnd else {}),
            **({"escalate": escalate} if escalate and (escalate[0] or escalate[1] is not None) else {}),
            **({"voice": True} if voice else {}),
            **({"memory_query": job.memory_query} if job.memory_query else {}),
            **({"previous_query": job.previous_query} if job.previous_query else {}),
            **({"user_words": list(job.user_words)} if job.user_words else {}),
            **({"pack": job.pack} if job.pack is not None else {}),
            # V4: the app the user named (the job's own, or one named after it was queued: AgentRuntime.name_app)
            **({"app_named": named} if (named := job.app_named or getattr(runtime, "_apps_named", {}).get(job.id))
               else {}),
        )
        begun = False
        while True:
            try:
                event = next(events)
            except StopIteration:
                break
            except Exception as exc:  # the agent handles its own; this is ours
                runtime.failed.emit(self.name, job.id, f"{type(exc).__name__}: {exc}")
                break
            if not begun:
                # Review F9: the run's body has begun (its first event comes before any action is judged): an app
                # named for this job after it became current is given again, so the body's start did not lose it.
                begun = True
                with self._state:
                    late = self._named_late.pop(job.id, None)
                setter = getattr(self.agent, "set_app_named", None)
                if late and callable(setter):
                    setter(late)

            if isinstance(event, Partial):
                runtime.partial.emit(self.name, job.id, event.what, event.text)
            elif isinstance(event, Thinking):
                runtime.thought.emit(self.name, job.id, event.text)
            elif isinstance(event, ToolCall):
                if allowed is not None and event.name not in allowed:
                    refused.append(event.name)
                if not framed:   # the task frame is built before the first model call: the island's chips read it
                    framed = True
                    view = frame_view(self.agent)
                    if view is not None:
                        runtime.framed.emit(self.name, job.id, view)
                runtime.tool_called.emit(self.name, job.id, event.name, dict(event.input))
            elif isinstance(event, ToolResult):
                runtime.tool_finished.emit(self.name, job.id, event.name, event.ok, event.summary)
                if event.ok and event.payload:
                    from yuki.ui.status import describe_result

                    try:
                        better = describe_result(event.name, None, event.payload)
                    except Exception:   # presentation only: never in the way of the run
                        better = ""
                    if better:
                        runtime.tool_labelled.emit(self.name, job.id, event.name, better)
            elif isinstance(event, AskUser):
                self._drain_answers()
                runtime.asked.emit(self.name, job.id, event.question)
                answer = self._wait_answer(job)
                if answer is _PARK:
                    with self._state:
                        self._parked = (job, event.question)
                    self.agent.cancel(PARKED)
                    self.agent.answer("(no answer yet: the question stays open until they answer it)")
                elif answer is None:
                    self.agent.cancel()
                    self.agent.answer("(the user cancelled instead of answering)")
                else:
                    self.agent.answer(answer)
            elif isinstance(event, Say):
                runtime.said.emit(self.name, job.id, event.text)
            elif isinstance(event, HelperStep):
                # A read-only helper's step (yuki.agent.subagents): its own signal, and as a step of
                # the request (tool name "helper") for a chat that lists tool calls. Emitted directly,
                # so it never counts as a tool this lane reached for.
                runtime.helper_step.emit(self.name, job.id, event.index, event.state, event.text, event.ok)
                label = f"helper {event.index}"
                if event.state == "started":
                    runtime.tool_called.emit(self.name, job.id, "helper", {"helper": event.index, "task": event.text})
                elif event.state == "finished":
                    runtime.tool_finished.emit(self.name, job.id, "helper", event.ok, f"{label}: {event.text}")
            elif isinstance(event, Final):
                runtime.finished.emit(self.name, job.id, event.text)
            elif isinstance(event, ErrorEvent):
                if self.agent.cancelled and getattr(self.agent, "cancel_reason", CANCELLED) == PARKED:
                    # Set aside on its unanswered question (v3.14b): neither failed nor done; an answer later
                    # carries it on (AgentRuntime.answer).
                    runtime.parked.emit(self.name, job.id, event.text)
                elif self.agent.cancelled and getattr(self.agent, "cancel_reason", CANCELLED) == WITHDRAWN:
                    # Replaced by the user's newer request: a quiet end, never an error (the chat shows it
                    # cancelled; the voice says nothing of it).
                    runtime.withdrawn.emit(self.name, job.id, event.text)
                else:
                    runtime.failed.emit(self.name, job.id, event.text)

        if self.blocked is not None:
            refused.extend(name for name in self.blocked.attempted if name not in refused)
        if self.agent.cancelled:
            return []  # stopped or withdrawn: a request the user dropped is never handed to the worker
        return refused

    def _drain_answers(self) -> None:
        """Throw away answers left over from a cancelled request.

        Called immediately before a question is published, so nothing still in the
        queue can be a reply to *this* question.
        """
        while True:
            try:
                self._answers.get_nowait()
            except queue.Empty:
                return


class AgentRuntime(QObject):
    """Owns the two lanes, routes requests between them, and reports progress.

    Every signal starts with the lane name and the request id, so the UI can keep
    one card per request and know which lane an answer belongs to.

    Args:
        settings: Settings for the worker. The front desk gets its own copy, so
            the two lanes can run different models without stepping on each other.
        ui_log: Where UI-side decisions (lane chosen, queued, cancelled) are written.
        worker_agent: Override the worker agent.
        front_desk_agent: Override the front-desk agent.
        front_desk_blocked: The backend wrapper for the front desk.
        memory: Yuki's memory, shared by both lanes and the tray (so the
            portrait is fetched once for all of them, and both lanes log their
            exchanges under the one conversation session of this app run,
            ``memory.session_id``). Defaults to the process-wide one.

    Signals:
        started: ``(lane, id, request)`` -- a lane picked the request up.
        thought: ``(lane, id, text)``
        tool_called: ``(lane, id, name, input)``
        tool_finished: ``(lane, id, name, ok, summary)``
        tool_labelled: ``(lane, id, name, label)`` -- a finished tool's result named what it worked on
            (a call's app and time): a plainer line for its step.
        asked: ``(lane, id, question)`` -- that lane is now paused.
        finished: ``(lane, id, text)`` -- the closing message.
        failed: ``(lane, id, text)`` -- error or cancellation.
        withdrawn: ``(lane, id, text)`` -- the request was withdrawn because a newer request of the user's
            replaced it (:meth:`withdraw`): it ended quietly, as cancelled, not as an error.
        queued: ``(id, request, waiting)`` -- request parked for the worker.
        summarized: ``(lane, id, summary)`` -- the request's ``request_summary``
            (time, steps, tokens, estimated cost), right after finished/failed.
        lane_done: ``(lane, id, request, needed_hands, refused_tools)`` -- the lane
            is free again.
        said: ``(lane, id, text)`` -- a progress line from the agent's ``say``.
        handed_off: ``(front-desk id, worker id, waiting)`` -- a front-desk request
            that needed hands went to the worker's queue under a new id.
        steer_unread: ``(lane, id, texts)`` -- things the user added to a request
            (:meth:`steer`) that arrived after its last step.
        helper_step: ``(lane, id, helper, state, text, ok)`` -- a read-only helper of the request
            started (text: its task), used a tool (text: tool and summary) or finished (text: its
            report's first line); also sent as ``tool_called`` / ``tool_finished`` named ``helper``.
        notice: ``(kind, text)`` -- something to tell the user once, unasked (``cost``: the day's
            model spend passed ``Settings.daily_cost_alert_usd``); the tray shows it.
    """

    started = Signal(str, int, str)
    thought = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    tool_labelled = Signal(str, int, str, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    withdrawn = Signal(str, int, str)
    queued = Signal(int, str, int)
    summarized = Signal(str, int, dict)
    lane_done = Signal(str, int, str, bool, list)
    said = Signal(str, int, str)
    handed_off = Signal(int, int, int)
    #: v3.14b: ``(lane, id, text)``: the request was set aside on a question nobody answered (the lane goes on).
    parked = Signal(str, int, str)
    #: v3.14b: ``(parked id, new id)``: an answer came for a parked request, which carries on as the new one.
    resumed = Signal(int, int)
    steer_unread = Signal(str, int, list)
    helper_step = Signal(str, int, int, str, str, bool)
    #: ``(lane, job, what, text)``: the reply or the thinking written so far (``what``: reply | thinking), live
    partial = Signal(str, int, str, str)
    notice = Signal(str, str)
    #: ``(lane, id, frame)``: the request's task frame as plain data (:func:`frame_view`: who it may be about and
    #: their usual route, the playbooks shown), once, before its first step; the island's "from memory" chip.
    framed = Signal(str, int, dict)

    def __init__(
        self,
        settings: Settings,
        ui_log: UiLog,
        *,
        worker_agent: Agent | None = None,
        front_desk_agent: Agent | None = None,
        front_desk_blocked: HandsBlockedBackend | None = None,
        memory: MemoryAccess | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.ui_log = ui_log
        self.memory = memory if memory is not None else default_memory()
        self._ids = itertools.count(1)
        self._worker_outstanding: list[int] = []
        #: Where each front-desk request came from, for a hand-off to the worker.
        self._sources: dict[int, str] = {}
        #: The memory query each front-desk request came with, kept across a hand-off to the worker.
        self._memory_queries: dict[int, str] = {}
        self._user_words: dict[int, tuple[str, ...]] = {}   # front-desk requests' heard words, kept for a hand-off
        self._apps_named: dict[int, str] = {}   # front-desk requests' named app (V4), kept for a hand-off

        session = ui_log.logger.session_id
        console = ui_log.logger.console
        if worker_agent is None:
            worker_agent = Agent(
                settings,
                SessionLogger(
                    settings.sessions_dir, console=console, session_id=f"{session}-worker"
                ),
                lane=WORKER,
                memory=self.memory,
            )
        if front_desk_agent is None:
            front_desk_blocked = front_desk_blocked or HandsBlockedBackend()
            front_desk_agent = Agent(
                replace(settings),  # its own copy: the lanes may diverge later
                SessionLogger(
                    settings.sessions_dir, console=console, session_id=f"{session}-frontdesk"
                ),
                dispatcher=Dispatcher(
                    front_desk_blocked,
                    screenshot_policy=settings.screenshot_policy,
                    tool_timeout_s=settings.tool_timeout_s,
                ),
                # Every tool is shown, the actions answered by the blocked backend: reaching for one is how a
                # request that needs hands gets queued for the worker (lane_done's needed_hands). With only the
                # look tools it could never reach for one, so it said what it "would do" and nothing was queued
                # (2026-09-27: "Making that receipts folder in Downloads now." and no folder).
                tool_names=None,
                extra_instructions=FRONT_DESK_INSTRUCTIONS,
                prewarm=False,  # it cannot run a shell, so there is nothing to warm
                lane=FRONT_DESK,
                memory=self.memory,
            )

        self.worker = Lane(WORKER, worker_agent, self)
        self.front_desk = Lane(FRONT_DESK, front_desk_agent, self, blocked=front_desk_blocked)
        self.lane_done.connect(self._on_lane_done)
        #: The daily cost soft alert (yuki.core.cost): checked after every request and every
        #: ``COST_CHECK_S`` (incremental log reads), one ``notice`` a day past the threshold.
        self._cost_alert = None
        self._cost_stop = threading.Event()
        try:
            from yuki.core.cost import CostAlert, CostLedger

            self._cost_alert = CostAlert(
                float(getattr(settings, "daily_cost_alert_usd", 3.0)),
                CostLedger(settings.sessions_dir.parent),
                notify=lambda text, spend: self.notice.emit("cost", text),
                log=lambda event, **fields: self.ui_log.event(event, **fields),
            )
        except Exception as exc:  # the alert is optional; it must never stop the tray
            self.ui_log.event("cost_alert", error=f"{type(exc).__name__}: {exc}")
        self.summarized.connect(lambda *_: self.check_cost())

    # -- lifecycle ---------------------------------------------------------

    #: Seconds between two background checks of the day's spend (memory spends while no request runs).
    COST_CHECK_S = 600.0

    def start(self) -> None:
        """Start both lane threads (and the daily cost check)."""
        self.worker.start()
        self.front_desk.start()
        if self._cost_alert is not None:
            def loop() -> None:
                while not self._cost_stop.wait(self.COST_CHECK_S if self._cost_checked else 30.0):
                    self._cost_checked = True
                    self._cost_alert.check()

            self._cost_checked = False
            threading.Thread(target=loop, name="yuki-cost-check", daemon=True).start()

    def check_cost(self) -> None:
        """Check the day's spend now, off the GUI thread (one notice a day past the line)."""
        if self._cost_alert is not None:
            threading.Thread(target=self._cost_alert.check, name="yuki-cost-check-now", daemon=True).start()

    def stop(self) -> None:
        """Cancel everything, wait for both threads to end, then (bounded) for memory's turn log.

        Each lane hands its last exchange to memory's background writer as its
        request ends; the writer is a daemon thread, so without the flush the
        exchange that closed the app could be lost at exit.
        """
        self._cost_stop.set()
        for lane in (self.worker, self.front_desk):
            lane.shutdown()
        for lane in (self.worker, self.front_desk):
            lane.wait(4000)
        try:
            flushed = self.memory.flush_turns(3.0)
        except Exception as exc:  # shutting down must never fail on memory
            self.ui_log.event("memory_turns_flush", error=f"{type(exc).__name__}: {exc}")
        else:
            self.ui_log.event("memory_turns_flush", flushed=flushed)

    # -- requests ----------------------------------------------------------

    @property
    def worker_busy(self) -> bool:
        """True while the worker has a request in hand or waiting."""
        return bool(self._worker_outstanding)

    def submit(
        self, request: str, *, origin_hwnd: int | None = None, source: str = "text", lane: str | None = None,
        view_hwnd: int | None = None, memory_query: str | None = None, previous_query: str | None = None,
        user_words: "Iterable[str] | str | None" = None, pack: Any = None, needs_hands: bool | None = None,
        app_named: str | None = None,
    ) -> tuple[str, int]:
        """Route one request to a lane.

        The worker takes it whenever the worker is free. Otherwise the front desk
        takes it, so the user gets an answer now instead of a progress bar -- and
        if it turns out to need hands, :meth:`_on_lane_done` queues it for the
        worker. The choice is about who is holding the mouse; nothing here reads
        what the request says.

        Args:
            request: What the user typed (or said, for ``source="voice"``).
            source: ``text`` or ``voice``; logged, never routed on.
            lane: :data:`WORKER` to queue it for the worker even while the worker is busy (a request that
                needs hands, or one that replaces the request being withdrawn: the front desk would only
                look, then queue it anyway, a whole model call later). The caller's decision, never read
                from the words.
            view_hwnd: A window to read and attach as the request starts (see :class:`Job`).
            memory_query: The user's own words for the memory lookup (see :class:`Job`); ``None``: the agent
                takes them from the request itself.
            previous_query: The previous request's words, for a follow-up.
            user_words: What the user actually said (voice: the heard turns) for the authorization judge; the
                talker's own reading of them never counts as the user's words.
            pack: A request pack the voice built while the user spoke (``Agent.build_pack``); used only when the
                worker takes the request now (a pack read for an idle worker is stale on a queue).
            needs_hands: The caller's own mark, never read from the words (voice: the talker's ``acts``). ``False``
                (it only finds out or answers): the reader lane (the front desk: read tools and ``hand_over``) takes
                it while that lane is free, even when the worker is idle, so the worker's hands stay free (revamp D,
                30b §2, :func:`yuki.agent.lanes.route`); a reader that finds it needs hands hands it to the worker
                as before. ``None`` / ``True``: as before.
            app_named: The app or route the user named in their own words (V4; voice: the talker's ``app_named``):
                kept across a front-desk hand-off and given to ``Agent.run(app_named=)``.

        Returns:
            ``(lane name, request id)``.
        """
        request_id = next(self._ids)
        worker_idle = not self.worker_busy   # before this request counts: a pack is fresh only for an idle worker
        to_reader = False
        if lane != WORKER and needs_hands is False:
            from yuki.agent.lanes import route

            reader_busy = self.front_desk.current_id is not None or bool(self.front_desk.waiting_ids())
            to_reader = route(needs_hands=False, worker_busy=not worker_idle, reader_busy=reader_busy) == "reader"
        lane = self.front_desk if to_reader else (
            self.worker if (lane == WORKER or worker_idle) else self.front_desk)
        if lane is self.worker:
            self._worker_outstanding.append(request_id)
        else:
            self._sources[request_id] = source  # kept if the front desk hands it to the worker
            if memory_query:
                self._memory_queries[request_id] = memory_query
        words = _words_tuple(user_words)
        if words and lane is not self.worker:
            self._user_words[request_id] = words
        named = " ".join(str(app_named or "").split()) or None
        if named and lane is not self.worker:
            self._apps_named[request_id] = named
        fresh = pack if (lane is self.worker and worker_idle) else None
        waiting = lane.enqueue(Job(request_id, request, origin_hwnd, source, view_hwnd, memory_query,
                                   previous_query, words, fresh, named))
        self.ui_log.event(
            "lane", lane=lane.name, id=request_id, waiting=waiting, request=request,
            origin_hwnd=origin_hwnd, source=source, view_hwnd=view_hwnd,
            **({"reader": True} if to_reader else {}),
        )
        return lane.name, request_id

    def queue_for_worker(self, request: str, *, origin_id: int | None = None, source: str = "text",
                         memory_query: str | None = None, user_words: "Iterable[str] | str | None" = None,
                         app_named: str | None = None) -> int:
        """Put a request on the worker's queue explicitly.

        Args:
            request: What the user asked for.
            origin_id: The front-desk request it came from, for the log.
            source: Where the request came from (``text`` or ``voice``), kept across the hand-off.
            memory_query: The user's own words for the memory lookup, kept across the hand-off.

        Returns:
            The new request id.
        """
        request_id = next(self._ids)
        self._worker_outstanding.append(request_id)
        waiting = self.worker.enqueue(Job(request_id, request, None, source, memory_query=memory_query,
                                          user_words=_words_tuple(user_words),
                                          app_named=" ".join(str(app_named or "").split()) or None))
        self.ui_log.event(
            "queued", id=request_id, origin=origin_id, waiting=waiting, request=request
        )
        self.queued.emit(request_id, request, waiting)
        return request_id

    def answer(self, lane_name: str, text: str) -> None:
        """Answer the question a lane is paused on.

        v3.14b: when that question was set aside (nobody answered it in time, or a newer request came), the answer
        carries its request on as a new request on the same lane, with the question and the answer (``resumed``: the
        chat and the voice keep one card for it). The request's steps so far are in the agent's own context.

        Args:
            lane_name: :data:`WORKER` or :data:`FRONT_DESK`.
            text: The user's answer.
        """
        lane = self.lane(lane_name)
        parked = None if lane.agent.awaiting_answer else lane.take_parked()
        if parked is None:
            lane.answer(text)
            self.ui_log.event("answer", lane=lane_name, text=text)
            return
        job, question = parked
        request = (f"{job.request}\n\n[This request was set aside on your question, which went unanswered for a "
                   f"while: \"{question}\". The user has now answered: \"{text}\". Carry on with it from where you "
                   f"stopped.]")
        _lane, new_id = self.submit(request, origin_hwnd=job.origin_hwnd, source=job.source,
                                    lane=WORKER if lane_name == WORKER else None)
        self.ui_log.event("answer", lane=lane_name, text=text, resumed=job.id, new_id=new_id)
        self.resumed.emit(job.id, new_id)

    def cancel_worker(self) -> None:
        """Cancel whatever the worker is doing."""
        self.ui_log.event("cancel", lane=WORKER, outstanding=len(self._worker_outstanding))
        self.worker.cancel()

    def cancel(self, request_id: int, reason: str = CANCELLED) -> str:
        """Cancel one request by id, running or still queued, on whichever lane has it.

        Returns:
            ``running``, ``queued`` or ``unknown`` (see :meth:`Lane.cancel_job`).
        """
        how = "unknown"
        for lane in (self.worker, self.front_desk):
            how = lane.cancel_job(request_id, reason)
            if how != "unknown":
                break
        self.ui_log.event("cancel", id=request_id, how=how, reason=reason)
        return how

    def withdraw(self, request_id: int) -> str:
        """Withdraw one request because the user's newer request replaces it (running or queued).

        The same stop as :meth:`cancel` (a streaming response is dropped at once, a tool in flight
        finishes), ending with the ``withdrawn`` signal instead of ``failed``: shown as cancelled, never
        as an error, and never spoken. Returns how, as :meth:`cancel`.
        """
        return self.cancel(request_id, WITHDRAWN)

    def steer(self, request_id: int, text: str) -> bool:
        """Pass something the user added to a running request (:meth:`Agent.steer`).

        Returns:
            ``False`` when that request is not running (finished, or still queued).
        """
        ok = any(lane.steer_job(request_id, text) for lane in (self.worker, self.front_desk))
        self.ui_log.event("steer", id=request_id, delivered=ok, text=text)
        return ok

    def name_app(self, request_id: int, app: str | None) -> bool:
        """V4: the app or route the user named, for a request already submitted (a voice hand-off started before the
        talker's call was complete). Given to its agent now when it runs, else kept for when it starts (or is handed
        to the worker). Returns whether a running agent took it."""
        named = " ".join(str(app or "").split()) or None
        if not named:
            return False
        if any(lane.name_app_job(request_id, named) for lane in (self.worker, self.front_desk)):
            return True
        self._apps_named[request_id] = named
        return False

    def running_ids(self) -> dict[str, int | None]:
        """``lane -> id`` of the request each lane is driving now."""
        return {lane.name: lane.current_id for lane in (self.worker, self.front_desk)}

    def lane(self, name: str) -> Lane:
        """The lane object for a lane name."""
        return self.worker if name == WORKER else self.front_desk

    def set_model(self, name: str) -> str:
        """Switch the model both lanes use for their next request.

        Args:
            name: Alias (``sonnet``/``opus``) or a full model id.

        Returns:
            The resolved model id.
        """
        resolved = self.worker.agent.set_model(name)
        self.front_desk.agent.set_model(name)
        self.ui_log.event("model", model=resolved)
        return resolved

    def set_effort(self, level: str) -> str:
        """Change how hard both lanes think, from the next request onwards.

        Args:
            level: One of :data:`yuki.config.EFFORT_LEVELS`.

        Returns:
            The level now in force.
        """
        resolved = self.worker.agent.set_effort(level)
        self.front_desk.agent.set_effort(level)
        self.ui_log.event("effort", effort=resolved)
        return resolved

    # -- internal ----------------------------------------------------------

    def _on_lane_done(
        self,
        lane_name: str,
        request_id: int,
        request: str,
        needed_hands: bool,
        refused: list,
    ) -> None:
        """Book-keeping when a lane finishes a request (GUI thread).

        A front-desk request that reached for a tool it does not have is queued for
        the worker here. Whether it did is a recorded fact about the run -- the
        model asked for ``click`` and did not have it -- so nothing here reads the
        model's prose.
        """
        if lane_name == WORKER:
            if request_id in self._worker_outstanding:
                self._worker_outstanding.remove(request_id)
            self._apps_named.pop(request_id, None)
            return
        if needed_hands:
            self.ui_log.event("handoff", id=request_id, refused=list(refused))
            new_id = self.queue_for_worker(request, origin_id=request_id,
                                           source=self._sources.pop(request_id, "text"),
                                           memory_query=self._memory_queries.pop(request_id, None),
                                           user_words=self._user_words.pop(request_id, None),
                                           app_named=self._apps_named.pop(request_id, None))
            self.handed_off.emit(request_id, new_id, len(self.worker.waiting_ids()))
        else:
            self._sources.pop(request_id, None)
            self._user_words.pop(request_id, None)
            self._apps_named.pop(request_id, None)
            self._memory_queries.pop(request_id, None)


def frame_view(agent: Any) -> dict[str, Any] | None:
    """The agent's task frame for the request now running (``yuki.agent.frame.TaskFrame``, read from the agent; a
    frame from an older agent, or none, gives ``None``), as plain data for the island: the people with routes
    (name and each route's label, app and how many messages the user sent there), the referent candidates' labels and
    the playbook ids the frame showed. Read only; never raises."""
    frame = getattr(agent, "_frame", None)
    if frame is None:
        return None
    try:
        persons = []
        for p in list(getattr(frame, "persons", None) or [])[:2]:
            if not isinstance(p, dict):
                continue
            routes = [{"label": r.get("label"), "app": r.get("app"), "mine": r.get("mine"), "ref": r.get("ref")}
                      for r in (p.get("routes") or [])[:3] if isinstance(r, dict)]
            persons.append({"name": p.get("name"), "routes": routes})
        cands = list(getattr(frame, "people", None) or [])[:3]
        people = [str(getattr(c, "label", "")) for c in cands]
        where = ""
        names = {str(p.get("name") or "") for p in persons}
        for c in cands:   # where the frame found the person it names (the ask bar's "because")
            if str(getattr(c, "label", "")) in names and getattr(c, "where", ""):
                age = getattr(c, "age_s", None)
                ago = "" if age is None else " now" if age < 10 else f" {int(age)} s ago" if age < 90 else \
                    f" {int(round(age / 60))} min ago" if age < 5400 else f" {age / 3600:.1f} h ago"
                where = f"{c.where}{ago}"
                break
        return {"persons": persons, "people": [x for x in people if x], "where": where,
                "playbooks": [int(x) for x in getattr(frame, "playbooks", None) or [] if isinstance(x, int)]}
    except Exception:
        return None
