"""Recorded UI Automation routes (rung B) and by-name pattern calls inside containers (PLAN.md Phase S, D.2).

Moved here from ``yuki/voice/skills.py`` by sCore (PLAN.md B.2 "Recorded-path dependencies", critique H5): the step
runner below (:class:`SkillRunner`) is the skill engine's ``uia_steps`` verb, its steps now coming from a route spec
with slots filled by code (:func:`run_route`); ``trace_replay.site()`` is ported (:func:`site`) and a pick's keyed
digests are taken with the trace's own formula (:func:`yuki.skills.learn.ways.name_digest` /
:func:`~yuki.skills.learn.ways.container_digest`, kWays), so phase P can delete the trace learners without breaking
rung B. ``yuki.voice.skills`` is now this very module under its old name (one module object: what a test patches
there is patched here). The talker-written steps (``RUN_SKILL_TOOL``, :func:`parse_steps`, :func:`fit_to_way`,
:func:`fill_slots`, :func:`check_texts`) and the A/B switch stay only for the old voice path (``skills = false``)
until phase P deletes them; the engine's routes never consult ``use_learned``.

**The user's own ways** (kWays, contract C4; audit 17 Oct): the capability ``app.way`` (``connections/ways.toml``)
has one rung B route, :func:`route_way` (``ROUTES["way"]``, done-check ``PROBES["way_done"]``): a way the learner
kept (:mod:`yuki.skills.learn.ways`) is replayed by :class:`SkillRunner` on the voice hands or the brain's desk
hands: the place the ``item`` slot names (a control by its own label; else a row of the way's learned list, by a group
label of theirs or by the row's own text; a send naming only a group asks first which row), the field by its
identity, their own words typed, the end key only when they said to send (on the brain: only under its consent grant
for this step, else the planner gets it with the text typed). The done line names the row it picked.

The by-name part (:func:`find_in`, :func:`press`, :func:`app_window`) is the engine's ``uia_invoke`` verb and
``uia_name`` probe: a control found by its role and name **inside its container path**, searched from the window
root by ``FindAll`` (never ``ElementFromPoint``, never an ``IsOffscreen`` filter), then a pattern (Invoke,
SelectionItem.Select, Value.SetValue, RangeValue.SetValue) on it: no focus, no key, no pointer. A pattern call on a
game's own window is refused (``gamegate.refuse_input("uia_pattern", hwnd=..., front_ok=True)``); on any other window
it may run with a game in front (D17: it is not input and takes no focus). Two matches are never guessed between.

The old voice path's description follows.

Talker skills: a learned way run as the talker's hand, step by step, each step checked (v3.18, 07 §6 F2).

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
must be their words or the way's own: the talker cannot compose text. Two exceptions, both for the chat driver's own
steps only: a send the brain's consent grant covers whole (:func:`granted_send`), and a draft the skill resolver kept
in Yuki's own wording for a run that doesn't send (:func:`note_draft`, lResolve: the 10 Oct decision, "a message Yuki
words itself stays a draft"), typed by a run that is that one typing step and nothing after it, so it is never sent.

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

import threading
import time
import weakref
from contextlib import nullcontext
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping

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
    #: kWays, a slot pick of the user's own ways (:func:`route_way`), only among the rows of the way's learned list
    #: (composite names of 2 or more parts, :meth:`SkillRunner._way_rows`): ``part`` the words are a group label of
    #: theirs, a later part of the row's name ("..., work"): the first such row in reading order; ``span`` the words
    #: stand, whole words in order, in exactly one row's own text (its first part), among the siblings of rows that
    #: carry one of the way's labels (``labels``). A way's other steps say what they are (``field``: its field by its
    #: keyed digests; ``control``: the control its item slot named), so the brain's desk hands know them
    #: (:meth:`DeskHands._own`). ``""``: not a way's step (a slot pick's item's whole name is the words).
    match: str = ""
    #: kWays: the way's group labels (its learned things), the anchor of a ``span`` pick's list.
    labels: tuple[str, ...] = ()

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


def _person_id(value: Any) -> int | None:
    """A person's id as a grant or a send declares it (an int, never a bool); ``None`` for anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


#: The chat kind (memory's ``contacts.ROUTE_KINDS``) a send grant may cover: the person's own direct message. A group
#: chat or channel is read by other people too, so a grant never covers a send there (review r1 H1).
GRANT_KIND = "dm"


def _dm_person(sends: Mapping[str, Any]) -> int | None:
    """The person id a send declares, only when it goes into that person's own direct message (``kind`` ``dm``, as
    memory keeps the chat's kind); ``None`` for a group chat or channel, or a send that doesn't say."""
    return _person_id(sends.get("person_id")) if sends.get("kind") == GRANT_KIND else None


def granted_send(sends: Mapping[str, Any] | None, grant: Mapping[str, Any] | None) -> bool:
    """Whether the brain's consent grant for this very ``run_skill`` step (:attr:`DeskHands.grant`, passed on as
    :func:`send_grant` validated it) covers this send whole (failure fix-plan Item 11's L2): the grant is a send
    (row 4 allowed it and its countdown ran, or the user said yes to it), what goes out is exactly the granted text
    (case and spaces aside, never a part of it) and it goes to the granted person's own direct message: the same
    person id memory's people index gave both, on a chat whose kind memory keeps as ``dm``. Identity and equality
    only: a group chat or channel is never covered, even the person's own usual one (review r1 H1), and a grant for
    another text or another person covers nothing."""
    if not isinstance(sends, Mapping) or not isinstance(grant, Mapping) or grant.get("send") is not True:
        return False
    what = _folded(sends.get("what"))
    pid, gid = _dm_person(sends), _person_id(grant.get("person_id"))
    return bool(what) and what == _folded(grant.get("text")) and pid is not None and pid == gid


def check_sends(args: dict[str, Any], words: list[str], *, grant: Mapping[str, Any] | None = None) -> str | None:
    """Why a way that sends may not run (``None``: it may). ``sends`` is the talker's own declaration; when true, whom
    and what must both be the user's own words, or (``grant``: the brain's consent grant for this very step) exactly
    the text and the person the consent rule allowed and counted down (:func:`granted_send`)."""
    if args.get("sends") is not True:
        return None
    to, what = _words(args.get("to")), _words(args.get("what"))
    if not to or not what:
        return "it would send something, and their words do not plainly say both what it says and who it goes to"
    if granted_send(args, grant):
        return None
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
        slot=way.get("do") == "pick" and not way.get("item"), box_digest=_words(box.get("digest")),
        match=_words(way.get("match")), labels=tuple(_words(x) for x in way.get("labels") or [] if _words(x)))


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


#: lResolve (the 10 Oct decision: "a message Yuki words itself stays a draft"): how long a draft the skill resolver kept
#: in Yuki's own wording (:func:`note_draft`) may be typed by that skill's own steps. The run starts right after the
#: resolve, and its chat may take a few seconds to open (the request's budget is 5 s; two rungs may try it).
DRAFT_KEEP_S = 60.0
_drafts_lock = threading.Lock()
#: (the skill's own steps' ref, the text folded) -> when the resolver kept it (monotonic). RAM only, never logged.
_drafts: dict[tuple[str, str], float] = {}


def _skill_ref(key: Any) -> str:
    """The ref a skill's own steps run under (``chat.run_steps`` and :func:`run_route` give ``skill:<key>``)."""
    return f"skill:{_words(key)}"


def note_draft(skill: str, text: str) -> None:
    """The skill resolver kept ``text``, Yuki's own wording of a message (not a span of the user's words), for a run of
    the capability ``skill`` that doesn't send (``yuki.skills.resolve``, filled ``worded``: the 10 Oct decision, "a
    message Yuki words itself stays a draft"). For :data:`DRAFT_KEEP_S` a run of that skill's own steps may type
    exactly it (:func:`draft_of`, :func:`check_texts`'s ``draft``): a run that is that one typing step and nothing
    else, so no key, click or line break ever sends it, and never a run that declares a send or a learned way. RAM
    only; never logged."""
    folded = _folded(text)
    if not folded or not _words(skill):
        return
    now = time.monotonic()
    with _drafts_lock:
        for k in [k for k, at in _drafts.items() if not (0.0 <= now - at <= DRAFT_KEEP_S)]:
            _drafts.pop(k, None)
        _drafts[(_skill_ref(skill), folded)] = now


def draft_of(steps: list[SkillStep], item: Mapping[str, Any], args: Mapping[str, Any]) -> str:
    """The text this run may type as a draft Yuki worded (folded; :func:`check_texts`'s ``draft``), else ``""``. All
    of these must hold: the run is a skill's own steps (no demo, trace, trial or parts: never a learned way), it
    declares no send (``args.sends``, ``item.sends``), it is exactly one ``type`` step (nothing pressed, clicked or
    picked after it) whose text is one line as the resolver kept it (no line break or tab that could send it), and the
    resolver noted that very text for this skill's own steps within :data:`DRAFT_KEEP_S` (:func:`note_draft`).
    Identity and structure only."""
    if args.get("sends") is True or item.get("sends") or any(item.get(k) for k in ("demo", "trace", "trial", "parts")):
        return ""
    if len(steps) != 1 or steps[0].do != "type":
        return ""
    text = steps[0].text
    if not text or text != _words(text):
        return ""
    key = (str(item.get("ref") or ""), _folded(text))
    now = time.monotonic()
    with _drafts_lock:
        at = _drafts.get(key)
    return key[1] if at is not None and 0.0 <= now - at <= DRAFT_KEEP_S else ""


