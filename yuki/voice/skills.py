"""Talker skills: a learned way run as the talker's hand, step by step, each step checked (v3.18, 07 §6 F2).

Practice memory keeps the ways that worked on this PC as procedures: short steps by a control's role and visible name,
keys by name, what changes each time as slots (``yuki.memory.practice``; written by the distiller from Yuki's own run
traces, each step citing trace steps that ran and were not shown to fail). When one fits the user's words closely and
memory is sure of it (:mod:`yuki.voice.practice` offers it on the talker's "Learned ways" line), the talker can run it
with ``run_skill`` instead of handing the request to the thinker: it writes the way's steps as structured hand steps
(the role and name, the keys, the text from their words) in its one reply, and this module runs them through the very
hands the talker already has (the same backend, dispatcher, guards and verdicts). When memory keeps the way's steps
executable (``MemoryClient.practice(detail=True)``'s ``run``, fx2: each a control by role and name, keys, an app, an
expected state, never a window id), the line shows them as run_skill takes them and the talker's steps must be the
way's own last steps, run as memory keeps them (:func:`fit_to_way`; only a type step's text is the talker's):

* **click**: the control (role and name, as the way names it) is found in a fresh read of the window in front, a popup
  or a window of the way's app, waiting a moment for it to appear after the step before; clicked by that read's
  element id, verified by the action layer's watch (``yuki.agent.loop.action_verdict``).
* **type**: only into the text field that has the keyboard, only words the user said or the way's own words (a slot
  filled from their words), never anything pressed after; verified by reading the field back.
* **keys**: held together, only into a window of the way's own app (or the one in front when it started), guarded by
  ``expect_hwnd``; verified by the watch (focus, window, title, state).
* **scroll**, **open_app**, **open_url**: the talker's own hands of those names.
* **pick** (capC4, report 78 §6.2): an item of a list, a chat, a file - a content control, whose name the action trace
  never keeps. The container (by role, and name when the way knows it) is found in a fresh read; its visible children
  of the item's role are read, the keyed digest C1 takes over a name is computed over each child's name (by memory,
  :meth:`yuki.memory.api.MemoryClient.name_digests`) and the one matching the way's ``item`` is clicked (a fixed item:
  "always Liked Songs"); or, when the way names a slot, the child whose name is the request's words, matched as a click
  matches. When the way keeps the container's identity (C1's keyed container digest), only that container's items
  count; without it the item must be the only match in the window. No unique visible match: the run stops and the
  thinker gets it. A way that sends never picks a fixed item, and a slot pick's words must be whom it goes to.

**Ways learned from what the user did** (capC4, report 78 §6.1-6.2, §7) run **in parts**: each part at most
:data:`MAX_STEPS` steps, at most 4 parts, each ending on a checked state memory recorded (a window of an app in front,
a page reached, or the part's last step verified). The parts run in order, and a part's end is verified before the
next part starts; a part whose end is not reached stops the run and hands over. The talker may give all the way's
steps, its last steps, or only its typing and slot steps (their text from the user's words); every other step comes
from memory. A way the user did only once is a **trial** ("your way, seen once"): a hint for the thinker, never run
here (decision D3). A **Warp command** step is the thinker's terminal tool's, never run here. A demo never authorises a
send: a way whose steps sent what was typed runs only when the talker declares ``sends`` with whom and what in the
user's own words (:func:`check_sends`); Yuki's own check (on Claude) still judges each step.

**Each step's expectation is checked** before the next one runs: the action's own measured verdict (a step whose report
shows it did not take effect, or that failed, stops the run), the next step's precondition (its control on the screen,
a text field with the keyboard), and, when the talker wrote one and the report could not tell, the step's ``expect``:
words the way says will show once it worked, looked for as a whole-word span in the title and the control names of the
window in front (it settles an unknown verdict; it never overrules a measured one). A key never follows a step whose
effect could not be seen (no blind chains). **At the first deviation the run stops** and the thinker gets the request by
code with what was done and where it deviated (the talker says nothing more: one outcome). Keys, and any step the
user's autonomy policy holds back, first pass Yuki's own authorization check (``yuki.agent.judge``, revamp D S1) with
the user's words: a held step stops the run and the user is asked.

**Never a send unless the user's words plainly said what goes to whom.** The talker declares ``sends`` for a way that
sends, posts or replies (its judgement, as the thinker declares ``risk``); then ``to`` and ``what`` must each be the
user's own words (a whole-word span of what they said), else nothing is run and the thinker gets it. Whatever is typed
must be their words or the way's own: the talker cannot compose text.

Every run is kept as a run trace (``MemoryAccess.record_run``: source ``hand``, one step per action, the way cited), so
the ledger counts the use and the learners see how it went. No regex, no keyword list, no per-app branch: strings are
only compared with strings (identity, whole-word spans); everything else is a measured report.

**Doing it like the user** (capC5, report 78 §7):

* **Each step verified by report 58's diff.** The steps of a learned way run through a dispatcher of their own with
  the before/after account on (:func:`skill_dispatcher`: the window read before and after each click, key and typing,
  compared by RuntimeId), so a single-page app's real effect is seen and never read as "nothing changed". The talker's
  quick hands keep their own fast dispatcher.
* **Anchors, in order**: a proven AutomationId (unique in the app, its name must agree: C4), the role and name, a pick
  by digest within its container (C4), and last report 58's RuntimeId of the very control an earlier step or run found
  **in this session** (:func:`remember_rid`; never across sessions: RuntimeIds do not survive an app restart).
* **The replay guard** (AgentRR style). A way learned from the trace is replayed against its recorded path
  (:func:`yuki.memory.trace_replay.way_path`): for each step, the app it acted in, the site when it was a web page,
  whether it stayed in the window of the step before, whether a dialog came up after it. A step whose observed state
  leaves that path stops the run and hands over ("I stopped at step 3: ..."): a different app, window or page than
  the recording; a dialog the recordings never showed; the control missing, or two that fit. Decided on machine facts
  only (window identity, owner windows, the page's host, the element's presence), never on titles: a timestamp in a
  title, an unread count, a window moved never stop a good run. A way without a recording is held to the apps it
  names, and a dialog that comes up is on its path only when the next step acts inside it.
* **The user's shortcuts**: a keys step goes only into the window this run verified (one it acted in, or one that came
  up during it) of the app the step was recorded in.
* **Takeover** (report 63): while the steps run, the user's keys, clicks or a large mouse move stop the run at once
  ("you took over"); nothing more is done and it is not counted against the way.
* **The A/B switch** (:func:`yuki.memory.trace_replay.use_learned`, read live): off, a way the action trace taught
  is refused before anything runs.
"""

from __future__ import annotations

import time
import weakref
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

#: What a skill step may do: the talker's own verified hands (``pick``: capC4, an item of a list by its name's digest).
SKILL_DOS = ("click", "type", "keys", "scroll", "open_app", "open_url", "pick")
#: Most steps one run takes (practice memory keeps at most 8 per procedure); a way learned from what the user did runs
#: in parts of at most this many steps each, at most :data:`MAX_PARTS` parts.
MAX_STEPS = 8
MAX_PARTS = 4
#: The states a part before the last may end on (practice memory's ``PART_END_KINDS``): the ones checked live here.
PART_END_KINDS = ("window_verified", "page_reached")
#: How deep below a container a picked item may sit (a ListItem inside a Group inside the List).
PICK_DEPTH = 6
#: How long a part's checked end may take to show (a window coming up, a page loading).
END_WAIT_S = 3.0
#: A key combination holds at most this many keys.
MAX_KEYS = 4
#: The first step's control should be on the screen already (the way fits the screen now, or it does not): one read
#: and a short wait. A later step's control may take a moment to appear after the step before; after an app or a page
#: was opened, longer (a launch takes 1.8-6.3 s, measured live, docs/VOICE.md v3.14b).
FIRST_FIND_WAIT_S = 0.6
FIND_WAIT_S = 2.5
AFTER_OPEN_WAIT_S = 4.0
#: How long a text field may take to get the keyboard after the step before (a quick switcher opening).
FOCUS_WAIT_S = 1.5
#: How long a step's expected words may take to show.
EXPECT_WAIT_S = 2.0
POLL_S = 0.2
#: Reports are kept to this many characters per step.
STEP_REPORT_CHARS = 220
#: capC5: the skill runner's own dispatcher (report 58's before/after account on): how many elements a read keeps and
#: how long it may take (between the talker's quick 300 / 0.8 s and the thinker's 1,000 / 6 s).
SKILL_TREE_ELEMENTS = 1000
SKILL_TREE_TIMEOUT_S = 2.5
#: capC5: a control found by an earlier step or run of this session is re-found by its RuntimeId at most this long
#: after (s), and only in the same window of the same app (RuntimeIds do not survive an app restart).
RID_KEEP_S = 2 * 3600.0
RID_KEEP_N = 200
#: The step the run keeps when the user took over (``yuki.memory.trace_replay.TAKEOVER_TOOL``).
TAKEOVER_TOOL = "takeover"

_STR = {"type": "string"}

#: The run_skill hand's schema (Pipecat standard function schema fields). say_first comes first: it is said while the
#: rest of the call is still being written (``yuki.voice.pipeline.SAY_FIRST_TOOLS``).
RUN_SKILL_TOOL: dict[str, Any] = {
    "name": "run_skill",
    "description": "Your hand for a way on the Learned ways line: its steps, one by one, each checked.",
    "properties": {
        "say_first": {"type": "string", "description": "Written first: words said as it starts, or \"\"."},
        "skill": {"type": "string", "description": "pb:N from the line."},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "do": {"type": "string", "enum": list(SKILL_DOS)},
            "role": _STR, "name": {"type": "string", "description": "click: visible name; open_app: the app."},
            "text": _STR, "keys": {"type": "array", "items": _STR},
            "direction": {"type": "string", "enum": ["up", "down"]}, "url": _STR, "app": _STR, "expect": _STR},
            "required": ["do"]}},
        "sends": {"type": "boolean"},
        "to": _STR,
        "what": _STR,
        "say_when_done": {"type": "string", "description": "Said once every step is verified, or \"\"."},
    },
    "required": ["say_first", "skill", "steps", "sends", "say_when_done"],
}


class SkillRefused(Exception):
    """A skill call refused before any step ran (the message says why); the thinker gets the request."""


class _TakenOver(Exception):
    """capC5 review F3: the user took over (report 63) while a step looked for its control or was being checked; the
    step does nothing and the run stops."""


