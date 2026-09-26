"""The bridge between the mouth (voice thread, asyncio) and the brain (Qt runtime) — docs/VOICE.md §6.

The only code that touches both worlds, and every crossing is one-way and
explicit:

* **voice → GUI**: :meth:`VoiceBridge._gui` emits a Qt signal carrying a
  callable; the bridge lives on the GUI thread, so Qt queues it there. That is
  how ``AgentRuntime.submit`` / ``answer`` / ``steer`` / ``cancel`` are called.
  A follow-up waits for the GUI thread's routing decision (a future resolved
  with ``call_soon_threadsafe``), so a correction that reaches a task as it
  finishes can start a new task instead of being lost.
* **GUI → voice**: runtime signals arrive on the GUI thread; the bridge turns
  them into ``(kind, payload)`` items and hands them to the waiting
  ``tell_yuki`` handler with ``loop.call_soon_threadsafe``. The handler passes
  each one to the conversation's :class:`~yuki.voice.narrator.NarratorGate`,
  which decides when it is said.

Routing is on state only (rule 3): is a job running, queued, closing or
waiting for an answer. What the words mean is the mouth's call (the ``intent``
it passes).

Each job keeps its real action log (:class:`~yuki.voice.progress.JobLog`):
the source for spoken progress (real tool calls only) and for
``check_progress`` ("how's it going?"). The job table is also the source for
:meth:`snapshot` (the UI and the tests).
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from PySide6.QtCore import QObject, Signal

from yuki.voice.mouth import SAVED_NOTE, SPOKEN_NOTE, brain_request, progress_payload, result_payload
from yuki.voice.narrator import RANKS, Item, condense
from yuki.voice.progress import JobLog, StepPacer, step_of

#: Job states.
QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED = (
    "queued", "running", "waiting_for_answer", "done", "failed", "cancelled")
OPEN_STATES = (QUEUED, RUNNING, WAITING)

#: Finished jobs kept in the table ("it finished a minute ago", "want the rest?").
KEEP_FINISHED = 6
#: A finished job still answers check_progress for this long.
RECENT_S = 180.0


@dataclass
class Job:
    """One request as the bridge tracks it.

    ``request`` is the mouth's request (or the typed text); it stays in
    memory for progress answers and is never logged by the voice log.
    """

    job_id: int | None
    request: str
    source: str = "voice"
    lane: str | None = None
    state: str = QUEUED
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    step: str | None = None
    last_say: str | None = None
    question: str | None = None
    outcome: str | None = None
    #: What of the outcome has not been spoken yet ("want the rest?").
    rest: str = ""
    #: The lane's closing text, held until ``lane_done`` says whether it was
    #: the answer or a front desk handing the request to the worker.
    closing: tuple[str, str] | None = None
    pending_steers: list[str] = field(default_factory=list)
    #: The mouth asked for this job to be stopped (cancel_tell_yuki).
    cancel_requested: bool = False
    #: Only for voice jobs: where the ``tell_yuki`` handler waits.
    events: asyncio.Queue | None = None
    token: int = 0
    log: JobLog | None = None
    pacer: StepPacer | None = None
    #: Handed over with intent ``remember`` (a rule, correction or life fact to keep).
    remember: bool = False
    #: The desktop action it is running now, in a few words (for the shared action log).
    acting: str = ""

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


def heard_words(context: Any, turns: int = HEARD_TURNS, skip: Callable[[Any], bool] | None = None) -> list[str]:
    """The user's latest spoken turns in a Pipecat ``LLMContext``, oldest first (plumbing for the brain).

    Several, not one: the mouth may hand a request over a turn or two after
    it was made (the user said something else meanwhile), and the brain must
    see the words the request came from, not only the last thing said.
    ``skip`` leaves out user-role messages nobody said: Pipecat's context summary is
    one (live 2026-09-25 the thinker was told the user had said a whole summary).
    """
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if skip is not None and skip(message):
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
        settings: Voice settings (result length, progress lines); defaults when ``None``.
        hands_backend: The desktop the mouth's hands act on (docs/VOICE.md §5.6); ``None``:
            the brain worker's own backend (the real desktop, or a test's stub).
    """

    _gui_call = Signal(object)

    def __init__(self, runtime: Any, log: Any = None, *, foreground: Callable[[], int | None] | None = None,
                 settings: Any = None, hands_backend: Any = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.log = log
        self._foreground = foreground or _foreground_hwnd
        self._max_sentences = int(getattr(settings, "voice_result_sentences", 2))
        self._max_chars = int(getattr(settings, "voice_result_chars", 280))
        self._progress_lines = int(getattr(settings, "voice_progress_lines", 2))
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
        #: The conversation's narrator gate (``None``: deliver at once, as in tests).
        self.gate: Any = None
        #: ``async (log_text, question) -> (text | None, facts)``: the isolated progress call.
        self.progress_answerer: Callable[[str, str], Awaitable[tuple[str | None, dict]]] | None = None
        #: The mouth's own quick tools in flight (check_progress): the gate waits for them.
        self.busy_tools = 0
        #: Whether the mouth's reply that is making a tool call has put any words out
        #: (set by the conversation; ``None``: assume it has).
        self.spoke_in_reply: Callable[[], bool] | None = None
        #: ``async ()``: a ``remember`` hand-off was saved (the conversation re-reads the rules).
        self.on_remembered: Callable[[], Awaitable[None]] | None = None
        #: ``(tool_call_id)``: words were spoken for this call (its say_first), set by the conversation.
        self.mark_spoken: Callable[[str], None] | None = None
        #: Whether the mouth run making the current tool call answered a user turn (not a
        #: result arriving); set by the conversation. ``None``: assume it did.
        self.user_turn: Callable[[], bool] | None = None
        #: ``(message) -> bool``: a user-role context message nobody said to Yuki (the context
        #: summary; in a call, what the mouth judged was said to the people on it).
        self.not_heard: Callable[[Any], bool] | None = None
        #: Whether the conversation is in call mode (set by the conversation; docs/VOICE.md §25).
        self.call_mode: Callable[[], bool] | None = None
        #: ``(tool_call_id) -> bool``: the call's say_first was already spoken while the mouth was still
        #: writing the call (v3.5, set by the conversation).
        self.said_early: Callable[[str], bool] | None = None
        #: Called on the voice loop whenever a job starts, changes state or finishes (the pill shows
        #: "Working on it" only while a job really runs; v3.5). Set by the conversation.
        self.on_jobs_changed: Callable[[], None] | None = None
        #: ``(job_id, state, summary)``: a job was submitted, started, took a step, asked, or finished (the chat
        #: UI's tool steps; on the GUI thread; set by the session). Words: the screen only.
        self.on_job_event: Callable[[int, str, str], None] | None = None
        #: ``()``: the user asked to end this voice conversation (``end_conversation``); set by the conversation.
        self.end_requested: Callable[[], Any] | None = None
        #: When a result or a question was handed to the mouth to be said (monotonic): a stay_quiet in the
        #: run it started is refused once, because a result or a question is always said (v3.5).
        self.said_owed_at: float | None = None
        #: The mouth's own one-step hands (docs/VOICE.md §5.6), over the brain's backend, and what
        #: they and the thinker did on the desktop (shared with the mouth and every hand-off).
        from yuki.voice.hands import ActionLog, Hands, hands_backend as _brain_backend

        self.actions = ActionLog()
        backend = hands_backend if hands_backend is not None else _brain_backend(runtime)
        self.hands: Hands | None = Hands(backend, log, self.actions) if backend is not None else None
        if self.hands is not None:
            self.hands.bridge = self
        self._gui_call.connect(self._run)
        for name, slot in (
            ("started", self._on_started), ("tool_called", self._on_tool_called),
            ("tool_finished", self._on_tool_finished),
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
            self.gate = None
            self.progress_answerer = None
            self.spoke_in_reply = None
            self.on_remembered = None
            self.mark_spoken = None
            self.user_turn = None
            self.call_mode = None
            self.said_early = None
            self.on_jobs_changed = None
            self.end_requested = None
            self.busy_tools = 0
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

    def _job_event(self, job: Job, state: str, summary: str) -> None:
        hook = self.on_job_event
        if hook is not None and job.job_id is not None:
            try:
                hook(int(job.job_id), state, summary[:400])
            except Exception:  # noqa: BLE001 - the screen never breaks the bridge
                pass

    def _changed(self) -> None:
        """A job started, changed state or finished: tell the conversation (on its loop; any thread)."""
        loop, hook = self._loop, self.on_jobs_changed
        if loop is not None and hook is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(hook)
            except RuntimeError:  # the loop closed in between
                pass

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

    def desk_job(self) -> str | None:
        """A few words on the job acting on the desktop now (running on the worker, the lane with hands), else
        ``None``: while one runs, the talker's window hands wait (one actor at a time; v3.5)."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) if j.state == RUNNING and j.lane == "worker"]
        if not live:
            return None
        job = max(live, key=lambda j: j.started_at)
        task = job.request if len(job.request) <= 80 else job.request[:79] + "…"
        return f"task \"{task}\"" + (f", now: {job.acting[:80]}" if job.acting else "")

    def brain_busy(self) -> bool:
        """Whether any request (typed or spoken) is still open."""
        with self._lock:
            return any(j.state in OPEN_STATES for j in list(self._jobs.values()) + list(self._unsubmitted.values()))

    def snapshot(self) -> list[dict[str, Any]]:
        """The job table: open jobs first, then the last few finished ones."""
        now = time.monotonic()
        with self._lock:
            open_jobs = [j for j in list(self._unsubmitted.values()) + list(self._jobs.values())
                         if j.state in OPEN_STATES]
            done = list(self._finished[-3:])
        return [j.status(now) for j in sorted(open_jobs, key=lambda j: j.started_at)] + [
            j.status(now) for j in reversed(done)]

    def _finish(self, job: Job, state: str, outcome: str) -> None:
        job.state, job.outcome, job.finished_at = state, outcome, time.monotonic()
        with self._lock:
            if job.job_id is not None:
                self._jobs.pop(job.job_id, None)
            self._finished.append(job)
            del self._finished[:-KEEP_FINISHED]
        self._changed()

    def _new_job(self, request: str, *, source: str = "voice", with_queue: bool = True) -> Job:
        return Job(job_id=None, request=request, source=source, token=next(self._tokens),
                   events=asyncio.Queue() if with_queue else None, log=JobLog(request=request),
                   pacer=StepPacer(self._progress_lines))

    # -- the mouth's tools (voice thread) ---------------------------------------

    async def tell_yuki(self, params: Any) -> None:
        """Pipecat handler for ``tell_yuki`` (async: registered with ``cancel_on_interruption=False``)."""
        args = params.arguments if isinstance(params.arguments, dict) else {}
        request = str(args.get("request") or args.get("message") or "").strip()
        context = str(args.get("context") or "").strip() or None
        corrects = str(args.get("corrects") or "").strip() or None
        intent = str(args.get("intent") or "new_task")
        heard = heard_words(params.context, skip=self.not_heard)
        request = request or (heard[-1] if heard else "")
        remember = intent == "remember"
        done_before = self.actions.text()
        text = brain_request(request, heard, context=context, remember=remember, done_before=done_before,
                             corrects=corrects)
        await self._ack_if_silent(params, args)
        if intent == "follow_up":
            target = self._follow_up_target()
            if target is not None:
                how = await self._follow_up(target, text)
                self._log("bridge", op="follow_up", job_id=target.job_id, how=how, call=params.tool_call_id)
                if how != "restart":
                    # The mouth acknowledged in the same reply: this settles the call without
                    # another turn (measured: with a turn, it asked itself "how's it going?").
                    from pipecat.frames.frames import FunctionCallResultProperties

                    await params.result_callback({"status": f"passed on to the running task ({how})"},
                                                 properties=FunctionCallResultProperties(run_llm=False))
                    return
                # The task was finishing: start the addition as its own task, quietly
                # (gradient-bang's task_closing path). This call carries its outcome.
                prev = target.outcome or (target.closing[1] if target.closing else "")
                text = brain_request(request, heard, context="; ".join(x for x in (
                    context, f'this adds to the request just before it ("{target.request}"), which had just '
                             f'finished{": " + condense(prev, max_sentences=2, max_chars=300)[0] if prev else ""}')
                    if x), done_before=done_before, corrects=corrects)
        job = self._new_job(request)
        if remember:
            # Saved quietly: no progress lines, and its outcome folded in unless it failed.
            job.remember = True
            job.pacer = StepPacer(0)
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        self._gui(lambda: self._submit(job, text, origin))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent=intent,
                  request_chars=len(request), context_chars=len(context or ""), origin_hwnd=origin,
                  done_before_chars=len(done_before), heard_turns=len(heard), corrects_chars=len(corrects or ""))
        self._changed()
        try:
            await self._relay(job, params)
        except asyncio.CancelledError:
            if not self.closing:
                # The mouth called cancel_tell_yuki: stop the brain's work too, and say
                # nothing more of it (a progress line of a stopped task is noise).
                self._gui(lambda: self._cancel(job))
                if self.gate is not None:
                    for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                        if item.drop is not None:
                            item.drop()
                self._log("bridge", op="cancel", job_id=job.job_id, by="mouth")
            raise

    async def _ack_if_silent(self, params: Any, args: dict[str, Any]) -> None:
        """Say the call's ``say_first`` words now when the reply that made it said nothing aloud.

        The mouth's words normally come before the call in its reply and are already
        being spoken; measured 2026-09-25 (live and in a text probe, 5 of 5), it also
        hands over with no words at all, and the user heard silence until the first
        progress line 11 s later. Whether the reply had words is a fact about the stream
        (:attr:`spoke_in_reply`), not about what they say; the words are the mouth's own.
        """
        words = str(args.get("say_first") or "").strip()
        if self.said_early is not None and self.said_early(str(params.tool_call_id)):
            return  # already said while the mouth was writing the call (v3.5)
        spoke = self.spoke_in_reply() if self.spoke_in_reply is not None else True
        if spoke or not words:
            return
        from pipecat.frames.frames import TTSSpeakFrame

        await params.llm.push_frame(TTSSpeakFrame(text=words))
        if self.mark_spoken is not None:
            self.mark_spoken(params.tool_call_id)
        self._log("bridge", op="ack_spoken", call=params.tool_call_id, chars=len(words))

    async def _relay(self, job: Job, params: Any) -> None:
        """Pass the job's items to the narrator gate (or straight to the mouth) until its final result is delivered."""
        from pipecat.frames.frames import FunctionCallResultProperties

        loop = asyncio.get_running_loop()
        while True:
            kind, payload = await job.events.get()
            self._log("mouth_tool_result", job_id=job.job_id, result=kind, final=kind in ("done", "failed"),
                      chars=len(str(payload)))
            if kind == "done" and job.remember:
                # The mouth already reacted when it handed this over; the brain's "saved" is
                # put into the conversation without a turn (a spoken "noted" would repeat it).
                await params.result_callback({"saved": payload, "how_to_handle_it": SAVED_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                self._log("bridge", op="remembered", job_id=job.job_id, chars=len(payload))
                if self.on_remembered is not None:
                    await self.on_remembered()  # the mouth's own rules layer follows at once
                return
            if kind in ("done", "failed"):
                head, rest = condense(payload, max_sentences=self._max_sentences, max_chars=self._max_chars)
                job.rest = rest
                delivered = loop.create_future()

                async def final(run_llm: bool, stale: bool, kind=kind, head=head, rest=rest,
                                delivered=delivered) -> None:
                    if run_llm and not stale:
                        self.said_owed_at = time.monotonic()
                    await params.result_callback(result_payload(kind, head, more=bool(rest), held_back=stale,
                                                                request=job.request),
                                                 properties=FunctionCallResultProperties(run_llm=run_llm))
                    if not delivered.done():
                        delivered.set_result(stale)

                self._log("bridge", op="condensed", job_id=job.job_id, chars=len(payload), spoken_chars=len(head),
                          rest_chars=len(rest))
                await self._offer(job, "final", final)
                await delivered
                return
            if kind == "step":
                label, detail = payload
                body: dict[str, Any] = progress_payload(label, detail)
            elif kind == "say":
                body = {"yuki_says": payload}
            elif kind == "ask":
                body = {"yuki_asks": payload}
            else:
                body = {"status": payload}

            async def intermediate(run_llm: bool, stale: bool, body=body, kind=kind) -> None:
                del stale
                if run_llm and kind == "ask":
                    self.said_owed_at = time.monotonic()
                await params.result_callback(body, properties=FunctionCallResultProperties(
                    is_final=False, run_llm=run_llm))

            drop = None
            if kind == "step" and job.pacer is not None:
                drop = (lambda label=payload[0]: job.pacer.dropped(label))
            await self._offer(job, kind, intermediate, drop)

    async def _offer(self, job: Job, kind: str, deliver: Callable[[bool, bool], Awaitable[None]],
                     drop: Callable[[], None] | None = None) -> None:
        gate = self.gate
        if gate is None:
            await deliver(True, False)
            return
        gate.enqueue(Item(job=job.token, kind=kind, deliver=deliver, drop=drop))

    async def check_progress(self, params: Any) -> None:
        """Pipecat handler for ``check_progress``: an isolated one-shot answer over the task's real log."""
        args = params.arguments if isinstance(params.arguments, dict) else {}
        question = str(args.get("question") or "").strip()
        if self.user_turn is not None and not self.user_turn():
            # Called in a run that a result or progress message started, not the user: nobody
            # asked (measured 2026-09-25: after a progress line the mouth checked progress on
            # its own, twice, and each answer was one more spoken turn). Settled quietly.
            from pipecat.frames.frames import FunctionCallResultProperties

            self._log("bridge", op="progress", refused="not_asked")
            await params.result_callback({"progress": None, "note": "Nobody asked; check_progress is only for "
                                          "when the user asks how a task is going."},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        job = self._progress_target()
        if job is None or job.log is None:
            self._log("bridge", op="progress", jobs=0)
            await params.result_callback({"progress": None, "note": "Nothing is running or recently finished."})
            return
        state = job.state if not (job.closing and job.state in OPEN_STATES) else "finishing"
        waiting = self.gate.take(job.token) if self.gate is not None and job.source == "voice" else []
        if waiting:
            # It has finished and its result is still waiting for a quiet moment: that result
            # is the answer. It goes into the conversation now (not spoken on its own), and
            # this call tells the mouth to say it.
            for item in waiting:
                await item.deliver(False, False)
            self._log("bridge", op="progress", job_id=job.job_id, state=state, answered="result")
            await params.result_callback({"progress": "It has just finished; its result is right above. Say "
                                                      "that result now, briefly.", "state": state})
            return
        answer, facts = None, {}
        self.busy_tools += 1
        try:
            if self.progress_answerer is not None:
                answer, facts = await self.progress_answerer(job.log.render(state), question)
        finally:
            self.busy_tools = max(0, self.busy_tools - 1)
            if self.gate is not None:
                self.gate.poke()
        self._log("bridge", op="progress", job_id=job.job_id, state=state, log_lines=len(job.log.lines),
                  answered=answer is not None, **{k: v for k, v in facts.items() if k != "error"},
                  error=facts.get("error"))
        if answer is None:
            await params.result_callback({"tasks": [job.status(time.monotonic())]})
            return
        await params.result_callback({"progress": answer, "state": state,
                                      "for_seconds": round(time.monotonic() - job.started_at),
                                      "how_to_say_it": "Say this in your own words, adding nothing."})

    async def more_of_result(self, params: Any) -> None:
        """Pipecat handler for ``more_of_result``: the next part of the latest result that has more."""
        with self._lock:
            job = next((j for j in reversed(self._finished) if j.source == "voice" and j.rest), None)
        if job is None:
            self._log("bridge", op="more", found=False)
            await params.result_callback({"more": None, "note": (
                "Nothing of the last result was held back. If they want more detail, that is a new "
                "question for the thinker: hand it over with tell_yuki in their words.")})
            return
        head, rest = condense(job.rest, max_sentences=self._max_sentences, max_chars=self._max_chars)
        job.rest = rest
        self._log("bridge", op="more", found=True, job_id=job.job_id, spoken_chars=len(head), rest_chars=len(rest))
        await params.result_callback({"more": head, "more_available": bool(rest),
                                      "how_to_say_it": SPOKEN_NOTE + (" Still more after this." if rest else "")})

    async def stay_quiet(self, params: Any) -> None:
        """Pipecat handler for ``stay_quiet``: the mouth chose to say nothing; settled without a turn."""
        from pipecat.frames.frames import FunctionCallResultProperties

        by_user = self.user_turn() if self.user_turn is not None else None
        in_call = bool(self.call_mode()) if self.call_mode is not None else False
        owed, self.said_owed_at = self.said_owed_at, None
        if not by_user and owed is not None and time.monotonic() - owed < 20.0:
            # This run was started by a result or a question to be said (live 2026-09-26: a result met
            # with stay_quiet was never heard). Refused once; the mouth says it now.
            self._log("bridge", op="quiet", refused="result_owed")
            await params.result_callback({"not_quiet": "A result, a failure or a question is always said. Say what it "
                                          "says now, in a few words of your own."})
            return
        # In a call, staying quiet on the user's own turn is the mouth's judgement that the words
        # were for the people on the call (logged as such; content-free).
        with_words = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else None
        self._log("bridge", op="quiet", asked_by_user=by_user, with_words=with_words,
                  **({"call_mode": True, "addressed": "others" if by_user else None} if in_call else {}))
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

    async def end_conversation(self, params: Any) -> None:
        """Pipecat handler for ``end_conversation``: the user wants this voice conversation to stop now.

        Its say_first (a short goodbye) was spoken as it was written; the conversation ends once it has
        played. Nothing is saved: stopping a conversation is never a standing rule (live 2026-09-26, "you can
        turn off the voice then" was saved as a rule to answer in text only). Running tasks go on.
        """
        from pipecat.frames.frames import FunctionCallResultProperties

        args = params.arguments if isinstance(params.arguments, dict) else {}
        await self._ack_if_silent(params, args)
        self._log("bridge", op="end_conversation")
        await params.result_callback({"ok": True, "note": "The conversation ends after your goodbye."},
                                     properties=FunctionCallResultProperties(run_llm=False))
        if self.end_requested is not None:
            self.end_requested()

    def _progress_target(self) -> Job | None:
        """The task "how's it going?" is about: the newest open voice task, else any open one, else a recent one."""
        now = time.monotonic()
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values()) if j.state in OPEN_STATES]
            recent = [j for j in self._finished if j.finished_at and now - j.finished_at < RECENT_S]
        voice = [j for j in live if j.source == "voice"]
        pool = voice or live or recent
        return max(pool, key=lambda j: j.started_at) if pool else None

    def _follow_up_target(self) -> Job | None:
        """The most recent unfinished voice job, the one waiting for an answer first."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES]
        if not live:
            return None
        waiting = [j for j in live if j.state == WAITING]
        return max(waiting or live, key=lambda j: j.started_at)

    async def _follow_up(self, job: Job, text: str) -> str:
        """Route a follow-up by the job's state, decided on the GUI thread; returns how.

        ``answered`` (it was waiting for the user), ``added`` (steered into the
        running work), ``added for when it starts`` (queued), or ``restart``:
        the job was already finishing, so the caller starts the addition as a
        new task.
        """
        loop = asyncio.get_running_loop()
        decided: asyncio.Future = loop.create_future()

        def decide() -> None:
            how = self._route(job, text)
            loop.call_soon_threadsafe(lambda: decided.done() or decided.set_result(how))

        self._gui(decide)
        try:
            return await asyncio.wait_for(decided, timeout=5.0)
        except asyncio.TimeoutError:
            return "restart"

    def _route(self, job: Job, text: str) -> str:
        """On the GUI thread: where a follow-up goes, by the job's state at this moment."""
        if job.log is not None:
            job.log.add("the user added", text.split("\n\n[", 1)[0])
        if job.closing is not None or job.state not in OPEN_STATES:
            return "restart"
        if job.state == WAITING and job.lane is not None:
            job.state, job.question = RUNNING, None
            self.runtime.answer(job.lane, text)
            self._changed()
            return "answered"
        if job.state == RUNNING and job.job_id is not None:
            if self.runtime.steer(job.job_id, text):
                return "added"
            return "restart"
        job.pending_steers.append(text)  # queued: delivered when it starts
        return "added for when it starts"

    # -- GUI thread -------------------------------------------------------------

    def _submit(self, job: Job, text: str, origin: int | None) -> None:
        lane, job_id = self.runtime.submit(text, origin_hwnd=origin, source="voice")
        with self._lock:
            self._unsubmitted.pop(job.token, None)
            job.job_id, job.lane = job_id, lane
            early = self._jobs.get(job_id)
            if early is not None and early.source == "text":
                job.state = early.state  # the runtime said "started" before submit returned
            self._jobs[job_id] = job
        self._log("bridge", op="submitted", token=job.token, job_id=job_id, lane=lane)
        self._changed()
        self._job_event(job, "submitted", job.request)

    def _steer(self, job: Job, job_id: int, text: str) -> None:
        if not self.runtime.steer(job_id, text):
            job.pending_steers.append(text)

    def _cancel(self, job: Job) -> None:
        # Queued on the GUI thread after this job's _submit, so its id is known.
        job.cancel_requested = True
        if job.log is not None:
            job.log.add("the user", "asked to stop")
        if job.job_id is not None:
            how = self.runtime.cancel(job.job_id)
            self._log("bridge", op="cancelled", job_id=job.job_id, how=how)

    def _typed(self, lane: str, job_id: int, request: str) -> Job:
        """A job the bridge did not submit (typed in the overlay): tracked for progress questions."""
        job = Job(job_id=job_id, request=request, source="text", lane=lane, state=RUNNING,
                  log=JobLog(request=request), pacer=StepPacer(0))
        with self._lock:
            self._jobs[job_id] = job
        return job

    def _on_started(self, lane: str, job_id: int, request: str) -> None:
        job = self._job(job_id) or self._typed(lane, job_id, request)
        job.lane, job.state = lane, RUNNING
        if job.log is not None:
            job.log.add("started", f"on the {lane.replace('_', ' ')}")
        steers, job.pending_steers = job.pending_steers, []
        for text in steers:
            self._steer(job, job_id, text)
        self._log("bridge", op="state", job_id=job_id, lane=lane, state=RUNNING, source=job.source)
        self._changed()
        self._job_event(job, RUNNING, job.request)

    def _on_tool_called(self, lane: str, job_id: int, name: str, tool_input: dict) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.acting = _action_words(name, tool_input)
        step = step_of(name, tool_input)
        if step is None:
            return
        label, detail = step
        job.step = label
        self._job_event(job, "step", label + (f" ({detail})" if detail else ""))
        if job.log is not None:
            job.log.steps += 1
            job.log.add("step", label + (f" ({detail})" if detail else ""))
        if job.source == "voice" and job.pacer is not None and job.pacer.offer(label):
            self._post(job, "step", (label, detail))
            self._log("bridge", op="step", job_id=job_id, tool=name)

    def _on_tool_finished(self, lane: str, job_id: int, name: str, ok: bool, summary: str) -> None:
        job = self._job(job_id)
        from yuki.agent.tools import ACTION_TOOL_NAMES

        if name in ACTION_TOOL_NAMES:
            # What the thinker did on the desktop, for the mouth and the next hand-off (a list
            # of tool names is plumbing: which calls act, not what anything means).
            what = (job.acting if job is not None and job.acting else name)
            self.actions.add("the thinker", what, summary[:400], ok=bool(ok), verified=None if ok else False)
        if job is None or job.log is None or step_of(name, None) is None:
            return
        job.log.add("returned", f"{job.step or name}: {'ok' if ok else 'failed'}, {summary}")

    def _on_said(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.last_say = text
        if job.log is not None:
            job.log.add("said to the user", text)
        self._post(job, "say", text)

    def _on_asked(self, lane: str, job_id: int, question: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.state, job.question, job.lane = WAITING, question, lane
        if job.log is not None:
            job.log.add("asked the user", question)
        self._post(job, "ask", question)
        self._log("bridge", op="state", job_id=job_id, state=WAITING)
        self._changed()
        self._job_event(job, WAITING, question)

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
        if job.log is not None:
            job.log.add("finished" if kind == "done" else "stopped", text)
        self._finish(job, state, text)
        self._job_event(job, state, text)
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
        if job.log is not None:
            job.log.add("queued", "it needs Yuki's hands; waiting for the task before it")
        ahead = max(0, waiting - 1)
        self._post(job, "queued", "It needs Yuki's hands; queued behind the current task"
                   + (f" ({ahead} more ahead)" if ahead else "") + ".")
        self._log("bridge", op="handed_off", job_id=origin_id, new_id=new_id, waiting=waiting)
        self._changed()

    def _on_steer_unread(self, lane: str, job_id: int, texts: list) -> None:
        """Something the user added arrived after the job's last step: run it as its own request."""
        with self._lock:
            finished = next((j for j in reversed(self._finished) if j.job_id == job_id), None)
        if finished is None or finished.source != "voice":
            return
        follow = self._new_job("(added to the earlier task) " + " ".join(texts), with_queue=self._loop is not None)
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
                    if kind == "step":
                        continue
                    if kind in ("done", "failed"):
                        head, rest = condense(payload, max_sentences=self._max_sentences, max_chars=self._max_chars)
                        follow.rest = rest
                        body = result_payload(kind, head, more=bool(rest))
                    else:
                        body = {{"say": "yuki_says", "ask": "yuki_asks"}.get(kind, kind): payload}

                    async def deliver(run_llm: bool, stale: bool, body=body) -> None:
                        del stale
                        await announce(body, run_llm)

                    gate = self.gate
                    if gate is not None:
                        gate.enqueue(Item(job=follow.token, kind="final" if kind in ("done", "failed") else kind,
                                          deliver=deliver))
                    else:
                        await deliver(True, False)
                    if kind in ("done", "failed"):
                        return
            asyncio.run_coroutine_threadsafe(relay(), loop)


def _action_words(name: str, tool_input: dict | None) -> str:
    """One line naming a thinker's action from its input (plumbing; typed text is left out)."""
    parts = [name]
    for key in ("element", "x", "y", "dy", "dx", "keys", "key", "url", "app", "query", "hwnd", "expect_hwnd"):
        value = (tool_input or {}).get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={str(value)[:80]}")
    return " ".join(parts)


def _foreground_hwnd() -> int | None:
    """The window in front at the end of the user's turn (``origin_hwnd``, §7.5)."""
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        return hwnd or None
    except Exception:  # noqa: BLE001
        return None