def check_texts(steps: list[SkillStep], words: list[str], way_text: str, *, granted: str = "",
                draft: str = "") -> str | None:
    """Why a step's text may not be typed (``None``: all may): only the user's own words or the way's own words
    (``way_text`` ``""`` for a way learned from what the user did: decision D8, its typing is always their words), or
    ``granted``: the whole text of a send the brain's consent grant covers (:func:`granted_send`), typed as it is
    (case and spaces aside), never a part of it; or ``draft`` (:func:`draft_of`, lResolve: the 10 Oct decision, "a
    message Yuki words itself stays a draft"): the whole text the skill resolver kept in Yuki's wording for a run that
    doesn't send, only in a run of type steps alone (nothing pressed or clicked after it), one line, never a part of
    it."""
    granted = _folded(granted)
    draft = _folded(draft) if steps and all(s.do == "type" for s in steps) else ""
    for step in steps:
        if step.do == "type" and granted and _folded(step.text) == granted:
            continue
        if step.do == "type" and draft and step.text == _words(step.text) and _folded(step.text) == draft:
            continue
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
    #: held: the consent rule handed the step over (``brain = "all"``: ``hands.authorize`` -> ``brain.check.one``
    #: says ``handover`` for a step that would send or join with no plan behind it): the planner takes the request
    #: (bSkill; bTalk merge note 4), the user is never asked "exactly this?"
    to_planner: bool = False


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


def site(host: Any) -> str:
    """A host as the guard compares it: lowercased, ``www.`` taken off (deterministic formatting of a machine fact;
    ported from ``yuki.memory.trace_replay.site``, which phase P deletes)."""
    return str(host or "").strip().lower().removeprefix("www.")


# ---------------------------------------------------------------------------------------------------------------------
# Ported from yuki.memory.trace_replay (phase L design section 7, critique L5): the A/B switch, the recorded path of a
# way (way_path with way_steps and _summary), and the recorded path of a learned rung-B route (recorded_path). Phase P
# deletes trace_replay once nothing imports it; until then the old memory call (MemoryClient.way_path) still works.
# ---------------------------------------------------------------------------------------------------------------------

#: The privacy file's key for the A/B switch (``[trace]`` table) and its default (``trace_replay.SWITCH_KEY``).
SWITCH_KEY = "use_learned"
SWITCH_DEFAULT = True
#: The trace's outcome classes that mean the window changed after a step, a dialog came up, or an owned window did.
WINDOW_CLASSES = ("window_changed",)
DIALOG_CLASS = "dialog"
OWNED_CLASS = "owned_window"
#: Row kinds that move to another app or page by themselves (never "the same window" as the step before).
MOVING_KINDS = ("switch", "nav")

_SWITCH_LOCK = threading.Lock()
_SWITCH_FILE: dict[str, Any] = {"key": None, "on": None}
_SWITCH_OVERRIDE: dict[str, Any] = {"on": None}


def set_use_learned(value: bool | None) -> None:
    """Force the A/B switch (``None``: read the settings file again). Ported from ``trace_replay.set_use_learned``."""
    with _SWITCH_LOCK:
        _SWITCH_OVERRIDE["on"] = None if value is None else bool(value)


def use_learned(path: Any = None) -> bool:
    """Whether Yuki uses what it learned from how the user works (``[trace] use_learned``, default on). Read live:
    one ``stat`` of the privacy file a call, parsed again only when it changed. Never raises. Ported from
    ``trace_replay.use_learned`` (the old voice path's switch; the engine's routes never consult it)."""
    with _SWITCH_LOCK:
        if _SWITCH_OVERRIDE["on"] is not None:
            return bool(_SWITCH_OVERRIDE["on"])
    try:
        from pathlib import Path

        from yuki.memory.privacy import user_config_path

        p = Path(path) if path is not None else user_config_path()
        try:
            st = p.stat()
            key = (str(p), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(p), 0, 0)
        with _SWITCH_LOCK:
            if _SWITCH_FILE["key"] == key and _SWITCH_FILE["on"] is not None:
                return bool(_SWITCH_FILE["on"])
        on = SWITCH_DEFAULT
        if key[1]:
            import tomllib

            with p.open("rb") as fh:
                table = tomllib.load(fh).get("trace")
            if isinstance(table, dict) and isinstance(table.get(SWITCH_KEY), bool):
                on = bool(table[SWITCH_KEY])
        with _SWITCH_LOCK:
            _SWITCH_FILE.update(key=key, on=on)
        return on
    except Exception:  # noqa: BLE001 - unreadable settings: the last good value, else the default
        with _SWITCH_LOCK:
            return bool(_SWITCH_FILE["on"]) if _SWITCH_FILE["on"] is not None else SWITCH_DEFAULT


def _summary(store: Any, row: Any) -> dict[str, Any]:
    """A trace row's outcome summary (decrypted counts of what changed after it), ``{}`` when it has none."""
    raw = row["outcome_ciphertext"] if "outcome_ciphertext" in row.keys() else None
    if not raw:
        return {}
    try:
        import json

        data = json.loads(store._dec(raw) or "{}")
    except Exception:  # noqa: BLE001 - another key, a broken value: nothing known
        return {}
    return data if isinstance(data, dict) else {}


def _page_site(store: Any, page_key: Any, cache: dict[str, str]) -> str:
    """The site of a page key when the timeline kept it as a web page (its host, cleartext), else ``""``."""
    if not page_key:
        return ""
    key = str(page_key)
    if key not in cache:
        try:
            rows = store._query("SELECT host FROM timeline WHERE page_key = ? AND host IS NOT NULL AND host != ''"
                                " ORDER BY id DESC LIMIT 1", (key,))
            cache[key] = site(rows[0][0]) if rows else ""
        except Exception:  # noqa: BLE001
            cache[key] = ""
    return cache[key]


def way_steps(o: Any) -> list[dict[str, Any]]:
    """A way's executable steps in order: its parts' steps, else its run (ported from ``trace_replay.way_steps``)."""
    parts = getattr(o, "parts", None) or []
    if parts:
        return [st for p in parts for st in p.get("run") or [] if isinstance(st, dict)]
    return [st for st in getattr(o, "run", None) or [] if isinstance(st, dict)]


def recorded_path(store: Any, actions: list[int]) -> dict[str, Any]:
    """The recorded path of a sequence of the user's trace actions, one entry per action: ``{"steps": [{"known",
    "app", "host", "same_window", "dialog", "new_window"}, ...], "recorded": bool}``. Machine facts only (the app key,
    the timeline's host for a web page, whether the window changed or a dialog came up after it), never words. The
    replay guard holds a step to it (a learned rung B carries it per step as ``rec``). Never raises."""
    empty: dict[str, Any] = {"steps": [], "dialog_apps": [], "recorded": False}
    try:
        from yuki.memory.store import app_key

        if not actions:
            return empty
        marks = ",".join("?" * len(actions))
        rows = {int(r["id"]): r for r in store._query(
            f"SELECT id, at, process, page_key, kind, source, outcome, outcome_ciphertext FROM ui_actions WHERE id IN"
            f" ({marks})", list(actions))}
        hosts: dict[str, str] = {}
        steps: list[dict[str, Any]] = []
        prev: Any = None
        prev_summary: dict[str, Any] = {}
        for aid in actions:
            r = rows.get(int(aid))
            if r is None or r["source"] not in ("user", "user_injected"):
                steps.append({"known": False})
                prev, prev_summary = None, {}
                continue
            summary = _summary(store, r)
            moved = any(int(prev_summary.get(c) or 0) > 0 for c in (*WINDOW_CLASSES, DIALOG_CLASS))
            same = (prev is not None and str(prev["process"] or "") == str(r["process"] or "")
                    and str(r["kind"] or "") not in MOVING_KINDS
                    and (str(prev["kind"] or "") in MOVING_KINDS or not moved))
            owned = int(summary.get(OWNED_CLASS) or 0) > 0
            steps.append({"known": True, "app": app_key(str(r["process"] or "")) or "",
                          "host": _page_site(store, r["page_key"], hosts), "same_window": bool(same),
                          "dialog": int(summary.get(DIALOG_CLASS) or 0) > 0 or owned,
                          "new_window": any(int(summary.get(c) or 0) > 0 for c in WINDOW_CLASSES) and not owned})
            prev, prev_summary = r, summary
        return {"steps": steps, "dialog_apps": [], "recorded": True}
    except Exception:  # noqa: BLE001 - no path: the runner checks only what the way names
        return empty


def _demo_tasks(store: Any, pid: int) -> list[int]:
    out: list[int] = []
    try:
        for r in store._query("SELECT run_key FROM playbook_events WHERE playbook_id = ? AND type = 'demonstrated'"
                              " AND run_key LIKE 'demo:%'", (int(pid),)):
            part = str(r[0]).split(":")
            if len(part) >= 2 and part[1].isdigit() and int(part[1]) not in out:
                out.append(int(part[1]))
    except Exception:  # noqa: BLE001
        return out
    return out


def _dialog_apps(store: Any, tasks: list[int]) -> list[str]:
    apps: list[str] = []
    for tid in tasks:
        try:
            spans = store._query("SELECT first_action, last_action FROM ui_task_actions WHERE task_id = ?", (int(tid),))
        except Exception:  # noqa: BLE001
            return apps
        for lo, hi in spans:
            for r in store._query("SELECT process, outcome_ciphertext FROM ui_actions WHERE id BETWEEN ? AND ?"
                                  " AND outcome = 'changed' AND source IN ('user', 'user_injected')"
                                  " AND outcome_ciphertext IS NOT NULL", (int(lo), int(hi))):
                if int(_summary(store, r).get(DIALOG_CLASS) or 0) > 0 and r["process"] and r["process"] not in apps:
                    apps.append(str(r["process"]))
    return apps


