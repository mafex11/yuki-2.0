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

**One20 (final-D)**: code says the measured tab-cover sentence (``perception.diff.cover_line``) of a flagged result.
**tidy21** (the user's decision 1, 19 Oct): under the tidy dial "do", code takes the flagged diff's one legal undoable
step (``perception.diff.tidy_step``, contract C7) itself (:meth:`Runner._tidy_by_code`: ``verbs.run("window", ...)``
through the gate's row 8, once a turn) and says its line; the result tells the agent what code did (``tidied``) and
keeps no window flag for it, in ``flags`` too (review F2: so voice ONE settles by code). Never a move of a window the
turn's own acts placed or sized as asked (review F1: ``RunState.arranged``), never the front given to Yuki's own window
(F5), and a later flagged act of the turn settles by code too (F4: one tidy decision a turn, as under ``all``). At
"ask" the result carries the step for the question only (``side_effect``); "off": nothing. A ``focus_back`` with no
window goes to the measured front before the act
(S2); a skill run that handed back gets the second way the planner gave it under ``all``, once, in code (S5: a rung
that never read runs once more; a pick-kind value no route found runs as Yuki's pick with it as context), never when
its value was only the agent's reading of their words (S12: that miss goes back to the agent, which decides); the
hand-off text is written for ONE.

**One22**: a reading no route found, in a slot that holds an app or a site, is measured as a name against the user's
own apps and sites (A5, decision 3: :meth:`Runner._names_winner`, ``skills.names.best_match`` on the agent's own value;
a channel is never one); a clear winner is acted on by code (:meth:`Runner._take_name`: an open or a switch, through
the gate, a site in the browser the run named). ``remember`` kind ``way`` keeps the way the acts of this turn and the
turns just before took in one app as the user's own told way (A13, contract C13, :meth:`Runner._keep_way`). The
agent's own typing is marked as Yuki's unsent text only once it may have landed (A21, :func:`_typing_landed`).

**kw2** (20 Oct, the user's direction: intent, never word logic): code no longer measures their whole newest turn
against their names (B1), never reads "not that one" from which words they repeated (B2: the agent's own ``corrects``,
``again`` and ``choose`` decide), never counts a value's words against a screen read nor asks "Did you mean X?" itself
(B3). A reading no route found and no name measures goes back to the agent with what the run read
(:data:`GUESS_BACK`): the agent asks, picks or tries again.

**fb** (final-F, lane B; measured facts and the model's structured fields only): the tidy step's front is the one the
act took (``DeskDiff.taken_front``, contract K-B: ``focus_taken``'s window, never ``focus_by_act`` alone); the tidy
dial "off" settles a flagged result by code; code's D6 line is not said over a desk whose front moved on; an asked
skill run that touched nothing is no act; a countdown their words changed owes ``LINE_STOPPED_SEND`` (contract K-C);
a skill run settled ``unverified`` is no miss of the retry window (``budget``); a done line equal to the say_first
the voice already spoke for the call is not said again (contract K-D, ``early_said``).

**integ27** (the lanes' merge notes, final-F): a close that raised the page's or the app's own question, or that was
pressed and stayed, is ``asked`` (never failed): no tidy step around it, and no call of the turn acts in that window
until their words change (:attr:`RunState.held`, :meth:`CallContext._held`; fa note 7). A master level or mute change
during a turn with no sound control's call is the diff's ``master_changed``: code says ``diff.master_line`` once a
turn and a flag set that is only that settles by code (fa note 3). At the tidy dial "ask" code says the measured
question itself (``diff.tidy_ask``, fa note 4). A verified browser focus says the tab it shows (``focus_line``, fa
note 5). The D6 line's front test is ``diff.front_moved_on``, one rule for both brains (fb note A3). A countdown a
chip changed owes nothing (fc note 3).

**The turn state** (:class:`RunState` on the gate's ``Turn``): every result, the act's own windows (tidy), the lines
said; ``private_read`` and the refusal count are the gate's and the budget's (``policy.Turn``, ``budget.TurnBudget``).

Logs: one content-free line per call (``one_run``: tool, verb, verdict, status, verified, ms, flags, sizes, the
budget's counts). Every helper fails closed: an error is a refusal or a failure, never an act.
"""

from __future__ import annotations

import asyncio
import json
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
#: tidy21 (the user's decision 1, 19 Oct): under the tidy dial "do" code takes the flagged diff's one legal undoable
#: step itself (:meth:`Runner._tidy_by_code`) and says its line; the result tells the agent what code did
#: (:data:`TIDIED`), never asks it to take the step (one20's "take it" note is gone, so it can't happen twice). With
#: the dial at "ask" the result carries the step for the question only (:data:`TIDY_ASK_NOTE`; the gate's row 8 asks
#: before any such step runs).
TIDY_ASK_NOTE = ("the flags are a side effect of your act on their screen; one undoable step undoes it: {step}. Their "
                 "settings ask first: ask in a few words whether to (the question alone); on their yes, call it.")
TIDIED: dict[str, str] = {
    "taken": "code undid that side effect with its one undoable step and said so (said)",
    "failed": "code's one undoable step to undo that side effect did not take, and code said so (said)",
    "left": "code left their windows as they are, on purpose",
    # integ22 (desk22 merge note 4b, D6): no legal step for a window flag: code said its one line about what is covered
    "told": "no one undoable step undoes it, so code said what is covered (said) and left their windows as they are",
    # fb (final-F B2): the tidy dial off: no window flag is anyone's to act on (the hands give no look at "off")
    "off": "their settings leave their windows to them: code left them as they are, on purpose",
    # integ27 (fa merge note 4, A7): the dial at "ask": code asked the question itself (said); the step is in
    # side_effect for their yes
    "asked": "code asked them whether to undo that side effect (said); say nothing more about it now",
    # integ27 (fa merge note 3, A9): a master change during an act not about the volume: code said what was measured
    # (said); nothing is undone (its cause is not known)
    "master": "code told them their PC's volume changed while this ran (said); nothing is undone: say nothing more "
              "about it",
    # integ27 (fa merge note 7, A1): the close raised the page's or the app's own question: code took no tidy step
    # around it (a focus_back would hide it, a key would answer it)
    "asking": "the page's or the app's own question is up in front of them: code left every window as it is, and "
              "nothing in that window is done this turn"}
#: integ27 (fa merge note 4, A7, final-F all p1_dial_ask_playlist_over_video): the dial at "ask" and code said the
#: measured question itself (``diff.tidy_ask``): what the agent reads (its yes is the step).
TIDY_ASKED_NOTE = ("code asked them in a few words whether to undo that side effect with its one undoable step: "
                   "{step}. Say nothing more about it now; on their yes, call it.")
#: integ27 (fa merge note 7, A1): code's question for a close that raised the page's or the app's own question, and
#: its line for a tab close pressed that stayed with no window measured (``voice.hands._FALLBACK_LINE``'s words): the
#: result's ``question`` (the voice owes it; the typed loop asks it).
LINE_CLOSE_ASKS = "It's asking something before it closes, so it's still open. Want me to go ahead?"
LINE_CLOSE_HELD = "I couldn't see it close. It may be asking you something first, so I left it."
#: integ27 (fa merge note 7): a step in a window a close of this turn left asking (:meth:`CallContext._held`).
LINE_CLOSE_ASKED = ("Not done: that window shows its own question, raised by your close this turn, and its answer is "
                    "theirs, so nothing more is done in it this turn. Ask them in a few words, with no call.")
#: The name of code's own tidy step's call context: no ONE tool, so the gate judges it as an inner step (never counted
#: on the agent's budget or refusals), at row 8 like any tidy step.
CODE_TIDY = "tidy_by_code"
#: How long the window in front before an act that moved the front is kept for a ``focus_back`` with no window
#: (``voice.hands._FRONT_KEEP_S``: the same "put it back" reach as under ``all``).
FRONT_KEEP_S = 600.0
#: One20 (final-D S12): a run that didn't land on a value that is only the agent's own reading of their words (none of
#: their things, not in what they said, no route found it), and that would change what plays or take the front.
#: One22 (A5, the user's decision 3): in a slot that holds an app or a site, that reading is measured as a name against
#: the user's own apps and sites (``skills.names.best_match`` on the value); a clear winner is acted on by code
#: (:meth:`Runner._take_name`). kw2 (B1, B3): nothing else is measured and code asks nothing: the miss goes back to the
#: agent with this note and the run's own evidence (what it read), and the agent decides.
GUESS_BACK = ("No route found {value}: it is none of their things, not in their words, and none of their apps or sites "
              "is that name. If it was your reading of their words, decide: ask them in a few words, or act on another "
              "reading that plainly fits; never run_skill with {value} again.")
#: One22 (A5): code acted on the measured name in place of the reading (the model reads it; the done line was code's).
NAME_TAKEN = ("Code took their words as {name}, one of their own {kind}s (measured by spelling and sound), in place of "
              "{value} (nothing of {value} ran), and {did}. Never name the reading to them.")
#: What code did with the measured name, by kind and by whether it took.
NAME_DID: dict[tuple[str, bool], str] = {("app", True): "brought it up", ("app", False): "tried to bring it up",
                                         ("site", True): "opened it", ("site", False): "tried to open it"}
#: The name of code's own act on a measured name (no ONE tool: the gate judges it as an inner step at its rows).
CODE_NAMES = "names_by_code"
#: One22 review M1: the bind sources whose slot is a thing's own value (a page's item, a page kind, a person): never
#: measured as an app or site name, whatever the skill's kind (``connection.BIND_SOURCES``).
ENTITY_BINDS = frozenset({"entity", "page", "contact"})
#: One22 review L1: how far back the steps a told way is kept from reach when ``policy.STEPS_KEEP_S`` is not there (the
#: talker hands' correction window, ``skills.prefs.CORRECTION_S``).
CORRECTION_FALLBACK_S = 600.0
#: One22 (A13, contract C13): ``remember`` kind ``way``: the way the acting steps of this turn and the turns just before
#: took in one app (review L1: the steps kept across turns, ``policy.Turn.steps_before``), kept as the user's own told
#: way (``skills.learn.ways.spec_from_steps`` then ``keep_told``, read with getattr).
WAY_KIND = T.WAY_KIND
LINE_WAY_KEPT = "Kept as their own way in {app}: {name}. Next time they ask for it by name it runs that way."
LINE_WAY_NAME = "Not kept: give the way a name in their words (text), then call it again."
LINE_WAY_NO_STEPS = "Not kept: no act of yours took in the last few minutes, so there is no way to keep."
LINE_WAY_GAP = ("Not kept: a step of it didn't take, so that way might not run again; say so in a few words (they can "
                "show it once more).")
LINE_WAY_UNBUILT = "Not kept: Yuki can't keep a told way here yet; say so in a few words."
LINE_WAY_NO_SPEC = "Not kept: those steps make no way Yuki can run again; say so in a few words."
LINE_WAY_FAILED = "Not kept: memory didn't take it; say so in a few words."
#: One22 review L5: how long a told way's write may take (latency is not a goal: a slow write that still commits must
#: not read as "not kept").
WAY_WRITE_S = 3.0
#: One20 (final-D S5): the engine's outcomes after which a run handed back with its request still open.
OPEN_OUTCOMES = frozenset({"handed_over", "failed"})
#: The engine's hand-off reasons after which nothing may run again (it may have acted, or a step was the user's):
#: ``engine.WHY_UNVERIFIED``, ``engine.WHY_ERROR``, ``engine.TO_PLANNER``, ``engine.WHY_DROPPED``.
NO_SECOND_RUN = frozenset({"unverified", "error", "to_planner", "dropped"})
#: A second way re-runs every pick kind (``taste.CHOOSE_KINDS``, read through :meth:`Runner._pick_slot`, so a new pick
#: kind is covered by its data) with ``choose``, except the engine's structural exception: ``engine._choice_request``
#: never chooses for ``web.open_page`` when an item is given (its ``item`` slot is any site's page, not only a pick),
#: so its re-run would drop the item and open another page than asked.
NO_PICK_RERUN = frozenset({"web.open_page"})
#: The fill sources that are only the caller's reading of their words (``resolve.FROM_READING``: the resolver's own
#: tag on the value, its data, never a reading of words).
READING_SOURCES = frozenset({"reading"})


def _desk_helper(name: str) -> Callable[..., Any] | None:
    """Contract C7 (lane DESK): ``yuki.perception.diff.tidy_step`` / ``cover_line``, pure; ``None`` until merged."""
    try:
        from yuki.perception import diff as desk_diff
    except ImportError:
        return None
    got = getattr(desk_diff, name, None)
    return got if callable(got) else None


def _opened_of(name: str, args: Mapping[str, Any], facts: Mapping[str, Any], acted: Sequence[Any],
               touched: Sequence[Any]) -> tuple[Any, ...]:
    """integ22 (desk22 merge note 4a, A24): the windows an act opened a page in, for the diff's ``opened``
    (``voice.hands._opens_page``: an open_url, a tab's ``new``, a skill run of kind open, by its scope's ``open``): the
    act's windows by id when measured, its apps only when none was (another window of the same browser is never the
    act's tab), as the talker's look passes them. ``()`` for any other act. Never raises."""
    try:
        from yuki.voice.hands import _opens_page

        if not _opens_page(str(name), dict(args or {}), dict(facts or {})):
            return ()
        found = [x for x in (*acted, *touched) if isinstance(x, int) and not isinstance(x, bool)]
        found = found or [x for x in (*acted, *touched) if isinstance(x, str) and x.strip()]
        return tuple(dict.fromkeys(found))
    except Exception:  # noqa: BLE001 - no opened scope: the classic tab cover only
        return ()


def _close_asking(facts: Mapping[str, Any]) -> bool:
    """integ27 (fa merge note 7, A1): the hands measured that this close raised the page's or the app's own question
    (``dialog_raised``: a window it owns came up in front, with or without its id; a window close measured asking) or
    that a tab close was pressed and its tab stayed with no window measured (``close_held``: a browser that asks inside
    its own window). The hands' own facts, never words. Never raises."""
    try:
        return bool(facts.get("dialog_raised") or facts.get("close_held"))
    except Exception:  # noqa: BLE001
        return False


def _sound_call(name: str, args: Mapping[str, Any]) -> bool | None:
    """integ27 (fa merge note 3, A9): whether a ONE call is a sound control's (``voice.hands._sound_act``: the mixer
    hand, the volume keys, the volume skill; our own tool and skill keys, never words); ``None``: unknown (the caller
    reads it as a sound act: the safe side, no master flag). Never raises."""
    try:
        from yuki.voice.hands import _sound_act

        return bool(_sound_act(str(name), dict(args or {})))
    except Exception:  # noqa: BLE001
        return None


def _step_call(step: Any) -> dict[str, Any] | None:
    """A legal tidy step (``diff.tidy_step``: ``{"action": "focus_back" | "place", "hwnd", "monitor"?, "how"?}``) as
    the ``window`` verb's own arguments (a place is the verb's ``move``); ``None``: not such a step."""
    if not isinstance(step, Mapping):
        return None
    action = str(step.get("action") or "")
    hwnd = step.get("hwnd")
    if isinstance(hwnd, bool):
        return None
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        return None
    if not hwnd:
        return None
    if action == "focus_back":
        return {"action": "focus_back", "window": hwnd}
    if action == "place":
        call: dict[str, Any] = {"action": "move", "window": hwnd, "monitor": _words(step.get("monitor")) or "other"}
        if step.get("how") in ("keep", "maximize", "left", "right"):
            call["how"] = step["how"]
        return call
    return None


def _step_words(step: Any, d: Any = None) -> str:
    """One20 (C7): a legal tidy step as the agent's own ``window`` call (its arguments, :func:`_step_call`), with the
    window's name from the diff; ``""``: not such a step. Safety timing 3: a place names the measured target monitor
    (``step["to"]``), so the agent's call on their yes (a later turn) moves the window to the screen asked about, never
    "other" from wherever they put it meanwhile (code's own step, :meth:`Runner._tidy_by_code`, runs at once)."""
    call = _step_call(step)
    if call is None:
        return ""
    to = step.get("to") if call.get("action") == "move" else None
    if isinstance(to, int) and not isinstance(to, bool):
        call["monitor"] = str(to)
    names = getattr(d, "names", None)
    name = _words(names.get(call["window"])) if isinstance(names, Mapping) else ""
    return f"window {json.dumps(call)}" + (f" ({name})" if name else "")


def _value_text(value: Any) -> str:
    """A slot value as shown (a canonical entity's ``name`` / ``key`` / ``title``, else the text)."""
    if isinstance(value, Mapping):
        for k in ("name", "title", "key"):
            if _words(value.get(k)):
                return _words(value.get(k))
        return ""
    return "" if isinstance(value, (bool, int, float)) or value is None else _words(value)


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
    #: One20 (C7): the flagged diff's one legal undoable step in the agent's words, given only with the tidy dial at
    #: "ask" (tidy21: under "do" code takes it itself) (``""``: none).
    tidy_step: str = ""
    #: tidy21 (decision 1): what code did about the flagged diff's windows (:data:`TIDIED`: its one step taken, not
    #: taken, or left), for the agent; ``tidy_state`` the code word (``taken`` / ``failed`` / ``left``) for the logs.
    tidied: str = ""
    tidy_state: str = ""
    #: One20 (S3, C7): the measured tab-cover sentence when code did not say it (a result not done): the agent words it.
    cover: str = ""
    #: One20 (S5): what code already did as the second way inside this call (a code word: ``unread``, ``choose``).
    second: str = ""
    #: names21 (merge note 5): an ``open_app`` whose launch took a misheard name for one of the user's own apps or
    #: sites: the measure's content-free facts (kind, how, score, margin, source), for the log line only.
    names_match: Mapping[str, Any] | None = None

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
        if self.tidied:
            out["tidied"] = self.tidied
            if self.tidy_state == "asked" and self.tidy_step:
                out["side_effect"] = TIDY_ASKED_NOTE.format(step=self.tidy_step)   # integ27 (A7): their yes's step
        elif self.tidy_step:
            out["side_effect"] = TIDY_ASK_NOTE.format(step=self.tidy_step)
        if self.cover:
            out["cover"] = self.cover
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
                "tidy": self.tidy, "tidy_step": bool(self.tidy_step), "tidied": self.tidy_state or None,
                "cover": bool(self.cover), "owed": bool(self.owed) or None,
                "second": self.second or None,
                "names_match": dict(self.names_match) if self.names_match else None,
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
    #: tidy21: code's own tidy step was tried this turn (:meth:`Runner._tidy_by_code`: at most once a turn), and the
    #: turn's speaker (the driver's ``speak`` of its latest call), so a tidy step code takes at the turn's tidy debt is
    #: said (voice) or shown (typed) like the rest of code's lines.
    tidied: bool = False
    speaker: Any = None
    #: tidy21 review F1: the windows whose own place or state this turn's acts were asked for
    #: (``hands.arranged_windows``: a ``window`` move / maximize / fullscreen / restore, a skill run given a place):
    #: code's tidy step never moves one away again (``diff.tidy_guard``'s ``asked_place``).
    arranged: set[int] = field(default_factory=set)
    #: integ27 (fa merge note 3, A9): a call of this turn was a sound control's (``hands._sound_act``: the mixer hand,
    #: the volume keys, the volume skill), noted at the call before it acts, so a master change measured around any
    #: act of the turn is theirs, never a side effect; ``master_said``: code's master line was said this turn (once).
    sound: bool = False
    master_said: bool = False
    #: integ27 (fa merge note 7, A1): the windows where a close of this turn raised the page's or the app's own
    #: question (the window the close acted in, the question's own window when measured), each with their words as
    #: they stood then: until their words change, no call of the turn acts there (:func:`_held_close`).
    held: dict[int, tuple[str, ...]] = field(default_factory=dict)


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
                 settings: Any = None, desk_read: Callable[..., Any] | None = None, bracket: bool = False) -> None:
        self.runner = runner
        self.turn = turn
        self.name = str(name)
        self.args = dict(args or {})
        self.settings = settings
        self._desk_read = desk_read
        #: Final-B (contract C2): the app whose mixer row both desk reads of this act carry (the first allowed
        #: target's app, or a skill's app slot), so the diff sees a sound change of the acted app; ``""``: none.
        self.sound_app = ""
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

    def read_desk(self) -> Any:
        """One desk read for this act (its before or its after): with the master and :attr:`sound_app`'s mixer row
        (contract C2), the same on both sides. A reader that takes no app (a stand-in) is read plainly."""
        read = self._desk_read
        if read is None:
            return None
        try:
            return read(self.sound_app)
        except TypeError:
            return read()

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
                    # integ27 (fa merge note 7): a window a close of this turn left asking is theirs to answer first
                    decision = self._held(self.name, args, target, inner=False) \
                        or policy.gate(self.name, args, target, self.turn)
                    self.first, self.facts, self.first_args = decision, dict(target), dict(args)
                else:
                    tabs = target.get("tabs") if isinstance(target.get("tabs"), list) else None
                    mapped = T.as_verb(str(name), args, tab_names=tabs)
                    decision = (self._held(mapped[0], mapped[1], target, inner=True)
                                or policy.gate(mapped[0], mapped[1], target, self.turn, inner=True)) \
                        if mapped is not None \
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
            if not self.sound_app:
                self.sound_app = _words(target.get("app"))
            try:
                self.before = self.read_desk()   # right after the gate, right before the act
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
        held = self._held(str(tool), dict(tool_input or {}), facts, inner=True)
        if held is not None:
            # integ27 (fa merge note 7): a skill step in a window a close of this turn left asking: held, theirs first
            verdict = _verdict(held)
            with self._lock:
                self.inner.append(verdict)
            return verdict
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

    def _held(self, name: str, args: Mapping[str, Any], target: Mapping[str, Any], *, inner: bool) -> Any:
        """integ27 (fa merge note 7, A1, final-F k_tab_close_leave_site): a step in a window where a close of this turn
        raised the page's or the app's own question ("Leave site?", a save prompt) while their words are as they were
        then (:attr:`RunState.held`): refused, so no call of the turn answers that question for them, finishes the
        close another way (the same close again, a key, a click, the whole window) or tidies around it (a
        ``focus_back``, an Escape); code's own tidy step too. Their next words (the next turn, or a typed answer synced
        into this one) lift it, and the commit check judges the click on the question with the measured fact
        (``brain.check``'s raised window). ``None``: not held. Ids and the turn's own words only; never raises."""
        try:
            hwnd = _held_close(self.turn, target, args)
            if hwnd is None:
                return None
            decision = policy.Decision(policy.REFUSE, "close_asked", line=LINE_CLOSE_ASKED, verb=str(name))
            if self.runner is not None:
                self.runner._emit("one_gate", tool=str(name), inner=inner, **decision.facts())
            return decision
        except Exception:  # noqa: BLE001 - the gate decides as before
            return None


def _held_close(turn: Any, target: Mapping[str, Any] | None, args: Mapping[str, Any] | None = None) -> int | None:
    """integ27 (fa merge note 7): the window a step acts in (its target's ``hwnd``, else its own ``hwnd`` or ``window``)
    when a close of this turn left it asking and their words are as they were then (:attr:`RunState.held`), else
    ``None``. Never raises."""
    try:
        state = getattr(turn, "_one_run", None)
        held = state.held if isinstance(state, RunState) else None
        if not held:
            return None
        words = tuple(getattr(turn, "words", None) or ())
        for raw in ((target or {}).get("hwnd"), (args or {}).get("hwnd"), (args or {}).get("window")):
            hwnd = _hwnd_of(raw)
            if hwnd is not None and held.get(hwnd) == words:
                return hwnd
    except Exception:  # noqa: BLE001
        return None
    return None


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


def _skill_app(sk: Any, slots: Mapping[str, Any], app: str) -> str:
    """The app a skill run acts on, for its desk reads' mixer row (contract C2): the value of its slot of type ``app``
    (the volume's ``target``, a web skill's ``browser``) when given, else the skill's own app. Code facts: the skill's
    declared slot types, never words."""
    for sl in getattr(sk, "slots", ()) or ():
        if getattr(sl, "type", "") == "app":
            given = _words((slots or {}).get(getattr(sl, "name", "")))
            if given:
                return given
    return _words(app)


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


def _typing_landed(ok: bool, facts: Mapping[str, Any]) -> bool:
    """One22 (A21): whether the agent's own typing may have reached its window, from the verb's own facts: never when
    nothing was dispatched (``sent_at`` unset: refused before it, a game or the Act switch read before the dispatch);
    yes when it took; a dispatch that failed partway, or that the dispatcher's outcome reads as refused or blocked,
    may have typed part of it first, so it counts (Yuki's text may wait there: the safe side) unless the hands say no
    character went out (``typed_any`` False; review L2: ``refused`` alone never drops it once the dispatch began)."""
    if facts.get("sent_at") is None:
        return False
    if ok:
        return True
    return facts.get("typed_any") is not False


def _land_typed(turn: Any, landed: bool) -> None:
    """The gate's staged typing of this thread's call marked (it landed) or dropped (:meth:`policy.Turn.land_typed`).
    Never raises."""
    land = getattr(turn, "land_typed", None)
    if callable(land):
        try:
            land(bool(landed))
        except Exception:  # noqa: BLE001
            pass


def _bare_address(url: Any) -> str:
    """One22 review L4: an address as a told way's step keeps it: its scheme, host (no user or password) and path; the
    query and the fragment (search words, a page's state) never. ``""`` when it doesn't read as an address."""
    text = _words(url)
    if not text or " " in text:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(text if "://" in text else f"https://{text}")
        host = (parts.hostname or "").casefold()
        if not host:
            return ""
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme or "https", host + port, parts.path or "", "", ""))
    except Exception:  # noqa: BLE001 - nothing kept rather than more than the address
        return ""


def _step_record(name: str, args: Mapping[str, Any], target: Mapping[str, Any], facts: Mapping[str, Any],
                 status: str, verified: Any, verb: str) -> dict[str, Any]:
    """One22 (A13, contract C13): one acting step as ``ways.spec_from_steps`` reads it: the ONE tool (also as ``do``,
    the key ways reads: ``click``, ``type``, ``keys``) and the verb it ran as, the window's app and hwnd, the control's
    own role and name (a click, a typing, a slider), what the step was (a window or tab action, the app or address
    opened, its query and fragment left out: review L4; the skill run, the keys pressed; for a typing only how many
    characters and whether Enter followed: typed text is never kept), its status and when. Code facts only."""
    role, cname = _words(target.get("role")), _words(target.get("name"))
    if not role and not cname:
        role, cname = _element_parts(target.get("element") or facts.get("target"))
    step: dict[str, Any] = {"tool": str(name), "do": str(name),
                            "verb": str(verb or (T.SPECS[name].verb if name in T.SPECS else name)),
                            "app": _words(target.get("app") or facts.get("app")),
                            "hwnd": _hwnd_of(next((h for h in (target.get("hwnd"), facts.get("live_hwnd"),
                                                               args.get("window")) if h is not None), None)),
                            "status": str(status), "ok": status in (DONE, ALREADY), "verified": verified,
                            "at": time.time()}
    if name in ("click", "type", "slider"):
        step.update(role=role, name=cname)
        if args.get("id") is not None:
            step["by_id"] = True
    if name == "type":
        step.update(chars=len(str(args.get("text") or "")), enter=args.get("enter") is True)
    elif name == "keys":
        keys = args.get("keys")
        step["keys"] = [str(k) for k in keys] if isinstance(keys, list) and keys else [str(args.get("key") or "")]
    elif name in ("window", "tab"):
        step["action"] = str(args.get("action") or ("show" if name == "tab" else ""))
    elif name == "open_app":
        step["open"] = _words(args.get("name"))
    elif name == "open_url":
        step["url"] = _bare_address(args.get("url"))
    elif name == "run_skill":
        step["skill"] = _words(args.get("skill"))
    return step


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
        own_names: One22 (A5): ``own_names() -> [entry]``, the user's own apps and sites a reading is measured
            against, each with its kind (an object or mapping with ``name`` and ``kind``, or ``(name, kind)``; a bare
            name is an app, or a site when written as a host); only kinds ``app`` and ``site`` are measured (review
            H2: a channel is never one). ``None``: ``skills.names.own_entries`` without channels (contract C14,
            read-only).
        early_said: fb (final-F B9, contract K-D, written by the voice driver): ``early_said(call_id) -> str``, the
            words of that call's say_first actually spoken early, ``""`` when none were. A code line of the turn
            equal to them is not said again (:meth:`run`). ``None``: the hands' ``bridge.early_said`` when there is
            one (:meth:`_early_heard`), else every line is said as before.
    """

    def __init__(self, verbs: Any, *, channel: str = "voice", settings: Any = None, countdown: Any = None,
                 memory: Any = None, dispatcher: Any = None, log: Callable[..., None] | None = None,
                 speak: Callable[[str], Any] | None = None, menu: str = "", lease: bool = True,
                 lease_wait_s: float = LEASE_WAIT_S, effect_budget_s: float = reads.EFFECT_BUDGET_S,
                 own_names: Callable[[], Sequence[Any]] | None = None,
                 early_said: Callable[[str], Any] | None = None) -> None:
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
        self._desk_shape: str | None = None   # review fix F2: the verbs' desk_read form, read once
        self.own_names = own_names
        self.early_said = early_said
        #: One20 (S2): the window in front before the newest act that moved the front (measured by its diff) and when:
        #: a ``focus_back`` with no window and no open tidy-up goes there (``Hands._front_before``'s reach).
        self._front_kept: tuple[int, float, Any] | None = None

    @property
    def memory(self) -> Any:
        return self._memory if self._memory is not None else getattr(self.verbs, "memory", None)

    @property
    def dispatcher(self) -> Any:
        return self._dispatcher if self._dispatcher is not None else getattr(self.verbs, "dispatcher", None)

    # -- the entry points ------------------------------------------------------------------------------------------

    def run(self, name: str, args: Mapping[str, Any] | None, turn: policy.Turn,
            speak: Callable[[str], Any] | None = None, call_id: str = "") -> OneResult:
        """One call, blocking (the caller's worker thread: the verb runs here, holding the desk lease here). Never
        raises: an error is a failure, nothing more done. ``call_id``: the model's tool call id (the voice driver
        passes it by name), for the say_first spoken early for it (:meth:`_early_heard`)."""
        started = time.perf_counter()
        name = str(name or "")
        args = dict(args) if isinstance(args, Mapping) else {}
        say = speak if speak is not None else self.speak
        state = state_of(turn)
        if say is not None:
            state.speaker = say
        if not state.sound and _sound_call(name, args) is not False:
            # integ27 (fa merge note 3, A9; the hands' review M1 rule): a sound control's call of this turn, noted at
            # the call before it acts (refused or not), so no master change of the turn is ever called a side effect
            state.sound = True
        early = self._early_heard(call_id, state)
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
        self._emit("one_run", **result.facts(), **(budget.facts() if budget is not None else {}),
                   early_said=early or None)
        return result

    def _early_heard(self, call_id: Any, state: RunState) -> bool:
        """fb (final-F B9, contract K-D): the words of this call's say_first that the voice spoke early
        (``early_said(call_id)``, the driver's record of what was actually said) are a line of Yuki's already heard
        this turn: kept with the turn's code lines (:attr:`RunState.lines`), so :meth:`_speak` never says a done line
        that is the same text again (``heard`` stays true: the result counts as heard). Yuki's own two lines compared
        as text; no word of theirs is read. ``True`` when such words were kept. Never raises.

        fb fix (review 1, K-D wiring): with no ``early_said`` given, the voice bridge's own reader is used (the hands'
        ``bridge.early_said(tool_id) -> str``, lane C's); absent (typed, or before it lands): every line said as now."""
        reader = self.early_said
        if not callable(reader):
            bridge = getattr(getattr(self.verbs, "hands", None), "bridge", None)
            reader = getattr(bridge, "early_said", None)
        if not callable(reader) or not call_id:
            return False
        try:
            text = _words(reader(str(call_id)))
        except Exception:  # noqa: BLE001 - unknown: every line is said as before
            return False
        if not text:
            return False
        state.lines.add(text.casefold())
        return True

    async def arun(self, name: str, args: Mapping[str, Any] | None, turn: policy.Turn,
                   speak: Callable[[str], Any] | None = None, call_id: str = "") -> OneResult:
        """:meth:`run` on a worker thread (never on the event loop), the turn's calls one after another in the order
        they arrived (Pipecat starts a reply's calls side by side, in the order written; ``asyncio.Lock`` wakes its
        waiters first come, first served: the hands' order gate, ``Hands._enter_order``)."""
        state = state_of(turn)
        loop = asyncio.get_running_loop()
        if state.order is None or state.order[0] is not loop:
            state.order = (loop, asyncio.Lock())
        async with state.order[1]:
            return await asyncio.to_thread(self.run, name, args, turn, speak, call_id)

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
        if name == "remember" and str(args.get("kind") or "") == WAY_KIND:
            return self._keep_way(args, turn, decision)
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

    def _focus_back_window(self, turn: policy.Turn) -> int | None:
        """One20 (final-D S2, desk-tidy RC-B): the window a ``focus_back`` with no window gives the front back to: the
        open tidy-up's measured ``focus_from`` (:attr:`policy.Tidy.focus_from`), else the window in front before the
        newest act that moved the front, within :data:`FRONT_KEEP_S`. Measured facts only; ``None``: none (the hands
        refuse it, as before)."""
        tidy = getattr(turn, "tidy", None)
        if tidy is not None and getattr(tidy, "open", False) and getattr(tidy, "focus_from", None):
            return int(tidy.focus_from)
        kept = self._front_kept
        if kept is not None and time.monotonic() - kept[1] < FRONT_KEEP_S:
            return int(kept[0])
        return None

    def _act(self, name: str, args: dict[str, Any], turn: policy.Turn, spec: T.Spec, say: Any,
             state: RunState) -> OneResult:
        from yuki.voice.hands import one_call

        if name == "window" and str(args.get("action") or "") == "focus_back" and args.get("window") is None:
            # one20 (S2): the hands keep no "front before" for ONE (only their own look sets it), so a focus_back with
            # no window had no target and was refused before the gate; the measured front fills it and it reaches
            # row 8 (a tidy step) like a named one
            target = self._focus_back_window(turn)
            if target is not None:
                args = {**args, "window": target}
                self._emit("one_focus_back", filled=True)
        ctx = self._ctx(turn, name, args, bracket=spec.kind == T.ACT)
        with _Lease(turn, wanted=self.lease and spec.kind == T.ACT, wait_s=self.lease_wait_s) as lease:
            if not lease.got:
                return OneResult(name, BUSY, verified=None, result=LINE_BUSY, evidence={"why": LINE_BUSY},
                                 verb=spec.verb)
            # one22 (A21): the gate stages the agent's own typing; it is marked as Yuki's unsent text only once it
            # landed (a verb that broke: unknown, so marked: the safe side)
            landed = True
            try:
                with one_call(ctx):
                    ok, report, facts = self.verbs.run(name, args)
                landed = _typing_landed(ok, dict(facts or {}))
            finally:
                _land_typed(turn, landed)
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
        # integ27 (fa merge note 7, A1, final-F k_tab_close_leave_site): a close that raised the page's or the app's own
        # question (``dialog_raised``, with or without its window's id) or that was pressed and stayed with no window
        # measured (``close_held``) is theirs to answer: asked, never failed (no miss, no other way, no tidy step)
        asking = _close_asking(facts) and status not in (DONE, ALREADY)
        if asking:
            status = ASKED
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
            # integ22 fix names-2: an open_url (or a new tab's address) taken for one of their own channels too
            named = _words(facts.get("opened_name")) \
                if name in ("open_app", "open_url", "tab") and isinstance(facts.get("names_match"), Mapping) else ""
            if named and done_line is None and named.casefold() not in line.casefold():
                # integration F2 (decision 3): a misheard name taken as one of their own apps or sites is said as what
                # opened (code's line), never the words as heard in the agent's line written before the act
                line = self.verbs.code_line(name, resolved, facts)
            focus = _words(facts.get("focus_line"))
            if focus and done_line is None and name == "window" and str(resolved.get("action") or "") == "focus":
                # integ27 (fa merge note 5, A2, final-F multi_open_then_close_yt_tab: "YouTube's up in Chrome." when
                # what came up was their own paused video): a browser window brought up is said by code from the tab
                # it shows now (the hands' read), never the agent's line written before the act, as under ``all``
                line = focus
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
        if isinstance(facts.get("names_match"), Mapping):
            result.names_match = dict(facts["names_match"])   # names21 (merge note 5): the launch's measure, logged
        if asked:
            result.question = str(facts.get("_asked") or "")
        elif asking:
            # integ27 (fa merge note 7): the report says what was measured and that nothing in it was answered; the
            # question is code's (the hands' fallback words): the agent asks it, the voice owes it, the typed loop asks
            # it and hands their answer back with the result
            result.evidence = self._evidence(name, resolved, ok, status, report, facts, probe_note)
            result.question = LINE_CLOSE_ASKS if facts.get("dialog_raised") else LINE_CLOSE_HELD
            self._hold_close(turn, ctx, resolved, facts, state)
        elif status not in CLEAN and status not in (NO_CHANGE, STARTING, READ):
            result.evidence = self._evidence(name, resolved, ok, status, report, facts, probe_note)
            result.owed = str(facts.get("say") or "") or OWED_LINES.get(
                "blocked" if facts.get("refused") == "blocked" else status, "")
            if facts.get("refused") == "game" and status == REFUSED:
                # a game in front held it (the verb's backstop, or the skill's own hold): said as it is, by code
                result.final, result.line = True, _words(facts.get("say")) or policy.LINE_GAME
                if not late:   # oVoice review L6: nothing said after the hard stop's line (the result settles silent)
                    result.said, result.heard = self._speak(result.line, say, state)
            if facts.get("held") and facts.get("question"):
                result.status, result.question = ASKED, str(facts.get("question") or "")
        acted = facts.get("sent_at") is not None or status in (DONE, NOT_CONFIRMED, FAILED)
        if ctx.before is not None:
            self._diff(name, resolved, facts, ctx, result, turn, state, scope=scope, say=say)
        if acted or result.status == DONE:
            # one20: the windows this turn's own acts acted in (an Escape there is a tidy step: the gate's row 8)
            self._note_acted(turn, ctx, facts, state)
        if acted:
            state.acted = True
            if budget is not None and decision is not None and result.status != ASKED:
                # an ask (the skill's own question, a hold with a question) is theirs to answer, not a miss: no retry
                # window opened, the step not barred, a lasting act not stopped for it; one20: keyed on the target the
                # gate found (the budget's step identity). fb (final-F B6): a skill run the engine settled
                # ``unverified`` (its route acted, its check could not read the effect) is no miss of the retry
                # window: ``unsure`` (the engine's own outcome and settled fields, code words)
                unsure = name == "run_skill" and str(facts.get("outcome") or "") == "unverified" \
                    and bool(facts.get("settled"))
                budget.after_call(name, ctx.first_args or args, acts=True,
                                  ok=status in (DONE, ALREADY, NO_CHANGE, STARTING), lasting=lasting,
                                  target=ctx.facts, unsure=unsure)
            try:
                # fb fix (review 2, B4): an asked run is never an ok record, whatever a rung touched first (``all``'s
                # ``as_voice``: ok = done), so the exchange floor (``_hand_marks``) never moves past their request;
                # ``sent_at`` (the speech-to-action moment) kept as measured
                self.verbs.record(name, resolved, report, ok=bool(ok) and result.status != ASKED, verified=verified,
                                  facts=facts)
            except Exception:  # noqa: BLE001 - a record never breaks an act
                pass
            note = getattr(turn, "note_step", None)
            if callable(note):
                # one22 (A13): the turn's acting steps, for "remember how I do this" (``remember`` kind way)
                try:
                    note(_step_record(name, args, ctx.facts, facts, result.status, result.verified, verb))
                except Exception:  # noqa: BLE001 - bookkeeping only
                    pass
        elif name == "run_skill" and budget is not None and decision is not None and result.status == REFUSED \
                and str(facts.get("outcome") or "") in OPEN_OUTCOMES and not facts.get("refused"):
            # one20 (C9): a run that handed back with nothing done leaves a part of their request open (no verb ran: no
            # retry window, but the closing hold counts it); like main, a run that did nothing is not recorded
            note = getattr(budget, "unfinished_run", None)
            if callable(note):
                note()
        elif name == "run_skill" and result.status == ASKED and str(facts.get("outcome") or "") == "asked":
            # fb (final-F B4, k_clarify_who_draft): the skill's own question with nothing on the PC touched is no act:
            # no ``sent_at`` (``_desk_skill``), so not ``acted`` and no speech-to-action moment; on the action log as
            # the voice hands keep it under ``all``, ``ok`` false, so the bridge's exchange floor (``_hand_marks``)
            # never moves past their words and the answer carries the request
            try:
                self.verbs.record(name, resolved, report, ok=False, verified=None, facts=facts)
            except Exception:  # noqa: BLE001 - a record never breaks a call
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
              result: OneResult, turn: policy.Turn, state: RunState, *, scope: Mapping[str, Any] | None,
              say: Any = None) -> None:
        """The act's diff: the desk read again, compared with its before (whose change is whose: ``Hands._scope``),
        the After text and its flags; the act's own windows and the attention windows before it to the turn (a
        tidy-up's only targets); new windows actable by id."""
        try:
            after = self._desk_read(ctx.sound_app)   # the same app's mixer row as its before (C2)
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
            opened = _opened_of(name, resolved, scoped, acted, touched)
            open_tidy = getattr(turn, "tidy", None)
            if open_tidy is not None and open_tidy.open and open_tidy.before is not None:
                # safety WW4: a later act of the turn joins the open tidy-up's scope (as the hands' ``_turn_before``
                # keeps every act of the turn since), so the tidy debt never reads the windows it opened as asked as
                # the flagged act's side effects (a new flagged diff below opens a tidy-up of its own, as before)
                open_tidy.acted = tuple(dict.fromkeys([*(open_tidy.acted or ()), *acted]))
                open_tidy.touched = tuple(dict.fromkeys([*(open_tidy.touched or ()), *touched]))
                open_tidy.opened = tuple(dict.fromkeys([*(getattr(open_tidy, "opened", None) or ()), *opened]))
            # integ22 (desk22 merge note 4a, A24): a new tab's autoplay over what played is a tab cover only in a window
            # this act opened a page in. integ27 (fa merge note 3, A9): ``sound_act`` is whether this act or any earlier
            # call of the turn was a sound control's (:attr:`RunState.sound`); else a master level or mute change
            # measured around this act is its side effect (``master_changed``)
            d = desk_diff.compare(ctx.before, after, acted=acted, touched=touched, opened=opened,
                                  sound_act=bool(state.sound))
            state.arranged |= self._arranged(name, resolved, scoped, ctx.before, after)
            own = {int(h) for h in self.verbs.act_windows(after, d, acted, touched) if h}
            state.own |= own
            result.flags = {k: bool(v) for k, v in dict(d.flags).items()}
            result.flagged = bool(d.any_flag)
            if not d.empty:
                result.after = desk_diff.render(d, max_chars=AFTER_CHARS)
            moved = self._front_moved(d)
            if moved is not None:
                self._front_kept = (moved, time.monotonic(), state)   # one20 (S2): a later focus_back's window
            elif self._front_kept is not None and self._front_kept[2] is not state:
                self._front_kept = None   # an act of a later turn that moved no front lets an older one go (as all)
            pending: dict[str, Any] | None = None
            step: dict[str, Any] | None = None
            settled = told = off = ask = False
            # integ27 (fa merge note 7, A1): a close that left the page's or the app's own question up takes no tidy
            # step around it (a focus_back would hide it, an Escape or any key would answer it): no tidy-up is opened
            asking = result.status == ASKED and _close_asking(facts)
            # integ27 (fa merge note 3, A9): a master change alone is no window's side effect: no tidy-up for it
            others = any(v for k, v in dict(d.flags).items() if k != "master_changed")
            if result.flagged and others and not result.tidy and not asking:
                # a new flagged diff opens a tidy-up on its windows; a tidy step's own flagged diff never renews it
                # (its two steps per flagged diff stay counted)
                attention = [h for h in [*list(d.attention or []), d.focus_from] if h]
                # one20 (C7): the one legal undoable step code measured for this diff; tidy21 (decision 1): under the
                # tidy dial "do" code takes it itself (below, once code's cover line is said); at "ask" the agent gets
                # it for the question only (the gate's row 8 asks before it runs)
                step, words = self._legal_step(d, own, after, turn)
                # fb (final-F B1): a focus_back with no window goes to the front the act took (the one front rule),
                # when the act's own move of the front (``moved``) was not measured
                taken = self._taken_front(d)
                turn.flag(own=sorted(own), attention=attention, before=ctx.before, acted=acted, touched=touched,
                          sound_app=ctx.sound_app, focus_from=moved if moved is not None else taken, step=step,
                          step_words=words, opened=opened)
                mode = util.tidy_mode(getattr(turn, "policy", None))
                do = mode == util.TIDY_DO
                off = mode == util.TIDY_OFF
                # review F1: a move of a window the turn's acts placed or sized as asked is no side effect: code
                # leaves it (its guard's ``asked_place``), at "ask" too (no question about it, as the planner)
                as_asked = step is not None and step.get("action") == "place" and step.get("hwnd") in state.arranged
                # integration (one-tidy-on-unlanded-act): code's step only after an act that took, as the hands' look
                # (a failed or unconfirmed try's turn goes on to another way, whose own diff or the tidy debt takes it)
                landed = result.status in (DONE, ALREADY)
                if state.tidied and not off:
                    # review F4: code already decided this turn's one tidy step (taken or left): a later flagged act
                    # settles by code too, as under "all" (one look a turn), never handed to the agent. integ27 (fa
                    # merge note 4): at "ask" too, once code asked its one question (or said its D6 line) this turn
                    settled = True
                elif off:
                    # fb (final-F B2, p1_dt_dial_off_playlist): with the tidy dial off no window flag is anyone's to
                    # act on (the hands give no look at "off": ``look = ... level != "off"``), so code settles it
                    # below (``_tidy_settled``: no agent round, no tidy debt); a sound left silent stays the agent's
                    pass
                elif step is not None and (do or as_asked):
                    if landed or not do:
                        pending = step
                    # else: code's one step waits (``state.tidied`` and ``Tidy.given`` stay unset, the flags stay on
                    # the result); under "do" the agent never takes or asks for it
                else:
                    result.tidy_step = words
                    # integ22 (desk22 merge note 4b, decision D6): no legal step for a window flag after an act that
                    # took: code's one measured line about what is covered (never silence), said after the cover line
                    told = step is None and landed
                    # integ27 (fa merge note 4, A7): the dial at "ask" and a legal step after an act that took: code
                    # says the measured question itself (``diff.tidy_ask``); a result with a ``then`` rest leaves it to
                    # the agent once the rest is done, as the hands' look with a rest does
                    ask = step is not None and landed and not result.then
            # r25 harness (H9): the diff's own record first, so a code tidy step's ``one_run`` row comes after it
            self._emit("one_diff", tool=name, **d.facts(), legal_step=bool(pending or result.tidy_step) or None)
            self._cover(d, result, turn, state, say)
            # integ27 (fa merge note 3, A9): code's master line, once a turn, after the act's own lines
            mastered = self._master(d, result, turn, state, say)
            if asking:
                if result.flagged:
                    self._tidy_settled(result, "asking", None)   # its own tidy-up never opened; an earlier one stands
            elif pending is not None:
                self._tidy_result(pending, d, after, result, turn, state, say)
            elif ask and step is not None:
                how = self._tidy_ask(step, after, result, turn, state, say)
                if how:
                    self._tidy_settled(result, how, turn)
            elif told:
                # fb (final-F B3): code's D6 line only over the desk the after-read saw; their own switch: silence,
                # and the flags settle by code all the same (left as they are, no agent round for them)
                how = self._flag_said(d, turn, state, say, result, after=after)
                if how:
                    self._tidy_settled(result, "told" if how == "said" else "left", turn)
            elif settled:
                self._tidy_settled(result, "", turn)
            elif off and result.flagged:
                self._tidy_settled(result, "off", turn)   # fb (B2): the dial off settles by code
            if result.flagged and not others and not asking and (mastered or state.master_said) \
                    and dict(result.flags).get("master_changed"):
                # integ27 (fa merge note 3): a flag set that is only the master's settles by code once its line was
                # said (the way ``_tidy_settled`` settles code's own tidy), with no tidy-up opened: no agent round
                self._tidy_settled(result, "master", None)
        except Exception as exc:  # noqa: BLE001 - no diff: the result stands
            self._emit("one_diff_error", tool=name, error=type(exc).__name__)

    @staticmethod
    def _front_moved(d: Any) -> int | None:
        """The window in front before the act when the act itself moved the front (``focus_by_act``), else ``None``."""
        try:
            if getattr(d, "focus_by_act", False) and d.focus_from and d.focus_from != d.focus_to:
                return int(d.focus_from)
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _taken_front(d: Any) -> int | None:
        """fb (final-F B1, contract K-B): the window the act took the front from, the one front rule both brains read:
        ``DeskDiff.taken_front`` (lane A's field; ``None``: no front taken, so no step from it). On a diff without that
        field (lane A not merged), the same rule from the diff's own measure, as the hands' look reads it
        (``hands._look_after``: ``focus_taken`` up, its ``focus_from``), never ``focus_by_act`` alone: that reads only
        the foreground window, while ``focus_taken`` also counts the window holding the keyboard (final-F
        p1_spotify_down_focus_back: the front went to an unlisted Spotify window, ``focus_taken`` up, and ONE had no
        step). Never raises (``None``)."""
        try:
            if hasattr(d, "taken_front"):
                got = getattr(d, "taken_front", None)
                return None if got is None or isinstance(got, bool) or not int(got) else int(got)
            if dict(getattr(d, "flags", {}) or {}).get("focus_taken") and getattr(d, "focus_from", None):
                return int(d.focus_from)
        except (TypeError, ValueError):
            return None
        return None

    def _legal_step(self, d: Any, own: set[int], after: Any, turn: policy.Turn) -> tuple[dict[str, Any] | None, str]:
        """One20 (contract C7): ``perception.diff.tidy_step(d, acts, front, monitors)`` (lane DESK) for a
        flagged diff: the act's own windows, the window it took the front from (fb, B1: :meth:`_taken_front`, the one
        front rule, as the hands' look keeps ``look["front"]``) and the monitors after it; the step and its words (a
        ``window`` call), or ``(None, "")``: none, the tidy dial off, or the helper not merged yet. Never raises."""
        helper = _desk_helper("tidy_step")
        if helper is None:
            return None, ""
        try:
            if util.tidy_mode(getattr(turn, "policy", None)) == util.TIDY_OFF:
                return None, ""
            front = self._taken_front(d)
            monitors = list(getattr(after, "monitors", None) or [])
            step = helper(d, sorted(own), front, monitors)
        except Exception as exc:  # noqa: BLE001 - no step: the agent reads the After as before
            self._emit("one_tidy_step", error=type(exc).__name__)
            return None, ""
        return (dict(step), _step_words(step, d)) if isinstance(step, Mapping) and _step_words(step, d) else (None, "")

    def _tidy_result(self, step: Mapping[str, Any], d: Any, after: Any, result: OneResult, turn: policy.Turn,
                     state: RunState, say: Any) -> None:
        """tidy21: code's own tidy step for a flagged act result (:meth:`_tidy_by_code`), and the result the agent
        reads: code's line added to ``said`` (heard), ``tidied`` (what code did), and no window flag left for the
        agent (code took the one legal step, or left the desk on purpose; :meth:`_tidy_settled`): the result is flagged
        only for a sound left silent (a volume step is the agent's). Never raises."""
        try:
            done, said = self._tidy_by_code(step, d, after, turn, state, say)
        except Exception as exc:  # noqa: BLE001 - nothing was done: the result stands as measured
            self._emit("one_tidy_code", error=type(exc).__name__)
            return
        if said:
            result.said = f"{result.said} {said}".strip()
            result.heard = True
        self._tidy_settled(result, done, turn)

    @staticmethod
    def _tidy_settled(result: OneResult, done: str, turn: policy.Turn) -> None:
        """tidy21: a flagged act result whose window tidy-up code decided (``done``: ``taken`` / ``failed`` /
        ``left``; ``""``: code's one step was decided earlier this turn, review F4) settles by code: ``tidied`` says
        what code did, and the window flags are cleared from ``flags`` as well as ``flagged`` (review F2: the voice
        driver reads any truthy flag as flagged, ``pipeline._one_flagged``), so no agent round comes for them; only a
        sound left silent stays (a volume step is the agent's). The measured flags stay in the ``one_diff`` log line.
        The flagged diff's tidy debt is code's too (``Tidy.given``). Never raises."""
        try:
            result.tidied, result.tidy_state = TIDIED.get(done, TIDIED["left"]), done or "once"
            silent = bool(dict(result.flags or {}).get("left_silent"))
            result.flags = {k: (bool(v) if k == "left_silent" else False) for k, v in dict(result.flags or {}).items()}
            result.flagged = silent
            tidy = getattr(turn, "tidy", None)
            if tidy is not None:
                tidy.given = True
        except Exception:  # noqa: BLE001 - the result stands as measured
            pass

    def _arranged(self, name: str, args: Mapping[str, Any], facts: Mapping[str, Any], before: Any,
                  after: Any) -> set[int]:
        """tidy21 review F1: the windows whose own place or state this act was asked for
        (``hands.arranged_windows``, from the act's own facts); empty when unknown. Never raises."""
        try:
            from yuki.voice.hands import arranged_windows

            return arranged_windows(name, dict(args or {}), dict(facts or {}), before, after)
        except Exception:  # noqa: BLE001
            return set()

    def _tidy_by_code(self, step: Mapping[str, Any], d: Any, after: Any, turn: policy.Turn, state: RunState,
                      say: Any) -> tuple[str, str]:
        """tidy21 (the user's decision 1, 19 Oct; final-D S1: 4 of 20 flagged diffs got a tidy try from the agent):
        under the tidy dial "do", code takes the flagged diff's one legal undoable step (``diff.tidy_step``: the front
        given back to the window the act took it from, or the act's covering window moved to the other monitor)
        itself, at most once a turn (:attr:`RunState.tidied`):

        * guards first: never once the user's next turn began (safety timing 4, ``Turn.moved_on``: a late act of an
          earlier turn, or their speech begun again while it ran); ``diff.tidy_guard`` (the window in front is still
          the one the after-read saw: never over a
          window they or another app brought forward since; a move never takes a window of theirs, nor one whose own
          place or state the turn's acts were asked for: :attr:`RunState.arranged`, review F1), never the front given
          to one of Yuki's own windows (review F5: ONE's desk reads keep them; the planner stands the origin in), and
          the Act switch of the step's window (:meth:`CallContext.act_off`);
        * then ``verbs.run("window", step)`` in the desk lease, under a call context of code's own (:data:`CODE_TIDY`):
          the gate judges it as an inner step at its rows (a game in front: row 1; the tidy dial and the flagged
          diff's own windows: row 8, counted as one of its tidy steps), never on the agent's budget or refusals; the
          hands' placement path (undoable, journaled in the turn's group, place's own game gate);
        * its record on the action log, then code's line (``diff.tidy_line``: what it did, or plainly that it did not
          take), said once. Left (a guard, a refusal, a game): nothing is said.

        The flagged diff's tidy-up is code's either way (``Tidy.given``: no tidy-debt round for it). Returns
        ``(state, said)``: ``taken`` / ``failed`` / ``left`` (``""``: code's step was tried this turn already) and the
        line said. Logs content-free (``one_tidy_code``). Never raises."""
        if state.tidied:
            return "", ""
        state.tidied = True
        tidy = getattr(turn, "tidy", None)
        if tidy is not None:
            tidy.given = True   # code decided this flagged diff's tidy-up: never a tidy-debt round for it
        started = time.perf_counter()
        call = _step_call(step)
        done, said, why = "left", "", ""
        ok, facts = False, {}
        try:
            from yuki.perception import diff as desk_diff
            from yuki.voice.hands import one_call

            if call is None:
                why = "no_step"
            elif getattr(turn, "moved_on", False):
                why = "moved_on"   # safety timing 4: their next turn began while this act ran: never a step after it
            else:
                reader = getattr(self.verbs, "front_now", None) or getattr(self.verbs, "foreground", None)
                front = reader() if callable(reader) else None
                why = desk_diff.tidy_guard(dict(step), d, front, getattr(after, "foreground", None), state.arranged)
                own = getattr(self.verbs, "own_window", None)
                if not why and call["action"] == "focus_back" and callable(own) and own(call["window"]):
                    why = "own_window"   # review F5: Yuki's own chat or bar is never "their" front to give back
            raw = desk_diff.step_app(dict(step), after)
            ctx = self._ctx(turn, CODE_TIDY, call or {}, bracket=False)
            if not why and raw and ctx.act_off(raw):
                why = "act_off"
            if not why:
                try:
                    self.verbs.learn(after)   # the step's windows are measured: actable by id (an act's after's rule)
                except Exception:  # noqa: BLE001 - the verb refuses an unknown window itself
                    pass
                with _Lease(turn, wanted=self.lease, wait_s=self.lease_wait_s) as lease:
                    if not lease.got:
                        why = "busy"
                    else:
                        with one_call(ctx):
                            ok, report, facts = self.verbs.run("window", dict(call or {}))
                facts = dict(facts or {})
                ran = facts.get("sent_at") is not None
                if not why:
                    if ok:
                        done = "taken"
                    elif ran and not facts.get("refused"):
                        done = "failed"
                    else:
                        why = str(facts.get("refused_why") or facts.get("refused") or "refused")
                if ran:
                    try:
                        self.verbs.record("window", dict(call or {}), report, ok=bool(ok),
                                          verified=facts.get("verified"), facts=facts)
                    except Exception:  # noqa: BLE001 - a record never breaks an act
                        pass
            budget = getattr(turn, "budget", None)
            if done != "left" and not (budget is not None and budget.late()):
                line = desk_diff.tidy_line(dict(step), util.app_name(raw) if raw else "", ok=ok)
                said = self._speak(line, say, state)[0] if line else ""
        except Exception as exc:  # noqa: BLE001 - a step that broke is a step not taken
            why = why or type(exc).__name__
        self._emit("one_tidy_code", action=str(step.get("action") or ""), state=done, why=why or None, ok=bool(ok),
                   said=bool(said), ms=round((time.perf_counter() - started) * 1000, 1))
        if done != "left" or facts.get("refused"):
            # r25 harness (H9): code's step as the act row it is (the "all" talker's ``hands op=window tidy=True``):
            # the window verb, its status, a tidy step by code; content-free
            self._emit("one_run", tool="window", verb="window", status=(DONE if ok else FAILED if done == "failed"
                                                                         else REFUSED),
                       tidy=True, by_code=True, said=bool(said), ms=round((time.perf_counter() - started) * 1000, 1))
        return done, said

    def _flag_said(self, d: Any, turn: policy.Turn, state: RunState, say: Any,
                   result: OneResult | None = None, *, after: Any = None) -> str:
        """integ22 (desk22 merge note 4b, the user's decision D6): a window flag up with no legal tidy step
        (``diff.tidy_step`` found none: one monitor and nothing taken, a minimised window, a cover by a window that
        isn't the act's own, a pop-up) gets code's one measured line (``diff.flag_line``: "Discord is covering
        Chrome.", app names only), said like the tidy line, once a turn (:attr:`RunState.tidied`: code's one tidy-up
        decision of the turn). At the dial "do" and "ask" (no step means nothing to ask about); "off": nothing. Not
        once their next turn began (safety timing 4) or after the hard stop.

        fb (final-F B3, p1_dt_alt_tab_before_tidy): only over the desk the after-read (``after``) saw, and not after
        their own switch (:meth:`_front_moved_on`): then code says nothing and leaves their windows as they are, its
        one tidy decision of the turn all the same (``left``).

        Returns ``"said"`` (``result``'s ``said`` gains the line), ``"left"`` (their desk moved on: nothing said), or
        ``""``: nothing decided (no line, a late turn, the dial off). Logs content-free (``one_tidy_code ...
        why=no_step`` / ``why=front_changed``). Never raises."""
        try:
            if state.tidied or util.tidy_mode(getattr(turn, "policy", None)) == util.TIDY_OFF:
                return ""
            if getattr(turn, "moved_on", False):
                return ""
            budget = getattr(turn, "budget", None)
            if budget is not None and budget.late():
                return ""
            helper = _desk_helper("flag_line")
            line = _words(helper(d, name=util.app_name)) if helper is not None else ""
            if not line:
                return ""
            moved_on = self._front_moved_on(d, after)
            tidy = getattr(turn, "tidy", None)
            if moved_on:
                state.tidied = True
                if tidy is not None:
                    tidy.given = True   # code decided this flagged diff's tidy-up (left): never a tidy-debt round
                self._emit("one_tidy_code", action="", state="left", why=moved_on, ok=False, said=False)
                return "left"
            said, _heard = self._speak(line, say, state)
            if not said:
                return ""
            state.tidied = True
            if tidy is not None:
                tidy.given = True   # code decided this flagged diff's tidy-up: never a tidy-debt round for it
            if result is not None:
                result.said = f"{result.said} {said}".strip()
                result.heard = True
            self._emit("one_tidy_code", action="", state="said", why="no_step", ok=True, said=True)
            return "said"
        except Exception as exc:  # noqa: BLE001 - nothing said: the agent reads the After as before
            self._emit("one_tidy_code", error=type(exc).__name__)
            return ""

    def _front_moved_on(self, d: Any, after: Any) -> str:
        """fb (final-F B3): whether their desk's front moved on from the one code's D6 line would speak about, measured
        only (final-F p1_dt_alt_tab_before_tidy: the after-read already showed "Focus: Chrome -> File Explorer (not
        from your act)", so a read of the front now alone could not tell).

        integ27 (fb merge note A3, review finding 3): the one rule both brains read, ``diff.front_moved_on`` (the
        hands' D6 path reads it too): ``diff.front_guard`` on the window in front now against the after-read's, plus
        the diff's own measure (the front moved during the act to a listed window that is not the act's). The runner's
        own copy of the front test is gone, so a front that can't be read now leaves their desk as it is with no line,
        as the hands and code's tidy step (``diff.tidy_guard``) read it. ``""``: the front is as the after-read saw
        it. Never raises (an error: ``""``, the line is said as before)."""
        try:
            from yuki.perception import diff as desk_diff

            reader = getattr(self.verbs, "front_now", None) or getattr(self.verbs, "foreground", None)
            now = reader() if callable(reader) else None
            why = desk_diff.front_moved_on(d, now, after)
            return "" if why == "error" else why
        except Exception:  # noqa: BLE001 - unread: as before
            return ""

    def _master(self, d: Any, result: OneResult, turn: policy.Turn, state: RunState, say: Any) -> str:
        """integ27 (fa merge note 3, A9, final-F one multi_japanese_and_volume: the master went to 0% and muted inside a
        playlist run): ``diff.master_line(d)`` ("Your PC's sound got muted while I did that."), the measured master
        change during an act that was not about the volume (``master_changed``: no call of the turn was a sound
        control's), said by code once a turn, right after the act's own done line (a result that took: the hands say it
        after the act's line too); never undone by code (its cause is not known). A result that did not take leaves it
        to the agent (its After says it first, and ``flags`` has it). Not after the hard stop, nor once their next turn
        began. Returns the line said now (``""``: none). Logs content-free (``one_master``). Never raises."""
        try:
            if state.master_said or result.status not in (DONE, ALREADY):
                return ""
            helper = _desk_helper("master_line")
            line = _words(helper(d)) if helper is not None else ""
            if not line:
                return ""
            budget = getattr(turn, "budget", None)
            if (budget is not None and budget.late()) or getattr(turn, "moved_on", False):
                return ""
            said, _heard = self._speak(line, say, state)
            if not said:
                return ""
            state.master_said = True
            result.said = f"{result.said} {said}".strip()
            result.heard = True
            self._emit("one_master", said=True)
            return said
        except Exception as exc:  # noqa: BLE001 - not said: the agent reads the After
            self._emit("one_master", error=type(exc).__name__)
            return ""

    def _tidy_ask(self, step: Mapping[str, Any], after: Any, result: OneResult, turn: policy.Turn, state: RunState,
                  say: Any) -> str:
        """integ27 (fa merge note 4, A7, final-F all p1_dial_ask_playlist_over_video: at "ask" the model worded the
        question itself and asked about pausing the video): under the tidy dial "ask", code says the measured question
        for the flagged diff's one legal step itself (``diff.tidy_ask``: "Want Chrome back in front?", "Want me to move
        Spotify to your other screen?"), the hands' rule for both brains, once a turn, only while the window in front is
        still the one the after-read saw (``diff.front_guard``) and their next turn has not begun. The result then
        carries the step for their yes (:data:`TIDY_ASKED_NOTE`) and settles by code (no agent round).

        Returns ``"asked"`` (said; ``result.said`` gains it), ``"left"`` (their desk moved on: nothing is asked, their
        windows as they are), or ``""``: nothing decided (no line, no speaker, the hard stop): the agent asks, as
        before (:data:`TIDY_ASK_NOTE`). Logs content-free (``one_tidy_code``). Never raises."""
        try:
            from yuki.perception import diff as desk_diff

            budget = getattr(turn, "budget", None)
            if state.tidied or (budget is not None and budget.late()):
                return ""
            action = str(step.get("action") or "")
            if getattr(turn, "moved_on", False):
                why = "moved_on"   # safety timing 4: never a question about an act after they moved on
            else:
                reader = getattr(self.verbs, "front_now", None) or getattr(self.verbs, "foreground", None)
                why = desk_diff.front_guard(reader() if callable(reader) else None, getattr(after, "foreground", None))
            tidy = getattr(turn, "tidy", None)
            if why:
                state.tidied = True
                if tidy is not None:
                    tidy.given = True   # code decided this flagged diff's tidy-up (left): never a tidy-debt round
                self._emit("one_tidy_code", action=action, state="left", why=why, ok=False, said=False)
                return "left"
            raw = desk_diff.step_app(dict(step), after)
            line = desk_diff.tidy_ask(dict(step), util.app_name(raw) if raw else "")
            said = self._speak(line, say, state)[0] if line else ""
            if not said:
                return ""
            state.tidied = True
            if tidy is not None:
                tidy.given = True
            result.said = f"{result.said} {said}".strip()
            result.heard = True
            self._emit("one_tidy_code", action=action, state="asked", ok=True, said=True)
            return "asked"
        except Exception as exc:  # noqa: BLE001 - nothing said: the agent asks, as before
            self._emit("one_tidy_code", error=type(exc).__name__)
            return ""

    def _cover(self, d: Any, result: OneResult, turn: policy.Turn, state: RunState, say: Any) -> None:
        """One20 (S3, contract C7): ``perception.diff.cover_line(d)``, the measured tab-cover sentence ("GitHub is up;
        X plays on in the other tab"): said by code right after a done line code said (the agent never owned it, and
        an unmarked round lost it in final-D), else on the result for the agent to word. Never raises."""
        helper = _desk_helper("cover_line")
        if helper is None:
            return
        try:
            line = _words(helper(d))
        except Exception as exc:  # noqa: BLE001
            self._emit("one_cover", error=type(exc).__name__)
            return
        if not line:
            return
        budget = getattr(turn, "budget", None)
        late = budget is not None and budget.late()
        if result.status == DONE and not late and (result.said or result.heard):
            # review fix: only right after a done line code said (a result with ``then`` and no done line said leaves
            # the cover to the agent, after its own words); fb (B9): or one already heard, not said twice (the
            # say_first the voice spoke early for this call was the very same line)
            said, _heard = self._speak(line, say, state)
            if said:
                result.said = f"{result.said} {said}".strip()
                result.heard = True
                return
        if not late:
            result.cover = line

    def _hold_close(self, turn: policy.Turn, ctx: CallContext, resolved: Mapping[str, Any], facts: Mapping[str, Any],
                    state: RunState) -> None:
        """integ27 (fa merge note 7, A1): a close of this turn left a window asking: that window (the one the close
        acted in: the gate's target, the hands' live window, the call's own windows) and the question's own window when
        its id was measured are held for the rest of the turn while their words stay as they are now
        (:attr:`RunState.held`, read by :meth:`CallContext._held`): no call of the turn answers it, finishes the close
        another way or tidies around it. Logged content-free (``one_close_asked``). Never raises."""
        try:
            raw: list[Any] = [ctx.facts.get("hwnd"), facts.get("live_hwnd"), facts.get("dialog"),
                              resolved.get("window")]
            many = resolved.get("_hwnds")
            if isinstance(many, (list, tuple)):
                raw.extend(many)
            words = tuple(getattr(turn, "words", None) or ())
            held = {h for h in map(_hwnd_of, raw) if h is not None}
            for hwnd in held:
                state.held[hwnd] = words
            self._emit("one_close_asked", dialog=bool(facts.get("dialog_raised")),
                       held_close=bool(facts.get("close_held")), dialog_id=isinstance(facts.get("dialog"), int),
                       windows=len(held))
        except Exception as exc:  # noqa: BLE001 - the gate and the commit check still judge every later step
            self._emit("one_close_asked", error=type(exc).__name__)

    @staticmethod
    def _note_acted(turn: policy.Turn, ctx: CallContext, facts: Mapping[str, Any], state: RunState) -> None:
        """One20: the windows this act acted in (its target, its live window) and the turn's own act windows, onto the
        gate's turn (:meth:`policy.Turn.note_acted`). Never raises."""
        note = getattr(turn, "note_acted", None)
        if not callable(note):
            return
        try:
            note([h for h in (ctx.facts.get("hwnd"), facts.get("live_hwnd"), *sorted(state.own)) if h])
        except Exception:  # noqa: BLE001
            pass

    def tidy_debt(self, turn: policy.Turn) -> dict[str, Any] | None:
        """Final-B (one-desk D3/F6): a reply code would settle while the turn's tidy-up is still open with no tidy step
        taken (an earlier act of the turn was flagged; the act that finished their request was clean against its own
        before, which already held the cover). The desk is read once and compared with the flagged act's own before,
        with the scope of that act and of every act of the turn since (safety WW4, :meth:`_diff`): still flagged, the
        net ``after`` and its ``flags`` (the agent gets one round with them);
        the cover gone, ``None`` (code settles as before). Once a turn (``Tidy.given``), so never a loop. Blocking
        (one desk read, a few ms). Never raises."""
        tidy = getattr(turn, "tidy", None)
        if tidy is None or not tidy.open or tidy.acts or tidy.given or tidy.before is None:
            return None
        tidy.given = True
        try:
            now = self._desk_read(tidy.sound_app)
            if now is None:
                return None
            from yuki.perception import diff as desk_diff

            d = desk_diff.compare(tidy.before, now, acted=tidy.acted, touched=tidy.touched,
                                  opened=tuple(getattr(tidy, "opened", None) or ()))
            self._emit("one_tidy_debt", **d.facts())
            if not d.any_flag:
                return None
            after = desk_diff.render(d, max_chars=AFTER_CHARS)
            debt: dict[str, Any] = {"after": after, "flags": [k for k, v in dict(d.flags).items() if v]}
            # one20 (C7): the net diff's one legal step, measured again
            own = set(tidy.own)
            try:
                own |= {int(h) for h in self.verbs.act_windows(now, d, tidy.acted, tidy.touched) if h}
            except Exception:  # noqa: BLE001 - the flagged act's own windows as kept
                pass
            if util.tidy_mode(getattr(turn, "policy", None)) == util.TIDY_OFF:
                # fb (final-F B2): the dial off: no window flag of the net diff is anyone's to act on (no round for
                # it, as the hands give no look at "off"); only a sound left silent is the agent's
                if not dict(d.flags).get("left_silent"):
                    return None
                return {"after": f"{after}\n{TIDIED['off']}".strip(), "flags": ["left_silent"]}
            step, words = self._legal_step(d, own, now, turn)
            if words:
                tidy.step, tidy.step_words = step, words
            state = state_of(turn)
            as_asked = bool(words) and step.get("action") == "place" and step.get("hwnd") in state.arranged
            if (util.tidy_mode(getattr(turn, "policy", None)) == util.TIDY_DO and (words or state.tidied)) or as_asked:
                # tidy21 (decision 1): code takes it itself (once a turn) and says so; review F4: once code decided
                # this turn's one step, the debt is code's too; review F1: a move of a window placed as asked is
                # code's to leave, at "ask" too. Nothing left for the agent unless a sound is silent
                done = self._tidy_by_code(step, d, now, turn, state, state.speaker or self.speak)[0] if words else ""
                if not dict(d.flags).get("left_silent"):
                    return None
                return {"after": f"{after}\n{TIDIED.get(done, TIDIED['left'])}".strip(), "flags": ["left_silent"]}
            if words:
                # the dial at "ask": the step for the question only (the voice driver copies ``after`` and ``flags``)
                debt["after"] = f"{after}\n{TIDY_ASK_NOTE.format(step=words)}".strip()
            else:
                how = self._flag_said(d, turn, state, state.speaker or self.speak, after=now)
                if how:
                    # integ22 (desk22 merge note 4b, D6): no legal step for the net diff's window flag: code said its
                    # one line (fb, B3: or left their moved-on desk as it is, saying nothing); nothing left for the
                    # agent unless a sound is silent
                    if not dict(d.flags).get("left_silent"):
                        return None
                    done = "told" if how == "said" else "left"
                    return {"after": f"{after}\n{TIDIED[done]}".strip(), "flags": ["left_silent"]}
            return debt
        except Exception as exc:  # noqa: BLE001 - no measure: code settles as before
            self._emit("one_tidy_debt_error", error=type(exc).__name__)
            return None

    def _desk_read(self, app: str = "") -> Any:
        """The desk now (``situation.DeskState``) for an act's before or after: final-B contract C2, with the master
        level and mute and ``app``'s own mixer row, so the diff sees what an act did to the sound. The call's shape is
        chosen once from the verbs' own signature (:meth:`_desk_read_shape`), never by catching a TypeError (that
        would hide one raised inside a real read). ``None``: no read."""
        read = getattr(self.verbs, "desk_read", None)
        if not callable(read):
            return None
        shape = self._desk_read_shape(read)
        rows = [app] if app else []
        try:
            if shape == "apps":
                return read(apps=rows)                  # hands19: the master always, and these apps' mixer rows
            if shape == "master_audio":
                return read(master=True, audio=rows)
            return read()
        except Exception as exc:  # noqa: BLE001
            self._emit("one_desk_read", op="error", shape=shape, error=type(exc).__name__)
            return None

    def _desk_read_shape(self, read: Callable[..., Any]) -> str:
        """Review fix F2: which form the verbs' ``desk_read`` takes, from its signature (read once): ``apps`` (hands19's
        ``desk_read(apps=...)``; the master is always read), ``master_audio`` (``master=..., audio=...``), else
        ``plain`` (no sound parts: the diff never sees the acted app's mixer row). A plain read is logged once,
        content-free, so a live test shows C2 is not wired."""
        if self._desk_shape is not None:
            return self._desk_shape
        import inspect

        try:
            params = inspect.signature(read).parameters
        except (TypeError, ValueError):
            params = {}
        names = set(params)
        if "apps" in names:
            shape = "apps"
        elif {"master", "audio"} <= names:
            shape = "master_audio"
        else:
            shape = "plain"
            self._emit("one_desk_read", op="fallback", shape=shape)
        self._desk_shape = shape
        return shape

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
        # kw2 (B2): "not that one" is the agent's own call (its corrects, again and choose fields), never code's read of
        # which of its values their newest words repeat: the call runs as the agent made it
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
                # contract C2: the skill's own app (its app slot's value, e.g. the volume's target, else the skill's
                # app) is the one whose mixer row both reads carry
                ctx.sound_app = _skill_app(sk, slots, app)
                ctx.before = self._desk_read(ctx.sound_app)   # after the gate and the countdown, right before the run
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
                second = ""
                if not desk_path:
                    # one20 (S5): the code-owned second way, once, inside this call (the planner's rung C under all):
                    # a rung that never read runs once more; a pick-kind run whose value found no route runs again as
                    # Yuki's pick with that value as context; each through the gate (an inner step), never a lasting
                    # skill (those run on the desk path) and never after a run that may have done it already
                    plan = self._second_way(key, sk, run_args, dict(facts or {}), turn)
                    if plan is not None:
                        second, again = plan
                        gated = policy.gate("run_skill", again, {"app": app}, turn, inner=True)
                        self._emit("one_second_way", skill=key, how=second, allowed=gated.allowed)
                        if gated.allowed:
                            turn.begin_skill(gated, again)
                            try:
                                ok, report, facts = self.verbs.run("run_skill", again)
                            finally:
                                turn.end_skill()
                            run_args = again
                        else:
                            second = ""
                if drafted and decision.type_after and ok:
                    ok, report, facts = self._type_after(turn, decision, key, app, ok, report, facts)
        facts = dict(facts or {})
        if second:
            facts["second_way"] = second
        guess = None if desk_path else self._guess_reading(key, sk, run_args, facts, turn)
        if guess is not None and guess[0] == "name":
            # one22 (A5, decision 3): the agent's own value, in a slot that holds an app or a site, measured as one of
            # their own apps or sites: code acts on that name (undoable: an open or a switch), never a question and
            # never a word about the mishearing; review L3: not once their next turn began (``None``: nothing done by
            # code, the run's own hand-off stands)
            taken = self._take_name(guess[1], guess[2], guess[3], guess[4], args, turn, say, state, via=guess[5])
            if taken is not None:
                return taken
            guess = None
        if guess is not None:
            # kw2 (B1, B3): a run that didn't land on a value that is only the agent's reading of their words, that
            # would change what plays or take the front, and that is no name of theirs: back to the agent with code's
            # note and what the run read (the engine's evidence lines after its head), to ask, pick or try again;
            # code asks nothing itself
            rest = str(report or "").split("\n", 1)[1] if "\n" in str(report or "") else ""
            report = f"{guess[1]}\n{rest}".strip()
        elif str(facts.get("outcome") or "") == "handed_over":
            report = self._one_handoff(report, facts, turn, second)   # one20 (S5): written for ONE
        if desk_path and can_send and not decision.join and facts.get("verified") is True:
            self._note_chat(turn, app)   # one20 (S17): the chat this turn's message skill opened
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
            # final-B (contract C1): the engine's measured front change when the run's payload carries it (else a run
            # on the desk path may have taken the front: counted as the act's, as before); the app-typed slot's value
            # is the act's too (one-desk RC1(b): by slot type, never words)
            # Review fix F6: the engine's measured ``acted`` False (no rung touched the PC, the front never changed) and
            # no draft typed after it: no change on the desk is this run's (its apps are not the act's); its measured
            # ``acted_apps`` are the act's too, as the hands' ``_skill_scope`` takes them
            measured = facts.get("focus_taken")
            untouched = facts.get("acted") is False and measured is not True and not drafted
            apps = [] if untouched else [a for a in dict.fromkeys(
                (app, ctx.sound_app, *(facts.get("acted_apps") or ()))) if a]
            # tidy21 (final-D desk-tidy, p1_discord under one: a draft the agent asked for with send "draft" counted as
            # a means, so its chat taking the front was a side effect): a run that left its window to the user is the
            # ask, by the same data rule as the hands' ``_skill_scope`` (a skill that can send, and these values did
            # not send: its ``sends_when`` data); code's own tidy step now acts on this measure, never hiding a draft
            values = facts.get("_values") if isinstance(facts.get("_values"), Mapping) else run_slots
            left = drafted or (bool(getattr(sk, "sends", False))
                               and not rule.skill_sends(sk, values, partial=False))
            scope = {"apps": apps,
                     "focus": measured if isinstance(measured, bool) else True,
                     "open": str(getattr(sk, "kind", "") or "") == "open", "left": left}
        line = _words(facts.get("done_line")) if facts.get("verified") is True else ""
        if drafted and facts.get("verified") is True and not line:
            line = rule.LINE_DRAFTED   # a draft is never "sent": consent's own words
        if str(facts.get("outcome") or "") == "asked":
            # the skill's own question (its resolver's: which one, who to): theirs to answer, the agent asks it; not a
            # failed step (``_finish`` books no miss for it). Final-B (one-voice RC E): only the structured question
            # (``facts["ask"]``, the engine's own field), never words read back out of the report: its first line is
            # the engine's model-facing shape ("Ask them: ...") and was spoken by the owed ledger. No question: the
            # status stays asked, nothing is owed as an ask, and the agent words it from the result
            facts["_asked"] = _words(facts.get("ask"))
        result = self._finish("run_skill", args, turn, ctx, ok, report, facts, say, state, done_line=line,
                              scope=scope, lasting=bool(decision.countdown or decision.join))
        result.second = second
        if counted:
            result.countdown = "sent"
        if result.status == DONE and result.verified is True:
            # one20 (S5): a later pick re-run of this skill by code's second way (this turn or the next) is a
            # correction of this run (kw2, B2: the slots it was given are no longer kept: nothing reads a re-ask)
            note = getattr(turn, "note_skill_done", None)
            if callable(note):
                note(key, self._pick_value(sk, facts))
        if prefs is not None:
            self._after_skill(prefs)
        return result

    # -- one20: the second way, the guessed reading, the hand-off for ONE ---------------------------------------------

    @staticmethod
    def _pick_slot(sk: Any) -> str:
        """The pick slot of a pick-kind skill (``taste.CHOOSE_SLOTS`` of its ``CHOOSE_KINDS`` kind), else ``""``."""
        try:
            from yuki.skills import taste

            cap = str(getattr(sk, "capability", "") or getattr(sk, "key", "") or "")
            return str(taste.CHOOSE_SLOTS.get(taste.CHOOSE_KINDS.get(cap, ""), "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _pick_value(self, sk: Any, facts: Mapping[str, Any]) -> str:
        """The value a verified run played for its pick slot (the run's own values), ``""``: none."""
        pick = self._pick_slot(sk)
        values = facts.get("_values") if isinstance(facts.get("_values"), Mapping) else {}
        return _value_text(values.get(pick)) if pick else ""

    @staticmethod
    def _sources(facts: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
        """Slot -> (source, value as shown) of a run's given values: the engine's ``fill_sources`` (contract C10, read
        with ``.get``), its guesses (``guessed``), the hand-off's pinned slots. Code words only."""
        out: dict[str, tuple[str, str]] = {}
        ev = facts.get("evidence") if isinstance(facts.get("evidence"), Mapping) else {}
        pin = facts.get("handoff_skill") if isinstance(facts.get("handoff_skill"), Mapping) else {}
        pinned = pin.get("slots") if isinstance(pin.get("slots"), Mapping) else {}
        for name, slot in pinned.items():
            if isinstance(slot, Mapping):
                out[str(name)] = (str(slot.get("from") or ""), _value_text(slot.get("value")))
        for g in facts.get("guessed") or ():
            if isinstance(g, Mapping) and g.get("slot"):
                out[str(g["slot"])] = (str(g.get("from") or ""), _value_text(g.get("value")))
        fills = ev.get("fill_sources") or facts.get("fill_sources")
        if isinstance(fills, Mapping):
            values = facts.get("_values") if isinstance(facts.get("_values"), Mapping) else {}
            for name, src in fills.items():
                shown = _value_text(values.get(name)) or out.get(str(name), ("", ""))[1]
                out[str(name)] = (str(src or ""), shown)
        return out

    def _reading_guess(self, facts: Mapping[str, Any]) -> tuple[str, str] | None:
        """The (slot, value) of a run's given value that is only the caller's reading of their words: the resolver's own
        ``reading`` tag on it (its data: none of their things, not said, not heard by sound or spelling); ``None``:
        none. kw2 (B3): no share of its words is counted against a screen read (whether the agent read it off the
        screen is the agent's to know: the note it gets says so)."""
        for name, (src, value) in self._sources(facts).items():
            if src in READING_SOURCES and value:
                return name, value
        return None

    @staticmethod
    def _open_run(facts: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], str] | None:
        """A run that handed back (or failed) with its request open and nothing that bars a second run: its evidence,
        its pinned hand-off and the hand-off's reason; ``None``: done, held, refused, or it may have acted already."""
        outcome = str(facts.get("outcome") or "")
        if outcome not in OPEN_OUTCOMES or facts.get("verified") is True:
            return None
        if facts.get("refused") or facts.get("held") or facts.get("act_off") or facts.get("memory_slow"):
            return None
        ev = facts.get("evidence") if isinstance(facts.get("evidence"), Mapping) else {}
        if facts.get("acted") is True or ev.get("acted") is True or facts.get("settled"):
            # review fix: a rung touched the PC (``Hands._skill``'s measured ``acted``: a send, an unverified rung, a
            # contradicted check, the engine's own ``acted``) or nothing more may be tried: a second run could do it
            # twice (rule 8); an ``unread`` rung says only that one rung never read, not that none acted
            return None
        pin = facts.get("handoff_skill") if isinstance(facts.get("handoff_skill"), Mapping) else {}
        why = str(pin.get("why") or ev.get("reason") or "")
        if why in NO_SECOND_RUN:
            return None
        tried =[*(facts.get("_tried") or ()), *(ev.get("tried") or ())]
        if any(isinstance(t, Mapping) and str(t.get("outcome") or "") == "contradicted" for t in tried):
            return None   # a rung acted and its own check said no: a second run could do it twice (x5Fallback)
        return ev, pin, why

    def _second_way(self, key: str, sk: Any, run_args: Mapping[str, Any], facts: Mapping[str, Any],
                    turn: policy.Turn) -> tuple[str, dict[str, Any]] | None:
        """One20 (final-D S5, one-core §3 fix 1): the second way code takes inside the call, once: ``("unread", the
        same input)`` when a rung never read (contract C10's ``unread``); ``("choose", the input as Yuki's pick)`` when
        a pick-kind skill (``taste.CHOOSE_KINDS`` but :data:`NO_PICK_RERUN`) found no route for its value or no item
        for its pick slot: the value goes to ``context``, ``choose`` true, ``corrects`` when the same skill ran verified
        this turn or the turn before (:meth:`policy.Turn.ran_before`; that value is named in the context as the one not
        wanted). Never for a value that is only the agent's reading of their words (S12: that miss goes back to the
        agent, kw2 B1/B3), never after a rung touched the PC (:meth:`_open_run`). Engine and turn facts only."""
        if sk is None:
            return None
        got = self._open_run(facts)
        if got is None:
            return None
        ev, pin, why = got
        if ev.get("unread") or facts.get("unread"):
            return "unread", dict(run_args)
        cap = str(getattr(sk, "capability", "") or getattr(sk, "key", "") or "")
        pick = self._pick_slot(sk)
        if cap in NO_PICK_RERUN or not pick:
            return None
        slots = dict(run_args.get("slots") or {}) if isinstance(run_args.get("slots"), Mapping) else {}
        value = _value_text(slots.get(pick))
        missing = str(ev.get("missing") or pin.get("missing") or "")
        if not value or not (why == "no_route" or missing == pick):
            return None
        chose = str(slots.get("choose")).strip().casefold() in ("true", "1", "yes")
        if not chose and self._reading_guess(facts) is not None:
            return None   # S12: their words never held it, none of theirs: the agent's to decide, never code's pick
        context = _words(slots.get("context"))
        if value.casefold() not in context.casefold():
            context = f"{context}; {value}" if context else value
        again_slots = {k: v for k, v in slots.items() if k not in (pick, "choose", "context")}
        again: dict[str, Any] = {**dict(run_args), "slots": again_slots}
        earlier = turn.ran_before(key) if callable(getattr(turn, "ran_before", None)) else None
        if earlier is not None:
            again["corrects"] = True
            if earlier and earlier.casefold() not in context.casefold():
                context = f"{context}, other than {earlier}"
        again_slots.update(choose=True, context=context)
        return "choose", again

    def _guess_reading(self, key: str, sk: Any, run_args: Mapping[str, Any], facts: Mapping[str, Any],
                       turn: policy.Turn) -> tuple[Any, ...] | None:
        """One20 (final-D S12, ONE part), one22 (A5, the user's decision 3), kw2 (B1, B3): a run did not land, the agent
        did not ask Yuki to choose, a value it gave is only its own reading of their words (:meth:`_reading_guess`: the
        resolver's own tag) and the skill would change what plays or take the front (a pick kind, a route that takes
        focus, an ``open`` skill: its data). Then:

        * the slot holds an app or a site (review M1, :meth:`_slot_fits`: a media title, a channel or a page's item is
          never an app's name) and the value, measured as a name (:meth:`_names_winner`), is one of the user's own:
          ``("name", name, kind, facts, the reading, via)``: code acts on it (:meth:`_take_name`; ``via``: the app the
          run named as its means, a site's browser, review M2: :meth:`_means_app`); never once their next turn began
          (review L3: ``Turn.moved_on``; then ``None``, the run's own hand-off stands);
        * else ``("back", note)``: the miss goes back to the agent with :data:`GUESS_BACK` before the run's evidence;
          the agent asks, picks or tries again. Code never measures their whole turn and never asks itself.

        ``None`` otherwise."""
        if sk is None or self._open_run(facts) is None:
            return None
        slots = run_args.get("slots") if isinstance(run_args.get("slots"), Mapping) else {}
        if str(slots.get("choose")).strip().casefold() in ("true", "1", "yes"):
            return None
        guess = self._reading_guess(facts)
        if guess is None:
            return None
        try:
            from yuki.skills import taste

            cap = str(getattr(sk, "capability", "") or getattr(sk, "key", "") or "")
            moves = cap in taste.CHOOSE_KINDS or str(getattr(sk, "kind", "") or "") == "open" or any(
                bool(getattr(r, "needs_focus", False)) for r in getattr(sk, "routes", ()) or ())
        except Exception:  # noqa: BLE001
            moves = False
        if not moves:
            return None
        won = self._names_winner(guess[1], avoid=self._skill_apps(sk, slots), fits=self._slot_fits(sk, guess[0]))
        if won is not None:
            self._emit("one_guess", skill=key, asked=False, took=won[1], how=won[2].get("how"),
                       score=won[2].get("score"), margin=won[2].get("margin"))
            if getattr(turn, "moved_on", False):
                # review L3 (safety timing 4): their next turn began while the skill ran: code's own act never after it
                self._emit("one_names_take", why="moved_on")
                return None
            via = self._means_app(sk, slots) if won[1] == "site" else ""
            return ("name", won[0], won[1], won[2], guess[1], via)
        self._emit("one_guess", skill=key, asked=False, took="back")
        return ("back", GUESS_BACK.format(value=guess[1]))

    @staticmethod
    def _skill_apps(sk: Any, slots: Mapping[str, Any]) -> list[str]:
        """The apps a run named or drives (its own app, its app-typed slots' values): a measured name that is one of
        them is no other thing of theirs (the run already aimed there; its value was the guess)."""
        out = [_words(getattr(sk, "app", ""))]
        for sl in getattr(sk, "slots", ()) or ():
            if getattr(sl, "type", "") == "app":
                out.append(_value_text((slots or {}).get(getattr(sl, "name", ""))))
        return [a for a in dict.fromkeys(out) if a]

    @staticmethod
    def _slot_fits(sk: Any, slot_name: str) -> frozenset[str]:
        """One22 review M1: the kinds of the user's own names a reading in this slot may be measured as (the reading
        as a name given to the act): an app-typed slot (or one bound to an occasion's ``app``) an app; a slot bound to
        a ``site`` a site; any other slot of an ``open`` skill (a site tab's service: the thing to bring up) either.
        None (``frozenset()``) for a pick slot (a playlist, a video, a channel: a title, never an app's name), a slot
        bound to a thing's own value (:data:`ENTITY_BINDS`), a slot with an entity kind, and every slot of a skill
        that changes what plays: there nothing is measured (kw2, B1: the miss goes back to the agent). The skill's own
        data, never words."""
        try:
            from yuki.skills import names as N

            slot = next((s for s in getattr(sk, "slots", ()) or () if getattr(s, "name", "") == slot_name), None)
            bind = str(dict(getattr(sk, "bind", None) or {}).get(slot_name) or "")
            if not slot_name or slot_name == Runner._pick_slot(sk) or bind in ENTITY_BINDS:
                return frozenset()
            if (slot is not None and getattr(slot, "type", "") == "app") or bind == "app":
                return frozenset({N.APP})
            if bind == "site":
                return frozenset({N.SITE})
            if str(getattr(sk, "kind", "") or "") == "open" and not str(getattr(slot, "kind", "") or ""):
                return frozenset({N.APP, N.SITE})
        except Exception:  # noqa: BLE001 - unknown: nothing is measured
            pass
        return frozenset()

    @staticmethod
    def _means_app(sk: Any, slots: Mapping[str, Any]) -> str:
        """One22 review M2: the app a run named as its means (the value of its app-typed ``means`` slot: a web skill's
        browser, "in Arc"), when it is not the skill's own app; ``""``: none named (the usual browser opens it)."""
        try:
            from yuki.skills import names as N

            own = _words(getattr(sk, "app", ""))
            for sl in getattr(sk, "slots", ()) or ():
                if getattr(sl, "type", "") != "app" or getattr(sl, "role", "") != "means":
                    continue
                value = _value_text((slots or {}).get(getattr(sl, "name", "")))
                if value and not (own and N.same_group(value, own)):
                    return value
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _own_entries(self) -> list[tuple[str, str]]:
        """The user's own apps and sites as ``(name, kind)`` (contract C14: ``skills.names.own_entries`` without
        channels, read-only: memory's timeline and links seen, the windows open, the Start menu as last read), or the
        runner's own source. Review H2: each name keeps the kind its source gave it, and only an app or a site is
        measured (a channel of theirs, or any other kind, is never launched as an app). Never raises."""
        try:
            from yuki.skills import names as N

            fn = self.own_names
            if callable(fn):
                got = list(fn() or ())
            else:
                memory = self.memory
                store = getattr(memory, "store", None) if memory is not None else None
                try:
                    got = list(N.own_entries(store, channels=False) or ())
                except TypeError:   # main's signature: apps and sites only
                    got = list(N.own_entries(store) or ())
            out: list[tuple[str, str]] = []
            for e in got:
                if isinstance(e, str):
                    name, kind = _words(e), (N.SITE if N.is_site(e) else N.APP)
                elif isinstance(e, Mapping):
                    name, kind = _words(e.get("name")), str(e.get("kind") or "")
                elif isinstance(e, (tuple, list)) and len(e) == 2:
                    name, kind = _words(e[0]), str(e[1] or "")
                else:
                    name, kind = _words(getattr(e, "name", "")), str(getattr(e, "kind", "") or "")
                if name and kind in (N.APP, N.SITE):
                    out.append((name, kind))
            return out
        except Exception:  # noqa: BLE001 - no names: nothing measures (the miss goes back to the agent)
            return []

    def _names_winner(self, value: str, *, avoid: Sequence[str] = (),
                      fits: frozenset[str] | Sequence[str] = ()) -> tuple[str, str, dict[str, Any]] | None:
        """One22 (A5, decision 3): the one of the user's own apps and sites the agent's own value names, measured by
        spelling and sound as a name given to the act (``skills.names.best_match`` ``as_name``, its bar; contract C14),
        only when its slot holds a name of a kind in ``fits`` (review M1, :meth:`_slot_fits`; none: never measured).
        kw2 (B1, contract K2): never their newest words as a whole turn: what they meant is the agent's reading; code
        resolves only the name the agent chose. ``(name, kind, facts)`` (``kind``: ``app`` / ``site``, as its source
        gave it, of a kind in ``fits``; facts content-free: how, score, margin), or ``None``: nothing to measure, no
        clear winner, a winner of another kind, or one of ``avoid`` (the run's own apps). Pure but for the names
        read."""
        fit = frozenset(fits or ())
        if not fit or not _words(value):
            return None
        try:
            from yuki.skills import names as N
        except Exception:  # noqa: BLE001 - not merged: nothing measures
            return None
        try:
            entries = self._own_entries()
            if not entries:
                return None
            pool = [n for n, _k in entries]
            kinds = {n.casefold(): k for n, k in reversed(entries)}   # the first listed wins a name both have
            got = N.best_match(value, pool, as_name=True)
            name = _words(got[0]) if got else ""
            if not name or kinds.get(name.casefold()) not in fit:
                return None
            if any(N.same_group(name, a) for a in avoid if _words(a)):
                return None
            kind = kinds[name.casefold()]
            return name, kind, {"kind": kind, "how": "value", "score": got[1], "margin": got[2],
                                "source": "one_reading"}
        except Exception:  # noqa: BLE001
            return None

    def _take_name(self, name: str, kind: str, measure: Mapping[str, Any], reading: str, args: Mapping[str, Any],
                   turn: policy.Turn, say: Any, state: RunState, *, via: str = "") -> OneResult | None:
        """One22 (A5, decision 3): code acts on the measured name in place of the agent's reading: an app is opened or
        brought up (``open_app``), a site opened (``open_url`` at its address; review M2: in ``via``, the browser the
        run named, when it named one), under a call context of code's own (:data:`CODE_NAMES`): the gate judges it as
        an inner step at its rows (a game in front, the Act switch, the commit check), the desk lease, the act diff and
        the tidy as for any act, and code's done line ("Opening Discord.") is said; no question, no word about the
        mishearing. The result is the run_skill call's (the agent reads :data:`NAME_TAKEN`). ``None``: nothing done
        by code, their next turn began before it could act (review L3, safety timing 4: ``Turn.moved_on``, read again
        once the lease is held). Never raises."""
        try:
            from yuki.skills import names as N
            from yuki.voice.hands import one_call

            if kind == N.SITE:
                tool, call = "open_url", {"url": N.site_url(name), **({"app": via} if _words(via) else {}),
                                          "then": T.then_of(args)}
            else:
                tool, call = "open_app", {"name": name, "then": T.then_of(args)}
            spec = T.SPECS[tool]
            ctx = self._ctx(turn, CODE_NAMES, call, bracket=True)
            with _Lease(turn, wanted=self.lease, wait_s=self.lease_wait_s) as lease:
                if not lease.got:
                    return OneResult("run_skill", BUSY, verified=None, result=LINE_BUSY, evidence={"why": LINE_BUSY},
                                     verb=spec.verb)
                if getattr(turn, "moved_on", False):
                    self._emit("one_names_take", why="moved_on")
                    return None
                with one_call(ctx):
                    ok, report, facts = self.verbs.run(tool, call)
            # integ22 names-3: the exact own name measured nothing at the launch, so code's line names what opened
            # itself ("Opened Discord.", a site by its host), as under "all", never a bare "It's open."
            result = self._finish(tool, call, turn, ctx, ok, report, dict(facts or {}), say, state,
                                  done_line=f"Opened {' '.join(name.split())[:60]}." if _words(name) else None)
        except Exception as exc:  # noqa: BLE001 - nothing more is known: a failure, nothing more done
            self._emit("one_names_take", error=type(exc).__name__)
            return OneResult("run_skill", FAILED, verified=False, result=type(exc).__name__,
                             evidence={"why": "Yuki's runner broke on this call"}, owed=OWED_LINES[FAILED])
        result.name = "run_skill"
        result.names_match = {**dict(result.names_match or {}), **dict(measure)}
        did = NAME_DID.get((kind, result.status in (DONE, ALREADY)), "tried it")
        lead = NAME_TAKEN.format(name=name, kind=kind, did=did, value=reading)
        result.result = f"{lead} {result.result}".strip()[:RESULT_CHARS]
        self._emit("one_names_take", kind=kind, how=measure.get("how"), status=result.status)
        return result

    # -- one22: a told way (A13) ---------------------------------------------------------------------------------------

    def _with_store(self, what: str, fn: Callable[[Any], Any], *, timeout_s: float = 1.0) -> Any:
        """``fn(store)`` on the runner's memory: through its bounded call (a ``MemoryAccess``), else on the store it
        holds (a client or the store itself; the hands' ``_memory_call`` rule). ``None`` on failure."""
        memory = self.memory
        if memory is None:
            return None
        try:
            call = getattr(memory, "call", None)
            if callable(call) and not hasattr(memory, "store"):
                return call(what, lambda client: fn(getattr(client, "store", client)), timeout_s=timeout_s)
            return fn(getattr(memory, "store", memory))
        except Exception:  # noqa: BLE001
            return None

    def _keep_way(self, args: Mapping[str, Any], turn: policy.Turn, decision: Any) -> OneResult:
        """One22 (A13, contract C13; audit A13: ``ways.keep_told`` had no caller, "learn after one go"): ``remember``
        kind ``way``, "remember how I do this": the acting steps as a told way (``ways.spec_from_steps(app, steps)``,
        read with getattr), kept at once as the user's own (``ways.keep_told(store, app=, spec=, name=)``: no bar,
        never retired by the nightly pass), named by ``text`` (their words).

        Review L1: the steps are this turn's and the turns' just before (``policy.Turn.steps_before``, kept across
        turns for ``policy.STEPS_KEEP_S``), so a way shown step by step over several turns is kept whole: the newest
        steps in one app (the app the last step acted in; going back, a step in another app ends them). A step that
        did not take is left out only when the same step took after it (a retry: the failed go's steps between them
        go too); any other leaves a gap, so nothing is kept and the agent says so (:data:`LINE_WAY_GAP`). Content-free log (``one_way``). Never raises: a way not
        kept is ``failed`` with a line for the agent."""
        name = _words(args.get("text"))[:120]

        def done(status: str, line: str, why: str, steps: int = 0) -> OneResult:
            self._emit("one_way", kept=status == WRITTEN, why=why, steps=steps)
            return OneResult("remember", status, verified=True if status == WRITTEN else None, result=line,
                             decision=decision, verb="remember")

        if not name:
            return done(FAILED, LINE_WAY_NAME, "no_name")
        steps = self._way_steps(turn)
        if not steps:
            return done(FAILED, LINE_WAY_NO_STEPS, "no_steps")
        if not all(s.get("ok") for s in steps):
            return done(FAILED, LINE_WAY_GAP, "gap", len(steps))
        app = next((_words(s.get("app")) for s in reversed(steps) if _words(s.get("app"))), "")
        try:
            from yuki.skills.learn import ways
        except Exception:  # noqa: BLE001
            ways = None
        build, keep = getattr(ways, "spec_from_steps", None), getattr(ways, "keep_told", None)
        if not callable(build) or not callable(keep):
            return done(FAILED, LINE_WAY_UNBUILT, "unbuilt", len(steps))
        if not app:
            return done(FAILED, LINE_WAY_NO_SPEC, "no_app", len(steps))
        try:
            spec = build(app, steps, end=None)
        except Exception:  # noqa: BLE001
            spec = None
        if not isinstance(spec, Mapping) or not spec:
            return done(FAILED, LINE_WAY_NO_SPEC, "no_spec", len(steps))
        key = self._with_store("one_keep_way", lambda store: keep(store, app=app, spec=dict(spec), name=name),
                               timeout_s=WAY_WRITE_S)
        if not key:
            return done(FAILED, LINE_WAY_FAILED, "not_kept", len(steps))
        try:
            shown = util.app_name(app) or app
        except Exception:  # noqa: BLE001
            shown = app
        return done(WRITTEN, LINE_WAY_KEPT.format(app=shown, name=name), "kept", len(steps))

    @staticmethod
    def _way_steps(turn: Any, now: float | None = None) -> list[dict[str, Any]]:
        """One22 review L1: the steps a told way is kept from, in the order they ran: the turns' just before
        (``steps_before``) then this turn's, within ``policy.STEPS_KEEP_S`` of ``now``; of them the newest run in one
        app (going back from the last step, a step in another app ends it, and so does a skill run, integ22; a step
        with no app of its own, an ``open_app``, stays in); a step that did not take is left out when the same step
        took after it (a retry), with the steps of that failed go between them. Each keeps its ``ok`` flag. Code facts
        only; never raises (``[]``)."""
        try:
            try:
                from yuki.memory.store import app_key
            except Exception:  # noqa: BLE001
                app_key = None

            def key(app: Any) -> str:
                text = _words(app)
                return str((app_key(text) if callable(app_key) else None) or text.casefold())

            now = time.time() if now is None else float(now)
            keep_s = float(getattr(policy, "STEPS_KEEP_S", CORRECTION_FALLBACK_S))
            ran = [*(getattr(turn, "steps_before", None) or []), *(getattr(turn, "steps", None) or [])]
            rows = [dict(s) for s in ran if isinstance(s, Mapping)
                    and not (isinstance(s.get("at"), (int, float)) and now - float(s["at"]) > keep_s)]
            # integ22 (the S6 rule under ONE): a skill run's own steps are never recorded here, so the steps before
            # it are not the way just done: going back from the last step, a run_skill ends the run (a skill run
            # last, "remember how I do this" after it: no steps, never an older go's steps in the same app)
            ran_skill = max((i for i, s in enumerate(rows) if s.get("tool") == "run_skill"), default=-1)
            rows = rows[ran_skill + 1:]
            app = next((key(s.get("app")) for s in reversed(rows) if _words(s.get("app"))), "")
            start = len(rows)
            while start > 0 and (not _words(rows[start - 1].get("app")) or key(rows[start - 1].get("app")) == app):
                start -= 1
            rows = rows[start:]

            def same(s: Mapping[str, Any]) -> tuple[Any, ...]:
                return (s.get("tool"), s.get("role"), s.get("name"), s.get("action"), s.get("open"), s.get("url"),
                        s.get("skill"), tuple(s.get("keys") or ()), s.get("enter"))

            out: list[dict[str, Any]] = []
            i = 0
            while i < len(rows):
                s = rows[i]
                if not s.get("ok"):
                    again = next((j for j in range(i + 1, len(rows)) if rows[j].get("ok") and same(rows[j]) == same(s)),
                                 None)
                    if again is not None:
                        i = again   # a retry: the go that worked goes on from it (the failed go's steps left out)
                        continue
                out.append(s)
                i += 1
            return out
        except Exception:  # noqa: BLE001
            return []

    def _one_handoff(self, report: str, facts: Mapping[str, Any], turn: policy.Turn, second: str) -> str:
        """One20 (final-D S5, one-core §3 fix 3): the engine's hand-off text was written for the planner ("with the
        skill pinned"); under ONE there is no planner: the head line is code's own, from the run's facts, and the
        evidence line after it stays. Routes that got nowhere: the verbs this channel has, and "not this skill again"
        only when its rungs read the app (a rung that never read, contract C10's ``unread``: the skill may run once
        more). A step that would send or join: the message skill and their click. Any other reason keeps the
        engine's own words (they name no planner)."""
        ev = facts.get("evidence") if isinstance(facts.get("evidence"), Mapping) else {}
        pin = facts.get("handoff_skill") if isinstance(facts.get("handoff_skill"), Mapping) else {}
        why = str(pin.get("why") or ev.get("reason") or "")
        if why in ("unverified", "error", "dropped"):
            return report
        rest = str(report or "").split("\n", 1)[1] if "\n" in str(report or "") else ""
        if why == "to_planner":
            head = ("A step of it would send or join something, so that step was not done: a message goes out only "
                    "through run_skill message with consent, and joining a call is their click.")
        else:
            channel = getattr(turn, "channel", "voice")
            verbs = ", ".join(n for n, s in T.SPECS.items() if s.kind == T.ACT and channel in s.channels
                              and n != "open_yuki")   # Yuki's own window never finishes a request of theirs
            touched = bool(facts.get("acted") or ev.get("acted"))
            head = "Its own routes didn't get there" + ("" if touched else "; nothing on the PC was touched")
            if second == "choose":
                head += "; code already ran it again as your pick from their things"
            elif second == "unread":
                head += "; code already ran it once more"
            head += f". Do it another way now: look, then your verbs ({verbs})"
            if ev.get("unread") or facts.get("unread"):
                head += "; its read of the app never came back, so nothing there was checked: look at it first."
            else:
                head += "; not this skill again with these values."
        return f"{head}\n{rest}".strip()

    def _note_chat(self, turn: policy.Turn, app: str) -> None:
        """One20 (S17): the window in front after a message skill's verified run, when it is that skill's app: the chat
        it opened (the agent's own typing there is allowed this turn). Never raises."""
        note = getattr(turn, "note_chat", None)
        if not callable(note) or not app:
            return
        try:
            front = self.verbs.foreground()
            if not front:
                return
            from yuki.voice.hands import _app_id

            target = self._facts_of(front)
            if _app_id(target.get("app")) == _app_id(app) or \
                    _app_id(util.app_name(target.get("app") or "")) == _app_id(util.app_name(app)):
                note(int(front))
        except Exception:  # noqa: BLE001
            pass

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
        # fb (final-F B5, contract K-C, p24_countdown_cancel): nothing was sent, a code fact the user is owed: the
        # driver says ``LINE_STOPPED_SEND`` when the turn ends with no new send and nothing of Yuki's heard (the owed
        # ledger's own rule), as the Stop path says it now; the agent's own words or a new send call replace it.
        # integ27 (fc merge note 3): only for a stop by their words; a chip they tapped (``swap``) is followed by the
        # agent's own new send of their choice, so nothing is owed for it
        return OneResult("run_skill", CHANGED, result=note, countdown="changed", swap=swap, decision=decision,
                         verb="run_skill", evidence={"why": note}, owed="" if swap else LINE_STOPPED_SEND)

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
        # fb (final-F B4): an ``asked`` run the engine measured touching nothing (``payload["acted"]`` not true) did
        # nothing on the PC: no ``sent_at``, so it is no act (never on the exchange floor as a hand that acted)
        untouched_ask = payload.get("outcome") == "asked" and payload.get("acted") is not True
        facts: dict[str, Any] = {"verified": verified if isinstance(verified, bool) else None,
                                 "outcome": str(payload.get("outcome") or ""),
                                 "done_line": str(payload.get("done_line") or ""), "skill": key,
                                 # final-B (one-voice RC E): an asked run's question as the engine's own field
                                 # (``payload["ask"]``, set by ``Dispatcher._run_skill`` for an ``asked`` outcome)
                                 "ask": str(payload.get("ask") or ""),
                                 "sent_at": started if payload.get("outcome") not in ("refused", None)
                                 and not untouched_ask else None}
        if isinstance(payload.get("focus_taken"), bool):
            facts["focus_taken"] = payload["focus_taken"]   # contract C1: measured per rung by the engine
        if isinstance(payload.get("acted"), bool):
            facts["acted"] = payload["acted"]               # C1 (review fix F6): something on the PC was touched
        if isinstance(payload.get("acted_apps"), (list, tuple)):
            facts["acted_apps"] = [str(a) for a in payload["acted_apps"] if isinstance(a, str) and a.strip()]
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