@dataclass
class SkillStep:
    """One step of a run, as the talker wrote it (validated)."""

    i: int
    do: str
    role: str = ""
    name: str = ""
    window: int | None = None
    text: str = ""
    keys: list[str] = field(default_factory=list)
    direction: str = ""
    amount: int = 0
    url: str = ""
    app: str = ""
    expect: str = ""
    #: capC4: a click's proven AutomationId (memory keeps it only where that app's are proven).
    aid: str = ""
    #: capC4, pick: the container (role, and name when known), the item's keyed name digest (a fixed item) or "" (the
    #: item is named by ``text``, the user's words), and whether the way names a slot.
    box_role: str = ""
    box_name: str = ""
    item: str = ""
    slot: bool = False
    #: second verification V8: the container's identity as C1 keys it (the keyed digest of its role, AutomationId and
    #: its parent's role), so a same-named item in another list of the same role is never picked.
    box_digest: str = ""

    def what(self) -> str:
        """The step in a few words, for reports (content: the thinker and memory only)."""
        if self.do == "click":
            return f'click {self.role or "control"} "{self.name}"'
        if self.do == "pick":
            return (f'pick "{self.text[:60]}"' if self.slot else "pick the usual item") + \
                f" in the {self.box_role or 'list'}"
        if self.do == "type":
            return f'type "{self.text[:80]}"'
        if self.do == "keys":
            return "keys " + "+".join(self.keys)
        if self.do == "scroll":
            return f"scroll {self.direction or 'down'}"
        if self.do == "open_app":
            return f"open the app {self.name or self.app}"
        return f"open {self.url}" + (f" in {self.app}" if self.app else "")


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_steps(raw: Any, *, cap: int = MAX_STEPS, allow_empty: bool = False) -> list[SkillStep]:
    """The talker's steps, validated (raises :class:`SkillRefused` before anything runs). ``cap``: at most this many
    (a way in parts: all its parts' steps); ``allow_empty``: none at all is fine (a way in parts with no typing)."""
    if allow_empty and isinstance(raw, list) and not raw:
        return []
    if not isinstance(raw, list) or not raw:
        raise SkillRefused("no steps were given")
    if len(raw) > cap:
        raise SkillRefused(f"more than {cap} steps")
    steps: list[SkillStep] = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise SkillRefused(f"step {i} is not a step")
        do = _words(item.get("do")).casefold()
        if do not in SKILL_DOS:
            raise SkillRefused(f"step {i} does {do or 'nothing'!r}, not one of {', '.join(SKILL_DOS)}")
        window = item.get("window")
        try:
            window = int(window) if window not in (None, "", 0) else None
        except (TypeError, ValueError):
            window = None
        keys = [_words(k).casefold() for k in (item.get("keys") or []) if _words(k)] if isinstance(
            item.get("keys"), list) else []
        try:
            amount = max(1, min(int(item.get("amount") or 5), 50))
        except (TypeError, ValueError):
            amount = 5
        step = SkillStep(i=i, do=do, role=_words(item.get("role")), name=_words(item.get("name")), window=window,
                         text=str(item.get("text") or ""), keys=keys,
                         direction=_words(item.get("direction")).casefold() or "down", amount=amount,
                         url=_words(item.get("url")), app=_words(item.get("app")), expect=_words(item.get("expect")))
        if do == "click" and not step.name:
            raise SkillRefused(f"step {i} clicks no named control")
        if do == "type" and not step.text.strip():
            raise SkillRefused(f"step {i} types nothing")
        if do == "keys" and not (1 <= len(keys) <= MAX_KEYS):
            raise SkillRefused(f"step {i} presses no keys (or too many at once)")
        if do == "open_app" and not (step.name or step.app):
            raise SkillRefused(f"step {i} opens no app")
        if do == "open_url" and not step.url:
            raise SkillRefused(f"step {i} opens no address")
        if do == "scroll" and step.direction not in ("up", "down"):
            raise SkillRefused(f"step {i} scrolls neither up nor down")
        steps.append(step)
    return steps


def said(text: str, sources: list[str]) -> bool:
    """Whether ``text`` stands, whole words in order, in one of ``sources`` (case and punctuation aside): a check of the
    talker's string against the user's own words or the way's own text, never a judgement of meaning."""
    from yuki.memory.contacts import span_in

    return bool(_words(text)) and any(span_in(text, s) for s in sources if s)


def check_sends(args: dict[str, Any], words: list[str]) -> str | None:
    """Why a way that sends may not run (``None``: it may). ``sends`` is the talker's own declaration; when true, whom
    and what must both be the user's own words."""
    if args.get("sends") is not True:
        return None
    to, what = _words(args.get("to")), _words(args.get("what"))
    if not to or not what:
        return "it would send something, and their words do not plainly say both what it says and who it goes to"
    if not said(to, words):
        return f"it would send to \"{to[:60]}\", which is not what they said"
    if not said(what, words):
        return f"it would send \"{what[:60]}\", which is not what they said"
    return None


def _same_step(mine: SkillStep, way: dict[str, Any]) -> bool:
    """Whether the talker's step is the way's stored step ``way`` (strings compared with strings: identity, or one
    name a whole-word span of the other - "Chrome" for "Google Chrome"; keys as a set)."""
    from yuki.memory.contacts import span_in

    if mine.do != way.get("do"):
        return False
    if mine.do in ("click", "open_app"):
        a, b = _words(mine.name or mine.app), _words(way.get("name"))
        return bool(a and b) and (a.casefold() == b.casefold() or span_in(a, b) or span_in(b, a))
    if mine.do == "keys":
        return {k.casefold() for k in mine.keys} == {str(k).casefold() for k in way.get("keys") or []}
    if mine.do == "open_url":
        return _words(mine.url).casefold().rstrip("/") == _words(way.get("url")).casefold().rstrip("/")
    if mine.do == "scroll":
        return mine.direction == str(way.get("direction") or "down")
    return True   # type: the text is the talker's (their words), checked by check_texts


def fit_to_way(steps: list[SkillStep], run: list[dict[str, Any]]) -> list[SkillStep]:
    """The talker's steps as the way's own executable steps (memory's ``run``, fx2): they must be the way's last steps
    in order - the first ones may be left out when the screen is already past them (Chrome already open), none may be
    added, changed or left out after the first one run. Each step then runs as memory keeps it: the control's role and
    name, the keys, the app, the address and the expected words from the way; only a type step's text is the talker's.
    Raises :class:`SkillRefused` (nothing runs; the thinker gets it) when they are not the way's steps."""
    if not run:
        return steps
    start = len(run) - len(steps)
    if start < 0 or not all(_same_step(mine, way) for mine, way in zip(steps, run[start:])):
        raise SkillRefused("those are not the learned way's own steps (its last steps, in order, as the line gives "
                           "them)")
    return [way_step(mine.i, way, mine) for mine, way in zip(steps, run[start:])]


def way_step(i: int, way: dict[str, Any], mine: SkillStep | None = None) -> SkillStep:
    """One of the way's stored steps as it runs: every field from memory; only the text (a type step's, a slot pick's)
    is the talker's (``mine``)."""
    mine = mine or SkillStep(i=i, do=str(way.get("do") or ""))
    box = way.get("in") if isinstance(way.get("in"), dict) else {}
    return SkillStep(
        i=i, do=str(way.get("do") or mine.do), role=_words(way.get("role")) or mine.role,
        name=_words(way.get("name")) or mine.name, window=None, text=mine.text,
        keys=[str(k).casefold() for k in way.get("keys") or []] or mine.keys,
        direction=str(way.get("direction") or mine.direction or "down"), amount=mine.amount or 5,
        url=_words(way.get("url")) or mine.url, app=_words(way.get("app")) or mine.app,
        expect=_words(way.get("expect")) or mine.expect, aid=_words(way.get("aid")),
        box_role=_words(box.get("role")), box_name=_words(box.get("name")), item=_words(way.get("item")),
        slot=way.get("do") == "pick" and not way.get("item"), box_digest=_words(box.get("digest")))


def fill_slots(steps: list[SkillStep], run: list[dict[str, Any]]) -> list[SkillStep]:
    """capC4: a way in parts run whole from memory, the talker giving only its text-bearing steps (its typing steps
    and the picks that name a slot), in order, each with the text from the user's words; every other step is the way's
    own. Raises :class:`SkillRefused` when the talker's steps are not exactly those, in that order."""
    wants = [n for n, s in enumerate(run) if s.get("do") == "type" or (s.get("do") == "pick" and not s.get("item"))]
    if len(steps) != len(wants) or any(mine.do != run[n].get("do") for mine, n in zip(steps, wants)):
        raise SkillRefused("those are not the learned way's steps (all of them in order, its last ones, or only its "
                           "typing and pick steps with the text from their words)")
    given = {n: mine for mine, n in zip(steps, wants)}
    return [way_step(n + 1, s, given.get(n)) for n, s in enumerate(run)]


def check_picks_for_send(steps: list[SkillStep], args: dict[str, Any]) -> str | None:
    """Second verification V1: why a way that sends may not run because of whom it picks (``None``: it may). A send
    goes to the person or chat the user named: a fixed item (the chat the user picked when they did it) is never the
    recipient of a send, and an item picked by name must be the very ``to`` the user's words gave."""
    to = _words(args.get("to")).casefold()
    for step in steps:
        if step.do != "pick":
            continue
        if not step.slot:
            return (f"step {step.i} always picks the same item, and a send goes only to whom their own words name: "
                    f"it is a hint for the thinker")
        if not to or _words(step.text).casefold() != to:
            return f"step {step.i} would pick \"{step.text[:60]}\", not \"{to[:60]}\" the message goes to"
    return None


def check_texts(steps: list[SkillStep], words: list[str], way_text: str) -> str | None:
    """Why a step's text may not be typed (``None``: all may): only the user's own words or the way's own words
    (``way_text`` ``""`` for a way learned from what the user did: decision D8, its typing is always their words)."""
    for step in steps:
        if step.do == "type" and not said(step.text, [*words, way_text]):
            return (f"step {step.i} would type \"{step.text[:60]}\", which is neither their words nor the learned "
                    f"way's own")
        if step.do == "pick" and step.slot and not said(step.text, words):
            return (f"step {step.i} would pick \"{step.text[:60]}\", which is not what they said" if step.text
                    else f"step {step.i} picks an item their words must name, and none was given")
    return None


@dataclass
class StepResult:
    """What one step did (content: the thinker, the run trace)."""

    step: SkillStep
    ok: bool
    verdict: bool | None
    why: str
    report: str
    kind: str = "action"          # action | read (a look that found the step did not fit)
    tool: str = ""
    target: str = ""
    app: str | None = None
    hwnd: int | None = None
    ms: float = 0.0
    at: float = 0.0
    deviation: str | None = None  # not_found | no_focus | window | failed | no_effect | blind | expect | held
    started: float = 0.0          # monotonic: when the step began
    question: str = ""            # held: the authorization check's question for the user
    #: capC5: what was different from the way's path, in plain words for the handover ("a dialog came up ...")
    different: str = ""


@dataclass
class Expect:
    """capC5: what the way's recorded path says of one step (machine facts; empty: nothing known, no check)."""

    #: App keys the step may act in (the recording's app; without one, the apps the way names).
    apps: frozenset[str] = frozenset()
    #: The site of the web page the step acted on in the recording (``""``: not a web page, or not known).
    host: str = ""
    #: The step acted in the same window as the step before it in the recording (no window change between them).
    same_window: bool = False
    #: A dialog came up after this step in the recordings (``None``: no recording - a dialog is on the path only when
    #: the next step acts inside it).
    dialog: bool | None = None
    #: A new top-level window came to the front after this step in the recording (second verification N4: kept apart
    #: from a dialog - it never lets an owned window through).
    new_window: bool = False
    recorded: bool = False


@dataclass
class Located:
    """capC5: where a step's control was looked for: ``status`` ``found`` (with the window and element), ``missing``,
    ``ambiguous`` (``n`` fit), or ``elsewhere`` (it fits only in a window off the way's path: ``seen_app``,
    ``why`` app | window)."""

    status: str
    hwnd: int = 0
    read: int = 0
    element: dict[str, Any] | None = None
    title: str = ""
    app: str = ""
    how: str = ""                 # aid | name | rid
    host: str = ""                # the site of the window's web page in that read, when it shows one
    n: int = 0
    seen_app: str = ""
    why: str = ""

    def found(self) -> tuple[int, int, dict[str, Any], str, str] | None:
        return (self.hwnd, self.read, self.element or {}, self.title, self.app) if self.status == "found" else None