def way_path(store: Any, pid: Any) -> dict[str, Any]:
    """The recorded path of one way of practice memory (ported from ``trace_replay.way_path``, which phase P deletes):
    ``{"steps": [...one per executable step...], "dialog_apps": [...], "recorded": bool}``; ``recorded`` False for a
    way not learned from the trace or whose cited actions do not line up one per step. Never raises."""
    empty: dict[str, Any] = {"steps": [], "dialog_apps": [], "recorded": False}
    try:
        from yuki.memory import practice as P
        from yuki.memory.store import app_key

        o = P.get(store, pid)
        if o is None or o.kind != "procedure":
            return empty
        trace = (o.body or {}).get("trace") if isinstance(o.body, dict) else None
        if not isinstance(trace, dict):
            return empty
        actions = [int(a) for a in trace.get("actions") or [] if str(a).isdigit()]
        run = way_steps(o)
        if not actions or len(actions) != len(run):
            return empty
        got = recorded_path(store, actions)
        if not got.get("recorded"):
            return empty
        tasks = _demo_tasks(store, int(o.id))
        if trace.get("task") and int(trace["task"]) not in tasks:
            tasks.append(int(trace["task"]))
        got["dialog_apps"] = [app_key(a) or a for a in _dialog_apps(store, tasks)]
        return got
    except Exception:  # noqa: BLE001
        return empty


def page_host(tree: dict[str, Any] | None) -> str:
    """The site of the web page a read shows (its Document's address, host only, ``www.`` off), ``""`` when none:
    a machine fact of the window, compared host to host (never titles)."""
    from urllib.parse import urlsplit

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


def _one_call() -> Any:
    """Phase 4 (``brain = "one"``): the ONE runner's call running on this thread (``voice.hands.one_now``), else
    ``None``. Imports nothing: with the hands module not loaded no ONE call can be running. Never raises."""
    import sys

    module = sys.modules.get("yuki.voice.hands")
    now = getattr(module, "one_now", None) if module is not None else None
    try:
        return now() if callable(now) else None
    except Exception:  # noqa: BLE001 - unreadable: no ONE call (the step as before)
        return None


class SkillRunner:
    """Runs one learned way through a :class:`yuki.voice.hands.Hands`' backend, step by step (blocking: off the loop)."""

    def __init__(self, hands: Any, *, takeover: Any = None, held_all: bool = False) -> None:
        self.hands = hands
        #: Phase L (design section 4.4, critique H4): a learned recorded path holds every step for Yuki's consent
        #: check, not only the ones the user's policy holds back.
        self._held_all = bool(held_all)
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
        #: The windows open just before the current step acted (seen by its last :meth:`_late_dialog` look).
        self._at_act: set[int] = set()
        self._brought: int | None = None
        #: Second verification N2: the app a UWP frame window hosts, per window, for this run.
        self._hosted: dict[int, str] = {}
        #: kWays, per run: the names of the rows a way's pick clicked (RAM only: the done line names the row).
        self.picked: list[str] = []

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
        """The keyed digests C1 takes over these names (memory's key: the trace's own formula,
        :func:`yuki.skills.learn.ways.name_digest`), or ``None`` when memory cannot answer. A way keeps a digest's
        first characters; a full digest starts with them."""
        memory = getattr(self.hands, "memory", None)
        if memory is None or not names:
            return None if memory is None else []
        from yuki.memory.store import app_key
        from yuki.skills.learn.ways import name_digest

        key = app_key(app) or app

        def read(c: Any) -> list[str]:
            store = getattr(c, "store", c)
            return [name_digest(store, key, role, str(n or "")) for n in list(names)[:400]]

        try:
            got = memory.call("pick_digests", read, timeout_s=1.5)
        except Exception:  # noqa: BLE001 - no digests: no match, the run stops
            return None
        return [str(d or "") for d in got] if isinstance(got, list) and len(got) == len(names) else None

    def _box_digests(self, app: str, boxes: list[tuple[str, str, str]]) -> list[str] | None:
        """Second verification V8: the keyed digests C1 takes over a picked item's container (its role, its
        AutomationId, its parent's role: :func:`yuki.skills.learn.ways.container_digest`), or ``None`` when memory
        cannot answer."""
        memory = getattr(self.hands, "memory", None)
        if memory is None or not boxes:
            return None if memory is None else []
        from yuki.memory.store import app_key
        from yuki.skills.learn.ways import container_digest

        key = app_key(app) or app

        def read(c: Any) -> list[str]:
            store = getattr(c, "store", c)
            return [container_digest(store, key, *(str(x or "") for x in tuple(b)[:3])) for b in list(boxes)[:400]]

        try:
            got = memory.call("pick_boxes", read, timeout_s=1.5)
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
                if step.slot and step.match:
                    hits = self._way_rows(step, cands)   # kWays: only the rows of the way's learned list
                elif step.slot:
                    said_name = _words(step.text).casefold()   # (never ``want``: that is the path's window)
                    hits = [e for e in cands if said_name and _words(e.get("name")).casefold() == said_name]
                else:
                    digests = self._digests(step.app or app, step.role, [_words(e.get("name")) for e in cands])
                    if digests is None:
                        return "no_digest", None, 0
                    # kWays: a way's field may have been drawn under several names (a composer in two layouts)
                    wanted = [w.casefold() for w in step.item.split("|") if w]
                    hits = [e for e, d in zip(cands, digests) if any(d.casefold().startswith(w) for w in wanted)]
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

    @staticmethod
    def _way_rows(step: SkillStep, cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """kWays (review H1): a way's slot pick, only among the rows of its learned list: a candidate whose composite
        name has 2 or more parts (UI Automation's ", "-joined row texts; a command button's one-part name never is
        one). ``part``: the label is a later part of the row's name (never its own text, the learner's rule), the
        first such row in reading order. ``span``: the words stand in the row's own text (its first part), among the
        siblings of rows that carry one of the way's labels (the list itself, never a toolbar)."""
        if not _words(step.text):
            return []
        rows = [(e, _name_parts(e.get("name"))) for e in cands]
        rows = [(e, parts) for e, parts in rows if len(parts) >= 2]
        if step.match == "part":
            label = _folded(step.text)
            return [e for e, parts in rows if label in {p.casefold() for p in parts[1:]}][:1]
        labels = {_folded(x) for x in step.labels if _words(x)}
        homes = {e.get("parent") for e, parts in rows if labels & {p.casefold() for p in parts[1:]}}
        return [e for e, parts in rows if e.get("parent") in homes and said(step.text, [parts[0]])]

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
        calls.

        Phase 4 (``brain = "one"``): while the ONE runner's call runs on this thread, the ONE gate decides the step
        (``yuki.one.runner.skill_step``): the Act switch of the step's app and of the run's skill's own app; with the
        request's desk hands their own steps under the gate's grant, the gate again on the send key (once, for
        exactly the counted text) and on the join press (their plain words); with any other hands the gate itself.
        Nothing is handed over. Under ``all`` nothing changes."""
        one = _one_call()
        if one is not None:
            from yuki.one.runner import skill_step

            return skill_step(self.hands, tool, tool_input, target, list(self._earlier),
                              force=force or self._held_all, ctx=one)
        off = self.hands.act_off(target.get("app")) if callable(getattr(self.hands, "act_off", None)) else None
        if off:
            return SimpleNamespace(allowed=False, question="", source="act_off", act_off=off,
                                   why=f'Act is switched off for {off} in Yuki\'s settings')
        authorize = getattr(self.hands, "authorize", None)
        if not callable(authorize):
            # a learned recorded path never runs a step its consent check can't see (design section 4.4)
            if self._held_all:
                return SimpleNamespace(allowed=False, question="", source="no_check", why="no consent check")
            return None
        verdict = authorize(tool, tool_input, target, list(self._earlier), force=force or self._held_all)
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
                        question=str(getattr(verdict, "question", "") or ""),
                        to_planner=getattr(verdict, "handover", False) is True)

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
                                 {"hwnd": hwnd, "app": app, "title": title, "element": role_name,
                                  **({"way_step": step.match} if step.match else {})},
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
            # kWays (review H1): a way's pick says how it matched, so the desk hands never pass a row found by the
            # user's words as the capability's own step (``DeskHands._own``: Yuki's one-step check sees it)
            verdict = self._held("click", {"hwnd": hwnd, "element": seen},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": seen,
                                  **({"way_step": step.match} if step.match else {})})
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
                if step.slot and step.match:
                    self.picked.append(_words(element.get("name")))   # kWays: the done line names it (RAM only)
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
                # integ22 fix S5: a told way's address opens as recorded (never taken for another channel)
                ok, report, facts = hands._open_url({"url": step.url, "app": step.app}, facts, heard=False)
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

    def run(self, args: dict[str, Any], *, item: dict[str, Any], words: list[str],
            grant: Mapping[str, Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
        """Run the way ``item`` (offered to the talker) with the talker's ``args``: ``(ok, report, facts)``.
        ``grant``: the brain's consent grant for this very ``run_skill`` step (the chat driver's own steps on the
        desk hands only; the voice and the old loop pass none): a send it covers whole (:func:`granted_send`) may
        type and send its text though the user's words don't hold it; the desk hands still check the send key
        against their own grant.

        Raises :class:`SkillRefused` before any step runs when the call cannot be run as written."""
        if item.get("trace") or item.get("demo"):
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
        # Item 11's L2: a send the brain's consent grant covers whole (its exact text, to its person) is the user's
        # instruction, though Yuki worded it; never a learned way's (a demo, its parts) and never a part of the text
        granted = str(args.get("what") or "") if (not demo and args.get("sends") is True
                                                    and granted_send(args, grant)) else ""
        # lResolve, the 10 Oct decision ("a message Yuki words itself stays a draft"): the draft the skill resolver kept
        # in Yuki's wording, typed by this skill's own one typing step and nothing after it; never a learned way's
        draft = "" if demo else draft_of(steps, item, args)
        why = check_sends(args, words, grant=grant if not demo else None) or (
            check_picks_for_send(steps, args) if item.get("sends") or args.get("sends") is True else None) or \
            check_texts(steps, words, "" if demo else way_text, granted=granted,
                        draft=draft)   # V9: a demo's typing: their words
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
        self.picked = []
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
                self._brought, self._at_act = None, set()
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
                    off = self._new_dialog(pre | self._at_act, exp, last=n + 1 >= len(steps))
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
        path: dict[str, Any] = item.get("path") if isinstance(item.get("path"), dict) else {}
        pid = item.get("id")
        memory = getattr(self.hands, "memory", None)
        if not path and (item.get("trace") or item.get("demo")) and memory is not None and pid is not None:
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
        payload = self._overview()
        windows = self._windows(payload)
        # what was open just before this step acted: a dialog among them did not come up after it (its own look)
        self._at_act |= set(windows)
        if self._prev_pre is None:
            return None
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
                if deviation.to_planner:
                    # bSkill (bTalk merge note 4): a send or join the consent rule handed over; the engine and the
                    # chat driver take it to rung C (the plan seed), never "exactly this?"
                    facts["to_planner"] = True
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


# =====================================================================================================================
# The skill engine's by-name pattern calls (verb ``uia_invoke``, probe ``uia_name``) and rung B's entry (``uia_steps``)
# =====================================================================================================================

#: The patterns a ``uia_invoke`` route may call (D0: UI Automation's Invoke, SelectionItem, Value, RangeValue).
#: chat19: ``focus`` is the element's own ``SetFocus`` (no pattern, no key, no pointer): the keyboard put in a field the
#: way a user's Tab would, e.g. an opened chat's composer (``chat._composer_focus``). It takes focus, so the game gate
#: is asked as for any focus (never ``front_ok``, :func:`press`).
PATTERNS: tuple[str, ...] = ("invoke", "select", "value", "range", "focus")
_PATTERN_IDS: dict[str, int] = {"invoke": 10000, "select": 10010, "value": 10002, "range": 10003}
#: One by-name search's budget on a warm window (its own UIA thread; a provider that blocks costs this, never more).
FIND_BUDGET_S = 0.6


@dataclass
class Found:
    """What a by-name search found: ``status`` ``found`` (exactly one), ``missing`` (the container is there, the
    control is not), ``ambiguous`` (``n`` > 1 fit: never guessed between), ``no_container``, ``no_window``,
    ``timeout`` or ``error``. ``elements`` are the live elements (RAM only); ``n`` how many fit."""

    status: str
    n: int = 0
    elements: list[Any] = field(default_factory=list)
    ms: float = 0.0


def app_window(process: str) -> int | None:
    """The top-level window of a program (its image name, case aside), minimised included; the largest wins when it
    has several. Read only (window enumeration); ``None`` when it has none."""
    want = str(process or "").strip().casefold()
    if not want:
        return None
    try:
        import win32gui
        import win32process

        from yuki.connect.spotify_desktop import _image_of
        from yuki.perception.windows import is_user_window
    except Exception:  # noqa: BLE001
        return None
    found: list[tuple[int, int]] = []

    def collect(hwnd: int, _extra: Any) -> bool:
        try:
            if not is_user_window(hwnd):
                return True
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            if _image_of(pid).casefold() != want:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = 0 if win32gui.IsIconic(hwnd) else (right - left) * (bottom - top)
            found.append((area, int(hwnd)))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001
        return None
    return max(found)[1] if found else None


def _steps(container: Any) -> tuple[Any, ...]:
    from yuki.connect.spotify_desktop import Step

    out = []
    for item in container or []:
        try:
            out.append(Step(str(item[0]), str(item[1])))
        except (IndexError, TypeError):
            continue
    return tuple(out)


def search(tree: Any, *, role: str, name: str, container: Any = ()) -> Found:
    """The by-name search on one window's live tree (``yuki.connect.spotify_desktop.UiaTree``): the container path's
    first step by ``FindAll`` from the root, its subtree in one request, the rest inside it, then every element of
    ``role`` named exactly ``name`` inside the container(s). No container: from the window root."""
    from yuki.connect.spotify_desktop import _container, local_find

    path = _steps(container)
    if path:
        scopes = _container(tree, path)
        if not scopes:
            return Found("no_container")
        hits = [e for s in scopes for e in local_find(s, role or None, name)]
    else:
        hits = tree.find(role or None, name)
    if not hits:
        return Found("missing")
    if len(hits) > 1:
        return Found("ambiguous", n=len(hits), elements=hits)
    return Found("found", n=1, elements=hits)


def _uia(job: Any, budget_s: float, automation_fn: Any = None) -> tuple[dict, bool]:
    from yuki.connect.spotify_desktop import run_uia

    return run_uia(job, budget_s, name="yuki-skill-uia", automation_fn=automation_fn)


def find_in(hwnd: int, *, role: str, name: str, container: Any = (), budget_s: float = FIND_BUDGET_S,
            automation_fn: Any = None) -> Found:
    """:func:`search` on ``hwnd``'s live tree, on its own UIA thread within ``budget_s`` (read only)."""
    if not hwnd:
        return Found("no_window")
    t0 = time.perf_counter()

    def job(automation: Any, shared: dict) -> None:
        from yuki.connect.spotify_desktop import UiaTree

        shared["found"] = search(UiaTree(automation, int(hwnd)), role=role, name=name, container=container)

    shared, late = _uia(job, budget_s, automation_fn)
    ms = round((time.perf_counter() - t0) * 1000.0, 1)
    if late:
        return Found("timeout", ms=ms)
    got = shared.get("found")
    if not isinstance(got, Found):
        return Found("error", ms=ms)
    got.ms = ms
    return got


