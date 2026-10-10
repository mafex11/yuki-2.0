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

from yuki.voice.expressive import strip_tags
from yuki.voice.mouth import (
    APP_NAMED_NOTE, CODE_SAID_NOTE, CORRECTION_MARK, REMEMBER_NOTE, SAVED_NOTE, SPOKEN_NOTE, WITHDRAWN_NOTE,
    brain_request, outcome_fields, result_payload, waiting_payload,
)
from yuki.voice.narrator import RANKS, Item, asks, condense, ends_sentence, machine_text, sentences
from yuki.voice.progress import JobLog, step_of

#: Job states.
QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED = (
    "queued", "running", "waiting_for_answer", "done", "failed", "cancelled")
OPEN_STATES = (QUEUED, RUNNING, WAITING)
#: A request pack built when the user's turn ended goes with that turn's hand-off only this soon after (s); the agent
#: itself reads the screen again when a pack is older than its own ``pack_max_age_s`` (20 s).
PACK_MAX_AGE_S = 15.0

#: Finished jobs kept in the table ("it finished a minute ago", "want the rest?").
KEEP_FINISHED = 6
#: A finished job still answers check_progress for this long.
RECENT_S = 180.0

#: What the mouth reads for a result the bridge said itself (the thinker's own finishing line).
RESULT_SAID_NOTE = ("Said aloud just now, word for word, the moment the action that did it succeeded. Do not "
                    "say it again or add to it; if they ask about it, answer from it.")
#: appTakeover (review F5): what the talker reads for a request that ended because nobody went on with it after the
#: user took over the keyboard or mouse (it waited paused, then stopped by itself; nothing else was done).
PAUSED_OUT_NOTE = ("Stopped by itself: they took over the keyboard or mouse while it worked, it waited paused, and "
                   "nobody went on with it for a long while, so nothing else was done. Say nothing about it; if they "
                   "ask, say that plainly.")


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
    #: The thinker's steps so far (tool calls that are steps): the number of the one running now, for its spoken
    #: line (v3.14, :mod:`yuki.voice.steplines`).
    step_seq: int = 0
    #: The thinker's own words for its latest step (``say_now``), and whether that step is running now (v3.14): what
    #: the talker's thinker line and a "still on it" line name, rather than the tool's generic label (live 09:08 on
    #: 28 Sept "Still on it, switching tabs now." came from the tab tool's label while it was closing a tab).
    step_line: str | None = None
    step_running: bool = False
    #: appPT: who a send of this job goes to while its send countdown runs (the island's "3 s to stop"), else
    #: ``None``: the talker's thinker line says so, so their "stop" in those seconds is taken for that send.
    sending: str | None = None
    #: appTakeover: paused because the user took over the keyboard or mouse (the talker's thinker line says so, so their
    #: "go on" or anything they add is for this task); ``paused_out``: it ended unresumed (quietly: nothing is said).
    paused: bool = False
    paused_out: bool = False
    #: Handed over with intent ``remember`` (a rule, correction or life fact to keep).
    remember: bool = False
    #: The desktop action it is running now, in a few words (for the shared action log).
    acting: str = ""
    #: The ``finishes_with`` of the action call it is running now (the brain's mark that the call completes
    #: the request), and that line once the call succeeded: the result it then ends with is said as it is,
    #: at once, by code (v3.6).
    finishing: str | None = None
    finished_by_action: str | None = None
    #: honest (87): the request's one outcome verdict (``yuki.agent.finish.outcome_verdict``), from the runtime's
    #: ``outcome`` signal just before its closing words: below likely, its words are never said as written.
    verdict: dict[str, Any] | None = None
    #: The tell_yuki call this job was started for early (the mouth's reply had just begun it; v3.6).
    early_call: str | None = None
    #: How many user turns the conversation had when this was handed over (v3.9): a result that arrives
    #: after the user said more is the outcome of an earlier request, never an answer to their newest words.
    user_turn: int = 0
    #: The conversation's turn it was handed over in (``<session>-<n>``, phase F): its request id in the logs is
    #: ``voice-<turn>-t<token>``, beside that turn's hands (``voice-<turn>-h<n>``), for ``python -m yuki.log.follow``.
    turn_id: str | None = None
    #: Withdrawn because a newer request of the user's replaced it (tell_yuki intent ``instead``): nothing of
    #: it is ever spoken, even an outcome that raced the withdrawal.
    superseded: bool = False
    #: N10 (report 47): the thinker's kept list for this request (its ``tasks`` call): ``[{"what", "state", "by"?}]``.
    tasks: list[dict[str, Any]] = field(default_factory=list)
    #: Started by code, not by a tell_yuki call: a hand of the talker's did not take effect and the thinker was
    #: handed the same request (v3.9). Its items reach the mouth through ``announce``.
    escalated: bool = False
    #: The talker judged it needs the PC's hands (tell_yuki ``acts``), or its own hand was tried first (v3.14b).
    acts: bool = False
    #: V4: the app or route the user named in their own words (tell_yuki ``app_named``), else ``None``.
    app_named: str | None = None
    #: Desktop actions of the job that succeeded (``ACTION_TOOL_NAMES``), from the runtime's tool signals.
    actions_ok: int = 0
    #: "Still on it" lines offered for it (v3.9: said only when the user has heard nothing for a while).
    waits: int = 0
    #: Sentences of its done message whose voice was fetched ahead (C2, v3.17).
    prefetched: set[str] = field(default_factory=set)
    #: E3a (v3.17): started by the router before the talker decided; ``adopted`` once the talker's tell_yuki took it.
    #: Until then it is invisible to the talker's view of its jobs and to the chat, and withdrawn unseen if not adopted.
    routed: bool = False
    adopted: bool = False
    deferred_events: list[tuple[str, str]] = field(default_factory=list)
    #: The user already heard an acknowledgement for it (its say_first, or words in the reply that handed it over):
    #: then nothing more is said until its outcome (v3.10, "acknowledge once, then stay quiet until the outcome").
    acked: bool = False
    wait_handle: Any = None

    @property
    def unadopted(self) -> bool:
        """Started by the router and not (yet) taken by a tell_yuki of the talker's."""
        return self.routed and not self.adopted

    def status(self, now: float) -> dict[str, Any]:
        return {
            "task": self.request if len(self.request) <= 240 else self.request[:239] + "…",
            "by": "voice" if self.source == "voice" else "typed",
            "state": self.state,
            "for_seconds": round(now - self.started_at),
            **({"current_step": self.step} if self.step and self.state == RUNNING else {}),
            **({"last_said": self.last_say} if self.last_say else {}),
            **({"asking": self.question} if self.question and self.state == WAITING else {}),
            **({"paused": "they took over the keyboard or mouse; it waits for them"} if self.paused
               and self.state == RUNNING else {}),
            **({"outcome": self.outcome} if self.outcome and self.state not in OPEN_STATES else {}),
            **(_checked(self.verdict) if self.outcome and self.state == DONE else {}),
        }

    def brief(self, limit: int = 90) -> str:
        """The request in a few words, for the talker's line about the thinker (content: never logged)."""
        text = " ".join(self.request.split("\n\n[", 1)[0].split())
        return text if len(text) <= limit else text[: limit - 1] + "…"


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


