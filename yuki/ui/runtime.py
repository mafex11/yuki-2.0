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
from dataclasses import dataclass, field, replace
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from yuki.agent.loop import Agent
from yuki.agent.memory import MemoryAccess, default_memory
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
    SessionLogger,
    Thinking,
    ToolCall,
    ToolResult,
)
from yuki.ui.uilog import UiLog

#: The lane names. Every signal carries one, so the UI knows who is talking.
WORKER = "worker"
FRONT_DESK = "front_desk"

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
    "hands, say in one line exactly what you would do and call done: the caller "
    "queues it for the task lane the moment you finish."
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

    @property
    def allowed_tools(self) -> tuple[str, ...] | None:
        """The tool subset this lane's agent was built with, if any."""
        return self.agent.tool_names

    # -- called from the GUI thread ----------------------------------------

    def enqueue(self, job: Job) -> int:
        """Add a job to this lane's FIFO and return how many are now waiting."""
        self._jobs.put(job)
        return self._jobs.qsize()

    def answer(self, text: str) -> None:
        """Hand the user's answer to a paused request."""
        self._answers.put(text)

    def cancel(self) -> None:
        """Cancel the request currently running on this lane.

        Takes effect at the agent's next checkpoint. A request paused on a question
        is released straight away with a cancellation answer.
        """
        self.agent.cancel()
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
            reached_for_hands: list[str] = []
            try:
                reached_for_hands = self._drive(job)
                # The agent writes its summary in the generator's finally, which
                # has run by now: the final/failed signal went out first, so the
                # UI can label the message that is already on screen.
                summary = self.agent.last_summary
                if summary is not None:
                    self.runtime.summarized.emit(self.name, job.id, dict(summary))
            finally:
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

        events = self.agent.run(job.request, origin_hwnd=job.origin_hwnd)
        while True:
            try:
                event = next(events)
            except StopIteration:
                break
            except Exception as exc:  # the agent handles its own; this is ours
                runtime.failed.emit(self.name, job.id, f"{type(exc).__name__}: {exc}")
                break

            if isinstance(event, Thinking):
                runtime.thought.emit(self.name, job.id, event.text)
            elif isinstance(event, ToolCall):
                if allowed is not None and event.name not in allowed:
                    refused.append(event.name)
                runtime.tool_called.emit(self.name, job.id, event.name, dict(event.input))
            elif isinstance(event, ToolResult):
                runtime.tool_finished.emit(self.name, job.id, event.name, event.ok, event.summary)
            elif isinstance(event, AskUser):
                self._drain_answers()
                runtime.asked.emit(self.name, job.id, event.question)
                answer = self._answers.get()
                if answer is None:
                    self.agent.cancel()
                    self.agent.answer("(the user cancelled instead of answering)")
                else:
                    self.agent.answer(answer)
            elif isinstance(event, Final):
                runtime.finished.emit(self.name, job.id, event.text)
            elif isinstance(event, ErrorEvent):
                runtime.failed.emit(self.name, job.id, event.text)

        if self.blocked is not None:
            refused.extend(name for name in self.blocked.attempted if name not in refused)
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
            portrait is fetched once for all of them). Defaults to the
            process-wide one.

    Signals:
        started: ``(lane, id, request)`` -- a lane picked the request up.
        thought: ``(lane, id, text)``
        tool_called: ``(lane, id, name, input)``
        tool_finished: ``(lane, id, name, ok, summary)``
        asked: ``(lane, id, question)`` -- that lane is now paused.
        finished: ``(lane, id, text)`` -- the closing message.
        failed: ``(lane, id, text)`` -- error or cancellation.
        queued: ``(id, request, waiting)`` -- request parked for the worker.
        summarized: ``(lane, id, summary)`` -- the request's ``request_summary``
            (time, steps, tokens, estimated cost), right after finished/failed.
        lane_done: ``(lane, id, request, needed_hands, refused_tools)`` -- the lane
            is free again.
    """

    started = Signal(str, int, str)
    thought = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    queued = Signal(int, str, int)
    summarized = Signal(str, int, dict)
    lane_done = Signal(str, int, str, bool, list)

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
                tool_names=LOOK_ONLY_TOOLS,
                extra_instructions=FRONT_DESK_INSTRUCTIONS,
                prewarm=False,  # it cannot run a shell, so there is nothing to warm
                lane=FRONT_DESK,
                memory=self.memory,
            )

        self.worker = Lane(WORKER, worker_agent, self)
        self.front_desk = Lane(FRONT_DESK, front_desk_agent, self, blocked=front_desk_blocked)
        self.lane_done.connect(self._on_lane_done)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start both lane threads."""
        self.worker.start()
        self.front_desk.start()

    def stop(self) -> None:
        """Cancel everything and wait for both threads to end."""
        for lane in (self.worker, self.front_desk):
            lane.shutdown()
        for lane in (self.worker, self.front_desk):
            lane.wait(4000)

    # -- requests ----------------------------------------------------------

    @property
    def worker_busy(self) -> bool:
        """True while the worker has a request in hand or waiting."""
        return bool(self._worker_outstanding)

    def submit(self, request: str, *, origin_hwnd: int | None = None) -> tuple[str, int]:
        """Route one request to a lane.

        The worker takes it whenever the worker is free. Otherwise the front desk
        takes it, so the user gets an answer now instead of a progress bar -- and
        if it turns out to need hands, :meth:`_on_lane_done` queues it for the
        worker. The choice is about who is holding the mouse; nothing here reads
        what the request says.

        Args:
            request: What the user typed.

        Returns:
            ``(lane name, request id)``.
        """
        request_id = next(self._ids)
        lane = self.worker if not self.worker_busy else self.front_desk
        if lane is self.worker:
            self._worker_outstanding.append(request_id)
        waiting = lane.enqueue(Job(request_id, request, origin_hwnd))
        self.ui_log.event(
            "lane", lane=lane.name, id=request_id, waiting=waiting, request=request,
            origin_hwnd=origin_hwnd,
        )
        return lane.name, request_id

    def queue_for_worker(self, request: str, *, origin_id: int | None = None) -> int:
        """Put a request on the worker's queue explicitly.

        Args:
            request: What the user asked for.
            origin_id: The front-desk request it came from, for the log.

        Returns:
            The new request id.
        """
        request_id = next(self._ids)
        self._worker_outstanding.append(request_id)
        waiting = self.worker.enqueue(Job(request_id, request))
        self.ui_log.event(
            "queued", id=request_id, origin=origin_id, waiting=waiting, request=request
        )
        self.queued.emit(request_id, request, waiting)
        return request_id

    def answer(self, lane_name: str, text: str) -> None:
        """Answer the question a lane is paused on.

        Args:
            lane_name: :data:`WORKER` or :data:`FRONT_DESK`.
            text: The user's answer.
        """
        self.lane(lane_name).answer(text)
        self.ui_log.event("answer", lane=lane_name, text=text)

    def cancel_worker(self) -> None:
        """Cancel whatever the worker is doing."""
        self.ui_log.event("cancel", lane=WORKER, outstanding=len(self._worker_outstanding))
        self.worker.cancel()

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
            return
        if needed_hands:
            self.ui_log.event("handoff", id=request_id, refused=list(refused))
            self.queue_for_worker(request, origin_id=request_id)
