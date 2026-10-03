"""Talker skills: a learned way run as the talker's hand, step by step, each step checked (v3.18, 07 §6 F2).

Practice memory keeps the ways that worked on this PC as procedures: short steps by a control's role and visible name,
keys by name, what changes each time as slots (``yuki.memory.practice``; written by the distiller from Yuki's own run
traces, each step citing trace steps that ran and were not shown to fail). When one fits the user's words closely and
memory is sure of it (:mod:`yuki.voice.practice` offers it on the talker's "Learned ways" line), the talker can run it
with ``run_skill`` instead of handing the request to the thinker: it writes the way's steps as structured hand steps
(the role and name, the keys, the text from their words) in its one reply, and this module runs them through the very
hands the talker already has (the same backend, dispatcher, guards and verdicts):

* **click**: the control (role and name, as the way names it) is found in a fresh read of the window in front, a popup
  or a window of the way's app, waiting a moment for it to appear after the step before; clicked by that read's
  element id, verified by the action layer's watch (``yuki.agent.loop.action_verdict``).
* **type**: only into the text field that has the keyboard, only words the user said or the way's own words (a slot
  filled from their words), never anything pressed after; verified by reading the field back.
* **keys**: held together, only into a window of the way's own app (or the one in front when it started), guarded by
  ``expect_hwnd``; verified by the watch (focus, window, title, state).
* **scroll**, **open_app**, **open_url**: the talker's own hands of those names.

**Each step's expectation is checked** before the next one runs: the action's own measured verdict (a step whose report
shows it did not take effect, or that failed, stops the run), the next step's precondition (its control on the screen,
a text field with the keyboard), and, when the talker wrote one, the step's ``expect``: words the way says will show
once it worked, looked for as a whole-word span in the title and the control names of the window in front. A key never
follows a step whose effect could not be seen (no blind chains). **At the first deviation the run stops** and the
thinker gets the request by code with what was done and where it deviated (the talker says nothing more: one outcome).

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
from typing import Any

#: What a skill step may do: the talker's own verified hands.
SKILL_DOS = ("click", "type", "keys", "scroll", "open_app", "open_url")
#: Most steps one run takes (practice memory keeps at most 8 per procedure).
MAX_STEPS = 8
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

    def what(self) -> str:
        """The step in a few words, for reports (content: the thinker and memory only)."""
        if self.do == "click":
            return f'click {self.role or "control"} "{self.name}"'
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


def parse_steps(raw: Any) -> list[SkillStep]:
    """The talker's steps, validated (raises :class:`SkillRefused` before anything runs)."""
    if not isinstance(raw, list) or not raw:
        raise SkillRefused("no steps were given")
    if len(raw) > MAX_STEPS:
        raise SkillRefused(f"more than {MAX_STEPS} steps")
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


def check_texts(steps: list[SkillStep], words: list[str], way_text: str) -> str | None:
    """Why a step's text may not be typed (``None``: all may): only the user's own words or the way's own words."""
    for step in steps:
        if step.do == "type" and not said(step.text, [*words, way_text]):
            return (f"step {step.i} would type \"{step.text[:60]}\", which is neither their words nor the learned "
                    f"way's own")
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

    def _held(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any]) -> Any:
        """Yuki's own authorization check on a step, as the thinker's tool it is (revamp D S1: keys can close or send
        anything, so every key step is judged; clicks and typing only when the user's policy holds them back): the
        verdict when it holds the step, else ``None``. Reads only their words and the calls."""
        authorize = getattr(self.hands, "authorize", None)
        if not callable(authorize):
            return None
        verdict = authorize(tool, tool_input, target, list(self._earlier))
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
            verdict = self._held("click", {"hwnd": hwnd, "element": role_name},
                                 {"hwnd": hwnd, "app": app, "title": title, "element": role_name})
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
                                "element": field_name})
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
        steps = parse_steps(args.get("steps"))
        way_text = str(item.get("text") or "")
        why = check_sends(args, words) or check_texts(steps, words, way_text)
        if why:
            raise SkillRefused(why)
        apps = [str(a) for a in item.get("apps") or [] if a]
        payload = self._overview()
        start_front = int(payload.get("foreground_hwnd") or 0)
        before = set(self._windows(payload))
        opened: set[int] = set()
        results: list[StepResult] = []
        deviation: StepResult | None = None
        after_open = False
        self._earlier = []
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
            if result.ok and result.deviation is None and result.verdict is not False and step.expect:
                if self._shows(step.expect, EXPECT_WAIT_S):
                    result.verdict, result.why = True, f'"{step.expect[:40]}" shows on the screen, as the way expects'
                else:
                    result.deviation = "expect"
                    result.verdict = False
                    result.why = f'"{step.expect[:40]}" does not show on the screen, as the way expected'
            results.append(result)
            if result.deviation is None and (not result.ok or result.verdict is False):
                result.deviation = "failed" if not result.ok else "no_effect"
            if result.deviation is not None:
                deviation = result
                break
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
        firsts = [r for r in results if r.kind == "action"]
        if firsts:
            facts["sent_at"] = firsts[0].started   # the turn log's end of speech -> action
        if deviation is None:
            report = (f"ran the learned way {ref} (\"{intent}\") by your hands, {len(steps)} step(s), each checked: "
                      + " ".join(lines))
            if not verified:
                report += " The last step's effect could not be seen."
            return True, report, facts
        done_lines = [ln for r, ln in zip(results, lines) if r is not deviation]
        if deviation.deviation == "held":
            # Yuki's own check held a step (revamp D S1): nothing more runs; the user is asked, never the thinker
            facts["held"] = True
            facts["question"] = deviation.question
            report = (f"stopped the learned way {ref} (\"{intent}\") before step {deviation.step.i} "
                      f"({deviation.step.what()}): {deviation.why}. "
                      + (("Done before it: " + " ".join(done_lines)) if done_lines else "Nothing was done."))
            return False, report, facts
        ran = deviation.kind == "action"   # the deviating step itself ran (its report showed no effect, or it failed)
        at = (f"step {deviation.step.i} ({deviation.step.what()}) " + ("ran, but " if ran else "was not done: ")
              + deviation.why)
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
            tool_input.update(role=s.role, name=s.name)
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
           "said", "trace_steps", "SKILL_DOS"]