def apply_pattern(element: Any, pattern: str, value: Any = None, module: Any = None) -> bool:
    """One pattern call on a live element (no key, no pointer): Invoke, SelectionItem.Select, Value.SetValue(text) or
    RangeValue.SetValue(number); ``focus`` (chat19) is the element's own ``SetFocus``, the only one that moves the
    keyboard. False when the element doesn't offer it or the call failed."""
    if pattern == "focus":
        try:
            element.SetFocus()
            return True
        except Exception:  # noqa: BLE001
            return False
    if pattern not in _PATTERN_IDS:
        return False
    try:
        if module is None:
            from yuki.perception.tree import _uia_core

            module = _uia_core()
        raw = element.GetCurrentPattern(_PATTERN_IDS[pattern])
        if not raw:
            return False
        if pattern == "invoke":
            raw.QueryInterface(module.IUIAutomationInvokePattern).Invoke()
        elif pattern == "select":
            raw.QueryInterface(module.IUIAutomationSelectionItemPattern).Select()
        elif pattern == "value":
            raw.QueryInterface(module.IUIAutomationValuePattern).SetValue(str(value if value is not None else ""))
        else:
            raw.QueryInterface(module.IUIAutomationRangeValuePattern).SetValue(float(value))
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Pressed:
    """What :func:`press` did: ``outcome`` ``pressed`` (the pattern call returned), ``failed`` (the element refused
    it), ``refused`` (a game's own window: nothing sent), or a :class:`Found` status that stopped it first."""

    outcome: str
    n: int = 0
    why: str = ""
    ms: float = 0.0
    #: fHands: a ``refused`` press's kind (``yuki.actions.gate``): ``"game"`` (a game's own window) or ``"blocked"``
    #: (the gate could not check and failed closed, or another guard refused it: a settled stop, never a game).
    kind: str = ""


def press(hwnd: int, *, role: str, name: str, pattern: str = "invoke", value: Any = None, container: Any = (),
          budget_s: float = FIND_BUDGET_S, automation_fn: Any = None, module: Any = None) -> Pressed:
    """Find one control by role and name inside its container, then call ``pattern`` on it, in one UIA job on its own
    thread. Refused (nothing sent) when the window belongs to a game (D17: a pattern call on any other window may run
    with a game in front); ``focus`` (chat19: ``SetFocus`` activates the element's window) is refused with a game in
    front too, as every focus is. Never two candidates: ``ambiguous`` and nothing pressed."""
    from yuki.actions import gate

    if pattern not in PATTERNS:
        return Pressed("error", why="pattern")
    if not hwnd:
        return Pressed("no_window")
    focus = pattern == "focus"
    if (why := gate.refuse("uia_focus" if focus else "uia_pattern", hwnd=int(hwnd), front_ok=not focus)) is not None:
        return Pressed("refused", why=str(why), kind=gate.kind_of(why))
    t0 = time.perf_counter()
    gone = threading.Event()

    def job(automation: Any, shared: dict) -> None:
        from yuki.connect.spotify_desktop import UiaTree

        found = search(UiaTree(automation, int(hwnd)), role=role, name=name, container=container)
        shared["found"] = found
        # never pressed after the caller gave up (a late press would act on a screen nobody is checking)
        if found.status == "found" and not gone.is_set():
            shared["pressed"] = apply_pattern(found.elements[0].ref, pattern, value, module)

    shared, late = _uia(job, budget_s, automation_fn)
    gone.set()
    ms = round((time.perf_counter() - t0) * 1000.0, 1)
    found = shared.get("found")
    if late and "pressed" not in shared:
        return Pressed("timeout", ms=ms)
    if not isinstance(found, Found):
        return Pressed("error", ms=ms)
    if found.status != "found":
        return Pressed(found.status, n=found.n, ms=ms)
    return Pressed("pressed" if shared.get("pressed") else "failed", n=1, ms=ms)