#: capC5: the dispatcher each set of hands runs learned ways through (report 58's account on), made once.
_SKILL_DISPATCHERS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def skill_dispatcher(hands: Any) -> Any:
    """The dispatcher a learned way's steps run through (capC5, report 78 §7.1): one with report 58's before/after
    account on (each click, key and typing bracketed by two reads of its window and compared by RuntimeId), over the
    hands' own backend, made once per set of hands. Hands that bring their own (``skill_dispatcher``), or that are not
    the real voice hands (a stand-in desk), use their own dispatcher as before."""
    own = getattr(hands, "skill_dispatcher", None)
    if own is not None:
        return own
    base = getattr(hands, "_dispatcher", None)
    backend = getattr(hands, "backend", None)
    if backend is None or base is None:
        return base
    try:
        from yuki.agent.tools import Dispatcher
    except Exception:  # noqa: BLE001
        return base
    if not isinstance(base, Dispatcher):
        return base
    try:
        made = _SKILL_DISPATCHERS.get(hands)
    except TypeError:
        return base
    if made is None:
        try:
            made = Dispatcher(backend, max_tree_elements=SKILL_TREE_ELEMENTS, tree_timeout_s=SKILL_TREE_TIMEOUT_S,
                              verify_outcomes=True)
        except Exception:  # noqa: BLE001 - the hands' own dispatcher then (its watch verifies)
            return base
        _SKILL_DISPATCHERS[hands] = made
    return made


#: capC5: ``{(session, step key): {"hwnd", "app", "rid", "role", "at"}}`` - the RuntimeId of the control a step found,
#: re-found by later steps or runs of the same session (report 58's identity), never across sessions.
_RIDS: dict[tuple[str, str], dict[str, Any]] = {}


def session_of(hands: Any) -> str:
    """The voice session a set of hands works in (memory's session id with the hands' identity): RuntimeIds are kept
    for it only."""
    memory = getattr(hands, "memory", None)
    return f"{id(hands)}:{getattr(memory, 'session_id', None) or ''}"


def step_key(step: SkillStep) -> str:
    """A step's identity for the RuntimeId memory: what it does and the control it names (never typed text)."""
    import json

    return json.dumps([step.do, step.role.casefold(), _words(step.name).casefold(), step.aid, step.box_role,
                       step.box_digest, step.item, step.app.casefold()], separators=(",", ":"))


def remember_rid(session: str, step: SkillStep, hwnd: int, app: str, element: dict[str, Any] | None,
                 *, now: float | None = None) -> None:
    """Keep the RuntimeId of the control a step acted on, for this session (capC5 anchor 4). Never for a pick by the
    user's words (review F1: a pick by name must match the name said now; the item picked for another name earlier is
    never it). The control's name is kept with it (in memory only, this session) so a later re-find must still bear it
    (review F2)."""
    rid = (element or {}).get("runtime_id")
    if not rid or not hwnd or (step.do == "pick" and step.slot):
        return
    now = time.time() if now is None else float(now)
    _RIDS[(session, step_key(step))] = {"hwnd": int(hwnd), "app": str(app or ""), "rid": list(rid),
                                        "role": str((element or {}).get("role") or ""),
                                        "name": _words((element or {}).get("name")), "at": now}
    if len(_RIDS) > RID_KEEP_N:
        for old in sorted(_RIDS, key=lambda k: _RIDS[k]["at"])[: len(_RIDS) - RID_KEEP_N]:
            _RIDS.pop(old, None)


def recall_rid(session: str, step: SkillStep, hwnd: int, app: str, elements: list[dict[str, Any]],
               *, now: float | None = None) -> dict[str, Any] | None:
    """The element of this read that is the very control the same step acted on earlier in this session: the same
    window of the same app, the same RuntimeId and role, kept less than :data:`RID_KEEP_S` ago, and its live name still
    bearing the name it had then (whole words in order: a count added to it, "Inbox" -> "Inbox 3 unread", is the same
    control; a toggle renamed, "Mute" -> "Unmute", is not - review F2). Never for a pick by the user's words (review
    F1). ``None`` otherwise."""
    from yuki.memory.contacts import span_in
    from yuki.memory.store import app_key

    if step.do == "pick" and step.slot:
        return None
    got = _RIDS.get((session, step_key(step)))
    now = time.time() if now is None else float(now)
    if not got or int(got["hwnd"]) != int(hwnd) or now - float(got["at"]) > RID_KEEP_S:
        return None
    if (app_key(got["app"]) or got["app"]) != (app_key(app) or app):
        return None
    hits = [e for e in elements if isinstance(e, dict) and e.get("runtime_id")
            and list(e["runtime_id"]) == list(got["rid"]) and str(e.get("role") or "") == got["role"]
            and (not got.get("name") or span_in(got["name"], _words(e.get("name"))))]
    return hits[0] if len(hits) == 1 else None


def hosted_app(hwnd: int, pid: int) -> str:
    """Second verification N2: the process a UWP frame window (``ApplicationFrameWindow``) hosts ("SystemSettings.exe"
    for Settings), ``""`` for any other window or when it cannot be told. Machine facts from Windows only: the window
    must still belong to ``pid`` (the window list's), and :func:`yuki.memory.extract.uia.content_pid` - the trace's own
    reading - names a different process as its content. Enumerates a frame's child windows; sends no messages."""
    if not hwnd or not pid:
        return ""
    try:
        import win32process

        from yuki.memory.extract.uia import content_pid, process_name

        if int(win32process.GetWindowThreadProcessId(int(hwnd))[1]) != int(pid):
            return ""
        other = int(content_pid(int(hwnd)) or 0)
        return process_name(other) if other and other != int(pid) else ""
    except Exception:  # noqa: BLE001 - not known: the window's own process
        return ""


def app_words(app: str) -> str:
    """An app as the user is told of it (second verification N2: never a process file name): its display name from
    the process name as people say it (``yuki.core.appnames.prettify``: "chrome.exe" -> "Chrome")."""
    try:
        from yuki.core.appnames import prettify

        return prettify(str(app or "")) or str(app or "")
    except Exception:  # noqa: BLE001
        return str(app or "")


def page_host(tree: dict[str, Any] | None) -> str:
    """The site of the web page a read shows (its Document's address, host only, ``www.`` off), ``""`` when none:
    a machine fact of the window, compared host to host (never titles)."""
    from urllib.parse import urlsplit

    from yuki.memory.trace_replay import site

    for e in (tree or {}).get("elements") or []:
        if not isinstance(e, dict) or str(e.get("role") or "") != "Document":
            continue
        value = str(e.get("value") or "")
        if "://" in value and " " not in value:
            try:
                return site(urlsplit(value).hostname)
            except ValueError:
                return ""
    return ""


