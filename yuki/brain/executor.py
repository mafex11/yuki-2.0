"""The executor (brain-design.md §2.3): a plan's steps run as they stream, on the existing ``Dispatcher``.

How a request's plans run (one :class:`Executor` per request; every call on the request's own thread)::

    ex = Executor(host, checker=CommitCheck(client, settings, log), plan_seed=seed)
    ex.begin(head)                 # goal .. question closed in the stream
    ex.add_step(step)              # each closed, validated step: a read or a non-committing act runs now
    ...                            #   (a RunResult back means stop the stream: steer, cancel, a mismatch)
    result = ex.finish(plan, stop_reason=...)   # the block is complete: commit check, the rest in order, done-check
    ...                            # result.status == "mismatch" / "steer": replan, then begin/add_step/finish again
    ex.close(reply=line)           # finally: acting marker withdrawn, takeover watch off, desk lease freed, run row

**Streamed intake.** A read (``look``, ``memory``, ``fetch``) or an act that can't commit runs the moment its step
closes; reads of one plan run side by side (window reads one at a time on the dispatcher, memory and fetches in
parallel), acts in order. At the first may-commit step everything after it waits for the plan block to complete;
then the **commit check** (``yuki.brain.check``) runs once over every may-commit step left in the plan, and the rest
runs in order. ``max_tokens`` or ``refusal`` runs no further step (the steps already run committed nothing). A web
fetch after a file, clipboard or memory read in this request (M9) is may-commit in every plan kind: in an act plan it
waits as above; in an answer plan it passes the commit check and the consent rule at the end of the block (asked about
or refused as row 5 says; no lease, no marker); an ask plan sets it aside.

**Every act step** passes, in order: the steer / cancel / pause checkpoint (between steps, never mid-step); its window
and element resolved by name in a read of that window (the Dispatcher acts only on ids from its own reads; UI
Automation's by-name search, ``uia_route.find_in``, then tells a thin read from a missing control); the **consent
rule** (``yuki.brain.consent.check``) with the commit check's verdict for that very step and window (a may-commit step
never runs without one: a step the batch did not cover gets its own one-step check); an ask (``host.ask``, the desk
lease freed while it waits, one look at the window again after), a refusal, a draft or the send countdown; the acting
marker (``host.acting_begin``) before a step that takes focus, sends input or acts on a window (sound-only steps
publish none); the Dispatcher call (the D17 backstop inside); then its check: the step's ``expect`` probe (bounded,
event-paced: ``done.wait_read``) or the action's own verdict (``util.action_verdict``). A contradicted step, or an
expectation still false after 1.5 s, is a **mismatch**: the planner gets the plan id, the step statuses, what showed
(at most 1.5k characters) and that window's view.

**open_url opens in the browser the plan named** (x9): the step's ``window``, else the plan's target app, goes to the
tool as ``app`` (the name Windows gives that program: "Arc.exe" -> "Arc") when machine data says it is a browser (the
user's own timeline, ``web_route.measured_browsers``: a program that showed pages of several sites; or a browser
Windows has registered), so a chat app a link is in is never one; with none, Windows' default handler opens it. A
``page_at`` check then reads that browser's page (its ``process``), never a page in another browser; a probe's
``process`` the plan wrote as an app's name ("Arc") is read as that program's image ("Arc.exe"), as the probes compare.

**A run_skill step** gets the user's words of this exchange, and before them the span of an earlier request of theirs
that says a value the step gives (:meth:`Executor._skill_words`). One that didn't get there ends, if the request ends
on it, on a plain line of code's (never its outcome's code word).

**One desk lease** (``yuki.actions.leases.input_lease``) per act run: taken at the first act step, held across replans,
freed only while an ask waits (a consent question, or an ask plan's own question); an answer plan never takes it.

**Outside text** (a command's output, a file, a page, a window's view) reaches the planner only fenced as untrusted
data (``yuki.agent.untrusted``), capped before the fence goes on; it never goes into a line said to the user.

**The done-check** (``yuki.brain.done``): the required probe for the plan's class, the code line from its evidence,
coverage, and a background re-check for probes that can flip.

**The after-look** (P1.2-P1.3, 18 Oct free-thinker plan): the desk is read (``yuki.perception.situation.snapshot``,
a few ms, no screenshot) right before the request's first act step, after its consent and any countdown, and again when
a plan ends done; ``yuki.perception.diff.compare`` says what the request's acts did to it, with what the request was
about (``acted``: the windows its open, switch and window steps aimed at) and what its acts brought up (``touched``:
every act step's window, a skill run's own window or app). When a flag is up (a window that was in front, playing or
holding the keyboard got covered, the front was taken, a pop-up was left), the result carries an "After:" block
(:attr:`RunResult.after`) and the Brain gives the planner one tidy-up turn. Yuki's own windows stand for the window
the user called it from (they are never what the user had). **The tidy-up turn** (:meth:`Executor.tidy_up`): only
undoable window steps (``plan.TIDY_ACTIONS``: place, focus back), at most :data:`TIDY_STEPS`, and only on what the
request's own acts changed (:meth:`Executor._tidy_mine`: place a window its acts brought up, give the front back to
the window that was in front or held their attention), through the same consent rule, asked first when the user's tidy
dial says ask; never bracketed again (one look per request); no done-check: the steps' own read-back decides, and
code's words for each step that changed something (:data:`LINE_TIDY`, from what the step's report read back, never the
model's) are said after the request's line. **tidy21** (the user's decision 1, 19 Oct): when the after-look holds the
one legal step (``diff.tidy_step``), code takes it itself with no planner call (:meth:`Executor.take_tidy_step`; the
dial at ask: code's yes/no question first); the planner's tidy-up turn is left only for a flag with no such step.

**Run rows** (D18): ``MemoryAccess.record_run`` in the shape the old loop writes, each step in its ``_trace_step``
shape with ``tool`` the Dispatcher name that ran, the run's verdict from the done-check.

Logs are content-free (``yuki.log.events.content_free``): verbs, rows, verdicts, counts, ms.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from yuki.agent.tools import _is_own_window, sends_for_certain, tool_traits
from yuki.brain import consent, done, util
from yuki.brain.check import CheckVerdict, CommitCheck
from yuki.brain.consent import Call, Live, field_of, words
from yuki.log.events import Paused, Say, SendPending, ToolCall, ToolResult

if TYPE_CHECKING:   # an annotation only: the branch imports without bCore's plan.py
    from yuki.brain.plan import ExecHost

#: The plan's verbs: the Dispatcher's own tool names, plus the reads and ``wait`` (§2.2).
ACT_VERBS: tuple[str, ...] = ("launch_app", "focus_window", "open_url", "window_action", "tab", "click", "type_text",
                              "hotkey", "press", "scroll", "set_slider", "app_volume", "run_powershell",
                              "write_clipboard", "run_skill", "open_yuki")
READ_VERBS: tuple[str, ...] = ("look", "memory", "fetch")
VERBS: tuple[str, ...] = ACT_VERBS + READ_VERBS + ("wait",)
#: ``look``'s ``value``: what to read, and the Dispatcher tool that reads it.
LOOK_TOOLS: dict[str, str] = {"window": "look_at_window", "desktop": "look_at_desktop", "focus": "look_at_focus",
                              "page": "read_page", "tabs": "read_tabs", "clipboard": "read_clipboard",
                              "system": "system_facts"}
#: The verbs whose step names an element (resolved by name in a read of its window).
ELEMENT_VERBS = frozenset({"click", "type_text", "scroll", "set_slider"})
#: Verbs whose ``value`` is one plain field of the tool's input.
PRIMARY_FIELD: dict[str, str] = {"launch_app": "query", "open_url": "url", "window_action": "action", "tab": "action",
                                 "type_text": "text", "press": "key", "run_powershell": "command",
                                 "write_clipboard": "text", "open_yuki": "page"}
#: The input fields a step's JSON ``value`` may set, per verb (never ids, coordinates, windows or Enter: those come
#: from the step's own fields and its resolution).
VALUE_FIELDS: dict[str, frozenset[str]] = {
    "launch_app": frozenset({"query", "args"}), "open_url": frozenset({"url", "app"}),
    "window_action": frozenset({"action", "monitor", "how"}), "tab": frozenset({"action"}),
    "click": frozenset({"button", "clicks", "toggle_to"}), "type_text": frozenset({"text", "clear", "accept_suggestion"}),
    "hotkey": frozenset({"keys"}), "press": frozenset({"key", "times"}), "scroll": frozenset({"dy", "dx", "until_stopped"}),
    "set_slider": frozenset({"to", "by", "keys"}), "app_volume": frozenset({"app", "change", "level", "mute"}),
    "run_powershell": frozenset({"command"}), "write_clipboard": frozenset({"text"}),
    "run_skill": frozenset({"skill", "slots", "place"}), "open_yuki": frozenset({"page"}),
}
#: A ``click``'s plain value (bCore's validator: ``left``, ``right``, ``middle``, ``double``) as the click tool's input.
CLICK_VALUES: dict[str, dict[str, Any]] = {"left": {}, "right": {"button": "right"}, "middle": {"button": "middle"},
                                           "double": {"clicks": 2}}

#: Budgets (§2.3): an act run takes at most this many replans (``brain_max_replans`` overrides it) and model calls;
#: an answer plan at most this many calls or seconds.
MAX_REPLANS = 3
ACT_MODEL_CALLS = 5
ANSWER_MODEL_CALLS = 8
ANSWER_SECONDS = 30.0
#: A mismatch's diff and window view are cut to these sizes (characters).
DIFF_CHARS = 1500
VIEW_CHARS = 6000
#: A read's text handed back to the planner (characters).
READ_CHARS = 12000
#: How long the desk lease is waited for (s), a ``wait`` step at most (s), and a resolution's by-name search (s).
LEASE_WAIT_S = 5.0
WAIT_MAX_S = 10.0
#: A read of a window is reused for an element lookup while its events show no change and it is at most this old.
READ_REUSE_S = 2.0
#: The send countdown when neither the host nor the settings set one (s): the user's "3 s to stop".
DEFAULT_COUNTDOWN_S = 3.0
#: A send countdown counts as one the user could stop (review r1 L3: a send grant for Yuki's own wording needs one, or
#: their yes) only when it was open at least this long and ran out (``going``). A host with no countdown (0 s: the
#: Brain's and the old loop's default, off the desktop) returns at once.
COUNTED_MIN_S = 0.5
#: review r2 M1: a ``run_skill`` send whose engine outcome is one of these sent nothing (it asked, handed over, was
#: refused or held, or its check saw nothing go), so the same message may be sent by a later step of the request.
NOT_SENT_OUTCOMES = frozenset({"asked", "handed_over", "refused", "held_game", "held_call", "failed"})
#: kBrain review M1: the skill engine's code words for a run it held before acting: a game in front, a call live
#: (decision 2: a pick is never put on then). Settled: the run ends on the engine's own line, never a replan.
HELD_OUTCOMES = {"held_game": "game", "held_call": "call"}
#: P1.3: the after-look's room: the diff's words and the desk after it (characters).
AFTER_DIFF_CHARS = 500
AFTER_DESK_CHARS = 700
#: P1.3: a tidy-up turn takes at most this many act steps (``plan.TIDY_ACTIONS``).
TIDY_STEPS = 2
#: A tidy-up step's words, said after the request's line once the step's own read-back saw it change something (code's
#: words from its report, never the model's; ``{app}``: the window's app as Windows names it, ``{where}``:
#: :data:`TIDY_WHERE`).
LINE_TIDY: dict[str, str] = {"place": "I moved {app} to {where}.", "place_here": "I moved {app}.",
                             "focus_back": "I put {app} back in front."}
#: The same as a question, when the user's tidy dial says ask first.
LINE_TIDY_ASK: dict[str, str] = {"place": "Should I move {app} to {where}?", "place_here": "Should I move {app}?",
                                 "focus_back": "Should I put {app} back in front?"}
#: Safety timing 3 (recheck): code's own step left after their yes, for any reason (measured again it is no longer the
#: step, their hold, a gate, a refusal), said plainly once, so a yes is never dropped in silence.
LINE_TIDY_LEFT: dict[str, str] = {"place": "I didn't move {app} to {where} after all.",
                                  "place_here": "I didn't move {app} after all.",
                                  "focus_back": "I didn't put {app} back in front after all."}
#: A tidy-up step's window when Windows names no app for it.
TIDY_NO_APP = "that window"
#: Integration review c2desk-2: code's one measured line after the request's own when the after-look shows what they
#: asked for did not reach its purpose (no tidy step can touch either): a control turned toward sound still silent
#: (``diff`` ``left_silent``: its own level or mute, else the PC's), and what played now behind another tab.
LINE_STILL_LEVEL = "It's still at {level}%."
LINE_STILL_MUTED = "It's still muted."
LINE_PC_SILENT = {"muted": "The PC's own volume is still muted.", "level": "The PC's own volume is still at {level}%."}
LINE_BEHIND_TAB = "What was playing is still playing behind it."
#: Where a placed window went, in words: another monitor, else a half of the same one (else ``place_here``).
TIDY_WHERE: dict[str, str] = {"other": "your other screen", "left": "the left half", "right": "the right half"}
#: aBrain (phase 2.3; 18 Oct live A: "Opening a page in Google Chrome." and "Clicking in Google Chrome." said by code
#: as every narrated step started): nothing is said as a step starts. A step still running after SLOW_STEP_S (s) is
#: said once, in the plan's goal's words (voice only; never a tidy-up step), so a quick step is silent and a slow one
#: says what Yuki is after, not which verb runs. Once per goal: a replan with the same goal says nothing again.
SLOW_STEP_S = 2.5
LINE_SLOW_STEP = "Still on it: {goal}."
#: aBrain (phase 2.5, "a done probe is required when an act can be read"): a plan by steps that wrote done none is read
#: by the probe its last readable act step's effect names (verb and its closed values -> probe; data, never a reading
#: of their words): what the act changed (the page, the front, a window's state, a closed window or tab, a typed box,
#: an app's level). ``window_action`` values map to the state :data:`done.WINDOW_STATES` names.
DERIVED_WINDOW_STATES: dict[str, str] = {"minimize": "minimized", "maximize": "maximized", "fullscreen": "fullscreen",
                                         "restore": "normal"}
#: aBrain (phase 2.3): a done's target fields its probe didn't read, in words for the evidence (``ending_facts``).
UNCHECKED_WORDS: dict[str, str] = {"app": "the app", "window": "the window or page", "person": "the person",
                                   "item": "the thing itself", "steps": "the steps before the last"}
#: Review M1: the "field" a derived done read leaves uncovered when an earlier act step wasn't seen to take
#: (:meth:`Executor._derived_rest`); no target field is called this.
DERIVED_REST = "steps"
#: The verbs whose acts the after-look is about (a clipboard write changes nothing on the desk).
DESK_VERBS = frozenset(ACT_VERBS) - {"write_clipboard"}
#: The verbs whose step aims at the window it ends on: that window is what the request was about (``acted``).
ASKED_VERBS = frozenset({"launch_app", "focus_window", "open_url", "window_action", "tab", "open_yuki"})
_WARMED = False

def _warm_desk() -> None:
    """Once per process: the after-look's first desk read pays the media and mixer reads' cold start in the background
    (``situation.warm``), so the read before the first act is ms."""
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    try:
        from yuki.perception import situation

        situation.warm()
    except Exception:  # noqa: BLE001
        pass


def _foreground_now() -> int:
    """The window in front now (one read-only Win32 call), ``0`` when it can't be read."""
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


#: How long a pause waits for the user to resume before the run stops quietly (s; ``takeover_idle_stop_s``).
PAUSE_LIMIT_S = 600.0

LINE_DECLINED = "Okay, I won't."
LINE_BUSY = "Something else is using the mouse and keyboard right now, so I stopped. Try again in a moment?"
LINE_PAUSE_STOP = "Stopped. Nothing else was done."
LINE_STOPPED_SEND = "Stopped. Nothing was sent."
LINE_CUT_SHORT = "I lost my train of thought partway through, so I stopped there."
#: A run the engine held for a live call that gave no line of its own (its own line says so and names the pick).
LINE_HELD_CALL = "You're on a call, so I held off."
#: fBrain (18 Oct live A, k_t3_draft: a focus the input gate refused said "You're in a game: after the round?" with no
#: game anywhere): an act step the input gate refused while no game is in front and none is its target. The gate
#: refuses for a game, or fails closed when its own check broke; with no game seen, the game line would be untrue, so
#: this one says only that the step was stopped (``{what}``: the step in words), and the run ends there (a replan would
#: meet the same gate). ``{what}``: :data:`STOPPED_WHAT`.
LINE_INPUT_STOPPED = "Something on the PC stopped me {what}, so I left it there."
#: hands19 (final-B all-messaging RC5, k_t3_draft: "Something on the PC stopped me bringing that window forward, so I
#: left it there." named neither the app nor what was left undone): the same stop when act steps of the plan were still
#: to run after it (``{rest}``: those steps in the island's words, at most :data:`STOPPED_REST_MAX`).
LINE_INPUT_STOPPED_REST = "Something on the PC stopped me {what}, so I didn't do the rest ({rest})."
STOPPED_REST_MAX = 2
#: fHands note 2: a ``run_skill`` step the gate stopped for anything but a game (``refused: blocked``) that gave no line
#: of its own: plain, never the game line.
LINE_BLOCKED = "Something on the PC stopped me there, so nothing was done."
#: The skill engine's code words for a run the gate stopped for anything but a game (the skills side's ``blocked``
#: outcome, whichever word it lands as): settled like a held run, said with :data:`LINE_BLOCKED`.
BLOCKED_OUTCOMES = frozenset({"blocked", "held_blocked"})
#: jBrain (fMsg B1; 18 Oct live A, k_clarify_who_draft and msg_quix_beat_draft: the message capability asked "Who to?"
#: or couldn't place the person, and the planner typed the text into whichever DM happened to be open): said when a
#: request ends with whom a message goes to still open.
LINE_WHO = "Who's it for?"
#: The planner's hint after a capability with a person slot dropped it as unheard or found no one by it (code's words).
HINT_WHO_UNHEARD = ("It didn't run: whom it goes to isn't in their words as heard, or no one of theirs goes by it. Ask "
                    "them whom: a plan kind ask with one short question (name the one person of theirs it sounds "
                    "like, if one does). Never type it into whichever chat is open.")
#: The refusal of a typing step into a chat while whom the message goes to is open (:meth:`Executor._who_guard`).
HINT_WHO_FIRST = ("not typed: whom this message goes to isn't settled (its capability asked or couldn't place the "
                  "person), so nothing is typed into whichever chat is open. Ask them whom (a plan kind ask), then run "
                  "the message capability with that person.")
#: The step a gate stopped, in words (the closed act verbs; anything else: "there").
STOPPED_WHAT: dict[str, str] = {
    "launch_app": "opening the app", "focus_window": "bringing that window forward", "open_url": "opening the page",
    "window_action": "arranging the window", "tab": "switching the tab", "click": "clicking", "type_text": "typing",
    "hotkey": "pressing the shortcut", "press": "pressing the key", "scroll": "scrolling",
    "set_slider": "moving the slider", "write_clipboard": "copying"}
#: hands19: the same, naming the app the stopped step acts in (``{app}``: its target window's program as people say
#: it, else the plan's target app), used when that app is known.
STOPPED_WHAT_APP: dict[str, str] = {
    "launch_app": "opening {app}", "focus_window": "bringing {app} forward", "open_url": "opening the page in {app}",
    "window_action": "arranging {app}'s window", "tab": "switching the tab in {app}", "click": "clicking in {app}",
    "type_text": "typing in {app}", "hotkey": "pressing the shortcut in {app}", "press": "pressing the key in {app}",
    "scroll": "scrolling in {app}", "set_slider": "moving the slider in {app}"}
#: fBrain (18 Oct live A, "Pull up ChatGPT and ElevenLabs": two open_url steps whose own reports said "navigation
#: confirmed", done none, said "I did it, but I couldn't confirm it worked."): the probe whose words say an act verb's
#: effect, for a plan by steps that wrote done none and whose every act step its own report saw take
#: (:meth:`Executor._steps_seen`). Verb -> probe: data, never a reading of their words.
STEP_LINE_PROBES: dict[str, str] = {"open_url": "page_at"}
LINE_DRAFT_HELD = "I opened the chat but didn't type the text in."
LINE_CHAT_UNCONFIRMED = "I couldn't confirm the chat is open, so I didn't type anything."
#: A ``run_skill`` step that didn't get it done, as said when the request ends on it (x9: never the outcome's code
#: word, "handed_over" or "asked", in speech: 10 Oct's "I got stuck running your usual way: handed_over"). By the
#: hand-off seed's ``why``: its routes didn't get there (or it didn't run), it acted but the result is unknown, or it
#: stopped before a step that sends or joins (that step didn't run).
LINE_SKILL_HANDED = "My usual way for that didn't get there, so it isn't done. Try another way?"
LINE_SKILL_UNSURE = "I tried my usual way for that, but I couldn't confirm it worked."
LINE_SKILL_STOPPED = "I stopped before the step that sends or joins, so that part isn't done."
#: The capability needed one thing more from the user (``{ask}``: its own question, capability data).
LINE_SKILL_ASK = "Before I can do that: {ask}"
LINE_SKILL_ASK_PLAIN = "I needed one more thing from you to do that."
LINE_SKILL_FAILED = "My usual way for that didn't work. Try another way?"
#: The slot types the skill resolver checks a given value of against the user's words (``yuki.skills.resolve.
#: check_value``: a whole-words span, or a name it is known by); an enum, a number or a switch is checked against its
#: own options instead (``yuki.skills.connection.SLOT_TYPES``).
WORD_SLOT_TYPES = frozenset({"text", "app", "person", "entity"})


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class StepStatus:
    """One step of the current plan as it went: ``state`` ``pending`` | ``ok`` | ``failed`` | ``refused`` |
    ``skipped`` | ``declined``; ``verdict`` its check; ``why`` a short reason; ``tool`` the Dispatcher name that
    ran; ``result`` what came back (for the planner, capped)."""

    index: int
    verb: str
    state: str = "pending"
    verdict: bool | None = None
    why: str = ""
    tool: str = ""
    ms: float = 0.0
    result: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"i": self.index, "verb": self.verb, "state": self.state, "verdict": self.verdict, "why": self.why,
                "tool": self.tool, "ms": self.ms}


@dataclass
class Mismatch:
    """What the planner gets back when a step didn't go as planned: the step, what it expected, what showed (at most
    :data:`DIFF_CHARS`; fenced as untrusted data when it is text from outside Yuki, ``seen_fenced``), that window's
    view (fenced, its body capped at :data:`VIEW_CHARS`), why (a code word), and a skill hand-off's plan seed when a
    capability's routes didn't get there (rung C)."""

    step: int
    verb: str
    expected: str
    seen: str
    view: str = ""
    why: str = ""
    refused: bool = False
    seed: dict[str, Any] | None = None
    seen_fenced: bool = False

    def as_text(self) -> str:
        parts = [f"Step {self.step + 1} ({self.verb}) did not go as planned ({self.why or 'mismatch'})."]
        if self.expected:
            parts.append(f"Expected: {self.expected}")
        if self.seen:
            # fenced text was capped before its fence went on: cutting it now would cut the fence's end off
            parts.append(f"What showed: {self.seen if self.seen_fenced else self.seen[:DIFF_CHARS]}")
        if self.view:
            parts.append(f"That window now:\n{self.view}")
        return "\n".join(parts)


@dataclass
class RunResult:
    """One plan's run, for the planner loop.

    ``status``: ``done`` (every step ran and the done-check ran: see ``done``), ``answer`` (an answer plan's reads are
    in ``reads``), ``ask`` (an ask plan: ``line`` is its question), ``mismatch`` (replan with ``mismatch``), ``steer``
    (the user's words in ``words``: replan with them), ``refused`` (``line``; ``final`` ends the run with it, else a
    replan may route around it), ``declined`` (the user said no), ``stopped`` (the stream was cut short, the
    countdown was stopped, or the pause ran out), ``cancelled``, ``busy`` (the desk lease was held elsewhere).
    ``line``: what code says when the run ends here. ``outcome``: ``Final.outcome`` when it ends here. ``parts``: on
    a run that didn't end done, the lines of the request's ``run_skill`` parts whose own check saw them done (this plan's
    and earlier plans'), said before the line the request ends on (kBrain review M2)."""

    status: str
    plan_id: str = ""
    statuses: list[StepStatus] = field(default_factory=list)
    mismatch: Mismatch | None = None
    words: list[str] = field(default_factory=list)
    reads: list[dict[str, Any]] = field(default_factory=list)
    done: done.DoneResult | None = None
    line: str = ""
    final: bool = False
    outcome: dict[str, Any] | None = None
    parts: list[str] = field(default_factory=list)
    #: P1.3: on a done run whose acts took something the user had (a diff flag), the "After:" block for the planner's
    #: tidy-up turn (``""``: nothing flagged, no turn); ``after_facts``: the diff's content-free facts.
    after: str = ""
    after_facts: dict[str, Any] = field(default_factory=dict)

    def statuses_text(self) -> str:
        """The step statuses in one block, for the replan message."""
        return json.dumps([s.as_dict() for s in self.statuses], ensure_ascii=False)


@dataclass
class _SkillRun:
    """One ``run_skill`` step that ran: its outcome, the capability and slots it ran with, whether it was a draft, and
    for a draft with Yuki's wording typed after the chat was seen open, that typing's own verdict."""

    index: int
    outcome: Any
    key: str
    slots: dict[str, Any]
    drafted: bool = False
    type_after: bool = False
    typed: bool | None = None