def route_steps(raw: Any, values: dict[str, Any]) -> list[dict[str, Any]]:
    """A route spec's steps with their slots filled by code (``{"do": "type", "text": "{text}"}`` -> the slot's
    value), in the step runner's own form. Raises :class:`SkillRefused` when a step names a slot with no value."""
    from yuki.skills.connection import fill_value

    out: list[dict[str, Any]] = []
    for i, step in enumerate(raw or [], 1):
        if not isinstance(step, dict):
            raise SkillRefused(f"route step {i} is not a step")
        try:
            out.append({k: fill_value(v, values) for k, v in step.items()})
        except KeyError as exc:
            raise SkillRefused(f"route step {i} needs {exc.args[0]}, which has no value") from exc
    return out


def run_route(hands: Any, steps: list[dict[str, Any]], *, words: list[str], apps: list[str], key: str,
              sends: dict[str, Any] | None = None, takeover: Any = None) -> tuple[bool, str, dict[str, Any]]:
    """Rung B: a recorded path's steps (slots already filled by code, :func:`route_steps`) run through the hands'
    backend by :class:`SkillRunner`, each step checked, stopping at the first deviation. ``sends``: ``{"to", "what"}``
    when the steps send what they type (rule 8: both must be the user's own words, :func:`check_sends`). The A/B
    switch is not consulted (the route is the skill's, not a trace demo). ``(ok, report, facts)``; raises
    :class:`SkillRefused` before any step runs when the route can't run as written."""
    # phase L: a learned path carries each step's recorded path (``rec``, from :func:`recorded_path`); the replay
    # guard holds the steps to it
    recs = [s.get("rec") if isinstance(s.get("rec"), dict) else {"known": False} for s in steps]
    clean = [{k: v for k, v in s.items() if k != "rec"} for s in steps]
    item: dict[str, Any] = {"run": [dict(s) for s in clean], "apps": list(apps), "ref": f"skill:{key}",
                            "intent": key, "text": ""}
    if any(r.get("known") for r in recs):
        item["path"] = {"steps": recs, "dialog_apps": [], "recorded": True}
    args: dict[str, Any] = {"steps": [dict(s) for s in clean], "sends": bool(sends)}
    if sends:
        item["sends"] = True
        args.update(to=str(sends.get("to") or ""), what=str(sends.get("what") or ""))
    # every rung B the engine runs is a learned path (capabilities ship rung A only): each step is held for the
    # consent check (design section 4.4: "every click and key is a held step"; critique H4)
    return SkillRunner(hands, takeover=takeover, held_all=True).run(args, item=item, words=list(words))


# =====================================================================================================================
# The user's own ways (kWays, contract C4): capability ``app.way``, rung B, a learned template replayed by identity
# =====================================================================================================================


def _name_parts(name: Any) -> list[str]:
    """A composite name's parts (UI Automation joins a row's texts with ", "): the learner's own split."""
    from yuki.skills.learn.ways import _parts

    return _parts(_words(name))


def choose_way(ways: list[dict[str, Any]], given: Any) -> dict[str, Any] | None:
    """The way a run means: the one ``given`` names (its key, or its name as the menu shows it: the caller's own
    pick from the menu's way lines); with nothing given, the only live one. ``None`` when that leaves none or several
    (the route asks "Which of your ways?" with their names, so the caller names one), or when a way was given that is
    none of these (review L1: never another way in its place). kw3 (C6, 20 Oct: "Add 'ask Claude about the build' to my
    to-do list" ran the Claude way): an app or a way's name found in the user's words never picks a way."""
    if isinstance(given, Mapping):
        given = given.get("key") or given.get("name")
    key = _words(given)
    if key:
        hits = [w for w in ways if w.get("key") == key or (w.get("name") and _folded(w["name"]) == _folded(key))]
        return hits[0] if hits else None
    return ways[0] if len(ways) == 1 else None