class SkillRunner:
    """Runs one learned way through a :class:`yuki.voice.hands.Hands`' backend, step by step (blocking: off the loop)."""

    def __init__(self, hands: Any, *, takeover: Any = None) -> None:
        self.hands = hands
        #: This run's earlier steps as calls (what the authorization check reads beside the user's words).
        self._earlier: list[str] = []
        #: capC5: the dispatcher the steps run through (report 58's before/after account on for the real hands).
        self._disp = skill_dispatcher(hands)
        #: capC5: ``(on_takeover) -> watch`` with ``start()``, ``stop()`` and ``fired`` (report 63's
        #: ``TakeoverThread``); ``None``: the real hands get one, a stand-in desk none.
        self._takeover = takeover
        self._session = session_of(hands)
        #: capC5, per run: the windows a step acted in (verified), the last one, the windows open before the run, and
        #: dialogs that came up after a step of a way with no recording (on the path only if the next step is inside).
        self._acted: set[int] = set()
        self._last_hwnd: int | None = None
        self._before_run: set[int] = set()
        self._dialogs: dict[int, dict[str, Any]] = {}
        self._pick_host = ""
        #: The run's takeover watch (report 63), looked at inside every wait and just before every action.
        self._watch: Any = None
        #: Second verification N1: no RuntimeId re-find in a run that picks an item by the user's words.
        self._no_rid = False
        #: Second verification N3/N5, per run: the windows open just before the step before this one began, what its
        #: path said, and the window its own measured report saw come to the front (``None``: none).
        self._prev_pre: set[int] | None = None
        self._prev_exp: Expect | None = None
        self._prev_brought: int | None = None
        self._brought: int | None = None
        #: Second verification N2: the app a UWP frame window hosts, per window, for this run.
        self._hosted: dict[int, str] = {}

    def _stop_if_taken(self) -> None:
        """Raise :class:`_TakenOver` when the user took over (review F3: checked inside each step, not only between
        steps)."""
        if self._watch is not None and getattr(self._watch, "fired", None):
            raise _TakenOver()

    # -- the screen ---------------------------------------------------------------

    def _overview(self) -> dict[str, Any]:
        try:
            _text, payload, _ms = (self._disp or self.hands._dispatcher).overview_text()
            return payload if isinstance(payload, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        """One action through the run's dispatcher (capC5: report 58's account on for the real hands)."""
        return (self._disp or self.hands._dispatcher).dispatch(name, args)

    def _read(self, hwnd: int) -> tuple[dict[str, Any], int, float] | None:
        """One read of ``hwnd`` through the run's dispatcher: ``(tree, read, ms)`` (its element ids good for that
        read), or ``None``. The hands' own read when the run uses the hands' dispatcher."""
        disp = self._disp
        if disp is None or disp is getattr(self.hands, "_dispatcher", None):
            return self.hands._read_tree(hwnd)
        try:
            outcome = disp.dispatch("look_at_window", {"hwnd": int(hwnd)})
        except Exception:  # noqa: BLE001
            return None
        if not getattr(outcome, "ok", False) or not isinstance(getattr(outcome, "payload", None), dict):
            return None
        latest = getattr(disp, "_latest_trees", {}).get(int(hwnd))
        return outcome.payload, (int(latest[0]) if latest else 0), float(getattr(outcome, "elapsed_ms", 0) or 0.0)

    @staticmethod
    def _windows(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
        out = {int(w["hwnd"]): w for w in payload.get("windows") or [] if isinstance(w, dict) and w.get("hwnd")}
        # capC5: a dialog in front that the window list leaves out (Windows' own owner fact, ``GW_OWNER``) counts as a
        # window of its owner's app, so the replay guard sees it come up and a step may act inside it
        front, owner = int(payload.get("foreground_hwnd") or 0), int(payload.get("foreground_owner_hwnd") or 0)
        if front and owner and front not in out:
            out[front] = {"hwnd": front, "title": str(payload.get("foreground_title") or ""), "owner_hwnd": owner,
                          "process_name": str((out.get(owner) or {}).get("process_name") or ""), "unlisted": True}
        return out

    def _app_of(self, window: dict[str, Any] | None) -> str:
        """The app a window of the window list shows: its process, except for a UWP frame (second verification N2),
        whose hosted app's process it is - the way the trace records it (``trace._window_pid``), so a way learned in
        Settings is compared with Settings, never with the frame's host."""
        w = window or {}
        own = str(w.get("process_name") or w.get("app") or "")
        hwnd, pid = int(w.get("hwnd") or 0), int(w.get("pid") or 0)
        if not hwnd or not pid:
            return own
        if hwnd not in self._hosted:
            self._hosted[hwnd] = hosted_app(hwnd, pid)
        return self._hosted[hwnd] or own

    def _in_way(self, app: str, apps: list[str]) -> bool:
        from yuki.memory.store import app_key

        key = app_key(app) if app else None
        return bool(key) and key in {app_key(a) for a in apps if a}

    def _search(self, step: SkillStep, apps: list[str], payload: dict[str, Any], extra: int | None = None
                ) -> list[tuple[int, str, str]]:
        """``(hwnd, title, app)`` of the windows a click looks in: the one named, else the one in front, the popups
        memory's Now picture sees, then the way's own app's windows (capC5: ``extra``, the window the step's path
        says it stays in, right after the one in front)."""
        windows = self._windows(payload)
        front = int(payload.get("foreground_hwnd") or 0)
        board = self.hands._now_board() or {}
        popups = {int(p["hwnd"]): p for p in board.get("popups") or [] if isinstance(p, dict) and p.get("hwnd")}
        out: list[tuple[int, str, str]] = []

        def add(hwnd: int, title: str, app: str) -> None:
            if hwnd and all(hwnd != h for h, _t, _a in out):
                out.append((hwnd, title, app))

        if step.window:
            w = windows.get(step.window)
            p = popups.get(step.window)
            if w is not None:
                add(step.window, str(w.get("title") or ""), self._app_of(w))
            elif p is not None:
                add(step.window, str(p.get("title") or ""), str(p.get("app") or ""))
            return out
        if front:
            w = windows.get(front)
            p = popups.get(front)
            add(front, str((w or p or {}).get("title") or ""), self._app_of(w) if w else str((p or {}).get("app") or ""))
        if extra and extra in windows:
            add(int(extra), str(windows[extra].get("title") or ""), self._app_of(windows[extra]))
        for hwnd, p in list(popups.items())[:2]:
            add(hwnd, str(p.get("title") or ""), str(p.get("app") or ""))
        for hwnd, w in windows.items():
            if len(out) >= 5:
                break
            if not w.get("is_minimized") and self._in_way(self._app_of(w), apps):
                add(hwnd, str(w.get("title") or ""), self._app_of(w))
        return out

    def _acceptable(self, hwnd: int, app: str, windows: dict[int, dict[str, Any]], exp: Expect | None,
                    window: int | None) -> tuple[bool, str]:
        """capC5, the replay guard's "where": whether a step may act in ``hwnd`` (of ``app``): an app the step's path
        allows (``exp.apps``; none known: any), and when the path says the step stayed in the window of the step
        before, that very window (``window``) or one it owns (a dialog of it); a popup outside the window list counts
        as its app's. ``(ok, why)`` with why ``app`` or ``window``. Machine facts only: window identity and owner,
        the app's process; never a title or a position."""
        from yuki.memory.store import app_key

        if exp is not None and exp.apps:
            key = app_key(app) if app else ""
            if not key or key not in exp.apps:
                return False, "app"
        if window and int(hwnd) != int(window):
            w = windows.get(int(hwnd))
            if w is not None and int(w.get("owner_hwnd") or 0) != int(window):
                return False, "window"
        return True, ""

    def _want_window(self, exp: Expect | None) -> int | None:
        """The window a step must act in when its path says it stayed in the window of the step before."""
        return self._last_hwnd if exp is not None and exp.same_window and self._last_hwnd else None

    def _anchor(self, step: SkillStep, elements: list[dict[str, Any]], hwnd: int, app: str
                ) -> tuple[dict[str, Any] | None, str, int]:
        """capC5 (report 78 §7.2): the step's control in one read, by the anchors in order - a proven AutomationId
        (unique in the read with the name agreeing, or the only one with no name: C4), the role and name, and last the
        RuntimeId of the control this very step acted on earlier in this session (:func:`recall_rid`).
        ``(element, how, how many fit)``; ``(None, "", n)`` when none or more than one fit."""
        role, name = step.role.casefold(), _words(step.name).casefold()
        if step.aid:
            # capC4: a proven AutomationId (memory keeps it only where that app's are proven) finds the very control
            # first; its role must agree, and (second verification V2: generic ids shared by several controls) so must
            # its name - or it has no name and is the only one with that id in the read
            by_aid = [e for e in elements if str(e.get("automation_id") or "") == step.aid
                      and (not role or str(e.get("role") or "").casefold() == role)]
            named = [e for e in by_aid if _words(e.get("name")).casefold() == name]
            if len(named) == 1:
                return named[0], "aid", 1
            if len(by_aid) == 1 and not _words(by_aid[0].get("name")):
                return by_aid[0], "aid", 1
        same = [e for e in elements if _words(e.get("name")).casefold() == name]
        hits = [e for e in same if str(e.get("role") or "").casefold() == role] if role else []
        hits = hits or same   # a role said in other words ("link" for Hyperlink): the name decides
        if len(hits) > 1:
            hits = [e for e in hits if e.get("is_interactive", True)] or hits
        if len(hits) == 1:
            return hits[0], "name", 1
        if hits:
            # several fit: the target is ambiguous and the run stops (second verification N1: the RuntimeId is a
            # re-find, never a tie-break - the control an earlier request of the session used may be another row's)
            return None, "", len(hits)
        # none fits by name: the very control this step acted on earlier in the session, its name still bearing the one
        # it had (review F2: never a control whose name flipped); never in a run that picks by the user's words (N1:
        # the controls after such a pick belong to the item picked now)
        again = None if self._no_rid else recall_rid(self._session, step, hwnd, app, elements)
        if again is not None:
            return again, "rid", 1
        return None, "", 0

    def _find(self, step: SkillStep, apps: list[str], wait_s: float, exp: Expect | None = None) -> Located:
        """The step's control in a fresh read (waits up to ``wait_s`` for it): :class:`Located` ``found`` in a
        window the step's path allows; else ``ambiguous`` (more than one fits there), ``elsewhere`` (it fits only in
        a window off the path) or ``missing``."""
        rank = {"missing": 0, "elsewhere": 1, "ambiguous": 2}
        deadline = time.monotonic() + max(0.0, wait_s)
        want = self._want_window(exp)
        while True:
            self._stop_if_taken()
            payload = self._overview()
            windows = self._windows(payload)
            best = Located("missing")
            for hwnd, title, app in self._search(step, apps, payload, extra=want):
                got = self._read(hwnd)
                self._stop_if_taken()
                if got is None:
                    continue
                tree, read, _ms = got
                elements = [e for e in (tree.get("elements") or []) if isinstance(e, dict)
                            and isinstance(e.get("id"), int)]
                hit, how, n = self._anchor(step, elements, hwnd, app)
                ok, why = self._acceptable(hwnd, app, windows, exp, want)
                if hit is None:
                    if n > 1 and ok and rank["ambiguous"] > rank[best.status]:
                        best = Located("ambiguous", hwnd=hwnd, title=title, app=app, n=n)
                    continue
                if not ok:
                    if rank["elsewhere"] > rank[best.status]:
                        best = Located("elsewhere", hwnd=hwnd, title=title, seen_app=app, why=why)
                    continue
                return Located("found", hwnd=hwnd, read=read, element=hit, title=title, app=app, how=how,
                               host=page_host(tree))
            if time.monotonic() >= deadline:
                return best
            time.sleep(POLL_S)

    def _digests(self, app: str, role: str, names: list[str]) -> list[str] | None:
        """The keyed digests C1 takes over these names (memory's key, :meth:`MemoryClient.name_digests`), or ``None``
        when memory cannot answer."""
        memory = getattr(self.hands, "memory", None)
        if memory is None or not names:
            return None if memory is None else []
        try:
            got = memory.call("pick_digests", lambda c: c.name_digests(app, role, names), timeout_s=1.5)
        except Exception:  # noqa: BLE001 - no digests: no match, the run stops
            return None
        return [str(d or "") for d in got] if isinstance(got, list) and len(got) == len(names) else None

    def _box_digests(self, app: str, boxes: list[tuple[str, str, str]]) -> list[str] | None:
        """Second verification V8: the keyed digests C1 takes over a picked item's container (its role, its
        AutomationId, its parent's role: :meth:`MemoryClient.container_digests`), or ``None`` when memory cannot
        answer."""
        memory = getattr(self.hands, "memory", None)
        if memory is None or not boxes:
            return None if memory is None else []
        try:
            got = memory.call("pick_boxes", lambda c: c.container_digests(app, boxes), timeout_s=1.5)
        except Exception:  # noqa: BLE001 - no digests: no match, the run stops
            return None
        return [str(d or "") for d in got] if isinstance(got, list) and len(got) == len(boxes) else None

    def _pick(self, step: SkillStep, apps: list[str], wait_s: float, exp: Expect | None = None
              ) -> tuple[str, tuple[int, int, dict[str, Any], str, str] | None, int]:
        """capC4: the item a pick step means, in a fresh read: ``("found", (hwnd, read, element, title, app), 1)``, or
        ``(why, None, how many matched)`` with why ``no_container`` (the list is not on the screen), ``no_match`` (no visible
        child matches), ``many`` (more than one does) or ``no_digest`` (memory could not compute the digests). The
        container is found by its role (and name when the way knows it); its visible descendants of the item's role are
        the candidates (an item the screen does not show is not in the read). A fixed item matches by the keyed digest
        of its name; a slot by the request's words, as a click matches a name."""
        box_role, box_name = step.box_role.casefold(), _words(step.box_name).casefold()
        role = step.role.casefold()
        deadline = time.monotonic() + max(0.0, wait_s)
        why = "no_container"
        want = self._want_window(exp)
        self._pick_host = ""
        while True:
            self._stop_if_taken()
            payload = self._overview()
            windows = self._windows(payload)
            for hwnd, title, app in self._search(SkillStep(i=step.i, do="pick"), apps, payload, extra=want):
                ok_here, off = self._acceptable(hwnd, app, windows, exp, want)
                if not ok_here:
                    # capC5: a window off the way's path is never picked in (a list of the same role in another app
                    # or window); said only when nothing on the path holds the list
                    why = f"elsewhere_{off}" if why == "no_container" else why
                    continue
                got = self._read(hwnd)
                self._stop_if_taken()
                if got is None:
                    continue
                tree, read, _ms = got
                els = [e for e in (tree.get("elements") or []) if isinstance(e, dict) and isinstance(e.get("id"), int)]
                by_id = {int(e["id"]): e for e in els}
                boxes = {int(e["id"]) for e in els if str(e.get("role") or "").casefold() == box_role
                         and (not box_name or _words(e.get("name")).casefold() == box_name)}
                if not boxes:
                    continue
                why = "no_match" if why == "no_container" or why.startswith("elsewhere_") else why

                def inside(e: dict[str, Any]) -> bool:
                    parent, depth = e.get("parent"), 0
                    while isinstance(parent, int) and parent >= 0 and depth < PICK_DEPTH:
                        if parent in boxes:
                            return True
                        parent = (by_id.get(parent) or {}).get("parent")
                        depth += 1
                    return False

                # second verification V8: with the container's identity known, only items whose own container is that
                # one (C1's keyed digest of its role, AutomationId and parent's role); without it, the item must be the
                # only match in the whole window (a same-named item in another list makes it unclear)
                cands = [e for e in els if str(e.get("role") or "").casefold() == role and _words(e.get("name"))
                         and (inside(e) or not step.box_digest)]
                if step.box_digest and cands:
                    def box_of(e: dict[str, Any]) -> tuple[str, str, str]:
                        parent = by_id.get(e.get("parent")) if isinstance(e.get("parent"), int) else None
                        grand = by_id.get((parent or {}).get("parent")) if isinstance(
                            (parent or {}).get("parent"), int) else None
                        return (str((parent or {}).get("role") or ""), str((parent or {}).get("automation_id") or ""),
                                str((grand or {}).get("role") or ""))
                    boxed = self._box_digests(step.app or app, [box_of(e) for e in cands])
                    if boxed is None:
                        return "no_digest", None, 0
                    cands = [e for e, d in zip(cands, boxed) if d.casefold().startswith(step.box_digest.casefold())]
                if step.slot:
                    said_name = _words(step.text).casefold()   # (never ``want``: that is the path's window)
                    hits = [e for e in cands if said_name and _words(e.get("name")).casefold() == said_name]
                else:
                    digests = self._digests(step.app or app, step.role, [_words(e.get("name")) for e in cands])
                    if digests is None:
                        return "no_digest", None, 0
                    hits = [e for e, d in zip(cands, digests) if step.item and d.casefold().startswith(
                        step.item.casefold())]
                if not hits and not step.slot and not self._no_rid:
                    # capC5 anchor 4: the very item this step picked earlier in this session, by its RuntimeId (its name
                    # may have grown since: an unread count), in the same window of the same app; never for a pick by
                    # the user's words (review F1: that must match the name said now), nor in a run that has one (N1).
                    # Several fitting is never settled by it (N1: a re-find, not a tie-break): that stops as "many"
                    again = recall_rid(self._session, step, hwnd, app, cands)
                    hits = [again] if again is not None else []
                if len(hits) > 1:
                    return "many", None, len(hits)
                if len(hits) == 1 and (step.box_digest or inside(hits[0])):
                    self._pick_host = page_host(tree)
                    return "found", (hwnd, read, hits[0], title, app), 1
            if time.monotonic() >= deadline:
                return why, None, 0
            time.sleep(POLL_S)

    def _front_app(self) -> tuple[str, str]:
        """``(app key, title)`` of the window in front."""
        from yuki.memory.store import app_key

        payload = self._overview()
        front = int(payload.get("foreground_hwnd") or 0)
        window = self._windows(payload).get(front) or {}
        return app_key(self._app_of(window)) or "", str(window.get("title") or "")

    def _app_title(self, app: str) -> str:
        """Second verification V7: the title of ``app``'s own window now - the one in front when it is that app's, else
        its first window that is not minimized; ``""`` when it has none (any page it then shows is a new one)."""
        from yuki.memory.store import app_key

        want = app_key(app) or ""
        payload = self._overview()
        windows = self._windows(payload)
        front = windows.get(int(payload.get("foreground_hwnd") or 0)) or {}
        if not want or app_key(self._app_of(front)) == want:
            return str(front.get("title") or "")
        for w in windows.values():
            if not w.get("is_minimized") and app_key(self._app_of(w)) == want:
                return str(w.get("title") or "")
        return ""

    def end_ok(self, end: dict[str, Any], last: StepResult | None, before: tuple[str, str]) -> tuple[bool, str]:
        """capC4: whether a part reached its checked end (report 78 §6.2): a window of the app in front (``window_
        verified``); a new page of the app in front (``page_reached``: the window in front is that app's and its title
        is not ``before``'s - that app's own window's title just before the part's last step, second verification V7);
        otherwise the part's last step was verified by its own measured report. ``(ok, why)``."""
        from yuki.memory.store import app_key

        kind = str(end.get("kind") or "")
        want = app_key(str(end.get("app") or "")) or ""
        if kind in ("window_verified", "page_reached"):
            deadline = time.monotonic() + END_WAIT_S
            while True:
                app, title = self._front_app()
                if kind == "window_verified" and want and app == want:
                    return True, f"a window of {want} is in front"
                if kind == "page_reached" and (not want or app == want) and title and title != before[1]:
                    return True, "a new page is in front"
                if time.monotonic() >= deadline:
                    return False, (f"no window of {want or 'the app'} came to the front" if kind == "window_verified"
                                   else "no new page came up")
                time.sleep(POLL_S)
        if last is not None and last.kind == "action" and last.ok and last.verdict is True:
            return True, "its last step was verified"
        return False, "its last step's effect could not be seen"

    def _focus(self, wait_s: float) -> Any:
        """The text field that has the keyboard (a FocusSnapshot), waiting up to ``wait_s``; ``None``: none."""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            self._stop_if_taken()
            snap = self.hands._focus(timeout_s=0.4)
            if snap is not None and getattr(snap, "accepts_text", False) and not getattr(snap, "is_password", False):
                return snap
            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_S)

    def _shows(self, words: str, wait_s: float) -> bool:
        """Whether ``words`` show (whole words) in the title or a control name of the window in front, within
        ``wait_s``."""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            payload = self._overview()
            front = int(payload.get("foreground_hwnd") or 0)
            names = [str(self._windows(payload).get(front, {}).get("title") or "")]
            got = self._read(front) if front else None
            if got is not None:
                names += [str(e.get("name") or "") for e in (got[0].get("elements") or []) if isinstance(e, dict)]
            if said(words, names):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_S)

    # -- one step ------------------------------------------------------------------

    def _held(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any], *, force: bool = False) -> Any:
        """Yuki's own authorization check on a step, as the thinker's tool it is (revamp D S1, 34-review H1/H5: keys
        can close or send anything, a click on a button, menu item or link can send or delete, typing puts words in a
        field - all judged; other clicks only when the user's policy holds them back; nothing at all in an app whose
        "Act" switch is off): the verdict when it holds the step, else ``None``. Reads only their heard words and the
        calls."""
        off = self.hands.act_off(target.get("app")) if callable(getattr(self.hands, "act_off", None)) else None
        if off:
            return SimpleNamespace(allowed=False, question="", source="act_off", act_off=off,
                                   why=f'Act is switched off for {off} in Yuki\'s settings')
        authorize = getattr(self.hands, "authorize", None)
        if not callable(authorize):
            return None
        verdict = authorize(tool, tool_input, target, list(self._earlier), force=force)
        if verdict is not None and not getattr(verdict, "allowed", False):
            return verdict
        return None

    # -- the replay guard (capC5) ------------------------------------------------------

    @staticmethod
    def _as_done(exp: Expect | None) -> str:
        """How the guard's words name the path: the user's own recording, or what the way names."""
        return "as when you did it" if exp is not None and exp.recorded else "where this way works"

    def _elsewhere_words(self, target: str, loc: Located, exp: Expect | None) -> str:
        seen = app_words(loc.seen_app)
        if loc.why == "window":
            return f"{target} was in another {seen or 'app'} window, not the one this way was working in"
        where = ", ".join(app_words(a) for a in sorted(exp.apps)) if exp is not None and exp.apps else "the way's app"
        return f"{target} was only in {seen or 'another app'}, not in {where} {self._as_done(exp)}"

    def _page_off(self, exp: Expect | None, host: str) -> tuple[str, str] | None:
        """A different web page than the recording: the site of the window's page (its Document's address) against
        the recorded one; nothing said when either is not known (an app with no address is no web page)."""
        from yuki.memory.trace_replay import site

        if exp is None or not exp.host or not host or site(host) == exp.host:
            return None
        return "page", f"a different site was open ({site(host)}, not {exp.host} {self._as_done(exp)})"

    def _dialog_off(self, hwnd: int | None) -> tuple[str, str] | None:
        """A dialog came up after the step before (a way with no recording) and this step is not inside it; or one came
        up after the step before was looked at (:meth:`_late_dialog`, second verification N3)."""
        late = self._late_dialog(hwnd)
        if late is not None:
            return late
        if not self._dialogs:
            return None
        windows = self._windows(self._overview())
        owner = int((windows.get(int(hwnd or 0)) or {}).get("owner_hwnd") or 0)
        if hwnd and (int(hwnd) in self._dialogs or owner in self._dialogs):
            return None
        title = next((str(w.get("title") or "") for w in self._dialogs.values()), "")
        return "dialog", (f"a dialog came up (\"{_words(title)[:50]}\") that the way never goes through"
                          if title else "a dialog came up that the way never goes through")

    def _where_off(self, hwnd: int | None, app: str, exp: Expect | None, what: str) -> tuple[str, str] | None:
        """Keys and typing: whether the window they would go to is on the way's path (its app, its window when the
        step stays in the window before, the dialog a step brought up, the recorded site)."""
        windows = self._windows(self._overview())
        ok, why = self._acceptable(int(hwnd or 0), app, windows, exp, self._want_window(exp))
        if not ok:
            loc = Located("elsewhere", seen_app=app, why=why)
            return why, self._elsewhere_words(what, loc, exp)
        off = self._dialog_off(hwnd)
        if off is not None:
            return off
        if exp is not None and exp.host and hwnd:
            got = self._read(int(hwnd))
            return self._page_off(exp, page_host(got[0]) if got is not None else "")
        return None

    def _step(self, step: SkillStep, *, apps: list[str], first: bool, after_open: bool,
              start_front: int, opened: set[int], exp: Expect | None = None) -> StepResult:
        from yuki.agent.loop import action_verdict

        started, at = time.monotonic(), time.time()

        def done(**kw: Any) -> StepResult:
            kw.setdefault("ms", round((time.monotonic() - started) * 1000, 1))
            kw.setdefault("at", at)
            return StepResult(step=step, started=started, **kw)

        def off_path(code: str, different: str, target: str, what: str) -> StepResult:
            # capC5: the screen left the way's path: nothing is done, the run stops and hands over
            return done(ok=False, verdict=False, kind="read", tool="guard", target=target, why=different,
                        report=f"{different}; {what}", deviation=code, different=different)

        def held(verdict: Any, target: str) -> StepResult:
            off = getattr(verdict, "act_off", None)
            if off:
                return done(ok=False, verdict=False, kind="control", tool="policy", target=target,
                            why=str(getattr(verdict, "why", "")), report="Act is off for that app; nothing was done",
                            deviation="act_off", question=str(off))
            return done(ok=False, verdict=False, kind="control", tool="authorize", target=target,
                        why=f"Yuki's own check held it back ({getattr(verdict, 'why', '') or 'not plainly asked'})",
                        report="held by Yuki's own check; nothing was done", deviation="held",
                        question=str(getattr(verdict, "question", "") or ""))

        if step.do == "click":
            wait = AFTER_OPEN_WAIT_S if after_open else (FIRST_FIND_WAIT_S if first else FIND_WAIT_S)
            loc = self._find(step, apps, wait, exp)
            target = f'{step.role or "control"} "{step.name}"'
            if loc.status == "missing":
                return done(ok=False, verdict=False, kind="read", tool="find", target=target,
                            why="the control the way names is not on the screen",
                            report=f"no {target} on the screen (looked for {wait:g} s)", deviation="not_found",
                            different=f"{target} was not on the screen")
            if loc.status == "ambiguous":
                # the target ambiguous is the target not found as one control (the same deviation, said as it is)
                return off_path("not_found", f"{loc.n} controls fit {target}, so which one was meant was not clear",
                                target, "nothing was clicked")
            if loc.status == "elsewhere":
                return off_path(loc.why or "app", self._elsewhere_words(target, loc, exp), target,
                                "nothing was clicked")
            hwnd, read, element, title, app = loc.found() or (0, 0, {}, "", "")
            off = self._page_off(exp, loc.host) or self._dialog_off(hwnd)
            if off is not None:
                return off_path(off[0], off[1], target, "nothing was clicked")
            role_name = f'{element.get("role") or step.role or "control"} "{_words(element.get("name"))[:80]}"'
            from yuki.voice.hands import RISKY_CLICK_ROLES

            verdict = self._held("click", {"hwnd": hwnd, "element": role_name},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": role_name},
                                 force=str(element.get("role") or "") in RISKY_CLICK_ROLES)
            if verdict is not None:
                return held(verdict, target)
            self._stop_if_taken()   # review F3: the user took over while it looked or was checked: nothing done
            late = self._late_dialog(hwnd)   # second verification N3: a dialog that came up while it was checked
            if late is not None:
                return off_path(late[0], late[1], target, "nothing was clicked")
            try:
                lead = self.hands._front(hwnd)
            except Exception as exc:  # noqa: BLE001 - the hands' refusal: could not bring it up
                return done(ok=False, verdict=False, tool="click", target=target, app=app, hwnd=hwnd,
                            why="its window could not be brought to the front", report=str(exc)[:200],
                            deviation="window")
            self._stop_if_taken()
            outcome = self._dispatch("click", {"element": element["id"], "read": read, "expect_hwnd": hwnd})
            self._saw_front(outcome)
            verdict, why = action_verdict("click", outcome)
            role = str(element.get("role") or step.role or "control")
            if outcome.ok:
                remember_rid(self._session, step, hwnd, app, element)   # capC5 anchor 4, this session only
            if loc.how == "rid":
                why += "; found again as the very control an earlier step of this session used"
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="click",
                        target=f'{role} "{_words(element.get("name"))[:80]}" in "{title[:60]}"', app=app, hwnd=hwnd,
                        report=f"{lead}clicked {role} \"{_words(element.get('name'))[:80]}\": {outcome.summary}")
        if step.do == "pick":
            wait = AFTER_OPEN_WAIT_S if after_open else (FIRST_FIND_WAIT_S if first else FIND_WAIT_S)
            how, found, n = self._pick(step, apps, wait, exp)
            target = f"an item of the {step.box_role or 'list'}"
            if found is None:
                if how.startswith("elsewhere_"):
                    loc = Located("elsewhere", why=how.removeprefix("elsewhere_"),
                                  seen_app=self._app_of(self._windows(self._overview()).get(
                                      int(self._overview().get("foreground_hwnd") or 0))))
                    return off_path(loc.why, self._elsewhere_words(f"the {step.box_role or 'list'}", loc, exp),
                                    target, "nothing was picked")
                why = {"no_container": f"the {step.box_role or 'list'} the way picks from is not on the screen",
                       "many": f"{n} items match, so which one is not clear",
                       "no_digest": "memory could not tell which item it is"}.get(
                    how, "the item the way picks is not visible in it")
                return done(ok=False, verdict=False, kind="read", tool="find", target=target, why=why,
                            report=f"{why} (looked for {wait:g} s)", deviation="not_found", different=why)
            hwnd, read, element, title, app = found
            off = self._page_off(exp, self._pick_host) or self._dialog_off(hwnd)
            if off is not None:
                return off_path(off[0], off[1], target, "nothing was picked")
            role = str(element.get("role") or step.role or "item")
            # second verification V1: Yuki's own check sees whose item it is (the live name, as for a click), never
            # only "an item of the list"
            seen = f'{role} "{_words(element.get("name"))[:80]}" in the {step.box_role}'
            verdict = self._held("click", {"hwnd": hwnd, "element": seen},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": seen})
            if verdict is not None:
                return held(verdict, target)
            self._stop_if_taken()   # review F3: the user took over while it looked or was checked: nothing done
            late = self._late_dialog(hwnd)   # second verification N3
            if late is not None:
                return off_path(late[0], late[1], target, "nothing was picked")
            try:
                lead = self.hands._front(hwnd)
            except Exception as exc:  # noqa: BLE001
                return done(ok=False, verdict=False, tool="click", target=target, app=app, hwnd=hwnd,
                            why="its window could not be brought to the front", report=str(exc)[:200],
                            deviation="window")
            self._stop_if_taken()
            outcome = self._dispatch("click", {"element": element["id"], "read": read, "expect_hwnd": hwnd})
            self._saw_front(outcome)
            verdict, why = action_verdict("click", outcome)
            if outcome.ok:
                remember_rid(self._session, step, hwnd, app, element)   # capC5 anchor 4, this session only
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="click",
                        target=f"{target} in \"{title[:60]}\"", app=app, hwnd=hwnd,
                        report=f"{lead}picked a {role} in the {step.box_role}: {outcome.summary}")
        if step.do == "type":
            snap = self._focus(FOCUS_WAIT_S if not first else 0.4)
            if snap is None:
                return done(ok=False, verdict=False, kind="read", tool="focus", target="a text field",
                            why="no text field has the keyboard", report="no text field has the keyboard",
                            deviation="no_focus", different="no text field had the keyboard")
            hwnd = int(getattr(snap, "window", 0) or 0) or None
            window = self._windows(self._overview()).get(hwnd or 0)
            app = self._app_of(window)
            field_name = f'{getattr(snap, "role", "") or "field"} "{str(getattr(snap, "name", "") or "")[:60]}"'
            off = self._where_off(hwnd, app, exp, f"the field with the keyboard ({field_name})")
            if off is not None:
                return off_path(off[0], off[1], field_name, "nothing was typed")
            check = self._held("type_text", {"hwnd": hwnd, "text": step.text},
                               {"hwnd": hwnd, "app": app, "title": str((window or {}).get("title") or ""),
                                "element": field_name}, force=True)
            if check is not None:
                return held(check, field_name)
            self._stop_if_taken()
            late = self._late_dialog(hwnd)   # second verification N3
            if late is not None:
                return off_path(late[0], late[1], field_name, "nothing was typed")
            outcome = self._dispatch("type_text", {"text": step.text, **({"expect_hwnd": hwnd} if hwnd else {})})
            self._saw_front(outcome)
            verdict, why = action_verdict("type_text", outcome)
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="type_text",
                        target=field_name, app=app, hwnd=hwnd, report=f"typed into {field_name}: {outcome.summary}")
        if step.do == "keys":
            payload = self._overview()
            front = int(payload.get("foreground_hwnd") or 0)
            window = self._windows(payload).get(front)
            app = self._app_of(window)
            mine = front == start_front or front in opened
            in_app = (bool(app) and self._in_way(app, sorted(exp.apps))) if exp is not None and exp.apps else (
                self._in_way(app, apps) if apps else mine)
            said_app = app_words(app)
            if not front or not in_app:
                return done(ok=False, verdict=False, kind="read", tool="hotkey", target=f"window {said_app or front}",
                            why="the window in front is not the way's app", deviation="window",
                            report=f"the window in front is {said_app or 'unknown'}, not one the way acts in; no key "
                                   f"sent",
                            different=f"{said_app or 'another app'} was in front, not the app the shortcut is for")
            # capC5 (report 78 §7.3): a shortcut goes only into the app's verified window - one this run acted in or
            # opened, the one in front when it began (before any step acted), a window owned by one it acted in, or
            # (second verification N5) one the step just before brought to the front by its own measured report, when
            # that step's recording allows a window change. A window that came up by itself is not verified.
            verified = (front in self._acted or front in opened or (not self._acted and front == start_front)
                        or self._owned_by_acted(front) or self._brought_by_prev(front))
            if not verified:
                return off_path("window", f"a {said_app} window this way had not worked in was in front",
                                f"window {said_app or front}", "no key was sent")
            off = self._where_off(front, app, exp, f"the {said_app} window in front")
            if off is not None:
                return off_path(off[0], off[1], f"window {said_app or front}", "no key was sent")
            title = str((window or {}).get("title") or "")
            check = self._held("hotkey", {"hwnd": front, "keys": list(step.keys)},
                               {"hwnd": front, "app": app, "title": title})
            if check is not None:
                return held(check, f'keys in "{title[:60]}"')
            self._stop_if_taken()
            late = self._late_dialog(front)   # second verification N3
            if late is not None:
                return off_path(late[0], late[1], f"window {said_app or front}", "no key was sent")
            outcome = self._dispatch("hotkey", {"keys": list(step.keys), "expect_hwnd": front})
            self._saw_front(outcome)
            verdict, why = action_verdict("hotkey", outcome)
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="hotkey",
                        target=f'keys in "{str((window or {}).get("title") or "")[:60]}"', app=app, hwnd=front,
                        report=f"pressed {'+'.join(step.keys)}: {outcome.summary}")
        # the talker's own hands of the same names (scroll, open_app, open_url), as they verify themselves
        facts: dict[str, Any] = {"sent_at": None, "verified": None}
        hands = self.hands
        tool, tool_input = {"scroll": ("scroll", {"direction": step.direction, "amount": step.amount}),
                            "open_app": ("launch_app", {"query": step.name or step.app}),
                            "open_url": ("open_url", {"url": step.url, **({"app": step.app} if step.app else {})})
                            }[step.do]
        check = self._held(tool, tool_input, {})
        if check is not None:
            return held(check, step.what())
        self._stop_if_taken()
        try:
            if step.do == "scroll":
                hands.snapshot()   # the window may be new since the talker's summary
                payload = self._overview()
                hwnd = step.window or int(payload.get("foreground_hwnd") or 0)
                # capC5: the replay guard holds a scroll to the way's path too (its app, its window, the dialog)
                off = self._where_off(hwnd, self._app_of(self._windows(payload).get(hwnd)), exp,
                                      "the window in front") if exp is not None and exp.recorded else None
                off = off or self._late_dialog(hwnd)   # second verification N3
                if off is not None:
                    return off_path(off[0], off[1], f"window {hwnd}", "nothing was scrolled")
                self._stop_if_taken()
                ok, report, facts = hands._act("scroll", {"window": hwnd, "direction": step.direction,
                                                          "amount": step.amount}, facts)
                tool, target, app = "scroll", f"window {hwnd}", facts.get("app")
            elif step.do == "open_app":
                ok, report, facts = hands._act("open_app", {"name": step.name or step.app}, facts)
                tool, target, app = "launch_app", f'app "{step.name or step.app}"', step.name or step.app
            else:
                ok, report, facts = hands._open_url({"url": step.url, "app": step.app}, facts)
                tool, target, app = "open_url", step.url, step.app or None
        except _TakenOver:
            raise
        except Exception as exc:  # noqa: BLE001 - a hand's refusal or crash is this step's failure
            return done(ok=False, verdict=False, tool=step.do, target=step.what(), why="it was refused or failed",
                        report=str(exc)[:200], deviation="failed")
        verdict = facts.get("verified")
        return done(ok=bool(ok), verdict=verdict if ok else False, tool=tool, target=target, app=app,
                    why=("its report verified it" if verdict is True else "its report did not show it"
                         if verdict is False else "its report could not tell"),
                    report=str(report)[:STEP_REPORT_CHARS], hwnd=facts.get("hwnd"))

    # -- the run -------------------------------------------------------------------

    def run(self, args: dict[str, Any], *, item: dict[str, Any], words: list[str]) -> tuple[bool, str, dict[str, Any]]:
        """Run the way ``item`` (offered to the talker) with the talker's ``args``: ``(ok, report, facts)``.

        Raises :class:`SkillRefused` before any step runs when the call cannot be run as written."""
        if item.get("trace") or item.get("demo"):
            from yuki.memory.trace_replay import use_learned

            if not use_learned():
                # capC5: the A/B switch (read live): off, nothing the action trace taught is used
                raise SkillRefused("using what Yuki learned from how you work is switched off, so that way is not "
                                   "used")
        if item.get("trial"):
            # capC4, decision D3: a way the user did once is a hint for the thinker, never run alone
            raise SkillRefused("that way was seen only once (your way, seen once): it is a hint, not run alone")
        parts = [p for p in item.get("parts") or [] if isinstance(p, dict)]
        if any(isinstance(s, dict) and s.get("do") == "command" for p in parts for s in p.get("run") or []):
            raise SkillRefused("it runs a terminal command, which only the thinker's terminal tool may run")
        if len(parts) > MAX_PARTS or any(len(p.get("run") or []) > MAX_STEPS for p in parts):
            raise SkillRefused(f"more than {MAX_PARTS} parts of {MAX_STEPS} steps")
        if item.get("hint_only") or (item.get("demo") and not parts):
            # review F2: a way learned from what the user did that Yuki's hands cannot take as it is (an action of it
            # cannot be repeated, or it is longer than 4 parts) is a hint for the thinker; the talker never writes its
            # steps itself
            raise SkillRefused("that way is a hint for the thinker: it cannot be run step by step by your hands")
        if any(isinstance(p.get("end"), dict) and str(p["end"].get("kind") or "") not in PART_END_KINDS
               for p in parts[:-1]):
            # review F7: a part before the last ends only on a state checked live on the screen
            raise SkillRefused("a part of that way ends on a state that cannot be checked on the screen")
        run = ([s for p in parts for s in p.get("run") or [] if isinstance(s, dict)] if parts
               else [s for s in item.get("run") or [] if isinstance(s, dict)])
        given = parse_steps(args.get("steps"), cap=max(MAX_STEPS, len(run)) if parts else MAX_STEPS,
                            allow_empty=bool(parts))
        if parts:
            # only the steps that carry the user's words (typing, a pick by name): the way runs whole from memory;
            # else the way's own steps (all, or its last ones)
            if all(s.do in ("type", "pick") for s in given):
                try:
                    steps = fill_slots(given, run)
                except SkillRefused:
                    steps = fit_to_way(given, run)
            else:
                steps = fit_to_way(given, run)
        else:
            steps = fit_to_way(given, run)
        if not steps:
            # review F4: a way in parts whose typing steps the talker left out runs nothing - never "done"
            raise SkillRefused("no steps to run: give the way's typing and pick steps with the words from the request")
        if item.get("sends") and args.get("sends") is not True:
            # a demo never authorises a send: the way sent what was typed when the user did it, so whom and what must
            # be the user's own words now (check_sends)
            raise SkillRefused("it sends what is typed, and only their own words may say what goes to whom")
        way_text = str(item.get("text") or "")
        demo = bool(item.get("demo") or parts)
        why = check_sends(args, words) or (
            check_picks_for_send(steps, args) if item.get("sends") or args.get("sends") is True else None) or \
            check_texts(steps, words, "" if demo else way_text)   # V9: a demo's typing is only ever their words
        if why:
            raise SkillRefused(why)
        # capC4: where each part ends (the index of its last step in ``steps``) and the checked state it ends on
        ends: dict[int, tuple[int, dict[str, Any]]] = {}
        if parts:
            skipped = len(run) - len(steps)
            pos = 0
            for k, p in enumerate(parts, 1):
                pos += len(p.get("run") or [])
                last = pos - 1 - skipped
                if last >= 0 and isinstance(p.get("end"), dict):
                    ends[last] = (k, p["end"])
        apps = [str(a) for a in item.get("apps") or [] if a]
        # capC5: the way's recorded path, one expectation per step that runs (the talker may leave out leading steps)
        expects = self.expectations(item, steps, len(run) - len(steps) if run else 0)
        payload = self._overview()
        start_front = int(payload.get("foreground_hwnd") or 0)
        before = set(self._windows(payload))
        opened: set[int] = set()
        results: list[StepResult] = []
        deviation: StepResult | None = None
        after_open = False
        self._earlier = []
        self._acted, self._last_hwnd, self._before_run, self._dialogs = set(), None, set(before), {}
        self._prev_pre, self._prev_exp, self._prev_brought, self._brought = None, None, None, None
        self._hosted = {}
        # second verification N1: a run that picks an item by the user's words never re-finds a control by RuntimeId
        self._no_rid = any(s.do == "pick" and s.slot for s in steps)
        part_before = ("", "")
        watch = self._watch = self._watch_takeover()

        def taken(step: SkillStep) -> StepResult:
            # report 63: the user took over (their keys, clicks or a large mouse move): nothing more is done
            return StepResult(step=step, ok=False, verdict=False, kind="control", tool=TAKEOVER_TOOL,
                              why="you took over (your keys, clicks or a large mouse move)",
                              report="you took over, so nothing more was done", deviation="takeover",
                              at=time.time(), different="you took over")

        try:
            for n, step in enumerate(steps):
                if watch is not None and getattr(watch, "fired", None):
                    deviation = taken(step)
                    results.append(deviation)
                    break
                if n in ends and str(ends[n][1].get("kind") or "") == "page_reached":
                    # second verification V7: a new page means the end app's own window changed by this last step -
                    # its title now, not whatever window was in front when the part began (often another app's)
                    part_before = ("", self._app_title(str(ends[n][1].get("app") or "")))
                exp = expects[n] if n < len(expects) else None
                pre = set(self._windows(self._overview()))
                self._brought = None
                try:
                    result = self._step(step, apps=apps, first=n == 0, after_open=after_open,
                                        start_front=start_front, opened=opened, exp=exp)
                except _TakenOver:
                    # review F3: the user took over while this step looked for its control or was checked: it did
                    # nothing
                    deviation = taken(step)
                    results.append(deviation)
                    break
                self._dialogs = {}   # a dialog a step of a way with no recording brought up: checked by this step
                if result.kind == "action" and result.ok:
                    import json

                    self._earlier.append(f"{result.tool} {json.dumps(trace_steps([result], None)[0]['input'])}")
                if step.do in ("open_app", "open_url"):
                    opened |= set(self._windows(self._overview())) - before
                    after_open = True
                elif result.kind == "action":
                    after_open = False
                if result.kind == "action" and result.ok and result.deviation is None:
                    self._note_acted(step, result)
                    off = self._new_dialog(pre, exp, last=n + 1 >= len(steps))
                    if off is not None:
                        result.deviation, result.different = "dialog", off
                        result.why += f"; then {off}"
                # second verification N3/N5: what the next step checks a late dialog and a new window against
                self._prev_pre, self._prev_exp, self._prev_brought = pre, exp, self._brought
                if self._run_rest(n, step, steps, result, results, ends, part_before) is not None:
                    deviation = results[-1]
                    break
        finally:
            self._watch = None
            if watch is not None:
                try:
                    watch.stop()
                except Exception:  # noqa: BLE001
                    pass
        return self._finish(item, steps, parts, results, deviation, way_text)

    def _run_rest(self, n: int, step: SkillStep, steps: list[SkillStep], result: StepResult,
                  results: list[StepResult], ends: dict[int, tuple[int, dict[str, Any]]],
                  part_before: tuple[str, str]) -> str | None:
        """The rest of one step of the run (its expectation, the deviation, the part's end, no blind key): appends to
        ``results``; returns why the run stops (``results[-1]`` is the deviation) or ``None`` to go on."""
        if result.ok and result.deviation is None and result.verdict is None and step.expect:
            # The expectation settles what the action's own report could not tell; it never overrules a measured
            # verdict (live-like replay: the talker's paraphrase of "the tabs come back" was no text on the screen
            # while the click had verifiably restored them).
            if self._shows(step.expect, EXPECT_WAIT_S):
                result.verdict, result.why = True, f'"{step.expect[:40]}" shows on the screen, as the way expects'
            else:
                result.why += f'; "{step.expect[:40]}" was not seen on the screen either'
        results.append(result)
        if result.deviation is None and (not result.ok or result.verdict is False):
            result.deviation = "failed" if not result.ok else "no_effect"
        if result.deviation is not None:
            return result.deviation
        if n in ends:
            # capC4: a part ends on a checked state; the next part starts only once it is reached
            k, end = ends[n]
            ok_end, why_end = self.end_ok(end, result, part_before)
            if not ok_end:
                results.append(StepResult(step=step, ok=False, verdict=False, kind="read", tool="check",
                                          why=f"part {k} of the way did not reach its checked end ({why_end})",
                                          report=f"part {k} ended without its checked state: {why_end}",
                                          deviation="part_end", at=time.time(),
                                          different=f"part {k} did not end where it did when you did it ({why_end})"))
                return "part_end"
            if result.verdict is None:
                result.verdict, result.why = True, f"part {k} reached its checked end ({why_end})"
        nxt = steps[n + 1] if n + 1 < len(steps) else None
        if nxt is not None and result.verdict is None and nxt.do == "keys":
            # a key after a step whose effect could not be seen would be blind (v3.5: no blind keys)
            results.append(StepResult(step=nxt, ok=False, verdict=False, kind="read", tool="hotkey",
                                      why=f"the effect of step {step.i} could not be seen before this key",
                                      report=f"step {step.i}'s report could not show its effect, so no key was sent "
                                             f"after it", deviation="blind", at=time.time(),
                                      different=f"what step {step.i} did could not be seen, so no shortcut was sent"))
            return "blind"
        return None

    # -- capC5: the path, the windows acted in, dialogs, takeover --------------------------

    def expectations(self, item: dict[str, Any], steps: list[SkillStep], skipped: int) -> list[Expect]:
        """One :class:`Expect` per step that runs: from the way's recorded path when it was learned from the trace
        (memory's :meth:`~yuki.memory.api.MemoryClient.way_path`, the talker's left-out leading steps skipped), else
        the apps the way names (a pick's own app). Memory not answering: the apps the way names."""
        from yuki.memory.store import app_key

        way_apps = frozenset(k for k in (app_key(str(a)) for a in item.get("apps") or [] if a) if k)
        path: dict[str, Any] = {}
        pid = item.get("id")
        memory = getattr(self.hands, "memory", None)
        if (item.get("trace") or item.get("demo")) and memory is not None and pid is not None:
            try:
                got = memory.call("way_path", lambda c: c.way_path(int(pid)), timeout_s=1.5)
                path = got if isinstance(got, dict) else {}
            except Exception:  # noqa: BLE001 - no path: what the way names
                path = {}
        recorded = [s if isinstance(s, dict) else {} for s in path.get("steps") or []] if path.get("recorded") else []
        out: list[Expect] = []
        for n, step in enumerate(steps):
            rec = recorded[n + skipped] if 0 <= n + skipped < len(recorded) else {}
            if rec.get("known"):
                app = str(rec.get("app") or "")
                # second verification N4: an owned window after the step is on the path only when the recording of
                # this very step had one (a Windows dialog, or an app's own modal window the trace saw was owned); a
                # new top-level window recorded, or a dialog elsewhere in the app's recordings, never allows one
                out.append(Expect(apps=frozenset({app}) if app else way_apps, host=str(rec.get("host") or ""),
                                  same_window=bool(rec.get("same_window")), dialog=bool(rec.get("dialog")),
                                  new_window=bool(rec.get("new_window")), recorded=True))
                continue
            own = app_key(step.app) if step.do == "pick" and step.app else ""
            out.append(Expect(apps=frozenset({own}) if own else way_apps))
        return out

    def _note_acted(self, step: SkillStep, result: StepResult) -> None:
        """A step acted: its window is one this run verified (a shortcut may go there), and the last one."""
        from yuki.memory.store import app_key

        hwnd = int(result.hwnd or 0)
        if not hwnd and step.do in ("open_app", "open_url"):
            payload = self._overview()
            front = int(payload.get("foreground_hwnd") or 0)
            if result.verdict is True:
                # its own report verified the app's window is in front: that window
                hwnd = front
            else:
                # review F6: a switch whose report could not tell leaves no stale window to hold the next step to -
                # the one in front only when it is the app the step opened (Windows' process fact), else none
                want = app_key(step.app or step.name) if (step.app or step.name) else ""
                seen = app_key(self._app_of(self._windows(payload).get(front))) if front else ""
                self._last_hwnd = front if want and seen and seen == want else None
                return
        if hwnd:
            self._acted.add(hwnd)
            self._last_hwnd = hwnd

    def _new_dialog(self, pre: set[int], exp: Expect | None, *, last: bool) -> str | None:
        """capC5: a dialog came up after a step (a new window owned by another - Windows' own fact - since just before
        the step), of this way: owned by a window this run acted in, or of the app the step's path names (review F4:
        another app's popup elsewhere on the desktop is not the way's and never stops it). On the path when the
        recordings showed one (or a window change) after this step; never, when they recorded none; for a way with no
        recording, only when the next step acts inside it (checked then; after the last step it is left to the step's
        expectation and the part's end - review F5). Returns what was different, or ``None``."""
        windows = self._windows(self._overview())
        new = {h: w for h, w in windows.items() if h not in pre and int(w.get("owner_hwnd") or 0)
               and self._of_way(w, windows, exp)}
        if not new:
            return None
        if exp is not None and exp.dialog is True:
            return None
        title = next((_words(w.get("title")) for w in new.values() if _words(w.get("title"))), "")
        said_title = f" (\"{title[:50]}\")" if title else ""
        if exp is not None and exp.dialog is False:
            return f"a dialog came up{said_title} that never came up when you did it"
        if last:
            return None
        self._dialogs = new   # the next step must act inside it
        return None

    def _saw_front(self, outcome: Any) -> None:
        """Second verification N5: the window this step's own measured report saw come to the front (report 58's
        account: ``foreground_changed`` with the new ``foreground_hwnd``), kept for the next step."""
        payload = getattr(outcome, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        if isinstance(payload.get("type_text"), dict):
            payload = payload["type_text"]
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        if details.get("foreground_changed") is True and details.get("foreground_hwnd"):
            try:
                self._brought = int(details["foreground_hwnd"])
            except (TypeError, ValueError):
                self._brought = None

    def _owned_by_acted(self, hwnd: int) -> bool:
        """Whether ``hwnd`` is owned (through at most 4 owners) by a window this run acted in."""
        windows = self._windows(self._overview())
        owner, hops = int((windows.get(int(hwnd)) or {}).get("owner_hwnd") or 0), 0
        while owner and hops < 4:
            if owner in self._acted:
                return True
            owner, hops = int((windows.get(owner) or {}).get("owner_hwnd") or 0), hops + 1
        return False

    def _brought_by_prev(self, hwnd: int) -> bool:
        """Second verification N5: ``hwnd`` came up during the step just before (not open when it began) and that
        step's own report saw it come to the front; when that step was recorded, its recording had a window change
        after it too (a new window or a dialog). With no recording, the report's own fact decides."""
        if not hwnd or self._prev_brought != int(hwnd) or self._prev_pre is None or int(hwnd) in self._prev_pre:
            return False
        exp = self._prev_exp
        return exp is None or not exp.recorded or bool(exp.new_window) or exp.dialog is True

    def _of_way(self, w: dict[str, Any], windows: dict[int, dict[str, Any]], exp: Expect | None) -> bool:
        """Whether an owned window is this way's: owned (through at most 4 owners) by a window this run acted in, or
        of the app the step's path names (review F4: another app's popup elsewhere is not the way's)."""
        from yuki.memory.store import app_key

        mine = self._acted | ({self._last_hwnd} if self._last_hwnd else set())
        owner, hops = int(w.get("owner_hwnd") or 0), 0
        while owner and hops < 4:   # a dialog of a dialog of a window acted in
            if owner in mine:
                return True
            owner, hops = int((windows.get(owner) or {}).get("owner_hwnd") or 0), hops + 1
        key = app_key(self._app_of(w)) if self._app_of(w) else ""
        return bool(exp is not None and exp.apps and key and key in exp.apps)

    def _late_dialog(self, hwnd: int | None) -> tuple[str, str] | None:
        """Second verification N3: a dialog that came up after the step before was looked at (its one look missed it),
        checked just before this step acts: an owned window not open before the step before began nor before the run,
        that this step would act in, or that is in front and is the way's. On the path when the step before's
        recording had a dialog; off it when the recording had none; for a way with no recording, only when this step
        acts inside it. Machine facts only (window identity and owner). ``(code, what was different)`` or ``None``."""
        if self._prev_pre is None:
            return None
        payload = self._overview()
        windows = self._windows(payload)
        front = int(payload.get("foreground_hwnd") or 0)
        seen = self._prev_pre | self._before_run

        def late(h: int) -> bool:
            w = windows.get(h)
            return bool(h) and w is not None and h not in seen and bool(int(w.get("owner_hwnd") or 0))

        act = int(hwnd or 0)
        found = [h for h in dict.fromkeys((act, front)) if late(h)
                 and (h == act or self._of_way(windows[h], windows, self._prev_exp))]
        if not found:
            return None
        exp = self._prev_exp
        if exp is not None and exp.dialog is True:
            return None
        title = next((_words(windows[h].get("title")) for h in found if _words(windows[h].get("title"))), "")
        said_title = f" (\"{title[:50]}\")" if title else ""
        if exp is not None and exp.dialog is False:
            return "dialog", f"a dialog came up{said_title} that never came up when you did it"
        owner = int((windows.get(act) or {}).get("owner_hwnd") or 0)
        if act and (act in found or owner in found):
            return None
        return "dialog", f"a dialog came up{said_title} that the way never goes through"

    def _watch_takeover(self) -> Any:
        """Report 63's takeover watch while the steps run (polls only while acting, no hook), or ``None``: the one
        given, the hands' own (``takeover_watch``), else one for the real hands (a stand-in desk gets none)."""
        factory = self._takeover or getattr(self.hands, "takeover_watch", None)
        if factory is None and self._disp is not None and self._disp is not getattr(self.hands, "_dispatcher", None):
            try:
                from yuki.actions.leases import TakeoverThread

                def factory(on_takeover: Any) -> Any:
                    return TakeoverThread(on_takeover)
            except Exception:  # noqa: BLE001
                factory = None
        if factory is None:
            return None
        try:
            watch = factory(lambda kind: True)
            start = getattr(watch, "start", None)
            if callable(start):
                start()
            return watch
        except Exception:  # noqa: BLE001 - the run goes on without it
            return None

    def _finish(self, item: dict[str, Any], steps: list[SkillStep], parts: list[dict[str, Any]],
                results: list[StepResult], deviation: StepResult | None, way_text: str
                ) -> tuple[bool, str, dict[str, Any]]:
        """The run's report and facts (the talker's and the thinker's), and the handover words at a deviation."""
        verified = deviation is None and bool(results) and results[-1].verdict is True
        ref = str(item.get("ref") or f"pb:{item.get('id')}")
        intent = _words(item.get("intent"))[:80]
        lines = [f"{r.step.i}. {r.step.what()} -> "
                 + ("verified" if r.verdict is True else "failed" if not r.ok else "not verified" if r.verdict is False
                    else "could not tell") + f" ({r.why}): {_words(r.report)[:STEP_REPORT_CHARS]}"
                 for r in results if not (r.kind == "read" and r is deviation and r.deviation == "blind")
                 and not (r is deviation and r.deviation == "takeover")]
        facts: dict[str, Any] = {
            "verified": verified if deviation is None else False,
            "steps": len(steps), "steps_done": sum(1 for r in results if r.kind == "action" and r.ok),
            "deviated_at": deviation.step.i if deviation is not None else None,
            "deviation": deviation.deviation if deviation is not None else None,
            "sent_at": None, "results": results, "skill": ref,
        }
        if parts:
            facts["parts"] = len(parts)
        firsts = [r for r in results if r.kind == "action"]
        if firsts:
            facts["sent_at"] = firsts[0].started   # the turn log's end of speech -> action
        if deviation is None:
            report = (f"ran the learned way {ref} (\"{intent}\") by your hands, {len(steps)} step(s), each checked: "
                      + " ".join(lines))
            if not verified:
                report += " The last step's effect could not be seen."
                facts["skill_context"] = (
                    f"I ran my learned way {ref} (\"{intent}\") with my own hands; every step ran: " + " ".join(lines)
                    + " But the last step's own report could not show its effect. Look at the screen: if it is done, "
                    "say so; if not, finish it your way. Do not redo the steps that were done.")
            return True, report, facts
        done_lines = [ln for r, ln in zip([r for r in results if not (r is deviation and r.deviation == "takeover")],
                                          lines) if r is not deviation]
        if deviation.deviation in ("held", "act_off"):
            # Yuki's own check held a step (revamp D S1), or Act is off for the app (34-review H5): nothing more runs;
            # the user is asked or told, never the thinker
            if deviation.deviation == "act_off":
                facts["act_off"] = deviation.question
            else:
                facts["held"] = True
                facts["question"] = deviation.question
            report = (f"stopped the learned way {ref} (\"{intent}\") before step {deviation.step.i} "
                      f"({deviation.step.what()}): {deviation.why}. "
                      + (("Done before it: " + " ".join(done_lines)) if done_lines else "Nothing was done."))
            return False, report, facts
        # capC5: what was different, in plain words, first of all: "I stopped at step N: ..."
        handover = f"I stopped at step {deviation.step.i}: {deviation.different or deviation.why}."
        facts["handover"] = handover
        if deviation.deviation == "takeover":
            # report 63: the user's hands are on it - nothing more is done, nothing is handed to the thinker, and it
            # is not counted against the way (no deviation); the talker says it in a few words
            facts["deviated_at"], facts["deviation"] = None, None
            facts["took_over_at"], facts["settled"] = deviation.step.i, True
            report = (f"{handover} Nothing more was done; it is yours from here, so nothing more can happen now. "
                      + (("Done before it: " + " ".join(done_lines)) if done_lines else "Nothing was done."))
            return False, report, facts
        ran = deviation.kind == "action"   # the deviating step itself ran (its report showed no effect, or it failed)
        at = (f"step {deviation.step.i} ({deviation.step.what()}) " + ("ran, but " if ran else "was not done: ")
              + deviation.why) if deviation.deviation != "part_end" else (
            f"after step {deviation.step.i} ({deviation.step.what()}), {deviation.why}")
        if deviation.deviation == "part_end":
            ran = True
        report = (f"{handover} Stopped the learned way {ref} (\"{intent}\"): {at}. "
                  + (("Done before it: " + " ".join(done_lines)) if done_lines else
                     ("" if ran else "Nothing was done.")))
        facts["skill_context"] = (
            f"{handover} I tried my learned way {ref} (\"{intent}\") with my own hands. "
            + (("Done, each checked: " + " ".join(done_lines) + " ") if done_lines else "")
            + f"Then {at}, so the screen did not fit the way ({_words(deviation.report)[:200]}). Nothing after it was "
            f"done. Do the request your own way from where it is now; do not redo the steps that were done. The way's "
            f"steps were: {_words(way_text)[:400]}")
        return False, report, facts


def trace_steps(results: list[StepResult], pid: int | None) -> list[dict[str, Any]]:
    """The run's steps as practice memory keeps them (``yuki.memory.practice.record_run``), each citing the way."""
    out = []
    for r in results:
        s = r.step
        tool_input: dict[str, Any] = {"do": s.do}
        if s.do == "click":
            tool_input.update(role=s.role, name=s.name, **({"aid": s.aid} if s.aid else {}))
        elif s.do == "pick":
            # the container, the item's role and how it was matched; never the item's name (a content control's)
            tool_input.update(role=s.role, app=s.app, **{"in": {k: v for k, v in (("role", s.box_role),
                                                                                  ("name", s.box_name),
                                                                                  ("digest", s.box_digest)) if v}},
                              **({"item": s.item} if s.item else {"slot": ""}))
        elif s.do == "type":
            tool_input["text"] = s.text
        elif s.do == "keys":
            tool_input["keys"] = list(s.keys)
        elif s.do == "scroll":
            tool_input.update(direction=s.direction, amount=s.amount)
        elif s.do == "open_app":
            tool_input["name"] = s.name or s.app
        elif s.do == "open_url":
            tool_input.update(url=s.url, app=s.app)
        if s.expect:
            tool_input["expect"] = s.expect
        out.append({"tool": r.tool or s.do, "kind": r.kind, "app": r.app or None, "target": r.target,
                    "input": tool_input, "ok": bool(r.ok), "verdict": r.verdict, "why": r.why, "result": r.report,
                    "ms": r.ms, "at": r.at, **({"playbook": f"pb:{pid}"} if pid is not None else {})})
    return out


__all__ = ["RUN_SKILL_TOOL", "SkillRunner", "SkillRefused", "SkillStep", "parse_steps", "check_sends", "check_texts",
           "check_picks_for_send",
           "fit_to_way", "fill_slots", "way_step", "said", "trace_steps", "SKILL_DOS", "MAX_PARTS"]
