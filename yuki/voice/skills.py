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
  matches. No unique visible match: the run stops and the thinker gets it.

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
"""

from __future__ import annotations

import time
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
        slot=way.get("do") == "pick" and not way.get("item"))


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


def check_texts(steps: list[SkillStep], words: list[str], way_text: str) -> str | None:
    """Why a step's text may not be typed (``None``: all may): only the user's own words or the way's own words."""
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


class SkillRunner:
    """Runs one learned way through a :class:`yuki.voice.hands.Hands`' backend, step by step (blocking: off the loop)."""

    def __init__(self, hands: Any) -> None:
        self.hands = hands
        #: This run's earlier steps as calls (what the authorization check reads beside the user's words).
        self._earlier: list[str] = []

    # -- the screen ---------------------------------------------------------------

    def _overview(self) -> dict[str, Any]:
        try:
            _text, payload, _ms = self.hands._dispatcher.overview_text()
            return payload if isinstance(payload, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _windows(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
        return {int(w["hwnd"]): w for w in payload.get("windows") or [] if isinstance(w, dict) and w.get("hwnd")}

    @staticmethod
    def _app_of(window: dict[str, Any] | None) -> str:
        return str((window or {}).get("process_name") or (window or {}).get("app") or "")

    def _in_way(self, app: str, apps: list[str]) -> bool:
        from yuki.memory.store import app_key

        key = app_key(app) if app else None
        return bool(key) and key in {app_key(a) for a in apps if a}

    def _search(self, step: SkillStep, apps: list[str], payload: dict[str, Any]) -> list[tuple[int, str, str]]:
        """``(hwnd, title, app)`` of the windows a click looks in: the one named, else the one in front, the popups
        memory's Now picture sees, then the way's own app's windows."""
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
        for hwnd, p in list(popups.items())[:2]:
            add(hwnd, str(p.get("title") or ""), str(p.get("app") or ""))
        for hwnd, w in windows.items():
            if len(out) >= 5:
                break
            if not w.get("is_minimized") and self._in_way(self._app_of(w), apps):
                add(hwnd, str(w.get("title") or ""), self._app_of(w))
        return out

    def _find(self, step: SkillStep, apps: list[str], wait_s: float) -> tuple[int, int, dict[str, Any], str, str] | None:
        """The step's control in a fresh read: ``(hwnd, read, element, title, app)``; waits up to ``wait_s`` for it."""
        role, name = step.role.casefold(), _words(step.name).casefold()
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            payload = self._overview()
            for hwnd, title, app in self._search(step, apps, payload):
                got = self.hands._read_tree(hwnd)
                if got is None:
                    continue
                tree, read, _ms = got
                if step.aid:
                    # capC4: a proven AutomationId (memory keeps it only where that app's are proven) finds the very
                    # control first; its role must agree
                    by_aid = [e for e in (tree.get("elements") or []) if isinstance(e, dict)
                              and isinstance(e.get("id"), int) and str(e.get("automation_id") or "") == step.aid
                              and (not role or str(e.get("role") or "").casefold() == role)]
                    if len(by_aid) == 1:
                        return hwnd, read, by_aid[0], title, app
                elements = [e for e in (tree.get("elements") or []) if isinstance(e, dict)
                            and isinstance(e.get("id"), int) and _words(e.get("name")).casefold() == name]
                hits = [e for e in elements if str(e.get("role") or "").casefold() == role] if role else []
                hits = hits or elements   # a role said in other words ("link" for Hyperlink): the name decides
                if len(hits) > 1:
                    hits = [e for e in hits if e.get("is_interactive", True)] or hits
                if len(hits) == 1:
                    return hwnd, read, hits[0], title, app
            if time.monotonic() >= deadline:
                return None
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

    def _pick(self, step: SkillStep, apps: list[str], wait_s: float
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
        while True:
            payload = self._overview()
            for hwnd, title, app in self._search(SkillStep(i=step.i, do="pick"), apps, payload):
                got = self.hands._read_tree(hwnd)
                if got is None:
                    continue
                tree, read, _ms = got
                els = [e for e in (tree.get("elements") or []) if isinstance(e, dict) and isinstance(e.get("id"), int)]
                by_id = {int(e["id"]): e for e in els}
                boxes = {int(e["id"]) for e in els if str(e.get("role") or "").casefold() == box_role
                         and (not box_name or _words(e.get("name")).casefold() == box_name)}
                if not boxes:
                    continue
                why = "no_match" if why == "no_container" else why

                def inside(e: dict[str, Any]) -> bool:
                    parent, depth = e.get("parent"), 0
                    while isinstance(parent, int) and parent >= 0 and depth < PICK_DEPTH:
                        if parent in boxes:
                            return True
                        parent = (by_id.get(parent) or {}).get("parent")
                        depth += 1
                    return False

                cands = [e for e in els if str(e.get("role") or "").casefold() == role and _words(e.get("name"))
                         and inside(e)]
                if step.slot:
                    want = _words(step.text).casefold()
                    hits = [e for e in cands if want and _words(e.get("name")).casefold() == want]
                else:
                    digests = self._digests(step.app or app, step.role, [_words(e.get("name")) for e in cands])
                    if digests is None:
                        return "no_digest", None, 0
                    hits = [e for e, d in zip(cands, digests) if step.item and d.casefold().startswith(
                        step.item.casefold())]
                if len(hits) == 1:
                    return "found", (hwnd, read, hits[0], title, app), 1
                if len(hits) > 1:
                    return "many", None, len(hits)
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

    def end_ok(self, end: dict[str, Any], last: StepResult | None, before: tuple[str, str]) -> tuple[bool, str]:
        """capC4: whether a part reached its checked end (report 78 §6.2): a window of the app in front (``window_
        verified``); a new page of the app in front (``page_reached``: the window in front is that app's and its title
        is not the one in front when the part began); otherwise the part's last step was verified by its own measured
        report. ``(ok, why)``."""
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
            got = self.hands._read_tree(front) if front else None
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

    def _step(self, step: SkillStep, *, apps: list[str], first: bool, after_open: bool,
              start_front: int, opened: set[int]) -> StepResult:
        from yuki.agent.loop import action_verdict

        started, at = time.monotonic(), time.time()

        def done(**kw: Any) -> StepResult:
            kw.setdefault("ms", round((time.monotonic() - started) * 1000, 1))
            kw.setdefault("at", at)
            return StepResult(step=step, started=started, **kw)

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
            found = self._find(step, apps, wait)
            target = f'{step.role or "control"} "{step.name}"'
            if found is None:
                return done(ok=False, verdict=False, kind="read", tool="find", target=target,
                            why="the control the way names is not on the screen",
                            report=f"no {target} on the screen (looked for {wait:g} s)", deviation="not_found")
            hwnd, read, element, title, app = found
            role_name = f'{element.get("role") or step.role or "control"} "{_words(element.get("name"))[:80]}"'
            from yuki.voice.hands import RISKY_CLICK_ROLES

            verdict = self._held("click", {"hwnd": hwnd, "element": role_name},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": role_name},
                                 force=str(element.get("role") or "") in RISKY_CLICK_ROLES)
            if verdict is not None:
                return held(verdict, target)
            try:
                lead = self.hands._front(hwnd)
            except Exception as exc:  # noqa: BLE001 - the hands' refusal: could not bring it up
                return done(ok=False, verdict=False, tool="click", target=target, app=app, hwnd=hwnd,
                            why="its window could not be brought to the front", report=str(exc)[:200],
                            deviation="window")
            outcome = self.hands._dispatcher.dispatch("click", {"element": element["id"], "read": read,
                                                                "expect_hwnd": hwnd})
            verdict, why = action_verdict("click", outcome)
            role = str(element.get("role") or step.role or "control")
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="click",
                        target=f'{role} "{_words(element.get("name"))[:80]}" in "{title[:60]}"', app=app, hwnd=hwnd,
                        report=f"{lead}clicked {role} \"{_words(element.get('name'))[:80]}\": {outcome.summary}")
        if step.do == "pick":
            wait = AFTER_OPEN_WAIT_S if after_open else (FIRST_FIND_WAIT_S if first else FIND_WAIT_S)
            how, found, n = self._pick(step, apps, wait)
            target = f"an item of the {step.box_role or 'list'}"
            if found is None:
                why = {"no_container": f"the {step.box_role or 'list'} the way picks from is not on the screen",
                       "many": f"{n} items match, so which one is not clear",
                       "no_digest": "memory could not tell which item it is"}.get(
                    how, "the item the way picks is not visible in it")
                return done(ok=False, verdict=False, kind="read", tool="find", target=target, why=why,
                            report=f"{why} (looked for {wait:g} s)", deviation="not_found")
            hwnd, read, element, title, app = found
            role = str(element.get("role") or step.role or "item")
            verdict = self._held("click", {"hwnd": hwnd, "element": f"{role} in the {step.box_role}"},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": f"{role} in the {step.box_role}"})
            if verdict is not None:
                return held(verdict, target)
            try:
                lead = self.hands._front(hwnd)
            except Exception as exc:  # noqa: BLE001
                return done(ok=False, verdict=False, tool="click", target=target, app=app, hwnd=hwnd,
                            why="its window could not be brought to the front", report=str(exc)[:200],
                            deviation="window")
            outcome = self.hands._dispatcher.dispatch("click", {"element": element["id"], "read": read,
                                                                "expect_hwnd": hwnd})
            verdict, why = action_verdict("click", outcome)
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="click",
                        target=f"{target} in \"{title[:60]}\"", app=app, hwnd=hwnd,
                        report=f"{lead}picked a {role} in the {step.box_role}: {outcome.summary}")
        if step.do == "type":
            snap = self._focus(FOCUS_WAIT_S if not first else 0.4)
            if snap is None:
                return done(ok=False, verdict=False, kind="read", tool="focus", target="a text field",
                            why="no text field has the keyboard", report="no text field has the keyboard",
                            deviation="no_focus")
            hwnd = int(getattr(snap, "window", 0) or 0) or None
            window = self._windows(self._overview()).get(hwnd or 0)
            app = self._app_of(window)
            field_name = f'{getattr(snap, "role", "") or "field"} "{str(getattr(snap, "name", "") or "")[:60]}"'
            check = self._held("type_text", {"hwnd": hwnd, "text": step.text},
                               {"hwnd": hwnd, "app": app, "title": str((window or {}).get("title") or ""),
                                "element": field_name}, force=True)
            if check is not None:
                return held(check, field_name)
            outcome = self.hands._dispatcher.dispatch("type_text", {"text": step.text,
                                                                    **({"expect_hwnd": hwnd} if hwnd else {})})
            verdict, why = action_verdict("type_text", outcome)
            return done(ok=bool(outcome.ok), verdict=verdict if outcome.ok else False, why=why, tool="type_text",
                        target=field_name, app=app, hwnd=hwnd, report=f"typed into {field_name}: {outcome.summary}")
        if step.do == "keys":
            payload = self._overview()
            front = int(payload.get("foreground_hwnd") or 0)
            window = self._windows(payload).get(front)
            app = self._app_of(window)
            mine = front == start_front or front in opened
            if not front or not (self._in_way(app, apps) if apps else mine):
                return done(ok=False, verdict=False, kind="read", tool="hotkey", target=f"window {app or front}",
                            why="the window in front is not the way's app", deviation="window",
                            report=f"the window in front is {app or 'unknown'}, not one the way acts in; no key sent")
            title = str((window or {}).get("title") or "")
            check = self._held("hotkey", {"hwnd": front, "keys": list(step.keys)},
                               {"hwnd": front, "app": app, "title": title})
            if check is not None:
                return held(check, f'keys in "{title[:60]}"')
            outcome = self.hands._dispatcher.dispatch("hotkey", {"keys": list(step.keys), "expect_hwnd": front})
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
        try:
            if step.do == "scroll":
                hands.snapshot()   # the window may be new since the talker's summary
                hwnd = step.window or int(self._overview().get("foreground_hwnd") or 0)
                ok, report, facts = hands._act("scroll", {"window": hwnd, "direction": step.direction,
                                                          "amount": step.amount}, facts)
                tool, target, app = "scroll", f"window {hwnd}", facts.get("app")
            elif step.do == "open_app":
                ok, report, facts = hands._act("open_app", {"name": step.name or step.app}, facts)
                tool, target, app = "launch_app", f'app "{step.name or step.app}"', step.name or step.app
            else:
                ok, report, facts = hands._open_url({"url": step.url, "app": step.app}, facts)
                tool, target, app = "open_url", step.url, step.app or None
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
        why = check_sends(args, words) or check_texts(steps, words, way_text)
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
        payload = self._overview()
        start_front = int(payload.get("foreground_hwnd") or 0)
        before = set(self._windows(payload))
        opened: set[int] = set()
        results: list[StepResult] = []
        deviation: StepResult | None = None
        after_open = False
        self._earlier = []
        part_before = self._front_app() if ends else ("", "")
        for n, step in enumerate(steps):
            result = self._step(step, apps=apps, first=n == 0, after_open=after_open, start_front=start_front,
                                opened=opened)
            if result.kind == "action" and result.ok:
                import json

                self._earlier.append(f"{result.tool} {json.dumps(trace_steps([result], None)[0]['input'])}")
            if step.do in ("open_app", "open_url"):
                opened |= set(self._windows(self._overview())) - before
                after_open = True
            elif result.kind == "action":
                after_open = False
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
                deviation = result
                break
            if n in ends:
                # capC4: a part ends on a checked state; the next part starts only once it is reached
                k, end = ends[n]
                ok_end, why_end = self.end_ok(end, result, part_before)
                if not ok_end:
                    deviation = StepResult(step=step, ok=False, verdict=False, kind="read", tool="check",
                                           why=f"part {k} of the way did not reach its checked end ({why_end})",
                                           report=f"part {k} ended without its checked state: {why_end}",
                                           deviation="part_end", at=time.time())
                    results.append(deviation)
                    break
                if result.verdict is None:
                    result.verdict, result.why = True, f"part {k} reached its checked end ({why_end})"
                part_before = self._front_app() if n + 1 < len(steps) else part_before
            nxt = steps[n + 1] if n + 1 < len(steps) else None
            if nxt is not None and result.verdict is None and nxt.do == "keys":
                # a key after a step whose effect could not be seen would be blind (v3.5: no blind keys)
                deviation = StepResult(step=nxt, ok=False, verdict=False, kind="read", tool="hotkey",
                                       why=f"the effect of step {step.i} could not be seen before this key",
                                       report=f"step {step.i}'s report could not show its effect, so no key was sent "
                                              f"after it", deviation="blind", at=time.time())
                results.append(deviation)
                break
        verified = deviation is None and bool(results) and results[-1].verdict is True
        ref = str(item.get("ref") or f"pb:{item.get('id')}")
        intent = _words(item.get("intent"))[:80]
        lines = [f"{r.step.i}. {r.step.what()} -> "
                 + ("verified" if r.verdict is True else "failed" if not r.ok else "not verified" if r.verdict is False
                    else "could not tell") + f" ({r.why}): {_words(r.report)[:STEP_REPORT_CHARS]}"
                 for r in results if not (r.kind == "read" and r is deviation and r.deviation == "blind")]
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
        done_lines = [ln for r, ln in zip(results, lines) if r is not deviation]
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
        ran = deviation.kind == "action"   # the deviating step itself ran (its report showed no effect, or it failed)
        at = (f"step {deviation.step.i} ({deviation.step.what()}) " + ("ran, but " if ran else "was not done: ")
              + deviation.why) if deviation.deviation != "part_end" else (
            f"after step {deviation.step.i} ({deviation.step.what()}), {deviation.why}")
        if deviation.deviation == "part_end":
            ran = True
        report = (f"stopped the learned way {ref} (\"{intent}\"): {at}. "
                  + (("Done before it: " + " ".join(done_lines)) if done_lines else
                     ("" if ran else "Nothing was done.")))
        facts["skill_context"] = (
            f"I tried my learned way {ref} (\"{intent}\") with my own hands. "
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
                                                                                  ("name", s.box_name)) if v}},
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
           "fit_to_way", "fill_slots", "way_step", "said", "trace_steps", "SKILL_DOS", "MAX_PARTS"]
