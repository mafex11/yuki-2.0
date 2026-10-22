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

bTalk (brain rebuild rev 2, design 2, 3, 10.1; only under ``Settings.brain = "all"``, :attr:`VoiceBridge.on_brain`):
a hand-off passes the user's exact words of the current exchange (IF-words below) as the new brain's ``user_words``
(no paraphrase, no envelope), with code-built, labelled context (the conversation's earlier turns and what was done
on the desktop), never quotable as their words; "how's it going?" is answered from the running plan's status line
(``Brain.status_line``) in :meth:`VoiceBridge.work_line`, with no model call; the brain's code lines (the done line,
"couldn't confirm", the stuck line) are said by code word for word, never re-voiced by the talker; the request turn is
read ahead when the user's turn ends (``Brain.begin_early``). Under ``loop`` and ``typed`` nothing here runs.

Fix plan of 10 Oct, Item 2 (VOICE-WORDS; docs/research/audit-2026-10-09/failure/fix-plan.md), under every brain setting:

* **IF-words.** A hand-off's words are the current exchange, not only the words since Yuki last replied: the user's own
  turns since the last request that ended (a job that ended without a question of its own, a hand of the talker's
  that acted, anything that tried to send) and after any request still open (it already has the words before it), at
  most :data:`EXCHANGE_TURNS`, heard within :data:`EXCHANGE_S`, oldest first, each exactly as heard
  (:func:`heard_words` with ``since_reply=True``; :func:`exact_words` and :meth:`VoiceBridge._turns` inherit it).
  "Just do it" across Yuki's interim replies, and "try another way" after its stuck line, carry the request; an ended
  request's words never come back to be done, or sent, twice. Still only their own words (M2).