def spoken_words(context: Any, turns: int = 2) -> list[str]:
    """What Yuki's voice said most recently in a Pipecat ``LLMContext`` (the assistant's words), oldest first.

    Plumbing for the thinker (v3.9): it is told what the talker already said, so its own words do not repeat
    them ("On it" said by the voice, then "On it, playing it now" from the thinker).
    """
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else " ".join(
            p.get("text", "") for p in (content or []) if isinstance(p, dict) and p.get("type") == "text")
        text = " ".join(str(text or "").split())
        if text:
            out.append(text if len(text) <= 200 else text[:199] + "…")
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
        #: The profile a spoken request's first model call runs at (v3.14): what the thinker's cache is warmed for.
        self._voice_profile = (getattr(settings, "voice_brain_effort", None),
                               getattr(settings, "voice_brain_thinking", None))
        self._max_chars = int(getattr(settings, "voice_result_chars", 280))
        #: C1 (v3.17): a verified or short result is said by code, word for word, with no talker run.
        self._results_by_code = bool(getattr(settings, "voice_results_by_code", True))
        #: C2 (v3.17): the sentences of a thinker's done message are fetched by the voice as each one is written, so
        #: a result said by code plays at once.
        self._prefetch_results = bool(getattr(settings, "voice_prefetch_results", True))
        self._progress_lines = int(getattr(settings, "voice_progress_lines", 2))
        #: Seconds of silence (nobody speaking) before a running task's one "still on it" line, and before a
        #: second one (v3.9: intermediate chatter only when silence would be awkward).
        self._wait_line_s = float(getattr(settings, "voice_wait_line_s", 6.0))
        self._wait_again_s = float(getattr(settings, "voice_wait_again_s", 15.0))
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._unsubmitted: dict[int, Job] = {}
        #: Jobs started early for a tell_yuki the mouth is still writing, by its tool call id (v3.6).
        self._early: dict[str, Job] = {}
        #: When the brain's prompt cache was last warmed from here (monotonic).
        self._brain_warmed_at = 0.0
        #: A stay_quiet on the first words after the name was refused in this conversation already.
        self._named_refused = False
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
        #: The conversation's step-line voice (:class:`yuki.voice.steplines.StepLines`, v3.14): the thinker's step
        #: and say lines, said by code at the right moments; ``None`` without a conversation.
        self.steps: Any = None
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
        #: ``() -> "fresh" | "stale" | None``: how sure memory is of the call (v3.10; set by the conversation).
        self.call_evidence: Callable[[], str | None] | None = None
        #: ``(minutes)``: the user asked Yuki to go quiet for a while (``go_quiet``; set by the conversation).
        self.quiet_requested: Callable[[float], None] | None = None
        #: Whether the mouth run now answers the first words after the wake word heard the user say the name
        #: (set by the conversation; v3.6): those words were said to Yuki, so staying quiet is refused once.
        self.named_turn: Callable[[], bool] | None = None
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
        #: run it started is refused once, because a result or a question is always said (v3.5). With a ledger
        #: (v3.16) it is not refused: the ledger says the outcome itself.
        self.said_owed_at: float | None = None
        #: The conversation's owed-output ledger (:class:`yuki.voice.narrator.OwedLedger`, v3.16), set while one is
        #: live: every result, failure or question handed to the talker stays owed until it is heard.
        self.ledger: Any = None
        #: The "still on it" line last handed to the talker, and when (``waiting_reply_stale``, v3.14).
        self._waiting_run: tuple[Job, float] | None = None
        #: ``() -> int``: the conversation's user turns so far (set by the conversation; v3.9): binds each job to
        #: the request it answers, so a result arriving after the user said more is framed as the earlier one's.
        self.user_turns: Callable[[], int] | None = None
        #: ``() -> str``: the id of the conversation's turn now open (set by the conversation; phase F): a job's
        #: request id in the logs names it (:attr:`Job.turn_id`).
        self.turn_key: Callable[[], str] | None = None
        #: ``() -> float``: seconds since anyone spoke (Yuki or the user; 0 while someone is speaking), set by
        #: the conversation: a running task's "still on it" line waits for real silence.
        self.quiet_for: Callable[[], float] | None = None
        #: ``async (text)``: say these words now, by code (the thinker's own finishing line of a job started by
        #: code, with no tell_yuki call to carry it); set by the conversation.
        self.speak: Callable[[str], Awaitable[None]] | None = None
        #: ``async (sentences)``: say a result's sentences by code (C1, v3.17), each its own voice piece, never held by
        #: a blocking line check; set by the conversation (``None``: the call's own ``push_frame``, as in tests).
        self.speak_result: Callable[[list[str]], Awaitable[None]] | None = None
        #: ``(text) -> bool``: fetch the voice of these exact words now, to be played when they are said (C2, v3.17);
        #: set by the conversation when its voice can (Gemini), else ``None``.
        self.prefetch_speech: Callable[[str], bool] | None = None
        #: ``() -> (part, more left) | None``: the next part of the talker's own reply held back as too long (H2,
        #: v3.17), for more_of_result when no result has more; set by the conversation.
        self.more_held: Callable[[], tuple[str, bool] | None] | None = None
        #: When the thinker last closed a tab or window (monotonic), for the talker's guidance (v3.17).
        self.closed_at: float | None = None
        #: The thinker's request pack built when the user's turn ended (``Agent.build_pack``, v3.18, 07 §6 E2):
        #: ``(pack, built at, words)``; passed with this turn's hand-off only when the runtime and the agent take one.
        self._pack: tuple[Any, float, str] | None = None
        self._pack_ok: bool | None = None
        #: Whether the runtime takes the heard words apart (``submit(user_words=)``, 34-review H4); probed once.
        self._user_words_ok: bool | None = None
        #: Whether the runtime takes the talker's "only finds out" mark (``submit(needs_hands=)``, fx2); probed once.
        self._needs_hands_ok: bool | None = None
        self._pack_building = False
        self._pack_lean = not bool(getattr(settings, "voice_brain_portrait", True))
        #: When a call of the mouth's was held until the user answers (doubtful words; monotonic), set by the
        #: conversation: the run that report starts owes them words (a question, or the call again), never silence.
        self.held_at: float | None = None
        #: The held call itself, ``(name, arguments, context)``, and whether a stay_quiet on it was refused.
        self.held_call: tuple[str, dict, Any] | None = None
        self._held_refused = False
        #: ``() -> bool``: Yuki spoke in this conversation within the last moments (an exchange is live), set by
        #: the conversation; a stay_quiet on the user's words then is refused once (v3.9, never silent).
        self.in_exchange: Callable[[], bool] | None = None
        self._quiet_refused_turn = -1
        #: The user turn an acknowledgement was last said for (v3.12: one per request).
        self._acked_turn = -1
        #: The mouth's own one-step hands (docs/VOICE.md §5.6), over the brain's backend, and what
        #: they and the thinker did on the desktop (shared with the mouth and every hand-off).
        from yuki.voice.hands import ActionLog, Hands, hands_backend as _brain_backend

        self.actions = ActionLog()
        backend = hands_backend if hands_backend is not None else _brain_backend(runtime)
        self.hands: Hands | None = Hands(backend, log, self.actions) if backend is not None else None
        if self.hands is not None:
            self.hands.bridge = self
        self._gui_call.connect(self._run)
        from yuki.voice.workers import gui_poster

        self._poster = gui_poster()               # made here, on the GUI thread, never first on the loop
        self._gui_ident = threading.get_ident()   # the bridge is made on the GUI thread
        for name, slot in (
            ("started", self._on_started), ("tool_called", self._on_tool_called),
            ("tool_finished", self._on_tool_finished),
            ("said", self._on_said), ("asked", self._on_asked), ("finished", self._on_finished),
            ("failed", self._on_failed), ("withdrawn", self._on_withdrawn), ("lane_done", self._on_lane_done),
            ("handed_off", self._on_handed_off), ("steer_unread", self._on_steer_unread),
            ("partial", self._on_partial), ("resumed", self._on_resumed), ("send_pending", self._on_send_pending),
            ("paused_changed", self._on_paused_changed), ("outcome", self._on_outcome),
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
        self._named_refused = False
        if loop is None:
            self.gate = None
            self.steps = None
            self.progress_answerer = None
            self.spoke_in_reply = None
            self.on_remembered = None
            self.mark_spoken = None
            self.user_turn = None
            self.call_mode = None
            self.call_evidence = None
            self.quiet_requested = None
            self.named_turn = None
            self.said_early = None
            self.on_jobs_changed = None
            self.end_requested = None
            self.user_turns = None
            self.turn_key = None
            self.quiet_for = None
            self.speak = None
            self.speak_result = None
            self.prefetch_speech = None
            self.more_held = None
            self.in_exchange = None
            self.held_at = None
            self.held_call = None
            self.ledger = None
            self._pack = None
            self.busy_tools = 0
            with self._lock:
                for job in list(self._jobs.values()) + list(self._unsubmitted.values()):
                    job.events = None
                    if job.wait_handle is not None:
                        job.wait_handle.cancel()
                        job.wait_handle = None

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a bridge error must not kill the GUI
            self._log("bridge_error", error=f"{type(exc).__name__}: {exc}")

    def _gui(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (queued; returns at once, and never calls into Qt from the caller).

        v3.14b: the voice loop only puts ``fn`` on a queue; one emitter thread (``workers.gui_poster``, made with the
        bridge) hands it to Qt (a queued signal). Offline 29 Sept the loop stalled 1.6 s inside this emit (``begin_early``): an emit
        releases and re-takes the interpreter lock and takes Qt's post-event lock, either of which another thread
        can hold. The order of calls is kept (one queue, one thread).
        """
        if threading.get_ident() == self._gui_ident:
            self._gui_call.emit(fn)   # already on the GUI thread: Qt runs it as before (queued to itself)
            return
        self._poster.post(lambda: self._gui_call.emit(fn))

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
        if job.unadopted:
            job.deferred_events.append((state, summary))   # shown once the talker adopts it (E3a), never if not
            return
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

    def _owe(self, job: Job, kind: str, text: str) -> None:
        """v3.16: an outcome handed to the talker to be said now stays owed until it is heard (the ledger)."""
        ledger = self.ledger
        if ledger is None or job.remember:
            return
        try:
            turns = int(self.user_turns()) if self.user_turns is not None else 0
        except Exception:  # noqa: BLE001
            turns = 0
        try:
            try:
                ledger.owe(job.token, kind, text, turns, done_at=job.finished_at)
            except TypeError:   # a stand-in ledger without the v3.17 argument
                ledger.owe(job.token, kind, text, turns)
        except Exception as exc:  # noqa: BLE001 - the ledger never breaks the bridge
            self._log("bridge_error", error=f"owe: {type(exc).__name__}: {exc}")

    def _unowe(self, job: Job) -> None:
        if self.ledger is not None:
            try:
                self.ledger.drop(job.token)
            except Exception:  # noqa: BLE001
                pass

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
        return sum(1 for j in live if j.events is not None and j.state in (QUEUED, RUNNING) and not j.unadopted)

    def desk_job(self) -> str | None:
        """A few words on the job acting on the desktop now (running on the worker, the lane with hands), else
        ``None``: while one runs, the talker's window hands wait (one actor at a time; v3.5)."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) if j.state == RUNNING and j.lane == "worker"
                    and not j.unadopted and not j.superseded and not j.cancel_requested]
        if not live:
            return None
        job = max(live, key=lambda j: j.started_at)
        task = job.request if len(job.request) <= 80 else job.request[:79] + "…"
        return f"task \"{task}\"" + (f", now: {job.acting[:80]}" if job.acting else "")

    def work_line(self, now: float | None = None) -> str | None:
        """What the thinker is doing right now, in one line for the talker (v3.9), or ``None`` when idle.

        The running task and its current step (or the question it is waiting on), what is queued behind it,
        and what finished in the last minute with its outcome, each with its task number (``replaces`` takes
        it). State only, read from the job table; the talker answers "what's happening?" from it at once.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            live = [j for j in list(self._unsubmitted.values()) + list(self._jobs.values()) if j.state in OPEN_STATES
                    and not j.unadopted]
            done = [j for j in self._finished if j.finished_at and now - j.finished_at < 60.0 and not j.remember
                    and not j.unadopted]
        parts: list[str] = []
        for j in sorted(live, key=lambda j: j.started_at):
            name = f"task {j.job_id}" if j.job_id is not None else "a task"
            ago = round(now - j.started_at)
            if j.state == QUEUED:
                parts.append(f'{name} "{j.brief()}" queued, waiting for the one before it ({ago} s)')
            elif j.state == WAITING:
                parts.append(f'{name} "{j.brief()}" waiting for their answer to: {j.question or "its question"} '
                             f'(asked already, never again: words that are not its answer are a new request)')
            elif j.paused:
                # appTakeover: paused because they took over: their wish to go on, or anything they add to it, is for
                # this task (added to it with tell_yuki, it goes on from what the screen shows); their stop cancels it
                parts.append(f'{name} "{j.brief()}" paused for {ago} s: they took over the keyboard or mouse, so it '
                             f'waits for them (their wish to go on, or anything they add to it, is for this task: '
                             f'tell_yuki adds it and it goes on from what the screen shows now; their stop is '
                             f'cancel_tell_yuki; say nothing of the pause unless they ask)')
            else:
                step = (f", now: about to send it to {j.sending}, a few seconds left to stop it (their stop or no now "
                        f"is for this send: cancel_tell_yuki)" if j.sending else
                        f', now: "{j.step_line}"' if j.step_line and j.step_running else
                        f", now: {j.step.lower()}" if j.step else ", starting")
                left = _tasks_left(j.tasks)
                parts.append(f'{name} "{j.brief()}" running for {ago} s{step}'
                             + (f"; still to do on its list: {'; '.join(left)}" if left else ""))
        if done:
            j = done[-1]
            what = {DONE: "finished", FAILED: "failed", CANCELLED: "stopped"}.get(j.state, j.state)
            if j.paused_out:
                what = "stopped after waiting paused (they had taken over and nobody went on with it)"
            elif j.superseded:
                left = _tasks_left(j.tasks)
                what = "withdrawn for a newer request" + (
                    f" (its list still had: {'; '.join(left)}; say so if they did not mean to drop it)" if left else "")
            outcome = condense(j.outcome or "", max_sentences=1, max_chars=120)[0] if j.state == DONE else ""
            held = self._held_verdict(j, "done") if j.state == DONE else None
            if held is not None:
                # honest (87): its words are the thinker's claim; the check's level is what the talker may repeat
                what += f", {str(held.get('label') or '').lower()} ({held.get('seen') or 'nothing showed it'})"
            parts.append(f'task {j.job_id} "{j.brief()}" {what} {round(now - (j.finished_at or now))} s ago'
                         + (f': "{outcome}"' if outcome else ""))
        return "; ".join(parts) or None

    def has_open_voice_job(self) -> bool:
        """Whether a job of this conversation is open (queued, running or waiting for the user's answer)."""
        with self._lock:
            live = list(self._jobs.values()) + list(self._unsubmitted.values())
        return any(j.source == "voice" and j.state in OPEN_STATES and j.events is not None and not j.unadopted
                   for j in live)

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
        turns = 0
        if self.user_turns is not None:
            try:
                turns = int(self.user_turns())
            except Exception:  # noqa: BLE001
                turns = 0
        turn_id = None
        if self.turn_key is not None:
            try:
                turn_id = str(self.turn_key() or "") or None
            except Exception:  # noqa: BLE001
                turn_id = None
        return Job(job_id=None, request=request, source=source, token=next(self._tokens),
                   events=asyncio.Queue() if with_queue else None, log=JobLog(request=request), user_turn=turns,
                   turn_id=turn_id)

    # -- the mouth's tools (voice thread) ---------------------------------------

    async def tell_yuki(self, params: Any) -> None:
        """Pipecat handler for ``tell_yuki`` (async: registered with ``cancel_on_interruption=False``)."""
        args = params.arguments if isinstance(params.arguments, dict) else {}
        request = str(args.get("request") or args.get("message") or "").strip()
        context = str(args.get("context") or "").strip() or None
        corrects = str(args.get("corrects") or "").strip() or None
        # V4: the app or route the user named in their own words (the talker's structured field), else None
        app_named = " ".join(str(args.get("app_named") or "").split())[:80] or None
        intent = str(args.get("intent") or "new_task")
        heard = heard_words(params.context, skip=self.not_heard)
        request = request or (heard[-1] if heard else "")
        remember = intent == "remember"
        screen = args.get("screen") is True
        acts = args.get("acts") is True
        with self._lock:
            early = self._early.pop(str(params.tool_call_id), None)
        if early is None and intent in ("new_task", "remember") and self.adopt_routed(str(params.tool_call_id)):
            # E3a: the router already started the thinker on these words (the call's begin was not seen): adopted.
            with self._lock:
                early = self._early.pop(str(params.tool_call_id), None)
        replaced: list[Job] = []
        if intent == "instead":
            # The talker judged that this replaces work already handed over (a correction, the same target done
            # another way, a change of mind): that work is withdrawn now and this runs next, on the worker (v3.9).
            replaced = self._replace_targets(args.get("replaces"), besides=early)
            words = heard[-1] if heard else request   # N10: a withdrawal carries the user's own words
            for old in replaced:
                self._drop_items(old)
                self._gui(lambda old=old: self._withdraw(old, words=words))
            self._log("bridge", op="replace", call=params.tool_call_id, withdrawn=[j.job_id for j in replaced])
            intent = "new_task"
        if early is not None and intent == "follow_up" and self._follow_up_target(besides=early) is not None:
            # Started early as a new task, but it adds to a task that is open after all: that task gets it.
            self._gui(lambda: self._cancel(early))
            self._log("bridge", op="early_dropped", why="follow_up", call=params.tool_call_id)
            early = None
        if early is not None:
            await self._adopt(early, params, args, context=context, corrects=corrects, remember=remember,
                              app_named=app_named)
            return
        if intent == "new_task" and not remember and not replaced:
            same = self._same_turn_job()
            if same is not None and same.escalated:
                # v3.14: the talker's hand did not take effect and code already gave the thinker these very words
                # (``escalate``); the talker's own hand-off of them in the run that says "on it" adds nothing. Its
                # words are said, the call settles, and the running job is not steered with a copy of its request
                # (offline 29 Sept that copy held the job's done for one more round).
                await self._ack_if_silent(params, args)
                self._log("bridge", op="merged_escalated", job_id=same.job_id, call=params.tool_call_id)
                from pipecat.frames.frames import FunctionCallResultProperties

                await params.result_callback({"status": "already being done for these words"},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
            if same is not None:
                # One job per request (v3.10): a second hand-off for the same user turn (the reply's own second call,
                # or a re-run of the same words) is that request again, never a second job saying a second outcome.
                intent = "follow_up"
                self._log("bridge", op="merged_same_turn", job_id=same.job_id, call=params.tool_call_id)
        done_before = self.actions.text()
        said = spoken_words(params.context)
        first = str(args.get("say_first") or "").strip()
        if first and first not in said:
            said.append(first)
        tabs = self.tabs_for_thinker()
        text = brain_request(request, heard, context=context, remember=remember, done_before=done_before,
                             corrects=corrects, said=said, tabs=tabs, app_named=app_named)
        await self._ack_if_silent(params, args)
        if intent == "follow_up":
            target = self._follow_up_target()
            if target is not None:
                how = await self._follow_up(target, text)
                if app_named and how != "restart" and target.job_id is not None:
                    self._name_app(target, app_named)
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
                held = self._held_verdict(target, "done")   # honest (87): its words at their checked level
                graded = f" ({held.get('label')}: {held.get('seen')})" if held is not None else ""
                text = brain_request(request, heard, context="; ".join(x for x in (
                    context, f'this adds to the request just before it ("{target.request}"), which had just '
                             f'finished{graded}{": " + condense(prev, max_sentences=2, max_chars=300)[0] if prev else ""}')
                    if x), done_before=done_before, corrects=corrects, said=said, tabs=tabs, app_named=app_named)
        job = self._new_job(request)
        job.acked = self._acked(params, args)
        job.acts = acts
        if remember:
            # Saved quietly: no step lines, and its outcome folded in unless it failed.
            job.remember = True
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        # To the worker straight away when it needs the worker's hands (the talker's `acts`) or replaces work
        # being withdrawn: through the front desk it would only look, then be queued anyway a model call later.
        lane = "worker" if (replaced or acts) else None
        view = (origin or self._desk_foreground()) if (screen or (acts and self._just_used_desk())) else None
        if lane == "worker" and not remember:
            self._pass_over_questions(lane)   # a question they went past would hold this behind it for good
        words = _memory_words(request, heard)
        reads = args.get("acts") is False and not remember   # the talker's own mark: it only finds out or answers
        self._gui(lambda: self._submit(job, text, origin, lane=lane, view=view, memory_query=words,
                                       user_words=heard, needs_hands=False if reads else None, app_named=app_named))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent=intent,
                  request_chars=len(request), context_chars=len(context or ""), origin_hwnd=origin,
                  done_before_chars=len(done_before), heard_turns=len(heard), corrects_chars=len(corrects or ""),
                  said_lines=len(said), tabs_chars=len(tabs or ""), screen=screen, acts=acts,
                  replaced=len(replaced), lane=lane, app_named=bool(app_named))
        self._changed()
        self._arm_wait(job)
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
                self._unowe(job)
                self._log("bridge", op="cancel", job_id=job.job_id, by="mouth")
            raise

    def _acked(self, params: Any, args: dict[str, Any]) -> bool:
        """Whether the user hears an acknowledgement for this hand-off (its say_first, or the reply's words)."""
        if str(args.get("say_first") or "").strip():
            return True
        try:
            return bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        except Exception:  # noqa: BLE001
            return False

    def _same_turn_job(self) -> Job | None:
        """An open voice job handed over for the user's current turn (the same request), else ``None``."""
        if self.user_turns is None:
            return None
        try:
            turn = int(self.user_turns())
        except Exception:  # noqa: BLE001
            return None
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and not j.superseded and not j.remember
                    and j.user_turn == turn and turn > 0 and not j.unadopted]
        return max(live, key=lambda j: j.started_at) if live else None

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
        if not self.may_ack():
            self._log("bridge", op="ack_skipped", call=params.tool_call_id, why="already_acknowledged")
            return
        from pipecat.frames.frames import TTSSpeakFrame

        self.acked()
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
            if self._step_item(job, kind, payload):
                continue
            if kind == "say_line":
                kind, payload = "say", payload[1]   # no step-line voice (a test's stand-in): the talker says it
            if kind in ("done", "failed", "withdrawn") and self.steps is not None:
                self.steps.closed(job.token)
            self._log("mouth_tool_result", job_id=job.job_id, result=kind, final=kind in ("done", "failed"),
                      chars=len(str(payload)))
            if job.paused_out and kind in ("done", "failed", "withdrawn"):
                # appTakeover (review F5): it ended because nobody went on with it after the user took over: settled
                # quietly with what really happened (never "replaced by a newer request")
                self._drop_items(job)
                self._unowe(job)
                await params.result_callback({"stopped": job.brief(), "how_to_handle_it": PAUSED_OUT_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                self._log("bridge", op="paused_out_settled", job_id=job.job_id)
                return
            if kind == "withdrawn" or (kind in ("done", "failed") and job.superseded):
                # Replaced by the user's newer request: nothing of it is ever said. Its call settles quietly, so
                # the talker knows it stopped, and its waiting lines go with it.
                self._drop_items(job)
                self._unowe(job)
                await params.result_callback({"withdrawn": job.brief(), "how_to_handle_it": WITHDRAWN_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                self._log("bridge", op="withdrawn_settled", job_id=job.job_id, raced=kind != "withdrawn")
                return
            late = self._moved_on(job)
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
                # One voice (v3.14 "one voice that knows what it said"; v3.10 had "only the talker speaks"). C1
                # (v3.17, 07 §6 C): a verified finish or a short result is said by code, word for word, in Yuki's
                # same voice, and goes into the talker's conversation as its own words: no talker run (its first token
                # alone is ~1.6 s), no rewording. Longer results, questions, failures, late outcomes and "nothing was
                # done" still go to the talker, which says them once, merged with whatever it is saying. The narrator
                # releases a final with only a short gap, the moment the user may be spoken to.
                held = self._held_verdict(job, kind)   # honest (87): below likely, never said as written
                verified = bool(kind == "done" and job.finished_by_action
                                and payload.strip() == job.finished_by_action) and held is None
                head, rest = condense(payload, max_sentences=self._max_sentences, max_chars=self._max_chars)
                job.rest = rest
                delivered = loop.create_future()
                nothing = self._nothing_done(job) if kind == "done" else False
                lines = None if held is not None else self._by_code(job, kind, head, rest, late=late,
                                                                    verified=verified, nothing_done=nothing)
                if held is not None:
                    self._log("bridge", op="outcome_held", job_id=job.job_id, level=held.get("level"),
                              said_by="talker")
                push = getattr(getattr(params, "llm", None), "push_frame", None)
                if lines is not None and push is None and self.speak_result is None and self.speak is None:
                    lines = None   # nothing here can put words into the voice (a bare stand-in): the talker says it
                if lines is not None:
                    async def settle(body: dict[str, Any], run_llm: bool) -> None:
                        await params.result_callback(body, properties=FunctionCallResultProperties(run_llm=run_llm))

                    self._log("bridge", op="condensed", job_id=job.job_id, chars=len(payload), spoken_chars=len(head),
                              rest_chars=len(rest), late=late, verified=verified, said_by="code")
                    await self._offer(job, "final", self._said_final(job, kind, head, rest, lines, settle, delivered,
                                                                     push=push, verified=verified),
                                      self_spoken=True)
                    await delivered
                    return

                async def final(run_llm: bool, stale: bool, kind=kind, head=head, rest=rest,
                                delivered=delivered, late=late, verified=verified, nothing=nothing) -> None:
                    if not stale and kind == "done" and self._newer_request(job):
                        # A newer request of theirs is open or done: this outcome is not what they wait for now
                        # (live 13:30:37 "Add another account." was answered with "Accounts list is expanded."). It
                        # goes into the conversation unsaid; the screen shows it anyway (v3.12).
                        stale, run_llm = True, False
                        self._log("bridge", op="outcome_folded", job_id=job.job_id, why="newer_request")
                    if run_llm and not stale and not late:
                        # An owed result is always said; a late one (they said more since) may be let go.
                        self.said_owed_at = time.monotonic()
                    if run_llm and not stale:
                        self._outcome_said(job)
                    if not stale:
                        # v3.16: owed until heard, late or not (only the stale rule lets it go): the ledger says it
                        # by code if the run that should say it is silent (honest: below likely, the verdict's line)
                        self._owe(job, kind, self._owed_words(job, kind, head))
                    self._waiting_run = None   # this run says the outcome: never "stale" (waiting_reply_stale)
                    await params.result_callback(result_payload(kind, head, more=bool(rest), held_back=stale,
                                                                request=job.request, late=late, verified=verified,
                                                                nothing_done=nothing, outcome=job.verdict),
                                                 properties=FunctionCallResultProperties(run_llm=run_llm))
                    if not delivered.done():
                        delivered.set_result(stale)

                self._log("bridge", op="condensed", job_id=job.job_id, chars=len(payload), spoken_chars=len(head),
                          rest_chars=len(rest), late=late, verified=verified, said_by="talker")
                await self._offer(job, "final", final)
                await delivered
                return
            if kind == "waiting":
                body: dict[str, Any] = waiting_payload(job.brief(200), *payload)
            elif kind == "say":
                body = {"yuki_says": payload}
            elif kind == "ask":
                body = {"yuki_asks": payload}
            else:
                body = {"status": payload}

            async def intermediate(run_llm: bool, stale: bool, body=body, kind=kind, payload=payload) -> None:
                del stale
                if run_llm and kind == "ask":
                    self.said_owed_at = time.monotonic()
                if kind == "ask":
                    self._owe(job, "ask", str(payload))   # a question of the thinker's is owed too (v3.16)
                if run_llm:
                    self._waiting_run = (job, time.monotonic()) if kind == "waiting" else None
                await params.result_callback(body, properties=FunctionCallResultProperties(
                    is_final=False, run_llm=run_llm))

            await self._offer(job, kind, intermediate)

    def _step_item(self, job: Job, kind: str, payload: Any) -> bool:
        """The thinker's step lines, say lines and step boundaries (v3.14): to the step-line voice
        (:mod:`yuki.voice.steplines`), said by code in Yuki's one voice, never through a talker run. ``True``: taken.

        A job handed over to be kept (``remember``) says nothing of its steps. Without a step-line voice (a test's
        stand-in conversation) the step items are dropped and a say line goes to the talker as before.
        """
        if kind == "reply_partial":
            self._prefetch_reply(job, str(payload or ""))
            return True
        if kind == "moved":
            # The job took its next step: a "still on it" line still waiting names a step it is past (v3.6: live
            # 04:47:28 "Opening Bluetooth settings for you" was said after they had opened).
            if self.gate is not None:
                for item in self.gate.take(job.token, kinds=("waiting", "queued")):
                    if item.drop is not None:
                        item.drop()
                    self._log("narrator", op="dropped_passed", job=job.token, item=item.kind)
            return True
        if kind not in ("step_line", "step_start", "step_end", "say_line", "step_hold", "closing"):
            return False
        steps = self.steps
        if steps is None:
            return kind != "say_line"
        if job.remember and kind in ("step_line", "say_line"):
            return True
        if kind == "step_start":
            steps.step_started(job.token, int(payload))
        elif kind == "step_end":
            seq, ok = payload if isinstance(payload, tuple) else (payload, True)
            steps.step_ended(job.token, int(seq), ok=bool(ok))
        elif kind in ("step_line", "say_line"):
            seq, text = payload
            steps.offer(job.token, str(text), kind="step" if kind == "step_line" else "say", seq=int(seq))
        elif kind == "step_hold":
            steps.hold(job.token)
        else:
            steps.closed(job.token)
        return True

    def step_said(self, token: int, kind: str, text: str) -> None:
        """A step or say line of a job went to the voice: into its action log, for "how's it going?" (v3.14)."""
        with self._lock:
            job = next((j for j in list(self._jobs.values()) + list(self._unsubmitted.values()) if j.token == token),
                       None)
        if job is not None and job.log is not None:
            job.log.add("said to the user", text)
            job.last_say = text

    async def _offer(self, job: Job, kind: str, deliver: Callable[[bool, bool], Awaitable[None]],
                     drop: Callable[[], None] | None = None, *, self_spoken: bool = False) -> None:
        gate = self.gate
        if gate is None:
            await deliver(True, False)
            return
        gate.enqueue(Item(job=job.token, kind=kind, deliver=deliver, drop=drop, self_spoken=self_spoken))

    # -- the early hand-off (v3.6) ------------------------------------------------

    def begin_early(self, call_id: str, heard: list[str], screen: bool = False,
                    said: list[str] | None = None, *, routed: bool = False) -> bool:
        """The mouth's reply has just begun a tell_yuki: submit the request now, on the user's own words.

        On the voice loop, from the reply's stream (``Conversation._early_handoff``). The request is what the
        user said (the thinker is told their words win over the mouth's wording anyway); when the call is
        complete, :meth:`tell_yuki` adopts this job, adding the call's ``context`` / ``corrects`` if it has
        them. ``screen``: the talker's mark (the call's first field) that the request is about something inside
        the window in front: the thinker gets that window read as it starts (v3.9). Returns whether a job was
        submitted.

        ``routed`` (E3a, v3.17): started by the router before the talker decided (``call_id`` is the router's own key,
        :data:`ROUTE_KEY`); the talker's tell_yuki adopts it (:meth:`adopt_routed`), anything else withdraws it unseen.
        """
        heard = [h for h in (heard or []) if h and h.strip()]
        if not heard or self._loop is None:
            return False
        request = heard[-1].strip()
        done_before = self.actions.text()
        said = [x for x in (said or []) if x and x.strip()]
        tabs = self.tabs_for_thinker()
        text = brain_request(request, heard, done_before=done_before, said=said, tabs=tabs)
        job = self._new_job(request)
        job.early_call = str(call_id)
        job.routed = routed
        with self._lock:
            replaced = self._early.get(str(call_id))
        if replaced is not None:
            self.drop_early(str(call_id))   # never two jobs under one key: the older one is withdrawn first
        with self._lock:
            self._unsubmitted[job.token] = job
            self._early[str(call_id)] = job
        origin = self._foreground()
        view = (origin or self._desk_foreground()) if (screen or self._just_used_desk()) else None
        words = _memory_words(request, heard)
        self._gui(lambda: self._submit(job, text, origin, view=view, memory_query=words, user_words=heard))
        self._log("bridge", op="submit", token=job.token, call=call_id, intent="routed" if routed else "early",
                  request_chars=len(request), context_chars=0, origin_hwnd=origin, done_before_chars=len(done_before),
                  heard_turns=len(heard), early=True, screen=bool(screen), tabs_chars=len(tabs or ""))
        if not routed:
            self._changed()
            self._arm_wait(job)
        return True

    #: The key a routed job waits under in the early table until the talker's call adopts it (E3a, v3.17).
    ROUTE_KEY = "router"

    def routed_job(self) -> Job | None:
        """The job the router started that the talker has not adopted yet, or ``None``."""
        with self._lock:
            return self._early.get(self.ROUTE_KEY)

    def adopt_routed(self, call_id: str) -> bool:
        """The talker's reply has begun a tell_yuki: the job the router started for this turn is that hand-off's.

        It moves to the call's id in the early table (so :meth:`tell_yuki` adopts it like an early start), its events
        held back so far go to the chat, and its "still on it" timer starts. ``False``: there is none.
        """
        with self._lock:
            job = self._early.pop(self.ROUTE_KEY, None)
            if job is None:
                return False
            self._early[str(call_id)] = job
        job.early_call = str(call_id)
        job.adopted = True
        events, job.deferred_events = job.deferred_events, []
        for state, summary in events:
            self._job_event(job, state, summary)
        self._log("bridge", op="routed_adopted", token=job.token, job_id=job.job_id, call=call_id,
                  after_ms=round((time.monotonic() - job.started_at) * 1000))
        self._changed()
        self._arm_wait(job)
        return True

    def drop_early(self, call_id: str | None = None) -> None:
        """Cancel early jobs whose call was never completed (``call_id``: that one; ``None``: every one)."""
        with self._lock:
            ids = [call_id] if call_id is not None else list(self._early)
            dropped = [self._early.pop(i) for i in ids if i in self._early]
        for job in dropped:
            if job.unadopted:
                # E3a: the router's start the talker did not take (an answer, a hand, silence, or too slow): withdrawn,
                # never spoken, shown nowhere by the voice; marked now, so no hand of the talker's waits for it.
                job.superseded = job.cancel_requested = True
                self._gui(lambda job=job: self._withdraw(job))
            else:
                self._gui(lambda job=job: self._cancel(job))
            if self.gate is not None:
                for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                    if item.drop is not None:
                        item.drop()
            self._unowe(job)
            self._log("bridge", op="early_dropped", why="routed_not_adopted" if job.unadopted else "call_not_made",
                      call=job.early_call)

    def settle_early(self, made: set[str]) -> None:
        """The reply's calls are complete: an early job whose call is not among them is void."""
        with self._lock:
            void = [i for i in self._early if i not in made]
        for call_id in void:
            self.drop_early(call_id)

    async def _adopt(self, job: Job, params: Any, args: dict[str, Any], *, context: str | None,
                     corrects: str | None, remember: bool, app_named: str | None = None) -> None:
        """The tell_yuki call a job was started early for is complete: it carries that job's outcome."""
        job.acked = job.acked or self._acked(params, args)
        job.acts = job.acts or args.get("acts") is True
        if remember:
            job.remember = True
        extra = []
        if app_named:
            # V4: the app the user named reaches the early-started thinker as a note and, for its judge, as a field
            extra.append(APP_NAMED_NOTE.format(app=app_named))
            self._gui(lambda: self._name_app(job, app_named))
        if context:
            extra.append(f"[References in that request, as the conversation had them: {context}]")
        if corrects:
            extra.append(f"{CORRECTION_MARK} What was done, and what they said about it: {corrects} "
                         f"Do it the way they want, not the way it was done.]")
        if remember:
            extra.append(REMEMBER_NOTE.strip())
        if extra:
            # Only what the user's own words cannot say: the references the conversation resolves, a
            # correction, or that this is to be kept. Said as Yuki's voice's note, not as the user's words.
            note = "(From Yuki's voice, about the request you are on) " + " ".join(extra)
            self._gui(lambda: self._steer(job, job.job_id, note) if job.job_id is not None
                      else job.pending_steers.append(note))
        self._log("bridge", op="adopted", job_id=job.job_id, token=job.token, call=params.tool_call_id,
                  context_chars=len(context or ""), corrects_chars=len(corrects or ""), remember=remember,
                  app_named=bool(app_named))
        await self._ack_if_silent(params, args)
        try:
            await self._relay(job, params)
        except asyncio.CancelledError:
            if not self.closing:
                self._gui(lambda: self._cancel(job))
                if self.gate is not None:
                    for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                        if item.drop is not None:
                            item.drop()
                self._unowe(job)
                self._log("bridge", op="cancel", job_id=job.job_id, by="mouth")
            raise

    def warm_brain(self, idle_s: float = 240.0) -> bool:
        """Write the thinker's prompt cache now, off every thread that matters, when it has gone cold.

        Live 2026-09-27 04:47:18 the first thinker call of the conversation wrote its whole 27.8k-token
        prompt (the cache had expired 9 minutes after the last request): 4.2 s to its first step instead of
        ~2.5 s. At the start of a conversation, when the worker has not called the model for ``idle_s``,
        its own warm-up (``Agent.warm_cache``: one token out, the same system blocks and tools) runs on a thread. Costs a cache
        write of the core layers (~$0.03-0.05) only when cold; returns whether it started one.
        """
        worker = getattr(self.runtime, "worker", None)
        agent = getattr(worker, "agent", None)
        warm = getattr(agent, "warm_cache", None)
        if agent is None or not callable(warm):
            return False
        idle = float(getattr(agent, "idle_s", idle_s))
        if min(idle, time.monotonic() - self._brain_warmed_at) < idle_s or self.brain_busy():
            return False
        self._brain_warmed_at = time.monotonic()

        def run() -> None:
            started = time.monotonic()
            effort, thinking = self._voice_profile
            try:
                # v3.14: the voice profile's own entry (on Sonnet 5 each effort is its own cached prefix, measured).
                ok = warm(effort=effort, thinking=thinking) if (effort or thinking is not None) else warm()
            except TypeError:  # a stand-in agent without the arguments
                ok = warm()
            except Exception as exc:  # noqa: BLE001 - only an optimisation
                ok = f"{type(exc).__name__}"
            self._log("brain_warm", ms=round((time.monotonic() - started) * 1000), ok=ok)

        from yuki.voice.workers import shared

        shared().submit(run)   # an existing thread: this is called on the voice loop (v3.14)
        return True

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
        if self.spoke_in_reply is not None and self.spoke_in_reply():
            # The reply already answered in words (from the thinker line, v3.9): a second answer to one question
            # would follow it (measured offline: "Searching for a jazz mix." then "Opening a YouTube link for the
            # jazz mix."). A fact about the stream, never about the words. Settled quietly.
            from pipecat.frames.frames import FunctionCallResultProperties

            self._log("bridge", op="progress", refused="already_answered")
            await params.result_callback({"progress": None, "note": "You already answered them in this reply."},
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
        if job is None and self.more_held is not None:
            # H2 (v3.17): no result has more, but the talker's own last reply was held back as too long.
            try:
                got = self.more_held()
            except Exception:  # noqa: BLE001
                got = None
            if got:
                head, left = got
                self._log("bridge", op="more", found=True, held=True, spoken_chars=len(head), more=left)
                await params.result_callback({"more": head, "more_available": bool(left),
                                              "how_to_say_it": SPOKEN_NOTE + (" Still more after this." if left
                                                                              else "")})
                return
        if job is None:
            self._log("bridge", op="more", found=False)
            await params.result_callback({"more": None, "note": (
                "Nothing of the last result was held back. If they want more detail, that is a new "
                "question for the thinker: hand it over with tell_yuki in their words.")})
            return
        head, rest = condense(job.rest, max_sentences=self._max_sentences, max_chars=self._max_chars)
        job.rest = rest
        # second review V2-5: the rest of a done held below likely keeps its level and its note, as its first part did
        held = self._held_verdict(job, "done") if job.state == DONE else None
        graded, graded_note = outcome_fields(held) if held is not None else ({}, "")
        self._log("bridge", op="more", found=True, job_id=job.job_id, spoken_chars=len(head), rest_chars=len(rest),
                  **({"level": held.get("level")} if held is not None else {}))
        await params.result_callback({"more": head, **graded, "more_available": bool(rest),
                                      "how_to_say_it": (graded_note or SPOKEN_NOTE)
                                      + (" Still more after this." if rest else "")})

    async def stay_quiet(self, params: Any) -> None:
        """Pipecat handler for ``stay_quiet``: the mouth chose to say nothing; settled without a turn."""
        from pipecat.frames.frames import FunctionCallResultProperties

        by_user = self.user_turn() if self.user_turn is not None else None
        in_call = bool(self.call_mode()) if self.call_mode is not None else False
        owed, self.said_owed_at = self.said_owed_at, None
        spoke_now = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        ledger = self.ledger
        held_recently = self.held_at is not None and time.monotonic() - self.held_at < 20.0
        if not by_user and not spoke_now and ledger is not None and ledger.pending and not held_recently:
            # v3.16 (07 §6 A): staying quiet is not an option on a run that owes an outcome. Not refused (that cost a
            # second talker run, and live 2 Oct "Pages restored." still ended in silence after it): the run settles,
            # and the ledger says the outcome by code at once, in the thinker's own words.
            ledger.quiet_run()
            self._log("bridge", op="quiet", owed=True, said_by="ledger")
            await params.result_callback({"ok": True, "note": "Its outcome is said for you now."},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if not by_user and not spoke_now and owed is not None and time.monotonic() - owed < 20.0:
            # This run was started by a result or a question to be said (live 2026-09-26: a result met
            # with stay_quiet was never heard). Refused once; the mouth says it now. Not when this very reply
            # already said it aloud before the call (v3.14: offline 29 Sept the refusal made it say the same
            # outcome twice, "Nothing's playing right now..." and again a second later).
            self._log("bridge", op="quiet", refused="result_owed")
            await params.result_callback({"not_quiet": "A result, a failure or a question is always said. Say what it "
                                          "says now, in a few words of your own."})
            return
        # V2 (3 Oct): a stay_quiet on the user's own words is final. It used to be refused once after the name (v3.6)
        # and once in a live exchange (v3.9); live 3 Oct "go away" got a quiet, the refusal made the talker run again,
        # and it said it was still there. Whom the words were for is the talker's judgement (the prompt: outside a
        # call they are for Yuki unless plainly not), and code never makes it speak after it chose silence.
        spoke = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        held = self.held_at
        if not by_user and not spoke and held is not None and time.monotonic() - held < 20.0:
            # The run a held call's report started (the words were unsure and nothing was said or done): the user
            # is waiting for an answer. Offline 2026-09-27 this ended in silence three times. Refused once; a
            # second stay_quiet does what was held (a hand-off), rather than leave them with nothing.
            if not self._held_refused:
                self._held_refused = True
                self._log("bridge", op="quiet", refused="held")
                await params.result_callback({"not_quiet": "They are still waiting and heard nothing: ask your "
                                              "short question now, or call it again alone if their words were "
                                              "clear."})
                return
            call, self.held_call, self.held_at, self._held_refused = self.held_call, None, None, False
            if call is not None and call[0] == "tell_yuki" and await self._run_held(call):
                from pipecat.frames.frames import FunctionCallResultProperties

                await params.result_callback({"ok": True, "note": "What you had held is being done now."},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
        evidence = self.call_evidence() if self.call_evidence is not None else None
        # In a call, staying quiet on the user's own turn is the mouth's judgement that the words
        # were for the people on the call (logged as such; content-free).
        with_words = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else None
        # Why (content-free, v3.15): in call mode the talker judged the words were for the people on the call
        # (with the call's evidence); outside a call, its judgement that they were for someone or something else.
        why = ("talker_judged_for_call" if in_call else "talker_judged_not_for_yuki") if by_user else "no_user_turn"
        self._log("bridge", op="quiet", asked_by_user=by_user, with_words=with_words, why=why,
                  **({"call_mode": True, "addressed": "others" if by_user else None, "evidence": evidence}
                     if in_call else {}))
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

    async def end_conversation(self, params: Any) -> None:
        """Pipecat handler for ``end_conversation``: the user wants this voice conversation to stop now.

        Its say_first (a short goodbye) was spoken as it was written; the conversation ends once it has
        played. Nothing is saved: stopping a conversation is never a standing rule (live 2026-09-26, "you can
        turn off the voice then" was saved as a rule to answer in text only). Running tasks go on.
        """
        from pipecat.frames.frames import FunctionCallResultProperties

        args = params.arguments if isinstance(params.arguments, dict) else {}
        words = str(args.get("say_first") or "").strip()
        spoke = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        await self._ack_if_silent(params, args)
        # V1 (3 Oct): "shut up" / "go away" end with no goodbye at all (an empty say_first): nothing is waited for
        quick = not words and not spoke
        self._log("bridge", op="end_conversation", goodbye=bool(words or spoke))
        await params.result_callback({"ok": True, "note": "The conversation ends after your goodbye."},
                                     properties=FunctionCallResultProperties(run_llm=False))
        if self.end_requested is not None:
            try:
                self.end_requested(quick=quick)
            except TypeError:   # a stand-in that takes no arguments
                self.end_requested()

    async def go_quiet(self, params: Any) -> None:
        """Pipecat handler for ``go_quiet`` (v3.10): the user wants Yuki quiet for a while.

        Its say_first (a short okay) is said; the wake word and the voice stay off for ``minutes`` (the session keeps
        it across restarts and the tray can undo it), and this conversation ends after the okay. The user's words
        decide the length, through the talker's judgement; nothing here reads them.
        """
        from pipecat.frames.frames import FunctionCallResultProperties

        args = params.arguments if isinstance(params.arguments, dict) else {}
        try:
            minutes = float(args.get("minutes") or 60)
        except (TypeError, ValueError):
            minutes = 60.0
        minutes = max(1.0, min(minutes, 24 * 60.0))
        # v3.16 (04 F6): in a call, "stop listening" is off for the rest of that call (the talker's judgement, its
        # this_call field); the session restores listening when the call ends and never stores it.
        in_call = bool(self.call_mode()) if self.call_mode is not None else False
        this_call = bool(args.get("this_call") is True and in_call)
        await self._ack_if_silent(params, args)
        self._log("bridge", op="go_quiet", minutes=round(minutes), this_call=this_call)
        if self.quiet_requested is not None:
            try:
                try:
                    self.quiet_requested(minutes, this_call)
                except TypeError:   # a stand-in that takes the minutes only
                    self.quiet_requested(minutes)
            except Exception as exc:  # noqa: BLE001 - logged; the conversation still ends
                self._log("bridge_error", error=f"go_quiet: {type(exc).__name__}: {exc}")
        note = ("Not listening for the rest of this call, after your okay." if this_call
                else f"Quiet for {round(minutes)} minutes after your okay.")
        await params.result_callback({"ok": True, "note": note},
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

    def _follow_up_target(self, besides: Job | None = None) -> Job | None:
        """The most recent unfinished voice job, the one waiting for an answer first (``besides``: not that one)."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and j is not besides and not j.unadopted]
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

    def _submit(self, job: Job, text: str, origin: int | None, *, lane: str | None = None,
                view: int | None = None, memory_query: str | None = None,
                user_words: list[str] | None = None, needs_hands: bool | None = None,
                app_named: str | None = None) -> None:
        extra: dict[str, Any] = {}
        if lane:
            extra["lane"] = lane
        elif needs_hands is False and self._takes_needs_hands():
            # fx2 (revamp D 30b §2): the talker said it only finds out or answers (acts false): the reader lane takes
            # it even while the worker is idle (the runtime decides by who is free; a reader that needs hands hands
            # it to the worker)
            extra["needs_hands"] = False
        words = [" ".join(str(t).split()) for t in (user_words or []) if str(t).strip()]
        if words and self._takes_user_words():
            # 34-review H4: what speech recognition heard, apart from the request (the talker's reading of them,
            # which never authorizes a send); the agent labels them as the user's heard words for the judge. Plain
            # strings, the runtime's contract (V4, 3 Oct: passed as {"how", "text"} dicts, they reached the judge as
            # Python reprs).
            extra["user_words"] = list(words)
        app_named = app_named or job.app_named
        if app_named:
            job.app_named = app_named
            if self._takes("app_named"):
                # V4: the app or route the user named, as a field the thinker's frame and its judge read
                extra["app_named"] = app_named
        if view:
            extra["view_hwnd"] = view
        if job.turn_id and self._takes("log_request_id"):
            # phase F (fPrivacy R1.3): the request's id in every log, next to its turn's hands (probed)
            extra["log_request_id"] = f"voice-{job.turn_id}-t{job.token}"
        if memory_query:
            # 22 §4 (v3.17): memory is searched with the user's own words (``memory_words``), passed explicitly, so it
            # never depends on the hand-off's format.
            extra["memory_query"] = memory_query
        pack = self._take_pack()
        if pack is not None:
            # v3.18 (07 §6 E2): what the thinker's first call needs (Agent.build_pack), built while the user's turn
            # ended, handed over with the request so it is not read again (only when the runtime and the agent take it)
            extra["pack"] = pack
        try:
            lane, job_id = self.runtime.submit(text, origin_hwnd=origin, source="voice", **extra)
        except TypeError:
            if "memory_query" not in extra:
                raise
            extra.pop("memory_query")   # a stand-in runtime without the argument: the agent takes them from the text
            lane, job_id = self.runtime.submit(text, origin_hwnd=origin, source="voice", **extra)
        if "pack" in extra:
            age = getattr(extra["pack"], "age_s", None)
            self._log("bridge", op="pack", token=job.token, age_s=round(age, 2) if isinstance(age, float) else None,
                      build_ms=getattr(extra["pack"], "build_ms", None))
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

    def _takes_needs_hands(self) -> bool:
        """Whether ``runtime.submit`` names ``needs_hands`` (fx2, the reader lane; probed once, never assumed)."""
        if self._needs_hands_ok is None:
            self._needs_hands_ok = _takes(getattr(self.runtime, "submit", None), "needs_hands")
            self._log("bridge", op="needs_hands_probe", supported=self._needs_hands_ok)
        return bool(self._needs_hands_ok)

    def _takes(self, name: str) -> bool:
        """Whether ``runtime.submit`` names the parameter ``name`` (probed once per name, never assumed)."""
        cache = self.__dict__.setdefault("_submit_params", {})
        if name not in cache:
            cache[name] = _takes(getattr(self.runtime, "submit", None), name)
            self._log("bridge", op="submit_probe", param=name, supported=cache[name])
        return bool(cache[name])

    def _name_app(self, job: Job, app: str) -> None:
        """On the GUI thread: the app the user named, for a job already submitted (its thinker started early, or a
        follow-up steered into it): the runtime gives it to that request's agent for its judge (V4). Probed."""
        name = getattr(self.runtime, "name_app", None)
        if job.job_id is None or not callable(name):
            job.app_named = app   # given with the submit if it has not happened yet
            return
        try:
            name(job.job_id, app)
        except Exception as exc:  # noqa: BLE001 - the note in the request still says it
            self._log("bridge_error", error=f"name_app: {type(exc).__name__}: {exc}")

    def _takes_user_words(self) -> bool:
        """Whether ``runtime.submit`` names a ``user_words`` parameter (34-review H4; probed once, never assumed)."""
        if self._user_words_ok is None:
            self._user_words_ok = _takes(getattr(self.runtime, "submit", None), "user_words")
            self._log("bridge", op="user_words_probe", supported=self._user_words_ok)
        return bool(self._user_words_ok)

    def _pack_agent(self) -> Any:
        """The worker's agent when the thinker takes a request pack (v3.18, revamp D S5): ``runtime.submit`` names a
        ``pack`` parameter, and the agent has ``build_pack`` and its ``run`` names ``pack`` (probed once, never
        assumed); else ``None``."""
        if self._pack_ok is None:
            agent = getattr(getattr(self.runtime, "worker", None), "agent", None)
            self._pack_ok = bool(_takes(getattr(self.runtime, "submit", None), "pack") and agent is not None
                                 and callable(getattr(agent, "build_pack", None))
                                 and _takes(getattr(agent, "run", None), "pack"))
            self._log("bridge", op="pack_probe", supported=self._pack_ok)
        return getattr(getattr(self.runtime, "worker", None), "agent", None) if self._pack_ok else None

    def prefetch_pack(self, words: str) -> None:
        """The user's turn just ended with ``words``: build the thinker's request pack now, off every loop, so a
        hand-off of this turn starts its first model call with the screen, the origin and memory's part already read
        (``Agent.build_pack``; 07 §6 E2: ~0.4 s of the thinker's start). Only when the thinker takes a pack and its
        worker is idle (a pack is built by the thread-safe reads of an idle agent); one at a time; returns at once."""
        agent = self._pack_agent()
        text = " ".join(str(words or "").split())
        if agent is None or not text or self._pack_building:
            return
        busy = getattr(self.runtime, "worker_busy", None)
        if callable(busy) and busy():
            return
        if self.call_mode is not None and self.call_mode():
            return   # in a call most words are for the others: no reads for a hand-off that rarely comes
        self._pack_building = True
        origin = self._foreground()
        lean = self._pack_lean

        def build() -> None:
            try:
                pack = agent.build_pack(text, origin_hwnd=origin, memory_query=text, voice=True, lean=lean)
                with self._lock:
                    self._pack = (pack, time.monotonic(), text)
            except Exception as exc:  # noqa: BLE001 - the agent reads what it needs itself
                self._log("bridge", op="pack_build", error=type(exc).__name__)
            finally:
                self._pack_building = False

        from yuki.voice.workers import shared

        shared().submit(build)

    def _take_pack(self) -> Any:
        """The pack built for this turn, once (``None`` when there is none, it is older than :data:`PACK_MAX_AGE_S`,
        or the thinker takes none)."""
        with self._lock:
            got, self._pack = self._pack, None
        if got is None or self._pack_agent() is None:
            return None
        pack, at, _words = got
        return pack if time.monotonic() - at <= PACK_MAX_AGE_S else None

    def _steer(self, job: Job, job_id: int, text: str) -> None:
        if not self.runtime.steer(job_id, text):
            job.pending_steers.append(text)

    def _cancel(self, job: Job) -> None:
        # Queued on the GUI thread after this job's _submit, so its id is known.
        job.cancel_requested = True
        if job.log is not None:
            job.log.add("the user", "asked to stop")
        if job.job_id is not None:
            cancel = self.runtime.cancel   # a stop by the voice: recorded as such, it ends as "Stopped"
            how = cancel(job.job_id, by="voice") if _takes(cancel, "by") else cancel(job.job_id)
            self._log("bridge", op="cancelled", job_id=job.job_id, how=how)

    def _withdraw(self, job: Job, words: str | None = None) -> None:
        """On the GUI thread: stop a job the user's newer request replaces (queued after its ``_submit``). N10: the
        user's words that replaced it go with it (the runtime's log and the withdrawn line), and what its kept list
        still had is logged and stays on the thinker line, so nothing is dropped silently."""
        job.superseded = True
        job.cancel_requested = True
        if job.log is not None:
            job.log.add("the user", "replaced it with a newer request" + (f": \"{words}\"" if words else ""))
        if job.job_id is not None:
            withdraw = getattr(self.runtime, "withdraw", None)
            if callable(withdraw):
                try:
                    how = withdraw(job.job_id, words=words)
                except TypeError:   # an older runtime without the words
                    how = withdraw(job.job_id)
            else:
                how = self.runtime.cancel(job.job_id)
            self._log("bridge", op="withdrawn", job_id=job.job_id, how=how, left=len(_tasks_left(job.tasks)))

    def _typed(self, lane: str, job_id: int, request: str) -> Job:
        """A job the bridge did not submit (typed in the overlay): tracked for progress questions."""
        job = Job(job_id=job_id, request=request, source="text", lane=lane, state=RUNNING,
                  log=JobLog(request=request))
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

    def _on_partial(self, lane: str, job_id: int, what: str, text: str) -> None:
        """A step line the thinker is writing, complete while the rest of its call still streams (v3.14); or its done
        message so far (``reply``): its finished sentences are fetched by the voice ahead (C2, v3.17)."""
        if what not in ("step", "reply"):
            return
        job = self._job(job_id)
        if job is None or job.source != "voice" or job.remember:
            return
        if what == "reply":
            if self._prefetch_results and self._results_by_code and not job.superseded and not job.cancel_requested:
                self._post(job, "reply_partial", text)
            return
        job.step_line = text
        self._post(job, "step_line", (job.step_seq + 1, text))

    def _prefetch_reply(self, job: Job, text: str) -> None:
        """C2 (v3.17, 07 §6 C): the thinker's done message so far; each sentence it has finished is fetched by the voice
        now, so that, if the result is said by code (C1), its audio is ready the moment the done is accepted.

        Nothing is heard here: the words play only once the thinker's own checks have accepted its done (a done held
        back for a claim with no evidence is rewritten, and its fetched voice is simply let go). Fetched only while the
        result can still be one said by code: not late, not for a request that asked for an action none of which worked
        yet, within the condense rule, no closing question mark and no address or path (the same facts as
        :meth:`_by_code`). The last sentence is complete only when the done is, so it is fetched when said.
        """
        fetch = self.prefetch_speech
        if fetch is None or self._moved_on(job) or (job.acts and job.actions_ok == 0):
            return
        parts = sentences(text)
        # The sentences followed by more text are finished; the last one too once it ends with its own mark (the rest
        # of the done's JSON takes ~150 ms more): a one-sentence result is fetched before the done is accepted.
        done = parts if parts and ends_sentence(text) else parts[:-1]
        if not done:
            return
        if len(done) > self._max_sentences or len(" ".join(done)) > self._max_chars or asks(" ".join(done)) \
                or machine_text(" ".join(done)):
            return
        seen = job.prefetched
        for sentence in done:
            if sentence in seen:
                continue
            seen.add(sentence)
            try:
                started = bool(fetch(sentence))
            except Exception:  # noqa: BLE001 - only a head start
                started = False
            self._log("bridge", op="prefetch", job_id=job.job_id, chars=len(sentence), started=started)

    def _on_send_pending(self, lane: str, job_id: int, send: dict) -> None:
        """appPT: a send of the job counts down (``counting``: who it goes to is on the thinker line, so the user's
        "stop" in those seconds is taken for it) or ended (going, stopped, swapped: off the line again)."""
        job = self._job(job_id)
        if job is None:
            return
        send = send if isinstance(send, dict) else {}
        job.sending = (" ".join(str(send.get("who") or "").split()) or "them") \
            if send.get("state") == "counting" else None
        self._log("bridge", op="send_countdown", job_id=job_id, state=send.get("state"))
        self._changed()

    def _on_paused_changed(self, lane: str, job_id: int, state: str, why: str) -> None:
        """appTakeover: the job paused because the user took over (``paused``), goes on (``resumed``), ended
        unresumed (``ended``), or ended while still paused (``closed``, small8). Nothing is said for any of them (Yuki never speaks first): a step or "still on it" line
        still waiting is dropped, none comes while it is paused, and an unresumed end is quiet."""
        job = self._job(job_id)
        if job is None:
            return
        if state == "paused":
            job.paused = True
            self._post(job, "moved", None)        # a "still on it" line still waiting goes unsaid
            self._post(job, "step_hold", None)    # and so does a step line
            if job.log is not None:
                job.log.add("paused", "the user took over the keyboard or mouse; it waits for them")
        elif state == "resumed":
            job.paused = False
            if job.log is not None:
                job.log.add("resumed", "it read the screen again and goes on")
        elif state == "closed":
            # small8: the request ended while still paused (it finished anyway, or was stopped): no longer paused; its
            # end is told as it really happened (its reply, or the stop), never as "nobody went on with it"
            job.paused = False
        else:
            job.paused, job.paused_out = False, True
            job.superseded = True                 # nothing of its end is spoken
            if job.log is not None:
                job.log.add("stopped", "nobody went on with it after the user took over")
        self._log("bridge", op="takeover", job_id=job_id, state=state, why=why or None)
        self._changed()

    def _on_tool_called(self, lane: str, job_id: int, name: str, tool_input: dict) -> None:
        from yuki.agent.tools import ACTION_TOOL_NAMES, FINISHES_FIELD, STEP_FIELD

        job = self._job(job_id)
        if (tool_input or {}).get("action") == "close":
            self.closed_at = time.monotonic()   # v3.17: the talker gets its guidance on getting a closed tab back
        if job is None:
            return
        if name == "tasks" and isinstance((tool_input or {}).get("items"), list):
            # N10: the kept list rides on the thinker line (what is left), so an interjection never loses it
            job.tasks = [dict(i) for i in tool_input["items"] if isinstance(i, dict)][:12]
            self._changed()
            return
        job.acting = _action_words(name, tool_input)
        step = step_of(name, tool_input)
        if step is None:
            return
        acts = name in ACTION_TOOL_NAMES
        finishing = (tool_input or {}).get(FINISHES_FIELD) if acts else None
        job.finishing = finishing.strip() if isinstance(finishing, str) and finishing.strip() else None
        label, detail = step
        if job.source == "voice" and job.step is not None and label != job.step:
            self._post(job, "moved", name)  # a "still on it" line still waiting is about an earlier step now
        job.step_seq += 1
        job.step_running = True
        line = (tool_input or {}).get(STEP_FIELD)
        job.step_line = line.strip() if isinstance(line, str) and line.strip() else None
        if job.source == "voice":
            # v3.14: the step starts now; its line (streamed ahead, or with the finished call) may be said while it
            # runs, when a pause would be noticeable (yuki.voice.steplines).
            self._post(job, "step_start", job.step_seq)
            if job.step_line:
                self._post(job, "step_line", (job.step_seq, job.step_line))
        job.step = label
        self._job_event(job, "step", label + (f" ({detail})" if detail else ""))
        if job.log is not None:
            job.log.steps += 1
            job.log.add("step", label + (f" ({detail})" if detail else ""))
        # v3.9 dropped the per-step progress line (a talker run after the step: stale). v3.14 says the thinker's own
        # step line by code while the step runs (yuki.voice.steplines); the step stays in the job table for the
        # talker's thinker line.
        del acts

    def _on_tool_finished(self, lane: str, job_id: int, name: str, ok: bool, summary: str) -> None:
        job = self._job(job_id)
        from yuki.agent.tools import ACTION_TOOL_NAMES

        if name in ACTION_TOOL_NAMES:
            # What the thinker did on the desktop, for the mouth and the next hand-off (a list
            # of tool names is plumbing: which calls act, not what anything means).
            what = (job.acting if job is not None and job.acting else name)
            self.actions.add("the thinker", what, summary[:400], ok=bool(ok), verified=None if ok else False)
            if job is not None:
                job.finished_by_action = job.finishing if ok else None
                job.finishing = None
                job.actions_ok += 1 if ok else 0
        if job is None or job.log is None or step_of(name, None) is None:
            return
        job.step_running = False
        if job.source == "voice":
            # v3.14: a line about this step not said yet is past if the step failed; after a step that worked it may
            # still be said while the silence after it runs long (yuki.voice.steplines).
            self._post(job, "step_end", (job.step_seq, bool(ok)))
        job.log.add("returned", f"{job.step or name}: {'ok' if ok else 'failed'}, {summary}")

    def _on_said(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        # v3.14: the thinker's say line is said by code in Yuki's one voice when the user may be spoken to (never a
        # talker run), and goes into its log once said (``step_said``).
        self._post(job, "say_line", (job.step_seq, text))

    def _on_asked(self, lane: str, job_id: int, question: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        job.state, job.question, job.lane = WAITING, question, lane
        if job.log is not None:
            job.log.add("asked the user", question)
        self._post(job, "step_hold", None)   # its question is what is said now, never a step line over it
        self._post(job, "ask", question)
        self._log("bridge", op="state", job_id=job_id, state=WAITING)
        self._changed()
        self._job_event(job, WAITING, question)

    def _on_outcome(self, lane: str, job_id: int, verdict: dict) -> None:
        """honest (87): the request's outcome verdict, kept on its job: its words are said at that level, never more."""
        job = self._job(job_id)
        if job is None or not isinstance(verdict, dict):
            return
        job.verdict = dict(verdict)
        if job.log is not None and verdict.get("label"):
            job.log.add("checked", f"{verdict.get('label')}: {verdict.get('seen') or ''}".rstrip(": "))
        if verdict.get("acted") and verdict.get("label"):
            # review (items not done): the thinker's steps reach the shared action ledger as "not verified" one by one
            # (a step's level is not on its event); the request's one verdict closes them, so the talker's desktop
            # block and the next hand-off read the checked level, never only the step marks
            level = str(verdict.get("level") or "")
            self.actions.add("the thinker", f'the request "{job.brief()}", as a whole',
                             f"{verdict.get('label')}: {verdict.get('seen') or ''}".rstrip(": "),
                             ok=level != "failed", verified=True if level == "verified" else False if level == "failed"
                             else None)
        self._log("bridge", op="outcome", job_id=job_id, level=verdict.get("level"), acted=verdict.get("acted"),
                  send=verdict.get("send"))

    def _held_verdict(self, job: Job, kind: str) -> dict[str, Any] | None:
        """honest (87): the verdict of a done whose words may not be said as written (below likely), else ``None``."""
        from yuki.agent.finish import sayable

        if kind != "done" or sayable(job.verdict):
            return None
        return job.verdict

    def _owed_words(self, job: Job, kind: str, head: str) -> str:
        """What the owed ledger says by code if the talker's run for a result is not heard: the thinker's words, or for
        an outcome below likely the verdict's own fixed line (``I tried to send it, but I couldn't confirm it went.``),
        never the thinker's claim (honest, report 87)."""
        from yuki.agent.finish import LINE_NOT_CHECKED, LINE_NOTHING_DONE

        held = self._held_verdict(job, kind)
        line = " ".join(str((held or {}).get("line") or "").split())
        if held is None or not line:
            return head
        if held.get("acted"):
            return line
        if head.strip() in (LINE_NOTHING_DONE, LINE_NOT_CHECKED):
            return head   # fix6: the thinker's claim was already replaced by its own fixed line
        return f"{head} {line}"   # an unread answer keeps its words, marked

    def _on_finished(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is not None:
            job.closing = ("done", text)

    def _on_failed(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is not None:
            job.closing = ("failed", text)

    def _on_withdrawn(self, lane: str, job_id: int, text: str) -> None:
        job = self._job(job_id)
        if job is not None:
            job.superseded = True
            job.closing = ("withdrawn", text)

    def _on_lane_done(self, lane: str, job_id: int, request: str, needed_hands: bool, refused: list) -> None:
        job = self._job(job_id)
        if job is None:
            return
        if needed_hands:
            return  # the front desk passed it to the worker: handed_off follows with the new id
        kind, text = job.closing or ("failed", "It stopped without saying anything.")
        state = DONE if kind == "done" else (CANCELLED if (job.cancel_requested or kind == "withdrawn") else FAILED)
        if job.log is not None:
            job.log.add("finished" if kind == "done" else "stopped", text)
        if job.wait_handle is not None:
            job.wait_handle.cancel()
            job.wait_handle = None
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
        if job.superseded:
            # Replaced by a newer request while the front desk looked at it: its hand-off never runs.
            self._gui(lambda: self._withdraw(job))
            return
        if job.log is not None:
            job.log.add("queued", "it needs Yuki's hands; waiting for the task before it")
        ahead = max(0, waiting - 1)
        self._post(job, "queued", "It needs Yuki's hands; queued behind the current task"
                   + (f" ({ahead} more ahead)" if ahead else "") + ".")
        self._log("bridge", op="handed_off", job_id=origin_id, new_id=new_id, waiting=waiting)
        self._changed()

    def _on_resumed(self, origin_id: int, new_id: int) -> None:
        """A question the lane set aside was answered: the job carries on under the new id (v3.14b, runtime)."""
        with self._lock:
            job = self._jobs.pop(origin_id, None)
            if job is not None:
                job.job_id, job.state, job.question, job.closing = new_id, QUEUED, None, None
                self._jobs[new_id] = job
        if job is not None:
            self._log("bridge", op="resumed", job_id=origin_id, new_id=new_id)
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
                     None, user_words=[str(t) for t in texts])
        self._log("bridge", op="late_follow_up", job_id=job_id, new_id=follow.job_id)
        loop = self._loop
        if loop is not None and self.announce is not None and follow.events is not None:
            asyncio.run_coroutine_threadsafe(self._relay_detached(follow), loop)

    # -- v3.9: binding, waiting lines, escalation, replacement ---------------------------------------------

    def _newer_request(self, job: Job) -> bool:
        """Whether the user asked for something newer (a voice job handed over after this one's words) that is open or
        just finished: this one's outcome is then not what they are waiting for."""
        now = time.monotonic()
        with self._lock:
            others = list(self._jobs.values()) + list(self._unsubmitted.values()) + list(self._finished)
        return any(o is not job and o.source == "voice" and not o.remember and o.user_turn > job.user_turn
                   and not o.unadopted
                   and (o.state in OPEN_STATES or (o.finished_at and now - o.finished_at < RECENT_S))
                   for o in others)

    def _answer_on_its_way(self) -> bool:
        """A hand is acting or a task is running for them now (not one waiting for their answer): its outcome will
        be said, so their "Yeah." meanwhile needs no words of its own (v3.13: live 01:26:55 "Yeah." after "Open
        Warp?" got its stay_quiet refused while the hand was opening Warp: "Warp is open. Warp's up.")."""
        if self.busy_tools > 0:
            return True
        with self._lock:
            jobs = list(self._jobs.values())
        if any(j.state == WAITING for j in jobs):
            return False   # a question of Yuki's is open: their words may be its answer
        return any(j.source == "voice" and not j.remember and j.state in (QUEUED, RUNNING) and not j.unadopted
                   for j in jobs)

    def _outcome_said(self, job: Job) -> None:
        """The outcome of what they asked last is being said: no acknowledgement may come after it (v3.13)."""
        try:
            turn = int(self.user_turns()) if self.user_turns is not None else -1
        except Exception:  # noqa: BLE001
            return
        if turn >= 0 and job.user_turn == turn:
            self._acked_turn = turn

    # -- one acknowledgement per request (v3.12) --------------------------------------------------

    def may_ack(self) -> bool:
        """Whether an acknowledgement may still be said for the user's current words (one per request: live 13:31:31
        "My bad, opening DevMafex now. Opening it. Let me open that for you." were three for one)."""
        try:
            turn = int(self.user_turns()) if self.user_turns is not None else -1
        except Exception:  # noqa: BLE001
            turn = -1
        return turn < 0 or turn != self._acked_turn

    def acked(self) -> None:
        """Something was said for the user's current words (an acknowledgement or the reply's own words)."""
        try:
            self._acked_turn = int(self.user_turns()) if self.user_turns is not None else -1
        except Exception:  # noqa: BLE001
            pass

    def _moved_on(self, job: Job) -> bool:
        """Whether the user has said more since this job was handed over (its result is then a late one)."""
        if self.user_turns is None:
            return False
        try:
            return int(self.user_turns()) > int(job.user_turn)
        except Exception:  # noqa: BLE001
            return False

    #: How long after a "still on it" line was handed to the talker its reply may still be on its way (s).
    WAITING_RUN_S = 10.0

    def waiting_reply_stale(self) -> bool:
        """Whether the talker's reply about to be heard was asked for by a "still on it" line whose job has finished
        since (v3.14). The line goes to the talker the moment the silence is long; its reply takes ~2 s to be heard,
        and offline 29 Sept (warp929) the job's outcome landed in that gap: "Still looking for it." was said after the
        tab had switched, then "There you go." Asked by the voice right before a reply's first audio would play (the
        gate holds the outcome until then: words on their way count as Yuki speaking), once per line handed over;
        never for a reply to the user's own words. Plumbing on job states and times only."""
        run, self._waiting_run = self._waiting_run, None   # asked once: the first reply heard after it is that one
        if run is None:
            return False
        job, at = run
        if time.monotonic() - at > self.WAITING_RUN_S:
            return False
        try:
            if self.user_turn is not None and self.user_turn():
                return False   # the user spoke since: this reply answers them
        except Exception:  # noqa: BLE001
            return False
        return job.closing is not None or job.state not in OPEN_STATES

    # -- C1 (v3.17): results by code ----------------------------------------------------------------------------

    def _by_code(self, job: Job, kind: str, head: str, rest: str, *, late: bool, verified: bool,
                 nothing_done: bool) -> list[str] | None:
        """The sentences of a result to say by code, word for word, with no talker run, or ``None`` (the talker says it).

        07 §6 C: a verified ``finishes_with`` result, or a ``done`` that fits the condense rule whole (``voice_result_
        sentences`` / ``voice_result_chars``: nothing held back), is the thinker's own spoken line already (its prompt
        says its done message is heard in Yuki's voice). The talker gets it when judgement is still needed: a failure,
        a result with more to offer, one that asks them something (its own closing question mark), one with an address
        or a path to describe rather than read, a late one (they said more since: it must be framed as the earlier
        request's), and one for a request that asked for something to be done when nothing was (v3.14b). Facts of the
        job's record and the text's own punctuation and machine formats, never what the words mean.
        """
        if not self._results_by_code or kind != "done" or job.remember or job.superseded or late or nothing_done:
            return None
        if rest and not verified:
            return None
        text = " ".join((head or "").split())
        if not strip_tags(text).strip() or asks(text) or machine_text(text):
            return None
        return sentences(text) or None

    def _said_final(self, job: Job, kind: str, head: str, rest: str, lines: list[str],
                    settle: Callable[[dict[str, Any], bool], Awaitable[None]], delivered: Any, *,
                    push: Callable[[Any], Awaitable[None]] | None, verified: bool = False
                    ) -> Callable[[bool, bool], Awaitable[None]]:
        """The narrator item's deliver for a result said by code (C1): the words to the voice, the call settled with them
        as said (no talker run), and the outcome owed until its words are heard (the ledger says it again if not)."""

        async def deliver(run_llm: bool, stale: bool) -> None:
            del run_llm   # it speaks itself: the talker is never run for it
            if not stale and self._newer_request(job):
                stale = True
                self._log("bridge", op="outcome_folded", job_id=job.job_id, why="newer_request")
            try:
                if stale:
                    await settle(result_payload(kind, head, more=bool(rest), held_back=True, request=job.request), False)
                    return
                self._outcome_said(job)
                self._waiting_run = None
                self._owe(job, kind, head)
                if self.ledger is not None:
                    try:
                        self.ledger.said_by_code(job.token)
                    except Exception:  # noqa: BLE001 - the ledger never breaks the bridge
                        pass
                await self._say_lines(lines, push)
                about = {"for_request": job.request if len(job.request) <= 200 else job.request[:199] + "…"}
                note = CODE_SAID_NOTE + (" There is more: offer it in a few words if they ask; more_of_result gives "
                                         "it." if rest else "")
                await settle({"result": head, **about, "said": True, **({"verified": True} if verified else {}),
                              **({"more_available": True} if rest else {}), "how_to_handle_it": note}, False)
                self._log("bridge", op="said_by_code", job_id=job.job_id, sentences=len(lines), chars=len(head),
                          verified=verified, more=bool(rest),
                          after_done_ms=round((time.monotonic() - job.finished_at) * 1000) if job.finished_at else None)
            finally:
                if delivered is not None and not delivered.done():
                    delivered.set_result(stale)

        return deliver

    async def _say_lines(self, lines: list[str], push: Callable[[Any], Awaitable[None]] | None) -> None:
        """Put a result's sentences into the voice (one ``TTSSpeakFrame`` each, so each can start as soon as its audio is
        ready, and C2's pieces fetched ahead are found by their exact words)."""
        speak = self.speak_result
        if speak is not None:
            await speak(list(lines))
            return
        from pipecat.frames.frames import TTSSpeakFrame

        for line in lines:
            if push is not None:
                await push(TTSSpeakFrame(text=line))
            elif self.speak is not None:
                await self.speak(line)

    def _nothing_done(self, job: Job) -> bool:
        """The request asked for something to be done and no action of its job succeeded (v3.14b; its record only)."""
        nothing = bool(job.acts and not job.remember and job.actions_ok == 0)
        if nothing:
            self._log("bridge", op="nothing_done", job_id=job.job_id)
        return nothing

    def _pass_over_questions(self, lane: str) -> list[Job]:
        """Release the voice jobs on ``lane`` that wait for an answer the user did not give (v3.14).

        A job that asked the user holds its lane until they answer (the lane waits on the answer, with no end), so
        work handed to that lane after it waited behind it for good: offline 29 Sept (zen928) an unanswered "which
        video?" held "Pause it." and "Open WhatsApp." until the run timed out, the talker saying "it's queued after
        the other thing". The talker already judged the new words a new request rather than the answer (it used a
        hand, or tell_yuki new_task): the question is passed over, as a person who asked and heard something else
        would, and its job is withdrawn, unsaid. Returns the jobs withdrawn.
        """
        with self._lock:
            waiting = [j for j in self._jobs.values()
                       if j.source == "voice" and j.state == WAITING and j.lane == lane and not j.superseded]
        for old in waiting:
            self._drop_items(old)
            if self.steps is not None:
                self.steps.closed(old.token, "passed_over")
            self._gui(lambda old=old: self._withdraw(old))
        if waiting:
            self._log("bridge", op="question_passed_over", lane=lane, withdrawn=[j.job_id for j in waiting])
        return waiting

    def _drop_items(self, job: Job) -> None:
        """Take a job's waiting items out of the narrator gate, unsaid (it was withdrawn or stopped)."""
        self._unowe(job)   # nothing of it is owed any more either (v3.16)
        if self.gate is None:
            return
        for item in self.gate.take(job.token, kinds=tuple(RANKS)):
            if item.drop is not None:
                item.drop()

    def _replace_targets(self, replaces: Any, besides: Job | None = None) -> list[Job]:
        """The open voice jobs a tell_yuki with intent ``instead`` replaces: the numbered ones, else the newest
        (``besides``: never that one, the job started early for this very call)."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and not j.remember and not j.superseded
                    and j is not besides]
        wanted: set[int] = set()
        for value in (replaces if isinstance(replaces, list) else [replaces]):
            try:
                if value is not None and str(value).strip():
                    wanted.add(int(value))
            except (TypeError, ValueError):
                continue
        picked = [j for j in live if j.job_id in wanted]
        if not picked and live:
            picked = [max(live, key=lambda j: j.started_at)]
        return picked

    #: A hand-off within this many seconds of the talker's own hands acting on the desktop gets the window in
    #: front read for the thinker even without the talker's screen mark (v3.9: "open YouTube", "scroll down"
    #: x3, "play make up my mind" means the video on that page). A fact about the action log's times.
    RECENT_HANDS_S = 90.0

    def _just_used_desk(self) -> bool:
        """Whether the talker's hands acted on the desktop in the last :attr:`RECENT_HANDS_S` seconds."""
        items = list(getattr(self.actions, "_items", []) or [])
        now = time.time()
        return any(r.who == "your hand" and now - r.at <= self.RECENT_HANDS_S for r in items)

    def _desk_foreground(self) -> int | None:
        """The window in front as the talker's last desktop summary saw it (when the live read gives none)."""
        snap = getattr(self.hands, "last", None) if self.hands is not None else None
        return getattr(snap, "foreground", None)

    def tabs_for_thinker(self) -> str | None:
        """The tabs of the window in front as the talker last saw them, with the ids the thinker's tab tool takes.

        So "close that tab" is one call for the thinker (``tab(hwnd, id, close)``), never a blind Ctrl+W on
        whatever is showing (live 2026-09-27 it pressed Ctrl+W: no read_tabs, no ids in hand). ``None`` without
        hands or tabs.
        """
        hands = self.hands
        snap = getattr(hands, "last", None) if hands is not None else None
        if snap is None or snap.foreground is None:
            return None
        names = list(snap.tabs.get(snap.foreground) or [])
        ids = list(snap.tab_ids.get(snap.foreground) or [])
        recent = list(getattr(hands, "screen_titles", None) or []) if getattr(hands, "screen_hwnd", None) \
            == snap.foreground else []
        if not names or len(ids) != len(names) or not any(ids):
            if recent:
                # An app that draws its own interface (v3.13): no tab items to read, but its titles are its tabs.
                return (f"window [{snap.foreground}] exposes no tabs to read; the titles it had recently (its tabs or "
                        f"sessions, most used first): " + "; ".join(f'"{t[:90]}"' for t in recent))
            return None
        rows = [f'{i} [{tid}] "{name[:90]}"' for i, (name, tid) in enumerate(zip(names, ids), 1) if tid]
        return f"tabs of window [{snap.foreground}], left to right, with their ids: " + "; ".join(rows)

    def _arm_wait(self, job: Job, delay: float | None = None) -> None:
        """Look again at this job's silence in ``delay`` s (default: the first wait) on the voice loop."""
        loop = self._loop
        if loop is None or job.events is None or job.remember or self._progress_lines <= 0:
            return
        delay = self._wait_line_s if delay is None else max(0.3, delay)

        def arm() -> None:
            if job.wait_handle is not None:
                job.wait_handle.cancel()
            job.wait_handle = loop.call_later(delay, self._wait_check, job)

        try:
            if asyncio.get_running_loop() is loop:
                arm()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(arm)

    def _wait_check(self, job: Job) -> None:
        """A task still running while the user has heard nothing for a while gets one short "still on it" line.

        Intermediate chatter ("that didn't work, trying another way") is never said; the outcome is said once.
        Only silence that would be awkward earns a line: ``voice_wait_line_s`` of nobody speaking, a second one
        after ``voice_wait_again_s`` more, at most ``voice_progress_lines`` per task (v3.9).
        """
        job.wait_handle = None
        if job.state not in (QUEUED, RUNNING) or job.events is None or self._loop is None or job.superseded:
            return
        if job.paused:
            # appTakeover: paused because the user took over: no "still on it" line (Yuki never speaks first); checked
            # again later, so a long silence after it goes on still gets its line
            self._arm_wait(job, self._wait_again_s)
            return
        if job.waits >= (1 if job.acked else self._progress_lines):
            # Its lines said: quiet until the outcome. v3.14: an acknowledged job gets one, others up to
            # ``voice_progress_lines``, each only after real silence (the complaint of 2026-09-29 was "On it" then
            # 6-24 s of nothing); the thinker's own step lines speak first and reset the silence, so these come only
            # in a long quiet (a thinking step, a slow page), and they name the thinker's own words for a running
            # step, never a tool's generic label. Before (v3.10-v3.13) an acknowledged job got none and
            # ``min(1, voice_progress_lines)`` ignored the setting.
            return
        due = self._wait_line_s if job.waits == 0 else self._wait_again_s
        quiet = self.quiet_for() if self.quiet_for is not None else due
        if quiet < due:
            self._arm_wait(job, due - quiet)
            return
        job.waits += 1
        self._post(job, "waiting", (job.step_line if job.step_running else None,
                                    round(time.monotonic() - job.started_at), job.state == QUEUED))
        self._log("bridge", op="waiting_line", job_id=job.job_id, n=job.waits)
        if job.waits < (1 if job.acked else self._progress_lines):
            self._arm_wait(job, self._wait_again_s)   # the next one, after as much silence again

    async def escalate(self, params: Any, name: str, args: dict[str, Any], report: str,
                       acked: bool = False, context: str | None = None) -> bool:
        """A hand of the talker's did not take effect: hand the same request to the thinker now, by code (v3.9).

        Live 2026-09-27 14:18 the talker narrated every failed try ("The stop key didn't take. Let me pause it
        instead." / "Hmm, the media keys aren't working...") before handing over, three lines for one pause.
        Now the thinker gets the user's words with the hand's report and the window in front already read, and
        the user hears one outcome (and one "still on it" only if it takes a while). Returns whether a job was
        started (``False`` with no conversation loop or no runtime).

        ``context`` (v3.18): what to tell the thinker instead of the one-step note: a learned way the talker ran that
        stopped at a step (what was done, where it deviated, the way's steps), so it carries on and never redoes them.
        """
        if self._loop is None:
            return False
        heard = heard_words(params.context, skip=self.not_heard)
        request = heard[-1] if heard else _action_words(name, args)
        context = (" ".join(str(context).split())[:1200] if context else
                   f"my own one-step try ({name}) did not take effect: {' '.join(str(report).split())[:400]}")
        said = spoken_words(params.context)
        text = brain_request(request, heard, context=context, done_before=self.actions.text(), said=said,
                             tabs=self.tabs_for_thinker())
        job = self._new_job(request)
        job.escalated = True
        job.acts = True   # the talker's own hand was tried for these words: they asked for something to happen
        job.acked = acked   # the talker says a word for it now (v3.14), so no "still on it" before its outcome
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        self._pass_over_questions("worker")   # the talker acted on these words: not the answer to a question
        words = _memory_words(request, heard)
        self._gui(lambda: self._submit(job, text, origin, lane="worker", view=origin or self._desk_foreground(),
                                       memory_query=words, user_words=heard))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent="escalated", hand=name,
                  request_chars=len(request), context_chars=len(context), origin_hwnd=origin, heard_turns=len(heard))
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    async def _run_held(self, call: tuple[str, dict, Any]) -> bool:
        """Do a held hand-off after all (the mouth stayed quiet twice on it): its words said by code, its job run."""
        _name, args, context = call
        heard = heard_words(context, skip=self.not_heard)
        request = str(args.get("request") or "").strip() or (heard[-1] if heard else "")
        if not request or self._loop is None:
            return False
        app_named = " ".join(str(args.get("app_named") or "").split())[:80] or None
        text = brain_request(request, heard, context=str(args.get("context") or "").strip() or None,
                             done_before=self.actions.text(), said=spoken_words(context),
                             tabs=self.tabs_for_thinker(), app_named=app_named)
        job = self._new_job(request)
        job.app_named = app_named
        job.acked = bool(str(args.get("say_first") or "").strip())
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        view = (origin or self._desk_foreground()) if args.get("screen") is True else None
        lane = "worker" if args.get("acts") is True else None
        query = _memory_words(request, heard)
        # (v3.18: bound by its own name - the lambda runs later on the GUI thread, and the say_first below used to
        # take this variable's name, so the held run searched memory with Yuki's own words)
        self._gui(lambda: self._submit(job, text, origin, lane=lane, view=view, memory_query=query,
                                       user_words=heard, needs_hands=False if args.get("acts") is False else None))
        words = str(args.get("say_first") or "").strip()
        if words and self.speak is not None:
            await self.speak(words)
        self._log("bridge", op="submit", token=job.token, intent="held_run", request_chars=len(request),
                  screen=view is not None, lane=lane)
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    async def _relay_detached(self, job: Job) -> None:
        """Items of a job no tell_yuki call carries (escalated, or a too-late addition): through ``announce``."""
        while True:
            kind, payload = await job.events.get()
            if self._step_item(job, kind, payload):
                continue
            if kind == "say_line":
                kind, payload = "say", payload[1]
            if kind in ("done", "failed", "withdrawn") and self.steps is not None:
                self.steps.closed(job.token)
            announce = self.announce
            if announce is None:
                return
            if job.paused_out and kind in ("done", "failed", "withdrawn"):
                self._drop_items(job)   # appTakeover (review F5): what really happened, quietly
                self._unowe(job)
                await announce({"stopped": job.brief(), "how_to_handle_it": PAUSED_OUT_NOTE}, False)
                return
            if kind == "withdrawn" or (kind in ("done", "failed") and job.superseded):
                self._drop_items(job)
                self._unowe(job)
                await announce({"withdrawn": job.brief(), "how_to_handle_it": WITHDRAWN_NOTE}, False)
                return
            self_spoken = False
            if kind in ("done", "failed"):
                # Like a tell_yuki call's own result: a short or verified one said by code (C1, v3.17), anything else
                # by the talker, once.
                late = self._moved_on(job)
                held = self._held_verdict(job, kind)   # honest (87): below likely, never said as written
                verified = bool(kind == "done" and job.finished_by_action
                                and payload.strip() == job.finished_by_action) and held is None
                head, rest = condense(payload, max_sentences=self._max_sentences, max_chars=self._max_chars)
                job.rest = rest
                nothing = self._nothing_done(job) if kind == "done" else False
                lines = None if held is not None else self._by_code(job, kind, head, rest, late=late,
                                                                    verified=verified, nothing_done=nothing)
                if held is not None:
                    self._log("bridge", op="outcome_held", job_id=job.job_id, level=held.get("level"),
                              said_by="talker")
                if lines is not None and self.speak_result is None and self.speak is None:
                    lines = None   # no voice to say it by code: the talker says it
                if lines is not None:
                    self._log("bridge", op="condensed", job_id=job.job_id, chars=len(payload), spoken_chars=len(head),
                              rest_chars=len(rest), late=late, verified=verified, said_by="code")
                    deliver = self._said_final(job, kind, head, rest, lines, announce, None, push=None,
                                               verified=verified)
                    self_spoken = True
                else:
                    async def deliver(run_llm: bool, stale: bool, kind=kind, head=head, rest=rest,
                                      late=late, verified=verified, nothing=nothing) -> None:
                        if not stale and kind == "done" and self._newer_request(job):
                            stale, run_llm = True, False
                            self._log("bridge", op="outcome_folded", job_id=job.job_id, why="newer_request")
                        if run_llm and not stale and not late:
                            self.said_owed_at = time.monotonic()
                        if run_llm and not stale:
                            self._outcome_said(job)
                        if not stale:
                            self._owe(job, kind, self._owed_words(job, kind, head))   # v3.16: owed until heard
                        self._waiting_run = None   # this run says the outcome: never "stale" (waiting_reply_stale)
                        await announce(result_payload(kind, head, more=bool(rest), held_back=stale,
                                                      request=job.request, late=late, verified=verified,
                                                      nothing_done=nothing, outcome=job.verdict),
                                       run_llm and not stale)
                item_kind = "final"
            else:
                if kind == "waiting":
                    body: dict[str, Any] = waiting_payload(job.brief(200), *payload)
                elif kind == "ask":
                    body = {"yuki_asks": payload}
                elif kind == "say":
                    body = {"yuki_says": payload}
                else:
                    body = {"status": payload}

                async def deliver(run_llm: bool, stale: bool, body=body, kind=kind, payload=payload) -> None:
                    del stale
                    if run_llm:
                        self._waiting_run = (job, time.monotonic()) if kind == "waiting" else None
                    if kind == "ask":
                        self._owe(job, "ask", str(payload))   # v3.16
                    await announce(body, run_llm)
                item_kind = kind if kind in RANKS else "say"
            gate = self.gate
            if gate is not None:
                gate.enqueue(Item(job=job.token, kind=item_kind, deliver=deliver, self_spoken=self_spoken))
            else:
                await deliver(not self_spoken, False)
            if kind in ("done", "failed"):
                return


def _takes(fn: Any, name: str) -> bool:
    """Whether ``fn`` names a parameter ``name`` (a catch-all ``**kwargs`` is not enough: it may drop it unread)."""
    if not callable(fn):
        return False
    try:
        import inspect

        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _memory_words(request: str, heard: Any) -> str | None:
    """The user's own words memory is searched with for a hand-off (``yuki.agent.memory.memory_words``), or ``None``."""
    try:
        from yuki.agent.memory import memory_words
    except ImportError:   # an older agent: it takes the words from the hand-off's text itself
        return None
    try:
        return memory_words(request, heard) or None
    except Exception:  # noqa: BLE001 - a hint for memory only
        return None


def _checked(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """honest (87): a finished job's outcome level for the talker's task view, when it is below likely (its words are
    then the thinker's claim, never what was seen): ``{"checked": "not confirmed: <what the check saw>"}``."""
    from yuki.agent.finish import sayable

    if sayable(verdict):
        return {}
    label = str((verdict or {}).get("label") or "").lower()
    seen = " ".join(str((verdict or {}).get("seen") or "").split())
    return {"checked": f"{label}: {seen}" if seen else label}


def _tasks_left(tasks: list[dict[str, Any]] | None) -> list[str]:
    """N10: the items of a job's kept list still to do, in order (a few words each)."""
    return [" ".join(str(t.get("what") or "").split())[:80] for t in tasks or []
            if isinstance(t, dict) and t.get("state") in ("todo", "doing") and str(t.get("what") or "").strip()][:6]


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