def way_steps_for(way: Mapping[str, Any], *, item: str, text: str, send: bool
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """The run of one learned way for these values: ``(the way's steps as the step runner keeps them, the steps as
    given, content-free facts)``. The ``item`` slot (the brain's reading of the user's words) decides the place, and
    nothing else does (review M1: never a label found somewhere in the utterance or the text): a control's own label
    (whole, case aside) runs that control; else a group label of theirs it holds picks that group's row (``part``);
    else its words pick the one row whose own text holds them (``span``); empty: where the app is now. The field is
    found by its identity (role, container digest, name digests: never its name); the typing is the user's words; the
    end key only when the run sends. Facts: ``item`` (``thing`` / ``span`` / ""), ``thing`` (the label, RAM only),
    ``control`` (its label, RAM only), ``controls`` (the way's control labels), ``field``, ``typed``, ``end``.
    Raises :class:`SkillRefused` when the values don't fit the way (a place it has no list for, nothing to run)."""
    spec = way.get("spec") if isinstance(way.get("spec"), Mapping) else {}
    app = str(spec.get("app") or way.get("app") or "")
    run: list[dict[str, Any]] = []
    given: list[dict[str, Any]] = []
    item_spec = spec.get("item") if isinstance(spec.get("item"), Mapping) else {}
    things = [_words(t.get("name")) for t in item_spec.get("things") or [] if isinstance(t, Mapping)]
    things = [t for t in things if t]
    controls = [s for s in spec.get("steps") or [] if isinstance(s, Mapping) and s.get("do") == "control"
                and _words(s.get("name"))]
    item_words = _words(item)
    control = next((c for c in controls if item_words and _folded(c.get("name")) == _folded(item_words)), None)
    thing = "" if control is not None else next((t for t in things if said(t, [item_words])), "")
    if item_words and control is None and not item_spec:
        raise SkillRefused("that way has no list to pick a place from")
    facts: dict[str, Any] = {"item": "", "thing": thing, "control": _words((control or {}).get("name")),
                             "controls": [_words(c.get("name")) for c in controls], "field": False, "typed": False,
                             "end": False}
    for s in spec.get("steps") or []:
        if not isinstance(s, Mapping):
            continue
        do = s.get("do")
        if do == "item" and item_words and control is None and item_spec:
            run.append({"do": "pick", "role": str(item_spec.get("role") or ""),
                        "in": {"role": str(item_spec.get("parent") or "")}, "app": app,
                        "match": "part" if thing else "span", "labels": things})
            given.append({"do": "pick", "text": thing or item_words})
            facts["item"] = "thing" if thing else "span"
        elif do == "control" and control is not None and s is control:
            run.append({"do": "click", "role": _words(s.get("role")), "name": _words(s.get("name")),
                        "match": "control", **({"aid": _words(s.get("aid"))} if _words(s.get("aid")) else {})})
            given.append({"do": "click", "role": _words(s.get("role")), "name": _words(s.get("name"))})
        elif do == "field":
            fields = [f for f in spec.get("field") or [] if isinstance(f, Mapping) and f.get("name_digest")]
            if not fields:
                continue
            box = str(fields[0].get("box") or "")
            same = [f for f in fields if str(f.get("box") or "") == box and f.get("role") == fields[0].get("role")]
            run.append({"do": "pick", "role": str(fields[0].get("role") or ""),
                        "in": {"role": str(fields[0].get("parent") or ""), **({"digest": box} if box else {})},
                        "item": "|".join(str(f["name_digest"]) for f in same), "app": app, "match": "field"})
            given.append({"do": "pick"})
            facts["field"] = True
        elif do == "text" and _words(text):
            run.append({"do": "type"})
            given.append({"do": "type", "text": str(text)})
            facts["typed"] = True
    keys = [str(k) for k in ((spec.get("end") or {}).get("keys") or []) if str(k).strip()]
    if send and facts["typed"] and keys:
        run.append({"do": "keys", "keys": keys})
        given.append({"do": "keys", "keys": keys})
        facts["end"] = True
    if not run:
        raise SkillRefused("nothing of that way can run with these values")
    return run, given, facts


def _way_store(memory: Any, fn: Any, what: str = "ways") -> Any:
    """``fn(store)`` through the run's memory (a ``MemoryAccess`` or a client), ``None`` when it can't answer."""
    if memory is None:
        return None
    try:
        call = getattr(memory, "call", None)
        if callable(call) and not hasattr(memory, "store"):
            return call(what, lambda c: fn(getattr(c, "store", c)), timeout_s=1.5)
        return fn(getattr(memory, "store", memory))
    except Exception:  # noqa: BLE001
        return None


#: The capability the user's own ways run through (``connections/ways.toml``; contract C4).
CAPABILITY_WAY = "app.way"
#: The done line's words per outcome (data). ``{where}``: the row the way picked by its own text (RAM only, never
#: logged) and the app, or the app alone; never what was typed.
WAY_SAY = {"sent": "Done: sent in {where}.", "draft": "Typed it in {where}; it's waiting there, not sent."}
#: kWays review M2: a send that names only one of the user's groups (a project) never goes into a row Yuki chose
#: unasked: it asks first, naming that group's latest row by its own text (or a new one, when the way has a control);
#: the very same run asked for again within this time is the user's answer to that question ("that one"). RAM only.
WAY_ASK_KEEP_S = 180.0
_way_asks: dict[tuple[str, str, str], float] = {}
_way_asks_lock = threading.Lock()


def _way_asked(key: tuple[str, str, str]) -> bool:
    """Whether this very run (way, group, text) was asked about moments ago (the ask is used up); else it is noted
    as asked now."""
    now = time.monotonic()
    with _way_asks_lock:
        for k, at in list(_way_asks.items()):
            if now - at > WAY_ASK_KEEP_S:
                _way_asks.pop(k, None)
        if _way_asks.pop(key, None) is not None:
            return True
        _way_asks[key] = now
        return False


def _peek_row(hands: Any, step: dict[str, Any], label: str, app: str) -> str:
    """The own text (first part) of the row a ``part`` pick of ``label`` would take now: one read of the app's
    windows, nothing clicked or brought forward; ``""`` when it isn't seen."""
    try:
        how, found, _n = SkillRunner(hands)._pick(way_step(0, step, SkillStep(i=0, do="pick", text=label)), [app],
                                                  0.0, None)
    except Exception:  # noqa: BLE001 - not seen: the question names no row
        return ""
    parts = _name_parts(found[2].get("name")) if how == "found" and found is not None else []
    return parts[0] if parts else ""


def route_way(call: Any) -> Any:
    """The ``app.way`` capability's rung B (contract C4): one of the user's own ways replayed by
    :class:`SkillRunner` with every step held for Yuki's own check (``held_all``, as :func:`run_route`), on the voice
    hands or the brain's desk hands (their declared-steps scope, as the chat driver's own steps: the field's pick,
    a group row's pick and a control the item slot named are the capability's; a row found by the user's words is
    seen by Yuki's one-step check; the end key is declared only under the brain's grant, else that check judges it).
    A send naming only a group asks first which row (:data:`WAY_ASK_KEEP_S`). Never with a game in front (the route
    takes focus). The done line names the row it picked; ``facts_ram`` carries what the done-check reads (typed,
    submitted)."""
    from yuki.skills.engine import TO_PLANNER, RouteResult
    from yuki.skills.learn import ways as learned

    ctx = call.ctx
    values = call.values if isinstance(call.values, Mapping) else {}
    words = [str(w) for w in ctx.words or [] if str(w).strip()]
    live = _way_store(ctx.memory, learned.live_ways) or []
    way = choose_way(list(live), values.get("way"))
    if way is None:
        return RouteResult(ran=False, outcome="no_way" if not live else "which_way",
                           ask=None if not live else "Which of your ways?",
                           options=[str(w.get("name") or "") for w in live][:4] if live else [])
    hands = ctx.hands
    if hands is None:
        return RouteResult(ran=False, outcome="no_hands")
    send = str(values.get("send") or "") == "send"
    text = str(values.get("text") or "")
    try:
        run, given, plan = way_steps_for(way, item=str(values.get("item") or ""), text=text, send=send)
    except SkillRefused:
        return RouteResult(ran=False, outcome="refused_steps")
    spec = way.get("spec") if isinstance(way.get("spec"), Mapping) else {}
    app = str(spec.get("app") or way.get("app") or "")
    noun = _words(spec.get("item_noun")) or "item"
    if send and plan["thing"] and not _way_asked((str(way["key"]), _folded(plan["thing"]), _folded(text))):
        pick = next((s for s in run if s.get("match") == "part"), None)
        row = _peek_row(hands, pick, plan["thing"], app) if pick is not None else ""
        new = plan["controls"][0] if plan["controls"] else ""
        where = f'your latest {plan["thing"]} {noun}, "{row[:60]}"' if row else f'which {plan["thing"]} {noun}'
        ask = f"Send it in {where}" + (f", or in a new {noun}?" if new else "?")
        return RouteResult(ran=False, outcome="which_item", ask=ask, options=[o for o in (row[:60], new) if o],
                           facts={"asked": "which_item", "seen": bool(row)}, app=app)
    item = {"run": run, "apps": [app], "ref": f"skill:{way['key']}", "intent": str(way.get("name") or ""),
            "text": ""}
    args = {"steps": given, "sends": False}
    declare = getattr(hands, "driver_steps", None)
    # the end key is the capability's own send only under the brain's consent grant for this very step (a send its
    # rule allowed on the user's plain words and counted down, for exactly this text: kBrain sets it, a way reaches no
    # person so the grant names none); otherwise it is not declared and Yuki's one-step check judges it (on the brain
    # it is handed to the planner with the text typed, a draft)
    grant = getattr(hands, "grant", None)
    granted = bool(send and isinstance(grant, Mapping) and grant.get("send") is True and grant.get("counted") is True
                   and _folded(grant.get("text")) == _folded(text)
                   and _words(grant.get("skill")) in (CAPABILITY_WAY, str(way["key"])))
    own = given if granted else [s for s in given if s.get("do") != "keys"]
    sends = {"to": _words(grant.get("person")) if granted else "", "what": text} if granted else None
    scope = declare(key=str(way["key"]), steps=own, way_text="", sends=sends) if callable(declare) else nullcontext()
    runner = SkillRunner(hands, held_all=True)
    try:
        with scope:
            ok, _report, facts = runner.run(args, item=item, words=words)
    except SkillRefused:
        return RouteResult(ran=False, outcome="refused_steps")
    results = [r for r in facts.get("results") or [] if isinstance(r, StepResult)]
    typed = any(r.step.do == "type" and r.ok and r.verdict is True for r in results)
    # review L2: sent only when the end key's effect was seen (an undecided screen change is not a send)
    submitted = any(r.step.do == "keys" and r.ok and r.verdict is True for r in results) if send else None
    done = int(facts.get("steps_done") or 0)
    ram = {"way": way["key"], "typed": typed if plan["typed"] else None, "submitted": submitted, "send": send,
           "picked": plan["item"]}
    shown = {"steps": int(facts.get("steps") or 0), "done": done, "pick": plan["item"] or "none",
             "control": bool(plan["control"]), "send": send}
    kind = facts.get("refused") if facts.get("refused") in ("game", "blocked") else None   # fHands: by its kind
    if kind or facts.get("act_off"):
        return RouteResult(ran=done > 0, held=kind, outcome="refused", facts=shown, facts_ram=ram, app=app)
    if facts.get("to_planner"):
        return RouteResult(ran=done > 0, outcome=TO_PLANNER, facts=shown, facts_ram=ram, app=app)
    if facts.get("held"):
        return RouteResult(ran=done > 0, ask=str(facts.get("question") or ""), outcome="held_by_check", facts=shown,
                           facts_ram=ram, app=app)
    if facts.get("took_over_at"):
        return RouteResult(ran=done > 0, outcome="taken_over", facts=shown, facts_ram=ram, app=app)
    verified = True if (ok and facts.get("verified") is True) else (False if facts.get("deviation") else None)
    if verified:
        _way_store(ctx.memory, lambda store: learned.note_use(store, str(way["key"])), "way_use")
    row = (_name_parts(runner.picked[0]) or [""])[0] if runner.picked else ""
    where = f'your {noun} "{row[:60]}" in {app_words(app)}' if row else app_words(app)
    say = WAY_SAY["sent" if send and submitted else "draft"].format(where=where) if ok else None
    return RouteResult(ran=done > 0 if done or not ok else None, verified=verified, say=say,
                       outcome="steps_ok" if ok else str(facts.get("deviation") or "failed"), facts=shown,
                       facts_ram=ram, app=app)


def probe_way_done(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """``app.way``'s done-check, from the route's own measured steps (``facts_ram``): a send is done when the end key
    was pressed after the typing was read back; a draft when the typing was read back in the field. No route result
    (a hand-off) or a re-read: ``None`` (the planner looks at the screen)."""
    result = getattr(ctx, "result", None)
    ram = dict(getattr(result, "facts_ram", None) or {})
    if not ram.get("way"):
        return None, {"status": "no_result"}
    if ram.get("typed") is False:
        return False, {"status": "not_typed"}
    if ram.get("send"):
        if ram.get("submitted") is True and ram.get("typed") is True:
            return True, {"status": "submitted"}
        return (False, {"status": "not_submitted"}) if ram.get("submitted") is False else (None, {"status": "unsure"})
    if ram.get("typed") is True:
        return True, {"status": "typed"}
    return None, {"status": "unsure"}


#: The driver tables the engine reads (``via = "driver"``, ``probe = "driver"``: ``connections/ways.toml``).
ROUTES: dict[str, Any] = {"way": route_way}
PROBES: dict[str, Any] = {"way_done": probe_way_done}


# =====================================================================================================================
# The desk hands: a capability's own driver steps on the new brain (zFinal; bSkill finding 1)
# =====================================================================================================================


def _element_words(element: Any) -> tuple[str, str]:
    """``(role, name)`` from the element words Yuki's own code builds for a check (``role "name"``, a pick's ``role
    "name" in the list``), else ``("", "")``. Plumbing of Yuki's own format, never a reading of meaning (the voice
    hands' ``_element_parts``)."""
    text = _words(element)
    head, sep, rest = text.partition(' "')
    if not sep or '"' not in rest or " " in head.strip():
        return "", ""
    role = head.strip()
    return ("" if role.casefold() in ("control", "field", "item") else role), rest[:rest.rindex('"')].strip()


#: :meth:`DeskHands._own`'s answer for a run's send key the brain's consent rule did not allow and count down.
NOT_GRANTED = "not_granted"


def _folded(text: Any) -> str:
    """A string compared with a string: whitespace and case folded (identity, never meaning)."""
    return _words(text).casefold()


def grant_person(store: Any, person: Any, *others: Any) -> int | None:
    """The id of the one person a send grant goes to (the executor's ``Executor._skill_grant``): the person the chat
    driver's own lookup reads ``person`` as (the capability's person slot; :func:`yuki.skills.chat.whom`, the same
    :func:`~yuki.skills.chat.find_target` a run makes, review r2 M2): a name or alias as written, a heard form memory
    learned ("hey meant"), a transliteration ("हेमंत"), never by sound. Each of ``others`` (the plan's recipient, as
    the countdown showed it) that names anyone must name that same person. ``None`` when ``person`` names nobody, two
    people, a group chat or channel by its whole name ("Rohan's bday"), or both a person and a place, or when another
    name reads as anyone or anything else. Never raises."""
    try:
        from yuki.skills.chat import whom

        kind, first = whom(store, _words(person))
        if kind != "person" or first is None:
            return None
        for other in others:
            if not _words(other):
                continue
            seen, pid = whom(store, _words(other))
            if seen == "unknown":
                continue
            if seen != "person" or pid != first:
                return None
        return int(first)
    except Exception:  # noqa: BLE001 - memory can't say: no person id, so the grant covers no worded send
        return None


def send_grant(grant: Any, *, hands: Any, skill: str = "", request_id: Any = None) -> tuple[dict[str, Any] | None, str]:
    """``(grant, "ok")`` when ``grant`` (the Dispatcher's ``skill_context["grant"]``) is the brain's consent grant for
    the ``run_skill`` running now, else ``(None, why)`` with a content-free code word (failure fix-plan Item 11's L2).
    It must be a send the consent rule allowed (row 4) after a countdown the user could stop actually ran or after their
    yes (``counted``, review r1 L3: a host with no countdown never sends Yuki's wording unasked), held right now by
    these very desk hands (:class:`DeskHands`, never the voice's), equal to theirs, one-step token and all (the executor
    sets it just before the step and clears it after, so a grant left from an earlier step never matches), on this
    thread, for this request and this skill, with its text and the one person id it goes to. Anything else (the voice,
    the old loop, a card, a draft's grant) is no grant, and the run checks the user's words as before."""
    if not isinstance(grant, Mapping) or not grant:
        return None, "none"
    if grant.get("send") is not True:
        return None, "not_send"
    if grant.get("counted") is not True:
        return None, "no_countdown"
    held = hands.grant if isinstance(hands, DeskHands) else None
    if not isinstance(held, Mapping) or not str(grant.get("token") or "") or dict(held) != dict(grant):
        return None, "stale"
    if grant.get("thread") != threading.get_ident():
        return None, "other_thread"
    if grant.get("request_id") and request_id and str(grant.get("request_id")) != str(request_id):
        return None, "other_request"
    if skill and _words(grant.get("skill")) != _words(skill):
        return None, "other_skill"
    if not _folded(grant.get("text")) or not _words(grant.get("text_slot")):
        return None, "no_text"
    if _person_id(grant.get("person_id")) is None or not _words(grant.get("person_slot")):
        return None, "no_person"
    return dict(grant), "ok"


class DeskHands:
    """The hands the engine's chat driver (``yuki.skills.chat``: the message and voice-join capabilities) runs its own
    steps through when the new brain runs a ``run_skill`` (``Dispatcher._do_run_skill``; bSkill finding 1). Without
    them the message capability can't type or press send on the brain path (``no_hands``), so every typed or spoken
    message would end as a draft or "couldn't confirm".

    It is the voice hands' surface that :class:`SkillRunner` and the chat driver read (``_dispatcher`` / ``backend``
    for :func:`skill_dispatcher`, ``memory``, ``_focus``, ``_now_board``, ``_read_tree``, ``_front``, ``act_off``,
    ``authorize``), over the request's own desktop, with the same consent and commit checks:

    * **Rows 1-2 on every step** (``consent.screen_gate``): a game in front, the app's Act switch off, a kind on
      "just suggest": refused, nothing done.
    * **The capability's own steps pass** (the steps its driver built from its data, declared for one run through
      :meth:`driver_steps` by ``chat.run_steps``): its data-built keys (the quick switcher, its open key), the chat's
      name memory keeps (the route's own words), the pick of the chat's row by its keyed digest, and the text of this
      ``run_skill`` step (typing without Enter: the user's words the consent rule let through, or a draft's).
    * **The send key only after the countdown**: in a run that declares a send, its key passes only when the brain's
      consent rule allowed this very ``run_skill`` as a send (row 4: the plan declared ``consent.commit = send``, the
      quote, the recipient and the text are the user's own words, the commit check said they asked for it, no
      near-duplicate; or the user said yes to its question) and its countdown ran (:attr:`grant`, set by the executor
      for that one step), and only for the same text and the same person (by the person id the grant and the driver's
      target both carry, in that person's own direct message; else the same name). ``SkillRunner`` checks again that
      both whom and what are spans of the user's words, or that the grant covers the send whole (:func:`granted_send`:
      Item 11's L2, Yuki's own wording of what they asked to say), and the driver presses nothing when the chat's
      composer holds other text or can't be read.
    * **A join's button only on plain words**: the capability's declared join control (catalog data) passes only when
      the consent rule allowed this ``run_skill`` as a join on the user's plain words (row 3, decision 7).
    * **Anything else** goes to ``yuki.brain.check.one`` (rows 1-6 with a one-step commit check, the request's own
      checker), as rung B's steps do on the voice: a step that would send or join is handed over to the planner.

    A learned recorded path (rung B) is never run through these hands (:attr:`rung_b`): on the brain it stays the
    planner's, with the skill as the plan seed. Logs are content-free (tool, row, source, allowed). One per request;
    every call on the request's own thread.
    """

    #: The engine's rung B (a learned recorded path, ``v_uia_steps``) does not run on these hands.
    rung_b = False

    def __init__(self, dispatcher: Any, *, memory: Any = None, words: Any = (), earlier: Any = (), checker: Any = None,
                 log: Any = None) -> None:
        self._dispatcher = dispatcher
        self.backend = getattr(dispatcher, "backend", None)
        self.memory = memory
        #: The user's own words for this request (typed, or the heard turns), never the planner's wording.
        self.words: list[str] = [str(w) for w in words or () if str(w).strip()]
        self.earlier: list[str] = [str(w) for w in earlier or () if str(w).strip()]
        self.checker = checker
        self._log = log
        #: The consent rule's verdict for the ``run_skill`` step running now (the executor sets it for that one step and
        #: clears it after): ``{"skill", "text", "person", "send": bool, "join": bool}``, and for a send also
        #: ``person_id``, ``text_slot`` / ``person_slot``, ``request_id``, ``step``, ``thread`` and a one-step
        #: ``token`` (:func:`send_grant` holds a skill run's grant to it); empty: nothing was granted.
        self.grant: dict[str, Any] = {}
        self._scopes: list[dict[str, Any]] = []
        #: Windows this run typed Yuki's text into with no send yet (hwnd -> monotonic): a later key or click there is
        #: judged as one that may send it.
        self._typed: dict[int, float] = {}

    # -- what the step runner and the chat driver read ---------------------------------------------------------------

    def _focus(self, timeout_s: float = 0.25) -> Any:
        """What has the keyboard (the backend's ``focus_snapshot``), or ``None`` when it can't say."""
        read = getattr(self.backend, "focus_snapshot", None)
        if not callable(read):
            return None
        try:
            snap = read(timeout_s=timeout_s)
        except TypeError:
            snap = read()
        except Exception:  # noqa: BLE001
            return None
        return snap if getattr(snap, "ok", False) else None

    def _now_board(self) -> dict[str, Any] | None:
        """Memory's Now picture of the real screen (a stand-in desk's own ``now_board``; else none)."""
        own = getattr(self.backend, "now_board", None)
        try:
            if callable(own):
                board = own()
                return board if isinstance(board, dict) else None
            from yuki.agent.tools import _LazyRealBackend

            if not isinstance(self.backend, _LazyRealBackend):
                return None
            from yuki.memory.now import shared_client

            client = shared_client()
            board = client.now(wait_s=0.05) if client is not None else None
            return board if isinstance(board, dict) else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def skill_dispatcher(self) -> Any:
        """The dispatcher the capability's steps run through (made once, read by :func:`skill_dispatcher`): report 58's
        before/after account on, over the request's own backend, with the request dispatcher's quieter-hands gate (it
        waits for the user to stop typing, and a stop ends the wait) and its notices. Never the request's dispatcher
        itself: that one is in the middle of the ``run_skill`` call."""
        made = getattr(self, "_skill_disp", None)
        if made is not None:
            return made or None
        self._skill_disp = False
        try:
            from yuki.agent.tools import Dispatcher

            if self.backend is None or not isinstance(self._dispatcher, Dispatcher):
                return None
            made = Dispatcher(self.backend, max_tree_elements=SKILL_TREE_ELEMENTS,
                              tree_timeout_s=SKILL_TREE_TIMEOUT_S, verify_outcomes=True)
            for name in ("input_gate", "cancel_check", "notices"):
                value = getattr(self._dispatcher, name, None)
                if value is not None:
                    setattr(made, name, value)
            self._skill_disp = made
        except Exception:  # noqa: BLE001 - no steps run through the request's own dispatcher mid-call
            return None
        return made

    def _disp(self) -> Any:
        disp = self.skill_dispatcher
        if disp is None:
            raise RuntimeError("the desk hands have no dispatcher of their own")
        return disp

    def _read_tree(self, hwnd: int) -> tuple[dict[str, Any], int, float] | None:
        """One read of ``hwnd`` through the steps' own dispatcher (never the request's, which is mid-call)."""
        try:
            disp = self._disp()
            outcome = disp.dispatch("look_at_window", {"hwnd": int(hwnd)})
        except Exception:  # noqa: BLE001
            return None
        if not getattr(outcome, "ok", False) or not isinstance(getattr(outcome, "payload", None), dict):
            return None
        latest = getattr(disp, "_latest_trees", {}).get(int(hwnd))
        return outcome.payload, (int(latest[0]) if latest else 0), float(getattr(outcome, "elapsed_ms", 0) or 0.0)

    def _front(self, hwnd: int) -> str:
        """Bring ``hwnd`` forward unless it is in front (through the steps' dispatcher: its game guard and the desk
        lease); raises when it can't."""
        disp = self._disp()
        try:
            _text, payload, _ms = disp.overview_text()
            if int(payload.get("foreground_hwnd") or 0) == int(hwnd):
                return ""
        except Exception:  # noqa: BLE001 - then it is brought forward
            pass
        outcome = disp.dispatch("focus_window", {"hwnd": int(hwnd)})
        if not getattr(outcome, "ok", False):
            raise RuntimeError(f"could not bring window {hwnd} to the front: {getattr(outcome, 'summary', '')}")
        return "brought it to the front; "

    def snapshot(self) -> Any:
        raise RuntimeError("not on the brain's desk hands")

    def _act(self, name: str, args: dict[str, Any], facts: dict[str, Any]) -> Any:
        raise RuntimeError("not on the brain's desk hands: the planner does it")

    def _open_url(self, args: dict[str, Any], facts: dict[str, Any], *, heard: bool = True) -> Any:
        raise RuntimeError("not on the brain's desk hands: the planner does it")

    def act_off(self, app: str | None) -> str | None:
        """The name Settings switched "Act" off for when ``app`` is that app (the brain's policy reader), else
        ``None``. An unreadable policy never blocks here (rows 1-2 read it again on every step)."""
        if not app:
            return None
        try:
            from yuki.brain import util

            policy = getattr(self._dispatcher, "act_policy", None) or util._shared_reader()
            return policy.app_off(str(app)) or None
        except Exception:  # noqa: BLE001
            return None

    # -- one run's declared steps ------------------------------------------------------------------------------------

    def driver_steps(self, *, key: str, steps: list[dict[str, Any]], way_text: str = "",
                     sends: Mapping[str, Any] | None = None) -> Any:
        """A context for one ``chat.run_steps`` run: the steps its driver built from the capability's data, the
        route's own words, and what it sends (``{"to", "what"}``) when it sends."""
        from contextlib import contextmanager

        @contextmanager
        def scope() -> Any:
            self._scopes.append({
                "key": str(key or ""),
                "keys": [[_folded(k) for k in s.get("keys") or [] if _words(k)] for s in steps
                         if isinstance(s, dict) and s.get("do") == "keys"],
                "texts": [_folded(s.get("text")) for s in steps if isinstance(s, dict) and s.get("do") == "type"],
                "picks": sum(1 for s in steps if isinstance(s, dict) and s.get("do") == "pick"),
                "way_text": _folded(way_text),
                "sends": dict(sends) if sends else None})
            try:
                yield self
            finally:
                self._scopes.pop()

        return scope()

    def _own(self, tool: str, tool_input: Mapping[str, Any], facts: Mapping[str, Any]) -> str | None:
        """Which of the capability's own steps this is (``key``, ``name``, ``text``, ``pick``, ``send``, ``join``), or
        ``None``. Strings compared with strings and catalog data only."""
        scope = self._scopes[-1] if self._scopes else None
        grant = self.grant or {}
        if scope is not None:
            if tool == "hotkey":
                keys = [_folded(k) for k in tool_input.get("keys") or [] if _words(k)]
                if keys and keys in scope["keys"]:
                    if not scope["sends"]:
                        return "key"
                    sends = scope["sends"]
                    # the same text, and the same person: when both have one, the person id memory's people index
                    # gave the grant and the driver's target, in that person's own direct message (Item 11's L2:
                    # "हेमंत" or "hey meant" in the user's words is Hemanth); else (a group chat or channel, or memory
                    # couldn't say) the grant's own name, as before
                    pid, gid = _dm_person(sends), _person_id(grant.get("person_id"))
                    same = pid == gid if pid is not None and gid is not None else \
                        _folded(sends.get("to")) == _folded(grant.get("person"))
                    if grant.get("send") is True and _folded(sends.get("what")) \
                            and _folded(sends.get("what")) == _folded(grant.get("text")) and same:
                        return "send"
                    return NOT_GRANTED   # a send key the consent rule didn't allow and count down: never pressed
            elif tool == "type_text":
                text = _folded(tool_input.get("text"))
                if text and text in scope["texts"]:
                    if text == scope["way_text"]:
                        return "name"
                    if text == _folded(grant.get("text")):
                        return "text"
            elif tool == "click" and scope["picks"]:
                # kWays: a learned way's own steps (its field found by its keyed digests, a row of the user's group
                # found by their label, the control its item slot named by its label) are the capability's even in a
                # run that sends (its send is the end key, judged on its own); a row found by the user's words never
                # is (review H1: Yuki's one-step check sees the element)
                way = facts.get("way_step")
                if way in ("field", "part", "control") or (not scope["sends"] and way != "span"):
                    return "pick"
            return None
        if tool == "click" and grant.get("join") is True:
            from yuki.brain import consent

            role, name = str(facts.get("role") or ""), _words(facts.get("name"))
            if (role.casefold(), name.casefold()) in consent.join_controls():
                return "join"
        return None

    # -- the check -----------------------------------------------------------------------------------------------------

    def authorize(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any] | None = None,
                  earlier: list[str] | None = None, *, force: bool = False) -> Any:
        """The verdict for one step (class docstring): rows 1-2 first, then the capability's own step, else
        ``yuki.brain.check.one``. Always answers (``allowed`` true for a step it lets through); fails closed."""
        from yuki.brain import consent

        facts = dict(target or {})
        if not facts.get("role") and not facts.get("name"):
            role, name = _element_words(facts.get("element"))
            if role or name:
                facts.update(role=role, name=name)
        if tool == "click" and force and not facts.get("role"):
            facts["role"] = "Button"   # the caller's own signal: a role that can send, delete or buy
        raw = facts.get("hwnd")
        hwnd = int(raw) if raw is not None and str(raw).lstrip("-").isdigit() else None
        facts["hwnd"] = hwnd
        now = time.monotonic()
        self._typed = {h: at for h, at in self._typed.items() if now - at < 600.0}
        if hwnd is not None and hwnd in self._typed:
            facts["typed_pending"] = True
        try:
            from yuki.brain.check import _board

            call = consent.Call(tool=str(tool), input=dict(tool_input or {}), index=0, hwnd=hwnd,
                                app=str(facts.get("app") or ""), title=str(facts.get("title") or ""),
                                role=str(facts.get("role") or ""), name=_words(facts.get("name")))
            board = _board()
            live = consent.Live(game=consent.game_now(board), calling=consent.calling_windows(board),
                                typed_pending=frozenset({hwnd}) if hwnd is not None and facts.get("typed_pending")
                                else frozenset(), join_controls=consent.join_controls())
            gate = consent.screen_gate(call, live)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return self._said(tool, SimpleNamespace(allowed=False, decision="ask_user", question="", source="error",
                                                    handover=False, act_off="", row=0,
                                                    why=f"the check could not run ({type(exc).__name__})"))
        if gate is not None:
            off = self.act_off(call.app) if gate.why == "act_off" else None
            return self._said(tool, SimpleNamespace(allowed=False, decision="refuse", question="",
                                                    source=f"row{gate.row}", handover=False, act_off=off or "",
                                                    row=gate.row, why=gate.line or gate.why))
        own = self._own(str(tool), dict(tool_input or {}), facts)
        if own == NOT_GRANTED:
            # never pressed: the text stays in the chat as a draft (the driver says so); the planner may take it on
            return self._said(tool, SimpleNamespace(allowed=False, decision="refuse", question="", source="row4",
                                                    handover=True, act_off="", row=4, why="send_not_granted"))
        if own is not None:
            if tool == "type_text" and hwnd is not None:
                self._typed[hwnd] = now   # Yuki's words wait there, unsent
            if own == "send" and hwnd is not None:
                self._typed.pop(hwnd, None)
            return self._said(tool, SimpleNamespace(allowed=True, decision="allow", question="", source="capability",
                                                    handover=False, act_off="", row=0, why=own))
        try:
            from yuki.brain.check import one

            before = [*self.earlier[-2:], *[_words(e) for e in earlier or [] if _words(e)]][-3:]
            verdict = one(str(tool), dict(tool_input or {}), facts, words=list(self.words), checker=self.checker,
                          earlier=before, log=self._log)
        except Exception as exc:  # noqa: BLE001 - fail closed
            verdict = SimpleNamespace(allowed=False, decision="ask_user", question="", source="error", handover=False,
                                      act_off="", row=0, why=f"the check could not run ({type(exc).__name__})")
        if tool == "type_text" and hwnd is not None and getattr(verdict, "allowed", False):
            self._typed[hwnd] = now
        return self._said(tool, verdict)

    def _said(self, tool: str, verdict: Any) -> Any:
        """The verdict, logged content-free."""
        if self._log is not None:
            try:
                self._log("desk_hands", tool=str(tool), allowed=bool(getattr(verdict, "allowed", False)),
                          row=getattr(verdict, "row", None), source=str(getattr(verdict, "source", "") or ""),
                          handover=bool(getattr(verdict, "handover", False)))
            except Exception:  # noqa: BLE001
                pass
        return verdict


__all__ = ["RUN_SKILL_TOOL", "SkillRunner", "SkillRefused", "SkillStep", "parse_steps", "check_sends", "check_texts",
           "check_picks_for_send",
           "fit_to_way", "fill_slots", "way_step", "said", "trace_steps", "SKILL_DOS", "MAX_PARTS", "site",
           "PATTERNS", "Found", "Pressed", "app_window", "search", "find_in", "apply_pattern", "press", "route_steps",
           "run_route", "use_learned", "set_use_learned", "way_steps", "way_path", "recorded_path", "DeskHands",
           "granted_send", "grant_person", "send_grant", "GRANT_KIND", "DRAFT_KEEP_S", "note_draft", "draft_of",
           "ROUTES", "PROBES", "route_way", "probe_way_done", "choose_way", "way_steps_for"]