class _Lease:
    """The desk lease for one act run (``input_lease``, re-entrant per thread): taken once, held across replans,
    freed while an ask waits and taken again after."""

    def __init__(self) -> None:
        self._stack: ExitStack | None = None
        self.owner: int | None = None

    @property
    def held(self) -> bool:
        return self._stack is not None

    def take(self, timeout_s: float = LEASE_WAIT_S) -> bool:
        if self._stack is not None:
            return True
        from yuki.actions.leases import LeaseBusy, input_lease

        stack = ExitStack()
        try:
            stack.enter_context(input_lease(timeout_s=timeout_s))
        except LeaseBusy:
            stack.close()
            return False
        self._stack, self.owner = stack, threading.get_ident()
        return True

    def release(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                stack.close()
            except RuntimeError:   # freed from another thread than the one that took it: it was that thread's
                pass
        self.owner = None


# ---------------------------------------------------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------------------------------------------------


class Executor:
    """One request's plan runs on the Dispatcher (module docstring).

    Args:
        host: The :class:`yuki.brain.plan.ExecHost` of the request (bCore's): ids, the user's words, memory, the
            dispatcher, settings, the acting marker, cancel / pause / steer, events, asks. Optional extras read with
            ``getattr``: ``log(event, **fields)``, ``earlier_words`` (the user's earlier turns, their own words),
            ``send_countdown_s``, ``send_wake`` / ``pause_wake`` (``threading.Event``s set on a cancel, a chip, a
            resume), ``nonce`` (the request's untrusted-data fence), ``model_calls`` / ``cost_usd`` (for the run row).
        checker: The commit check (one per request; ``None``: one is made with ``getattr(host, "client")``).
        plan_seed: A skill hand-off's pinned dict (rung C): its done-check is fixed.
    """

    def __init__(self, host: ExecHost, *, checker: CommitCheck | None = None,
                 plan_seed: Mapping[str, Any] | None = None) -> None:
        self.host = host
        self.dispatcher = host.dispatcher
        self.settings = getattr(host, "settings", None)
        self._log_fn = getattr(host, "log", None)
        self.checker = checker or CommitCheck(getattr(host, "client", None), self.settings, self._log_fn)
        self.seed = dict(plan_seed) if isinstance(plan_seed, Mapping) and plan_seed else None
        self.words: list[str] = [str(w) for w in (getattr(host, "user_words", None) or []) if str(w).strip()]
        self.earlier: list[str] = [str(w) for w in (getattr(host, "earlier_words", None) or []) if str(w).strip()]
        #: bRound2: the user's turns of the whole conversation (memory's turn log), read on first use (:meth:`_said_before`)
        self._convo: list[str] | None = None
        self.steers: list[str] = []
        self.started = time.time()
        self.started_mono = time.monotonic()
        self.lease = _Lease()
        #: A step that named no window acted on the request's origin (logged once).
        self._origin_used = False
        self.acting = False
        self.replans = 0
        self.revision = 0
        #: kBrain: the lines of parts earlier plans of this request did, their own skill's check seeing them (kept by
        #: :meth:`begin`): said first in the request's line (``done.parts_done`` / ``done.with_parts``).
        self._parts_before: list[str] = []
        self.trace: list[dict[str, Any]] = []
        self.numbers: set[int] = set()
        self.typed_pending: dict[int, str] = {}
        self.reads_seen: set[str] = set()
        self.last_failed: dict[str, Any] | None = None
        self.last_act: dict[str, Any] = {}
        self.join_controls = consent.join_controls()
        self.nonce = str(getattr(host, "nonce", "") or "")
        if not self.nonce:
            from yuki.agent.untrusted import new_nonce

            self.nonce = new_nonce()
        self._pool: ThreadPoolExecutor | None = None
        # every Dispatcher read and act (overviews, probe reads, the reads running beside an act) takes this lock: its
        # screen model is shared; re-entrant, so a read inside a held section can't deadlock its own thread
        self._dispatch_lock = threading.RLock()
        self._read_cache: dict[int, tuple[float, int | None, list[dict[str, Any]], dict[str, Any]]] = {}
        self._closed = False
        self._recheck: threading.Thread | None = None
        self._recheck_verdict: dict[str, Any] = {}
        self._result: RunResult | None = None
        self._last_done: done.DoneResult | None = None
        #: hands19: the hand-off's own check was read at a plan's finish (:meth:`_seed_also`): the brain's observer
        #: never reads it again for this request.
        self.seed_read = False
        #: hands19: the hand-off's own check (``done``, ``done_words``, ``baseline``) held for the current plan when it is
        #: ``Plan.seed_also`` (the brain sets it with each plan: ``Brain._seed_executor``), else ``None``.
        self.seed_also: dict[str, Any] | None = None
        self._skill_key: str | None = None
        #: zFinal: the request's desk hands for ``run_skill`` (``None`` until a run_skill needs them; ``False``: they
        #: couldn't be made).
        self._desk: Any = None
        self._swap_words: list[str] = []
        #: review r1 L2: the messages a send grant let go in this request (person, folded text): never a second time
        self._sent_keys: set[tuple[Any, str]] = set()
        #: review r1 L3: whether the last send countdown ran long enough for the user to stop it
        self._counted = False
        self._done_replan = False
        self._last_overview: dict[str, Any] = {}
        #: x9: the program (process image) of the browser this request's last open_url of a web address opened it in
        #: by name; a page_at check reads that browser's page, never another browser's (``""``: none named, or the
        #: last one went to Windows' default handler)
        self.browser = ""
        #: x9 (review r1 M1): the programs that are browsers by machine data, read once per request
        #: (:meth:`_browsers`; ``None``: not read yet)
        self._browser_rows: list[dict[str, Any]] | None = None
        #: P1.2: the desk right before this request's first act (the after-look's baseline), and the windows or apps
        #: its acts aimed at (``acted``) and brought up (``touched``)
        self._desk_first: Any = None
        self._desk_asked: set[Any] = set()
        self._desk_touched: set[Any] = set()
        #: integration T3: the program keys (``situation._key``) this request's launches measured (the launch's process,
        #: its identity's image names): a new window of one of them in front after the acts is the ask, by id
        self._desk_launched: set[str] = set()
        #: safety WW3 / T-1: the program keys a skill run's rungs were measured acting in (its ``acted_apps``): with
        #: :attr:`_desk_launched`, the programs whose windows new since the first act are the acts' (``diff.compare``'s
        #: ``fresh``); any other new window the user opened while the plan ran, and stays theirs
        self._desk_programs: set[str] = set()
        #: tidy21 review F1: the windows whose own place or state this request's acts were asked for (a window_action
        #: place / maximize / fullscreen / restore, a skill run given a place: ``diff.arranges``): code's tidy step
        #: never moves one away again (``diff.tidy_guard``'s ``asked_place``)
        self._desk_arranged: set[int] = set()
        #: integration review c2desk-1: the apps whose mixer rows this request's acts set (an app_volume step's app, a
        #: skill run's measured apps): the after-look's ``left_silent`` scope only, never its window scope
        self._desk_sound: set[Any] = set()
        #: integration review c2desk-2: the after-look's measured line for what didn't reach its purpose ("" none)
        self._purpose_line = ""
        #: P1.3: the after-look's diff when a flag was up (what a tidy-up may touch: :meth:`_tidy_mine`), and how many
        #: tidy steps changed something (a done re-check that reads after one is not a false done)
        self._after_diff: Any = None
        self._tidy_changed = 0
        #: aBrain: the goals a slow step was said in (:meth:`_slow_watch`: once each)
        self._slow_said: set[str] = set()
        #: P1.3: the tidy-up turn, while it runs (:meth:`tidy_up`): ``{"mode", "acts", "words"}``; ``None`` otherwise
        self.tidy: dict[str, Any] | None = None
        #: desk20 (final-D S20): the last act step of this request whose own report said it did not take, in code's
        #: words (:meth:`_stop_words`: the step and its app, the plan's act steps after it), kept across a later plan
        #: that ran no act (an invalid one) and let go once a later act step takes: ``{"what", "rest"}`` or ``None``
        self._stuck_said: dict[str, str] | None = None
        #: desk20 / tidy21: the after-look's one legal tidy step (``diff.tidy_step``), which code takes itself under the
        #: tidy dial "do" (:meth:`take_tidy_step`), and the window the after-look saw in front (the step's guard)
        self._after_step: dict[str, Any] | None = None
        self._after_front: int | None = None
        self._after_state: Any = None
        #: safety timing 3 (recheck): set only while code's asked step is measured again after their yes
        #: (:meth:`_tidy_still`): Yuki's own windows are measured as if they were not there (:meth:`_own_aside`)
        self._own_beneath = False
        _warm_desk()
        self._reset_plan()
        self._skill_context()

    # -- the plan being run --------------------------------------------------------------------------------------------

    def _reset_plan(self) -> None:
        self.head: Any = None
        self.plan_id = ""
        self.steps: list[Any] = []
        self.statuses: list[StepStatus] = []
        self.holding = False
        self.pending_reads: list[tuple[int, Future]] = []
        self.read_results: list[dict[str, Any]] = []
        self.skill_outcomes: list[_SkillRun] = []
        self.batch: dict[int, CheckVerdict] = {}
        self._batch_typed: dict[int, bool] = {}
        self._batch_words = -1   # how many of the user's words the batch was checked on (they only grow)
        #: x9: each open_url step's browser image as its resolution found it (step index -> image)
        self._open_images: dict[int, str] = {}
        #: jBrain (fSkills merge note 2): each open_url step's page its own report confirmed (step index -> the page's
        #: title, as launch.open_url read it), for a done that saw only the app (:meth:`_pages_cover`)
        self._open_pages: dict[int, str] = {}
        #: aBrain: each act step's call as it ran (step index -> Call), for a done none's derived probe
        #: (:meth:`_derived_probe`)
        self._ran_calls: dict[int, Call] = {}
        #: jBrain (fMsg B1): a capability with a person slot asked about it, or dropped it as unheard, in this request,
        #: and no later run of it confirmed a chat: whom the message goes to is not settled, so nothing is typed into
        #: whichever chat is open (:meth:`_who_guard`)
        from yuki.brain.plan import person_open

        self._who_unsettled = person_open(self.seed)   # a hand-off that left the person open (the talker's run)
        self.stopped: RunResult | None = None
        self.plan_since = time.time()

    def begin(self, head: Any) -> None:
        """A new plan (or a revision): its header (goal .. question) closed in the stream. Steps already done stay
        done; a revision lists only what is left (or what it redoes). kBrain: the parts the plans before it did and
        their own skill saw done (a later part stopped that plan, or the planner went on after it) are kept, so the
        request's line says them too; not after the user's own words redirected it (a steer)."""
        self._join_reads()
        if self._result is not None and self._result.status == "mismatch":
            self.replans += 1          # a replan after a mismatch counts; one after the user's own words does not
        if self._result is not None and self._result.status != "steer":
            self._parts_before = self._done_parts()
        self._reset_plan()
        self.revision += 1
        self.head = head
        self.plan_id = str(field_of(head, "id", "") or f"p{self.revision}")
        self._emit_log("brain_plan", kind=str(field_of(head, "kind", "act")), route=_route_kind(head),
                       commit=consent.plan_consent(head)["commit"], revision=self.revision)

    def add_step(self, step: Any) -> RunResult | None:
        """One closed, validated step of the plan streaming now. A read, or an act that can't commit, runs at once;
        from the first may-commit step on, steps wait for :meth:`finish`. Returns a :class:`RunResult` when the run
        must stop here (the caller then stops reading the stream), else ``None``."""
        if self.head is None:
            raise RuntimeError("begin() the plan before its steps")
        index = len(self.steps)
        self.steps.append(step)
        self.statuses.append(StepStatus(index=index, verb=str(field_of(step, "verb", ""))))
        if self.stopped is not None or self.holding:
            return self.stopped
        if str(field_of(self.head, "kind", "act")) in ("answer", "ask"):
            # a web fetch after a file, clipboard or memory read could carry what was read out (M9): it waits for
            # finish(), where it passes the commit check and the consent rule (answer) or is set aside (ask)
            if self._verb(step) in READ_VERBS and not self._fetch_commits(step):
                self._start_read(index, step)
            return None
        verb = self._verb(step)
        if verb in READ_VERBS and not self._fetch_commits(step):
            self._start_read(index, step)
            return None
        if verb == "wait":
            self._join_reads()
            return self._stop_if(self._wait(index, step))
        call, why = self._resolve(index, step)
        if call is None:
            return self._stop_if(self._mismatch_result(index, why))
        if self.tidy is not None:
            bounded = self._tidy_gate(index, call)   # before any commit check is spent on it
            if bounded is not None:
                return self._stop_if(bounded)
        live = self._live(call)
        if consent.may_commit(call, live, self.head):
            self.holding = True
            return None
        return self._stop_if(self._run_act(index, step, call=call))

    def finish(self, plan: Any = None, *, stop_reason: str | None = None) -> RunResult:
        """The plan block is complete (``plan``: the whole plan, steps included; ``None``: the header and the steps fed
        so far). Runs the commit check over every may-commit step left, the rest in order, then the done-check.
        ``stop_reason`` ``max_tokens`` / ``refusal``: nothing further runs."""
        if plan is not None:
            fed = len(self.steps)
            for step in list(field_of(plan, "steps", []) or [])[fed:]:
                self.steps.append(step)
                self.statuses.append(StepStatus(index=len(self.steps) - 1, verb=str(field_of(step, "verb", ""))))
            self.head = plan
        if self.stopped is not None:
            return self._finished(self.stopped)
        if stop_reason in ("max_tokens", "refusal"):
            self._join_reads()
            for s in self.statuses:
                if s.state == "pending":
                    s.state, s.why = "skipped", f"the plan was cut short ({stop_reason})"
            return self._finished(RunResult("stopped", line=LINE_CUT_SHORT, final=True))
        kind = str(field_of(self.head, "kind", "act"))
        if kind in ("ask", "answer"):
            started = {j for j, _f in self.pending_reads} | {int(r.get("i", -1)) for r in self.read_results}
            gated: list[int] = []
            for i, step in enumerate(self.steps):
                if self.statuses[i].state != "pending" or self._verb(step) not in READ_VERBS or i in started:
                    continue
                if not self._fetch_commits(step):
                    self._start_read(i, step)
                elif kind == "ask":
                    s = self.statuses[i]
                    s.state, s.why = "skipped", "a fetch after a file, clipboard or memory read runs only in an act " \
                                                "or answer plan, after the commit check"
                else:
                    gated.append(i)
            if gated:
                stop = self._gate_fetches(gated)
                if stop is not None:
                    self._join_reads()
                    return self._finished(stop)
            self._join_reads()
            if kind == "ask":
                self.lease.release()   # the desk is free while the user answers; the next act step takes it again
                return self._finished(RunResult("ask", line=words(field_of(self.head, "question", ""))))
            return self._finished(RunResult("answer", reads=list(self.read_results)))
        result = self._run_rest()
        if result is not None:
            return self._finished(result)
        self._join_reads()
        if self.tidy is not None:
            # the tidy-up's steps were read back by their own act (place verifies the monitor and the state): their
            # words, said after the request's line; no done-check, no after-look again
            said = " ".join(w for w in self.tidy["words"] if w)
            return self._finished(RunResult("done", line=said, final=True, outcome={}))
        result = self._done_check()
        if result.status == "done":
            result.after, result.after_facts = self.after_look()
            sent = isinstance(result.outcome, Mapping) and bool(result.outcome.get("send"))
            if self._purpose_line and result.line and not sent:
                # integration review c2desk-2: said after the request's own line (never on a send's, consent's own)
                result.line = f"{result.line.rstrip()} {self._purpose_line}"
        return self._finished(result)

    def run(self, plan: Any, *, stop_reason: str | None = None) -> RunResult:
        """A whole plan at once (not streamed): :meth:`begin`, each step, :meth:`finish`."""
        self.begin(plan)
        for step in list(field_of(plan, "steps", []) or []):
            early = self.add_step(step)
            if early is not None:
                return self.finish(stop_reason=stop_reason)
        return self.finish(stop_reason=stop_reason)

    def close(self, *, reply: str = "", outcome: str = "final") -> None:
        """The request ended (call it in a ``finally``): reads joined, the acting marker withdrawn and the takeover
        watch off (``host.acting_end``), the desk lease freed, the run row written (after the re-check when one is
        still reading). Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            self._join_reads()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.host.acting_end()   # idempotent (ExecHost): withdraws the marker and stops the takeover watch
        except Exception:  # noqa: BLE001
            pass
        self.acting = False
        self.lease.release()
        if self._pool is not None:
            self._pool.shutdown(wait=False)
        run = self._run_row(reply=reply, outcome=outcome)
        thread = self._recheck
        if thread is not None and thread.is_alive():
            def later() -> None:
                thread.join(done.RECHECK_AFTER_S + 3.0)
                self._write_run({**run, **self._recheck_verdict})

            threading.Thread(target=later, name="yuki-brain-run-row", daemon=True).start()
            return
        self._write_run({**run, **self._recheck_verdict})

    # -- the after-look and the tidy-up turn (P1.2-P1.3) -------------------------------------------------------------

    def tidy_up(self, mode: str) -> None:
        """The Brain's tidy-up turn begins (the next plan): only undoable window steps (``plan.TIDY_ACTIONS``), at most
        :data:`TIDY_STEPS`, each asked first when ``mode`` is ``util.TIDY_ASK``; their words are the run's line."""
        self.tidy = {"mode": str(mode or util.TIDY_DO), "acts": 0, "words": []}

    def after_look(self) -> tuple[str, dict[str, Any]]:
        """What this request's acts did to the desk (module docstring): ``(the "After:" block, the diff's facts)``;
        the block is ``""`` when no flag is up (no turn, no cost). Logged content-free. Never raises."""
        self._purpose_line = ""   # integration review c2desk-2: this look's own, never an earlier plan's
        self._after_step = self._after_state = self._after_front = None
        if self.tidy is not None or self._desk_first is None:
            return "", {}
        try:
            from yuki.perception import diff as desk_diff
            from yuki.perception import situation

            after = self._desk_now()
            if after is None:
                return "", {}
            self._launched_front(after)
            named = {a for a in self._desk_asked | self._desk_touched if isinstance(a, str)}
            # safety WW3 / T-1: a window new since the first act is the acts' only with evidence (a step's own report
            # by id, a launched or acted-in program, an owner among the acts' windows); else the user opened it
            d = desk_diff.compare(self._desk_first, after, acted=tuple(self._desk_asked),
                                  touched=tuple(self._desk_touched), sound_apps=tuple(self._desk_sound | named),
                                  fresh=tuple(self._desk_launched | self._desk_programs))
            facts = d.facts()
            self._emit_log("after_look", ms=round(float(getattr(after, "ms", 0.0) or 0.0), 1), **facts)
            if not d.any_flag:
                return "", facts
            # integration review c2desk-2: no tidy step touches the sound or a tab, so code says it, from the measure
            self._purpose_line = self._purpose_words(d)
            if not self._tidy_can_act(d):
                # integration (desk19 merge note 5): only flags the tidy turn's window steps can't touch (the sound
                # left silent, a tab over what played): no turn, so no planner call is spent on it
                self._emit_log("after_look_skip", why="not_window")
                return "", facts
            self._after_diff = d
            text = desk_diff.render(d, max_chars=AFTER_DIFF_CHARS)
            desk = situation.render(after, purpose="planner", max_chars=AFTER_DESK_CHARS)
            # desk20: the one legal step (diff.tidy_step), from the same windows the tidy gate allows
            # (:meth:`_tidy_ours`) and the front the acts took; tidy21 (decision 1): code takes it itself
            # (:meth:`take_tidy_step`, the Brain's code tidy-up), so the planner's tidy turn is only for no step
            front = d.focus_from if (d.flags or {}).get("focus_taken") else None
            step = desk_diff.tidy_step(d, self._tidy_ours(d), front, len(getattr(after, "monitors", None) or ()))
            self._after_step = step
            self._after_state, self._after_front = after, done.as_hwnd(getattr(after, "foreground", None))
            self._emit_log("after_step", action=(step or {}).get("action"), flag=(step or {}).get("flag"))
            return f"After:\n{text}\n\nThe desk now:\n{desk}", facts
        except Exception as exc:  # noqa: BLE001 - no look: the request ends as before
            self._emit_log("after_look", error=type(exc).__name__)
            return "", {}

    def _launched_front(self, after: Any) -> None:
        """Integration T3 (the talker's rule, ``Hands._scope``, narrowed to a launch): the window in front after the
        acts is the ask, by id, when it is new (not on the desk before the first act) and its program is one this
        request's launches measured (:attr:`_desk_launched`): the app a launch opened, whose window came after the
        launch's own wait or after a splash it reported. Other windows of that program (a browser window playing their
        video) stay theirs. Never raises."""
        try:
            if not self._desk_launched or self._desk_first is None:
                return
            from yuki.perception.situation import _key

            front = done.as_hwnd(getattr(after, "foreground", None))
            if front is None or front in {int(w.hwnd) for w in self._desk_first.windows}:
                return
            w = next((w for w in after.windows if int(w.hwnd) == front), None)
            if w is not None and _key(w.app) in self._desk_launched:
                self._desk_asked.add(front)
                self._desk_touched.add(front)
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass

    def has_tidy_step(self) -> bool:
        """tidy21: whether the latest after-look holds one legal tidy step for code to take (:meth:`take_tidy_step`)."""
        return bool(self._after_step) and self._after_diff is not None and self.tidy is None

    def take_tidy_step(self, mode: str) -> str:
        """tidy21 (the user's decision 1, 19 Oct; final-D S1: the planner's tidy turns took 0 of 3 steps): the Brain's
        tidy-up by code. The after-look's one legal step (``diff.tidy_step``: the front given back to the window the
        acts took it from, or the acts' covering window moved to the other monitor) runs with no planner call, once per
        request, inside the tidy turn's own bounds: :meth:`_tidy_mine` (only what this request's acts changed),
        ``diff.tidy_guard`` (the window in front is still the one the after-look saw; a move never takes a window of
        theirs, nor one whose own place or state a step of this request was asked for: review F1, ``_desk_arranged``),
        the user's hold on the run (safety T-2: a stop, the takeover pause or their added words waiting, as every act
        step's checkpoint; again after an ask), rows 1-2 (a game in front, the "just suggest" kinds) and the window's
        own Act switch, the desk lease,
        the acting marker, and :meth:`_place_act` (``yuki.actions.place``: undoable, journaled in the request's group
        so one "put it back" undoes it, place's own game gate). ``mode`` ``util.TIDY_ASK``: the step in code's words
        as the yes/no question first (:meth:`_tidy_answer`), run on their yes only, and only while it is still the step
        measured again after it (:meth:`_tidy_still`, safety timing 3; rows 1-2 again); ``util.TIDY_OFF``: nothing.

        Returns code's words for after the request's line: the step's own (:data:`LINE_TIDY`, from what it read back)
        when it changed something, the plain line (``diff.LINE_TIDY_FAILED``) when it ran and did not take; after their
        yes, a step left for any reason gets its own plain line (:data:`LINE_TIDY_LEFT`, safety timing 3 recheck: a yes
        is never dropped in silence); else ``""`` (not taken: a guard, a refusal, their no, or nothing changed). Logs
        content-free (``tidy`` with ``by_code``). Never raises."""
        step, d = self._after_step, self._after_diff
        self._after_step = None   # once per request, taken or not
        mode = str(mode or util.TIDY_DO)
        if not step or d is None or self.tidy is not None or self._closed or mode == util.TIDY_OFF:
            return ""
        state, why, said, yes = "left", "", "", False
        call: Call | None = None
        started = time.perf_counter()
        action = "place" if step.get("action") == "place" else "focus_back"

        def held() -> str:
            # safety T-2: the user's own hold on the run, as every act step's checkpoint reads it (a stop, the takeover
            # pause, their added words waiting): code's step never starts over it. Peeks only (the steer stays queued)
            host = self.host
            for code, name in (("cancelled", "cancelled"), ("paused", "paused")):
                test = getattr(host, name, None)
                try:
                    if test() if callable(test) else bool(test):
                        return code
                except Exception:  # noqa: BLE001 - unreadable: the other checks stand
                    continue
            try:
                peek = getattr(host, "steer_pending", None)
                steer = peek() if callable(peek) else None
            except Exception:  # noqa: BLE001
                steer = None
            return "steer" if any(str(w).strip() for w in steer or ()) else ""

        try:
            from yuki.perception import diff as desk_diff

            hwnd = int(step["hwnd"])
            raw = desk_diff.step_app(step, self._after_state)
            call = Call("window_action", {"action": action, **({"monitor": "other", "how": "keep"}
                                                                  if action == "place" else {})},
                        index=0, hwnd=hwnd, app=raw)
            why = desk_diff.tidy_guard(step, d, self._front_since(), self._after_front, self._desk_arranged)
            if not why and not self._tidy_mine(action, hwnd):
                why = "not_ours"
            if not why:
                why = held()
            if not why:
                gate = consent.screen_gate(call, self._live(call))   # rows 1-2: a game in front, "just suggest"
                why = str(gate.why or "refused") if gate is not None else ""
            if not why and raw:
                try:
                    why = "act_off" if util._shared_reader().app_off(raw) else ""
                except Exception:  # noqa: BLE001 - an unreadable policy never blocks (rows 1-2's own rule)
                    why = ""
            if not why and self.lease.held and self.lease.owner != threading.get_ident():
                why = "wrong_thread"
            if not why and not self.lease.take():
                why = "busy"
            if not why and mode == util.TIDY_ASK:
                yes, back = self._tidy_answer(call)
                why = "" if yes and back else "busy" if yes else "declined"   # a yes with the lease gone: busy
            if not why and mode == util.TIDY_ASK:
                why = held()   # safety T-2: they took over while the question waited
            if not why and mode == util.TIDY_ASK:
                # safety timing 3: the step measured again after their yes, as the consent question's own re-look (the
                # desk read again; rows 1-2 again): one no longer valid is left
                why = self._tidy_still(step)
                if not why:
                    gate = consent.screen_gate(call, self._live(call))
                    why = str(gate.why or "refused") if gate is not None else ""
            if not why:
                if not self.acting:
                    try:
                        self.host.acting_begin()
                        self.acting = True
                    except Exception:  # noqa: BLE001 - the marker never breaks a run
                        pass
                self._emit(ToolCall(call.tool, {**call.input, "_about": self._about(call)}))
                t0, wall = time.perf_counter(), time.time()
                with self._dispatch_lock:
                    outcome = self._place_act(call)
                ms = round((time.perf_counter() - t0) * 1000, 1)
                self._emit(ToolResult(call.tool, bool(outcome.ok), str(outcome.summary or "")[:300],
                                      payload=outcome.payload, about=self._about(call)))
                payload = outcome.payload if isinstance(outcome.payload, dict) else {}
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                self._trace_step(call, outcome, ok=bool(outcome.ok), verdict=bool(outcome.ok), why="tidy_by_code",
                                 ms=ms, started=wall)
                refused = _refused_kind(payload)
                if outcome.ok:
                    state = "taken"
                    if details.get("changed"):   # review L1: words only for what its own read-back saw change
                        self._tidy_changed += 1
                        said = self._tidy_line(call, details, LINE_TIDY)
                    else:
                        why = "unchanged"
                elif refused:
                    # a game or another gate: nothing reached it, nothing is said (after their yes: the line below)
                    why = f"refused_{refused}"
                else:
                    state = "failed"
                    said = desk_diff.tidy_line(step, util.app_name(raw) if raw else "", ok=False)
        except Exception as exc:  # noqa: BLE001 - a step that broke is a step not taken; the request's line stands
            why = why or type(exc).__name__
        if yes and state == "left" and not said and call is not None:
            # safety timing 3 (recheck): their yes, and code then left the step (measured again it is no longer the
            # step, their hold, a gate, a refusal): one plain line, so the yes is never dropped in silence
            try:
                said = self._tidy_line(call, None, LINE_TIDY_LEFT)
            except Exception:  # noqa: BLE001 - no words: the request's line stands
                said = ""
        self._emit_log("tidy", verb="window_action", action=action, state=state, by_code=True, mode=mode,
                       why=why or None, said=bool(said), ms=round((time.perf_counter() - started) * 1000, 1))
        return said

    def _tidy_still(self, step: Mapping[str, Any]) -> str:
        """Safety timing 3: after their yes to code's tidy question, the step measured again before it runs: the desk
        read again (:meth:`_desk_now`) and compared with the request's first before, with the acts' same scope (as
        :meth:`after_look`), must give the very same step (its action and window; a place: that window still on the
        monitor it was on at the after-look, so "other" is the screen the question meant), and ``diff.tidy_guard`` on
        that fresh measure must pass with a fresh front. A window they moved, raised, covered or uncovered themselves
        while asked leaves the step. Recheck: their yes may come typed in Yuki's own bar or chat page, which is then in
        front, so this measure (the desk and the fresh front) is taken as if Yuki's own windows were not there
        (:meth:`_own_aside` with :attr:`_own_beneath`: the window right under them stands for them, never the origin):
        Yuki's own window in front, by itself, never leaves the step. ``""``: still the step; else why not (``stale``,
        ``unread`` or the guard's word). Never raises (``stale``)."""
        try:
            from yuki.perception import diff as desk_diff

            self._own_beneath = True
            try:
                now = self._desk_now()
            finally:
                self._own_beneath = False
            if now is None:
                return "unread"
            named = {a for a in self._desk_asked | self._desk_touched if isinstance(a, str)}
            d = desk_diff.compare(self._desk_first, now, acted=tuple(self._desk_asked),
                                  touched=tuple(self._desk_touched), sound_apps=tuple(self._desk_sound | named),
                                  fresh=tuple(self._desk_launched | self._desk_programs))
            front = d.focus_from if (d.flags or {}).get("focus_taken") else None
            again = desk_diff.tidy_step(d, self._tidy_ours(d), front, len(getattr(now, "monitors", None) or ()))
            hwnd = done.as_hwnd(step.get("hwnd"))
            if not again or again.get("action") != step.get("action") or done.as_hwnd(again.get("hwnd")) != hwnd:
                return "stale"

            def monitor(state: Any) -> Any:
                return next((w.monitor for w in getattr(state, "windows", None) or () if int(w.hwnd) == hwnd), None)

            if step.get("action") == "place" and monitor(now) != monitor(self._after_state):
                return "stale"
            return desk_diff.tidy_guard(again, d, self._front_since(now),
                                        done.as_hwnd(getattr(now, "foreground", None)), self._desk_arranged)
        except Exception:  # noqa: BLE001 - not measured again: left
            return "stale"

    def _front_since(self, measured: Any = None) -> int | None:
        """tidy21: the window in front now, as the after-look reads it (:meth:`_own_aside`: Yuki's own window stands for
        the one the user called it from), for :meth:`take_tidy_step`'s guard; ``None`` when it can't be read.
        ``measured``: the desk :meth:`_tidy_still` measured again as if Yuki's own windows were not there (safety timing
        3 recheck): Yuki's own window in front then stands for that measure's front (the window right under it)."""
        front = _foreground_now()
        if not front:
            return None
        if _is_own_window(front):
            if measured is not None:
                return done.as_hwnd(getattr(measured, "foreground", None))
            return done.as_hwnd(getattr(self.host, "origin_hwnd", None))
        return int(front)

    @staticmethod
    def _purpose_words(d: Any) -> str:
        """Integration review c2desk-2: code's measured words for what the request's acts left short of its purpose
        (:data:`LINE_STILL_LEVEL` and the rest): the first control turned toward sound that is still silent (its own
        level or mute, else the PC's), and a tab of the act's over what played. ``""`` for neither. Never raises."""
        try:
            out: list[str] = []
            for v in getattr(d, "volume_changes", None) or ():
                why = list(v.get("silent") or ())
                if not why:
                    continue
                if "muted" in why:
                    out.append(LINE_STILL_MUTED)
                elif "level" in why:
                    out.append(LINE_STILL_LEVEL.format(level=round(float(v.get("level_to") or 0.0))))
                elif "master" in why:
                    out.append(LINE_PC_SILENT["muted"] if v.get("master_muted") else LINE_PC_SILENT["level"].format(
                        level=round(float(v.get("master_level") or 0.0))))
                break
            if any(c.get("kind") == "tab" and c.get("side_effect") for c in getattr(d, "covered", None) or ()):
                # desk20 (final-D S3): the measured sentence (what plays on, by its title), shared with the voice's
                # hands and ONE (``diff.cover_line``); the plain line when it can't be built
                from yuki.perception.diff import cover_line

                out.append(cover_line(d) or LINE_BEHIND_TAB)
            return " ".join(out)
        except Exception:  # noqa: BLE001 - no words: the request's line stands
            return ""

    @staticmethod
    def _tidy_can_act(d: Any) -> bool:
        """Whether a flag is up that the tidy turn's undoable window steps (``plan.TIDY_ACTIONS``) could act on: the
        front taken, a pop-up left, or a window of theirs covered. A tab over what played (a tab switch is no tidy
        step) and the sound left silent (no volume step) are not."""
        flags = getattr(d, "flags", None) or {}
        if flags.get("focus_taken") or flags.get("new_popup"):
            return True
        return any(c.get("attention") and not c.get("asked") and c.get("side_effect") and c.get("kind") != "tab"
                   for c in getattr(d, "covered", None) or ())

    def _desk_now(self) -> Any:
        """The desk now (``situation.snapshot``: windows, visible shares, the keyboard, media, the mixer; the holds are
        not needed for a diff), with Yuki's own windows standing aside for :meth:`_own_aside`. ``None`` when it can't
        be read."""
        try:
            from yuki.perception import situation

            return self._own_aside(situation.snapshot(holds=""))
        except Exception:  # noqa: BLE001
            return None

    def _own_aside(self, state: Any) -> Any:
        """Yuki's own windows (the ask bar, the chat page) are never what the user had: in front or with the keyboard,
        the window the user called Yuki up from (``host.origin_hwnd``, when listed) stands for them.

        Safety timing 3 (recheck), while :attr:`_own_beneath` (:meth:`_tidy_still`'s measure after their yes, which may
        have been typed in Yuki's own window, so that window came up over whatever was in front then, not over the
        origin): the desk is measured as if Yuki's own windows were not there: they leave the read, and in front or
        with the keyboard the window right under them stands for them (the top of the rest by Z order, not minimised;
        ``None`` when there is none)."""
        own = {int(w.hwnd) for w in state.windows if _is_own_window(w.hwnd)}
        if not own:
            return state
        if getattr(self, "_own_beneath", False):
            rest = [w for w in state.windows if int(w.hwnd) not in own]
            under = min((w for w in rest if not w.minimized and w.kind == "window"), key=lambda w: w.z, default=None)
            stand_in = int(under.hwnd) if under is not None else None
            state.windows = rest
        else:
            origin = done.as_hwnd(getattr(self.host, "origin_hwnd", None))
            listed = {int(w.hwnd) for w in state.windows}
            stand_in = origin if origin is not None and origin in listed and origin not in own else None
        if int(state.foreground or 0) in own:
            state.foreground = stand_in
        if int(state.keyboard or 0) in own:
            state.keyboard = stand_in
        for w in state.windows:
            if int(w.hwnd) in own:
                w.foreground = w.keyboard = False
            elif stand_in is not None and int(w.hwnd) == stand_in:
                w.foreground = w.foreground or state.foreground == stand_in
                w.keyboard = w.keyboard or state.keyboard == stand_in
        return state

    def _desk_note(self, call: Call, outcome: Any) -> None:
        """What one act aimed at and brought up, for the after-look: the step's window, the window its report names
        (a launch, a skill run's chat), else a skill run's app by its connection data; a window step's own target is
        what the request was about. Never raises."""
        try:
            payload = outcome.payload if isinstance(getattr(outcome, "payload", None), dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            hwnds = {h for h in (done.as_hwnd(call.hwnd), done.as_hwnd(details.get("hwnd"))) if h}
            self._desk_touched |= hwnds
            if call.tool == "run_skill":
                key, slots = consent.skill_input(call, self.head)
                # integration review F3 (contract C1): the windows the engine measured its rungs bringing forward are
                # the act's by id (Spotify raised by its library rung is never "not from your act"); the apps its
                # rungs acted in are the act's for their sound only (c2desk-1: a volume run touched no window)
                measured = {h for h in (done.as_hwnd(w) for w in payload.get("acted_windows") or ()) if h}
                self._desk_touched |= measured
                self._desk_sound |= {words(a) for a in payload.get("acted_apps") or () if words(a)}
                from yuki.perception.situation import _key

                # safety WW3 / T-1: a new window of a program its rungs acted in is the run's (``diff`` ``fresh``)
                self._desk_programs |= {_key(a) for a in payload.get("acted_apps") or ()
                                        if isinstance(a, str) and _key(a)}
                if not hwnds:
                    self._desk_touched |= set(self._skill_programs(payload, key, slots))
            elif call.tool == "app_volume" and words(call.input.get("app")):
                # integration review c2desk-1 (desk19 merge note 7): the app whose mixer row this act set is the act's
                # for left_silent only; its windows stay out of the act's window scope
                self._desk_sound.add(words(call.input.get("app")))
            elif call.tool in ASKED_VERBS:
                self._desk_asked |= hwnds
                if call.tool == "launch_app":
                    # integration T3: the launch's measured programs (its process, its identity's image names), never
                    # the query's words (a misheard name, "the scored", names no window); after_look takes a new
                    # window of one of them in front as the ask, by id
                    from yuki.perception.situation import _key

                    identity = details.get("identity") if isinstance(details.get("identity"), dict) else {}
                    launched = {_key(n) for n in (details.get("process_name"), *(identity.get("image_names") or ()))
                                if isinstance(n, str) and _key(n)}
                    self._desk_launched |= launched
                    if not hwnds and not launched and words(details.get("name")):
                        self._desk_asked.add(words(details.get("name")))   # the resolved Start entry's name
            # tidy21 review F1: a step whose ask is its window's own place or state (by our verb names, never words)
            if call.tool == "window_action":
                from yuki.perception.diff import arranges

                if arranges(call.input.get("action")):
                    self._desk_arranged |= {int(h) for h in hwnds}
            elif call.tool == "focus_window":
                # safety WW6: the window they asked to come up is where they asked for it (as a window focus)
                self._desk_arranged |= {int(h) for h in hwnds}
            elif call.tool == "run_skill" and words(call.input.get("place")):
                self._desk_arranged |= {int(h) for h in hwnds}
                self._desk_arranged |= {h for h in (done.as_hwnd(w) for w in payload.get("acted_windows") or ()) if h}
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass

    def _tidy_gate(self, index: int, call: Call) -> RunResult | None:
        """The tidy-up turn's bounds: an undoable window step (``plan.TIDY_ACTIONS``) on a window :meth:`_tidy_mine`
        allows, at most :data:`TIDY_STEPS` of them. Anything else ends the turn there (the request's own line
        stands)."""
        from yuki.brain.plan import TIDY_ACTIONS

        tidy = self.tidy or {}
        action = str(call.input.get("action") or "") if call.tool == "window_action" else ""
        why = ("tidy_only" if action not in TIDY_ACTIONS else "tidy_steps" if int(tidy.get("acts", 0)) >= TIDY_STEPS
               else "" if self._tidy_mine(action, call.hwnd) else "tidy_not_ours")
        if not why:
            return None
        status = self.statuses[index]
        status.state, status.why = "refused", why
        self._emit_log("tidy", verb=call.tool, state="refused", why=why)
        return RunResult("refused", line="", final=True)

    def _tidy_mine(self, action: str, hwnd: Any) -> bool:
        """Review M1 (P1.5: a tidy-up touches only what Yuki's own act changed), from the after-look's diff: a place
        only on a window this request's acts brought up (a step's window or its report's, a new window, the act's
        window over one of theirs, the front its acts took); a focus back only to the window that was in front, or
        one that held their attention (playing, the keyboard), before the acts."""
        target = done.as_hwnd(hwnd)
        d = self._after_diff
        if target is None or d is None:
            return False
        if action == "focus_back":
            return target == done.as_hwnd(d.focus_from) or target in {done.as_hwnd(h) for h in d.attention}
        return target in self._tidy_ours(d)

    def _tidy_ours(self, d: Any) -> set[int]:
        """The windows a tidy-up's place may move (:meth:`_tidy_mine`): the ones this request's acts brought up (a
        step's window or its report's), new windows measurably the acts' (``by_act``, safety WW3: never one the user
        opened while the plan ran), the act's window over one of theirs, the front its acts took."""
        ours = {h for h in self._desk_touched if isinstance(h, int) and not isinstance(h, bool)}
        ours |= {done.as_hwnd(n.get("hwnd")) for n in d.new_windows if n.get("by_act")}
        # integration T1: only a covering window that is itself the act's (new, touched or asked: ``by_mine``); a
        # window the user brought over a window a step touched stays theirs
        ours |= {done.as_hwnd(c.get("by")) for c in d.covered if c.get("by_mine")}
        if d.focus_by_act:
            ours.add(done.as_hwnd(d.focus_to))
        ours.discard(None)
        return ours

    @staticmethod
    def _tidy_line(call: Call, details: Mapping[str, Any] | None, table: Mapping[str, str]) -> str:
        """Code's words for a tidy-up step (:data:`LINE_TIDY` or :data:`LINE_TIDY_ASK`): the app as Windows names it
        (:data:`TIDY_NO_APP` when none), and for a place where it went (another monitor, by what the step read back
        when it ran, else as asked; a half of the same one)."""
        action = str(call.input.get("action") or "")
        details = details or {}
        raw = str(details.get("app") or call.app or "").strip()
        app = util.app_name(raw) if raw else TIDY_NO_APP
        if action != "place":
            return table.get(action, "").format(app=app)
        before = details.get("from_monitor")
        after = details.get("to_monitor_seen", details.get("to_monitor"))
        asked = str(call.input.get("monitor") or "other")
        other = before != after if before is not None and after is not None else not asked.startswith("with:")
        where = TIDY_WHERE["other"] if other else TIDY_WHERE.get(str(call.input.get("how") or "keep"), "")
        return table["place"].format(app=app, where=where) if where else table["place_here"].format(app=app)

    def _tidy_ask(self, call: Call) -> bool:
        """The tidy dial says ask: the step in code's words as a yes/no question (the desk lease freed while it
        waits); ``True`` on their yes with the lease taken back (:meth:`_tidy_answer`)."""
        yes, back = self._tidy_answer(call)
        return back and yes

    def _tidy_answer(self, call: Call) -> tuple[bool, bool]:
        """:meth:`_tidy_ask`'s question, as ``(their yes, the desk lease taken back)``: code's own step tells a yes it
        then can't act on from a no (safety timing 3 recheck: such a yes gets :data:`LINE_TIDY_LEFT`, never silence)."""
        question = self._tidy_line(call, None, LINE_TIDY_ASK)
        if not question:
            return False, False
        self.lease.release()
        try:
            yes = bool(self.host.ask(question))
        except Exception:  # noqa: BLE001 - no answer is a no
            yes = False
        return yes, bool(self.lease.take())

    def _place_act(self, call: Call) -> Any:
        """A place, focus back or put back step (``yuki.actions.place``), as the Dispatcher's outcome shape. The
        group is the request, so one "put it back" undoes the whole tidy-up. ``details["changed"]``: whether it changed
        the desk (review L1: a place that found the window already there, a focus back to the window already in
        front, change nothing and get no words)."""
        from yuki.actions import place

        action = str(call.input.get("action") or "")
        group = getattr(self.host, "request_id", None) or self.plan_id
        if action == "place":
            result = place.place_window(int(call.hwnd or 0), monitor=call.input.get("monitor") or "other",
                                        how=str(call.input.get("how") or "keep"), activate=False, group=group)
            changed = bool(result.ok) and "to_monitor_seen" in (result.details or {})   # read back after a move
        elif action == "focus_back":
            front = _foreground_now()
            result = place.focus_back(int(call.hwnd or 0), group=group)
            changed = bool(result.ok) and front != int(call.hwnd or 0)
        else:
            result = place.undo_last()
            changed = bool(result.ok)
        if isinstance(getattr(result, "details", None), dict):
            result.details["changed"] = changed
        wrap = getattr(self.dispatcher, "_from_action", None)
        if callable(wrap):
            return wrap("window_action", result)
        from yuki.agent.tools import ToolOutcome

        details = dict(getattr(result, "details", None) or {})
        return ToolOutcome(name="window_action", ok=bool(result.ok), summary=str(result.summary or ""),
                           content=[{"type": "text", "text": str(result.summary or "")}],
                           payload={"ok": bool(result.ok), "summary": str(result.summary or ""), "details": details},
                           elapsed_ms=float(getattr(result, "elapsed_ms", 0.0) or 0.0))

    # -- budgets and status ------------------------------------------------------------------------------------------

    @property
    def max_replans(self) -> int:
        return int(getattr(self.settings, "brain_max_replans", 0) or MAX_REPLANS)

    def over_budget(self, model_calls: int, *, answer: bool = False) -> bool:
        """Whether the planner may not be asked again: an act run past its replans or model calls, an answer plan past
        its calls or seconds (§2.3)."""
        if answer:
            return model_calls >= ANSWER_MODEL_CALLS or time.monotonic() - self.started_mono >= ANSWER_SECONDS
        return self.replans >= self.max_replans or model_calls >= ACT_MODEL_CALLS

    def status_line(self) -> str | None:
        """The running plan in a few words, for "how's it going?" (``bridge.work_line``): the step running or the
        last one done, and how many are left. ``None`` before a plan."""
        if not self.statuses:
            return None
        done_n = sum(1 for s in self.statuses if s.state == "ok")
        total = len(self.statuses)
        current = next((s for s in self.statuses if s.state == "pending"), None)
        goal = words(field_of(self.head, "goal", ""))
        if current is None:
            return f"{goal}: all {total} steps done, checking it." if goal else f"All {total} steps done, checking it."
        step = self.steps[current.index] if current.index < len(self.steps) else {}
        what = _mid(self._step_said(step))   # mid-sentence: "step 2 of 3, opening a page in Arc"
        lead = f"{goal}: " if goal else ""
        return f"{lead}step {current.index + 1} of {total}" + (f", {what}" if what else "") + \
            (f" ({done_n} done)" if done_n else "")

    def _reads_now(self) -> set[str]:
        """What this request has read (M9): this executor's own reads and the planner's (:meth:`note_read`), and
        ``private`` when the host says the request already holds the user's private content read elsewhere
        (``host.private_read()``, bCore's)."""
        out = set(self.reads_seen)
        private = getattr(self.host, "private_read", None)
        if callable(private):
            try:
                if private():
                    out.add("private")
            except Exception:  # noqa: BLE001 - this executor's own reads still count
                pass
        return out

    def note_read(self, kind: str) -> None:
        """The planner's own tools read a file, the clipboard or memory this request (``file`` / ``clipboard`` /
        ``memory``): a later web fetch is then a may-commit step (M9), as for a plan's own reads."""
        if kind in ("file", "clipboard", "memory"):
            self.reads_seen.add(kind)

    def add_words(self, more: Sequence[str]) -> None:
        """The user's own words that arrived outside a step checkpoint (an answer to the planner's own question): they
        count as theirs for the consent rule's spans and the commit check."""
        self.words += [str(w) for w in more or [] if str(w).strip()]

    @property
    def last_failed_step(self) -> Mapping[str, Any] | None:
        """The step that just failed, as the plan wrote it (bCore's validator rejects a replan repeating it unchanged
        once)."""
        return (self.last_failed or {}).get("step")

    # -- steps -------------------------------------------------------------------------------------------------------

    @staticmethod
    def _verb(step: Any) -> str:
        return str(field_of(step, "verb", "") or "")

    def _stop_if(self, result: RunResult | None) -> RunResult | None:
        if result is not None:
            self.stopped = result
        return result

    def _run_rest(self) -> RunResult | None:
        """After the block completed: the commit check over the may-commit steps still to run, then each pending step
        in order. ``None`` when every step went as planned."""
        self._join_reads()
        pending = [i for i, s in enumerate(self.statuses) if s.state == "pending"]
        self._batch_check(pending)
        for i in pending:
            step = self.steps[i]
            verb = self._verb(step)
            if verb in READ_VERBS and not self._fetch_commits(step):
                self._start_read(i, step)
                continue
            self._join_reads()
            if verb == "wait":
                result = self._wait(i, step)
            else:
                result = self._run_act(i, step)
            if result is not None:
                return result
        return None

    def _batch_check(self, pending: Sequence[int]) -> None:
        """The commit check, once, over every may-commit step of ``pending`` (their windows as the overview shows them
        now, their elements as the plan names them; at run time a step whose window or element turns out different is
        checked again on its own)."""
        payload = done.overview(self.dispatcher, lock=self._dispatch_lock)
        self._last_overview = payload or self._last_overview
        calls: list[Call] = []
        typed_by_step: dict[int, str] = {}   # each may-commit step: Yuki's unsent text in its window at its moment
        typed: dict[int, str] = dict(self.typed_pending)
        reads = self._reads_now()
        for i in pending:
            step = self.steps[i]
            verb = self._verb(step)
            kind = self._read_kind(step)
            if verb in READ_VERBS and (verb != "fetch" or kind):
                if kind:
                    reads.add(kind)
                continue
            if verb == "wait":
                continue
            call = self._predicted(i, step, payload)
            if call is None:
                continue
            live = Live(typed_pending=frozenset(typed), reads=frozenset(reads), join_controls=self.join_controls)
            if consent.may_commit(call, live, self.head):
                calls.append(call)
                if call.hwnd and int(call.hwnd) in typed:
                    typed_by_step[i] = typed[int(call.hwnd)]
            # the same rule as after a step runs (_after_act); a run_skill has no window before it runs, so its draft is
            # not foreseen here: a step whose waiting text differs at run time is checked again on its own
            self._typed_step(typed, call)
        if calls:
            # one call; each step's line says whether Yuki's unsent text waits in its window at its own moment
            self._batch_words = len(self.words)
            self.batch = self.checker.run(self._all_words(), calls, earlier=self.earlier, plan=self.head,
                                          typed_by_step=typed_by_step)
            self._batch_typed = {c.index: c.index in typed_by_step for c in calls}

    def _gate_fetches(self, indices: Sequence[int]) -> RunResult | None:
        """An answer plan's web fetches that come after a file, clipboard or memory read (M9): one commit check over
        them, then the consent rule for each (row 5: the check's ``none`` or ``asked`` allows, else ask; a failed
        check asks; an undeclared send is refused). An allowed or confirmed fetch runs as a read (fenced). A fetch
        takes no input, so no desk lease and no acting marker; the lease, if an earlier act holds it, is freed while
        the user is asked. ``None`` when every gated fetch ran."""
        calls: dict[int, Call | None] = {i: self._fetch_call(i, self.steps[i]) for i in indices}
        live0 = Live(reads=frozenset(self._reads_now()), join_controls=self.join_controls)
        todo = [c for c in calls.values() if c is not None and consent.may_commit(c, live0, self.head)]
        if todo:
            self._batch_words = len(self.words)
            self.batch.update(self.checker.run(self._all_words(), todo, earlier=self.earlier, plan=self.head))
        for i in indices:
            call, status = calls[i], self.statuses[i]
            if call is None:
                status.state, status.why = "failed", "a fetch needs an address or a path"
                continue
            live = self._live(call)
            verdict = consent.check(call, self.head, self._all_words(), live, self._verdict_for(call, live))
            self._emit_log("consent", verb=call.tool, row=verdict.row, result=verdict.result, why=verdict.why)
            if verdict.result == consent.ASK:
                self.lease.release()
                try:
                    yes = bool(self.host.ask(verdict.question))
                except Exception:  # noqa: BLE001 - no answer is a no
                    yes = False
                if not yes:
                    status.state, status.why = "declined", "the user said no"
                    self._trace_step(call, None, ok=False, verdict=False, why="declined", kind="ask")
                    return RunResult("declined", line=LINE_DECLINED, final=True)
                verdict = consent.after_ask(verdict)
            if verdict.result != consent.ALLOW:   # a refusal (a draft can't come of a fetch: it is refused too)
                status.state, status.why = "refused", verdict.why
                self._trace_step(call, None, ok=False, verdict=False, why=f"refused: {verdict.why}", kind="control",
                                 refused=True)
                if verdict.final:
                    return RunResult("refused", line=verdict.line, final=True)
                return self._mismatch_result(i, verdict.why, refused=True, seen=verdict.line or verdict.why)
            self._start_read(i, self.steps[i])
        return None

    def _predicted(self, index: int, step: Any, payload: Mapping[str, Any]) -> Call | None:
        """A step's call as planned, before it runs (its window from the overview, its element as the plan names it)."""
        verb = self._verb(step)
        if verb == "fetch":
            return self._fetch_call(index, step)
        try:
            tool_input = self._tool_input(verb, step)
        except ValueError:
            return None
        hwnd, entry = done.window_of(self.dispatcher, field_of(step, "window", None), payload=payload) \
            if field_of(step, "window", None) else (None, {})
        if verb == "open_url":   # the browser it will open in, as it will run (x9)
            hwnd, entry, _image = self._open_url_browser(tool_input, field_of(step, "window", None), hwnd, entry,
                                                         payload, log=False)
            entry = entry or ({"process_name": words(tool_input.get("app"))} if tool_input.get("app") else {})
        elif hwnd is None and not field_of(step, "window", None) and \
                verb in ELEMENT_VERBS | {"hotkey", "press", "window_action", "tab", "focus_window"}:
            hwnd = self._front(payload, log=False)
            entry = next((w for w in payload.get("windows") or [] if isinstance(w, dict)
                          and done.as_hwnd(w.get("hwnd")) == hwnd), {}) if hwnd else {}
        element = field_of(step, "element", None)
        return Call(tool=verb, input=tool_input, index=index, hwnd=hwnd, app=str(entry.get("process_name") or ""),
                    title=str(entry.get("title") or ""), role=str(field_of(element, "role", "") or ""),
                    name=str(field_of(element, "name", "") or ""))

    # -- one act step ------------------------------------------------------------------------------------------------

    def _checkpoint(self) -> RunResult | None:
        """Between steps: a cancel ends the run; the user's added words stop it for a replan; a pause holds it."""
        if self.host.cancelled():
            return RunResult("cancelled")
        held = self._hold_pause()
        if held is not None:
            return held
        try:
            steer = self.host.steer_pending()
        except Exception:  # noqa: BLE001
            steer = None
        steer = [str(w) for w in (steer or []) if str(w).strip()]
        if steer:
            self.steers += steer
            self.words += steer
            return RunResult("steer", words=steer)
        return None

    def _hold_pause(self) -> RunResult | None:
        """The user took over: no new step starts until they resume (or stop, or the limit passes: the run ends
        quietly). Says :class:`Paused` ``paused`` / ``resumed`` / ``ended``."""
        try:
            if not self.host.paused():
                return None
        except Exception:  # noqa: BLE001
            return None
        hosted = getattr(self.host, "hold_paused", None)
        if callable(hosted):
            # the host's own hold (bCore's Brain: its Paused events, the surfaces' callback, the takeover watch off
            # and back on, the pause limit, waiting on its wake event): one hold, never two
            try:
                goes_on = bool(hosted())
            except Exception:  # noqa: BLE001 - no hold ran: nothing more starts
                goes_on = False
            if goes_on:
                self._read_cache.clear()   # the screen is read again before the next step
                return None
            return RunResult("cancelled")   # a stop, or the limit (the host cancelled the request with its line)
        since = time.monotonic()
        limit = float(getattr(self.settings, "takeover_idle_stop_s", 0) or PAUSE_LIMIT_S)
        wake = getattr(self.host, "pause_wake", None)
        self._emit(Paused("paused"))
        while True:
            if self.host.cancelled():
                return RunResult("cancelled")
            if not self.host.paused():
                self._emit(Paused("resumed", seconds=round(time.monotonic() - since, 1)))
                self._read_cache.clear()   # the screen is read again before the next step
                return None
            left = limit - (time.monotonic() - since)
            if left <= 0:
                self._emit(Paused("ended", seconds=round(time.monotonic() - since, 1)))
                return RunResult("stopped", line=LINE_PAUSE_STOP, final=True)
            if isinstance(wake, threading.Event):
                wake.wait(min(left, 1.0))
                wake.clear()
            else:
                threading.Event().wait(min(left, 0.25))

    def _run_act(self, index: int, step: Any, *, call: Call | None = None) -> RunResult | None:
        """One act step, start to check (module docstring). ``None`` when it went as planned."""
        status = self.statuses[index]
        stop = self._checkpoint()
        if stop is not None:
            return stop
        if self.tidy is None:
            # desk20 (S20): what a stuck ending names is the latest act step only (kept across a plan that ran none)
            self._stuck_said = None
        if call is None:
            call, why = self._resolve(index, step)
            if call is None:
                return self._mismatch_result(index, why)
        if not self.lease.held:
            if not self.lease.take():
                status.state, status.why = "skipped", "the desk lease was busy"
                return RunResult("busy", line=LINE_BUSY, final=True)
        elif self.lease.owner != threading.get_ident():
            status.state, status.why = "skipped", "wrong thread"
            return RunResult("refused", line=LINE_BUSY, final=True)
        if self.tidy is not None:
            bounded = self._tidy_gate(index, call)
            if bounded is not None:
                return bounded
        live = self._live(call)
        refused = self._who_guard(index, call, live)
        if refused is not None:
            return refused
        cv = self._verdict_for(call, live)
        effect = str(getattr(cv, "effect", "") or "") if cv is not None and not getattr(cv, "failed", False) else ""
        verdict = consent.check(call, self.head, self._all_words(), live, cv)
        self._emit_log("consent", verb=call.tool, row=verdict.row, result=verdict.result, why=verdict.why)
        said_yes = False
        # Item 11's L2 (review r1 L1, L2): a send's person is looked up before any question or countdown (a Stop
        # during the lookup is seen by the countdown and the checks after it), and the same message to the same person
        # goes once per request: a later step that would send it again is skipped (never asked, never run)
        person_id: int | None = None
        send_key: tuple[Any, str] | None = None
        if call.tool == "run_skill" and verdict.row == 4 and verdict.result in (consent.ALLOW, consent.ASK) \
                and self._sends_for_sure(call):
            person_id, send_key = self._send_person(call)
            if send_key in self._sent_keys:
                status.state, status.why = "skipped", "already_sent"
                self._trace_step(call, None, ok=False, verdict=None, why="already sent in this request",
                                 kind="control")
                self._emit_log("consent", verb=call.tool, row=4, result="skip", why="duplicate_send")
                return None
        if verdict.result == consent.ASK:
            self.lease.release()
            try:
                yes = bool(self.host.ask(verdict.question))
            except Exception:  # noqa: BLE001 - no answer is a no
                yes = False
            if not self.lease.take():
                return RunResult("busy", line=LINE_BUSY, final=True)
            if not yes:
                status.state, status.why = "declined", "the user said no"
                self._trace_step(call, None, ok=False, verdict=False, why="declined", kind="ask")
                return RunResult("declined", line=LINE_DECLINED, final=True)
            self._read_cache.pop(int(call.hwnd or 0), None)
            again, why = self._resolve(index, step)       # one look at the window again
            if again is None:
                return self._mismatch_result(index, why)
            if again.hwnd != call.hwnd or again.role != call.role or again.name != call.name:
                return self._mismatch_result(index, "the window changed while you were asked")
            # rows 1-2 again: a game may have come to the front, or a switch changed, while the user was asked
            gate = consent.screen_gate(again, self._live(again))
            call, verdict = again, (gate if gate is not None else consent.after_ask(verdict))
            said_yes = gate is None
        if verdict.result == consent.REFUSE:
            status.state, status.why = "refused", verdict.why
            self._trace_step(call, None, ok=False, verdict=False, why=f"refused: {verdict.why}", kind="control",
                             refused=True)
            if verdict.focus:
                self._bring_forward(verdict.focus)
            if verdict.final:
                return RunResult("refused", line=verdict.line, final=True)
            return self._mismatch_result(index, verdict.why, refused=True, seen=verdict.line)
        if verdict.result == consent.DRAFT:
            return self._run_draft(index, step, call, verdict)
        if self.tidy is not None and self.tidy.get("mode") == util.TIDY_ASK and not self._tidy_ask(call):
            status.state, status.why = "declined", "tidy_declined"
            return RunResult("declined", line="", final=True)
        if call.tool == "run_skill" and send_key is None and self._grant_sends(call, verdict):
            person_id, send_key = self._send_person(call)    # an allowed send no question came before
        self._counted = False
        if verdict.countdown:
            went = self._countdown(call)
            if went == "stopped":
                status.state, status.why = "skipped", "stopped in the countdown"
                self._trace_step(call, None, ok=False, verdict=False, why="send countdown", kind="control")
                return RunResult("stopped", line=LINE_STOPPED_SEND, final=True)
            if went == "swapped":
                status.state, status.why = "skipped", "swapped in the countdown"
                return RunResult("steer", words=list(self._swap_words))
            # the countdown ran: a takeover, a cancel, a game or a switch since then stops the send here
            held = self._hold_pause()
            if held is not None or self.host.cancelled():
                status.state, status.why = "skipped", "stopped after the countdown"
                self._trace_step(call, None, ok=False, verdict=False, why="send countdown", kind="control")
                return held if held is not None and held.status == "stopped" else RunResult(
                    "stopped", line=LINE_STOPPED_SEND, final=True)
            gate = consent.screen_gate(call, self._live(call))
            if gate is not None:
                status.state, status.why = "refused", gate.why
                self._trace_step(call, None, ok=False, verdict=False, why=f"refused: {gate.why}", kind="control",
                                 refused=True)
                return RunResult("refused", line=gate.line, final=True)
        grant = None
        if call.tool == "run_skill":
            # review r1 L3: Yuki's own wording sends only after a countdown the user could stop, or their yes
            grant = self._skill_grant(call, verdict, person_id=person_id, counted=self._counted or said_yes)
        result = self._dispatch_act(index, step, call, effect=effect, grant=grant)
        if grant is not None and grant.get("send") is True and send_key is not None and self._may_have_sent(index):
            # review r2 M1: counted as sent only once the step's own result says it may have gone; a run that asked,
            # handed over, was refused or saw nothing go (a retry after "WhatsApp instead?" -> yes) never counts
            self._sent_keys.add(send_key)
        return result

    def _skill_grant(self, call: Call, verdict: consent.Verdict | None, *, person_id: int | None = None,
                     counted: bool = False) -> dict[str, Any]:
        """What the consent rule allowed this ``run_skill`` step (zFinal, bSkill finding 1: the desk hands read it for
        that one step): a send (row 4 allowed it and its countdown ran), a join on the user's plain words (row 3,
        decision 7), and the step's own text and person slots (code facts: the capability's checks name its slots)."""
        key, slots = consent.skill_input(call, self.head)
        sk = util.skill(key)
        tname = consent.text_slot(sk) if sk is not None else ""
        pname = consent.person_slot(sk) if sk is not None else ""
        allowed = verdict is not None and verdict.allowed
        send = bool(allowed and self._grant_sends(call, verdict))
        join = bool(allowed and verdict.row == 3 and verdict.why == "join_words")
        text, person = (words(slots.get(tname)) if tname else ""), (words(slots.get(pname)) if pname else "")
        grant: dict[str, Any] = {"skill": key, "text": text, "person": person, "send": send, "join": join}
        if send:
            # Item 11's L2: the skill run this step makes holds the grant to this very step (a one-step token, the
            # request, this thread) and to whom by identity: the one person the person slot names in memory's people
            # index, the same one the plan's recipient names (the countdown showed it), looked up before the
            # countdown (:meth:`_send_person`). ``counted``: a countdown the user could stop ran, or they said yes;
            # only then may Yuki's own wording go (review r1 L3, ``uia_route.send_grant``). The resolver and the chat
            # driver's send check then take exactly this text to exactly this person
            grant.update(text_slot=tname, person_slot=pname, step=int(call.index),
                         request_id=getattr(self.host, "request_id", None), thread=threading.get_ident(),
                         token=secrets.token_hex(8), person_id=person_id, counted=bool(counted))
        return grant

    def _grant_sends(self, call: Call, verdict: consent.Verdict | None) -> bool:
        """Whether this ``run_skill`` step's verdict lets it send for sure: row 4 allowed it with its countdown, and
        its capability sends with these slots (:meth:`_sends_for_sure`)."""
        if verdict is None or not verdict.allowed or verdict.row != 4 or not verdict.countdown:
            return False
        return self._sends_for_sure(call)

    def _sends_for_sure(self, call: Call) -> bool:
        """Whether this ``run_skill`` step's capability sends with these slots (its own ``sends_when`` data)."""
        key, slots = consent.skill_input(call, self.head)
        sk = util.skill(key)
        return sk is not None and consent.skill_sends(sk, slots, partial=False)

    def _may_have_sent(self, index: int) -> bool:
        """Whether the ``run_skill`` send of step ``index`` may have gone out (review r2 M1), from the engine's own
        outcome for it: never for one that asked (a question, the chat's kept draft, "WhatsApp instead?"), handed
        over, was refused or held before anything went, or whose check saw nothing go (``failed``: the message
        capability's check says no only when no send key was pressed). Anything else (done, unconfirmed, an error,
        no outcome) may have sent: counted, so it never goes twice."""
        run = next((r for r in reversed(self.skill_outcomes) if r.index == index), None)
        payload = getattr(getattr(run, "outcome", None), "payload", None) if run is not None else None
        state = str(payload.get("outcome") or "") if isinstance(payload, dict) else ""
        return state not in NOT_SENT_OUTCOMES

    def _send_person(self, call: Call) -> tuple[int | None, tuple[Any, str]]:
        """``(person id, send key)`` of a ``run_skill`` send: the one person its person slot names (memory's people
        index, :meth:`_grant_person`; ``None`` when memory can't say) and the message's identity in this request
        (that person, else the slot's words folded, and the text folded), which goes once per request (review r1
        L2). RAM only, never logged."""
        key, slots = consent.skill_input(call, self.head)
        sk = util.skill(key)
        tname = consent.text_slot(sk) if sk is not None else ""
        pname = consent.person_slot(sk) if sk is not None else ""
        text, person = (words(slots.get(tname)) if tname else ""), (words(slots.get(pname)) if pname else "")
        person_id = self._grant_person(person, consent.plan_consent(self.head)["to"])
        who: Any = person_id if person_id is not None else person.casefold()
        return person_id, (who, text.casefold())

    def _grant_person(self, person: str, to: str) -> int | None:
        """The id of the one person a send grant goes to (``yuki.skills.uia_route.grant_person``: the person slot's
        one person in memory's people index, never by sound, whom the plan's recipient names too); ``None`` when
        memory can't say, which leaves the grant to the user's own words as before."""
        memory = self.host.memory
        call = getattr(memory, "call", None)
        if not person or not callable(call):
            return None

        def go(client: Any) -> int | None:
            from yuki.skills.uia_route import grant_person

            return grant_person(getattr(client, "store", client), person, to)

        try:
            got = call("brain_grant_person", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return None
        return got if isinstance(got, int) and not isinstance(got, bool) else None

    def _desk_hands(self) -> Any:
        """The request's desk hands (``yuki.skills.uia_route.DeskHands``, made once): the hands the message and
        voice-join capabilities type and press through when a ``run_skill`` runs here, with the same consent and commit
        checks (``None`` when they can't be made: those capabilities then hand back, as before)."""
        if self._desk is None:
            try:
                from yuki.skills.uia_route import DeskHands

                self._desk = DeskHands(self.dispatcher, memory=self.host.memory, words=self.words,
                                       earlier=self.earlier, checker=self.checker, log=self._emit_log)
            except Exception:  # noqa: BLE001
                self._desk = False
        return self._desk or None

    def _dispatch_act(self, index: int, step: Any, call: Call, *, speak: bool = True,
                      effect: str = "", grant: Mapping[str, Any] | None = None) -> RunResult | None:
        """The Dispatcher call of an allowed step, its acting marker, step line, events, trace and check. ``effect``:
        the commit check's effect for this step (``""``: none was had), for what the step leaves waiting. ``grant``:
        a ``run_skill``'s consent grant (:meth:`_skill_grant`) for the desk hands, this step only."""
        status = self.statuses[index]
        if self._takes_marker(call):
            try:
                self.host.acting_begin()
                self.acting = True
            except Exception:  # noqa: BLE001 - the marker never breaks a run
                pass
        if self.tidy is None and call.tool in DESK_VERBS and self._desk_first is None:
            # P1.2: the desk right before the request's first act (after its consent and any countdown)
            self._desk_first = self._desk_now()
        about = self._about(call)
        desk = None
        if call.tool == "run_skill":
            # the engine checks slot values against the user's words as they stand now (x9: and an earlier request of
            # theirs that holds one of this step's values)
            desk = self._desk_hands()
            # Item 11's L2: a send's grant goes to the skill run too, which uses it only while these desk hands hold
            # the very same one (uia_route.send_grant); any other step's grant stays with the hands alone
            sends = desk is not None and isinstance(grant, Mapping) and grant.get("send") is True
            self._skill_context(call, grant=grant if sends else None)
            if desk is not None:
                # this step's grant only (the send key passes only for what the consent rule allowed and counted down)
                desk.grant = dict(grant or {})
                desk.words = list(self.words)
        self._emit(ToolCall(call.tool, {**call.input, "_about": about}))
        started_at, t0 = time.time(), time.perf_counter()
        from yuki.brain.plan import PLACE_ACTIONS

        # aBrain (phase 2.3): nothing is said as the step starts; a step still running after SLOW_STEP_S is said once
        # in the goal's words (a tidy-up's words come after the request's line, never here)
        slow = self._slow_watch() if speak and self.tidy is None else None
        try:
            with self._dispatch_lock:
                if call.tool == "window_action" and str(call.input.get("action") or "") in PLACE_ACTIONS:
                    outcome = self._place_act(call)   # P1.4: yuki.actions.place (the Dispatcher has no such op)
                else:
                    outcome = self.dispatcher.dispatch(call.tool, call.input)
        finally:
            if slow is not None:
                slow.cancel()
            if desk is not None:
                desk.grant = {}
            if call.tool == "run_skill":
                context = getattr(self.dispatcher, "skill_context", None)
                if isinstance(context, dict):
                    context.pop("grant", None)   # never left for a later step
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self._emit(ToolResult(call.tool, bool(outcome.ok), str(outcome.summary or "")[:300], payload=outcome.payload,
                              about=about))
        if self.tidy is None and call.tool in DESK_VERBS:
            self._desk_note(call, outcome)
        self._note_numbers(call, outcome)
        self._names_log(call, outcome)
        self._read_cache.pop(int(call.hwnd or 0), None)
        if call.tool == "open_url" and _web_host(call.input.get("url")):
            # the browser a later page_at reads: the one this address went to by name; one that went to Windows'
            # default handler names none, so an earlier one no longer holds the check (review r1 L3)
            if words(call.input.get("app")):
                self._note_browser(call, outcome)
            else:
                self.browser = ""
        own, why = util.action_verdict(call.tool, outcome)
        verdict = own
        expect = field_of(step, "expect", None)
        if outcome.ok and expect is not None:
            probe = done.probe_name(expect, "")   # the design's "probe", or bCore's validated Probe's "name"
            args, bad = done.parse_args(probe, field_of(expect, "args", "{}"))
            if args is not None:
                args = self._probe_args(probe, args)   # x9: page_at reads the named browser's page
            if args is not None and probe in done.PROBES:
                env = done.ProbeEnv(since=started_at, dispatcher=self.dispatcher, memory=self.host.memory,
                                    last_verdict=own, lock=self._dispatch_lock)
                got, evidence, _reads = done.wait_read(probe, args, env, budget_s=done.EXPECT_BUDGET_S,
                                                       hwnd=call.hwnd, cancelled=self.host.cancelled)
                if got is not None or own is None:
                    verdict = got
                    why = "its expectation was seen" if got else done.seen_words(probe, got, evidence)
        status.tool, status.ms, status.verdict = call.tool, ms, verdict
        # text from outside Yuki (a command's output, a file, a page) is fenced as untrusted data, as the old loop
        # fences run_powershell even when it failed; capped before the fence goes on, so the fence stays whole
        from yuki.agent.untrusted import EXTERNAL_TEXT_TOOLS, source_for, wrap

        external = call.tool in EXTERNAL_TEXT_TOOLS
        body = self._result_text(outcome)
        shown = wrap(body, source=source_for(call.tool, call.input), nonce=self.nonce) if external and body else body
        status.result = shown
        if call.tool in ("fetch_url", "read_file") or (call.tool == "run_powershell" and outcome.ok):
            # a fetch the commit check let through (M9), or a command that ran: what it read or printed goes back to
            # the planner, fenced (a failed command's output goes back fenced in its mismatch)
            self.read_results.append({"i": index, "verb": "fetch" if call.tool != "run_powershell" else call.tool,
                                      "tool": call.tool, "ok": bool(outcome.ok), "ms": ms,
                                      "text": wrap(self._result_text(outcome, READ_CHARS),
                                                   source=source_for(call.tool, call.input), nonce=self.nonce)})
        send_state, send_sig = None, None
        if call.tool == "run_skill":
            payload = outcome.payload if isinstance(outcome.payload, dict) else {}
            key, slots = consent.skill_input(call, self.head)
            self.skill_outcomes.append(_SkillRun(index, outcome, key, slots))
            self._skill_key = str(payload.get("skill") or key or "") or None
            sk = util.skill(key)
            if consent.skill_sends(sk, slots, partial=False):   # what ran sends for sure (an allowed send fills it)
                send_state = "confirmed" if verdict is True else "unconfirmed"
                text = words(slots.get(consent.text_slot(sk))) if consent.text_slot(sk) else ""
                if text:   # which message (a fingerprint only, never the text): a retry is the same message
                    send_sig = hashlib.sha1(text.casefold().encode("utf-8", "replace")).hexdigest()[:12]
        self._trace_step(call, outcome, ok=bool(outcome.ok), verdict=verdict, why=why, ms=ms, started=started_at,
                         send=send_state, send_sig=send_sig)
        self._after_act(call, outcome, effect=effect, verdict=verdict)
        if call.tool == "open_url":
            self._note_page(index, outcome, verdict)
        self.last_act = {"hwnd": call.hwnd, "app": call.app, "title": call.title, "verdict": verdict}
        self._ran_calls[index] = call
        if call.tool == "run_skill":
            return self._skill_result(index, step, call, outcome)
        if not outcome.ok or verdict is False:
            status.state, status.why = "failed", why
            if self.tidy is None:
                self._note_stuck(index, call)   # desk20 (S20): the ending names its app and what was left
            payload = outcome.payload if isinstance(outcome.payload, dict) else {}
            kind = _refused_kind(payload)
            if kind == "game" and self._game_seen(call):
                return RunResult("refused", line=consent.LINE_GAME, final=True)
            if kind in ("game", "blocked"):
                # fHands note 2: the gate stopped the step for anything but a game (it failed closed, another guard:
                # "blocked"), or said game with none in front: a settled stop with a plain line, never the game line,
                # never a replan (it would meet the same gate)
                status.state, status.why = "refused", "input_gate"
                self._emit_log("brain_gate_no_game", verb=call.tool, kind=kind)
                return RunResult("refused", line=self._stopped_line(index, call), final=True)
            return self._mismatch_result(index, why, seen=shown, call=call, seen_fenced=external and bool(body))
        status.state, status.why = "ok", why
        if self.tidy is None and call.tool in ACT_VERBS:
            self._stuck_said = None   # desk20 (S20): a later act took: an earlier stuck step is no longer the ending
        if self.tidy is not None:
            self.tidy["acts"] = int(self.tidy.get("acts", 0)) + 1
            payload = outcome.payload if isinstance(outcome.payload, dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            changed = bool(details.get("changed"))
            if changed:   # review L1: words only for what the step's own read-back saw change
                self._tidy_changed += 1
                self.tidy["words"].append(self._tidy_line(call, details, LINE_TIDY))
            self._emit_log("tidy", verb=call.tool, action=str(call.input.get("action") or ""), state="ok",
                           changed=changed)
        return None

    def _skill_result(self, index: int, step: Any, call: Call, outcome: Any) -> RunResult | None:
        """A ``run_skill`` step's own outcome: done (its check saw it), held for a game or a call, a question, a
        hand-off (rung C: a plan seed for the planner), or not done."""
        status = self.statuses[index]
        payload = outcome.payload if isinstance(outcome.payload, dict) else {}
        state = str(payload.get("outcome") or "")
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        kind = _refused_kind(payload)
        held = HELD_OUTCOMES.get(state) or ("game" if kind == "game" else "")
        if kind == "blocked" or state in BLOCKED_OUTCOMES:
            # fHands note 2: a skill step the gate stopped for anything but a game: the same settled stop with the
            # plain line (the engine's own when it gave one), never the game line, never a replan or the next rung
            status.state, status.why = "refused", "blocked"
            self._emit_log("brain_gate_no_game", verb=call.tool, kind="blocked")
            return RunResult("refused", line=done.engine_line(outcome) or LINE_BLOCKED, final=True)
        if held:
            # kBrain review M1: held before it acted (a game in front, a call live): settled, never a replan; the
            # engine's own line says so and names the pick it kept for later
            status.state, status.why = "refused", held
            return RunResult("refused", line=done.engine_line(outcome) or (
                consent.LINE_GAME if held == "game" else LINE_HELD_CALL), final=True)
        if details.get("refused") == "act_off" or payload.get("refused") == "act_off":
            status.state, status.why = "refused", "act_off"
            return RunResult("refused", line=done.engine_line(outcome) or str(outcome.summary or ""), final=True)
        who = self._who_result(index, call, outcome, state, details)
        if who is not None:
            return who
        # ``why`` stays the outcome's code word (the planner's report, the logs); ``line`` is what is said if the
        # request ends on this step: plain words only, never the code word
        if state == "handed_over" and isinstance(payload.get("handoff"), dict):
            status.state, status.why = "failed", "handed_over"
            self.seed = dict(payload["handoff"])
            return self._mismatch_result(index, "handed_over", seen=str(outcome.summary or ""), call=call,
                                         seed=dict(payload["handoff"]), line=_handed_line(payload["handoff"]))
        if state == "asked":
            status.state, status.why = "failed", "asked"
            ask = _engine_ask(outcome)
            return self._mismatch_result(index, "asked", seen=str(outcome.summary or ""), call=call,
                                         line=LINE_SKILL_ASK.format(ask=ask) if ask else LINE_SKILL_ASK_PLAIN)
        if not outcome.ok or state in ("failed", "refused"):
            status.state, status.why = "failed", state or "failed"
            return self._mismatch_result(index, state or "failed", seen=str(outcome.summary or ""), call=call,
                                         line=_engine_said(outcome) or LINE_SKILL_FAILED)
        status.state, status.why = "ok", state
        if self.tidy is None:
            self._stuck_said = None   # desk20 (S20): a later act took: an earlier stuck step is no longer the ending
        return None

    def _who_result(self, index: int, call: Call, outcome: Any, state: str,
                    details: Mapping[str, Any]) -> RunResult | None:
        """jBrain (fMsg B1): a ``run_skill`` of a capability with a person slot (a message) that asked its own question
        or couldn't place the person (its hand-off dropped that slot as unheard, or its routes found no one by it):
        whom it goes to is open, so the user is asked, never a fallback by steps into whichever chat is open. The
        capability's own question is asked at once (the Brain asks it and the answer goes back to the planner, so the
        next plan runs the capability with that person); a person it couldn't place goes back to the planner, with no
        hand-off seed (the capability stays open for the answer), to ask whom in one short question. A run that
        confirmed its chat settles it. Code facts only (the capability's slots, the engine's outcome words)."""
        key, _slots = consent.skill_input(call, self.head)
        slot = consent.person_slot(util.skill(key))
        if not slot:
            return None
        if state in ("done", "already") or details.get("verified") is True:
            self._who_unsettled = False
            return None
        status = self.statuses[index]
        if state == "asked":
            self._who_unsettled = True
            ask = _engine_ask(outcome)
            status.state, status.why = "failed", "asked"
            self._emit_log("brain_who", how="ask", own=bool(ask))
            return RunResult("ask", line=ask or LINE_WHO)
        payload = outcome.payload if isinstance(outcome.payload, dict) else {}
        handoff = payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {}
        from yuki.brain.plan import handoff_person_open

        if state == "handed_over" and handoff_person_open(handoff, slot):
            self._who_unsettled = True
            status.state, status.why = "failed", "who_open"
            self._emit_log("brain_who", how="replan", own=False)
            return self._mismatch_result(index, "who_open", seen=HINT_WHO_UNHEARD, call=call, line=LINE_WHO)
        return None

    def _who_guard(self, index: int, call: Call, live: consent.Live) -> RunResult | None:
        """jBrain (fMsg B1): while whom a message goes to is open in this request (:meth:`_who_result`), a typing step
        into a chat's window (``consent.chat_window``; unknown counts as a chat) is refused back to the planner: ask
        whom, then run the capability with that person (its draft types only into the chat it confirmed open).
        ``None``: the step goes on."""
        if not self._who_unsettled or call.tool != "type_text":
            return None
        try:
            chat = consent.chat_window(call, live)
        except Exception:  # noqa: BLE001 - can't tell: as a chat (fail closed)
            chat = None
        if chat is False:
            return None
        status = self.statuses[index]
        status.state, status.why = "refused", "who_open"
        self._trace_step(call, None, ok=False, verdict=False, why="refused: who_open", kind="control", refused=True)
        self._emit_log("consent", verb=call.tool, row=4, result="refuse", why="who_open")
        return self._mismatch_result(index, "who_open", refused=True, seen=HINT_WHO_FIRST, line=LINE_WHO)

    def _run_draft(self, index: int, step: Any, call: Call, verdict: consent.Verdict) -> RunResult | None:
        """Row 4's draft: the capability runs with its send slot on a non-sending option; Yuki's own wording is typed
        without Enter once the capability saw the chat open (nothing is typed into a chat not confirmed open)."""
        status = self.statuses[index]
        drafted = Call(tool="run_skill", input=dict(verdict.draft_input or {}), index=index)
        # a draft never sends: its grant carries only its own text (typed without Enter by the capability)
        result = self._dispatch_act(index, step, drafted, grant=self._skill_grant(drafted, None))
        run = self.skill_outcomes[-1] if self.skill_outcomes and self.skill_outcomes[-1].index == index else None
        if run is not None:
            run.drafted, run.type_after = True, bool(verdict.type_after)
        if result is not None:
            return result
        if verdict.type_after:
            payload = getattr(run.outcome, "payload", None) if run is not None else None
            opened = (payload.get("details") or {}).get("verified") is True if isinstance(payload, dict) else False
            if not opened:
                status.state, status.why = "failed", "chat_not_confirmed"
                return RunResult("refused", line=LINE_CHAT_UNCONFIRMED, final=True)
            # the user may take over or stop between the capability's check and the typing: nothing starts then
            held = self._hold_pause()
            if held is not None or self.host.cancelled():
                status.state, status.why = "skipped", "stopped before typing"
                return held if held is not None else RunResult("cancelled")
            key, slots = (run.key, run.slots) if run is not None else consent.skill_input(drafted, self.head)
            outcome_run = run.outcome if run is not None else None
            front, entry, fits, _listed = self._skill_windows(outcome_run, key, slots)
            if front is None or fits is False:
                # the window in front is not the capability's chat (another app came forward): nothing is typed
                status.state, status.why = "failed", "chat_not_in_front"
                return RunResult("refused", line=LINE_CHAT_UNCONFIRMED, final=True)
            typed = Call(tool="type_text",
                         input={"text": verdict.type_after, "press_enter": False, "expect_hwnd": front},
                         index=index, hwnd=front, app=str(entry.get("process_name") or ""),
                         title=str(entry.get("title") or ""))
            typed_live = self._live(typed)
            gate = consent.check(typed, None, self._all_words(), typed_live, None)
            self._emit_log("consent", verb=typed.tool, row=gate.row, result=gate.result, why=gate.why)
            if gate.result == consent.ASK:
                # a kind the user's settings hold for asking (typing): their yes, with the desk free while asked
                self.lease.release()
                try:
                    yes = bool(self.host.ask(gate.question))
                except Exception:  # noqa: BLE001 - no answer is a no
                    yes = False
                if not self.lease.take():
                    return RunResult("busy", line=LINE_BUSY, final=True)
                if not yes:
                    status.state, status.why = "declined", "the user said no"
                    self._trace_step(typed, None, ok=False, verdict=False, why="declined", kind="ask")
                    return RunResult("declined", line=LINE_DECLINED, final=True)
                again, _entry, fits, _listed = self._skill_windows(outcome_run, key, slots)
                if again != front or fits is False:   # the chat must still be in front after the answer
                    status.state, status.why = "failed", "chat_not_in_front"
                    return RunResult("refused", line=LINE_CHAT_UNCONFIRMED, final=True)
                gate = consent.screen_gate(typed, self._live(typed)) or consent.after_ask(gate)
            if not gate.allowed:
                status.state, status.why = "refused", gate.why
                self._trace_step(typed, None, ok=False, verdict=False, why=f"refused: {gate.why}", kind="control",
                                 refused=True)
                return RunResult("refused", line=gate.line or LINE_DRAFT_HELD, final=True)
            result = self._dispatch_act(index, step, typed, speak=False)
            if run is not None:
                run.typed = self.statuses[index].verdict   # the typing's own check (None: couldn't read it back)
            if result is not None:
                return result
        status.state, status.why = "ok", "drafted"
        return None

    def _bring_forward(self, hwnd: int) -> None:
        """A refused join: its window brought forward (row 1 still applies: never with a game in front)."""
        call = Call(tool="focus_window", input={"hwnd": int(hwnd)}, hwnd=int(hwnd))
        game = consent.game_now(_board())
        if game is None or game:
            return
        try:
            if self._takes_marker(call):
                self.host.acting_begin()
                self.acting = True
            with self._dispatch_lock:
                outcome = self.dispatcher.dispatch("focus_window", {"hwnd": int(hwnd)})
            self._trace_step(call, outcome, ok=bool(outcome.ok), verdict=util.action_verdict("focus_window", outcome)[0],
                             why="brought forward for the user's click")
        except Exception:  # noqa: BLE001 - the line still says it
            pass

    def _countdown(self, call: Call) -> str:
        """The send countdown (``SendPending`` with its swap chip): ``going``, ``stopped`` (a cancel), or ``swapped``
        (the user's words arrived: a chip tap or a correction; they go to the planner). The host's own countdown
        (``host.countdown``) when it has one, else this one."""
        self._swap_words = []
        c = consent.plan_consent(self.head)
        key, slots = consent.skill_input(call, self.head)
        sk = util.skill(key)
        app_slot = next((sl.name for sl in getattr(sk, "slots", ()) or () if getattr(sl, "type", "") == "app"), "")
        app = (words(slots.get(app_slot)) if app_slot else "") or (str(getattr(sk, "app", "") or "") if sk else "")
        info = {"who": c["to"], "where": util.app_name(app) if app else "", "app": util.app_name(app) if app else "",
                "text": c["text"], "typed": False, "from_memory": False, "window": ""}
        hosted = getattr(self.host, "countdown", None)
        self._counted = False
        opened = time.monotonic()
        if callable(hosted):
            # the host's own countdown (bCore's Brain: its swap chips, and its takeover and chip state see the send
            # open); one countdown, never two
            try:
                state = str(hosted(who=info["who"], where=info["where"], app=info["app"], text=info["text"],
                                   typed=False) or "")
            except Exception:  # noqa: BLE001 - no countdown ran: nothing is sent
                state = "stopped"
            state = state if state in ("going", "stopped", "swapped") else "stopped"
            if state in ("going", "swapped"):
                # words the user said while it counted (a correction) stop the send for a replan, as a chip would;
                # hands19 (C4): the host's countdown also ends ``swapped`` the moment their words reach it (while it
                # counted, or paused on their speech): those words are theirs for the consent rule and the planner
                try:
                    late = [str(w) for w in (self.host.steer_pending() or []) if str(w).strip()]
                except Exception:  # noqa: BLE001
                    late = []
                if late:
                    self._swap_words = late
                    self.steers += late
                    self.words += late
                    state = "swapped"
            # review r1 L3: the host's countdown ran only if it was open (its 0 s default returns at once)
            self._counted = state == "going" and time.monotonic() - opened >= COUNTED_MIN_S
            self._emit_log("send_countdown", state=state, hosted=True, counted=self._counted)
            return state
        seconds = getattr(self.host, "send_countdown_s", None)
        if seconds is None:
            seconds = getattr(self.settings, "send_countdown_s", None)
        seconds = DEFAULT_COUNTDOWN_S if seconds is None else float(seconds or 0.0)
        if seconds <= 0:
            return "going"
        if getattr(self.host, "channel", "") == "voice" and c["to"]:
            self._emit(Say(f"Sending to {c['to']}"))
        self._emit(SendPending("counting", seconds=seconds, **info))
        wake = getattr(self.host, "send_wake", None)
        deadline = time.monotonic() + seconds
        state = "going"
        while True:
            if self.host.cancelled():
                state = "stopped"
                break
            try:
                steer = [str(w) for w in (self.host.steer_pending() or []) if str(w).strip()]
            except Exception:  # noqa: BLE001
                steer = []
            if steer:
                self._swap_words = steer
                self.steers += steer
                self.words += steer
                state = "swapped"
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if isinstance(wake, threading.Event):
                wake.wait(left)
                wake.clear()
            else:
                threading.Event().wait(min(left, 0.1))
        self._emit(SendPending(state, seconds=seconds, **info))
        self._counted = state == "going" and time.monotonic() - opened >= COUNTED_MIN_S
        self._emit_log("send_countdown", state=state, seconds=seconds, counted=self._counted)
        return state

    # -- resolution --------------------------------------------------------------------------------------------------

    def _front(self, payload: Mapping[str, Any] | None, *, log: bool = True) -> int | None:
        """The window a step that names none acts on: the foreground, except while Yuki's own window is in front (a
        typed request: the ask bar or the chat page), when it is the request's origin (``host.origin_hwnd``: the window
        in front when the user called Yuki up), if that window is still listed. Code facts only."""
        payload = payload if isinstance(payload, Mapping) else {}
        front = done.as_hwnd(payload.get("foreground_hwnd"))
        origin = done.as_hwnd(getattr(self.host, "origin_hwnd", None))
        if origin is None or origin == front:
            return front
        if not any(isinstance(w, dict) and done.as_hwnd(w.get("hwnd")) == origin for w in payload.get("windows") or []):
            return front
        if front is not None and not _is_own_window(front):
            return front
        if log and not self._origin_used:
            self._origin_used = True
            self._emit_log("brain_origin", used=True, front_known=front is not None)
        return origin

    def _open_url_browser(self, tool_input: dict[str, Any], ref: Any, hwnd: int | None, entry: Mapping[str, Any],
                          payload: Mapping[str, Any], *, log: bool = True) -> tuple[int | None, dict[str, Any], str]:
        """An ``open_url`` step's browser (x9, C5: the plan's ``window: Arc`` never reached the tool, so Windows'
        default handler opened YouTube in Chrome three times on 10 Oct). Only for a web page address whose step value
        gives no ``app``; the browser is the step's own ``window``, else the plan's target app, each read so:

        1. a listed window of it (an hwnd on the list, a window of the app it names, or the one window its title hint
           names): ``app`` is the name Windows gives that program ("Arc.exe" -> "Arc"), so open_url hands the address
           to that very browser;
        2. else the name as written: a browser that isn't open (open_url starts it with the address; ``launch_app``
           never guesses, so a name that fits no Start menu app fails the step with the candidates and the planner
           replans).

        Either way the program is taken only when machine data says it is a browser (:meth:`_is_browser`, review r1
        M1): for "open the YouTube link Hemanth sent me on Discord" the plan's target app is Discord, which
        would be handed the link and never open it. A name that is the address's own site ("YouTube" for youtube.com,
        "Microsoft Teams" for teams.microsoft.com) names no browser, and Yuki's own window is never one. A name an app
        on this PC is known by (``appnames.aliases``: machine data) is that app, never a title hint: "Arc" while Arc is
        closed starts Arc, never a Chrome window whose title has the word "Arc". A name that fits windows on screen by
        their titles but no one program is not started either. No browser named: ``app`` stays unset (Windows' default
        handler, as before).

        A bare host ("youtube.com", :func:`_bare_host`) is a web address here only when its browser is named (review
        r1 L1); it goes to that browser as ``https://``, as a browser opens it. An ``app`` the step value wrote is kept
        as written, and the call's window is that app's when one is listed (review r1 L4).

        Sets ``tool_input["app"]`` (and ``"url"`` for a bare host); returns the window the address goes to (``None``
        when its app isn't open; the step's own window when no browser was chosen), its entry, and its program's
        process image (``""``: not known before it opens)."""
        windows = [dict(w) for w in payload.get("windows") or [] if isinstance(w, Mapping)
                   and done.as_hwnd(w.get("hwnd"))]
        front = done.as_hwnd(payload.get("foreground_hwnd"))
        given = words(tool_input.get("app"))
        url = str(tool_input.get("url") or "").strip()
        host = _web_host(url)
        bare = "" if host else _bare_host(url)
        chosen: tuple[int | None, dict[str, Any], str] | None = None
        source = "none"
        if given:
            # the plan wrote its app itself: kept as written; the call is that app's window when it is listed (its Act
            # switch, the screen gate and the done-check's window are the app the address goes to), else none
            found = _app_windows(windows, given, front)
            source = "value"
            chosen = (done.as_hwnd(found[0].get("hwnd")), found[0], str(found[0].get("process_name") or "")) \
                if found else (None, {}, "")
            if bare and self._is_browser(chosen[2] or given):
                tool_input["url"] = "https://" + url
        elif not (host or bare):
            source = "not_web"   # a file, a folder or another scheme: its own handler opens it, as before
        else:
            target = words(field_of(field_of(self.head, "target", {}), "app", "")) if self.head is not None else ""
            names = [(name, kind) for name, kind in ((words(ref) if ref is not None else "", "window"),
                                                     (target, "target"))
                     if name and not _names_site(name, host or bare)]
            resolved = dict(entry) if hwnd is not None and entry.get("process_name") and \
                not _is_own_window(int(hwnd)) else None   # the one listed window the step's window field named
            for name, kind in names:   # the step's own window wholly first, then the plan's target app
                if done.as_hwnd(name) is not None:
                    # an hwnd: the window it is while it is listed (an hwnd no longer listed names no program)
                    found = [resolved] if kind == "window" and resolved is not None else []
                else:
                    found = _app_windows(windows, name, front)
                    if not found and kind == "window" and resolved is not None and not _known_app(name):
                        found = [resolved]   # the one window the hint's title names
                if found:
                    image = str(found[0].get("process_name") or "")
                    if not self._is_browser(image):
                        source = "not_browser"   # a program that isn't a browser (the chat app a link is in)
                        continue
                    tool_input["app"] = _launch_name(image)
                    chosen, source = (done.as_hwnd(found[0].get("hwnd")), found[0], image), kind
                    break
                if done.as_hwnd(name) is not None:
                    continue
                fits = done.fitting(windows, name)
                if any(_is_own_window(done.as_hwnd(w.get("hwnd")) or 0) for w in fits):
                    continue   # Yuki's own window is never a browser
                if fits and not _known_app(name):
                    continue   # a title hint for windows on screen that fit no one program: nothing to start
                if not self._is_browser(name):
                    source = "not_browser"   # no browser goes by that name on this PC: nothing to start
                    continue
                tool_input["app"] = name   # a browser that isn't open: open_url starts it with the address
                chosen, source = (None, {}, ""), "named_" + kind
                break
            if bare and chosen is not None:
                tool_input["url"] = "https://" + url   # a bare host its browser opens as https
            elif bare:
                source = "not_web"   # no browser named: a bare name.tld goes to its own handler, as before
        if log:
            self._emit_log("brain_open_url", source=source, open=bool(chosen and chosen[0]))   # a code word (log enum)
        return chosen if chosen is not None else (hwnd, dict(entry), "")

    def _browsers(self) -> list[dict[str, Any]]:
        """The programs that are browsers on this PC by machine data, each ``{"key", "name"}`` (app key, name), read
        once per request; zero setup, never a list of browsers:

        * the user's own timeline (``yuki.skills.web_route.measured_browsers`` with ``by_site=False``: a program that
          showed pages of at least ``BROWSER_MIN_HOSTS`` hosts in the last ``BROWSER_DAYS`` days, never Yuki's own
          rows; the web skill's definition, so a chat app that draws its one site, Discord or Slack, is never one),
          through memory (one bounded query);
        * the browsers Windows has registered (:func:`_registered_browsers`), so one installed but not used lately
          ("open it in Firefox") counts too, and Chrome or Edge do while memory can't answer.

        ``[]`` when neither can be read: then no program is taken for a browser (Windows' default handler opens the
        address and the check reads the window in front, as before x9). Never raises."""
        if self._browser_rows is None:
            rows: Any = None
            try:
                run = done._memory_call(self.host.memory)

                def go(client: Any) -> list[dict[str, Any]]:
                    from yuki.skills.web_route import measured_browsers

                    return measured_browsers(getattr(client, "store", client), by_site=False)

                rows = run("brain_browsers", go) if run is not None else None
            except Exception:  # noqa: BLE001 - memory unavailable or slow: the registered browsers only
                rows = None
            measured = [dict(r) for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []
            registered = _registered_browsers()
            self._browser_rows = measured + registered
            self._emit_log("brain_browsers", measured=len(measured), registered=len(registered))
        return self._browser_rows

    def _is_browser(self, name: str) -> bool:
        """Whether a program (its image, "Arc.exe", or an app's name, "Arc", "Google Chrome") is a browser by machine
        data (:meth:`_browsers`: the user's timeline, Windows' registered browsers), by every name it goes by
        (``util.app_names``: as written, its process key, the name Windows gives it). Never a list of browsers.
        ``False`` when it can't be read."""
        try:
            want = util.app_names(name)
            return bool(want) and any(
                want & (util.app_names(str(row.get("key") or "")) | util.app_names(str(row.get("name") or "")))
                for row in self._browsers())
        except Exception:  # noqa: BLE001
            return False

    def _note_browser(self, call: Call, outcome: Any) -> None:
        """An ``open_url`` that named its app ran (x9): that app's program (its process image) is the browser a later
        page_at check reads, when machine data says it is one (:meth:`_is_browser`); else none (``""``: the
        check reads the window in front, as before), never an earlier browser of the request. Known from the window it
        was resolved to, else from the window open_url reports (``launch_app``'s ``details.hwnd`` with its
        ``process_name``, else as the window list shows that hwnd now; a ``process_name`` with no hwnd is the
        launcher's own window in front, never the browser's). Never raises."""
        image = ""
        try:
            image = self._open_images.get(call.index, "")
            if not image:
                payload = outcome.payload if isinstance(getattr(outcome, "payload", None), dict) else {}
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                opened = done.as_hwnd(details.get("hwnd"))
                image = str(details.get("process_name") or "").strip() if opened is not None else ""
                if not image and opened is not None:
                    now = done.overview(self.dispatcher, lock=self._dispatch_lock)
                    self._last_overview = now or self._last_overview
                    image = next((str(w.get("process_name") or "") for w in now.get("windows") or []
                                  if isinstance(w, dict) and done.as_hwnd(w.get("hwnd")) == opened), "")
        except Exception:  # noqa: BLE001 - bookkeeping: the check then reads the window in front, as before
            image = ""
        self.browser = image if image and self._is_browser(image) else ""

    def _page_process(self, site: str = "") -> str:
        """The program a page_at check reads (x9: YouTube in Chrome is never "likely" for an Arc request): the browser
        this request's last open_url opened its address in by name, else the plan's target app when it is a browser
        (:meth:`_is_browser`: never the chat app a link was opened from), a window of it is listed and it isn't the
        page's own site. ``""`` when neither: the check reads the window in front, as before."""
        if self.browser:
            return self.browser
        target = words(field_of(field_of(self.head, "target", {}), "app", "")) if self.head is not None else ""
        if not target or _names_site(target, site):
            return ""
        image = self._listed_image(target)
        return image if image and self._is_browser(image) else ""

    def _listed_image(self, name: str) -> str:
        """The process image of the one program an app name names among the listed windows ("Arc" -> "Arc.exe"), read
        now; ``""`` when no listed window is that app's, or the name fits several programs."""
        payload = done.overview(self.dispatcher, lock=self._dispatch_lock)
        self._last_overview = payload or self._last_overview
        windows = [w for w in payload.get("windows") or [] if isinstance(w, Mapping) and done.as_hwnd(w.get("hwnd"))]
        found = _app_windows(windows, name, done.as_hwnd(payload.get("foreground_hwnd")))
        return str(found[0].get("process_name") or "") if found else ""

    def _probe_args(self, probe: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """A probe's arguments as the check reads them (x9). A probe's ``process`` is compared with a window's process
        image (``yuki.skills.donecheck``: page_at, foreground, tab_shown, uia_name, audio_active, mic_held), so one the
        plan wrote as an app's name ("Arc") becomes that program's image when a window of it is listed ("Arc.exe":
        "Arc" would never equal it, so the check would always read "another app", never what is there). For page_at
        with none written, it is the browser of this request (:meth:`_page_process`, :func:`done.with_browser`): a page
        in another browser is "not so", never "likely done". Every other argument as given."""
        out = dict(args)
        spec = done.PROBE_ARGS.get(probe, ((), ()))
        if "process" not in spec[0] + spec[1]:
            return out
        written = words(out.get("process"))
        if written:
            image = "" if written.casefold().endswith(".exe") else self._listed_image(written)   # an image already
            if image:
                out["process"] = image
            return out
        if probe != "page_at":
            return out
        return done.with_browser(probe, out, self._page_process(str(out.get("site") or "")))

    def _resolve(self, index: int, step: Any) -> tuple[Call | None, str]:
        """The step as a Dispatcher call with its target's code facts: the window (an hwnd, or the one window a title
        hint or app names), the element by its name and role in a read of that window, the tab by its name in the
        window's strip. ``(None, why)`` when it can't be found as one thing."""
        verb = self._verb(step)
        if verb not in ACT_VERBS and verb != "fetch":
            return None, f"{verb!r} is not a step verb"
        if verb == "fetch":
            call = self._fetch_call(index, step)
            return (call, "") if call is not None else (None, "a fetch needs an address or a path")
        try:
            tool_input = self._tool_input(verb, step)
        except ValueError as exc:
            return None, str(exc)
        payload = done.overview(self.dispatcher, lock=self._dispatch_lock)
        self._last_overview = payload or self._last_overview
        ref = field_of(step, "window", None)
        hwnd, entry = done.window_of(self.dispatcher, ref, payload=payload) if ref else (None, {})
        if verb == "open_url":
            # x9: the step's window (else the plan's target app) is the browser to open it in, handed to open_url as
            # its app; a browser that isn't open (or is open twice) still runs: open_url starts or picks it
            hwnd, entry, image = self._open_url_browser(tool_input, ref, hwnd, entry, payload)
            if image:
                self._open_images[index] = image
            else:
                self._open_images.pop(index, None)
        elif ref and hwnd is None:
            return None, "no single window fits that window"
        front = self._front(payload)
        from yuki.brain.plan import WINDOWLESS_ACTIONS

        windowless = verb == "window_action" and str(tool_input.get("action") or "") in WINDOWLESS_ACTIONS
        if windowless:
            return Call(tool=verb, input=tool_input, index=index), ""   # undo: whatever the journal holds
        if hwnd is None and verb in ELEMENT_VERBS | {"hotkey", "press", "window_action", "tab", "focus_window"}:
            hwnd = front
            entry = next((w for w in payload.get("windows") or [] if isinstance(w, dict)
                          and done.as_hwnd(w.get("hwnd")) == hwnd), {}) if hwnd else {}
        call = Call(tool=verb, input=tool_input, index=index, hwnd=hwnd, app=str(entry.get("process_name") or ""),
                    title=str(entry.get("title") or ""))
        if verb == "open_url" and not call.app and words(tool_input.get("app")):
            call.app = words(tool_input.get("app"))   # a browser named that isn't open: its "Act" switch still applies
        element = field_of(step, "element", None)
        if verb in ("focus_window", "window_action"):
            if hwnd is None:
                return None, "no window to act on"
            tool_input["hwnd"] = hwnd
        elif verb == "tab":
            if hwnd is None or element is None:
                return None, "a tab step names its window and the tab"
            tab_id = self._tab_id(hwnd, str(field_of(element, "name", "") or ""))
            if tab_id is None:
                return None, "no single tab of that window has that name"
            tool_input.update(hwnd=hwnd, tab=tab_id)
            tool_input.setdefault("action", "select")
            call.name, call.role = str(field_of(element, "name", "")), "TabItem"
        elif verb in ELEMENT_VERBS:
            if element is None and verb in ("click", "set_slider"):
                return None, f"a {verb} step names its element"
            if element is not None:
                found, why = self._element(hwnd, element)
                if found is None:
                    return None, why
                tool_input["element"] = int(found["id"])
                call.role, call.name = str(found.get("role") or ""), str(found.get("name") or "")
            elif verb == "scroll":
                center = _center(entry.get("bounds"))
                if center is None:
                    return None, "a scroll names its element or its window"
                tool_input.update(x=center[0], y=center[1])
            if hwnd:
                tool_input["expect_hwnd"] = hwnd
        elif verb in ("hotkey", "press"):
            keys = tool_input.get("keys") if verb == "hotkey" else [tool_input.get("key")]
            system = bool(keys) and all(isinstance(k, str) and k.strip().casefold() in util._SYSTEM_KEYS for k in keys)
            if hwnd and not system:
                tool_input["expect_hwnd"] = hwnd
        elif verb == "run_skill":
            key, _slots = consent.skill_input(call, self.head)
            if not key or util.skill(key) is None:
                return None, "no capability or learned skill by that id"
            tool_input["skill"] = key
            call.app = ""
        return call, ""

    def _element(self, hwnd: int | None, element: Any) -> tuple[dict | None, str]:
        """The element a step names, in a read of its window: by the id a look gave (``role`` ``id``), else by its
        name and role. A miss is told apart from a thin read by UI Automation's by-name search."""
        if not hwnd:
            return None, "no window to look in"
        name = str(field_of(element, "name", "") or "")
        role = str(field_of(element, "role", "") or "")
        if role.casefold() == "id" or _element_ref(name) is not None:
            # an id from a look: role "id", or the name written "[12]" / "#12" (bCore's Element.ref_id)
            ref = done.as_hwnd(name.strip("[]# "))
            info = self.dispatcher.element_info({"element": ref, "expect_hwnd": hwnd}) if ref is not None else None
            if not info:
                return None, "that element id is not in the latest read of its window"
            return {"id": ref, "role": info.get("role") or "", "name": info.get("name") or ""}, ""
        elements = self._window_elements(hwnd)
        if elements is None:
            return None, "the window could not be read"
        found, n = done.find_named(elements, name, role)
        if found is not None and isinstance(found.get("id"), int):
            return found, ""
        if n > 1:
            return None, f"{n} controls fit {role or 'that'} \"{name[:60]}\", so which one is not clear"
        try:
            from yuki.skills import uia_route

            live = uia_route.find_in(int(hwnd), role=role, name=name)
        except Exception:  # noqa: BLE001
            live = None
        if live is not None and live.status == "found":
            return None, f"{role or 'the control'} \"{name[:60]}\" is there but outside the read: read it with look"
        if live is not None and live.status == "ambiguous":
            return None, f"{live.n} controls fit {role or 'that'} \"{name[:60]}\", so which one is not clear"
        return None, f"no {role or 'control'} \"{name[:60]}\" in that window"

    def _window_elements(self, hwnd: int) -> list[dict[str, Any]] | None:
        """A read of the window through the Dispatcher (its ids are what it acts on), reused while the window's events
        show no change and it is at most :data:`READ_REUSE_S` old. A window not read lately gets the Dispatcher's
        full read budget, which is its warm-up for a cold window."""
        hub = done._hub(self.dispatcher)
        generation = None
        if hub is not None:
            try:
                hub.watch(int(hwnd))
                generation = hub.generation(int(hwnd))
            except Exception:  # noqa: BLE001
                generation = None
        cached = self._read_cache.get(int(hwnd))
        if cached is not None and generation is not None and cached[1] == generation and \
                time.monotonic() - cached[0] <= READ_REUSE_S:
            return cached[2]
        with self._dispatch_lock:
            outcome = self.dispatcher.dispatch("look_at_window", {"hwnd": int(hwnd)})
        payload = getattr(outcome, "payload", None)
        if not getattr(outcome, "ok", False) or not isinstance(payload, dict) or \
                str(payload.get("status") or "ok") != "ok":
            return None
        elements = [e for e in payload.get("elements") or [] if isinstance(e, dict)]
        self._read_cache[int(hwnd)] = (time.monotonic(), generation, elements, payload)
        self.numbers.add(int(hwnd))
        return elements

    def _tab_id(self, hwnd: int, name: str) -> str | None:
        """The id of the one tab of the window's strip named ``name`` (exact, case aside)."""
        with self._dispatch_lock:
            outcome = self.dispatcher.dispatch("read_tabs", {"hwnd": int(hwnd)})
        payload = getattr(outcome, "payload", None)
        tabs = payload.get("tabs") if isinstance(payload, dict) else None
        want = words(name).casefold()
        hits = [t for t in tabs or [] if isinstance(t, dict) and words(t.get("name")).casefold() == want]
        if len(hits) != 1:
            return None
        tab = hits[0]
        return str(tab.get("tab_id") or tab.get("id") or "") or None

    def _tool_input(self, verb: str, step: Any) -> dict[str, Any]:
        """The Dispatcher input a step's ``value`` (and ``submit``) give. Raises :class:`ValueError` when the value
        can't be read for that verb."""
        value = field_of(step, "value", None)
        obj = _json_object(value)
        out: dict[str, Any] = {}
        if obj is not None:
            allowed = VALUE_FIELDS.get(verb, frozenset())
            out.update({k: v for k, v in obj.items() if k in allowed})
        elif value not in (None, ""):
            text = str(value)
            from yuki.brain.plan import place_spec

            spec = place_spec(text) if verb == "window_action" else None
            if spec is not None:
                out.update(action="place", monitor=spec[0], how=spec[1])   # P1.4: place:<monitor>[:<how>]
            elif verb == "window_action":
                out["action"] = " ".join(text.split()).casefold()
            elif verb in PRIMARY_FIELD:
                out[PRIMARY_FIELD[verb]] = text
            elif verb == "hotkey":
                out["keys"] = [k.strip() for k in text.split("+") if k.strip()]
            elif verb == "scroll":
                out["dy"] = _number(text, int)
            elif verb == "set_slider":
                number = _number(text, float)
                out["by" if text.strip()[:1] in "+-" else "to"] = number
            elif verb == "app_volume":
                app = words(field_of(field_of(self.head, "target", {}), "app", ""))
                if not app:
                    raise ValueError("app_volume names the app in its value: {\"app\": ..., \"level\": ...}")
                plain = text.strip().casefold()
                if plain in ("mute", "unmute"):   # the plan's fixed words for the mixer's mute (bCore's validator)
                    out.update(app=app, mute=plain == "mute")
                else:
                    number = _number(text, float)
                    out.update(app=app, **({"change": number} if text.strip()[:1] in "+-" else {"level": number}))
            elif verb == "run_skill":
                raise ValueError("run_skill's value is its slots as a JSON object")
            elif verb == "click":
                plain = text.strip().casefold()
                if plain in CLICK_VALUES:
                    out.update(CLICK_VALUES[plain])
                else:
                    raise ValueError(f"click's value is one of {', '.join(CLICK_VALUES)} (or none), not {text[:40]!r}")
            elif verb == "focus_window":
                pass   # its window is the step's own field
            else:
                raise ValueError(f"{verb} can't take that value")
        if verb == "type_text":
            out.setdefault("text", "")
            out["press_enter"] = bool(field_of(step, "submit", False))   # Enter only by the plan's own submit
        if verb == "run_skill":
            slots = obj.get("slots") if obj is not None and isinstance(obj.get("slots"), dict) else (
                {k: v for k, v in (obj or {}).items() if k not in ("skill", "place")} if obj is not None else {})
            out = {"skill": words((obj or {}).get("skill")), "slots": slots}
            if words((obj or {}).get("place")):
                # P1.4: where the skill's app window goes before anything is typed (``engine.place_hint``)
                out["place"] = words((obj or {}).get("place"))
        return out

    def _fetch_call(self, index: int, step: Any) -> Call | None:
        """A ``fetch`` step as the Dispatcher's ``fetch_url`` (an http(s) address) or ``read_file`` (a path)."""
        value = field_of(step, "value", None)
        obj = _json_object(value)
        target = words((obj or {}).get("url") or (obj or {}).get("path") or value)
        if not target:
            return None
        from urllib.parse import urlparse

        try:
            web = urlparse(target).scheme.casefold() in ("http", "https")
        except ValueError:
            web = False
        if web:
            return Call(tool="fetch_url", input={"url": target}, index=index)
        start = (obj or {}).get("start_line")
        return Call(tool="read_file", input={"path": target, **({"start_line": start} if isinstance(start, int) else {})},
                    index=index)

    def _fetch_commits(self, step: Any) -> bool:
        """M9: a web fetch after a file, clipboard or memory read in this request (or earlier in this plan) is a
        may-commit step: it waits for the commit check and runs as an act."""
        if self._verb(step) != "fetch":
            return False
        call = self._fetch_call(-1, step)
        if call is None or call.tool != "fetch_url":
            return False
        earlier = self._reads_now()
        for other in self.steps:
            if other is step:
                break
            kind = self._read_kind(other)
            if kind:
                earlier.add(kind)
        return bool(earlier)

    # -- live facts --------------------------------------------------------------------------------------------------

    def _all_words(self) -> list[str]:
        """The user's own words for this request: the host's (runtime / bridge provenance) and what they added while
        it ran."""
        return list(self.words)

    def _live(self, call: Call) -> Live:
        """The code facts for one step, read now: the game in front, the calling windows, Yuki's typed text waiting,
        this request's reads, the join controls, and for a send the user's recent messages to that person."""
        board = _board()
        recent: tuple[Mapping[str, Any], ...] = ()
        if call.tool == "run_skill":
            c = consent.plan_consent(self.head)
            if c["commit"] == "send" and c["to"]:
                recent = tuple(self._recent_to(c["to"], c["quote"]))
        return Live(game=consent.game_now(board), calling=consent.calling_windows(board),
                    typed_pending=frozenset(self.typed_pending), reads=frozenset(self._reads_now()),
                    join_controls=self.join_controls, recent=recent, people_in=self._people_in)

    def _verdict_for(self, call: Call, live: Live) -> CheckVerdict | None:
        """The commit check's verdict for this step on this window: the plan's batch when it covered this very step
        on this very window and element, the memo, else a one-step check now. ``None`` when the step can't commit."""
        if not consent.may_commit(call, live, self.head):
            return None
        typed = bool(call.hwnd) and int(call.hwnd) in self.typed_pending
        # a batch verdict holds only for the words it was given: after a steer or added words, the step is checked anew
        got = self.batch.get(call.index) if self._batch_words == len(self.words) else None
        if got is not None and not got.failed and int(got.hwnd or 0) == int(call.hwnd or 0) and \
                self._batch_typed.get(call.index, typed) == typed:
            planned = self._predicted_key(call.index)
            if planned is None or planned == (call.role.casefold(), words(call.name).casefold()):
                return got
        cached = self.checker.cached(call, typed=typed, words=self._all_words())
        if cached is not None:
            return cached
        return self.checker.run(self._all_words(), [call], earlier=self.earlier, plan=self.head,
                                typed_pending=dict(self.typed_pending)).get(call.index)

    def _predicted_key(self, index: int) -> tuple[str, str] | None:
        if not 0 <= index < len(self.steps):
            return None
        element = field_of(self.steps[index], "element", None)
        if element is None:
            return None
        return str(field_of(element, "role", "") or "").casefold(), words(field_of(element, "name", "")).casefold()

    def _people_in(self, text: str) -> list[list[str]] | None:
        """Memory's people index: each person named in ``text`` with every name they go by (``None``: memory can't
        say)."""
        memory = self.host.memory
        call = getattr(memory, "call", None)
        if not callable(call):
            return None

        def go(client: Any) -> list[list[str]]:
            from yuki.memory.contacts import named_in

            return [list(c.names) for c, _matched in named_in(getattr(client, "store", client), text)]

        try:
            got = call("brain_people", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return None
        return got if isinstance(got, list) else None

    def _recent_to(self, to: str, quote: str) -> list[dict[str, Any]]:
        """The user's last (at most 3) dated messages to the person ``to`` names, newest first (memory's capture of
        their chats; ``[]`` when memory can't say)."""
        memory = self.host.memory
        call = getattr(memory, "call", None)
        if not callable(call):
            return []

        def go(client: Any) -> list[dict[str, Any]]:
            from yuki.memory.contacts import named_in, resolve, samples, tokens

            store = getattr(client, "store", client)
            people = [c for c, _m in named_in(store, quote) if any(tokens(n) == tokens(to) for n in c.names)]
            people = people or resolve(store, to)
            rows: list[dict[str, Any]] = []
            for person in people[:1]:
                for route in person.routes:
                    rows += samples(store, route, 3)
            rows.sort(key=lambda r: -float(r.get("at") or 0.0))
            return rows[:3]

        try:
            got = call("brain_recent", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return []
        return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []

    def _takes_marker(self, call: Call) -> bool:
        """The acting marker goes up before a step that takes focus, sends input or acts on a window (Traits
        ``needs``); a sound-only step (the mixer, a media key, a skill whose routes neither take focus nor send input)
        publishes none (D18; the engine's ``on_focus`` rule)."""
        if call.tool == "run_skill":
            sk = util.skill(consent.skill_input(call, self.head)[0])
            routes = list(getattr(sk, "routes", ()) or ())
            return not routes or any(getattr(r, "needs_focus", True) or getattr(r, "game", "never") != "always"
                                     for r in routes)
        if call.tool in ("press", "hotkey"):
            keys = call.input.get("keys") if call.tool == "hotkey" else [call.input.get("key")]
            if keys and all(isinstance(k, str) and k.strip().casefold() in util._SYSTEM_KEYS for k in keys):
                return False
        return tool_traits(call.tool, call.input).needs in ("input", "focus", "window")

    @staticmethod
    def _typed_step(typed: dict[int, str], call: Call, *, effect: str = "") -> None:
        """What one step on a window does to Yuki's unsent text there (``typed``: window -> text, changed in place).
        The one rule :meth:`_after_act` applies after a step ran and :meth:`_batch_check` simulates before the steps
        run. The text stops waiting only when the step sent for sure (typed with Enter or a line break, a click while
        it waits: ``sends_for_certain``) or the commit check said this very step sends (``effect = send``; the batch
        can't know that yet, so it keeps the text). Any other key or click leaves it waiting (a Tab, an emoji button,
        shift+Enter send nothing), as the old loop's ``_typed`` did. ``type_text`` without Enter adds its text
        (``clear`` empties the field first). A step with no window (a ``run_skill``) changes nothing here."""
        if not call.hwnd:
            return
        hwnd = int(call.hwnd)
        if effect == "send" or sends_for_certain(consent.traits_of(call, Live(typed_pending=frozenset(typed)))):
            typed.pop(hwnd, None)
        elif call.tool == "type_text":
            held = "" if call.input.get("clear") else typed.get(hwnd, "")
            typed[hwnd] = (held + str(call.input.get("text") or ""))[-2000:]

    def _after_act(self, call: Call, outcome: Any, *, effect: str = "", verdict: bool | None = None) -> None:
        """What an act leaves for the next steps' code facts: Yuki's unsent text waiting in a window (a click there may
        send it). A step on a window follows :meth:`_typed_step`. A ``run_skill`` has no window of its own: see
        :meth:`_after_skill`."""
        if call.tool == "run_skill":
            self._after_skill(call, outcome, verdict)
            return
        if not getattr(outcome, "ok", False) or not call.hwnd:
            return
        self._typed_step(self.typed_pending, call, effect=effect)

    def _after_skill(self, call: Call, outcome: Any, verdict: bool | None) -> None:
        """A capability that ran with text in its text slot: when it sent for sure and its own check saw it, its chat
        holds nothing of Yuki's any more; otherwise (a draft, or a send its check couldn't see) that text waits in its
        chat window, so a later key or click there is judged as one that may send it (rows 4-5). The chat window: the
        capability's own ``details.hwnd`` when it reports one, else the window in front when it is the capability's
        app, else that app's listed windows. Never raises."""
        try:
            payload = outcome.payload if isinstance(getattr(outcome, "payload", None), dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            if payload.get("refused") or details.get("refused") or str(payload.get("outcome") or "") in HELD_OUTCOMES:
                return   # held before it acted: nothing was typed
            key, slots = consent.skill_input(call, self.head)
            sk = util.skill(key)
            name = consent.text_slot(sk) if sk is not None else ""
            text = words(slots.get(name)) if name else ""
            sent = bool(getattr(outcome, "ok", False)) and verdict is True and consent.skill_sends(sk, slots,
                                                                                                    partial=False)
            if not text and not sent:
                return
            front, _entry, fits, listed = self._skill_windows(outcome, key, slots)
            own = done.as_hwnd(details.get("hwnd"))
            windows = {own} if own is not None else (
                ({front} if front is not None and fits is not False else set()) | (set(listed) if fits is not True
                                                                                     else set()))
            for hwnd in windows:
                if sent:
                    self.typed_pending.pop(int(hwnd), None)
                else:
                    self.typed_pending[int(hwnd)] = text[-2000:]
        except Exception:  # noqa: BLE001 - bookkeeping; a missed record leaves the next click to the commit check
            pass

    def _skill_windows(self, outcome: Any, key: str, slots: Mapping[str, Any]
                       ) -> tuple[int | None, dict[str, Any], bool | None, list[int]]:
        """Where a capability's chat is now: ``(the window in front, its window-list entry, whether it is the
        capability's app, every listed window of that app)``. The app is its route's own ``app`` (the route the run
        took, by ``payload.route``), else its app slot, else the capability's app; its programs are that app's
        connection data (``[chat] processes`` / ``images``). Fit ``None``: the data names no program. When the
        capability reports its window (``details.hwnd``), the fit also needs that window in front."""
        payload = outcome.payload if isinstance(getattr(outcome, "payload", None), dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        now = done.overview(self.dispatcher, lock=self._dispatch_lock)
        self._last_overview = now or self._last_overview
        front = done.as_hwnd(now.get("foreground_hwnd"))
        windows = [w for w in now.get("windows") or [] if isinstance(w, dict) and done.as_hwnd(w.get("hwnd"))]
        entry = next((w for w in windows if done.as_hwnd(w.get("hwnd")) == front), {}) if front else {}
        programs = self._skill_programs(payload, key, slots)
        from yuki.memory.store import app_key

        fits: bool | None = None
        if programs:
            fits = bool(entry) and app_key(str(entry.get("process_name") or "")) in programs
        own = done.as_hwnd(details.get("hwnd"))
        if own is not None:
            fits = own == front and fits is not False
        listed = [int(done.as_hwnd(w.get("hwnd")) or 0) for w in windows
                  if programs and app_key(str(w.get("process_name") or "")) in programs]
        return front, dict(entry), fits, [h for h in listed if h]

    @staticmethod
    def _skill_programs(payload: Mapping[str, Any], key: str, slots: Mapping[str, Any]) -> frozenset[str]:
        """The programs (app keys) of the app a capability's run acted in, from catalog data (module docstring of
        :meth:`_skill_windows`); empty when the data names none. Never raises."""
        try:
            sk = util.skill(key)
            if sk is None:
                return frozenset()
            app = ""
            taken = str(payload.get("route") or "")
            for route in getattr(sk, "routes", ()) or ():
                if taken and str(getattr(route, "key", "") or "") == taken:
                    app = str((getattr(route, "args", None) or {}).get("app") or "")
                    break
            if not app:
                slot = next((sl.name for sl in getattr(sk, "slots", ()) or () if getattr(sl, "type", "") == "app"), "")
                app = words(slots.get(slot)) if slot else ""
            app = app or str(getattr(sk, "app", "") or "")
            from yuki.memory.store import app_key
            from yuki.skills.chat import app_data

            data = app_data(app) if app else {}
            names = [str(n) for n in list(data.get("processes") or []) + list(data.get("images") or [])]
            return frozenset(k for k in (app_key(n) for n in names) if k)
        except Exception:  # noqa: BLE001 - no data: the fit is unknown
            return frozenset()

    # -- reads -------------------------------------------------------------------------------------------------------

    def _read_kind(self, step: Any) -> str:
        """What a read step reads, for M9: ``file`` (a fetch of a path), ``clipboard`` (a look at it), ``memory``."""
        verb = self._verb(step)
        if verb == "memory":
            return "memory"
        if verb == "look" and words(field_of(step, "value", "")).casefold() == "clipboard":
            return "clipboard"
        if verb == "fetch":
            call = self._fetch_call(-1, step)
            return "file" if call is not None and call.tool == "read_file" else ""
        return ""

    def _start_read(self, index: int, step: Any) -> None:
        """A read starts now, beside the others of this plan (window reads one at a time on the dispatcher)."""
        kind = self._read_kind(step)
        if kind:
            self.reads_seen.add(kind)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="yuki-brain-read")
        self.pending_reads.append((index, self._pool.submit(self._read, index, step)))

    def _join_reads(self) -> None:
        """Wait for this plan's reads, and keep what they returned (for the planner) and their statuses."""
        pending, self.pending_reads = self.pending_reads, []
        for index, future in pending:
            try:
                got = future.result()
            except Exception as exc:  # noqa: BLE001
                got = {"i": index, "verb": "read", "ok": False, "text": f"{type(exc).__name__}"}
            self.read_results.append(got)
            if 0 <= index < len(self.statuses):
                s = self.statuses[index]
                s.state = "ok" if got.get("ok") else "failed"
                # the read's text as returned (capped at READ_CHARS before any fence went on: cut it no further)
                s.tool, s.ms, s.result = str(got.get("tool") or ""), float(got.get("ms") or 0.0), str(
                    got.get("text") or "")

    def _read(self, index: int, step: Any) -> dict[str, Any]:
        """One read step on its own thread: ``{"i", "verb", "tool", "ok", "text", "ms"}`` (outside text fenced as
        untrusted data)."""
        from yuki.agent.untrusted import EXTERNAL_TEXT_TOOLS, source_for, wrap

        verb = self._verb(step)
        started_at, t0 = time.time(), time.perf_counter()
        tool, tool_input, outcome = "", {}, None
        try:
            if verb == "memory":
                tool = "memory"
                tool_input = _json_object(field_of(step, "value", None)) or {"kind": "recall",
                                                                             "query": words(field_of(step, "value", ""))}
                outcome = self.host.memory.run_tool("memory", tool_input, request=" ".join(self.words)[:600])
            elif verb == "fetch":
                call = self._fetch_call(index, step)
                if call is None:
                    raise ValueError("a fetch needs an address or a path")
                tool, tool_input = call.tool, call.input
                outcome = self.dispatcher.dispatch(tool, tool_input)
            else:
                what = words(field_of(step, "value", "")).casefold() or (
                    "window" if field_of(step, "window", None) else "desktop")
                tool = LOOK_TOOLS.get(what, "look_at_window")
                if tool in ("look_at_window", "read_page", "read_tabs"):
                    hwnd, _entry = done.window_of(self.dispatcher, field_of(step, "window", None),
                                                    lock=self._dispatch_lock)
                    if hwnd is None:
                        hwnd = self._front(done.overview(self.dispatcher, lock=self._dispatch_lock))
                    if hwnd is None:
                        raise ValueError("no window to read")
                    tool_input = {"hwnd": hwnd}
                with self._dispatch_lock:
                    outcome = self.dispatcher.dispatch(tool, tool_input)
        except Exception as exc:  # noqa: BLE001 - a read that broke says so
            ms = round((time.perf_counter() - t0) * 1000, 1)
            return {"i": index, "verb": verb, "tool": tool, "ok": False, "text": f"{type(exc).__name__}: {exc}"[:300],
                    "ms": ms}
        ms = round((time.perf_counter() - t0) * 1000, 1)
        text = "\n".join(str(b.get("text") or "") for b in getattr(outcome, "content", None) or []
                         if isinstance(b, dict) and b.get("type") == "text")
        payload = getattr(outcome, "payload", None)
        if tool == "look_at_desktop" and getattr(outcome, "ok", False) and isinstance(payload, Mapping) \
                and payload.get("windows"):
            from yuki.brain.context import SITUATION_LOOK_CHARS, desk_text

            text = desk_text(text, payload, limit=READ_CHARS, situation_chars=SITUATION_LOOK_CHARS)   # P1.1: the desk in plain words, then the Now block
        text = (text or str(getattr(outcome, "summary", "") or ""))[:READ_CHARS]
        if tool in EXTERNAL_TEXT_TOOLS:   # capped first, so the fence stays whole
            text = wrap(text, source=source_for(tool, tool_input), nonce=self.nonce)
        ok = bool(getattr(outcome, "ok", False))
        self._trace_step(Call(tool=tool, input=dict(tool_input), index=index, hwnd=done.as_hwnd(tool_input.get("hwnd"))),
                         outcome, ok=ok, verdict=None, why="", ms=ms, started=started_at,
                         kind="memory" if verb == "memory" else "read")
        return {"i": index, "verb": verb, "tool": tool, "ok": ok, "text": text, "ms": ms}

    def _wait(self, index: int, step: Any) -> RunResult | None:
        """A ``wait`` step: a pause of at most :data:`WAIT_MAX_S` (its value in seconds), ended early by a cancel."""
        stop = self._checkpoint()
        if stop is not None:
            return stop
        try:
            seconds = min(WAIT_MAX_S, max(0.0, float(str(field_of(step, "value", "1") or "1").strip())))
        except ValueError:
            seconds = 1.0
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.host.cancelled():
            threading.Event().wait(min(0.2, max(0.0, end - time.monotonic())))
        status = self.statuses[index]
        status.state, status.tool, status.ms = "ok", "wait", round(seconds * 1000, 1)
        return RunResult("cancelled") if self.host.cancelled() else None

    # -- the done-check ----------------------------------------------------------------------------------------------

    def _done_check(self) -> RunResult:
        """Every step went as planned: the plan's one done-check, the code line, coverage, the background re-check."""
        cls = done.plan_class(self.head, self.seed)
        acted = {"hwnd": self.last_act.get("hwnd"), "title": self.last_act.get("title") or "",
                 "process_name": self.last_act.get("app") or ""} if self.last_act.get("hwnd") else {}
        env = done.ProbeEnv(since=self.started, dispatcher=self.dispatcher,
                            memory=self.host.memory, last_verdict=self.last_act.get("verdict"),
                            baseline=dict((self.seed or {}).get("baseline") or {}), acted=acted,
                            lock=self._dispatch_lock)
        deciding: _SkillRun | None = None
        if cls == "skill" and self.seed and self.seed.get("done") and not str(
                field_of(self.head, "route", "")).startswith("skill:"):
            result = done.seed_done(self.seed, env)
        elif cls in ("skill", "send", "join"):
            deciding = self._deciding_skill(cls)
            if deciding is None:
                # nothing was done by the capability that decides this plan: never "I did it"
                acted_any = any(s.get("kind") == "action" for s in self.trace)
                result = done.DoneResult(None, util.UNVERIFIED,
                                         done.LINE_NO_CAPABILITY[cls] if acted_any else util.LINE_NOTHING_DONE,
                                         seen="its capability didn't run", probe="skill")
            else:
                sends = consent.skill_sends(util.skill(deciding.key), deciding.slots, partial=False)
                result = done.skill_done(deciding.outcome, drafted=deciding.drafted, sends=sends,
                                         type_after=deciding.type_after, typed=deciding.typed)
        else:
            result = self._pick_done(env)
            if field_of(self.head, "seed_also", False) and self.seed_also and result.probe != done.PARTS:
                result = self._seed_also(result, env)
        if cls in ("skill", "join") and not (deciding is not None and deciding.drafted):
            # kBrain review M2: the request's other parts their own skills saw done (this plan's, and earlier plans' a
            # later part stopped) are said first; a send's or a draft's line stands alone (consent's own words)
            result.line = done.with_parts(self._done_parts(exclude=deciding), result.line)
        self._last_done = result
        self._emit_log("done_check", check=result.probe, result={True: "done", False: "false", None: "unverified"}[
            result.verdict], level=result.level, covered=len(result.covered), uncovered=len(result.uncovered))
        # what the check looked for in code's words, never the planner's goal text (x9: it called the user "they"); a
        # request of parts already says each part in its own words (the probe's part as "couldn't confirm")
        parted = result.probe == done.PARTS
        unconfirmed = (result.line if parted else done.not_confirmed(
            seen=result.seen, probe=result.probe, args=result.args, window_entry=result.window)) \
            if result.verdict is False else ""
        if result.verdict is False and cls == "pick" and not self._done_replan and self.replans < self.max_replans:
            self._done_replan = True   # one replan with what showed; still false after it: "I couldn't confirm"
            self.last_failed = {"step": None, "verb": "done", "words": "the final check", "seen": result.seen}
            # ``line``: said if the request ends here after all (no replan left): the same honest line, never the
            # mismatch's code word ("I got stuck partway: done_false")
            return RunResult("mismatch", plan_id=self.plan_id, statuses=list(self.statuses), done=result,
                             mismatch=Mismatch(step=len(self.steps) - 1, verb="done", why="done_false",
                                               expected=f"{result.probe} true", seen=result.seen,
                                               view=self._view(self.last_act.get("hwnd"))), line=unconfirmed)
        if result.verdict is False and cls == "pick" and not parted:
            result.line = unconfirmed
        self._start_recheck(result, env)
        final = done.outcome(result, self.trace, step_words=self._step_words(), refused=self._refused_count())
        return RunResult("done", plan_id=self.plan_id, statuses=list(self.statuses), done=result, line=result.line,
                         final=True, outcome=final, reads=list(self.read_results))

    def _stopped_line(self, index: int, call: Call) -> str:
        """hands19 (final-B all-messaging RC5): the line for an act step the input gate stopped with no game in front,
        from code facts: the stopped step's verb and the app it acts in (its target window's program, else the plan's
        target app; :data:`STOPPED_WHAT_APP`), and that the plan's act steps after it were not done (each in the
        island's words, :meth:`_step_said`; :data:`LINE_INPUT_STOPPED_REST`). Never the plan's free text. Never
        raises: the old line when anything is missing."""
        try:
            what, shown = self._stop_words(index, call)
            if not shown:
                return LINE_INPUT_STOPPED.format(what=what)
            return LINE_INPUT_STOPPED_REST.format(what=what, rest=shown)
        except Exception:  # noqa: BLE001 - the plain line
            return LINE_INPUT_STOPPED.format(what=STOPPED_WHAT.get(call.tool, "there"))

    def _stop_words(self, index: int, call: Call) -> tuple[str, str]:
        """``(what, rest)``: a step that did not go, in code's words (shared by :meth:`_stopped_line` and, desk20 S20,
        the stuck ending): the step's verb and the app it acts in (its target window's program, else the plan's target
        app; :data:`STOPPED_WHAT_APP`), and the plan's act steps after it that were not done (each in the island's
        words, :meth:`_step_said`, at most :data:`STOPPED_REST_MAX`; ``""`` none). Never the plan's free text. May
        raise (callers keep their own plain line)."""
        target = field_of(field_of(self.head, "target", None), "app", None) if self.head is not None else None
        app = util.app_name(call.app) if call.app else (words(target) if target else "")
        app = "" if app in ("", "It") else app
        what = (STOPPED_WHAT_APP[call.tool].format(app=app) if app and call.tool in STOPPED_WHAT_APP
                else STOPPED_WHAT.get(call.tool, "there"))
        rest: list[str] = []
        for step in list(self.steps)[index + 1:]:
            if self._verb(step) not in ACT_VERBS:
                continue
            said = _mid(self._step_said(step))
            if said and said not in rest:
                rest.append(said)
        shown = ", ".join(rest[:STOPPED_REST_MAX]) + (", and more" if len(rest) > STOPPED_REST_MAX else "") \
            if rest else ""
        return what, shown

    def _note_stuck(self, index: int, call: Call) -> None:
        """desk20 (final-D S20, k_t3_draft: "I got stuck typing. Try another way?" named neither T3 Code nor what was
        left): an act step whose own report said it did not take is kept in code's words (:meth:`_stop_words`), so a
        request that ends stuck on it names its app and what was left undone (:meth:`ending_facts`). Never raises."""
        try:
            what, rest = self._stop_words(index, call)
            self._stuck_said = {"what": what, "rest": rest} if what and what != "there" else None
        except Exception:  # noqa: BLE001 - the plain stuck line then
            self._stuck_said = None

    def _seed_also(self, own: done.DoneResult, env: done.ProbeEnv) -> done.DoneResult:
        """hands19 (final-B all-spotify RC3: "Touch grass should be on, but I couldn't confirm it" while the harness saw
        it playing): a seeded plan by steps whose hand-off check is a skill driver's own (``Plan.seed_also``) has that
        check read now, at the finish, before its line is worded (``done.seed_done``: the skill's own done words), and
        the stronger verdict is kept (``plan.stronger_done``: verified beats likely; never over the plan's own "not
        so"). Only when the plan's own check didn't already read it verified. :attr:`seed_read` marks it read, so the
        brain's after-the-line observer doesn't read it a second time. Never raises."""
        held = dict(self.seed_also or {})
        if own.verdict is False or own.level == util.VERIFIED or not held.get("done") or not words(
                held.get("done_words")):
            return own   # a seed with no done words of its own would say "couldn't confirm" at a verified level
        try:
            seeded = done.seed_done(held, env)
        except Exception as exc:  # noqa: BLE001 - the plan's own check stands
            self._emit_log("done_seed_also", error=type(exc).__name__)
            return own
        self.seed_read = True
        try:
            from yuki.brain.plan import stronger_done

            better = stronger_done(own, seeded)
        except ImportError:   # a branch without bCore's plan.py: the same rule
            better = seeded.verdict is True and seeded.level == util.VERIFIED
        self._emit_log("done_seed_also", own=own.level, seed=seeded.level, kept="seed" if better else "plan",
                       check=seeded.probe)
        return seeded if better else own

    def _part(self, run: _SkillRun, *, several: bool = True) -> done.DoneResult:
        """One ``run_skill`` part of this plan, by its own skill's check (``several``: the request has other parts)."""
        sends = consent.skill_sends(util.skill(run.key), run.slots, partial=False)
        return done.part_result(run.outcome, drafted=run.drafted, sends=sends, type_after=run.type_after,
                                typed=run.typed, several=several)

    def _done_parts(self, exclude: _SkillRun | None = None) -> list[str]:
        """The lines of the request's ``run_skill`` parts whose own check saw them done, each once: earlier plans'
        (kept by :meth:`begin`) and this plan's, ``exclude`` aside (the part the line itself is about)."""
        out = list(self._parts_before)
        for run in self.skill_outcomes:
            if run is exclude:
                continue
            try:
                result = self._part(run)
            except Exception:  # noqa: BLE001 - a part not read is not said
                continue
            if result.verdict is True and result.line and result.line not in out:
                out.append(result.line)
        return out

    def _pick_done(self, env: done.ProbeEnv) -> done.DoneResult:
        """A plan the planner checks (no skill route, no send or join): each ``run_skill`` part by its own skill's
        check, and the plan's ``done`` probe for the part done by steps (said as a part: what it looked for). A plan
        whose every act step is a skill and whose ``done`` is none is decided by its parts alone (kBrain: a request of
        two parts, a playlist and a level, is one plan with a part per skill, each checked by its own skill)."""
        runs = list(self.skill_outcomes)
        before = self._parts_before
        probe, args, _why = done.plan_done(self.head)
        only_skills = all(self._verb(s) == "run_skill" for s in self.steps if self._verb(s) in ACT_VERBS)
        if (runs or before) and probe == done.NONE and only_skills:
            several = len(runs) + len(before) > 1
            return done.parts_done([self._part(run, several=several) for run in runs], before=before)
        entry = {"process_name": self.last_act.get("app") or "", "title": self.last_act.get("title") or ""}
        wrote_none = probe == done.NONE
        derived = self._derived_probe() if wrote_none else None
        if derived is not None:
            # aBrain (phase 2.5): a plan by steps with no probe is read by what its last readable act changed, so
            # "couldn't confirm" means it truly couldn't be read
            probe, args = derived   # its arguments already as the check reads them (_probe_args)
            self._emit_log("done_derived", check=probe)
        elif args is not None:
            # x9: page_at reads the page of the browser this request named (never a page in another browser)
            args = self._probe_args(probe, args)
        result = done.pick_done(self.head, env, hwnd=self.last_act.get("hwnd"), last_window=entry,
                                cancelled=self.host.cancelled, args=args,
                                probe=probe if derived is not None else None)
        if derived is not None and result.verdict is True:
            result = self._derived_rest(result)
        if derived is not None and result.verdict is False:
            # the planner didn't pick this read, so a "not so" from it is evidence for the line, never a replan
            result = done.DoneResult(None, util.UNVERIFIED, done.not_confirmed(
                seen=result.seen, probe=result.probe, args=result.args, window_entry=result.window),
                seen=result.seen, probe=result.probe, args=result.args,
                evidence={**dict(result.evidence or {}), "derived": "not_so"}, window=result.window)
        if wrote_none and result.verdict is None and not runs and result.evidence.get("derived") != "not_so":
            # what each step's own report saw stands only when the derived read couldn't read (never over its "not so")
            result = self._steps_seen() or result
        elif result.verdict is True and result.uncovered:
            result = self._pages_cover(result) or result
        if not runs and not before:
            return result
        if result.verdict is False:
            # the part done by steps, as a part: what its probe looked for, in code's words (never the goal text)
            result.line = done.not_confirmed(seen=result.seen, probe=result.probe, args=result.args,
                                             window_entry=result.window)
        return done.parts_done([*(self._part(run) for run in runs), result], before=before)

    def _derived_probe(self) -> tuple[str, dict[str, Any]] | None:
        """aBrain (phase 2.5): the probe that reads what this plan's last act step changed, for a plan by steps that
        wrote done none: ``(probe, args)`` from that step's verb, its closed values and the call's own facts (its
        window, its process), the arguments as the check reads them. ``None`` when that step has no readable effect (a
        click, a key, a command: their own verdicts stand), or its program can't be told. Code facts only."""
        index = self._last_ran()
        if index is None:
            return None
        # only the last act step: a later step (a click after typing, a focus back after a focus) may have changed
        # what an earlier one did, so an earlier step's read would check something that no longer holds
        got = _effect_probe(self._ran_calls[index], self.steps[index] if index < len(self.steps) else None,
                            self._open_images.get(index, ""))
        if got is None:
            return None
        if got[0] == "page_at" and (index in self._open_pages or len(self._seen_pages() or ()) > 1):
            # its own report confirmed the page (review L: a redirect or a page still loading would read "not so"),
            # or several pages each seen by their own step's report: _steps_seen says them, as before
            return None
        resolved = self._probe_args(*got)
        image = str(resolved.get("process") or "").casefold()
        if "process" in done.PROBE_ARGS.get(got[0], ((), ()))[0] and not image.endswith(".exe"):
            return None   # an app name no listed window resolves to its program would never match the one in front
        return got[0], resolved

    def _last_ran(self) -> int | None:
        """The index of this plan's last act step that ran and took (its call kept), ``None`` when none did."""
        ran = [i for i in sorted(self._ran_calls) if i < len(self.statuses) and self.statuses[i].state == "ok"]
        return ran[-1] if ran else None

    def _derived_rest(self, result: done.DoneResult) -> done.DoneResult:
        """Review M1: a derived read that read true reads only the last act step. The plan is ``verified`` only when
        that step is its only act step (``run_skill`` parts aside: their own checks say them) or every other act step
        was seen to take by its own report (``status.verdict`` True); else ``likely`` with done's "...; I couldn't
        check the rest." and :data:`DERIVED_REST` uncovered, so the evidence says what wasn't read. Code facts only."""
        index = self._last_ran()
        others = [i for i, step in enumerate(self.steps) if i != index and self._verb(step) in ACT_VERBS
                  and self._verb(step) != "run_skill"]
        if all(i < len(self.statuses) and self.statuses[i].verdict is True for i in others):
            return result
        if not result.uncovered:   # a likely one says "I couldn't check the rest." already
            result.line = done.LINE_REST.format(line=str(result.line or "").rstrip(". "))
        result.uncovered = [*result.uncovered, DERIVED_REST]
        result.level = util.LIKELY
        self._emit_log("done_derived_rest", steps=len(others))
        return result

    def ending_facts(self) -> dict[str, str]:
        """aBrain (phase 2.3): what a request that didn't end done can say from the evidence, in plain words for the
        line and the voice's talker (content: never logged). ``did``: the last act step that ran, as done ("Opened a
        page in Arc"); ``stuck``: the step that failed or was refused, as it runs ("clicking Continue"), and ``why``
        when its reason is plain words (a code word is left out); ``claim``: what the done-check looked for ("Excalidraw
        is open in Arc") and ``seen``: what it saw, when it didn't read it done. Only the current plan's steps, except
        desk20's ``stuck_what`` / ``stuck_rest`` (:meth:`_note_stuck`): the request's last act step that did not take,
        with its app ("typing in T3 Code"), and the act steps after it in its plan ("" none)."""
        out: dict[str, str] = {}
        try:
            ran = [i for i, st in enumerate(self.statuses) if st.state == "ok" and i < len(self.steps)
                   and self._verb(self.steps[i]) in ACT_VERBS and self._verb(self.steps[i]) != "run_skill"]
            if ran:
                out["did"] = self._step_said(self.steps[ran[-1]], done=True)
            failed = next((st for st in self.statuses if st.state in ("failed", "refused")), None)
            if failed is not None and failed.index < len(self.steps):
                out["stuck"] = _mid(self._step_said(self.steps[failed.index]))
                why = words(failed.why)
                if why and " " in why and "_" not in why and len(why) <= 120:
                    out["why"] = why   # a code word ("done_false", "input_gate") is for the log and the planner
            result = self._last_done
            if result is not None and result.verdict is not True:
                claim = done.claim_for(result.probe, result.args, window_entry=result.window)
                if claim:
                    out["claim"] = claim
                if result.seen:
                    out["seen"] = words(result.seen)
                # a check that read nothing (None) vs one that read "not so" (False, or a derived read's "not so")
                if result.verdict is None and dict(result.evidence or {}).get("derived") != "not_so":
                    out["unread"] = "yes"
            elif result is not None and result.uncovered:
                # a done that read only part of the target ("Arc is in front; I couldn't check the rest.")
                out["unchecked"] = ", ".join(UNCHECKED_WORDS.get(f, f) for f in result.uncovered)
            if self._stuck_said:
                # desk20 (S20): this request's last act step that did not take, naming its app, and the act steps
                # after it in that plan (any plan of the request, not only the current one)
                out["stuck_what"] = self._stuck_said.get("what", "")
                out["stuck_rest"] = self._stuck_said.get("rest", "")
        except Exception:  # noqa: BLE001 - no facts: the line stands as it was
            return {k: v for k, v in out.items() if v}
        return {k: v for k, v in out.items() if v}

    def _steps_seen(self) -> done.DoneResult | None:
        """fBrain: a plan by steps that wrote done none (so nothing reads what they asked for) whose every act step ran
        and was seen to take by its own report (``status.verdict`` True: :func:`util.action_verdict`, or the step's
        own expect), each said in its probe's words (:data:`STEP_LINE_PROBES`, from the step's own arguments: "Chatgpt
        is open in Chrome"). Level ``likely``, never ``verified``: the steps were seen, what they asked for was not
        read. ``None`` when any act step wasn't seen (a key, a click with nothing watched) or no step's verb has such
        words: the plan's "couldn't confirm" line stands. Code facts only."""
        pages = self._seen_pages()
        if not pages:
            return None
        lines: list[str] = []
        for _index, args, _title in pages:
            try:
                line = done.line_for("page_at", args, {})
            except (KeyError, IndexError, ValueError):
                continue
            if line and line not in lines:
                lines.append(line)
        if not lines:
            return None
        parts = [done.DoneResult(True, util.LIKELY, line, probe="steps", seen="each step's own report saw it")
                 for line in lines]
        result = done.parts_done(parts)
        result.level = util.LIKELY
        return result

    def _seen_pages(self) -> list[tuple[int, dict[str, str], str]] | None:
        """The pages this plan's open_url steps opened, each ``(index, page_at args, the page's title)`` (``args``: the
        step's own address as ``site``, its browser image as ``process`` when its resolution found one; the title as
        the step's own report confirmed it, ``""`` when it gave none), when every act step ran and was seen to take by
        its own report (``status.verdict`` True); ``None`` when any act step wasn't (a key, a click with nothing
        watched). Code facts only (:data:`STEP_LINE_PROBES`)."""
        out: list[tuple[int, dict[str, str], str]] = []
        for index, step in enumerate(self.steps):
            verb = self._verb(step)
            if verb not in ACT_VERBS:
                continue
            status = self.statuses[index] if index < len(self.statuses) else None
            if status is None or status.state != "ok" or status.verdict is not True:
                return None
            if STEP_LINE_PROBES.get(verb) != "page_at":
                continue
            address = words(field_of(step, "value", ""))
            if not _web_host(address):
                continue
            args = {"site": address}
            image = self._open_images.get(index, "")
            if image:
                args["process"] = image
            out.append((index, args, self._open_pages.get(index, "")))
        return out

    def _note_page(self, index: int, outcome: Any, verdict: bool | None) -> None:
        """An open_url step's page as its own report confirmed it (``details.page_verified`` and the page's title,
        ``launch.open_url``), kept by step index; anything else forgets it. Bookkeeping only; never raises."""
        try:
            payload = outcome.payload if isinstance(outcome.payload, dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            page = details.get("page") if isinstance(details.get("page"), dict) else {}
            if verdict is True and outcome.ok and details.get("page_verified") is True:
                self._open_pages[index] = words(page.get("title"))
            else:
                self._open_pages.pop(index, None)
        except Exception:  # noqa: BLE001 - bookkeeping only
            self._open_pages.pop(index, None)

    def _pages_cover(self, result: done.DoneResult) -> done.DoneResult | None:
        """jBrain (fSkills merge note 2; 18 Oct live A, open_excalidraw: open_url reported "navigation confirmed ...
        page 'Excalidraw Whiteboard'" and the plan's done, reading only the app in front, said "Arc is in front; I
        couldn't check the rest."): a done that read true but left target fields uncovered, in a plan whose every act
        step was seen to take, and whose open_url steps' own reports confirmed pages that cover those fields (the page
        as the target's window by its address or title, the browser as its app: :func:`done.coverage` of ``page_at``
        on the step's own facts). The line then says those pages ("Excalidraw is open in Arc."), level ``likely``
        (the steps saw them; the done read the app). ``None`` when a field stays uncovered (the honest "couldn't check
        the rest" stands). Code facts only."""
        pages = self._seen_pages()
        if not pages:
            return None
        covered = set(result.covered)
        for _index, args, title in pages:
            entry = {"title": title, "process_name": args.get("process", "")} if title else None
            try:
                got, _left = done.coverage("page_at", args, self.head, window_entry=entry)
            except Exception:  # noqa: BLE001 - a page not read covers nothing
                continue
            covered.update(got)
        if any(field not in covered for field in result.uncovered):
            return None
        seen = self._steps_seen()
        if seen is None:
            return None
        seen.covered, seen.uncovered = sorted(covered), []
        self._emit_log("done_pages_cover", pages=len(pages), fields=len(result.uncovered))
        return seen

    def _game_seen(self, call: Call) -> bool:
        """fBrain: whether a game is in front, or is the window this step aimed at, read now through the game gate's
        own reads (``gamegate.game_in_front`` / ``window_game``): the input gate's refusal is then a game's. ``True``
        when the read itself fails (fail closed: the game line, as before). Never raises."""
        try:
            from yuki.memory.gamegate import game_in_front, window_game

            if game_in_front():
                return True
            return bool(call.hwnd and window_game(int(call.hwnd)))
        except Exception:  # noqa: BLE001 - can't tell: as a game
            return True

    def _deciding_skill(self, cls: str) -> _SkillRun | None:
        """The ``run_skill`` step whose capability's own check decides the plan. A send plan: the last one that sent
        for sure with its slots, or was the send's draft. A join plan: the last one whose capability ends in joining
        a call. A skill plan: the route's skill, else the last one. ``None`` when no such step ran (the line is then
        "couldn't confirm", never another capability's done)."""
        if not self.skill_outcomes:
            return None
        if cls == "send":
            return next((r for r in reversed(self.skill_outcomes) if r.drafted or consent.skill_sends(
                util.skill(r.key), r.slots, partial=False)), None)
        if cls == "join":
            return next((r for r in reversed(self.skill_outcomes) if consent.skill_joins(util.skill(r.key))), None)
        route = words(field_of(self.head, "route", ""))
        key = route.split(":", 1)[1].strip() if route.startswith("skill:") else ""
        for run in reversed(self.skill_outcomes):
            payload = getattr(run.outcome, "payload", None) or {}
            ran = str(payload.get("skill") or "") if isinstance(payload, dict) else ""
            if key and key in (ran, run.key):
                return run
        return self.skill_outcomes[-1]

    def _start_recheck(self, result: done.DoneResult, env: done.ProbeEnv) -> None:
        """The second look for a probe that can flip; a disagreement is one correcting line and a false done logged."""
        def on_result(verdict: bool | None) -> None:
            word = {True: "agrees", False: "false_done", None: "unverified"}[verdict]
            if verdict is False and self._tidy_changed:
                # review L6: a tidy-up step (place, focus back) changed the desk after the done: a probe that reads the
                # front or a window's state reads that, not a false done
                word, verdict = "after_tidy", None
            self._recheck_verdict = {"recheck": word}
            self._emit_log("done_recheck", check=result.probe, result=word)
            if verdict is False:
                late = getattr(self.host, "late_line", None)
                if self._closed and callable(late):   # the request already ended: the host's late line (bCore's)
                    try:
                        late(done.LINE_RECHECK)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    self._emit(Say(done.LINE_RECHECK))

        self._recheck = done.recheck_later(result, env, on_result)

    # -- mismatches --------------------------------------------------------------------------------------------------

    def _mismatch_result(self, index: int, why: str, *, refused: bool = False, seen: str = "",
                         call: Call | None = None, seed: dict[str, Any] | None = None,
                         seen_fenced: bool = False, line: str = "") -> RunResult:
        """A step that didn't go as planned: its status, the planner's mismatch (with that window's view), and the
        failed step kept for the "same step again" rejection. ``seen_fenced``: ``seen`` is outside text already
        capped and fenced (it is not cut again). ``line``: code's plain line for the user if the request ends on this
        step (the Brain says it instead of a stuck line built from ``why``); ``""``: none of its own."""
        status = self.statuses[index] if 0 <= index < len(self.statuses) else None
        if status is not None and status.state == "pending":
            status.state, status.why = ("refused" if refused else "failed"), why
        step = self.steps[index] if 0 <= index < len(self.steps) else {}
        hwnd = call.hwnd if call is not None else None
        # the stuck line is said to the user: outside text (fenced) never goes into it, only the reason
        self.last_failed = {"step": step, "verb": self._verb(step), "seen": why if seen_fenced else (seen or why),
                            "words": self._step_said(step) or self._verb(step)}
        expected = ""
        expect = field_of(step, "expect", None)
        if expect is not None:
            args = field_of(expect, "args", "")
            args = json.dumps(dict(args), ensure_ascii=False, default=str) if isinstance(args, Mapping) else str(args)
            expected = f"{done.probe_name(expect, '')} {args}".strip()
        fenced = seen_fenced and bool(seen)
        mismatch = Mismatch(step=index, verb=self._verb(step), expected=expected,
                            seen=seen if fenced else (seen or why)[:DIFF_CHARS], view=self._view(hwnd) if hwnd else "",
                            why=why, refused=refused, seed=seed, seen_fenced=fenced)
        self._emit_log("brain_mismatch", verb=self._verb(step), why=_code(why), refused=refused)
        return RunResult("mismatch", plan_id=self.plan_id, statuses=list(self.statuses), mismatch=mismatch,
                         reads=list(self.read_results), line=line)

    def _view(self, hwnd: int | None) -> str:
        """That window as a look shows it now (for the replan), capped."""
        if not hwnd:
            return ""
        try:
            with self._dispatch_lock:
                outcome = self.dispatcher.dispatch("look_at_window", {"hwnd": int(hwnd)})
        except Exception:  # noqa: BLE001
            return ""
        text = "\n".join(str(b.get("text") or "") for b in getattr(outcome, "content", None) or []
                         if isinstance(b, dict) and b.get("type") == "text")
        from yuki.agent.untrusted import source_for, wrap

        return wrap(text[:VIEW_CHARS], source=source_for("look_at_window", {"hwnd": hwnd}), nonce=self.nonce) \
            if text else ""

    # -- events, trace, run row --------------------------------------------------------------------------------------

    def _finished(self, result: RunResult) -> RunResult:
        result.plan_id = result.plan_id or self.plan_id
        result.statuses = result.statuses or list(self.statuses)
        result.reads = result.reads or list(self.read_results)
        if result.status != "done" and not result.parts:
            try:
                result.parts = self._done_parts()   # kBrain review M2: said before the line a request ends on
            except Exception:  # noqa: BLE001 - parts not read are not said
                result.parts = []
        if result.final and result.outcome is None:
            result.outcome = done.outcome(self._last_done if result.status == "done" else None, self.trace,
                                          step_words=self._step_words(), refused=self._refused_count())
            if result.status != "done" and result.line and result.outcome.get("acted"):
                result.outcome["line"] = result.line
        self._result = result
        self._emit_log("brain_run", status=result.status, steps=len(result.statuses),
                       ok=sum(1 for s in result.statuses if s.state == "ok"), revision=self.revision)
        return result

    def _about(self, call: Call) -> dict[str, Any]:
        """What a person can read about the step's target (``_about`` for the island's step words): the control's role
        and name, the app as people say it, whether it sends or changes something for certain."""
        about: dict[str, Any] = {}
        if call.name:
            about["element"] = call.name[:80]
        if call.role:
            about["role"] = call.role
        if call.app:
            about["app"] = util.app_name(call.app)
        try:
            from yuki.agent.tools import call_traits, sends_for_certain

            traits = call_traits(call.tool, call.input, role=call.role or None,
                                 typed_pending=bool(call.hwnd) and int(call.hwnd) in self.typed_pending)
            if sends_for_certain(traits):
                about["sends"] = True
            fixed = tool_traits(call.tool, call.input)
            if (fixed.destroys or fixed.reaches) and fixed.needs != "input":
                about["changes"] = True
        except Exception:  # noqa: BLE001 - presentation only
            pass
        return about

    def _names_log(self, call: Call, outcome: Any) -> None:
        """names21 (merge note 5): a ``launch_app`` that took a misheard name for one of the user's own apps or sites
        (its result's ``details["names_match"]``, which the Dispatcher's outcome carries as it came) logs the measure,
        content-free: kind, how, source, score, margin; never the name or the words. Never raises."""
        if call.tool != "launch_app":
            return
        try:
            payload = outcome.payload if isinstance(getattr(outcome, "payload", None), dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            match = details.get("names_match")
            if isinstance(match, dict):
                self._emit_log("names_match", verb=call.tool, kind=match.get("kind"), how=match.get("how"),
                               source=match.get("source"), score=match.get("score"), margin=match.get("margin"))
        except Exception:  # noqa: BLE001 - a log line only
            pass

    def _note_numbers(self, call: Call, outcome: Any) -> None:
        for key in ("hwnd", "expect_hwnd", "element", "read", "x", "y"):
            v = call.input.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                self.numbers.add(int(v))
        details = (getattr(outcome, "payload", None) or {}).get("details") if isinstance(
            getattr(outcome, "payload", None), dict) else None
        for key in ("hwnd", "foreground_hwnd"):
            v = (details or {}).get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                self.numbers.add(v)

    @staticmethod
    def _result_text(outcome: Any, cap: int = DIFF_CHARS) -> str:
        text = "\n".join(str(b.get("text") or "") for b in getattr(outcome, "content", None) or []
                         if isinstance(b, dict) and b.get("type") == "text")
        return (text or str(getattr(outcome, "summary", "") or ""))[:cap]

    def _trace_step(self, call: Call, outcome: Any, *, ok: bool | None, verdict: bool | None, why: str = "",
                    ms: float = 0.0, started: float | None = None, kind: str | None = None, send: str | None = None,
                    send_sig: str | None = None, refused: bool = False) -> None:
        """One step of the run row, in the old loop's ``_trace_step`` shape: ``tool`` the Dispatcher name that ran,
        ``kind``, ``app`` (a windowless action's: the app in front), ``target`` (the control's role and name and the
        window's title; never a window number), ``input`` (a ``launch_app``'s launcher facts and ``no_window`` mark
        added), ``ok``, ``verdict``, ``why``, ``expected``, ``result``, ``ms``, ``call`` (the model calls so far),
        ``sig``, ``at``, with its start and end; a send's ``send``, ``send_sig`` and chat ``window``. Never raises."""
        try:
            if kind is None:
                kind = "action" if call.tool in ACT_VERBS else "read"
            payload = getattr(outcome, "payload", None)
            payload = payload if isinstance(payload, dict) else {}
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            acted_hwnd = call.hwnd or done.as_hwnd(details.get("hwnd"))
            window = next((w for w in self._last_overview.get("windows") or [] if isinstance(w, dict)
                           and acted_hwnd and done.as_hwnd(w.get("hwnd")) == acted_hwnd), {})
            title = call.title or str(window.get("title") or "")
            app = call.app or str(window.get("process_name") or "") or None
            windowless = kind == "action" and not acted_hwnd and _windowless_tool(call.tool)
            if app is None and kind == "action":
                app = _front_app(self._last_overview)   # the old loop's rule: a windowless step's app is the front one
            parts = []
            if call.name:
                parts.append(f"{call.role or 'element'} \"{call.name[:80]}\"")
            if title:
                parts.append(f"window \"{title[:80]}\"")
            acting = {k: v for k, v in (call.input or {}).items() if k not in ("_about",)}
            sig = hashlib.sha1(json.dumps(acting, sort_keys=True, default=str).encode("utf-8", "replace")).hexdigest()[:12]
            now = time.time()
            calls = getattr(self.host, "model_calls", None)
            step = {"tool": call.tool, "kind": kind, "app": app, "target": ", ".join(parts),
                    "input": acting, "ok": ok, "verdict": verdict, "why": why,
                    "expected": why in (util.EXPECTED_UNVERIFIABLE, util.PAGE_SCROLL_UNVERIFIABLE),
                    "result": " ".join(str(getattr(outcome, "summary", "") or "").split())[:400],
                    "ms": round(float(ms or 0.0), 1), "call": calls if isinstance(calls, int) else None, "sig": sig,
                    "playbook": None, "at": started or now, "started_at": started or now, "ended_at": now}
            if call.name or call.role:
                step["control"] = f"{call.role}|{call.name[:80]}"
            if refused:
                step["refused"] = True
            if send:
                step["send"] = send
                if send_sig:
                    step["send_sig"] = send_sig   # which message (a fingerprint): a retry is the same message
                if title:
                    step["window"] = title[:80]   # which chat, when no text was known
            if windowless:
                step["windowless"] = True
            launcher = details.get("launcher") if call.tool == "launch_app" else None
            if isinstance(launcher, dict):
                # which launcher took it and when (practice memory tells a game this launch started from the user's
                # own doing); the old loop's launch watch stays with the loop
                step["input"] = {**acting, **{k: launcher[k] for k in ("handed_to", "handed_at") if launcher.get(k)}}
            if call.tool == "launch_app" and ok and verdict is None and details and not details.get("hwnd"):
                step["input"] = {**step["input"], "no_window": True}   # no window of it came at all
            if isinstance(payload.get("saved"), bool):
                step["saved"] = payload["saved"]
            self.trace.append(step)
        except Exception:  # noqa: BLE001 - the trace never breaks a run
            pass

    def _run_row(self, *, reply: str, outcome: str) -> dict[str, Any]:
        """The run row (``MemoryAccess.record_run``), in the shape the old loop writes, with the done-check's verdict."""
        result = self._last_done
        words_text = " ".join(self.words)[:600]
        overview_payload = {}
        try:
            overview_payload = done.overview(self.dispatcher, lock=self._dispatch_lock) if self.trace else {}
        except Exception:  # noqa: BLE001
            overview_payload = {}
        numbers = set(self.numbers)
        for w in overview_payload.get("windows") or []:
            if isinstance(w, dict) and isinstance(w.get("hwnd"), int):
                numbers.add(int(w["hwnd"]))
        cost = getattr(self.host, "cost_usd", None)
        cost = (float(cost) if isinstance(cost, (int, float)) else 0.0) + float(self.checker.cost_usd or 0.0)
        return {
            "source": "agent", "session_id": getattr(self.host.memory, "session_id", None),
            "request_id": f"{getattr(self.host, 'lane', '')}-{getattr(self.host, 'request_id', '')}",
            "lane": "voice" if getattr(self.host, "channel", "") == "voice" else getattr(self.host, "lane", ""),
            "at": self.started, "ended_at": time.time(), "words": words_text, "reply": reply, "outcome": outcome,
            "steps": list(self.trace), "cost_usd": round(cost, 6),
            "model_calls": getattr(self.host, "model_calls", None),
            "wall_s": round(time.monotonic() - self.started_mono, 2), "note": None, "steers": list(self.steers),
            "breaker": 0, "numbers": sorted(numbers), "injected": [], "app": _front_app(overview_payload),
            "skill": self._skill_key or ((self.seed or {}).get("key") if self.seed else None),
            "verdict": None if result is None else result.verdict,
            "done": None if result is None else {"probe": result.probe, "level": result.level,
                                                 "covered": list(result.covered), "uncovered": list(result.uncovered)},
            "plan": {"kind": str(field_of(self.head, "kind", "act")) if self.head is not None else None,
                     "route": _route_kind(self.head), "replans": self.replans,
                     "check_calls": int(self.checker.calls)},
        }

    def _write_run(self, run: dict[str, Any]) -> None:
        record = getattr(self.host.memory, "record_run", None)
        if not callable(record) or not getattr(self.host.memory, "installed", False) or not run.get("steps"):
            return
        try:
            record(run)
            self._emit_log("run_trace_queued", steps=len(run.get("steps") or []))
        except Exception:  # noqa: BLE001 - memory never breaks a request
            pass

    def _step_words(self) -> str:
        failed = next((s for s in self.statuses if s.state in ("failed", "refused")), None)
        if failed is None or failed.index >= len(self.steps):
            return ""
        return self._step_said(self.steps[failed.index])[:80]

    def _step_said(self, step: Any, *, done: bool = False) -> str:
        """A step in the island's words (``yuki.ui.step_words.describe_plan_step``: its verb, its control's role and
        name, the plan's target app; never its free text), as it runs or (``done``) as done. ``""`` when none come.
        Typing is said without its Enter (review r1 M2, as the old ``plan.step_line``: "Typed in the Search box", never
        "Typed and sent a message": Enter in an address bar sends nothing, and the brain never sends by raw typing)."""
        try:
            from dataclasses import is_dataclass, replace

            from yuki.ui.step_words import describe_plan_step

            if isinstance(step, dict) and step.get("submit"):
                step = {**step, "submit": False}
            elif is_dataclass(step) and getattr(step, "submit", False):
                step = replace(step, submit=False)
            app = field_of(field_of(self.head, "target", None), "app", None) if self.head is not None else None
            return " ".join(str(describe_plan_step(step, app=app, done=done) or "").split())
        except Exception:  # noqa: BLE001 - presentation only
            return ""

    def _slow_watch(self) -> threading.Timer | None:
        """aBrain (phase 2.3): a timer that says the plan's goal once (:data:`LINE_SLOW_STEP`) if the step about to run
        is still running after :data:`SLOW_STEP_S`; the caller cancels it when the step returns. Voice only; ``None``
        when nothing would be said (typed, no goal, that goal said already). Never raises."""
        try:
            if getattr(self.host, "channel", "") != "voice":
                return None
            goal = words(field_of(self.head, "goal", "")).rstrip(" .")
            key = goal.casefold()
            if not goal or key in self._slow_said:
                return None
            line = LINE_SLOW_STEP.format(goal=goal)

            def fire() -> None:
                try:
                    if self._closed or self.host.cancelled() or key in self._slow_said:
                        return
                    self._slow_said.add(key)
                    self._emit(Say(line[:160]))
                    self._emit_log("slow_step", chars=len(line), after_s=SLOW_STEP_S)
                except Exception:  # noqa: BLE001 - a line only
                    pass

            timer = threading.Timer(SLOW_STEP_S, fire)
            timer.daemon = True
            timer.start()
            return timer
        except Exception:  # noqa: BLE001
            return None

    def _refused_count(self) -> int:
        return sum(1 for s in self.trace if s.get("refused"))

    def _emit(self, event: Any) -> None:
        try:
            self.host.emit(event)
        except Exception:  # noqa: BLE001 - an event never breaks a run
            pass

    def _emit_log(self, event: str, **fields: Any) -> None:
        if not callable(self._log_fn):
            return
        try:
            from yuki.log.events import content_free

            rec = content_free({"type": event, **{k: v for k, v in fields.items() if v is not None}})
            rec.pop("type", None)
            self._log_fn(event, **rec)
        except Exception:  # noqa: BLE001
            pass

    def _earlier_requests(self) -> list[list[str]]:
        """The user's earlier requests (``host.earlier_words``: their own words, never a card's or the screen's), oldest
        first, each as its turns (the Brain keeps a request's turns joined by line breaks), without a turn this
        exchange already holds or an older request already gave; requests left with no turn are dropped."""
        seen = {words(w).casefold() for w in self.words}
        out: list[list[str]] = []
        for entry in self.earlier:
            turns: list[str] = []
            for turn in str(entry).split("\n"):
                key = words(turn).casefold()
                if key and key not in seen:
                    seen.add(key)
                    turns.append(turn.strip())
            if turns:
                out.append(turns)
        return out

    def _given_values(self, call: Call | None) -> list[str]:
        """The values a ``run_skill`` step gives for the slots the engine checks against the user's words (text, app,
        person and entity slots: ``yuki.skills.resolve.check_value``; an enum, a number or a switch is checked against
        its options instead), as written. ``[]`` for any other step."""
        return [value for value, _type in self._given_typed(call)]

    def _given_typed(self, call: Call | None) -> list[tuple[str, str]]:
        """:meth:`_given_values` with each value's slot type (``""`` when the catalog doesn't say)."""
        if call is None or call.tool != "run_skill":
            return []
        key, slots = consent.skill_input(call, self.head)
        sk = util.skill(key) if key else None
        types = {str(getattr(sl, "name", "")): str(getattr(sl, "type", "")) for sl in getattr(sk, "slots", ()) or ()}
        out: list[tuple[str, str]] = []
        for name, value in (slots or {}).items():
            if types and types.get(str(name)) not in WORD_SLOT_TYPES:
                continue
            if isinstance(value, Mapping):
                value = value.get("name")
            if isinstance(value, str) and words(value):
                out.append((words(value), types.get(str(name), "")))
        return out

    def _said_before(self) -> list[str]:
        """bRound2 (B28): the user's own turns of this conversation that memory's turn log holds
        (``host.said_before()``: their words as heard, said to Yuki, the last minutes, newest last), read once per
        request and only when a step gives an app no turn of this request or an earlier one says. ``[]`` when the host
        has none or memory can't say."""
        if self._convo is None:
            fn = getattr(self.host, "said_before", None)
            try:
                got = fn() if callable(fn) else []
            except Exception:  # noqa: BLE001 - a help only: without it the spans are the requests' own
                got = []
            self._convo = [str(t) for t in (got or []) if str(t).strip()]
        return self._convo

    def _skill_words(self, call: Call | None = None) -> tuple[list[str], list[str]]:
        """``(words, earlier)``: the user's own words a capability checks this step's slot values against (x9, fix plan
        item 9: "skill words = self.words + self.earlier").

        ``words``: this exchange's turns as they stand now (with what was added mid-task), newest last; before them,
        each value this very step gives that the exchange doesn't hold but an earlier request of theirs does, as the
        span of their own (newest such) turn that says it: "Arc" out of "open YouTube in Arc" a request ago, then
        "just do it", keeps the planner's ``browser: Arc`` instead of dropping it. Only the span goes in, never the rest
        of that turn: the engine also fills an empty app slot from ``words``, newest turn last (x6's merge note), so
        an older turn read whole could fill a slot nobody named now ("...the YouTube tab in Chrome" filling the
        browser), and appended after the exchange it would read as the newest.

        ``earlier``: every earlier request's turns, oldest first, for the resolver's validate-only ``earlier=`` once
        the Dispatcher passes ``skill_context["earlier"]`` through. Whole words in order, never meaning.

        bRound2 (B28, 10 Oct 21:09: "Open the fucking YouTube tab..." after "I ask you to open YouTube in arc not in
        Chrome" four minutes before): an app value (a browser, an app; slot type ``app``) that neither this exchange
        nor an earlier request of the Brain's says is looked for in the user's own turns of the whole conversation
        (:meth:`_said_before`: memory's turn log, which also holds the turns only the voice talker answered), newest
        first, the same span only. So what they named anywhere in the conversation beats a learned fill policy or
        their most-used browser, and only for a value the planner itself gave from that conversation (never a fill
        from the words, and never a message's text or a person: those keep the request's own words, rule 8)."""
        older = self._earlier_requests()
        spans: list[str] = []
        for value, kind in self._given_typed(call):
            if consent.in_words(value, self.words):
                continue
            # the newest earlier request that says it (the request the user most likely meant)
            found = next((s for request in reversed(older) for turn in reversed(request)
                          if (s := _span_text(value, turn))), "")
            if not found and kind == "app":
                found = next((s for turn in reversed(self._said_before()) if (s := _span_text(value, turn))), "")
                if found:
                    self._emit_log("brain_said_before", turns=len(self._said_before()))
            if found and found.casefold() not in {s.casefold() for s in spans}:
                spans.append(found)
        return spans + list(self.words), [turn for request in older for turn in request]

    def _skill_context(self, call: Call | None = None, *, grant: Mapping[str, Any] | None = None) -> None:
        """What the Dispatcher's ``run_skill`` needs from the request (``Dispatcher.skill_context``: memory, the user's
        words for ``call``'s slot values (:meth:`_skill_words`), the request id, the log, settings), set the way the
        old loop sets it at a request's start, and again before each ``run_skill`` step. ``grant``: that step's
        consent grant (:meth:`_skill_grant`), as ``skill_context["grant"]`` for that step only (Item 11's L2; taken
        out again after the step, and the engine uses it only while the desk hands hold the same one)."""
        try:
            said, earlier = self._skill_words(call)
        except Exception:  # noqa: BLE001 - the spans are a help: without them the engine gets the exchange, as before
            said, earlier = list(self.words), []
        try:
            context = {"memory": self.host.memory, "words": said, "earlier": earlier,
                       "request_id": getattr(self.host, "request_id", None), "log": self._log_fn,
                       "settings": self.settings}
            # zFinal (bSkill finding 1): the capability's own steps type and press through the desk hands here (a
            # plain object until a step uses it; the front desk never runs an act step, so it never does)
            desk = self._desk_hands()
            if desk is not None:
                context["hands"] = desk
                if grant:
                    context["grant"] = dict(grant)
            self.dispatcher.skill_context = context
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------------


def _json_object(value: Any) -> dict[str, Any] | None:
    """A step value that is a JSON object (``"{...}"``), parsed; ``None`` for anything else (a plain value)."""
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value or "").strip()
    if not text.startswith("{"):
        return None
    try:
        got = json.loads(text)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _element_ref(name: str) -> int | None:
    """An element id written as a step's element name, ``[12]`` or ``#12`` (bCore's ``Element.ref_id``), else
    ``None``."""
    text = str(name or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    elif text.startswith("#"):
        text = text[1:].strip()
    else:
        return None
    return int(text) if text.isdigit() else None


def _number(text: str, kind: type) -> Any:
    try:
        return kind(float(str(text).strip()))
    except (TypeError, ValueError):
        raise ValueError(f"expected a number, got {text!r}") from None


def _center(bounds: Any) -> tuple[int, int] | None:
    try:
        left, top, right, bottom = (int(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left + right) // 2, (top + bottom) // 2


def _mid(text: str) -> str:
    """Words that start a sentence, put mid-sentence: a first capital before a small letter is lowered ("Opening a
    page" -> "opening a page"; "YouTube ..." and "I ..." stay). Letter case only."""
    return text[:1].lower() + text[1:] if text[:1].isupper() and text[1:2].islower() else text


def _effect_probe(call: Call, step: Any, image: str = "") -> tuple[str, dict[str, Any]] | None:
    """aBrain (phase 2.5): the probe that reads one act step's effect, from its verb, its closed values and the call's
    facts (:class:`yuki.brain.consent.Call`): an address -> ``page_at`` its site (in the browser it opened in); a
    launch or a focus -> ``foreground`` its program; a window's minimise / maximise / full screen / restore ->
    ``window_state``; a close -> ``gone``; a tab picked -> ``tab_shown`` by its name, closed -> ``gone`` by its name;
    text typed with no Enter -> ``field_holds``; an app's level -> ``level``. ``None`` for anything else. Data only."""
    tool, given = call.tool, dict(call.input or {})
    hwnd = str(call.hwnd) if call.hwnd else ""
    action = words(given.get("action")).casefold()
    role = {"role": call.role} if words(call.role) else {}
    if tool == "open_url":
        site = words(given.get("url"))
        if not _web_host(site):
            return None
        return "page_at", {"site": site, **({"process": image} if image else {})}
    if tool == "launch_app":
        name = words(given.get("query")) or words(call.app)
        return ("foreground", {"process": name}) if name else None
    if tool == "focus_window" or (tool == "window_action" and action == "focus"):
        return ("foreground", {"process": call.app}) if words(call.app) else None
    if tool == "window_action" and hwnd:
        if action in DERIVED_WINDOW_STATES:
            return "window_state", {"window": hwnd, "state": DERIVED_WINDOW_STATES[action]}
        return ("gone", {"window": hwnd}) if action == "close" else None
    if tool == "tab" and hwnd and words(call.name):
        if action == "close":
            return "gone", {"window": hwnd, "name": words(call.name), **role}
        if action == "select" and words(call.app):
            return "tab_shown", {"process": call.app, "name": words(call.name)}
        return None
    if tool == "type_text" and hwnd and words(call.name) and not field_of(step, "submit", False) \
            and not given.get("press_enter"):
        text = words(given.get("text"))
        return ("field_holds", {"window": hwnd, "name": words(call.name), "text": text, **role}) if text else None
    level = given.get("level")
    if tool == "app_volume" and isinstance(level, (int, float)) and not isinstance(level, bool):
        return "level", {"value": float(level), "target": words(given.get("app")) or "system"}
    return None


def _web_host(url: Any) -> str:
    """The host of a web page address (casefolded), ``""`` for anything else (a file, a folder, another scheme). The
    address's shape is parsed as ``launch.open_url`` reads it (``launch._page_address``: a bare ``www.host.tld`` counts
    as ``https://``), a fixed machine format."""
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    try:
        from yuki.actions.launch import _page_address

        address = _page_address(text) or ""
    except Exception:  # noqa: BLE001 - launch not importable here: the address as written
        address = text
    try:
        parts = urlsplit(address)
    except ValueError:
        return ""
    return (parts.hostname or "").casefold() if parts.scheme.casefold() in ("http", "https") else ""


def _bare_host(url: Any) -> str:
    """The host of a bare host value ("youtube.com", casefolded): one token with no scheme, path, port or user part,
    not a file or folder on disk, whose ``https://`` reading has a dotted host. ``launch._page_address`` doesn't count
    it a page (a bare ``name.tld`` is as likely a file), so it is read as one only for a step that names its browser
    (review r1 L1: the 10 Oct talker wrote exactly "youtube.com"). ``""`` for anything else. A fixed machine format."""
    import os
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    if not text or any(ch.isspace() or ch in "/\\:@" for ch in text):
        return ""
    try:
        if os.path.exists(text):
            return ""
        host = urlsplit("https://" + text).hostname or ""
    except (ValueError, OSError):
        return ""
    labels = host.split(".")
    return host.casefold() if len(labels) > 1 and all(labels) else ""


#: Where Windows keeps the browsers it lists as web browsers (each a client key with ``shell\open\command``), machine
#: wide (both registry views) and the user's own.
_BROWSER_CLIENTS = (("HKEY_LOCAL_MACHINE", r"SOFTWARE\Clients\StartMenuInternet"),
                    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"),
                    ("HKEY_CURRENT_USER", r"SOFTWARE\Clients\StartMenuInternet"))


def _registered_browsers() -> list[dict[str, str]]:
    """The browsers Windows has registered as web browsers (``Clients\\StartMenuInternet``: the key its own list of
    browsers reads), each ``{"key", "name"}``: its program's app key from the client's open command ("chrome" from
    ``"C:\\...\\chrome.exe"``) and the client's name ("Google Chrome"; a resource reference, ``@...``, is left out).
    Packaged browsers (Arc) don't register there; the timeline counts them. ``[]`` off Windows or when it can't be
    read. Machine data, read-only; never raises."""
    try:
        import ntpath
        import winreg

        from yuki.core.appnames import app_key
    except ImportError:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for hive_name, path in _BROWSER_CLIENTS:
        try:
            root = winreg.OpenKey(getattr(winreg, hive_name), path)
        except OSError:
            continue
        with root:
            for i in range(256):   # a handful of browsers; the bound only keeps a broken hive from spinning
                try:
                    client = winreg.EnumKey(root, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, client + r"\shell\open\command") as key:
                        command = str(winreg.QueryValueEx(key, "")[0] or "").strip()
                    with winreg.OpenKey(root, client) as key:
                        try:
                            name = str(winreg.QueryValueEx(key, "")[0] or "").strip()
                        except OSError:
                            name = ""
                except OSError:
                    continue
                # the program: the quoted path, else the path up to its ".exe" (unquoted, it may hold spaces)
                end = command.casefold().find(".exe")
                exe = command[1:].partition('"')[0] if command.startswith('"') else \
                    (command[:end + 4] if end >= 0 else "")
                program = app_key(ntpath.basename(exe)) if exe.casefold().endswith(".exe") else ""
                if program and program not in seen:
                    seen.add(program)
                    out.append({"key": program, "name": "" if name.startswith("@") else name})
    return out


def _span_text(value: str, turn: str) -> str:
    """The part of ``turn`` (the user's own words) that says ``value`` in whole words, in order, case and punctuation
    aside (``contacts.span_in``'s reading: runs of letters and digits), as the user's turn has it ("YouTube" in "open
    youtube, in Arc!" for "youtube"); ``""`` when the turn doesn't say it."""
    runs: list[tuple[int, int, str]] = []   # each word of the turn: where it starts and ends, casefolded
    start = -1
    for i, ch in enumerate(str(turn or "") + " "):
        if ch.isalnum():
            if start < 0:
                start = i
        elif start >= 0:
            runs.append((start, i, turn[start:i].casefold()))
            start = -1
    from yuki.memory.contacts import tokens

    want = tokens(value)
    if not want:
        return ""
    for i in range(len(runs) - len(want) + 1):
        if [r[2] for r in runs[i:i + len(want)]] == want:
            return turn[runs[i][0]:runs[i + len(want) - 1][1]]
    return ""


def _squash(text: Any) -> str:
    """Letters and digits only, casefolded (``appnames._squash``): "YouTube" and "youtube" are the same name."""
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _names_site(name: str, host: str) -> bool:
    """Whether an app name is the address's own site, not a browser: one label of the host is the name's letters and
    digits ("YouTube" for ``www.youtube.com``), or a name of several words has every word among the host's labels
    ("Microsoft Teams" for ``teams.microsoft.com``, "YouTube Music" for ``music.youtube.com``; "Google Chrome" is not
    ``www.google.com``'s: "chrome" isn't one of its labels). String identity of two machine facts, never a list of
    sites."""
    from yuki.memory.contacts import tokens

    want = _squash(name)
    labels = {_squash(label) for label in str(host or "").split(".") if _squash(label)}
    if not want or not labels:
        return False
    parts = set(tokens(name))
    return want in labels or (len(parts) > 1 and parts <= labels)


def _known_app(name: str) -> bool:
    """Whether an app on this PC goes by ``name`` (``yuki.core.appnames.aliases``: the names Windows gave the programs
    memory has seen run here, machine data), so the name means that program, never a window title's word. ``False``
    when it can't be read."""
    try:
        from yuki.core.appnames import aliases

        return bool(aliases(words(name)))
    except Exception:  # noqa: BLE001
        return False


def _app_windows(windows: Sequence[Mapping[str, Any]], name: str, front: int | None) -> list[dict[str, Any]]:
    """The listed windows of the program an app name names (by every name the app goes by: as written, its process
    key, the name Windows gives it: ``util.app_names``), the one in front first; ``[]`` when none fits, when the fits
    are more than one program, or when it is Yuki's own window. Machine identities only."""
    want = util.app_names(name)
    if not want:
        return []
    hits = [dict(w) for w in windows if want & util.app_names(str(w.get("process_name") or ""))
            and not _is_own_window(done.as_hwnd(w.get("hwnd")) or 0)]
    if len({done._app_key(w.get("process_name")) for w in hits}) != 1:
        return []
    hits.sort(key=lambda w: done.as_hwnd(w.get("hwnd")) != front)
    return hits


def _launch_name(image: str) -> str:
    """A program's process image as the app is launched by (``launch_app`` matches Start menu names): the name Windows
    gives that program (``appnames.name_for_key``: "Arc.exe" -> "Arc", "chrome.exe" -> "Google Chrome"), else the image
    made presentable."""
    try:
        from yuki.core.appnames import app_key, name_for_key, prettify

        pretty = prettify(image)
        return name_for_key(app_key(image), pretty) or pretty or str(image)
    except Exception:  # noqa: BLE001
        return util.app_name(image)


#: A hand-off seed's ``why`` after its skill acted and the result is unknown (``yuki.skills.engine``'s code words, x5:
#: ``WHY_UNVERIFIED`` its check couldn't confirm it, ``WHY_ERROR`` it broke partway), and before any step of it ran
#: that sends or joins (``TO_PLANNER``).
_ACTED_WHYS = frozenset({"unverified", "error"})
_TO_PLANNER = "to_planner"


def _handed_line(seed: Mapping[str, Any]) -> str:
    """The plain line for a ``run_skill`` hand-off, by the seed's ``why`` (a code word the engine sets): it stopped
    before a step that sends or joins, it acted and the result is unknown, or its routes didn't get there (or it
    didn't run: a value it was given wasn't in the user's words)."""
    why = str(seed.get("why") or "")
    if why == _TO_PLANNER:
        return LINE_SKILL_STOPPED
    if why in _ACTED_WHYS:
        return LINE_SKILL_UNSURE
    return LINE_SKILL_HANDED


def _refused_kind(payload: Mapping[str, Any]) -> str | None:
    """fHands: the refusal kind an act's payload carries (``"game"`` / ``"blocked"``: ``yuki.actions.gate.carried``),
    its own or its details', ``"game"`` first; ``None`` for any other refusal (a ToolError's ``True``, ``act_off``)."""
    from yuki.actions.gate import carried

    details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
    kinds = {carried(payload), carried(details)}
    return "game" if "game" in kinds else ("blocked" if "blocked" in kinds else None)


def _engine_ask(outcome: Any) -> str:
    """The capability's own question in a ``run_skill`` report (the engine's fixed shape ``Ask them: <question>``; the
    question is the capability's data, e.g. "Who to?"), ``""`` when there is none."""
    summary = " ".join(str(getattr(outcome, "summary", "") or "").split())
    head, sep, rest = summary.partition("Ask them: ")
    return rest.strip() if sep and not head.strip() else ""


def _engine_said(outcome: Any) -> str:
    """The engine's own line in a ``run_skill`` report that didn't get it done: its done-line marks
    (``done.engine_line``), else what follows ``Not done (<outcome>).`` (``SkillResult.report``'s fixed shape)."""
    line = done.engine_line(outcome)
    if line:
        return line
    summary = " ".join(str(getattr(outcome, "summary", "") or "").split())
    if summary.startswith("Not done ("):
        return summary.partition(")")[2].lstrip(". ").strip()
    return ""


def _route_kind(plan: Any) -> str:
    route = words(field_of(plan, "route", "steps")) if plan is not None else ""
    return "skill" if route.startswith("skill:") else (route or "steps")


def _code(text: str) -> str:
    """A reason as a log code word when it is one (``handed_over``), else ``mismatch``."""
    t = str(text or "")
    return t if t.replace("_", "").isalpha() and t.islower() and len(t) <= 40 else "mismatch"


def _front_app(payload: Mapping[str, Any]) -> str | None:
    front = done.as_hwnd((payload or {}).get("foreground_hwnd"))
    for w in (payload or {}).get("windows") or []:
        if isinstance(w, dict) and done.as_hwnd(w.get("hwnd")) == front:
            return str(w.get("process_name") or "") or None
    return None


def _windowless_tool(name: str) -> bool:
    """A tool with no window of its own: its schema takes no ``expect_hwnd``, ``hwnd`` or ``element`` (a schema fact,
    the old loop's ``_windowless`` rule)."""
    try:
        from yuki.agent.tools import TOOLS_BY_NAME

        props = ((TOOLS_BY_NAME.get(name) or {}).get("input_schema") or {}).get("properties") or {}
    except Exception:  # noqa: BLE001
        return False
    return bool(props) and not any(k in props for k in ("expect_hwnd", "hwnd", "element"))


def _board() -> Mapping[str, Any] | None:
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        return client.now(wait_s=0.05) if client is not None else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["ACT_VERBS", "ANSWER_MODEL_CALLS", "ANSWER_SECONDS", "ACT_MODEL_CALLS", "ELEMENT_VERBS", "Executor",
           "LOOK_TOOLS", "MAX_REPLANS", "Mismatch", "READ_VERBS", "RunResult", "StepStatus", "VERBS"]
