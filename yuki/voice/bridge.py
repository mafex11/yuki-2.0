"""The bridge between the mouth (voice thread, asyncio) and the brain (Qt runtime) — docs/VOICE.md §6.

The only code that touches both worlds, and every crossing is one-way and
explicit:

* **voice → GUI**: :meth:`VoiceBridge._gui` emits a Qt signal carrying a
  callable; the bridge lives on the GUI thread, so Qt queues it there. That is
  how ``AgentRuntime.submit`` / ``answer`` / ``steer`` / ``cancel`` are called.
* **GUI → voice**: runtime signals arrive on the GUI thread; the bridge turns
  them into ``(kind, payload)`` items and hands them to the waiting
  ``tell_yuki`` handler with ``loop.call_soon_threadsafe``.

Routing is on state only (rule 3): is a job running, queued, or waiting for an
answer. What the words mean is the mouth's call (the ``intent`` it passes).

The job table (§6.3) is the single source of truth for ``yuki_status``: every
request the runtime runs is in it (typed ones too), with its state, current
step, last spoken progress line and pending question.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from yuki.agent.tools import tool_label
from yuki.voice.mouth import brain_request

#: Job states.
QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED = (
    "queued", "running", "waiting_for_answer", "done", "failed", "cancelled")
OPEN_STATES = (QUEUED, RUNNING, WAITING)

#: Finished jobs kept in the table for ``yuki_status`` ("it finished a minute ago").
KEEP_FINISHED = 6


@dataclass
class Job:
    """One request as the bridge tracks it.

    ``request`` is the mouth's own message (or the typed text); it stays in
    memory for ``yuki_status`` and is never logged by the voice log.
    """

    job_id: int | None
    request: str
    source: str = "voice"
    lane: str | None = None
    state: str = QUEUED
    started_at: float = field(default_factory=time.monotonic)
    step: str | None = None
    last_say: str | None = None
    question: str | None = None
    outcome: str | None = None
    #: The lane's closing text, held until ``lane_done`` says whether it was
    #: the answer or a front desk handing the request to the worker.
    closing: tuple[str, str] | None = None
    pending_steers: list[str] = field(default_factory=list)
    #: The mouth asked for this job to be stopped (cancel_tell_yuki).
    cancel_requested: bool = False
    #: Only for voice jobs: where the ``tell_yuki`` handler waits.
    events: asyncio.Queue | None = None
    token: int = 0

    def status(self, now: float) -> dict[str, Any]:
        return {
            "task": self.request if len(self.request) <= 240 else self.request[:239] + "…",
            "by": "voice" if self.source == "voice" else "typed",
            "state": self.state,
            "for_seconds": round(now - self.started_at),
            **({"current_step": self.step} if self.step and self.state == RUNNING else {}),
            **({"last_said": self.last_say} if self.last_say else {}),
            **({"asking": self.question} if self.question and self.state == WAITING else {}),
            **({"outcome": self.outcome} if self.outcome and self.state not in OPEN_STATES else {}),
        }


#: How many of the user's latest utterances go to the brain with a request.
HEARD_TURNS = 3


def heard_words(context: Any, turns: int = HEARD_TURNS) -> list[str]:
    """The user's latest spoken turns in a Pipecat ``LLMContext``, oldest first (plumbing for the brain).

    Several, not one: the mouth may hand a request over a turn or two after
    it was made (the user said something else meanwhile), and the brain must
    see the words the request came from, not only the last thing said.
    """
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        if text.strip():  # async-tool results are "developer"/"tool" messages, so never picked up here
            out.append(text.strip())
            if len(out) >= turns:
                break
    return list(reversed(out))


class VoiceBridge(QObject):
    """Job table and hand-over between the mouth's tools and :class:`~yuki.ui.runtime.AgentRuntime`.

    Args:
        runtime: The runtime (or anything with the same signals and methods).
        log: ``VoiceLog``-like object with ``event(kind, **fields)``; content-free.
        foreground: Returns the window in front (``origin_hwnd`` for a voice request).
    """

    _gui_call = Signal(object)

    def __init__(self, runtime: Any, log: Any = None, *, foreground: Callable[[], int | None] | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.log = log
        self._foreground = foreground or _foreground_hwnd
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._unsubmitted: dict[int, Job] = {}
        self._finished: list[Job] = []
        self._tokens = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Set while a conversation is being torn down: a cancelled handler
        #: then must not cancel the brain's job (only the user can, §16).
        self.closing = False
        #: Called (on the voice loop) with a payload for a result that has no
        #: open tell_yuki call to go to (a correction that arrived too late).
        self.announce: Callable[[dict[str, Any]], Any] | None = None
        self._gui_call.connect(self._run)
        for name, slot in (
            ("started", self._on_started), ("tool_called", self._on_tool_called),
            ("said", self._on_said), ("asked", self._on_asked), ("finished", self._on_finished),
            ("failed", self._on_failed), ("lane_done", self._on_lane_done),
            ("handed_off", self._on_handed_off), ("steer_unread", self._on_steer_unread),
        ):
            signal = getattr(runtime, name, None)
            if signal is not None:
                signal.connect(slot)

    # -- plumbing --------------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """The voice loop results are delivered to (``None`` when no conversation runs).

        Detaching lets go of every job's handler queue: a job still running
        when a conversation ends keeps running, and its outcome lands as a
        card or in the strip like a typed request's.
        """
        self._loop = loop
        self.closing = False
        if loop is None:
            with self._lock:
                for job in list(self._jobs.values()) + list(self._unsubmitted.values()):
                    job.events = None

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a bridge error must not kill the GUI
            self._log("bridge_error", error=f"{type(exc).__name__}: {exc}")

    def _gui(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (queued; returns at once)."""
        self._gui_call.emit(fn)

    def _post(self, job: Job, kind: str, payload: Any) -> None:
        """Hand one item to the job's waiting ``tell_yuki`` handler (from the GUI thread)."""
        loop, queue = self._loop, job.events
        if loop is None or queue is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def _log(self, event_type: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(event_type, **fields)

    def _job(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def owns(self, job_id: int) -> bool:
        """Whether a job came from voice (the UI speaks its questions instead of popping the overlay)."""
        job = self._job(job_id)
        return job is not None and job.source == "voice"

    def open_voice_jobs(self) -> int:
        """Jobs of this conversation whose outcome the mouth still owes the user.

        A job waiting for the user's answer owes nothing until they speak: its
        question has been said, and it must not hold a quiet conversation open.
        """
        with self._lock:
            live = list(self._jobs.values()) + list(self._unsubmitted.values())
        return sum(1 for j in live if j.events is not None and j.state in (QUEUED, RUNNING))

    def snapshot(self) -> list[dict[str, Any]]:
        """``yuki_status``'s answer: open jobs first, then the last few finished ones."""
        now = time.monotonic()
        with self._lock:
            open_jobs = [j for j in list(self._unsubmitted.values()) + list(self._jobs.values())
                         if j.state in OPEN_STATES]
            done = list(self._finished[-3:])
        return [j.status(now) for j in sorted(open_jobs, key=lambda j: j.started_at)] + [
            j.status(now) for j in reversed(done)]

    def _finish(self, job: Job, state: str, outcome: str) -> None:
        job.state, job.outcome = state, outcome
        with self._lock:
            if job.job_id is not None:
                self._jobs.pop(job.job_id, None)
            self._finished.append(job)
            del self._finished[:-KEEP_FINISHED]

    # -- the mouth's tools (voice thread) ---------------------------------------

    async def tell_yuki(self, params: Any) -> None:
        """Pipecat handler for ``tell_yuki`` (async: registered with ``cancel_on_interruption=False``)."""
        from pipecat.frames.frames import FunctionCallResultProperties

        args = params.arguments if isinstance(params.arguments, dict) else {}
        message = str(args.get("message") or "").strip()
        intent = str(args.get("intent") or "new_task")
        heard = heard_words(params.context)
        text = brain_request(message or (heard[-1] if heard else ""), heard)
        if intent == "follow_up":
            target = self._follow_up_target()
            if target is not None:
                how = self._follow_up(target, text)
                self._log("bridge", op="follow_up", job_id=target.job_id, how=how,
                          call=params.tool_call_id)
                await params.result_callback({"status": f"passed on to the running task ({how})"})
                return
        job = Job(job_id=None, request=message or (heard[-1] if heard else ""), events=asyncio.Queue(),
                  token=next(self._tokens))
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        self._gui(lambda: self._submit(job, text, origin))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent=intent,
                  message_chars=len(message), origin_hwnd=origin)
        intermediate = FunctionCallResultProperties(is_final=False)
        try:
            while True:
                kind, payload = await job.events.get()
                self._log("mouth_tool_result", job_id=job.job_id, result=kind, final=kind in ("done", "failed"),
                          chars=len(str(payload)))
                if kind == "say":
                    await params.result_callback({"yuki_says": payload}, properties=intermediate)
                elif kind == "ask":
                    await params.result_callback({"yuki_asks": payload}, properties=intermediate)
                elif kind == "queued":
                    await params.result_callback({"status": payload}, properties=intermediate)
                elif kind in ("done", "failed"):
                    # "result", not "done": the word must not tell the mouth it worked;
                    # the brain's own words say what happened.
                    await params.result_callback({"result" if kind == "done" else "failed": payload})
                    return
        except asyncio.CancelledError:
            if not self.closing:
                # The mouth called cancel_tell_yuki: stop the brain's work too.
                self._gui(lambda: self._cancel(job))
                self._log("bridge", op="cancel", job_id=job.job_id, by="mouth")
            raise

    async def yuki_status(self, params: Any) -> None:
        """Pipecat handler for ``yuki_status``: the job table, instantly."""
        jobs = self.snapshot()
        self._log("bridge", op="status", jobs=len(jobs))
        await params.result_callback({"tasks": jobs} if jobs else {"tasks": [], "note": "Nothing is running."})

    def _follow_up_target(self) -> Job | None:
        """The most recent unfinished voice job, the one waiting for an answer first."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES]
        if not live:
            return None
        waiting = [j for j in live if j.state == WAITING]
        return max(waiting or live, key=lambda j: j.started_at)

    def _follow_up(self, job: Job, text: str) -> str:
        """Route a follow-up by the job's state: answer a question, steer running work, or hold it."""
        if job.state == WAITING and job.lane is not None:
            job.state, job.question = RUNNING, None
            lane = job.lane
            self._gui(lambda: self.runtime.answer(lane, text))
            return "answered"
        if job.state == RUNNING and job.job_id is not None:
            job_id = job.job_id
            self._gui(lambda: self._steer(job, job_id, text))
            return "added"
        job.pending_steers.append(text)  # queued: delivered when it starts
        return "added for when it starts"

    # -- GUI thread -------------------------------------------------------------

    def _submit(self, job: Job, text: str, origin: int | None) -> None:
        lane, job_id = self.runtime.submit(text, origin_hwnd=origin, source="voice")
        with self._lock:
            self._unsubmitted.pop(job.token, None)
            job.job_id, job.lane = job_id, lane
            self._jobs[job_id] = job
        self._log("bridge", op="submitted", token=job.token, job_id=job_id, lane=lane)

    def _steer(self, job: Job, job_id: int, text: str) -> None:
        if not self.runtime.steer(job_id, text):
            job.pending_steers.append(text)

    def _cancel(self, job: Job) -> None:
        # Queued on the GUI thread after this job's _submit, so its id is known.
        job.cancel_requested = True
        if job.job_id is not None:
            how = self.runtime.cancel(job.job_id)
            self._log("bridge", op="cancelled", job_id=job.job_id, how=how)

    def _typed(self, lane: str, job_id: int, request: str) -> Job:
        """A job the bridge did not submit (typed in the overlay): tracked for yuki_status."""
        job = Job(job_id=job_id, request=request, source="text", lane=lane, state=RUNNING)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def _on_started(self, lane: str, job_id: int, request: str) -> None:
        job = self._job(job_id) or self._typed(lane, job_id, request)
        job.lane, job.state = lane, RUNNING
        steers, job.pending_steers = job.pending_steers, []
        for text in steers:
            self._steer(job, job_id, text)
        self._log("bridge", op="state", job_id=job_id, lane=lane, state=RUNNING, source=job.source)

    def _on_tool_called(self, lane: str, job_id: int, name: str, tool_input: dict) -> None:
        job = self._job(job_id)
        if job is not None:
            job.step = tool_label(name) or name.replace("_", " ")

    def _on_said(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.last_say = text
        self._post(job, "say", text)

    def _on_asked(self, lane: str, job_id: int, question: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.state, job.question, job.lane = WAITING, question, lane
        self._post(job, "ask", question)
        self._log("bridge", op="state", job_id=job_id, state=WAITING)

    def _on_finished(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is not None:
            job.closing = ("done", text)

    def _on_failed(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is not None:
            job.closing = ("failed", text)

    def _on_lane_done(self, lane: str, job_id: int, request: str, needed_hands: bool, refused: list) -> None:
        job = self._job(job_id)
        if job is None:
            return
        if needed_hands:
            return  # the front desk passed it to the worker: handed_off follows with the new id
        kind, text = job.closing or ("failed", "It stopped without saying anything.")
        state = DONE if kind == "done" else (CANCELLED if job.cancel_requested else FAILED)
        self._finish(job, state, text)
        self._post(job, kind, text)
        self._log("bridge", op="state", job_id=job_id, state=state, source=job.source)

    def _on_handed_off(self, origin_id: int, new_id: int, waiting: int) -> None:
        with self._lock:
            job = self._jobs.pop(origin_id, None)
            if job is not None:
                job.job_id, job.lane, job.state, job.closing, job.step = new_id, "worker", QUEUED, None, None
                self._jobs[new_id] = job
        if job is None:
            return
        ahead = max(0, waiting - 1)
        self._post(job, "queued", "It needs Yuki's hands; queued behind the current task"
                   + (f" ({ahead} more ahead)" if ahead else "") + ".")
        self._log("bridge", op="handed_off", job_id=origin_id, new_id=new_id, waiting=waiting)

    def _on_steer_unread(self, lane: str, job_id: int, texts: list) -> None:
        """Something the user added arrived after the job's last step: run it as its own request."""
        with self._lock:
            finished = next((j for j in reversed(self._finished) if j.job_id == job_id), None)
        if finished is None or finished.source != "voice":
            return
        follow = Job(job_id=None, request="(added to the earlier task) " + " ".join(texts),
                     events=asyncio.Queue() if self._loop else None, token=next(self._tokens))
        with self._lock:
            self._unsubmitted[follow.token] = follow
        self._submit(follow, "\n".join(texts) + "\n\n[Added by voice right after the previous request finished.]",
                     None)
        self._log("bridge", op="late_follow_up", job_id=job_id, new_id=follow.job_id)
        loop, announce = self._loop, self.announce
        if loop is not None and announce is not None and follow.events is not None:
            async def relay() -> None:
                while True:
                    kind, payload = await follow.events.get()
                    await announce({{"say": "yuki_says", "ask": "yuki_asks", "done": "result"}.get(kind, kind): payload})
                    if kind in ("done", "failed"):
                        return
            asyncio.run_coroutine_threadsafe(relay(), loop)


def _foreground_hwnd() -> int | None:
    """The window in front at the end of the user's turn (``origin_hwnd``, §7.5)."""
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        return hwnd or None
    except Exception:  # noqa: BLE001
        return None