* **Asks by code.** A thinker's question is said word for word in Yuki's voice, like a done line, never reworded.
* **IF-answer.** While a voice job waits for an answer, the user's next turn goes to it (:meth:`VoiceBridge.
  answer_waiting`, and a talker hand that did not take effect) unless the talker's own call for that turn says the words
  are a new task, replace it, or were not for Yuki; a refused or failed hand never withdraws the waiting job, and a job
  the runtime sets aside on its unanswered question ends quietly.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import sys
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from PySide6.QtCore import QObject, Signal

from yuki.voice.expressive import strip_tags
from yuki.voice.mouth import (
    APP_NAMED_NOTE, CODE_SAID_NOTE, CORRECTION_MARK, HANDOFF_TOOLS, OPENED_QUIET, REMEMBER_NOTE, SAVED_NOTE,
    SPOKEN_NOTE, WITHDRAWN_NOTE,
    brain_request, outcome_fields, result_payload, say_first_words, talker_kind, voice_on_brain, voice_one,
    waiting_payload,
)
from yuki.voice.narrator import RANKS, Item, asks, condense, ends_sentence, machine_text, sentences
from yuki.voice.progress import JobLog, step_of

#: voice22 (A25, final-D one-core §4: in both brains "Opening Spotify." was said before the game gate held the act):
#: the calls whose say_first announces an act the game gate decides at its step: an app or a page opened (row 1: it
#: takes the screen) and a skill run (gated rung by rung, by each rung's own data).
GAME_GATED_SAYS = frozenset({"open_app", "open_url", "run_skill"})


def say_waits_for_game(name: Any, args: Any = None) -> bool:
    """voice22 (A25): whether a call's say_first must not be said yet, because the game gate holds its act: a game is
    in front now (the gate's own local read, ``yuki.memory.gamegate.game_in_front``) and the call opens an app or a
    page (row 1 holds those with a game in front), or runs a skill none of whose rungs may run with one (its data, as
    the engine's ``_game_blocks`` reads it: a rung that takes no focus whose ``game`` is ``always``, or ``states``
    with proven states listed; a ``states`` rung with none listed never runs with a game). A skill
    not named yet (``args`` still streaming), not in the catalog, or unreadable: it waits. The act's own outcome is
    then what is heard: the game line when it is held, its done line when it went. ``args``: the call's arguments
    (a mapping), or the skill key. Blocking (a foreground read, the catalog): off the voice loop. Never raises
    (``False``: no game read, the say goes as before)."""
    tool = str(name or "")
    if tool not in GAME_GATED_SAYS:
        return False
    try:
        from yuki.memory.gamegate import game_in_front

        if not game_in_front():
            return False
    except Exception:  # noqa: BLE001 - no read: as before
        return False
    if tool != "run_skill":
        return True
    key = str((args.get("skill") if isinstance(args, dict) else args) or "").strip()
    if not key:
        return True
    try:
        from yuki.skills.catalog import load

        catalog = load()
        skill = catalog.get(key) or (catalog.get(key.split("#", 1)[0]) if "#" in key else None)
        routes = tuple(getattr(skill, "routes", None) or ()) if skill is not None else ()
    except Exception:  # noqa: BLE001 - unreadable: it waits
        return True
    def may_run(route: Any) -> bool:
        game = str(getattr(route, "game", "never") or "never")
        return not getattr(route, "needs_focus", True) and (
            game == "always" or (game == "states" and bool(getattr(route, "game_states", ()))))

    return not any(may_run(r) for r in routes)


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
#: Fix plan Item 2(b): what the talker reads for a thinker's question said by code, word for word, in its voice.
ASKED_NOTE = ("Asked aloud just now in your voice, word for word: this question is your own words to them, already "
              "heard. Do not ask it again or add to it. Their answer goes back as a follow_up hand-off; words of "
              "theirs that are not its answer are a new request.")
#: Fix plan Item 2(c): the result of a follow-up hand-off whose words already went to the waiting task as its answer.
ANSWERED_NOTE = ("These words already went to the waiting task as their answer; it carries on with them. Say nothing "
                 "about it now; its outcome is said when it comes.")

#: Item 2(d): what the talker reads for a request the runtime set aside on a question nobody answered (v3.14b: new
#: work waited on its lane, or it went unanswered for ``ask_park_s``): it ends quietly, never "it stopped".
SET_ASIDE_NOTE = ("Set aside: its question went unanswered while other work waited, so it stopped there and nothing "
                  "more was done for it. Say nothing about it now; if they ask, say that plainly.")

#: IF-words (fix plan Item 2a): a hand-off carries at most this many of the user's own turns (the current exchange) ...
EXCHANGE_TURNS = 4
#: ... each heard at most this long before it (s; a turn whose time is not known counts only while no reply of Yuki's
#: came after it, as before).
EXCHANGE_S = 120.0
#: IF-answer (Item 2c): a turn claimed as the answer to a waiting question goes to it once the talker's run for that
#: turn has settled (its own call, if it makes one, decides first): nobody speaking or writing, no reply owed and no
#: tool in flight for this long (s) ...
ANSWER_SETTLE_S = 0.4
#: ... never sooner than this after their last words (s) ...
ANSWER_HOLD_MIN_S = 0.8
#: ... and at the latest this long after them (s). Measured on the live turns of 1-10 Oct (voice_turns.csv): the
#: talker's hand-off came p90 3.5 s, max 3.9 s after the end of speech; a hand p90 4.1 s, max 5.3 s.
ANSWER_HOLD_MAX_S = 8.0
#: ... or, while a hand of the talker's for the turn is still in flight (a skill run took 26-32 s live 10 Oct), this
#: long after them (s): that hand's outcome decides first (it acted: the turn is used; it went to the thinker: routed).
ANSWER_TOOL_MAX_S = 45.0
#: Without a view of the talker's state (a stand-in conversation): routed this long after the claim (s).
ANSWER_HOLD_S = 3.0
#: How often a held claim looks at the talker's state (s).
ANSWER_TICK_S = 0.2
#: Only while the question was said this recently (s; the runtime sets a question aside after as long).
ANSWER_S = 120.0
#: A claim stands for the user's turn it holds this long after their last words in it (s): a talker's call for a turn
#: comes within :data:`ANSWER_HOLD_MAX_S`; the same words said again later are a new turn.
CLAIM_TURN_S = 20.0
#: The same turn offered to :meth:`VoiceBridge.answer_waiting` twice this close together (s; the same final transcript
#: passed on twice) is taken once.
ANSWER_SAME_TURN_S = 2.0
#: hands19 (final-B all-questions RC3a: "pause the song, also when's my meeting tomorrow": the hand-off started before
#: the pause's result came back, the planner re-did the pause and toggled the music back on): a hand-off waits for the
#: other calls of the same reply (Pipecat runs a reply's calls side by side) at most this long (s) ...
HANDS_FIRST_MAX_S = 12.0
#: ... and, when those calls can't be read, gives them this long to reach the hands' order gate (s), looking this often.
HANDS_ARRIVE_S = 0.3
HANDS_TICK_S = 0.05
#: What the planner is told the same words already got (code facts: the hands' own verified reports).
#: Review M1: it starts with the brain's ``plan.DONE_HERE_LEAD`` (the planner's answer turn for a request of several
#: parts reads it); :func:`_done_here_note` checks that they agree.
DONE_HERE_CONTEXT = ("Done already for these very words in this turn by Yuki's voice, and verified by the PC's own "
                     "check (code facts, not their words): {done}. Those parts are done: never do them again; plan "
                     "only what is left of their words.")
#: voice21 (final-D S22c, desk20 merge note 3): what the follow-up's note adds when the finished request it quotes ended
#: by asking them something (its own question mark: Yuki's question or offer). Live final-D k_video_another_one t2
#: (all): the talker folded Yuki's own "want me to bring it up?" into a new hand-off; the volume went up unasked.
FINISHED_OFFER_NOTE = (" That ending was Yuki's own question or offer, not their request: it is done only if their "
                       "newest words take it up.")
#: hands19 (final-B all-messaging RC3, contract C4): a send countdown the user's speech paused counts on (their words
#: neither stopped nor changed it) once the talker settled on their turn this long (s), never sooner than
#: ANSWER_HOLD_MIN_S after their last words; a pause that brought no words, with nobody speaking, was noise after this
#: long (s; brain one's ``ONE_NOISE_S``); with no view of the talker's state it counts on after ANSWER_HOLD_S.
SEND_SETTLE_S = 0.4
SEND_NOISE_S = 2.0
SEND_TICK_S = 0.2
#: The countdown's marks (``yuki.brain.agent.SEND_HOLD`` / ``SEND_GO_ON``), through the runtime's chip channel.
SEND_HOLD_MARK, SEND_GO_ON_MARK = "hold", "go_on"
#: Review r2 M1: a context message is one of a claim's turns when the heard log places it this close to them (s; the
#: same entry gives the same time, this only absorbs a turn noted twice far apart); a later turn is never the claim's.
CLAIM_HEARD_S = 0.5

_WORD = re.compile(r"\w+")


def _tokens(text: Any) -> tuple[str, ...]:
    """The words of a heard text as plain tokens (casefolded, punctuation and spacing aside): speech recognition's eager
    and final text, and the context's joined turns, line up on these. String plumbing, never meaning."""
    return tuple(_WORD.findall(str(text or "").casefold()))


def _within(part: tuple[str, ...], whole: tuple[str, ...]) -> bool:
    """Whether ``part`` stands in ``whole`` as a run of whole words (``yes`` is not in ``yesterday``)."""
    n = len(part)
    if not n or n > len(whole):
        return False
    return any(whole[i:i + n] == part for i in range(len(whole) - n + 1))


#: :meth:`VoiceBridge.exchange_floor`'s default: the newest turn in the heard log.
_NEWEST: Any = object()

#: The talker's hands that only bring a window or a tab to the front, by hand and action (``tab`` shows by default):
#: a step on the way to what was asked, never the end of a request (fix plan C1, Item 3(b)).
_PREPARATORY = {"window": ("focus", "restore"), "tab": ("", "show", "new")}


def _preparatory(what: str) -> bool:
    """Whether a hand's action-log line (``yuki.voice.hands._what``: ``"<hand> key=value ..."``, Yuki's own format)
    names a hand that only brought a window or a tab to the front. Plumbing on that format, never meaning."""
    parts = what.split()
    if not parts or parts[0] not in _PREPARATORY:
        return False
    action = next((p.split("=", 1)[1] for p in parts[1:] if p.startswith("action=")), "")
    return action in _PREPARATORY[parts[0]]


def _skill_sends(run: dict[str, Any]) -> bool:
    """Whether the skill of a voice hand's run (``hands.last_skill``: its key and slot values) sends something to other
    people, by its data (``yuki.skills.connection.sends_with``, as the hands' own rule-8 check reads it). ``True`` when
    the catalog can't be read: a run that may have sent is never taken for one that did not. Data only."""
    try:
        from yuki.skills.catalog import load
        from yuki.skills.connection import sends_with

        skill = load().get(str(run.get("skill") or ""))
        if skill is None:
            return False
        values = run.get("values")
        return bool(sends_with(skill, values if isinstance(values, dict) else {}, partial=True))
    except Exception:  # noqa: BLE001
        return True


@dataclass
class _Claim:
    """IF-answer (fix plan Item 2c): a turn of the user's claimed as the answer to a voice job's open question. Words in
    RAM only (never logged)."""

    job: "Job"
    parts: list[str]
    at: float = field(default_factory=time.monotonic)
    #: When their last words in it were heard (monotonic): the hold is counted from here.
    last_at: float = field(default_factory=time.monotonic)
    #: Since when the talker's run has been seen settled (monotonic), else ``None``.
    quiet_since: float | None = None
    handle: Any = None
    #: Settled (``True``): routed to the job, or dropped; else still held for the talker's own call.
    done: bool = False
    #: Dropped: the talker's own call said the turn is not the job's answer (or a hand of its used it, or the job was
    #: gone): never routed, and no later call of that turn takes it as answered.
    dropped: bool = False
    #: What routed it (code words: ``held``, ``now``, ``follow_up``, ``escalated``, ``held_call``), once routed.
    via: str = ""
    #: Review r2 M1: when its first and its last part were heard (:class:`HeardLog`'s clock), else ``None``: a talker
    #: call is for this claim only when the context's newest message is one of these turns (never a later turn that
    #: merely holds the same words: "Yes." answered, then "Yes, and turn the volume down" is a request of its own).
    heard_from: float | None = None
    heard_at: float | None = None

    @property
    def text(self) -> str:
        return " ".join(self.parts)


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
    #: hands19 (C4): its open countdown is paused on the user's speech (``SendPending("paused")``): their words decide.
    send_held: bool = False
    #: hands19 (C4): no hand-off call carries it (code passed it on: a hand's or skill's fallback, a held run, a late
    #: addition), so the talker's cancel tool reaches it only through :meth:`VoiceBridge.cancel_passed_on`; and stopped
    #: that way (nothing more of it is said, as for a cancelled hand-off call, unless its send went).
    detached: bool = False
    talker_cancelled: bool = False
    #: voice20 (final-D S11): the talker's cancelling reply said no words (a bare cancel call): its own ending is then
    #: said by code (:meth:`VoiceBridge._say_stopped`); and whether a send of it was counting down when it was stopped.
    talker_silent: bool = False
    cancel_had_send: bool = False
    #: hands19 review (H2, M2, M3): each pause of its countdown on the user's speech is one hold (its watch ends when a
    #: newer hold takes over); the final words heard while it is held (RAM only, never logged; keyed by when each turn
    #: was heard); and when the newest of them was steered into it by code (the talker routed none of them), so the
    #: talker's own later call for the same words is never a second steer or a second job.
    send_gen: int = 0
    held_words: dict[float, str] = field(default_factory=dict)
    held_steered_at: float | None = None
    #: Item 2 review r1 M2: a send countdown of this job ended ``going`` (the message went out, or may have): kept for
    #: good, so its end closes the exchange whatever its outcome says (the new brain's stuck endings carry no ``send``)
    #: and no later request carries its words again (rule 8: never sent twice).
    sent: bool = False
    #: appTakeover: paused because the user took over the keyboard or mouse (the talker's thinker line says so, so their
    #: "go on" or anything they add is for this task); ``paused_out``: it ended unresumed (quietly: nothing is said).
    paused: bool = False
    paused_out: bool = False
    #: Handed over with intent ``remember`` (a rule, correction or life fact to keep).
    remember: bool = False
    #: The desktop action it is running now, in a few words (for the shared action log).
    acting: str = ""
    #: lVoice: ``(hwnd, tab id)`` that action leaves its effect on (``_acting_on``), for the log's liveness check.
    acting_on: tuple[int | None, str] | None = None
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
    #: hands19 (final-B all-browser R2): the conversation it was handed over in (the shared action log's number): that
    #: turn count restarts at 0 in each conversation, so every comparison of turns is of ``(convo, user_turn)``.
    convo: int = 0
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
    #: bTalk review r1 M1: seen running on the new brain (the runtime lane's driving agent was the Brain while it ran
    #: this job): only then are its words code's own line, said word for word with the honesty gate off.
    brain_run: bool = False
    #: Item 2: when its open question was said to the user (monotonic; by code, or handed to the talker), else
    #: ``None``: only a question they heard takes their next turn as its answer.
    asked_at: float | None = None
    #: Item 2 review r2 L1: the keys of the user's skills it was pinned with or ran (``run_skill``): a skill hand-off of
    #: the talker's for one of these while it waits on a question is its answer, never a request of its own.
    skills: set[str] = field(default_factory=set)

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


class HeardLog:
    """When each of the user's final spoken turns was heard (monotonic), newest last: the clock the exchange is cut by
    (IF-words, fix plan Item 2a). Fed from the conversation's final-words hook (:meth:`VoiceBridge.prefetch_pack`);
    the words stay in RAM for the last few turns and are never logged.

    The talker's context holds the same turns as its user messages (speech recognition's text as heard; one message
    may hold several turns the context joined). :meth:`times` lines the messages up with these turns by their words
    (:func:`_tokens`: casefolded, punctuation aside, whole words only), newest first, so each message gets the time it
    was heard. Plumbing on string identity, never meaning.
    """

    KEEP = 24
    #: Heard turns newer than the newest message that may not be in the context yet (said while the talker ran), or
    #: that never reached it, passed over at most this many at a time.
    AHEAD = 3

    def __init__(self) -> None:
        self._turns: deque[tuple[float, tuple[str, ...]]] = deque(maxlen=self.KEEP)

    def note(self, text: str, at: float | None = None) -> float | None:
        """A final turn of the user's was heard now (or ``at``); returns when that turn was heard (``None``: no words).

        The same turn noted twice (the conversation's final-words hook, which Pipecat's observers run on their own
        task, and :meth:`VoiceBridge.answer_waiting`, which may come first) is kept once: the same words noted within
        :data:`ANSWER_SAME_TURN_S` of a recent note are that note's turn (review r2 M1: a claim is bound to its turn's
        time)."""
        words = _tokens(text)
        if not words:
            return None
        when = time.monotonic() if at is None else float(at)
        for k in range(len(self._turns) - 1, max(-1, len(self._turns) - 1 - self.AHEAD), -1):
            seen, same = self._turns[k]
            if same == words and abs(when - seen) <= ANSWER_SAME_TURN_S:
                return seen
        self._turns.append((when, words))
        return when

    def newest(self) -> float | None:
        """When the newest final turn was heard (monotonic), or ``None``."""
        return self._turns[-1][0] if self._turns else None

    def heard_at(self, text: str) -> float | None:
        """When the newest heard turn holding ``text`` was heard (a message of the context: one turn or several the
        context joined), or ``None`` when no heard turn of the last few lines up with it."""
        return self.times([text])[0] if text else None

    def times(self, texts: list[str]) -> list[float | None]:
        """When each of ``texts`` (the context's user messages, newest first) was heard, ``None`` where unknown.

        The alignment goes newest first and stops at the first message no heard turn lines up with (a summary, a turn
        from before this log, a message whose text differs from what was heard): every older one is unknown too."""
        heard = list(reversed(self._turns))
        out: list[float | None] = []
        i = 0
        for text in texts:
            message = _tokens(text)
            at: float | None = None
            for k in range(i, min(len(heard), i + self.AHEAD + 1)):
                if _within(heard[k][1], message):
                    at, used, i = heard[k][0], len(heard[k][1]), k + 1
                    # older turns the context joined into this same message (each whole, within its length)
                    while i < len(heard) and _within(heard[i][1], message) and used + len(heard[i][1]) <= len(message):
                        used += len(heard[i][1])
                        i += 1
                    break
            if at is None:
                break
            out.append(at)
        return out + [None] * (len(texts) - len(out))


#: The bridge whose heard turns and finished requests cut the exchange for :func:`heard_words` when its caller passes
#: none (the conversation's own direct call, ``pipeline._early_handoff``). One bridge lives in the app (made with the
#: session); the newest one made wins. A weak reference: it never keeps a bridge alive.
_LIVE: "weakref.ReferenceType[Any] | None" = None


def _live_exchange() -> Any:
    ref = _LIVE
    return ref() if ref is not None else None


class _Floored:
    """A bridge's exchange seen from a later floor (``at``, monotonic): only turns heard after it count."""

    def __init__(self, bridge: Any, at: float) -> None:
        self.heard_log = getattr(bridge, "heard_log", None)
        self._bridge, self._at = bridge, float(at)

    def exchange_floor(self, newest_at: float | None = None) -> float:
        floor = self._bridge.exchange_floor(newest_at)
        return self._at if floor is None else max(floor, self._at)


def heard_words(context: Any, turns: int = HEARD_TURNS, skip: Callable[[Any], bool] | None = None, *,
                since_reply: bool = False, exchange: Any = None) -> list[str]:
    """The user's latest spoken turns in a Pipecat ``LLMContext``, oldest first (plumbing for the brain).

    Several, not one: the mouth may hand a request over a turn or two after
    it was made (the user said something else meanwhile), and the brain must
    see the words the request came from, not only the last thing said.
    ``skip`` leaves out user-role messages nobody said: Pipecat's context summary is
    one (live 2026-09-25 the thinker was told the user had said a whole summary).

    ``since_reply`` (Phase S, D10; fix plan Item 2a, IF-words): the request's own words, exactly as heard (the
    hand-off's first line, never the talker's paraphrase): the current exchange. Live 10 Oct, cutting at Yuki's last
    reply lost every request said before a "just do it" or a "try another way". Now: the user's turns back to the last
    request that ended (a job that ended without a question of its own, a hand of the talker's that acted, anything
    that tried to send), and after the hand-over of any request still open (those words are its own), at most
    :data:`EXCHANGE_TURNS`, each heard within :data:`EXCHANGE_S` (:meth:`VoiceBridge.exchange_floor`): an ended
    request's words never come back to be done, or sent, twice. The newest turn always counts. A turn whose time
    is unknown (no heard log, or the alignment could not place it) counts only while no reply of Yuki's came after it,
    as before. ``exchange``: the bridge that knows the heard turns and the requests (:class:`VoiceBridge`); ``None``:
    the live one. The context's structure, the heard turns' times and the job table only, never the words' meaning.
    """
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return []
    if since_reply:
        return _exchange_words(messages or [], skip, exchange if exchange is not None else _live_exchange())
    out: list[str] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if skip is not None and skip(message):
            continue
        text = _user_text(message)
        if text:  # async-tool results are "developer"/"tool" messages, so never picked up here
            out.append(text)
            if len(out) >= turns:
                break
    return list(reversed(out))


def _user_text(message: dict) -> str:
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return text.strip()


def _exchange_words(messages: list[Any], skip: Callable[[Any], bool] | None, exchange: Any) -> list[str]:
    """IF-words: the user's turns of the current exchange, oldest first (see :func:`heard_words`)."""
    # newest first: (text, nobody said it to Yuki, a reply of Yuki's came after it)
    users: list[tuple[str, bool, bool]] = []
    replied = False
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            # the reply being written now (the call that asks, its tool calls) comes first from the end: passed over
            replied = replied or bool(users)
            continue
        if role != "user":
            continue   # async results ("developer" / "tool") are no reply and nobody's words
        text = _user_text(message)
        if text:
            users.append((text, bool(skip is not None and skip(message)), replied))
    log = getattr(exchange, "heard_log", None)
    try:
        times = log.times([u[0] for u in users]) if log is not None else [None] * len(users)
    except Exception:  # noqa: BLE001 - no times: the old cut at Yuki's last reply
        times = [None] * len(users)
    try:
        # the newest turn's own time: a hand that acted after it acted on these very words, never a finished request
        floor = exchange.exchange_floor(times[0] if times else None) if exchange is not None else None
    except Exception:  # noqa: BLE001
        floor = None
    now = time.monotonic()
    out: list[str] = []
    for (text, skipped, after_reply), at in zip(users, times):
        if out:   # the newest of their turns always counts
            if len(out) >= EXCHANGE_TURNS:
                break
            if at is None:
                if after_reply:
                    break   # no time known: Yuki's last reply before their words ends them, as before
            elif now - at > EXCHANGE_S or (floor is not None and at <= floor):
                break       # too long ago, or before the last finished request / an open one's hand-over
        if skipped:
            continue
        out.append(text)
    return list(reversed(out))


def exact_words(context: Any, skip: Callable[[Any], bool] | None = None, heard: list[str] | None = None, *,
                exchange: Any = None) -> str:
    """The user's own words for a hand-off (Phase S, D10; IF-words): their turns of the current exchange
    (:func:`heard_words` ``since_reply``), joined, exactly as speech recognition heard them; the latest heard turn when
    there is none (a re-run with no new words)."""
    words = [w for w in heard_words(context, skip=skip, since_reply=True, exchange=exchange) if w.strip()]
    if words:
        return " ".join(" ".join(w.split()) for w in words)
    heard = heard if heard is not None else heard_words(context, skip=skip)
    return heard[-1].strip() if heard else ""


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

    # Fix plan Item 2's state, set per bridge in __init__; these class defaults keep a stand-in made without it (the
    # tests' ``VoiceBridge.__new__``) working as before: no heard log, no claim, nothing closed.
    heard_log: "HeardLog | None" = None
    _closed_at: float | None = None
    _escalated_hands: "deque[Any] | None" = None
    _claim: "_Claim | None" = None
    talker_busy: "Callable[[], bool] | None" = None
    #: oVoice: the ONE agent owns the spoken turn (set in __init__; a stand-in made without it is not ONE).
    one = False

    def __init__(self, runtime: Any, log: Any = None, *, foreground: Callable[[], int | None] | None = None,
                 settings: Any = None, hands_backend: Any = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.log = log
        self._foreground = foreground or _foreground_hwnd
        self._max_sentences = int(getattr(settings, "voice_result_sentences", 2))
        #: bTalk (design 10.1): spoken requests run on the new brain (``Settings.brain = "all"``). Every bTalk path in
        #: this class runs only then; under loop / typed the bridge is exactly as before.
        self.on_brain = voice_on_brain(settings)
        #: oVoice (phase 4): the ONE agent owns the spoken turn (``Settings.brain = "one"``): no hand-over, no thinker
        #: warm-up or request pack, no job table; the conversation's ONE driver (``voice.pipeline.OneVoice``) acts
        #: through the runner. Under every other value this is false and the bridge is exactly as before.
        self.one = voice_one(settings)
        #: oVoice: the ids the ONE driver's send countdowns were shown under on the island (:meth:`one_send_pending`),
        #: so the island's Stop reaches this voice (:meth:`owns`); the current one and whether it closed.
        self._one_send_ids: deque[int] = deque(maxlen=32)
        self._one_send_rid: int | None = None
        self._one_send_closed = True
        self._one_send_seq = itertools.count(1)
        #: The talker's hand-off tool (``hand_over`` for the lean talker, else ``tell_yuki``), for the lines it reads.
        self._lean = talker_kind(settings) == "lean"
        self._handoff_tool = "hand_over" if self._lean else "tell_yuki"
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
        #: Integration review VC-2: the last :meth:`cancel_passed_on` found the job's send gone already (nothing was
        #: stopped; the talker is told it went, never "will not go on").
        self.cancel_found_sent = False
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
        #: voice21 (final-D S10): ``(spoke) -> result | None``: a stay_quiet on the user's turn now is the one refused
        #: (the first words of a conversation they opened themselves, no call, no dictation, nothing heard yet;
        #: ``spoke``: this reply put words out), taken once a conversation (``Conversation._opened_quiet``): the
        #: refusal's tool result (``mouth.OPENED_QUIET``, with the reply format in the marker channel; ``True``: the
        #: note alone), else ``None``. Set by the conversation. voice22 (review L3): called ``(spoke, tool_call_id)``,
        #: the refused stay_quiet's id (the run its result starts is the refusal's answer).
        self.opened_quiet: Callable[..., Any] | None = None
        #: la A2 (final-H2 kw_offer_then_yes t2): ``(spoke, tool_call_id) -> result | None``: a lone stay_quiet on the
        #: talker's own first reply to words that follow Yuki's own heard line (no call, game or dictation, nothing
        #: heard this turn, once a turn: ``Conversation._follow_quiet``) is refused once, with ONE's own note; else
        #: ``None``. Set by the conversation; under one ``OneVoice.control`` refuses its own.
        self.follow_quiet: Callable[..., Any] | None = None
        #: integ30 (le merge note 1, contract K-R talker side): ``() -> float``: when words of Yuki's last left the
        #: speaker (monotonic, 0: never; the observer's ``words_heard_at``), set by the conversation.
        self.yuki_heard_at: Callable[[], float] | None = None
        #: integ30 (K-R talker side): the talker's remember held to ask first (:meth:`_reversal_gate`):
        #: ``(conversation, user turn, when that turn was heard, what it turns around)``, else ``None`` (RAM only).
        self._remember_held: tuple[int, int, float, str] | None = None
        #: Whether the mouth run now answers the first words after the wake word heard the user say the name
        #: (set by the conversation; v3.6): those words were said to Yuki, so staying quiet is refused once.
        self.named_turn: Callable[[], bool] | None = None
        #: ``(tool_call_id) -> bool``: the call's say_first was already spoken while the mouth was still
        #: writing the call (v3.5, set by the conversation).
        self.said_early: Callable[[str], bool] | None = None
        #: fc (C5, contract K-D): ``(tool_call_id) -> str``: the words that say_first spoke while the call was written
        #: (set by the conversation; read through :meth:`early_said`).
        self.early_words: Callable[[str], str] | None = None
        #: fc (C5): the say_first words :meth:`_ack_if_silent` spoke by code just before a call ran, by call id (content,
        #: RAM only, the last few; this conversation's).
        self._acked_words: dict[str, str] = {}
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
        #: The user turn an acknowledgement was last said for (v3.12: one per request). hands19: ``(conversation, turn)``
        #: (or -1), reset as each conversation goes live.
        self._acked_turn: Any = -1
        #: hands19 (C4): the watch of a send countdown the user's speech paused (:meth:`_held_send_watch`), kept so it
        #: is never collected before it ends.
        self._send_watch: Any = None
        #: hands19 review (M2): the talker's hand-off calls still deciding where their words go (entered, not yet
        #: submitted, steered or refused), and when each hand-off call came in (monotonic, the last few): a held
        #: countdown's words the talker routed itself are never steered in by code as well.
        self._routing: dict[int, float] = {}
        #: integ23 S1: the router's verdicts still out on the user's words while a send counted down (the pipeline's
        #: ``_route_turn`` puts each decision's deadline here and its task's end takes it out, monotonic): a held
        #: countdown never counts on before they are in (:meth:`_held_send_watch`).
        self.router_sends: dict[int, float] = {}
        self._handovers_at: deque[float] = deque(maxlen=8)
        #: IF-words (Item 2a): when each of the user's final turns was heard, and when the last spoken request ended
        #: the exchange (monotonic): the exchange a hand-off carries starts after it (:meth:`exchange_floor`).
        self.heard_log = HeardLog()
        self._closed_at: float | None = None
        #: Hands of the talker's that did not take effect and went to the thinker (their action records, by identity):
        #: the job they became decides whether the request finished, never the hand's own record.
        self._escalated_hands: deque[Any] = deque(maxlen=16)
        #: Review r2 L4: ``(skill run, whether its skill sends)`` for the latest skill run read (:meth:`_run_sends`).
        self._sends_of: tuple[dict[str, Any], bool] | None = None
        #: IF-answer (Item 2c): the user's turn claimed as the answer to the waiting question, until it is routed or
        #: the talker's own call for the turn says it is not (:meth:`answer_waiting`).
        self._claim: _Claim | None = None
        #: ``() -> bool``: the talker is still on the user's turn (hearing, writing, owing its reply, a tool in
        #: flight); set by the conversation if it can say so, else read from the narrator gate's own state
        #: (:meth:`_talker_busy`).
        self.talker_busy: Callable[[], bool] | None = None
        global _LIVE
        _LIVE = weakref.ref(self)
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
            ("paused_changed", self._on_paused_changed), ("outcome", self._on_outcome), ("parked", self._on_parked),
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
        self._drop_claim("detached")   # Item 2(c): a turn held as an answer goes no further once its conversation ends
        self._loop = loop
        self.closing = False
        self._named_refused = False
        if loop is not None:
            # lVoice (live 17 Oct, problem 2): a conversation going live starts the shared action log's lines anew.
            # Before, one log carried a quarter of an hour across conversations: 5 of 5 browser tasks that worked in a
            # first run failed in the second (aimed at a tab closed since, or "There you go" with nothing done)
            self._new_conversation()
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
            self.opened_quiet = None
            self.follow_quiet = None
            self.yuki_heard_at = None
            self._remember_held = None
            self.named_turn = None
            self.said_early = None
            self.early_words = None
            self._acked_words = {}
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

    def _new_conversation(self) -> None:
        """lVoice: the hands (and the action log) start a new conversation's state. Never raises."""
        try:
            hands = self.hands
            if hands is not None and callable(getattr(hands, "new_conversation", None)):
                hands.new_conversation()   # it starts the shared log's new conversation too
            else:
                self.actions.new_conversation()
            # hands19 (all-browser R2, one-voice C): the last conversation's acknowledged turn is not this one's (turn 1
            # here read as acknowledged, so most turn-1 say_firsts were never said)
            self._acked_turn = -1
            self._log("bridge", op="new_conversation", convo=getattr(self.actions, "convo", None))
        except Exception:  # noqa: BLE001 - a log that keeps its lines is the old behaviour, never a crash
            pass

    def _done_text(self) -> str:
        """lVoice (problem 2): the action log as the thinker reads it with a request: this conversation's lines, each
        with its age, none whose window or tab is gone (the hands' identity check on the last desktop read or the OS;
        cheap, on the loop)."""
        alive = getattr(self.hands, "record_alive", None) if self.hands is not None else None
        try:
            return self.actions.text(alive=alive) if callable(alive) else self.actions.text()
        except TypeError:   # an older log without the check
            return self.actions.text()

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

    #: Phase 2 (2.3): the owed ledger's tokens for a hand's or skill's own outcome (negative: never a job's token).
    _line_tokens = itertools.count(-1, -1)

    def owe_line(self, text: str, kind: str = "failed") -> int | None:
        """Phase 2 (2.3): code's own line for a hand's or skill run's non-success outcome, kept owed while the talker
        says the outcome in its own words: the ledger says it by code only when no talker words are heard for it (its
        run chose silence, wrote only calls, or never came in time). Returns its token (``drop_owed_line``), or
        ``None`` with no ledger. Never raises."""
        ledger = self.ledger
        if ledger is None or not " ".join(str(text or "").split()):
            return None
        token = next(self._line_tokens)
        try:
            turns = int(self.user_turns()) if self.user_turns is not None else 0
        except Exception:  # noqa: BLE001
            turns = 0
        try:
            return token if ledger.owe(token, kind, text, turns) is not None else None
        except Exception as exc:  # noqa: BLE001 - the ledger never breaks a hand
            self._log("bridge_error", error=f"owe_line: {type(exc).__name__}")
            return None

    def drop_owed_line(self, token: int) -> None:
        """Phase 2 (2.3): the talker acted on that outcome (its next call): code's fallback line is let go."""
        if self.ledger is not None:
            try:
                self.ledger.drop(token)
            except Exception:  # noqa: BLE001
                pass

    def hold_owed_line(self, token: int, held: bool) -> None:
        """Review r1 M4: code's fallback line held while the talker's next call for that outcome runs (``True``), or
        due again because that call was not tried (``False``). Never raises."""
        hold = getattr(self.ledger, "hold", None) if self.ledger is not None else None
        if callable(hold):
            try:
                hold(token, held)
            except Exception:  # noqa: BLE001
                pass

    def _unowe(self, job: Job) -> None:
        if self.ledger is not None:
            try:
                self.ledger.drop(job.token)
            except Exception:  # noqa: BLE001
                pass

    def owns(self, job_id: int) -> bool:
        """Whether a job came from voice (the UI speaks its questions instead of popping the overlay). oVoice: also the
        id a ONE send countdown was shown under (the island's Stop then reaches the voice: ``ui.app._on_send_stop``
        calls ``voice.interrupt()``, which cancels the countdown)."""
        if getattr(self, "one", False) and job_id in self._one_send_ids:
            return True
        job = self._job(job_id)
        return job is not None and job.source == "voice"

    #: oVoice: the ONE driver's countdown ids start here, far above the runtime's own request ids (never the same id).
    ONE_SEND_ID_BASE = 1_900_000_000
    #: oVoice (review fix L3): what a hand-off tool answers if it is ever called under ``brain = "one"``.
    ONE_NO_HANDOFF = "Not done: there is no hand-over here, so nothing was handed on. Do it with your own tools."

    async def _one_refuses(self, params: Any, tool: str) -> bool:
        """oVoice (review fix L3, defence in depth): under ``brain = "one"`` the old hand-off handlers refuse (nothing is
        submitted, no thinker job; they are not registered under one, so this is never reached on a live turn)."""
        if not getattr(self, "one", False):
            return False
        self._log("bridge", op=tool, refused="one")
        try:
            from pipecat.frames.frames import FunctionCallResultProperties

            await params.result_callback({"handed_over": False, "note": self.ONE_NO_HANDOFF},
                                         properties=FunctionCallResultProperties(run_llm=False))
        except Exception as exc:  # noqa: BLE001 - nothing was submitted either way
            self._log("bridge_error", error=f"one_refuses: {type(exc).__name__}")
        return True

    def one_send_pending(self, event: Any) -> int | None:
        """oVoice (phase 4 addendum, must-fix 5): the ONE countdown host's ``publish`` for voice: its ``SendPending``
        goes to the island as the runtime's own countdowns do (``runtime.send_pending``, lane ``voice``), under one id
        per countdown (a pause and its resume keep it). Any thread; never raises. Returns the id shown."""
        state = str(getattr(event, "state", "") or "")
        with self._lock:
            if self._one_send_rid is None or (state == "counting" and self._one_send_closed):
                self._one_send_rid = self.ONE_SEND_ID_BASE + next(self._one_send_seq)
                self._one_send_ids.append(self._one_send_rid)
            rid = self._one_send_rid
            self._one_send_closed = state in ("going", "stopped", "swapped")
        signal = getattr(self.runtime, "send_pending", None)
        emit = getattr(signal, "emit", None)
        if callable(emit):
            try:
                emit("voice", int(rid), {
                    "state": state, "who": getattr(event, "who", ""), "where": getattr(event, "where", ""),
                    "app": getattr(event, "app", ""), "text": getattr(event, "text", ""),
                    "seconds": float(getattr(event, "seconds", 0.0) or 0.0),
                    "who_alts": list(getattr(event, "who_alts", ()) or ()),
                    "where_alts": list(getattr(event, "where_alts", ()) or ()), "typed": False,
                    "from_memory": bool(getattr(event, "from_memory", False)),
                    "window": getattr(event, "window", "")})
            except Exception as exc:  # noqa: BLE001 - presentation only; the countdown runs on
                self._log("bridge_error", error=f"one_send_pending: {type(exc).__name__}")
        self._log("bridge", op="one_send", state=state)
        return rid

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
        if getattr(self, "one", False):
            return None   # oVoice: no thinker; the ONE agent's own context holds what it is doing
        if self.on_brain:
            return self._brain_work_line(now)   # bTalk: the running plan's status, by code
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

    def awaiting_words(self) -> str:
        """D1 (20 Oct, keyword audit A1): what of this conversation's spoken requests the user's next words belong to,
        as a code word: ``"task"`` (one is queued or running, or waits for their answer: their words are its, for the
        talker to route), ``"countdown"`` (the only open ones are sends counting down, :attr:`Job.sending`: their speech
        holds the send, their words decide it, and an end of the conversation never lets it go), ``""`` (none). A job
        already stopping (its stop asked) waits for no words. The router's end of the conversation reads it
        (``Conversation._words_awaited``). State only; never raises (unsure: ``"task"``)."""
        try:
            with self._lock:
                live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                        if j.source == "voice" and j.state in OPEN_STATES and j.events is not None and not j.unadopted
                        and not j.cancel_requested]
            if not live:
                return ""
            if all(j.state == RUNNING and j.sending for j in live):
                return "countdown"
            return "task"
        except Exception:  # noqa: BLE001
            return "task"

    def _open_sends(self) -> list[Job]:
        """The voice jobs whose send countdown is open now and has not sent (:meth:`stop_sends`). Call under the
        lock."""
        return [j for j in self._jobs.values() if j.source == "voice" and j.state == RUNNING and j.sending
                and not j.sent and not j.cancel_requested and not j.superseded and j.job_id is not None]

    def counting_sends(self) -> frozenset[int]:
        """integ23 S2: the job ids :meth:`stop_sends` would stop now (none under brain one). The router's decision
        keeps them, so a stop it makes after they spoke again stops only those, never a send opened since. State only;
        never raises."""
        try:
            if getattr(self, "one", False):
                return frozenset()
            with self._lock:
                return frozenset(int(j.job_id) for j in self._open_sends())
        except Exception:  # noqa: BLE001
            return frozenset()

    def stop_sends(self, only: frozenset[int] | None = None) -> int:
        """D1 (20 Oct): stop at once every send of this conversation's spoken requests that is counting down now (an
        "all" voice job's open countdown, :attr:`Job.sending`, counting or held): the router's model judged that the
        user's words call it off, or send Yuki away while it counts. The job stops as the voice's own stop
        (:meth:`_cancel`, on the GUI thread): nothing is sent. A send that went already is never touched. Under brain
        one: ``0`` (its own countdown is cancelled by the pipeline's driver). ``only`` (integ23 S2): just these job
        ids (:meth:`counting_sends` when the words were judged). Returns how many were stopped. State only; never
        raises."""
        try:
            if getattr(self, "one", False):
                return 0
            with self._lock:
                live = [j for j in self._open_sends() if only is None or int(j.job_id) in only]
            self._router_stopped = list(live)   # integ27 fix TV1: :meth:`stopped_told` settles these when told
            for job in live:
                job.cancel_requested = True
                job.cancel_had_send = True
                self._gui(lambda j=job: self._cancel(j))
                self._unowe(job)
            if live:
                self._log("bridge", op="sends_stopped", n=len(live), by="router")
                self._changed()
            return len(live)
        except Exception as exc:  # noqa: BLE001 - the held countdown never counts on past the end either way
            self._log("bridge_error", error=f"stop_sends: {type(exc).__name__}")
            return 0

    def stopped_told(self) -> int:
        """integ27 fix TV1 (C4 retest under all: the router's end said "Stopped. Nothing was sent." and the stopped
        job's own final could follow it): the router's end says the stop line itself, so the sends the last
        :meth:`stop_sends` stopped settle as a talker's cancel that said so (:meth:`cancel_passed_on` with ``said``):
        ``talker_cancelled``, not silent, their items at the narrator gate taken, nothing owed. :meth:`_relay_detached`
        then drops the job's own final instead of relaying it. A send that went already is left as it is. Returns how
        many. State only; never raises."""
        try:
            jobs, self._router_stopped = list(getattr(self, "_router_stopped", None) or ()), []
            told = 0
            for job in jobs:
                if job.sent:
                    continue
                job.talker_cancelled = True
                job.talker_silent = False
                if self.gate is not None:
                    for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                        if item.drop is not None:
                            item.drop()
                self._unowe(job)
                told += 1
            if told:
                self._log("bridge", op="sends_told", n=told, by="router")
            return told
        except Exception as exc:  # noqa: BLE001
            self._log("bridge_error", error=f"stopped_told: {type(exc).__name__}")
            return 0

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
        if self._closes_exchange(job, state):
            self._closed_at = job.finished_at
            self._log("bridge", op="exchange_closed", job_id=job.job_id)
        self._changed()

    @staticmethod
    def _closes_exchange(job: Job, state: str) -> bool:
        """IF-words (Item 2a): whether a finished job ends the exchange, so the user's next words start a new one.

        Every spoken request that ended (done, failed, "couldn't confirm", stopped) ends it, unless the brain marked
        its ending as waiting for their answer (contract K1, keyword audit A7: ``verdict["awaits"]``, set by the brain
        when it ended on a question to them, a plan that asks, or an offer it made, "I can try your liked songs
        instead" with no question mark included): then their next words are its answer and carry the request ("try
        another way" after the stuck line is about that same playlist). A verdict without the field (an older brain)
        closes it, as a finished act does, and so does any value but ``True`` (the safe side). A "?" in the closing
        line no longer decides it (punctuation is no intent). Its words are never carried into a new request (live
        10 Oct a request after a failed one re-ran the failed YouTube task; the brain takes every carried turn as
        their request): what they say next ("message Hemanth", or "just do it") goes with the labelled earlier lines
        instead. A done act at ``verified``, ``likely`` or ``unverified`` ends it
        even with an offer after it ("Want it louder?" then "yes" never plays it again; review r2 L2: below likely the
        thinker's words, offer and all, are never said as written, and an act that may have worked is never redone by
        the next request), and so does any request that tried to send
        (the verdict's ``send``, or a send countdown of its that ended ``going``: the new brain's stuck endings carry
        no ``send`` even after one went, review r1 M2): nothing is ever sent twice (rule 8). A request to keep
        (``remember``) ends it when it ends (review r1 L2: its words are never handed over, or saved, again). A request
        withdrawn for a newer one leaves its words to that one. A done act at ``failed`` keeps the brain's own mark
        ("That didn't work. Want me to try another way?" stays open when it awaits their answer). The verdict's fields
        and the job's own marks only, never the words nor their punctuation."""
        if job.source != "voice" or job.unadopted:
            return False
        verdict = job.verdict if isinstance(job.verdict, dict) else {}
        if verdict.get("send") or job.sent:
            return True
        if job.remember:
            return not job.superseded
        if job.superseded and not job.paused_out:
            return False
        level = str(verdict.get("level") or "")
        if state == DONE and (level in ("verified", "likely") or (level == "unverified" and verdict.get("acted"))):
            return True
        return verdict.get("awaits") is not True

    def exchange_floor(self, newest_at: Any = _NEWEST) -> float | None:
        """IF-words (Item 2a): the time before which a heard turn is not part of the current exchange (monotonic), or
        ``None``. The newest of:

        * the end of the last spoken request that ended the exchange (:meth:`_closes_exchange`);
        * the hand-over of a spoken request still open from an earlier turn (its words are its own: a new request never
          carries them again, so it can never redo, or send twice, what that one is doing; a job made for the user's
          current turn, started early or by a hand that did not take effect, is this request's own and sets none);
        * the last turn routed to such an open job as the answer to its question (review r2 M1: those words are its
          own too);
        * a hand of the talker's that acted before the newest of their turns (``newest_at``, monotonic; by default the
          newest turn in :attr:`heard_log`; ``None``: no hand counts) (:meth:`_hand_marks`): "volume down", done, then
          "what's on my calendar?" never turns the volume down again.

        Called with no argument by the pipeline's own exchange (Item 3: the hands' commit check and a held call's
        words). The job table, the shared action log's records and the clock only."""
        if newest_at is _NEWEST:
            newest_at = self.heard_log.newest() if self.heard_log is not None else None
        turn = self._turn_now()   # hands19: ``(conversation, turn)``
        with self._lock:
            # a request to keep counts too (review r1 L2), and one replaced after its send went keeps its words its own
            # (rule 8)
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and (j.sent or not (j.superseded or
                                                                                         j.cancel_requested))
                    and not j.unadopted and (turn is None or (j.convo, j.user_turn) < turn)]
        marks = [m for m in (self._closed_at, *(j.started_at for j in live)) if m is not None]
        claim = self._claim
        if claim is not None and claim.done and not claim.dropped and claim.heard_at is not None \
                and any(claim.job is j for j in live):
            # review r2 M1: the turns routed to an open job as its answer are that job's words, like its request: a
            # later request ("Yes, and turn the volume down" after "Yes.") never carries them
            marks.append(claim.heard_at)
        if newest_at is not None:
            marks.extend(m for m in self._hand_marks() if m < newest_at)
        return max(marks) if marks else None

    def _hand_marks(self) -> list[float]:
        """When the talker's own hands finished a request (monotonic): each record of a hand that acted (``ok``), a
        skill run that acted but could not confirm it (its engine outcome ``unverified``: it may have sent), and a run
        of a skill that sends which ended ``failed`` (it may have gone anyway, :func:`_skill_sends`), unless the hand
        went to the thinker (:meth:`_note_escalated_hand`). A refused or failed hand did nothing: "just do it"
        after it still carries the request. Nor does a hand that only brought a window or a tab to the front
        (:func:`_preparatory`; live 10 Oct "do it." got a focus of Arc and nothing more, fix plan C1): what was asked is
        still to do. Records and code words only."""
        wall, mono = time.time(), time.monotonic()
        escalated = list(self._escalated_hands or ())
        out: list[float] = []
        for record in list(getattr(getattr(self, "actions", None), "_items", None) or []):
            if getattr(record, "who", "") == "your hand" and getattr(record, "ok", False) \
                    and not _preparatory(str(getattr(record, "what", "") or "")) \
                    and not any(record is e for e in escalated):
                out.append(mono - (wall - float(getattr(record, "at", wall))))
        last = getattr(getattr(self, "hands", None), "last_skill", None)
        if isinstance(last, dict) and not any(last is e for e in escalated) and (
                last.get("outcome") == "unverified" or (last.get("outcome") == "failed" and self._run_sends(last))):
            # review r1 M2: a skill that sends and failed after a rung acted may have sent anyway (the engine never
            # tries again for that reason): its words are never handed over again either (rule 8)
            try:
                out.append(mono - (wall - float(last.get("at") or wall)))
            except (TypeError, ValueError):
                pass
        return out

    def _run_sends(self, run: dict[str, Any]) -> bool:
        """:func:`_skill_sends` of a skill run, read once per run (review r2 L4: :meth:`_hand_marks` runs on the voice
        loop at every hand-off and claim, and the catalog read stats every connection file; the lightweight rule)."""
        cached = getattr(self, "_sends_of", None)
        if cached is not None and cached[0] is run:
            return cached[1]
        sends = _skill_sends(run)
        self._sends_of = (run, sends)
        return sends

    #: A hand's record (or its skill run) this recent when :meth:`escalate` is called is the one that went over (s).
    ESCALATED_RECENT_S = 10.0

    def _note_escalated_hand(self, name: str) -> None:
        """A hand of the talker's did not take effect and goes to the thinker now (:meth:`escalate`): its record (the
        newest of the talker's, written just before the hand-off) never counts as a finished request; the job (or the
        answer) it became decides. ``name``: the hand, so only a ``run_skill`` marks the latest skill run."""
        if self._escalated_hands is None:
            return   # a stand-in bridge: no hand records are read either
        now = time.time()
        for record in reversed(list(getattr(getattr(self, "actions", None), "_items", None) or [])):
            if getattr(record, "who", "") == "your hand":
                if now - float(getattr(record, "at", 0.0) or 0.0) <= self.ESCALATED_RECENT_S:
                    self._escalated_hands.append(record)
                break
        last = getattr(getattr(self, "hands", None), "last_skill", None)
        if name == "run_skill" and isinstance(last, dict):
            try:
                recent = now - float(last.get("at") or 0.0) <= self.ESCALATED_RECENT_S
            except (TypeError, ValueError):
                recent = False
            if recent:
                self._escalated_hands.append(last)

    def _new_job(self, request: str, *, source: str = "voice", with_queue: bool = True,
                 turn: tuple[int, int] | None = None) -> Job:
        """A new job of ``request``, stamped with the user's current turn (hands19 review L1: or ``turn``, the
        ``(conversation, turn)`` its hand-off came in on)."""
        if turn is not None:
            job = self._new_job(request, source=source, with_queue=with_queue)
            job.convo, job.user_turn = int(turn[0]), int(turn[1])
            return job
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
                   turn_id=turn_id, convo=self._convo())

    def _convo(self) -> int:
        """hands19: this conversation's number (the shared action log's: bumped as each conversation goes live)."""
        try:
            return int(getattr(self.actions, "convo", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _turn_now(self) -> tuple[int, int] | None:
        """hands19 (final-B all-browser R2, one-voice C): the user's current turn as ``(conversation, turn)``, else
        ``None``: the conversation's own turn count restarts at 0 in each one, so a job or an acknowledgement of an
        earlier conversation is never taken for this one's (compare these pairs, never the bare counts)."""
        if self.user_turns is None:
            return None
        try:
            return self._convo(), int(self.user_turns())
        except Exception:  # noqa: BLE001
            return None

    # -- the mouth's tools (voice thread) ---------------------------------------

    async def tell_yuki(self, params: Any) -> None:
        """Pipecat handler for ``tell_yuki`` (async: registered with ``cancel_on_interruption=False``), and for the lean
        talker's ``hand_over``. bTalk: under ``brain = "all"`` it is :meth:`_hand_over` (the exact words, by code).
        oVoice (review fix L3): never under ``brain = "one"`` (no hand-over: nothing is submitted)."""
        if await self._one_refuses(params, "tell_yuki"):
            return
        self._drop_step_line()   # sVoiceFix (F5): a hand-off answers a step check: no step line is owed any more
        if self.on_brain:
            await self._hand_over(params)
            return
        args = params.arguments if isinstance(params.arguments, dict) else {}
        context = str(args.get("context") or "").strip() or None
        corrects = str(args.get("corrects") or "").strip() or None
        # V4: the app or route the user named in their own words (the talker's structured field), else None
        app_named = " ".join(str(args.get("app_named") or "").split())[:80] or None
        intent = str(args.get("intent") or "new_task")
        heard = heard_words(params.context, skip=self.not_heard)
        remember = intent == "remember"
        screen = args.get("screen") is True
        acts = args.get("acts") is True
        with self._lock:
            early = self._early.pop(str(params.tool_call_id), None)
        if await self._claim_decides(params, args, intent, heard, early):
            return
        if early is None and intent in ("new_task", "remember") and self.adopt_routed(str(params.tool_call_id)):
            # E3a: the router already started the thinker on these words (the call's begin was not seen): adopted.
            with self._lock:
                early = self._early.pop(str(params.tool_call_id), None)
        replaced: list[Job] = []
        if intent == "instead":
            # The talker judged that this replaces work already handed over (a correction, the same target done
            # another way, a change of mind): that work is withdrawn now and this runs next, on the worker (v3.9).
            replaced = self._replace_targets(args.get("replaces"), besides=early)
            words = heard[-1] if heard else None   # N10: a withdrawal carries the user's own words
            for old in replaced:
                old.superseded = True   # Item 2a: at once, so its words are this request's again (exchange_floor)
                self._drop_items(old)
                self._gui(lambda old=old: self._withdraw(old, words=words))
            self._log("bridge", op="replace", call=params.tool_call_id, withdrawn=[j.job_id for j in replaced])
            intent = "new_task"
        # Phase S (D10), IF-words (Item 2a): the thinker gets the user's own words of the current exchange, never the
        # talker's paraphrase (after the withdrawals above: the words of a replaced request are this one's)
        request = exact_words(params.context, skip=self.not_heard, heard=heard, exchange=self)
        if early is not None and intent == "follow_up" and self._follow_up_target(besides=early) is not None:
            # Started early as a new task, but it adds to a task that is open after all: that task gets it.
            self._gui(lambda: self._cancel(early))
            self._log("bridge", op="early_dropped", why="follow_up", call=params.tool_call_id)
            early = None
        if early is not None:
            await self._adopt(early, params, args, context=context, corrects=corrects, remember=remember,
                              app_named=app_named)
            return
        if intent in ("new_task", "follow_up") and not remember and not replaced:
            same = self._same_turn_job()
            if same is not None and same.escalated:
                # v3.14: the talker's hand did not take effect and code already gave the thinker these very words
                # (``escalate``); the talker's own hand-off of them in the run that says "on it" adds nothing. Its
                # words are said, the call settles, and the running job is not steered with a copy of its request
                # (offline 29 Sept that copy held the job's done for one more round). Item 2(e): a follow-up of the
                # same words too (live 10 Oct the escalated job 3 was steered with its own request).
                await self._ack_if_silent(params, args)
                self._log("bridge", op="merged_escalated", job_id=same.job_id, call=params.tool_call_id,
                          intent=intent)
                from pipecat.frames.frames import FunctionCallResultProperties

                await params.result_callback({"status": "already being done for these words"},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
            if same is not None and intent == "new_task":
                # One job per request (v3.10): a second hand-off for the same user turn (the reply's own second call,
                # or a re-run of the same words) is that request again, never a second job saying a second outcome.
                intent = "follow_up"
                self._log("bridge", op="merged_same_turn", job_id=same.job_id, call=params.tool_call_id)
        done_before = self._done_text()
        said = spoken_words(params.context)
        first = say_first_words(args.get("say_first"))
        if first and first not in said:
            said.append(first)
        tabs = self.tabs_for_thinker()
        text = brain_request(request, heard, context=context, remember=remember, done_before=done_before,
                             corrects=corrects, said=said, tabs=tabs, app_named=app_named)
        await self._ack_if_silent(params, args)
        if intent == "follow_up":
            target = self._follow_up_target()
            if target is not None:
                given = text
                if target.state == WAITING and target.asked_at is not None:
                    # review r1 L3: their answer is the words since its question was said (as _hand_over cuts it), not
                    # the whole exchange again (the steers before the question already reached it)
                    answer = " ".join(self._turns(params.context, heard, after=target.asked_at))
                    if answer and answer != request:
                        given = brain_request(answer, heard, context=context, remember=remember,
                                              done_before=done_before, corrects=corrects, said=said, tabs=tabs,
                                              app_named=app_named)
                how = await self._follow_up(target, given)
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
                  words_chars=len(request), context_chars=len(context or ""), origin_hwnd=origin,
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
        if say_first_words(args.get("say_first")):
            return True
        try:
            return bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        except Exception:  # noqa: BLE001
            return False

    def _same_turn_job(self, turn: tuple[int, int] | None = None) -> Job | None:
        """An open voice job handed over for the user's current turn (the same request; hands19 review L1: or for
        ``turn``), else ``None``."""
        now = turn if turn is not None else self._turn_now()   # hands19: of this conversation only
        if now is None:
            return None
        convo, turn = now
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and not j.superseded and not j.remember
                    and j.convo == convo and j.user_turn == turn and turn > 0 and not j.unadopted]
        return max(live, key=lambda j: j.started_at) if live else None

    def turn_handed_over(self) -> bool:
        """vRound2: whether the user's current turn was handed to the thinker already and that job is still open
        (:meth:`_same_turn_job`): the hands' step check then hands nothing over again (a reply that brought a tab up
        and called hand_over beside it would otherwise start a second job for the same words). State only."""
        return self._same_turn_job() is not None

    #: pVoice: how recent the request before this one must be for :meth:`last_not_done` (s).
    LAST_NOT_DONE_S = 300.0

    def last_not_done(self) -> str | None:
        """pVoice: the outcome of this conversation's newest finished spoken request when it really failed (its state
        ``FAILED`` or its verdict ``failed``, or ``unverified`` when it was asked to act and nothing acted), within
        :attr:`LAST_NOT_DONE_S`, else ``None``: the hands hand an open rest of a request on with it ("try another
        way" after the playlist run that did not get there), so the thinker never redoes that way. Review r1
        M2: an answered question (``answer``), a request with no verdict and any other ending are no failure (the
        thinker is never told a false "ended not done"). Its own words (content: RAM only, never logged). Not one the
        user stopped or a newer request replaced. The job table's states and verdicts only."""
        now = time.monotonic()
        with self._lock:
            done = list(self._finished)
        for job in reversed(done):
            if job.source != "voice" or job.remember or job.unadopted:
                continue
            if job.finished_at is None or now - job.finished_at > self.LAST_NOT_DONE_S:
                return None
            if job.superseded or job.state == CANCELLED or job.paused_out:
                return None
            verdict = job.verdict if isinstance(job.verdict, dict) else {}
            level = str(verdict.get("level") or "")
            failed = job.state == FAILED or level == "failed" or (
                level == "unverified" and job.acts and not verdict.get("acted"))
            if not failed:
                return None
            outcome = " ".join(str(job.outcome or "").split())
            return outcome[:300] or None
        return None

    def turn_had_job(self) -> bool:
        """pVoice review r1 L2: whether a spoken job (open or finished; never one to keep) was made for the user's
        current turn: their request went to the thinker, so a ``then`` the hands still hold for this turn is closed
        (never a second job for the same words). State only."""
        now = self._turn_now()   # hands19: of this conversation only
        if now is None or now[1] <= 0:
            return False
        with self._lock:
            jobs = list(self._jobs.values()) + list(self._unsubmitted.values()) + list(self._finished)
        return any(j.source == "voice" and not j.remember and not j.unadopted and (j.convo, j.user_turn) == now
                   for j in jobs)

    async def note_planned(self, text: str) -> bool:
        """lVoice review r1 L2: a labelled note (code's words: a part of the reply the voice planned and did not run,
        once the thinker took their whole request) for the open job made for the user's current turn
        (:meth:`_same_turn_job`): steered into it while it runs, or kept for when it starts. Never routed as the answer
        to a question it asks (a job waiting on one is left alone), never a new job. ``True`` when it went. Logged
        content-free. Never raises."""
        job = self._same_turn_job()
        if job is None or not text or job.state == WAITING:
            return False
        loop = asyncio.get_running_loop()
        decided: asyncio.Future = loop.create_future()

        note = "(From Yuki's voice, about the request you are on) " + " ".join(str(text).split())

        def route() -> None:
            how = "none"
            try:
                if job.closing is None and job.state in OPEN_STATES and job.state != WAITING:
                    if job.job_id is not None and job.state == RUNNING:
                        self._steer(job, job.job_id, note)   # as an adopted job's note is steered in (_adopt)
                        how = "added"
                    elif job.state == QUEUED:
                        job.pending_steers.append(note)   # not started yet: delivered when it starts (_on_started)
                        how = "queued"
            except Exception:  # noqa: BLE001
                how = "none"
            loop.call_soon_threadsafe(lambda: decided.done() or decided.set_result(how))

        self._gui(route)
        try:
            how = await asyncio.wait_for(decided, timeout=5.0)
        except asyncio.TimeoutError:
            how = "none"
        self._log("bridge", op="note_planned", token=job.token, how=how, chars=len(text))
        return how != "none"

    async def _ack_if_silent(self, params: Any, args: dict[str, Any]) -> None:
        """Say the call's ``say_first`` words now when the reply that made it said nothing aloud.

        The mouth's words normally come before the call in its reply and are already
        being spoken; measured 2026-09-25 (live and in a text probe, 5 of 5), it also
        hands over with no words at all, and the user heard silence until the first
        progress line 11 s later. Whether the reply had words is a fact about the stream
        (:attr:`spoke_in_reply`), not about what they say; the words are the mouth's own.
        """
        words = say_first_words(args.get("say_first"))   # sVoiceFix (F6): never a call run into the string, whole
        if self.said_early is not None and self.said_early(str(params.tool_call_id)):
            return  # already said while the mouth was writing the call (v3.5)
        spoke = self.spoke_in_reply() if self.spoke_in_reply is not None else True
        if spoke or not words:
            return
        name = str(getattr(params, "function_name", "") or "")
        if name in GAME_GATED_SAYS:
            try:
                waits = await asyncio.to_thread(say_waits_for_game, name, args)
            except Exception:  # noqa: BLE001 - no read: as before
                waits = False
            if waits:
                # voice22 (A25): a game is in front and the gate holds this act: its game line (or its done line,
                # when it went after all) is what they hear, never an "Opening it." first
                self._log("bridge", op="ack_skipped", call=params.tool_call_id, why="game")
                return
        if not self.may_ack():
            self._log("bridge", op="ack_skipped", call=params.tool_call_id, why="already_acknowledged")
            return
        from pipecat.frames.frames import TTSSpeakFrame

        self.acked()
        await params.llm.push_frame(TTSSpeakFrame(text=words))
        if self.mark_spoken is not None:
            self.mark_spoken(params.tool_call_id)
        self._acked_words[str(params.tool_call_id)] = words   # fc (C5, K-D): the words said for this call
        if len(self._acked_words) > 32:
            for old in list(self._acked_words)[:16]:
                del self._acked_words[old]
        self._log("bridge", op="ack_spoken", call=params.tool_call_id, chars=len(words))

    def early_said(self, tool_id: Any) -> str:
        """fc (C5, contract K-D): the words already spoken for this call's say_first (said while the call was being
        written, or by code right before it ran: :meth:`_ack_if_silent`), else ``""`` (nothing said for it: a done line
        is said as now). A reader skips a done line that is these same words (two of Yuki's own lines compared, never
        the user's). Any thread; never raises."""
        key = str(tool_id or "")
        if not key:
            return ""
        words = ""
        fn = self.early_words
        if fn is not None:
            try:
                words = str(fn(key) or "")
            except Exception:  # noqa: BLE001 - nothing known: the line is said as now
                words = ""
        if not words:
            words = str(self._acked_words.get(key, "") or "")
        return " ".join(words.split())

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
            if kind in ("done", "failed", "withdrawn", "set_aside") and self.steps is not None:
                self.steps.closed(job.token)
            self._log("mouth_tool_result", job_id=job.job_id, result=kind, final=kind in ("done", "failed"),
                      chars=len(str(payload)))
            if kind == "set_aside":
                # Item 2(d): the runtime set it aside on its unanswered question (v3.14b): it ends quietly, as it is
                self._drop_items(job)
                await params.result_callback({"set_aside": job.brief(), "how_to_handle_it": SET_ASIDE_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                self._log("bridge", op="set_aside_settled", job_id=job.job_id)
                return
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
            held_keep = kind == "done" and job.remember and isinstance(job.verdict, dict) and (
                job.verdict.get("held_keep") is True or job.verdict.get("awaits") is True)
            if held_keep:
                # integ30 (S4, contract K-R): the planner's shared handler held a keep of this request to ask first (a
                # code fact on its outcome; or the planner marked its ending as waiting for their answer): nothing was
                # saved and its ending is that question, so it takes the spoken path below (the talker says it; a
                # remember job is never said by code), never the quiet "saved"
                self._log("bridge", op="keep_held", job_id=job.job_id)
            if kind == "done" and job.remember and not held_keep:
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
                if kind == "done":
                    # P1.3: the brain's tidy-up said the request's line first; only what is new is said now
                    payload, first = self._said_first(job, payload)
                    if first and not payload:   # all of it was heard already: settled as said, no talker run
                        self._outcome_said(job)
                        await params.result_callback({"done": first, "how_to_handle_it": RESULT_SAID_NOTE},
                                                     properties=FunctionCallResultProperties(run_llm=False))
                        return
                held = self._held_verdict(job, kind, payload)   # honest (87): below likely, never said as written
                verified = bool(kind == "done" and job.finished_by_action
                                and payload.strip() == job.finished_by_action) and held is None
                head, rest, by_rule = self._condensed(job, payload)   # voice20 (S22g): an answer is never cut by count
                job.rest = rest
                delivered = loop.create_future()
                nothing = self._nothing_done(job) if kind == "done" else False
                lines = None if held is not None else self._by_code(job, kind, head, rest, late=late,
                                                                    verified=verified, nothing_done=nothing,
                                                                    by_rule=by_rule)
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
                                                                nothing_done=nothing, outcome=job.verdict,
                                                                lean=self._lean),
                                                 properties=FunctionCallResultProperties(run_llm=run_llm))
                    if not delivered.done():
                        delivered.set_result(stale)

                self._log("bridge", op="condensed", job_id=job.job_id, chars=len(payload), spoken_chars=len(head),
                          rest_chars=len(rest), late=late, verified=verified, said_by="talker")
                await self._offer(job, "final", final)
                await delivered
                return
            if kind == "ask":
                # Item 2(b): the thinker's question is said by code, word for word, like a done line (live 10 Oct the
                # talker reworded "Did you mean Hemanth?" into "Yeah, confirming, messaging Hemanth")
                async def settle_ask(body: dict[str, Any], run_llm: bool) -> None:
                    await params.result_callback(body, properties=FunctionCallResultProperties(
                        is_final=False, run_llm=run_llm))

                spoken = self._ask_by_code(job, str(payload or ""), settle_ask,
                                           push=getattr(getattr(params, "llm", None), "push_frame", None))
                if spoken is not None:
                    await self._offer(job, "ask", spoken, self_spoken=True)
                    continue
            if kind == "waiting":
                body: dict[str, Any] = waiting_payload(job.brief(200), *payload)
            elif kind == "say":
                body = {"yuki_says": payload}
            elif kind == "ask":
                body = {"yuki_asks": payload}   # an address or a path in it: the talker describes it (or no voice)
            else:
                body = {"status": payload}

            async def intermediate(run_llm: bool, stale: bool, body=body, kind=kind, payload=payload) -> None:
                del stale
                if run_llm and kind == "ask":
                    self.said_owed_at = time.monotonic()
                if kind == "ask":
                    job.asked_at = time.monotonic()
                    self._owe(job, "ask", str(payload))   # a question of the thinker's is owed too (v3.16)
                if run_llm:
                    self._waiting_run = (job, time.monotonic()) if kind == "waiting" else None
                await params.result_callback(body, properties=FunctionCallResultProperties(
                    is_final=False, run_llm=run_llm))

            await self._offer(job, kind, intermediate)

    def _ask_by_code(self, job: Job, question: str, settle: Callable[[dict[str, Any], bool], Awaitable[None]], *,
                     push: Callable[[Any], Awaitable[None]] | None) -> Callable[[bool, bool], Awaitable[None]] | None:
        """Item 2(b): the narrator item's deliver for a thinker's question said by code, word for word, in Yuki's one
        voice (no talker run, never reworded into a claim), or ``None`` when it cannot be: no words, an address or a
        path in it (a voice describes those, :func:`machine_text`), or nothing here can put words into the voice (a
        bare stand-in). The question goes into the talker's conversation as said (:data:`ASKED_NOTE`) and stays owed
        until heard (the ledger says it again if not). Facts of the text's machine formats only, never its meaning."""
        text = " ".join(question.split())
        if not strip_tags(text).strip() or machine_text(text):
            return None
        if push is None and self.speak_result is None and self.speak is None:
            return None
        lines = sentences(text) or [text]

        async def deliver(run_llm: bool, stale: bool) -> None:
            del run_llm, stale   # it speaks itself: the talker is never run for it; a question is never stale
            if job.superseded or job.cancel_requested or job.state != WAITING:
                await settle({"status": "That question is no longer open."}, False)
                return
            job.asked_at = time.monotonic()
            self._waiting_run = None
            self._owe(job, "ask", text)
            if self.ledger is not None:
                try:
                    self.ledger.said_by_code(job.token)
                except Exception:  # noqa: BLE001 - the ledger never breaks the bridge
                    pass
            await self._say_lines(lines, push)
            await settle({"asked": text, "said": True, "how_to_handle_it": ASKED_NOTE}, False)
            self._log("bridge", op="asked_by_code", job_id=job.job_id, sentences=len(lines), chars=len(text))

        return deliver

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

    def _said_first(self, job: Job, text: str) -> tuple[str, str]:
        """P1.3 (qBrain merge note 6): a done whose start the brain already put out as a say line says only what is
        new. The brain's tidy-up turn says the request's line at once (a :class:`Say`, ``outcome["said_first"]``), and
        its Final is that line then the tidy words. ``(text, first)``: when that line went to the voice as this job's
        last say line and the done starts with it, ``first`` is the line and ``text`` the rest (``""``: all of it was
        heard); otherwise ``first`` is ``""`` and ``text`` is as given, so a say line that was dropped (talked over, a
        repeat, a question first) is never lost. String identity of code's own lines only, never their meaning."""
        verdict = job.verdict if isinstance(job.verdict, dict) else {}
        first = " ".join(str(verdict.get("said_first") or "").split())
        heard = " ".join(str(job.last_say or "").split())   # step_said sets it once the say line went to the voice
        body = " ".join(str(text or "").split())
        if not first or heard.casefold() != first.casefold() or not body.casefold().startswith(first.casefold()):
            if first:
                self._log("bridge", op="said_first", job_id=job.job_id, heard=False, chars=len(body))
            return text, ""
        rest = body[len(first):].strip()
        self._log("bridge", op="said_first", job_id=job.job_id, heard=True, chars=len(first), rest_chars=len(rest))
        return rest, first

    async def _offer(self, job: Job, kind: str, deliver: Callable[[bool, bool], Awaitable[None]],
                     drop: Callable[[], None] | None = None, *, self_spoken: bool = False) -> None:
        gate = self.gate
        if gate is None:
            await deliver(True, False)
            return
        gate.enqueue(Item(job=job.token, kind=kind, deliver=deliver, drop=drop, self_spoken=self_spoken))

    # -- the early hand-off (v3.6) ------------------------------------------------

    def begin_early(self, call_id: str, heard: list[str], screen: bool = False,
                    said: list[str] | None = None, *, routed: bool = False, words: str | None = None,
                    turns: list[str] | None = None, convo: Any = None) -> bool:
        """The mouth's reply has just begun a tell_yuki: submit the request now, on the user's own words.

        On the voice loop, from the reply's stream (``Conversation._early_handoff``). The request is what the
        user said (Phase S: tell_yuki has no wording of its own any more, D10); when the call is
        complete, :meth:`tell_yuki` adopts this job, adding the call's ``context`` / ``corrects`` if it has
        them. ``screen``: the talker's mark (the call's first field) that the request is about something inside
        the window in front: the thinker gets that window read as it starts (v3.9). Returns whether a job was
        submitted.

        ``routed`` (E3a, v3.17): started by the router before the talker decided (``call_id`` is the router's own key,
        :data:`ROUTE_KEY`); the talker's tell_yuki adopts it (:meth:`adopt_routed`), anything else withdraws it unseen.
        ``words`` (Phase S, D10; Item 2a): the user's turns of the current exchange (:func:`exact_words`), the
        request's first line; the latest heard turn without them.

        kMind: never under ``brain = "all"``: the lean talker's hand_over starts when its call is complete, so the
        talker's understanding goes with the request (contract C3). ``turns`` / ``convo`` are unused there.
        """
        del turns, convo
        heard = [h for h in (heard or []) if h and h.strip()]
        if not heard or self._loop is None or self.on_brain:
            return False
        request = " ".join(str(words or "").split()) or heard[-1].strip()
        done_before = self._done_text()
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
                  words_chars=len(request), context_chars=0, origin_hwnd=origin, done_before_chars=len(done_before),
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
            extra.append(f"[References in their words, as the conversation had them: {context}]")
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

        kBrain (18 Oct): under ``brain = "all"`` the new brain's planner is warmed instead (``runtime.warm_brain``: its
        cached prefix and connection, a no-op when warm, retried after a failed warm-up), with the same guards (not
        within ``idle_s`` of the last warm-up from here, never while it works), off the voice loop.
        """
        if getattr(self, "one", False):
            return False   # oVoice: no thinker or planner behind the voice (the ONE agent's cache is the talker's own)
        if self.on_brain:
            warm_planner = getattr(self.runtime, "warm_brain", None)
            if not callable(warm_planner):
                return False
            if time.monotonic() - self._brain_warmed_at < idle_s or self.brain_busy():
                return False
            self._brain_warmed_at = time.monotonic()

            def run_planner() -> None:
                started = time.monotonic()
                try:
                    ok = bool(warm_planner())
                except Exception as exc:  # noqa: BLE001 - only an optimisation
                    ok = f"{type(exc).__name__}"
                self._log("brain_warm", ms=round((time.monotonic() - started) * 1000), ok=ok, brain=True)

            from yuki.voice.workers import shared

            shared().submit(run_planner)   # the runtime may make the brain first: never on the voice loop
            return True
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
        """Pipecat handler for ``check_progress``: an isolated one-shot answer over the task's real log. oVoice (review
        fix L3): never under ``brain = "one"`` (no thinker's task to ask about)."""
        if await self._one_refuses(params, "check_progress"):
            return
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
        self._not_an_answer(params, "progress")   # Item 2(c): a question about the work, not the waiting job's answer
        if self.spoke_in_reply is not None and self.spoke_in_reply():
            # The reply already answered in words (from the thinker line, v3.9): a second answer to one question
            # would follow it (measured offline: "Searching for a jazz mix." then "Opening a YouTube link for the
            # jazz mix."). A fact about the stream, never about the words. Settled quietly.
            from pipecat.frames.frames import FunctionCallResultProperties

            self._log("bridge", op="progress", refused="already_answered")
            await params.result_callback({"progress": None, "note": "You already answered them in this reply."},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if self.on_brain:
            # bTalk (design 3): no progress model call; the running plan's status lines, by code (the work line)
            line = self.work_line()
            self._log("bridge", op="progress", brain=True, answered=line is not None)
            await params.result_callback({"progress": line or "Nothing is running or recently finished.",
                                          "how_to_say_it": "Say what it is doing now in a few words of your own, "
                                                           "adding nothing."})
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
        if self.user_turn is None or self.user_turn():
            self._not_an_answer(params, "more")   # Item 2(c): they asked for more of a result, not the job's answer
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
            if getattr(self, "one", False):
                # oVoice: no thinker to hand to; the ONE agent reads more with its own tools
                await params.result_callback({"more": None, "note": (
                    "Nothing of the last result was held back. If they want more detail, read it with your tools.")})
                return
            await params.result_callback({"more": None, "note": (
                "Nothing of the last result was held back. If they want more detail, that is a new "
                f"question for the thinker: hand it over with {self._handoff_tool} in their words.")})
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

    def _drop_step_line(self) -> None:
        """sVoiceFix (F5): the hands' owed step line, dropped (something other than silence answered the step)."""
        hands = getattr(self, "hands", None)
        if hands is not None:
            try:
                hands._step_owed = None
            except Exception:  # noqa: BLE001 - a stand-in without it
                pass

    async def _say_step_line(self, params: Any) -> bool:
        """sVoiceFix (F5): a run a silent window or tab step started (its step check) ends in stay_quiet with no words:
        the step's own line of what is showing now is said by code (live-like 16 Oct sim: "put something on YouTube on
        Arc" showed Arc's old tab and then nothing at all, 5 of 5 runs). The step check says that line at once as a rule
        (review r1 M1); this is the backstop for one it could not say then. ``True`` when said; the call is settled."""
        hands = getattr(self, "hands", None)
        take = getattr(hands, "take_step_line", None) if hands is not None else None
        line = take() if callable(take) else None
        push = getattr(getattr(params, "llm", None), "push_frame", None)
        if not line or push is None:
            return False
        from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

        await push(TTSSpeakFrame(text=line))
        if self.mark_spoken is not None:
            self.mark_spoken(params.tool_call_id)
        self._log("bridge", op="quiet", said_by="step_line", chars=len(line))
        await params.result_callback({"ok": True, "note": f"Said aloud for you just now: {line} Say nothing more."},
                                     properties=FunctionCallResultProperties(run_llm=False))
        return True

    async def stay_quiet(self, params: Any) -> None:
        """Pipecat handler for ``stay_quiet``: the mouth chose to say nothing; settled without a turn."""
        from pipecat.frames.frames import FunctionCallResultProperties

        by_user = self.user_turn() if self.user_turn is not None else None
        in_call = bool(self.call_mode()) if self.call_mode is not None else False
        if by_user and in_call:
            # Item 2(c), review r1 M1: only in a call is a quiet on the user's turn a judgement that the words were for
            # the others. Outside a call the talker's prompt answers "yeah" with stay_quiet while an outcome is on its
            # way; after "Did you mean Hemanth?" that "yeah" is the waiting job's answer and still goes to it (the plan
            # lets only a new_task / instead hand-off take a turn from the waiting job)
            self._not_an_answer(params, "quiet_in_call")
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
        hands = getattr(self, "hands", None)
        # oVoice: under one the hands' then/rest choreography never runs (the ONE agent ends its own turn)
        rest = getattr(hands, "quiet_rest", None) if hands is not None and not getattr(self, "one", False) else None
        if not by_user and not spoke_now and callable(rest) and await rest(params):
            # pVoice: a run a "then" started (the rest of their request still to do) ended in quiet with no hand: the
            # rest went to the thinker by code, never silence (the hands settled the call)
            self._drop_step_line()
            self._log("bridge", op="quiet", said_by="rest_handed_on")
            return
        if not by_user and not spoke_now and await self._say_step_line(params):
            return   # sVoiceFix (F5): never silence after a step that only brought something up
        if by_user or spoke_now:
            self._drop_step_line()
        if by_user and not in_call and not getattr(self, "one", False) and self.opened_quiet is not None:
            # voice21 (final-D S10, the user's decision 2 of 19 Oct): the one exception to V2 below. A quiet on the
            # first words of a conversation they opened themselves (the wake word, the hotkey, the tray or a card), with
            # no call, no dictation and nothing heard yet, is refused once (live final-D q_discord_today: unmarked
            # scratch and stay_quiet on a question to Yuki); the talker runs once more. A second quiet is final, and
            # "go away" still ends it (end_conversation). Under one, OneVoice.control decides.
            try:
                refused = self.opened_quiet(spoke_now, getattr(params, "tool_call_id", None))
            except Exception:  # noqa: BLE001 - no reading: the quiet stands
                refused = None
            if refused:
                self._log("bridge", op="quiet", refused="opened_first", asked_by_user=True)
                await params.result_callback(dict(refused) if isinstance(refused, dict)
                                             else {"not_quiet": OPENED_QUIET})
                return
        if (by_user and not in_call and not spoke_now and not getattr(self, "one", False)
                and self.follow_quiet is not None):
            # la A2 (final-H2 kw_offer_then_yes t2: "Yeah, go ahead and do that." right after Yuki's offer got a lone
            # stay_quiet, and the conversation ended in silence): ONE's follow-on refusal under all. The talker's own
            # first quiet on words that follow Yuki's own heard line is refused once with ONE's note (answer; a
            # sign-off or "go away" ends with end_conversation); the run its result starts answers, and its own quiet
            # is final (V2 below). Stream facts only (Conversation._follow_quiet).
            try:
                refused = self.follow_quiet(spoke_now, getattr(params, "tool_call_id", None))
            except Exception:  # noqa: BLE001 - no reading: the quiet stands
                refused = None
            if refused:
                self._log("bridge", op="quiet", refused="follow_on", asked_by_user=True)
                await params.result_callback(dict(refused))
                return
        # V2 (3 Oct): a stay_quiet on the user's own words is final. It used to be refused once after the name (v3.6)
        # and once in a live exchange (v3.9); live 3 Oct "go away" got a quiet, the refusal made the talker run again,
        # and it said it was still there. Whom the words were for is the talker's judgement (the prompt: outside a
        # call they are for Yuki unless plainly not), and code never makes it speak after it chose silence.
        # voice21: but for the S10 refusal above, once a conversation; la A2: and the follow-on refusal, once a turn.
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
            if call is not None and call[0] in ("tell_yuki", "hand_over") and await self._run_held(call):
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
        words = say_first_words(args.get("say_first"))
        spoke = bool(self.spoke_in_reply()) if self.spoke_in_reply is not None else False
        self._drop_claim("ended")   # Item 2(c): "shut up" is never the answer to a waiting question
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
        self._drop_claim("quiet_requested")   # Item 2(c): never the answer to a waiting question
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
        """The most recent unfinished voice job, the one waiting for an answer first (``besides``: not that one). A
        remember job is never one (integ29, md merge note 2b: its note says nothing else is asked, so a follow_up in
        the same reply as a remember would be lost there; as :meth:`_same_turn_job`)."""
        with self._lock:
            live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                    if j.source == "voice" and j.state in OPEN_STATES and j is not besides and not j.unadopted
                    and not j.remember]
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
                app_named: str | None = None, skill: dict[str, Any] | None = None,
                context: str | None = None, understanding: dict[str, Any] | None = None) -> None:
        extra: dict[str, Any] = {}
        if understanding:
            # kMind / kBrain (contract C3): the talker's reading of the words, a seed for the brain (their words win);
            # AgentRuntime.submit takes it as its own field
            extra["understanding"] = dict(understanding)
        if context and self._takes("context"):
            # bTalk: the new brain's labelled context (code facts and earlier turns), never quotable as their words
            extra["context"] = context
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
        if skill and str(skill.get("key") or ""):
            job.skills.add(str(skill.get("key")))   # review r2 L1: its own skill (the key, data only)
        if skill and self._takes("skill"):
            # Phase S: the user's usual way the code handed over (rung C), pinned in the thinker's frame
            extra["skill"] = dict(skill)
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
        worker is idle (a pack is built by the thread-safe reads of an idle agent); one at a time; returns at once.

        bTalk (``brain = "all"``): the new brain's request turn read ahead instead (:meth:`_brain_prefetch`).

        IF-words (Item 2a): every final turn is noted in :attr:`heard_log` here first, with when it was heard (the clock
        the exchange is cut by), under every brain setting. It is not offered to a waiting question here: this hook
        also sees the turns the fast path takes (the name alone), which never reach the talker and are nobody's answer;
        the pipeline offers the turns that do reach it (:meth:`answer_waiting`, IF-answer, Item 3)."""
        if self.heard_log is not None:
            self.heard_log.note(words)
        if getattr(self, "one", False):
            return   # oVoice: no hand-over, so no thinker's request read ahead (no old-thinker pack either)
        if self.on_brain:
            self._brain_prefetch(words)
            return
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
        self._note_runner(job, lane)
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
        # hands19 (C4): a countdown their speech paused (``paused``) is still open: their words now are for it
        state = send.get("state")
        job.sending = (" ".join(str(send.get("who") or "").split()) or "them") \
            if state in ("counting", "paused") else None
        # review: a "counting" that comes after a newer pause was asked for is still that pause's (its "paused" follows)
        job.send_held = state == "paused" or (state == "counting" and job.send_held)
        if state == "going":
            job.sent = True   # Item 2 review r1 M2: it went (or may have): its end closes the exchange (rule 8)
        self._log("bridge", op="send_countdown", job_id=job_id, state=state)
        if (state == "counting" and not job.send_held and job.source == "voice" and self.on_brain
                and not getattr(self, "one", False) and self._loop is not None and self._user_speaking()):
            # review M3: it opened (or counts on) while they are speaking: it pauses at once, their words decide, as
            # brain one's countdown does
            self._hold_send(job, "spoke_first", self.heard_log.newest() if self.heard_log is not None else None)
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
        self._note_runner(job, lane)
        if name == "run_skill" and str((tool_input or {}).get("skill") or "").strip():
            job.skills.add(str(tool_input["skill"]).strip())   # review r2 L1: a skill it ran (its key, data only)
        if name == "tasks" and isinstance((tool_input or {}).get("items"), list):
            # N10: the kept list rides on the thinker line (what is left), so an interjection never loses it
            job.tasks = [dict(i) for i in tool_input["items"] if isinstance(i, dict)][:12]
            self._changed()
            return
        job.acting = _action_words(name, tool_input)
        job.acting_on = _acting_on(name, tool_input)   # lVoice: the window / tab its effect sits on (identities)
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
            hwnd, tab = (getattr(job, "acting_on", None) or (None, "")) if job is not None else (None, "")
            try:
                try:
                    own = job is not None and job.convo == self._convo()   # hands19: a turn of this conversation
                    self.actions.add("the thinker", what, summary[:400], ok=bool(ok), verified=None if ok else False,
                                     turn=job.user_turn if own else None, hwnd=hwnd, tab_id=tab)
                except TypeError:   # an older log without the identities
                    self.actions.add("the thinker", what, summary[:400], ok=bool(ok), verified=None if ok else False)
            except Exception as exc:  # noqa: BLE001 - a record never breaks the thinker's step (content-free log)
                self._log("bridge", op="action_record", job_id=job_id, ledger_error=type(exc).__name__)
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
        job.asked_at = None   # set when the question is said (Item 2c: only a question they heard takes an answer)
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
        self._note_runner(job, lane)
        job.verdict = dict(verdict)
        if job.log is not None and verdict.get("label"):
            job.log.add("checked", f"{verdict.get('label')}: {verdict.get('seen') or ''}".rstrip(": "))
        if verdict.get("acted") and verdict.get("label"):
            # review (items not done): the thinker's steps reach the shared action ledger as "not verified" one by one
            # (a step's level is not on its event); the request's one verdict closes them, so the talker's desktop
            # block and the next hand-off read the checked level, never only the step marks
            level = str(verdict.get("level") or "")
            try:
                self.actions.add("the thinker", f'the request "{job.brief()}", as a whole',
                                 f"{verdict.get('label')}: {verdict.get('seen') or ''}".rstrip(": "),
                                 ok=level != "failed",
                                 verified=True if level == "verified" else False if level == "failed" else None)
            except Exception as exc:  # noqa: BLE001 - a record never breaks the verdict (content-free log)
                self._log("bridge", op="action_record", job_id=job_id, ledger_error=type(exc).__name__)
        self._log("bridge", op="outcome", job_id=job_id, level=verdict.get("level"), acted=verdict.get("acted"),
                  send=verdict.get("send"))

    def _held_verdict(self, job: Job, kind: str, text: str | None = None) -> dict[str, Any] | None:
        """honest (87): the verdict of a done whose words may not be said as written (below likely), else ``None``.

        bTalk (review r1 M1): under ``brain = "all"`` the gate is off only for words that are code's own line: a job
        seen running on the new brain (its words after an act are the probe's line, never a claim), or words that are
        the outcome's own ``line`` word for word. The old loop serving voice (before the runtime patch, or its
        fallback) keeps the gate: its closing words are the model's."""
        from yuki.agent.finish import sayable

        if self.on_brain and (job.brain_run or verdict_line(job.verdict, text)):
            return None
        if kind != "done" or sayable(job.verdict):
            return None
        return job.verdict

    def _owed_words(self, job: Job, kind: str, head: str) -> str:
        """What the owed ledger says by code if the talker's run for a result is not heard: the thinker's words, or for
        an outcome below likely the verdict's own fixed line (``I tried to send it, but I couldn't confirm it went.``),
        never the thinker's claim (honest, report 87)."""
        from yuki.agent.finish import LINE_NOT_CHECKED, LINE_NOTHING_DONE

        held = self._held_verdict(job, kind, head)
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

    def _on_parked(self, lane: str, job_id: int, text: str) -> None:
        """v3.14b: the runtime set the job aside on a question nobody answered (new work waited on its lane, or it went
        unanswered for ``ask_park_s``). Item 2(d): no longer withdrawn by the bridge when a hand escalates, a waiting
        job can be set aside this way; it then ends quietly (``set_aside``), never as "It stopped without saying
        anything" (its lane's ``lane_done`` follows)."""
        job = self._job(job_id)
        if job is not None:
            job.closing = ("set_aside", text)
            if job.log is not None:
                job.log.add("set aside", "its question went unanswered while other work waited")
            self._log("bridge", op="set_aside", job_id=job_id)

    def _on_lane_done(self, lane: str, job_id: int, request: str, needed_hands: bool, refused: list) -> None:
        job = self._job(job_id)
        if job is None:
            return
        if needed_hands:
            return  # the front desk passed it to the worker: handed_off follows with the new id
        kind, text = job.closing or ("failed", "It stopped without saying anything.")
        state = DONE if kind == "done" else (CANCELLED if (job.cancel_requested or kind in ("withdrawn", "set_aside"))
                                             else FAILED)
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
                job.brain_run = False   # a new run: who drives it is seen again
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
                job.brain_run = False   # a new run: who drives it is seen again
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
        if self.on_brain:
            # bTalk: their words alone as their words; the note as labelled context
            self._submit(follow, "\n".join(texts), None, user_words=[str(t) for t in texts],
                         context=f"{LATE_ADDED}\n\n{self._done_block()}".strip())
        else:
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
        # hands19 (all-browser R2): by ``(conversation, turn)``: a job of an earlier conversation's turn 2 is never
        # newer than this conversation's turn 1 (k_resume_anime's outcome was folded by one two conversations back)
        return any(o is not job and o.source == "voice" and not o.remember
                   and (o.convo, o.user_turn) > (job.convo, job.user_turn) and not o.unadopted
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

    def _answer_for_words(self) -> bool:
        """Integration F3 (voice21 S10): a task handed over for the user's current words (this conversation's current
        turn, ``(convo, user_turn)`` as :meth:`_turn_now` reads it) is queued or running, so its outcome will answer
        them. A job of an earlier turn or conversation never counts (the bridge outlives conversations). The turn
        unread: :meth:`_answer_on_its_way` (unsure: on its way)."""
        now = self._turn_now()
        if now is None:
            return self._answer_on_its_way()
        with self._lock:
            jobs = list(self._jobs.values())
        return any(j.source == "voice" and not j.remember and j.state in (QUEUED, RUNNING) and not j.unadopted
                   and (j.convo, j.user_turn) == now for j in jobs)

    def _outcome_said(self, job: Job) -> None:
        """The outcome of what they asked last is being said: no acknowledgement may come after it (v3.13)."""
        now = self._turn_now()
        if now is not None and now[1] >= 0 and (job.convo, job.user_turn) == now:
            self._acked_turn = now   # hands19: ``(conversation, turn)``

    # -- one acknowledgement per request (v3.12) --------------------------------------------------

    def may_ack(self) -> bool:
        """Whether an acknowledgement may still be said for the user's current words (one per request: live 13:31:31
        "My bad, opening DevMafex now. Opening it. Let me open that for you." were three for one)."""
        now = self._turn_now()
        return now is None or now[1] < 0 or now != self._acked_turn   # hands19: this conversation's turn only

    def acked(self) -> None:
        """Something was said for the user's current words (an acknowledgement or the reply's own words)."""
        now = self._turn_now()
        self._acked_turn = now if now is not None else -1   # hands19: ``(conversation, turn)``

    def _moved_on(self, job: Job) -> bool:
        """Whether the user has said more since this job was handed over (its result is then a late one)."""
        now = self._turn_now()
        try:
            return now is not None and now > (int(job.convo), int(job.user_turn))   # hands19: a later conversation too
        except (TypeError, ValueError):
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

    def _condensed(self, job: Job, payload: str) -> tuple[str, str, str]:
        """``(head, rest, by_rule)``: a result's words to say now and what is held back (``condense``), and the rest
        the sentence rule alone would hold back (:meth:`_by_code` decides who says it from that, as before).

        voice20 (final-D S22g, questions Y2): an answer (the outcome's level ``answer``: the planner answered, it did
        no act for it) is cut by characters only, never by the sentence count: live final-D k_two_asks under all, a
        173-character answer of three sentences was cut to its first two and the sentence that answered (the usual
        meeting) went to "more". The outcome's own level and the text's sentence marks, never the words."""
        head, rest = condense(payload, max_sentences=self._max_sentences, max_chars=self._max_chars)
        level = str((job.verdict or {}).get("level") or "") if isinstance(job.verdict, dict) else ""
        if level != "answer" or not rest:
            return head, rest, rest
        whole, left = condense(payload, max_sentences=max(self._max_sentences, len(sentences(payload))),
                               max_chars=self._max_chars)
        if left != rest:
            self._log("bridge", op="answer_whole", job_id=job.job_id, spoken_chars=len(whole), rest_chars=len(left),
                      by_rule_chars=len(head))
        return whole, left, rest

    def _by_code(self, job: Job, kind: str, head: str, rest: str, *, late: bool, verified: bool,
                 nothing_done: bool, by_rule: str | None = None) -> list[str] | None:
        """The sentences of a result to say by code, word for word, with no talker run, or ``None`` (the talker says it).

        07 §6 C: a verified ``finishes_with`` result, or a ``done`` that fits the condense rule whole (``voice_result_
        sentences`` / ``voice_result_chars``: nothing held back), is the thinker's own spoken line already (its prompt
        says its done message is heard in Yuki's voice). The talker gets it when judgement is still needed: a failure,
        a result with more to offer, one that asks them something (its own closing question mark), one with an address
        or a path to describe rather than read, a late one (they said more since: it must be framed as the earlier
        request's), and one for a request that asked for something to be done when nothing was (v3.14b). Facts of the
        job's record and the text's own punctuation and machine formats, never what the words mean. voice20:
        ``by_rule`` (:meth:`_condensed`): what the sentence rule alone holds back, for "fits whole" (an answer's head
        may be longer now; who says it is decided as before).
        """
        v = job.verdict if isinstance(job.verdict, dict) else {}
        if kind == "done" and job.brain_run and v.get("evidence") and not v.get("send") and not v.get("said_first"):
            # Phase 2 (2.3, aBrain merge note 1): a brain ending that carries its evidence (not confirmed, failed,
            # partial, stuck, refused, or likely with part of it unread) is the talker's to word from that evidence
            # (:func:`result_payload` passes it as data); this line waits in the owed ledger as its fallback
            # (``_owed_words``). The gate is the evidence, never the level: the brain attaches none to a send's or a
            # join's ending (consent's words) or a tidy-up's (``said_first``), so those stay code's, word for word.
            self._log("bridge", op="outcome_worded", job_id=job.job_id, said_by="talker", evidence=True,
                      level=str(v.get("level") or "") or None)
            return None
        if self.on_brain and self._results_by_code and kind == "done" and not job.remember and not job.superseded \
                and ((job.brain_run and code_line(job.verdict)) or verdict_line(job.verdict, f"{head} {rest}")):
            # bTalk (design 3, "narrator re-voice off for code lines"): the brain's line after an act, or its own code
            # line, is voice-shaped and says no more than the check saw: said by code word for word, late or not.
            # Phase 2 (2.3): a brain run's line with no evidence (a success, a send or join, a tidy-up) stays here. The
            # old loop's line: only a success (checked or likely, whole); any other outcome is the talker's, and this
            # line waits in the owed ledger as its fallback (``_owed_words``). Review r1 M3: an outcome of a send (a
            # lasting act: ``send`` set) keeps code's own line word for word, so no talker run can make it a "Sent."
            lasting = bool(v.get("send"))
            if not job.brain_run and not success_line(job.verdict) and not lasting:
                self._log("bridge", op="outcome_worded", job_id=job.job_id, said_by="talker",
                          level=str((job.verdict or {}).get("level") or "") or None)
                return None
            text = " ".join((head or "").split())
            if strip_tags(text).strip():
                return sentences(text) or None
        if not self._results_by_code or kind != "done" or job.remember or job.superseded or late or nothing_done:
            return None
        if (rest if by_rule is None else by_rule) and not verified:
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

        Fix plan Item 2(d): only for an explicit new task of the talker's (a hand-off with ``new_task`` / ``instead``
        that goes to the worker), never because a hand was refused or did not take effect (:meth:`escalate`): live
        10 Oct "Do it, bro" became a refused click and withdrew the job waiting for exactly that yes. The runtime sets
        a question aside by itself when new work waits on its lane (v3.14b), so nothing is held behind it for good.
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

    # -- IF-answer (fix plan Item 2c): the user's answer reaches the job that asked ----------------------------------

    def answer_waiting(self, text: str, *, hold_s: float | None = None) -> bool:
        """The user's turn that just ended (``text``, exactly as heard) goes to the voice job waiting for their answer;
        returns whether the turn was taken (``False``: no job waits on a question they heard, it was asked over
        :data:`ANSWER_S` ago, or the conversation is in a call, where most words are for the others).

        Live 10 Oct "Yeah." and "Yes." after "Did you mean Hemanth?" never reached the job: an answer was routed only
        when the talker called a follow-up hand-off. Now the turn is claimed for the job and routed (``_route``, the
        runtime's ``answer``) once the talker's run for it has settled (:meth:`_claim_tick`), unless the talker's own
        call for the same turn says first that it is a new task or replaces the job (``hand_over`` / ``tell_yuki``
        ``new_task`` / ``instead``), asks about the work (``check_progress``, ``more_of_result``), ends the
        conversation, or (in a call only) stays quiet on it; outside a call a ``stay_quiet`` keeps the claim ("yeah" is
        often met with silence while an outcome is on its way). The talker's follow-up hand-off, or a hand of its that
        did not take effect (not a skill run: :meth:`escalate`), routes it at once; a
        hand of its that acted on the turn (not one that only brought a window to the front) used it, so it is not
        routed as well. ``hold_s``: route after exactly this long instead (``0``: at once). Another turn while the claim
        is held is added to it. Called on the voice loop by the pipeline's user-turn point (Item 3, guarded by
        ``getattr``) for each final turn that reaches the talker (never the fast path's: the name alone is nobody's
        answer); the same turn offered twice is taken once. Routing on state, times and string identity only, never on
        what the words mean."""
        words = " ".join(str(text or "").split())
        self._note_held_words(words)   # hands19 review M2/M3: a send countdown open now is paused by them, kept with it
        if not words or self._loop is None:
            return False
        job = self._answerable()
        if job is None:
            return False
        # review r2 M1: the turn's own time in the heard log (noted here if the final-words hook has not yet: Pipecat
        # runs observers on their own task), so the claim is bound to this turn, never to its words
        heard = self.heard_log.note(words) if self.heard_log is not None else None
        now = time.monotonic()
        claim = self._claim
        if claim is not None and claim.job is job and claim.parts and claim.parts[-1] == words \
                and now - claim.last_at <= ANSWER_SAME_TURN_S:
            return not claim.dropped   # the same turn offered twice: taken once
        if claim is not None and not claim.done and claim.job is job:
            claim.parts.append(words)   # they said more while it was held: their answer is all of it
            claim.last_at, claim.quiet_since = now, None
            claim.heard_at = heard
            if claim.heard_from is None:
                claim.heard_from = heard
        else:
            if claim is not None and not claim.done:
                self._drop_claim("replaced")
            claim = self._claim = _Claim(job=job, parts=[words], heard_from=heard, heard_at=heard)
        self._log("bridge", op="answer_claimed", job_id=job.job_id, parts=len(claim.parts), chars=len(claim.text))
        hold = None if hold_s is None else max(0.0, float(hold_s))
        if hold == 0.0:
            self._commit_claim("now", claim)
            return True
        self._arm_claim(claim, hold)
        return True

    def _arm_claim(self, claim: _Claim, hold: float | None, delay: float | None = None) -> None:
        """Look at a held claim again on the voice loop: after ``hold`` (a fixed hold), else after ``delay`` or one
        :data:`ANSWER_TICK_S`."""
        loop = self._loop
        if loop is None:
            return
        wait = hold if hold is not None else (ANSWER_TICK_S if delay is None else max(0.05, delay))

        def arm() -> None:
            if claim.handle is not None:
                claim.handle.cancel()
            claim.handle = loop.call_later(wait, self._claim_tick, claim, hold)

        try:
            if asyncio.get_running_loop() is loop:
                arm()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(arm)

    def _claim_tick(self, claim: _Claim, hold: float | None) -> None:
        """A held claim, looked at again: routed once the talker's run for the turn has settled for
        :data:`ANSWER_SETTLE_S` (and at least :data:`ANSWER_HOLD_MIN_S` after their last words), or at the latest
        :data:`ANSWER_HOLD_MAX_S` after them (while one of its hands is still in flight, or a call of its for the turn
        is held on unsure words until they answer, up to :data:`ANSWER_TOOL_MAX_S`: that outcome decides first, and
        their next turn joins the claim); with no view of the talker,
        :data:`ANSWER_HOLD_S` after them. Dropped instead when a hand of the talker's acted on the turn meanwhile."""
        claim.handle = None
        if claim is not self._claim or claim.done:
            return
        if hold is not None:
            self._settle_claim(claim)
            return
        now = time.monotonic()
        age = now - claim.last_at
        busy = self._talker_busy()
        if busy is None:
            if age >= ANSWER_HOLD_S:
                self._settle_claim(claim)
            else:
                self._arm_claim(claim, None, ANSWER_HOLD_S - age)
            return
        if busy:
            claim.quiet_since = None
        elif claim.quiet_since is None:
            claim.quiet_since = now
        settled = claim.quiet_since is not None and now - claim.quiet_since >= ANSWER_SETTLE_S
        tools = self.busy_tools > 0
        # a call of the talker's held on unsure words after their last words in the claim (it asked them first, and
        # they have not answered yet): its decision is still to come
        held = self.held_at is not None and self.held_at >= claim.last_at and now - self.held_at < ANSWER_TOOL_MAX_S
        if (settled and age >= ANSWER_HOLD_MIN_S and not held) or (age >= ANSWER_HOLD_MAX_S and not tools and not held) \
                or age >= ANSWER_TOOL_MAX_S:
            self._settle_claim(claim)
            return
        self._arm_claim(claim, None)

    def _settle_claim(self, claim: _Claim) -> None:
        """The talker's run for a claimed turn is over without a call that decided it: routed to the waiting job, unless
        a hand of the talker's acted on the turn meanwhile (that hand used their words: never acted on twice)."""
        if any(m >= claim.at for m in self._hand_marks()):
            self._drop_claim("hand_acted")
            return
        self._commit_claim("held", claim)

    def _talker_busy(self) -> bool | None:
        """Whether the talker is still on the user's turn: they are speaking, it is writing a reply or owes one, or one
        of its tools is in flight (``None``: nothing here can tell). The conversation's own hook
        (:attr:`talker_busy`) if set, else the narrator gate's live state (``user_speaking``, ``generating``,
        ``owes_reply``) and the bridge's own count of tools in flight. States only."""
        if self.busy_tools > 0:
            return True
        hook = self.talker_busy
        if callable(hook):
            try:
                return bool(hook())
            except Exception:  # noqa: BLE001
                return None
        state = getattr(self.gate, "_state", None) if self.gate is not None else None
        if not callable(state):
            return None
        try:
            s = state()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(s, dict):
            return None
        return bool(s.get("user_speaking") or s.get("generating") or s.get("owes_reply") or s.get("busy_tools"))

    def _answerable(self) -> Job | None:
        """The voice job waiting on a question the user heard (said within :data:`ANSWER_S`), the newest one, else
        ``None``; never in a call (most words there are for the others: the talker judges those)."""
        try:
            if self.call_mode is not None and self.call_mode():
                return None
        except Exception:  # noqa: BLE001
            return None
        now = time.monotonic()
        with self._lock:
            waiting = [j for j in self._jobs.values()
                       if j.source == "voice" and j.state == WAITING and j.lane is not None and not j.superseded
                       and not j.cancel_requested and not j.remember and j.asked_at is not None
                       and now - j.asked_at <= ANSWER_S]
        return max(waiting, key=lambda j: j.asked_at or 0.0) if waiting else None

    def _waiting_ran(self, key: str) -> bool:
        """Review r2 L1: whether the voice job waiting on a question the user heard (or the one a still-held claim is
        for) was pinned with, or ran, the user's skill ``key`` (:attr:`Job.skills`). Keys only (data), never words."""
        if not key:
            return False
        claim = self._claim
        job = claim.job if claim is not None and not claim.done else self._answerable()
        return job is not None and key in job.skills

    def _claim_of(self, newest: str | None) -> _Claim | None:
        """The claim the user's newest turn (``newest``: the context's newest user message, which may join their last
        turns) belongs to, else ``None``. Their last words in it were heard within :data:`CLAIM_TURN_S`, and the message
        *is* one of the claim's turns, not merely a later turn holding the same words (review r2 M1: "Yes." answered the
        question, then "Yes, and turn the Spotify volume down a bit." 3 s later was settled as that answer, and the new
        request was lost): the heard log places the message (:meth:`HeardLog.heard_at`) between the claim's first and
        last part. Its words must also hold the claim's last part or be part of what it holds (the context may join
        turns). When the heard log cannot place it, the claim's last part must end the message (their newest words;
        the context joins older turns before it). The heard turns' times and words as tokens (:func:`_tokens`), never
        meaning."""
        claim = self._claim
        if claim is None or not claim.parts or time.monotonic() - claim.last_at > CLAIM_TURN_S:
            return None
        message = _tokens(newest)
        if not message:
            return None
        last, whole = _tokens(claim.parts[-1]), _tokens(claim.text)
        at = None
        if self.heard_log is not None:
            try:
                at = self.heard_log.heard_at(str(newest))
            except Exception:  # noqa: BLE001 - placed by its words alone, below
                at = None
        if at is not None and claim.heard_from is not None and claim.heard_at is not None:
            if not claim.heard_from - CLAIM_HEARD_S <= at <= claim.heard_at + CLAIM_HEARD_S:
                return None   # a turn heard after the claim's last part (or before it began): never the claim's
            return claim if _within(last, message) or _within(message, whole) else None
        if (last and message[-len(last):] == last) or _within(message, whole):
            return claim
        return None

    def _commit_claim(self, why: str, claim: _Claim | None = None) -> bool:
        """Route the held claim (``claim``: only if it is still the current one) to its job now; returns whether it
        went. On the GUI thread the job's state decides (``_route``): answered while it waits, else added to it."""
        current = self._claim
        if current is None or current.done or (claim is not None and claim is not current):
            return False
        current.done, current.via = True, why
        if current.handle is not None:
            current.handle.cancel()
            current.handle = None
        job, text, parts = current.job, current.text, len(current.parts)
        if job.superseded or job.cancel_requested or job.state not in OPEN_STATES:
            current.dropped = True
            self._log("bridge", op="answer_dropped", job_id=job.job_id, why="job_gone")
            return False

        def route() -> None:
            how = self._route(job, text)
            self._log("bridge", op="answer_routed", job_id=job.job_id, how=how, why=why, parts=parts)

        self._gui(route)
        return True

    def _drop_claim(self, why: str) -> None:
        """The talker's own call says the held turn is not the waiting job's answer (or a hand of its used the turn):
        it is never routed to it. Kept, marked dropped, so a later call of the same turn never takes it as answered."""
        claim = self._claim
        if claim is None or claim.done:
            return
        claim.done = claim.dropped = True
        if claim.handle is not None:
            claim.handle.cancel()
            claim.handle = None
        self._log("bridge", op="answer_withdrawn", job_id=claim.job.job_id, why=why)

    def _not_an_answer(self, params: Any, why: str) -> None:
        """The talker's call for the user's newest turn says it is not the waiting job's answer (they were not talking
        to Yuki, asked how the work goes, asked for more of a result): a claim of that turn still held is dropped."""
        claim = self._claim
        if claim is None or claim.done:
            return
        heard = heard_words(getattr(params, "context", None), skip=self.not_heard)
        if self._claim_of(heard[-1] if heard else None) is claim:
            self._drop_claim(why)

    def _claim_verdict(self, intent: str, heard: list[str]) -> tuple[_Claim, str] | None:
        """Item 2(c, e): what the talker's hand-off of the user's newest turn means for a claim of that turn:
        ``(claim, "answered")`` (a follow-up: routed now if still held), ``(claim, "merged")`` (the words already went
        to the waiting job as its answer: never a second job on them), or ``None``: no claim of this turn, or the
        hand-off says it is not the answer (a new task or a replacement, judged before the claim was routed: dropped,
        the call goes on as usual; ``instead`` after it was routed still replaces the job, as the talker said).
        A remember is neither: the claim stands and the call goes on."""
        claim = self._claim_of(heard[-1] if heard else None)
        if claim is None or claim.dropped or intent == "remember":
            return None
        if claim.done and intent in ("new_task", "follow_up"):
            return claim, "merged"
        if intent in ("new_task", "instead"):
            self._drop_claim(intent)
            return None
        if intent == "follow_up":
            if not self._commit_claim("follow_up", claim):
                return None   # the waiting job has gone meanwhile: the follow-up goes on as usual, never lost
            return claim, "answered"
        return None

    async def _claim_decides(self, params: Any, args: dict[str, Any], intent: str, heard: list[str],
                             early: Job | None) -> bool:
        """The talker's hand-off for a turn claimed as the waiting job's answer (:meth:`_claim_verdict`); returns
        whether the call was settled here (quietly: the job's outcome is said when it comes)."""
        from pipecat.frames.frames import FunctionCallResultProperties

        decided = self._claim_verdict(intent, heard)
        if decided is None:
            return False
        claim, how = decided
        status = ("already being done for these words" if how == "merged" and claim.via == "escalated" else
                  "passed on to the waiting task (answered)")
        app_named = " ".join(str(args.get("app_named") or "").split())[:80] or None
        if app_named and not claim.dropped:
            # review r1 L3: the app they named in these words goes to the job they answered (V4, as a follow-up's does)
            job = claim.job
            self._gui(lambda: self._name_app(job, app_named))
        if early is not None:
            self._gui(lambda: self._cancel(early))
        await self._ack_if_silent(params, args)
        self._log("bridge", op="follow_up", job_id=claim.job.job_id, how=how, call=params.tool_call_id,
                  intent=intent, claimed=True, via=claim.via)
        await params.result_callback({"status": status, "how_to_handle_it": ANSWERED_NOTE},
                                     properties=FunctionCallResultProperties(run_llm=False))
        return True

    def _escalate_to_answer(self, params: Any, name: str, heard: list[str], *, fresh: bool = True) -> bool:
        """Item 2(c, d): a hand of the talker's did not take effect while a voice job waits on a question the user
        heard: their words since that question are its answer (routed now), never a second job on the same words, and
        the waiting job is never withdrawn for it (live 10 Oct "Do it, bro" became a refused click, withdrew the job
        waiting for exactly that yes and restarted an old one). ``False``: nothing waits; escalate as before.
        ``fresh=False`` (a skill run handing over): only words already routed to the job as its answer stay its answer
        (never a second job on them); none are claimed for it here."""
        claim = self._claim_of(heard[-1] if heard else None)
        if claim is not None:
            if claim.dropped:
                return False   # the talker's own call said these words are not its answer: a request of their own
            if not claim.done and not self._commit_claim("escalated", claim):
                return False   # the waiting job is gone: these words are a request of their own
            self._log("bridge", op="escalated_to_answer", hand=name, job_id=claim.job.job_id, claimed=True,
                      call=params.tool_call_id)
            return True
        if not fresh:
            return False
        job = self._answerable()
        if job is None:
            return False
        newest = self.heard_log.heard_at(heard[-1]) if heard and self.heard_log is not None else None
        if newest is not None and job.asked_at is not None and newest <= job.asked_at:
            return False   # their words came before the question was said: not its answer
        turns = self._turns(params.context, heard, after=job.asked_at)
        if not turns:
            return False
        if self._claim is not None and not self._claim.done:
            self._drop_claim("replaced")
        # review r2 M1: bound to these turns (said after the question, the newest at ``newest``), never to their words
        claim = self._claim = _Claim(job=job, parts=list(turns),
                                     heard_from=job.asked_at if newest is not None else None, heard_at=newest)
        if not self._commit_claim("escalated", claim):
            return False
        self._log("bridge", op="escalated_to_answer", hand=name, job_id=job.job_id, turns=len(turns),
                  call=params.tool_call_id)
        return True

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
                       acked: bool = False, context: str | None = None, skill: dict[str, Any] | None = None) -> bool:
        """A hand of the talker's did not take effect: hand the same request to the thinker now, by code (v3.9).

        Live 2026-09-27 14:18 the talker narrated every failed try ("The stop key didn't take. Let me pause it
        instead." / "Hmm, the media keys aren't working...") before handing over, three lines for one pause.
        Now the thinker gets the user's words with the hand's report and the window in front already read, and
        the user hears one outcome (and one "still on it" only if it takes a while). Returns whether a job was
        started (``False`` with no conversation loop or no runtime).

        ``context`` (v3.18): what to tell the thinker instead of the one-step note: a learned way the talker ran that
        stopped at a step (what was done, where it deviated, the way's steps), so it carries on and never redoes them.

        ``skill`` (Phase S, map-skills S3.4): one of the user's skills whose own routes didn't get there (rung C): the
        pinned dict (``yuki.skills.engine.pinned``) goes with the request (``runtime.submit(skill=)``, probed), so the
        thinker's frame shows it first and its done counts only on the skill's own check. The engine never hands over
        with a game in front (D17(2)).

        oVoice (review fix L3): never under ``brain = "one"`` (``False``: nothing is handed to a thinker; the ONE agent
        reads the hand's result itself).
        """
        if self._loop is None or getattr(self, "one", False):
            if getattr(self, "one", False):
                self._log("bridge", op="escalate", refused="one", hand=name)
            return False
        self._note_escalated_hand(name)   # Item 2(a): its record is no finished request; what it becomes decides
        heard = heard_words(params.context, skip=self.not_heard)
        seeded = isinstance(skill, dict) and bool(skill.get("key"))
        own = seeded and self._waiting_ran(str(skill.get("key")))
        if seeded and not own:
            # review r1 L1: the talker picked one of the user's skills for these words: a request of their own ("play my
            # Japanese playlist" while "Did you mean Hemanth?" waits), never that question's answer. A claim of the turn
            # still held is dropped; the runtime sets the waiting job aside for this one (quietly, ``parked``).
            # Review r2 L1: unless the waiting job itself runs that skill ("Yeah." to "Did you mean Hemanth?", and the
            # talker ran the message skill on it): then the words are its answer, as a refused hand's are
            self._not_an_answer(params, "skill")
        if self._escalate_to_answer(params, name, heard, fresh=not seeded or own):
            return True   # Item 2(c, d): a voice job waits on a question they heard: these words are its answer
        if self.on_brain:
            return self._brain_escalate(params, name, args, report, heard, acked=acked, context=context, skill=skill)
        request = exact_words(params.context, skip=self.not_heard, heard=heard, exchange=self) \
            or _action_words(name, args)
        if skill and not context:
            context = (f"my own try of their usual way ({skill.get('key')}) did not get there: "
                       f"{' '.join(str(report).split())[:300]}; it is pinned below, do it that way")
        context = (" ".join(str(context).split())[:1200] if context else
                   f"my own one-step try ({name}) did not take effect: {' '.join(str(report).split())[:400]}")
        said = spoken_words(params.context)
        text = brain_request(request, heard, context=context, done_before=self._done_text(), said=said,
                             tabs=self.tabs_for_thinker())
        job = self._new_job(request)
        job.escalated = True
        job.acts = True   # the talker's own hand was tried for these words: they asked for something to happen
        job.acked = acked   # the talker says a word for it now (v3.14), so no "still on it" before its outcome
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        # Item 2(d): a question is never passed over because a hand did not take effect (only on an explicit new task);
        # one still open is set aside by the runtime while this runs, and its answer later carries it on
        words = _memory_words(request, heard)
        pin = dict(skill) if isinstance(skill, dict) and skill.get("key") else None
        self._gui(lambda: self._submit(job, text, origin, lane="worker", view=origin or self._desk_foreground(),
                                       memory_query=words, user_words=heard, skill=pin))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent="escalated", hand=name,
                  words_chars=len(request), context_chars=len(context), origin_hwnd=origin, heard_turns=len(heard),
                  skill=pin.get("key") if pin else None)
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    #: fc (C1/C2): what the thinker is told when the talker's two replies to their words both meant to act and nothing ran.
    LOST_NOTE = ("Yuki's voice answered these words twice and neither reply's step came through, so nothing has been "
                 "done for them yet. Do what they asked.")

    def hand_lost(self, context: Any, name: str = "", *, why: str = "broken") -> bool:
        """fc (C1/C2, final-F open_claude: "On it." and nothing): under ``brain = "all"`` the talker answered the user's
        words twice and neither reply's call came through (a call written that never ran, or a reply marked act with no
        call; ``why``, a code word, for the log): nothing was done, so the thinker gets their exact words now, by code,
        as an escalated hand's request (the commit check, the countdown and every gate of the thinker hold as for any
        request). Their words that answer a question a job waits on go to it (:meth:`_escalate_to_answer`). ``False``:
        no job (no conversation loop, ``one``, the legacy brain, or no words). Never raises."""
        if self._loop is None or getattr(self, "one", False) or not self.on_brain:
            return False
        from types import SimpleNamespace

        try:
            params = SimpleNamespace(context=context, tool_call_id=None)
            heard = heard_words(context, skip=self.not_heard)
            if not heard:
                return False
            tool = str(name or "") or "reply"
            if self._escalate_to_answer(params, tool, heard):
                self._log("bridge", op="hand_lost", why=str(why or ""), answer=True)
                return True
            handed = self._brain_escalate(params, tool, {}, "", heard, context=self.LOST_NOTE)
            self._log("bridge", op="hand_lost", why=str(why or ""), handed=bool(handed), tool=tool)
            return bool(handed)
        except Exception as exc:  # noqa: BLE001 - code's own line then
            self._log("bridge", op="hand_lost", error=type(exc).__name__)
            return False

    async def _run_held(self, call: tuple[str, dict, Any]) -> bool:
        """Do a held hand-off after all (the mouth stayed quiet twice on it): its words said by code, its job run.
        oVoice (review fix L3): never under ``brain = "one"`` (``False``: no hand-off to run)."""
        if getattr(self, "one", False):
            self._log("bridge", op="held_run", refused="one")
            return False
        _name, args, context = call
        heard = heard_words(context, skip=self.not_heard)
        intent = str(args.get("intent") or "new_task")
        decided = self._claim_verdict(intent, heard)
        if decided is not None:
            # Item 2(c): the held words are the waiting job's answer (routed now, or already): never a second job
            self._log("bridge", op="held_run", job_id=decided[0].job.job_id, how=decided[1], claimed=True)
            return True
        request = exact_words(context, skip=self.not_heard, heard=heard, exchange=self)   # D10, Item 2a: their words
        if not request or self._loop is None:
            return False
        if self.on_brain:
            return await self._brain_run_held(args, context, heard)
        app_named = " ".join(str(args.get("app_named") or "").split())[:80] or None
        text = brain_request(request, heard, context=str(args.get("context") or "").strip() or None,
                             done_before=self._done_text(), said=spoken_words(context),
                             tabs=self.tabs_for_thinker(), app_named=app_named)
        job = self._new_job(request)
        job.app_named = app_named
        job.acked = bool(say_first_words(args.get("say_first")))
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
        words = say_first_words(args.get("say_first"))
        if words and self.speak is not None:
            await self.speak(words)
        self._log("bridge", op="submit", token=job.token, intent="held_run", words_chars=len(request),
                  screen=view is not None, lane=lane)
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    #: voice20 (final-D S11): what code says when the talker's bare cancel stopped a passed-on job: with a send counting
    #: down when it was stopped, the both brains' words for a stopped send (``executor.LINE_STOPPED_SEND``,
    #: ``one.runner.LINE_STOPPED_SEND``), else that it stopped. Facts of the job's record, never the words.
    LINE_STOPPED_SEND = "Stopped. Nothing was sent."
    LINE_STOPPED = "Okay, stopped."

    def _say_stopped(self, job: Job) -> None:
        """voice20 (final-D S11): the talker's cancel of a passed-on job came as a bare call (no words), and the job has
        stopped with nothing sent: its own ending is said by code, through the narrator gate like a code-said final
        (when Yuki may speak), and goes into the talker's conversation as said (:data:`CODE_SAID_NOTE`), never with a
        talker run. Never raises."""
        line = self.LINE_STOPPED_SEND if job.cancel_had_send else self.LINE_STOPPED
        announce = self.announce

        async def deliver(run_llm: bool, stale: bool) -> None:
            del run_llm   # it speaks itself: the talker is never run for it
            if stale or job.sent:
                self._log("bridge", op="cancel_said", job_id=job.job_id, said=False, stale=bool(stale))
                return
            try:
                await self._say_lines([line], None)
                if announce is not None:
                    await announce({"stopped": job.brief(), "said": line, "how_to_handle_it": CODE_SAID_NOTE}, False)
                self._log("bridge", op="cancel_said", job_id=job.job_id, said=True, send=job.cancel_had_send,
                          said_by="code")
            except Exception as exc:  # noqa: BLE001 - a line never breaks the bridge
                self._log("bridge_error", error=f"cancel_said: {type(exc).__name__}")

        if self.speak_result is None and self.speak is None:
            self._log("bridge", op="cancel_said", job_id=job.job_id, said=False, voice=False)
            return
        try:
            if self.gate is not None:
                self.gate.enqueue(Item(job=job.token, kind="final", deliver=deliver, self_spoken=True))
            else:
                asyncio.ensure_future(deliver(False, False))
        except Exception as exc:  # noqa: BLE001
            self._log("bridge_error", error=f"cancel_said: {type(exc).__name__}")

    async def _relay_detached(self, job: Job) -> None:
        """Items of a job no tell_yuki call carries (escalated, or a too-late addition): through ``announce``."""
        job.detached = True   # hands19 (C4): the talker's cancel reaches it through cancel_passed_on
        while True:
            kind, payload = await job.events.get()
            # voice20 review L2: a bare cancel's job that ended done finished before the cancel landed (a stop the
            # runtime's cancel made ends "failed"), so "stopped" would be untrue: its own final is relayed below as any
            # detached job's (late: they spoke since), never dropped and never called stopped. The job's record only.
            finished = kind == "done" and job.talker_cancelled and job.talker_silent and not job.sent
            if finished:
                self._log("bridge", op="cancel_said", job_id=job.job_id, said=False, finished=True)
            if job.talker_cancelled and not job.sent and not finished:
                # hands19 (C4): the talker stopped it (cancel_passed_on) and said so in the same reply: nothing more of
                # it is said, as for a cancelled hand-off call (a send that went is still said). voice20 (final-D
                # S11): a bare cancel call said nothing: its own ending is said by code once it has stopped
                if kind in ("done", "failed", "withdrawn", "set_aside"):
                    if self.steps is not None:
                        self.steps.closed(job.token)
                    self._drop_items(job)
                    self._unowe(job)
                    if job.talker_silent:
                        self._say_stopped(job)
                    return
                continue
            if self._step_item(job, kind, payload):
                continue
            if kind == "say_line":
                kind, payload = "say", payload[1]
            if kind in ("done", "failed", "withdrawn", "set_aside") and self.steps is not None:
                self.steps.closed(job.token)
            announce = self.announce
            if announce is None:
                return
            if kind == "set_aside":
                self._drop_items(job)   # Item 2(d): set aside on its unanswered question: a quiet end
                await announce({"set_aside": job.brief(), "how_to_handle_it": SET_ASIDE_NOTE}, False)
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
                if kind == "done":
                    # P1.3: the brain's tidy-up said the request's line first; only what is new is said now
                    payload, first = self._said_first(job, payload)
                    if first and not payload:   # all of it was heard already: settled as said, no talker run
                        self._outcome_said(job)
                        await announce({"done": first, "how_to_handle_it": RESULT_SAID_NOTE}, False)
                        return
                held = self._held_verdict(job, kind, payload)   # honest (87): below likely, never said as written
                verified = bool(kind == "done" and job.finished_by_action
                                and payload.strip() == job.finished_by_action) and held is None
                head, rest, by_rule = self._condensed(job, payload)   # voice20 (S22g): an answer is never cut by count
                job.rest = rest
                nothing = self._nothing_done(job) if kind == "done" else False
                lines = None if held is not None else self._by_code(job, kind, head, rest, late=late,
                                                                    verified=verified, nothing_done=nothing,
                                                                    by_rule=by_rule)
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
                                                      nothing_done=nothing, outcome=job.verdict, lean=self._lean),
                                       run_llm and not stale)
                item_kind = "final"
            else:
                spoken = self._ask_by_code(job, str(payload or ""), announce, push=None) if kind == "ask" else None
                if kind == "waiting":
                    body: dict[str, Any] = waiting_payload(job.brief(200), *payload)
                elif kind == "ask":
                    body = {"yuki_asks": payload}   # Item 2(b): only when it cannot be said by code (above)
                elif kind == "say":
                    body = {"yuki_says": payload}
                else:
                    body = {"status": payload}

                async def deliver(run_llm: bool, stale: bool, body=body, kind=kind, payload=payload) -> None:
                    del stale
                    if run_llm:
                        self._waiting_run = (job, time.monotonic()) if kind == "waiting" else None
                    if kind == "ask":
                        job.asked_at = time.monotonic()
                        self._owe(job, "ask", str(payload))   # v3.16
                    await announce(body, run_llm)
                if spoken is not None:
                    deliver, self_spoken = spoken, True   # said by code, word for word (Item 2b)
                item_kind = kind if kind in RANKS else "say"
            gate = self.gate
            if gate is not None:
                gate.enqueue(Item(job=job.token, kind=item_kind, deliver=deliver, self_spoken=self_spoken))
            else:
                await deliver(not self_spoken, False)
            if kind in ("done", "failed"):
                return

    # -- bTalk (brain rebuild rev 2, design 2, 3, 10.1): spoken requests on the new brain (brain = "all") --------

    #: The talker's conversation given to the new brain with a hand-off (design 3: "plus the last turns, built by
    #: code"): at most this many earlier lines, oldest first, each cut to this many characters, and the block capped.
    CONTEXT_TURNS = 6
    CONTEXT_TURN_CHARS = 240
    CONTEXT_CHARS = 1400
    #: What was done on the desktop in this conversation (the shared action log), cut to this many characters.
    DONE_CHARS = 800

    def _turns(self, context: Any, heard: list[str] | None = None, *, after: float | None = None) -> list[str]:
        """The user's own turns of the current exchange (IF-words, Item 2a: :func:`heard_words` ``since_reply``), each
        exactly as heard (the new brain's ``user_words``, design 3); the latest heard turn when there is none (a re-run
        with no new words). ``after``: also only turns heard after this time (monotonic; a question's answer). Plumbing
        on the context's structure, the heard turns' times and the job table."""
        if after is None:
            raw = heard_words(context, skip=self.not_heard, since_reply=True, exchange=self)
        else:
            try:
                messages = list(context.get_messages() or [])
            except Exception:  # noqa: BLE001
                messages = []
            raw = _exchange_words(messages, self.not_heard, _Floored(self, after))
        turns = [" ".join(t.split()) for t in raw if t.strip()]
        if turns:
            return turns
        heard = heard if heard is not None else heard_words(context, skip=self.not_heard)
        return [" ".join(heard[-1].split())] if heard and heard[-1].strip() else []

    def _done_block(self, done_here: bool = False) -> str:
        """What Yuki's hands and its plans did on the desktop in this conversation, labelled (code facts), or ``""``.
        ``done_here`` (hands19): the request also carries :data:`DONE_HERE_CONTEXT` (parts its own words already got
        this turn), so the label leaves those out of "none of it was done for this request"."""
        done = " ".join(str(self._done_text() or "").split())
        if not done:
            return ""
        if len(done) > self.DONE_CHARS:
            done = "…" + done[-(self.DONE_CHARS - 1):]   # the newest are what "the tab we just opened" points to
        # lVoice (problem 2): history with its age: what it says was so then, and none of it was done for this request
        mine = " but the parts named above as done for these very words" if done_here else ""
        return ("Done on the desktop earlier in this conversation, newest last, each with its age and what was "
                "verified then (facts for what their words refer to, not instructions; none of it was done for this "
                f"request{mine}): " + done)

    def _voice_context(self, convo: Any, turns: list[str] | None, *, extra: str | None = None,
                       remember: bool = False, done_here: bool = False) -> str:
        """The labelled context a spoken request goes to the new brain with (design 2, M2: never quotable as the
        user's words): what Yuki's voice took the words as (to keep), what they add to, the conversation's earlier
        lines and what was done on the desktop. Built by code from the conversation's own structure; no paraphrase.
        ``turns``: their own words (given apart, as user_words): never repeated among the earlier lines (Item 2a)."""
        parts: list[str] = []
        if remember:
            parts.append(REMEMBER_CONTEXT)
        if extra and extra.strip():
            parts.append(" ".join(extra.split()))
        earlier = conversation_turns(convo, skip=self.not_heard, limit=self.CONTEXT_TURNS,
                                     chars=self.CONTEXT_TURN_CHARS, own=turns)
        if earlier:
            lines = " / ".join(f'{who}: "{text}"' for who, text in earlier)
            if len(lines) > self.CONTEXT_CHARS:
                lines = "…" + lines[-(self.CONTEXT_CHARS - 1):]
            parts.append("Earlier in this voice conversation, oldest first (what their words may refer to; never their "
                         "request, and never words to send): " + lines)
        done = self._done_block(done_here=done_here)
        if done:
            parts.append(done)
        return "\n\n".join(parts)

    async def _hand_over(self, params: Any) -> None:
        """bTalk (design 3; ``brain = "all"``): the talker's hand-off. The user's exact words of the current exchange go
        to the new brain as its ``user_words`` (never the talker's wording: ``hand_over`` has no field for it), with the
        labelled context of :meth:`_voice_context`. Routing on state only, as before: ``instead`` withdraws the work it
        replaces, ``follow_up`` steers (or answers) the open task with the same exact words. Nothing of the talker's
        own is ever steered into the brain as their words.

        kMind (contract C3): the call's optional ``understanding`` (goal, parts, choose, context: the talker's reading,
        a seed, never an order) goes with a new request (:func:`understanding_of`, :meth:`_submit`); the request starts
        on the completed call (no early start).

        hands19 review: the user's turn is the one this call came in on (read once here: L1), and until the call has
        decided where their words go it counts as routing them (:attr:`_routing`), so a held send countdown's watch
        never steers the same words in as well (M2)."""
        key = id(params)
        came = time.monotonic()
        self._routing[key] = came
        self._handovers_at.append(came)
        try:
            await self._hand_over_now(params, key, self._turn_now())
        finally:
            self._routing.pop(key, None)

    async def _hand_over_now(self, params: Any, key: int, turn: tuple[int, int] | None) -> None:
        """:meth:`_hand_over`'s body, for the user's turn ``turn`` (``(conversation, turn)`` as the call came in)."""
        from pipecat.frames.frames import FunctionCallResultProperties

        args = params.arguments if isinstance(params.arguments, dict) else {}
        intent = str(args.get("intent") or "new_task")
        if intent not in ("new_task", "follow_up", "instead", "remember"):
            intent = "new_task"
        heard = heard_words(params.context, skip=self.not_heard)
        remember = intent == "remember"
        if await self._claim_decides(params, args, intent, heard, None):
            return   # Item 2(c, e): their answer to the waiting question, routed once
        asked = ""
        if remember:
            hold, asked = self._reversal_gate(args, turn)
            if hold:
                # integ30 (le merge note 1, K-R talker side): a reversal of this talk with no reason said is never
                # kept before their answer; the call's say_first is the one question (HAND_OVER), said if unsaid
                await self._ack_if_silent(params, args)
                # integ30 fix (T2): whether that question was said is measured (the words said for this very call,
                # early or by code just now; the reply's own words): an early say the one-acknowledgement rule held
                # back, or an ack refused, leaves it unsaid, and then the talker asks it now
                spoke = self.spoke_in_reply() if self.spoke_in_reply is not None else True
                unsaid = not spoke and not self.early_said(params.tool_call_id)
                self._log("bridge", op="remember_held", call=params.tool_call_id, unsaid=unsaid or None)
                await params.result_callback({"held": REMEMBER_HELD, "how_to_handle_it": REMEMBER_HELD_ASK_NOTE
                                              if unsaid else REMEMBER_HELD_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=unsaid))
                return
            if asked:
                self._log("bridge", op="remember_released", call=params.tool_call_id)
        steered = self._held_steered_job() if intent in ("new_task", "follow_up") else None
        if steered is not None:
            # hands19 review M2: code already gave these very words to the task whose send they paused (the talker
            # routed none of them in time): never a second steer of them, nor a second job for them
            await self._ack_if_silent(params, args)
            self._log("bridge", op="merged_held_steer", job_id=steered.job_id, call=params.tool_call_id, intent=intent)
            await params.result_callback({"status": "passed on to the running task (added)"},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        replaced: list[Job] = []
        if intent == "instead":
            replaced = self._replace_targets(args.get("replaces"))
            for old in replaced:
                old.superseded = True   # Item 2a: at once, so its words are this request's again (exchange_floor)
                self._drop_items(old)
        # IF-words (Item 2a): the user's own turns of the current exchange (after the withdrawals above: the words of a
        # replaced request are this one's)
        turns = self._turns(params.context, heard)
        request = " ".join(turns)
        if replaced:
            for old in replaced:
                self._gui(lambda old=old: self._withdraw(old, words=request))
            self._log("bridge", op="replace", call=params.tool_call_id, withdrawn=[j.job_id for j in replaced])
            intent = "new_task"
        elif intent == "instead":
            self._log("bridge", op="replace", call=params.tool_call_id, withdrawn=[])
            intent = "new_task"
        # hands19 (final-B all-questions RC3a): the same reply's other calls return first (a hand of theirs that passed
        # the request on is then this turn's job, below); what this turn's hands verified goes with the request as done.
        # Review L2: only a new request waits (a follow-up's words, a "don't send" steer among them, go on at once; one
        # that starts a new request after all waits below)
        done_here = await self._hands_first(params, turn) if intent == "new_task" else []
        if intent in ("new_task", "follow_up") and not replaced:
            same = self._same_turn_job(turn)
            if same is not None and same.escalated:
                # code already gave the brain these very words (a hand of the talker's did not take effect, v3.14);
                # Item 2(e): a follow-up of them too is never steered in a second time (live 10 Oct, job 3)
                await self._ack_if_silent(params, args)
                self._log("bridge", op="merged_escalated", job_id=same.job_id, call=params.tool_call_id,
                          intent=intent)
                await params.result_callback({"status": "already being done for these words"},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
            if same is not None and intent == "new_task":
                intent = "follow_up"   # one job per request (v3.10)
                self._log("bridge", op="merged_same_turn", job_id=same.job_id, call=params.tool_call_id)
        await self._ack_if_silent(params, args)
        extra: str | None = None
        if intent == "follow_up":
            target = self._follow_up_target()
            if target is not None:
                said = request
                if target.state == WAITING and target.asked_at is not None:
                    # their answer: the words since its question was said (Item 2c)
                    said = " ".join(self._turns(params.context, heard, after=target.asked_at)) or request
                how = await self._follow_up(target, said)   # their exact words: the brain's steer, or its answer
                self._log("bridge", op="follow_up", job_id=target.job_id, how=how, call=params.tool_call_id)
                if how != "restart":
                    await params.result_callback({"status": f"passed on to the running task ({how})"},
                                                 properties=FunctionCallResultProperties(run_llm=False))
                    return
                prev = target.outcome or (target.closing[1] if target.closing else "")
                quoted = condense(prev, max_sentences=2, max_chars=300)[0] if prev else ""
                # voice21 (final-D S22c, desk20 merge note 3): an ending that asked them something (its own question
                # mark: Yuki's question or offer, "want me to bring it up?") is no part of this request unless their
                # newest words take it up (live final-D k_video_another_one t2: the offer was done unasked)
                extra = (f'These words add to the request just before ("{target.brief()}"), which had just finished'
                         + (f": {quoted}" if quoted else ".")
                         + (FINISHED_OFFER_NOTE if quoted and asks(quoted) else "")
                         + " Take it from what the screen shows now.")
        if intent == "follow_up":
            done_here = await self._hands_first(params, turn)   # review L2: it starts a new request after all
        job = self._new_job(request, turn=turn)
        job.acked = self._acked(params, args)
        job.remember = remember
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        # replacing withdrawn work: straight to the worker (through the front desk it would only look first)
        lane = "worker" if replaced else None
        if lane == "worker" and not remember:
            self._pass_over_questions(lane)
        if done_here:
            note = _done_here_note(done_here)
            extra = f"{extra}\n\n{note}" if extra else note
        if asked:
            note = REMEMBER_ASKED.format(around=asked)
            extra = f"{extra}\n\n{note}" if extra else note
        context = self._voice_context(params.context, turns, extra=extra, remember=remember,
                                      done_here=bool(done_here))
        query = _memory_words(request, heard)
        understanding = None if remember else understanding_of(args.get("understanding"))
        self._gui(lambda: self._submit(job, request, origin, lane=lane, memory_query=query, user_words=turns,
                                       context=context, understanding=understanding))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent=intent, brain=True,
                  words_chars=len(request), turns=len(turns), context_chars=len(context), origin_hwnd=origin,
                  done_here=len(done_here),
                  replaced=len(replaced), lane=lane, remember=remember,
                  understanding=len((understanding or {}).get("parts") or []) if understanding else None)
        self._changed()
        self._routing.pop(key, None)   # hands19 review M2: their words have their request now
        self._arm_wait(job)
        try:
            await self._relay(job, params)
        except asyncio.CancelledError:
            if not self.closing:
                # cancel_hand_over: stop the brain's work too, and say nothing more of it; hands19 (C4): marked at once,
                # so a countdown their speech paused is never counted on before the GUI thread has the stop
                job.cancel_requested = True
                self._gui(lambda: self._cancel(job))
                if self.gate is not None:
                    for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                        if item.drop is not None:
                            item.drop()
                self._unowe(job)
                self._log("bridge", op="cancel", job_id=job.job_id, by="mouth")
            raise

    def _reversal_gate(self, args: dict[str, Any], turn: tuple[int, int] | None) -> tuple[bool, str]:
        """integ30 (le merge note 1, contract K-R talker side; final-H2 m21_reversal_checkback): whether the talker's
        remember hand-over is held to ask first, and what a released one turns around.

        Released (``(False, item)``): a hold of this conversation is recorded for an earlier user turn, within
        ``brain.tools.HOLD_S``, the user has spoken since (the turn count moved on) and words of Yuki's were heard
        after the turn that carried it (:attr:`yuki_heard_at`, the narrator's measured time: her question), whatever
        this call's ``turns_around`` says, so the item is never asked twice. Held (``(True, "")``): no release, and
        the call's ``turns_around`` (the talker's required declaration: something given in this talk, turned around
        with no reason said) is set; recorded with when that turn was heard. Anything else (``turns_around`` empty or
        missing, or no turn or heard time to measure by): ``(False, "")``, it goes on as before (the shared handler
        still checks the planner's own declaration). The model's own field and stream facts only, never the words;
        never raises."""
        try:
            from yuki.brain.tools import HOLD_S

            now = time.monotonic()
            held = self._remember_held
            if held is not None and (turn is None or held[0] != turn[0] or now - held[2] > HOLD_S):
                held = self._remember_held = None   # another conversation's, or lapsed
            seen = self.yuki_heard_at
            heard = float(seen() or 0.0) if callable(seen) else 0.0
            if held is not None and turn is not None and turn[1] > held[1] and heard > held[2]:
                self._remember_held = None
                return False, held[3]
            around = " ".join(str(args.get("turns_around") or "").split())[:160]
            if not around or turn is None or not callable(seen):
                return False, ""
            newest = self.heard_log.newest() if self.heard_log is not None else None
            self._remember_held = (turn[0], turn[1], float(newest) if newest is not None else now, around)
            return True, ""
        except Exception:  # noqa: BLE001 - no measure: as before
            return False, ""

    def _reply_calls(self, params: Any) -> set[Any] | None:
        """hands19 (all-questions RC3a): the other calls of the reply this call came in that are still running
        (Pipecat's running calls of the same group: one model reply's calls share it), hand-offs aside (two hand-offs
        never wait on each other); ``None`` when Pipecat's table can't be read or the call has no group. Plumbing."""
        llm = getattr(params, "llm", None)
        table = getattr(llm, "_function_call_tasks", None)
        if not isinstance(table, dict):
            return None
        try:
            items = list(table.items())
            mine = next((item for _task, item in items
                         if getattr(item, "tool_call_id", None) == getattr(params, "tool_call_id", None)), None)
            group = getattr(mine, "group_id", None) if mine is not None else None
            if group is None:
                return None
            return {task for task, item in items if getattr(item, "group_id", None) == group
                    and getattr(item, "tool_call_id", None) != params.tool_call_id
                    and getattr(item, "function_name", "") not in HANDOFF_TOOLS and not task.done()}
        except Exception:  # noqa: BLE001
            return None

    async def _hands_first(self, params: Any, turn: tuple[int, int] | None = None) -> list[str]:
        """hands19 (final-B all-questions RC3a): before a hand-off goes, the same reply's hands return (bounded,
        :data:`HANDS_FIRST_MAX_S`): its other calls, read from Pipecat's running calls; when those can't be read, this
        turn's hands in the hands' order gate (after :data:`HANDS_ARRIVE_S` for them to reach it). Returns what this
        turn's hands verified (``Hands.verified_this_turn``: code's words), which the request carries as done
        (:data:`DONE_HERE_CONTEXT`). Review L1: of ``turn``, the user's turn the hand-off came in on (their newer words
        during the wait are not its turn). Content-free log. Never raises but for a cancel while waiting."""
        hands = self.hands
        if hands is None:
            return []
        started = time.monotonic()
        siblings = self._reply_calls(params)
        how = "reply"
        if siblings:
            await asyncio.wait(siblings, timeout=HANDS_FIRST_MAX_S)
        elif siblings is None:
            how = "gate"
            running = getattr(hands, "turn_hands_running", None)
            await asyncio.sleep(HANDS_ARRIVE_S)
            while callable(running) and running() and time.monotonic() - started < HANDS_FIRST_MAX_S:
                await asyncio.sleep(HANDS_TICK_S)
        read = getattr(hands, "verified_this_turn", None)
        try:
            got = (read(turn=turn) if turn is not None and _takes(read, "turn") else read()) if callable(read) else []
            done = [str(d) for d in got or [] if str(d).strip()]
        except Exception:  # noqa: BLE001
            done = []
        self._log("bridge", op="hands_first", how=how, calls=len(siblings or ()), done=len(done),
                  waited_ms=round((time.monotonic() - started) * 1000))
        return done

    # -- hands19 (contract C4): the user's speech on an "all" send countdown, and a cancel of a passed-on job -------

    def user_speech_started(self) -> bool:
        """hands19 (final-B all-messaging RC3, contract C4): the user started speaking (the pipeline's VAD start of their
        speech, called through a ``getattr`` guard while a countdown is open). An open send countdown of the new brain's
        (a voice job whose countdown counts: :attr:`Job.sending`) pauses at once with the time it had left (the
        brain's ``SEND_HOLD`` mark), so a spoken "wait, don't send it" is heard before the send can go. Their words then
        decide: a cancel (``cancel_hand_over``, :meth:`cancel_passed_on`) or a replacement stops it; their words routed
        to the job end it for the planner to decide (the talker's follow-up, or, review M2, code's own steer of them
        when the talker routed none of them: brain one's countdown ends on their words too); a pause whose words the
        talker took elsewhere (a new request, a hand), or that brought no words, counts on for the time it had left
        (:meth:`_held_send_watch`). Under brain one or the old thinker: ``False`` (brain one's countdown pauses itself).
        Returns whether a countdown was paused. State only; never raises."""
        try:
            if getattr(self, "one", False) or not self.on_brain or self._loop is None:
                return False
            with self._lock:
                live = [j for j in self._jobs.values() if j.source == "voice" and j.state == RUNNING and j.sending
                        and not j.send_held and not j.superseded and not j.cancel_requested and j.job_id is not None]
            if not live:
                return False
            since = self.heard_log.newest() if self.heard_log is not None else None
            return self._hold_send(max(live, key=lambda j: j.started_at), "speaking", since)
        except Exception as exc:  # noqa: BLE001 - the countdown's own time then
            self._log("bridge_error", error=f"user_speech_started: {type(exc).__name__}")
            return False

    def _hold_send(self, job: Job, why: str, since: float | None) -> bool:
        """hands19 (C4; review M3): ``job``'s open countdown pauses now (the brain's ``SEND_HOLD`` mark) and a watch of
        this pause starts (:meth:`_held_send_watch`; a watch of an earlier pause of it ends). ``since``: when the newest
        turn heard before the words this pause waits for was heard (``None``: none). ``why``: ``speaking`` (their speech
        began), ``spoke_first`` (it opened while they spoke), ``words`` (their final words came while it counted). Any
        thread; state only. Returns ``True``."""
        job.send_held = True
        job.send_gen += 1
        job.held_words = {}
        gen = job.send_gen
        self._gui(lambda: self._send_mark(job, SEND_HOLD_MARK))
        self._log("bridge", op="send_held", job_id=job.job_id, why=why)
        coro = self._held_send_watch(job, since, time.monotonic(), gen)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            task = asyncio.ensure_future(coro)
        else:
            task = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._send_watch = task
        return True

    def _note_held_words(self, text: str) -> None:
        """hands19 review (M2, M3): a final turn of theirs that reaches the talker (:meth:`answer_waiting`) while an
        "all" send countdown is open is kept with that job while its countdown is held (RAM only, never logged): the
        words its watch gives the job when the talker routes none of them. One heard while the countdown still counts
        (their speech began before it opened, or the final came after the watch counted on) pauses it now, as brain
        one's countdown does. Never raises."""
        try:
            words = " ".join(str(text or "").split())
            if not words or getattr(self, "one", False) or not self.on_brain or self._loop is None:
                return
            with self._lock:
                live = [j for j in self._jobs.values() if j.source == "voice" and j.state == RUNNING and j.sending
                        and not j.superseded and not j.cancel_requested and j.job_id is not None]
            if not live:
                return
            job = max(live, key=lambda j: j.started_at)
            at = self.heard_log.note(words) if self.heard_log is not None else None
            at = float(at) if at is not None else time.monotonic()
            if not job.send_held:
                self._hold_send(job, "words", at - 0.001)
            job.held_words[at] = words
            for old in sorted(job.held_words)[:-EXCHANGE_TURNS]:
                job.held_words.pop(old, None)
        except Exception as exc:  # noqa: BLE001 - the pause on their speech and the talker's own calls still hold
            self._log("bridge_error", error=f"note_held_words: {type(exc).__name__}")

    def _send_mark(self, job: Job, mark: str) -> None:
        """On the GUI thread: the countdown mark for ``job``'s running agent (``Runtime.send_choice``, the countdown's
        chip channel; any agent but the new brain's says no). A pause the agent did not take ends its watch."""
        if job.job_id is None:
            return
        choose = getattr(self.runtime, "send_choice", None)
        try:
            ok = bool(choose(int(job.job_id), mark, "voice")) if callable(choose) else False
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._log("bridge_error", error=f"send_mark: {type(exc).__name__}")
        if not ok and mark == SEND_HOLD_MARK:
            job.send_held = False   # no countdown took it (it ended meanwhile): nothing is paused
            job.send_gen += 1
        self._log("bridge", op="send_mark", job_id=job.job_id, mark=mark, delivered=ok)

    def _user_speaking(self) -> bool:
        """hands19 review (H2, M3): whether the user is speaking now: the conversation's VAD (``user_speaking``) or
        the microphone's voiced audio just now (``user_sound``), from the narrator gate's live state; ``False`` when it
        can't be read. State only."""
        state = getattr(self.gate, "_state", None) if self.gate is not None else None
        if not callable(state):
            return False
        try:
            s = state()
        except Exception:  # noqa: BLE001
            return False
        return isinstance(s, dict) and bool(s.get("user_speaking") or s.get("user_sound"))

    def _talker_routed(self, job: Job) -> bool:
        """hands19 review M2: whether the talker's own calls took the words held for ``job``'s countdown somewhere: a
        hand-off call came in after the first of them (a new request, a follow-up, a fact to keep, a replacement), or
        a hand or skill of its ran on their turn. State and times only."""
        first = min(job.held_words) if job.held_words else None
        if first is not None and any(at >= first for at in list(self._handovers_at)):
            return True
        called = getattr(self.hands, "called_this_turn", None) if self.hands is not None else None
        try:
            return bool(callable(called) and called())
        except Exception:  # noqa: BLE001
            return False

    def _held_steered_job(self) -> Job | None:
        """hands19 review M2: the open voice job of this conversation that code gave the user's newest heard words to
        while its countdown was held (:meth:`_held_send_watch`), when no newer turn was heard since; else ``None``. A
        hand-off of those same words then is neither a second steer nor a second job. Times only."""
        newest = self.heard_log.newest() if self.heard_log is not None else None
        if newest is None:
            return None
        convo = self._convo()
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.source == "voice" and j.held_steered_at is not None
                    and newest <= j.held_steered_at and j.state in OPEN_STATES and not j.superseded
                    and not j.cancel_requested and j.convo == convo]
        return max(jobs, key=lambda j: j.started_at) if jobs else None

    def _router_judging(self, now: float) -> bool:
        """integ23 S1: whether a router verdict on the user's words while a send counted down is still out (its
        deadline in :attr:`router_sends` not passed: decide's timeout plus the end's wait, about 6 s, inside the
        brain's pause limit). Times only; ``False`` when it can't be read."""
        try:
            return any(until > now for until in tuple((getattr(self, "router_sends", None) or {}).values()))
        except Exception:  # noqa: BLE001
            return False

    async def _held_send_watch(self, job: Job, since: float | None, held_at: float, gen: int) -> None:
        """hands19 (C4): a countdown paused on the user's speech, until its pause is decided. It ends when the countdown
        did (``job.sending`` cleared: a cancel, a replacement, their words routed to it, the time ran out) or a newer
        pause of it took over (``gen``). Else it waits while the talker is on their turn (they speak or their voice is
        heard, it writes or owes a reply, a tool or a hand-off of its is deciding) and, integ23 S1, while the router's
        verdict on their words is still out (:meth:`_router_judging`: its stop only works while the job still counts
        down), then:

        * words came (a final turn heard after ``since``), :data:`SEND_SETTLE_S` after the talker settled and at least
          ANSWER_HOLD_MIN_S after the last of them: review M2, when the talker routed none of the words it was given
          (it answered in words only, or stayed quiet; never in a call, where most words are for the others) they go to
          the job as their follow-up, which ends the countdown with nothing sent (the planner sends again only through
          a new countdown); else (it took them elsewhere, or they never reached it) the countdown counts on;
        * no words: review H2, :data:`SEND_NOISE_S` after their speech ended (the first quiet tick), never timed from
          its start (the final transcript comes 0.5-1.6 s after the VAD stop, and a reply is owed only then): noise,
          it counts on;
        * no view of the talker's state: ANSWER_HOLD_S after the pause or their last words.

        Counting on is the brain's ``SEND_GO_ON`` for the time it had left; the brain's own pause limit (nothing sent)
        bounds it all. States and times only; never what was said."""
        settled_at: float | None = None
        try:
            while True:
                await asyncio.sleep(SEND_TICK_S)
                if (job.send_gen != gen or job.sending is None or job.state not in OPEN_STATES or job.superseded
                        or job.cancel_requested or job.talker_cancelled):
                    return
                now = time.monotonic()
                newest = self.heard_log.newest() if self.heard_log is not None else None
                words = newest is not None and (since is None or newest > since)
                busy = self._talker_busy()
                if busy or self._user_speaking() or self._routing or self._router_judging(now):
                    settled_at = None
                    continue
                settled_at = settled_at if settled_at is not None else now
                last = float(newest) if words else held_at
                if busy is None:
                    due = now - max(held_at, last) >= ANSWER_HOLD_S
                elif words:
                    due = now - settled_at >= SEND_SETTLE_S and now - last >= ANSWER_HOLD_MIN_S
                else:
                    due = now - settled_at >= SEND_NOISE_S
                if not due:
                    continue
                in_call = False
                try:
                    in_call = bool(self.call_mode()) if self.call_mode is not None else False
                except Exception:  # noqa: BLE001
                    in_call = False
                if words and job.held_words and not in_call and not self._talker_routed(job):
                    text = " ".join(job.held_words[k] for k in sorted(job.held_words))
                    at = max(job.held_words)
                    turns = len(job.held_words)
                    how = await self._follow_up(job, text)
                    if how != "restart":
                        job.held_steered_at = at
                        self._log("bridge", op="send_words_steered", job_id=job.job_id, how=how, turns=turns,
                                  held_ms=round((now - held_at) * 1000))
                        return
                    # their words could not reach it (it is finishing): never counted on past them; the brain's
                    # pause limit ends it with nothing sent
                    self._log("bridge", op="send_words_unsteered", job_id=job.job_id, turns=turns)
                    return
                if self.closing or self._loop is None:
                    # integration review VC-1: the conversation is ending (the user ended it, by the router's or the
                    # talker's judgement, or any end): never counted on past it; the brain's pause limit ends it with
                    # nothing sent, as brain one's close() does
                    self._log("bridge", op="send_held_closing", job_id=job.job_id)
                    return
                job.send_held = False
                self._gui(lambda: self._send_mark(job, SEND_GO_ON_MARK))
                self._log("bridge", op="send_go_on", job_id=job.job_id, words=words, view=busy is not None,
                          held_ms=round((now - held_at) * 1000))
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - the brain's pause limit ends it then (nothing sent)
            self._log("bridge_error", error=f"held_send_watch: {type(exc).__name__}")

    def cancel_passed_on(self, said: bool = True) -> bool:
        """hands19 (final-B all-messaging RC3, contract C4): the talker's cancel when no hand-off call of its is running
        (the pipeline's cancel tool, through a ``getattr`` guard): the open voice job code passed on for their words
        (a ``run_skill`` or hand fallback, a held run, a late addition: :attr:`Job.detached`, no call carries it) is
        stopped: this user turn's, else the newest of this conversation (p24_countdown_cancel: the send was passed on
        in turn 1, "wait, no, don't send it" was turn 2, and the cancel tool answered "no hand_over call is running"
        five times). Nothing more of it is said (the talker says it is stopping in the same reply), unless its send
        went. ``True`` when one was stopped. Integration review VC-2: one whose send went already is not stopped
        (``False``, with :attr:`cancel_found_sent` set). Never under brain one. State only; never raises.

        voice20 (final-D S11): ``said`` false: the talker's cancelling reply said no words (a bare cancel call; live
        final-D p24_countdown_cancel under all: the cancel landed and Yuki said nothing). The job's own ending is then
        said by code once it has stopped (:meth:`_relay_detached`, :meth:`_say_stopped`): never a silent cancel."""
        self.cancel_found_sent = False
        try:
            if getattr(self, "one", False):
                return False
            now = self._turn_now()
            convo = now[0] if now is not None else self._convo()
            with self._lock:
                live = [j for j in list(self._jobs.values()) + list(self._unsubmitted.values())
                        if j.source == "voice" and (j.escalated or j.detached) and j.state in OPEN_STATES
                        and not j.superseded and not j.cancel_requested and not j.remember and not j.unadopted
                        and j.convo == convo]
            if not live:
                self._log("bridge", op="cancel_passed_on", found=False)
                return False
            same = [j for j in live if now is not None and j.user_turn == now[1]]
            job = max(same or live, key=lambda j: j.started_at)
            if job.sent:
                # integration review VC-2: its send went already (state "going"): nothing is stopped, and the caller
                # says it went (:attr:`cancel_found_sent`), never that it will not go on
                self.cancel_found_sent = True
                self._log("bridge", op="cancel_passed_on", found=True, job_id=job.job_id, sent=True)
                return False
            job.talker_cancelled = True
            job.cancel_requested = True
            job.talker_silent = not said
            # a send of it counted down (counting or paused now, or paused on their speech before)
            job.cancel_had_send = job.sending is not None or job.send_held or job.send_gen > 0
            self._gui(lambda: self._cancel(job))
            if self.gate is not None:
                for item in self.gate.take(job.token, kinds=tuple(RANKS)):
                    if item.drop is not None:
                        item.drop()
            self._unowe(job)
            self._log("bridge", op="cancel_passed_on", found=True, job_id=job.job_id, this_turn=bool(same),
                      sending=job.sending is not None, said=bool(said))
            self._changed()
            return True
        except Exception as exc:  # noqa: BLE001
            self._log("bridge_error", error=f"cancel_passed_on: {type(exc).__name__}")
            return False

    def _brain_escalate(self, params: Any, name: str, args: dict[str, Any], report: str, heard: list[str], *,
                        acked: bool = False, context: str | None = None, skill: dict[str, Any] | None = None) -> bool:
        """bTalk: :meth:`escalate` under ``brain = "all"``: a hand of the talker's that did not take effect, a skill's
        hand-off (rung C), or a hand handed over because it could send or join (design 3, L10: never refused
        silently). The user's exact words go as their words; what the hand did, as labelled context."""
        turns = self._turns(params.context, heard)
        request = " ".join(turns) or _action_words(name, args)
        if context:
            note = " ".join(str(context).split())[:1200]
        elif skill:
            note = (f"Yuki's voice ran the user's usual way ({skill.get('key')}) and it did not get there: "
                    f"{' '.join(str(report).split())[:300]}")
        else:
            note = (f"Yuki's voice tried one step itself ({name}) and it did not take effect: "
                    f"{' '.join(str(report).split())[:400]}")
        ctx = self._voice_context(params.context, turns, extra=note)
        job = self._new_job(request)
        job.escalated = True
        job.acts = True
        job.acked = acked
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        # Item 2(d): never _pass_over_questions here (live 10 Oct a refused click withdrew the job waiting for exactly
        # that yes); a question still open is set aside by the runtime while this runs, and its answer carries it on
        query = _memory_words(request, heard)
        pin = dict(skill) if isinstance(skill, dict) and skill.get("key") else None
        self._gui(lambda: self._submit(job, request, origin, lane="worker", memory_query=query, user_words=turns,
                                       skill=pin, context=ctx))
        self._log("bridge", op="submit", token=job.token, call=params.tool_call_id, intent="escalated", brain=True,
                  hand=name, words_chars=len(request), turns=len(turns), context_chars=len(ctx), origin_hwnd=origin,
                  skill=pin.get("key") if pin else None)
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    async def _brain_run_held(self, args: dict[str, Any], context: Any, heard: list[str]) -> bool:
        """bTalk: :meth:`_run_held` under ``brain = "all"``: the held hand-off done after all, on the exact words."""
        turns = self._turns(context, heard)
        request = " ".join(turns)
        if not request:
            return False
        job = self._new_job(request)
        job.acked = bool(say_first_words(args.get("say_first")))
        with self._lock:
            self._unsubmitted[job.token] = job
        origin = self._foreground()
        ctx = self._voice_context(context, turns)
        query = _memory_words(request, heard)
        self._gui(lambda: self._submit(job, request, origin, memory_query=query, user_words=turns, context=ctx))
        words = say_first_words(args.get("say_first"))
        if words and self.speak is not None:
            await self.speak(words)
        self._log("bridge", op="submit", token=job.token, intent="held_run", brain=True, words_chars=len(request),
                  turns=len(turns))
        self._changed()
        self._arm_wait(job)
        asyncio.ensure_future(self._relay_detached(job))
        return True

    def _note_runner(self, job: Job, lane: str) -> None:
        """bTalk (review r1 M1): mark ``job`` as running on the new brain when the runtime's lane is driving it with a
        :class:`yuki.brain.Brain` now (its ``active`` agent, read before its ``current_id``, so a run that changed in
        between is never taken for this one). Called from the job's own signals while it runs; a run seen by none of
        them stays unmarked, and its words keep the old rules (the honesty gate on). Identity of objects only."""
        if not self.on_brain or job.brain_run or job.job_id is None:
            return
        try:
            get = getattr(self.runtime, "lane", None)
            obj = get(lane) if callable(get) else getattr(self.runtime, str(lane), None)
            agent = getattr(obj, "active", None)
            if agent is None or getattr(obj, "current_id", None) != job.job_id:
                return
            # no Brain was ever made when its module is not loaded (never imported here, on the GUI thread)
            brain = getattr(sys.modules.get("yuki.brain"), "Brain", None)
            if isinstance(brain, type) and isinstance(agent, brain):
                job.brain_run = True
        except Exception:  # noqa: BLE001 - unknown: unmarked
            return

    def _lane_brain(self, name: str = "worker") -> Any:
        """The new brain of the runtime's lane ``name`` (the runtime's ``Lane.brain()``, made there on first use under
        ``brain = typed / all``), or ``None`` (no such lane, an older runtime, or none can be made). Blocking on its
        first use: call it off the voice loop."""
        lane = getattr(self.runtime, name, None)
        get = getattr(lane, "brain", None)
        if not callable(get):
            return None
        try:
            return get()
        except Exception as exc:  # noqa: BLE001 - then nothing is read ahead
            self._log("bridge_error", error=f"lane_brain: {type(exc).__name__}")
            return None

    def _brain_prefetch(self, words: str) -> None:
        """bTalk (design 10.1, ``Brain.begin_early``): the user's turn just ended with ``words``: the new brain reads its
        request turn now (the window list, "you right now", people named, profile, menu), off every loop, so a hand-off
        of these words finds it ready. Only while the worker is idle (it takes the request then) and not in a call
        (most words there are for the others); returns at once. The brain uses it only for the same words."""
        text = " ".join(str(words or "").split())
        if not text or self._pack_building:
            return
        busy = getattr(self.runtime, "worker_busy", False)
        try:
            busy = busy() if callable(busy) else bool(busy)
        except Exception:  # noqa: BLE001
            busy = True
        if busy:
            return
        if self.call_mode is not None and self.call_mode():
            return
        self._pack_building = True

        def read() -> None:
            try:
                brain = self._lane_brain("worker")
                begin = getattr(brain, "begin_early", None)
                if callable(begin):
                    begin(text)   # returns at once: the brain reads on a thread of its own
                self._log("bridge", op="brain_prefetch", started=callable(begin))
            except Exception as exc:  # noqa: BLE001 - the brain reads what it needs itself
                self._log("bridge", op="brain_prefetch", error=type(exc).__name__)
            finally:
                self._pack_building = False

        from yuki.voice.workers import shared

        shared().submit(read)

    def _status_line(self, job: Job) -> str | None:
        """The running plan's status in plain words (``Brain.status_line()``, by code), for the job its lane runs now, or
        ``None`` (another agent, or nothing to say)."""
        if job.lane is None or job.job_id is None:
            return None
        lane = None
        try:
            get = getattr(self.runtime, "lane", None)
            lane = get(job.lane) if callable(get) else None
        except Exception:  # noqa: BLE001
            lane = None
        lane = lane if lane is not None else getattr(self.runtime, str(job.lane), None)
        if lane is None or getattr(lane, "current_id", None) != job.job_id:
            return None
        status = getattr(getattr(lane, "active", None), "status_line", None)
        if not callable(status):
            return None
        try:
            line = " ".join(str(status() or "").split())
        except Exception:  # noqa: BLE001
            return None
        return (line if len(line) <= 200 else line[:199] + "…") or None

    def _brain_work_line(self, now: float) -> str | None:
        """bTalk (design 3, 7): :meth:`work_line` under ``brain = "all"``: the running task's plan status
        (:meth:`_status_line`), or the question it waits on, what is queued behind it and what finished in the last
        minute with its line, each with its task number. State only, by code; "how's it going?" is answered from it."""
        tool = self._handoff_tool
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
                parts.append(f'{name} "{j.brief()}" paused for {ago} s: they took over the keyboard or mouse, so it '
                             f'waits for them (their wish to go on, or anything they add, is for this task: {tool} '
                             f'follow_up; their stop is cancel_{tool}; say nothing of the pause unless they ask)')
            else:
                status = None if j.sending else self._status_line(j)
                step = (f", now: about to send it to {j.sending}, a few seconds left to stop it (their stop or no now "
                        f"is for this send: cancel_{tool})" if j.sending else
                        f", now: {status}" if status else
                        f', now: "{j.step_line}"' if j.step_line and j.step_running else
                        f", now: {j.step.lower()}" if j.step else ", starting")
                parts.append(f'{name} "{j.brief()}" running for {ago} s{step}')
        if done:
            j = done[-1]
            what = {DONE: "finished", FAILED: "failed", CANCELLED: "stopped"}.get(j.state, j.state)
            if j.paused_out:
                what = "stopped after waiting paused (they had taken over and nobody went on with it)"
            elif j.superseded:
                what = "withdrawn for a newer request"
            outcome = condense(j.outcome or "", max_sentences=1, max_chars=120)[0] if j.state == DONE else ""
            parts.append(f'task {j.job_id} "{j.brief()}" {what} {round(now - (j.finished_at or now))} s ago'
                         + (f': "{outcome}"' if outcome else ""))
        return "; ".join(parts) or None


#: bTalk: the labelled note a spoken request to keep goes with (design 3: the talker's ``remember`` intent; never
#: steered in as the user's words).
REMEMBER_CONTEXT = ("Yuki's voice took these words as something to keep: a rule about how Yuki talks or behaves, a "
                    "correction of something Yuki got wrong about them, a fact about their life, or how they do "
                    "something on this PC (the app or way they use). Keep it with "
                    "remember when it is one (something about this moment only is not kept). The voice has already "
                    "answered them, so the answer is not spoken unless keeping it failed; ask them nothing but the "
                    "one question a keep held to ask first needs.")
#: integ30 (le merge note 1, contract K-R talker side): the talker's remember hand-over that declares it turns around
#: something given in this talk (``turns_around``, a required field of ``mouth.HAND_OVER``) is held by code, as the
#: shared handler holds the planner's and ONE's (``brain.tools.reversal_hold``): nothing goes to the planner, the call
#: settles quietly (its say_first was the one question), and the next remember hand-over after Yuki's words were heard
#: past the turn that carried it goes through (:meth:`VoiceBridge._reversal_gate`).
REMEMBER_HELD = "not kept: their answer to your question decides"
REMEMBER_HELD_NOTE = "Nothing was kept. Their answer is the change: hand_over intent remember again then."
#: integ30 fix (T2): the same hold when its question was never said (measured: no words of the call's say_first, nor
#: of the reply, went out): the talker asks it now.
REMEMBER_HELD_ASK_NOTE = ("Nothing was kept, and your question was not said. Ask it now: one short question naming "
                          "both. Their answer is the change: hand_over intent remember again then.")
#: integ30 (K-R): the labelled, code-measured line a released keep goes to the planner with (the held item is the
#: talker's own field; "heard" is the narrator's measured time), so its remember names their answer as their_reason.
REMEMBER_ASKED = ("Yuki's voice held a keep that turns around {around}, to ask them first; its question was heard, "
                  "and these are their words since. That change is kept only on their yes (their_reason: their "
                  "yes); anything else they want kept is kept as usual.")
#: bTalk: the note a late addition (words said after the request's last step) goes with.
LATE_ADDED = "Added by voice right after the previous request finished: take it from what the screen shows now."


def code_line(verdict: dict[str, Any] | None) -> bool:
    """bTalk: whether a new-brain result is code's own line (design 2.4): the probe's line after an act, "couldn't
    confirm", the stuck line, a refusal or a decline (any level but ``answer``, or a ``line`` the outcome names); a
    model's answer (level ``answer`` with no line) is not. The outcome dict's own fields, never the words."""
    if not isinstance(verdict, dict):
        return False
    level = str(verdict.get("level") or "")
    return level not in ("", "answer") or bool(str(verdict.get("line") or "").strip())


def success_line(verdict: dict[str, Any] | None) -> bool:
    """Phase 2 (2.3): whether a new-brain outcome's code line is a success said fast by code: level ``verified`` or
    ``likely``, nothing of it partial or left open. Every other outcome (not confirmed, failed, partial, a stuck line, a
    refusal or decline) is worded by the talker from its evidence. The outcome dict's own fields, never the words."""
    if not isinstance(verdict, dict):
        return False
    level = str(verdict.get("level") or "")
    return level in ("verified", "likely") and not verdict.get("partial") and not int(verdict.get("open") or 0)


def verdict_line(verdict: dict[str, Any] | None, text: str | None) -> bool:
    """bTalk (review r1 M1): whether ``text`` is the outcome's own code ``line``, word for word (whitespace aside):
    code's sentence in either brain, never a model's claim. String identity only."""
    if not isinstance(verdict, dict) or text is None:
        return False
    line = " ".join(str(verdict.get("line") or "").split())
    return bool(line) and " ".join(str(text).split()) == line


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        return " ".join(" ".join(str(p.get("text") or "").split()) for p in content
                        if isinstance(p, dict) and p.get("type") == "text").strip()
    return ""


def conversation_turns(context: Any, *, skip: Callable[[Any], bool] | None = None, limit: int = 6,
                       chars: int = 240, own: list[str] | None = None) -> list[tuple[str, str]]:
    """bTalk: the talker conversation's lines before the user's words since Yuki last replied, oldest first, as
    ``(who, text)`` (``"they"`` / ``"Yuki"``; Yuki's lines include what code said in its voice), at most ``limit``,
    each cut to ``chars``. The reply being written now and the request's own words are passed over, as
    :func:`heard_words` passes them; ``skip`` leaves out user-role messages nobody said. The context's structure only.

    ``own`` (IF-words, Item 2a): the request's own words, the user's turns of the current exchange (which may reach
    back past Yuki's replies): each of their messages that is one of these is passed over wherever it stands, and Yuki's
    lines between them stay (a question it asked, a failure it said), so no line of theirs is both their request and
    "never their request"."""
    try:
        messages = list(context.get_messages() or [])
    except Exception:  # noqa: BLE001
        return []
    mine = {" ".join(str(t).split()) for t in (own or []) if str(t).strip()}
    out: list[tuple[str, str]] = []
    phase = 0   # 0: the reply being written now; 1: the request's own words; 2: earlier lines
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message)
        if role == "user" and (not text or (skip is not None and skip(message))):
            continue
        if phase == 0:
            if role == "user":
                phase = 1
            continue
        if phase == 1:
            if role == "user":
                continue
            phase = 2
        if not text:
            continue
        if role == "user" and text in mine:
            continue   # one of the request's own turns (given apart, as their words)
        out.append(("they" if role == "user" else "Yuki", text if len(text) <= chars else text[: chars - 1] + "…"))
        if len(out) >= limit:
            break
    return list(reversed(out))


#: An understanding's parts kept, and the characters of each text field.
UNDERSTANDING_PARTS = 6
UNDERSTANDING_CHARS = 300


def _done_here_note(done: list[str]) -> str:
    """hands19: :data:`DONE_HERE_CONTEXT` for ``done`` (code's words), led by the brain's own label for it
    (``plan.DONE_HERE_LEAD``, review M1) even if the two wordings ever drift apart."""
    note = DONE_HERE_CONTEXT.format(done="; ".join(done))
    try:
        from yuki.brain.plan import DONE_HERE_LEAD
    except ImportError:   # an older brain: no answer turn reads it
        return note
    return note if note.startswith(DONE_HERE_LEAD) else f"{DONE_HERE_LEAD}: {note}"


def understanding_of(raw: Any) -> dict[str, Any] | None:
    """The hand_over call's ``understanding`` in the shape of contract C3 (``{"goal": str, "parts": [{"what": str,
    "skill": str | None, "slots": {}}], "choose": bool, "context": str}``), cut to size; ``None`` when there is none.
    Plumbing on our own tool's fields, never a reading of the words."""
    if not isinstance(raw, dict):
        return None

    def text(value: Any) -> str:
        return " ".join(str(value or "").split())[:UNDERSTANDING_CHARS]

    parts: list[dict[str, Any]] = []
    for part in (raw.get("parts") if isinstance(raw.get("parts"), list) else [])[:UNDERSTANDING_PARTS]:
        if not isinstance(part, dict) or not text(part.get("what")):
            continue
        slots = part.get("slots") if isinstance(part.get("slots"), dict) else {}
        parts.append({"what": text(part.get("what")), "skill": text(part.get("skill")) or None,
                      "slots": {str(k)[:40]: v for k, v in list(slots.items())[:8]}})
    out = {"goal": text(raw.get("goal")), "parts": parts, "choose": bool(raw.get("choose")),
           "context": text(raw.get("context"))}
    return out if (out["goal"] or parts or out["choose"] or out["context"]) else None


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


def _acting_on(name: str, tool_input: dict | None) -> tuple[int | None, str]:
    """lVoice (problem 2): ``(hwnd, tab id)`` a thinker's desktop step leaves its effect on, from its own input (the
    window it names, the tab it selects by id), so the action log drops the line once that window or tab is gone. A
    close leaves nothing there (its line says it is gone, and stays). Plumbing of the tools' fields, never words."""
    data = tool_input if isinstance(tool_input, dict) else {}
    if str(data.get("action") or "") == "close":
        return None, ""
    raw = data.get("hwnd", data.get("expect_hwnd"))
    try:
        hwnd = int(raw) if raw not in (None, "", 0) and str(raw).lstrip("-").isdigit() else None
    except (TypeError, ValueError):
        hwnd = None
    tab = str(data.get("tab") or "") if name == "tab" and str(data.get("action") or "") == "select" else ""
    return hwnd, tab


def _foreground_hwnd() -> int | None:
    """The window in front at the end of the user's turn (``origin_hwnd``, §7.5)."""
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        return hwnd or None
    except Exception:  # noqa: BLE001
        return None
