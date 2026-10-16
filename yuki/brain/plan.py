"""The plan: one object per model call that says what the user wants, where, how, and how Yuki will know it worked.

Brain rebuild rev 2, design section 2.2 (``docs/research/audit-2026-10-09/next/brain-design.md``). bCore owns this file;
bExec builds the executor against the same pinned text (the schema below and :class:`ExecHost`).

* :func:`plan_schema` is the ``plan`` tool's ``input_schema``: strict-compatible (every object
  ``additionalProperties: false``, every property required, nullable scalars as type arrays, nullable objects as
  ``anyOf [object, null]``), no ``maxLength`` (caps are enforced here, in code), no free-form object (probe arguments
  are a JSON object written as a string and checked against the probe's own argument list), no skill ids (the tool
  block stays byte-stable while the menu changes nightly), ``steps`` last (the consent and done fields are complete
  before step 1 can run), no self-report field and no ``done.line`` (code writes the line from the probe's evidence).
* :func:`parse_header`, :func:`parse_step` and :func:`validate_plan` are the validator. The planner validates the header
  once ``steps`` begins, each step as its object closes in the stream, and the whole plan at the end. A failure is a
  :class:`PlanError` whose plain-words ``errors`` go back to the model as an ``is_error`` tool result (retried within
  the replan budget; its code words are logged: :func:`error_codes`).
* :class:`ExecHost` is the protocol the executor runs against, verbatim as pinned in design 10.1.
* :class:`StepSource` and :class:`ExecResult` are bCore's side of the executor seam (what the Brain hands the executor
  and what it reads back). They are not part of the pinned text: the merge reviewer reconciles them with bExec's
  executor in ``yuki/brain/agent.py`` (``_run_executor``), the one place that calls it.

Nothing here reads the user's words for meaning: verbs, probes, commit classes and routes are closed vocabularies, and
the checks are on types, sizes, string identity and the catalog's own data (``sends_with``).
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from yuki.brain import done as _done
from yuki.log.events import AgentEvent

# ---------------------------------------------------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------------------------------------------------

#: Act verbs: the Dispatcher's own tool names (``yuki.agent.tools.Dispatcher``), so run rows carry the names phase L's
#: asked occasions already map (design H4): no translation table can drift.
ACT_VERBS: tuple[str, ...] = (
    "launch_app", "focus_window", "open_url", "window_action", "tab", "click", "type_text", "hotkey", "press",
    "scroll", "set_slider", "app_volume", "run_powershell", "write_clipboard", "run_skill", "open_yuki",
)
#: Read verbs: they change nothing (an answer plan holds only these). ``wait`` is a bounded pause.
READ_VERBS: tuple[str, ...] = ("look", "memory", "fetch", "wait")
VERBS: tuple[str, ...] = ACT_VERBS + READ_VERBS

KINDS: tuple[str, ...] = ("act", "answer", "ask")
COMMITS: tuple[str, ...] = ("none", "send", "join", "lasting")

#: The skill engine's probes (``yuki.skills.connection.PROBES`` minus ``all``, read through ``donecheck``) and the four
#: generic UIA probes: one list, ``yuki/brain/done.py``'s (bWire merge: the plan and the done-check read one table).
SKILL_PROBES: tuple[str, ...] = _done.ENGINE_PROBES
GENERIC_PROBES: tuple[str, ...] = _done.GENERIC_PROBES
#: The closed probe list of ``done.probe`` and ``expect.probe``; ``none`` is allowed and is never "done".
PROBES: tuple[str, ...] = SKILL_PROBES + GENERIC_PROBES + ("none",)

#: Each probe's argument names, required first (what ``done.args`` / ``expect.args`` may hold): ``done.PROBE_ARGS``,
#: which :func:`yuki.brain.done.parse_args` enforces (a process probe's ``window`` is read as its ``process``).
PROBE_ARGS: dict[str, tuple[str, ...]] = {
    **{name: (*required, *optional) for name, (required, optional) in _done.PROBE_ARGS.items()}, "none": ()}
#: Probes only the skill engine can read (a route's own read-back, a driver module's probe): never a plan's pick.
ENGINE_ONLY_PROBES: frozenset[str] = frozenset({"read_back", "driver"})


def probe_args_text() -> str:
    """Each probe a plan may pick, with its argument names (``?``: optional), on one line for the plan tool: the
    planner writes ``done.args`` / ``expect.args`` with the names the done-check reads (``done.PROBE_ARGS``)."""
    parts = []
    for name, (required, optional) in _done.PROBE_ARGS.items():
        if name in ENGINE_ONLY_PROBES:
            continue
        names = [*required, *(f"{o}?" for o in optional)]
        if name == "window_state":
            names = [n if n != "state" else "state=" + "|".join(_done.WINDOW_STATES) for n in names]
        parts.append(f"{name} {','.join(names)}")
    return "done/expect args, JSON object as string (? optional): " + "; ".join(parts)

#: What ``look`` reads (a step's value, the ``look`` tool's ``what``) and the windows it needs.
LOOK_WHATS: tuple[str, ...] = ("desktop", "window", "page", "tabs", "focus", "clipboard", "system")
LOOK_NEEDS_WINDOW: frozenset[str] = frozenset({"window", "page", "tabs"})
WINDOW_ACTIONS: tuple[str, ...] = ("minimize", "maximize", "fullscreen", "restore", "focus", "close")
TAB_ACTIONS: tuple[str, ...] = ("select", "close")
CLICK_BUTTONS: tuple[str, ...] = ("left", "right", "middle", "double")
MEMORY_KINDS: tuple[str, ...] = (
    "recall", "activity", "calls", "call_timeline", "threads", "before", "links", "people", "skills",
)

#: How each verb's fields are read (the executor's contract; the tool description says it in short). ``open_url``'s
#: window is the browser it opens in (the executor hands that window's app to the tool: x10, Item 9's side): a step
#: with no browser opens in Windows' default one.
VERB_FIELDS: dict[str, str] = {
    "launch_app": "value: app name",
    "focus_window": "window",
    "open_url": "window: the browser it opens in; value: address, file or folder",
    "window_action": "window; value: minimize|maximize|fullscreen|restore|focus|close",
    "tab": "window; element: the tab; value: select|close",
    "click": "element; value: null|right|middle|double",
    "type_text": "element or focus; value: text; submit: Enter after",
    "hotkey": "value: chord (ctrl+t)",
    "press": "value: one key",
    "scroll": "element or window; value: notches, + is up",
    "set_slider": "element; value: 0-100",
    "app_volume": "target.app; value: 0-100, +N, -N, mute|unmute",
    "run_powershell": "value: command",
    "write_clipboard": "value: text",
    "run_skill": "value: JSON {\"skill\": id, \"slots\": {}}",
    "open_yuki": "value: page",
    "look": "value: desktop|window|page|tabs|focus|clipboard|system; window",
    "memory": "value: JSON memory-tool args with kind",
    "fetch": "value: http(s) address or file path",
    "wait": "value: seconds (max 10)",
}

#: String caps, enforced in code (strict mode has no ``maxLength``). Goal, question and say are cut to fit (display
#: text); a value over its cap is rejected (a cut value could act on the wrong thing).
GOAL_CHARS = 200
QUESTION_CHARS = 200
SAY_CHARS = 120
VALUE_CHARS = 1500
TARGET_CHARS = 200
QUOTE_CHARS = 600
NAME_CHARS = 200
ROLE_CHARS = 60
ARGS_CHARS = 1500
#: The most steps one plan holds (a longer job is replanned after a look).
MAX_STEPS = 15
#: The longest ``wait`` step (s).
WAIT_MAX_S = 10.0

#: The plan tool's name.
PLAN_TOOL = "plan"
#: A route that runs steps (the other form is ``skill:<id>``).
STEPS_ROUTE = "steps"
SKILL_ROUTE_PREFIX = "skill:"


# ---------------------------------------------------------------------------------------------------------------------
# The schema (strict-compatible)
# ---------------------------------------------------------------------------------------------------------------------


def _nullable(kind: str, description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": [kind, "null"]}
    if description:
        out["description"] = description
    return out


def _probe_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["probe", "args"],
        "properties": {
            # the probes a plan may write (a seed's engine-only probe is taken by writing none: the seed's check
            # applies); each one's argument names are in the plan tool's description (probe_args_text), so the
            # 8,000-character tool block stays in budget (bWire merge)
            "probe": {"type": "string", "enum": [p for p in PROBES if p not in ENGINE_ONLY_PROBES]},
            "args": {"type": "string"},
        },
    }


def plan_schema() -> dict[str, Any]:
    """The ``plan`` tool's ``input_schema`` (design 2.2). Built fresh each call, the same bytes every time."""
    target = {
        "type": "object",
        "additionalProperties": False,
        "required": ["app", "window", "person", "item"],
        "properties": {
            "app": _nullable("string"),
            "window": _nullable("string"),
            "person": _nullable("string"),
            "item": _nullable("string"),
        },
    }
    consent = {
        "type": "object",
        "additionalProperties": False,
        "required": ["commit", "quote", "to", "text"],
        "properties": {
            "commit": {"type": "string", "enum": list(COMMITS)},
            "quote": _nullable("string", "Their exact words asking for it."),
            "to": _nullable("string", "The person or channel."),
            # sBrainFix (F1c): their own words, never translated or restyled (the core prompt says it in full)
            "text": _nullable("string", "The message, in their words."),
        },
    }
    element = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "role"],
                "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
            },
            {"type": "null"},
        ]
    }
    step = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verb", "window", "element", "value", "submit", "expect", "say"],
        "properties": {
            "verb": {"type": "string", "enum": list(VERBS)},
            "window": _nullable("string", "An hwnd or a title."),
            "element": element,
            "value": _nullable("string"),
            "submit": {"type": "boolean"},
            "expect": {"anyOf": [_probe_schema(), {"type": "null"}]},
            # sBrainFix (F7): spoken the moment the step is dispatched, so before its result (the room: revises's
            # description, which said nothing code uses: a replan's revises is code's own, parse_header)
            "say": _nullable("string", "Spoken before any result."),
        },
    }
    # goal, route and question carry no description here: the core prompt says each (one line in their terms; steps
    # or skill:<id>; kind ask: one question), and the probe arguments took the room (say has a few words: sBrainFix)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["goal", "kind", "revises", "target", "route", "consent", "done", "question", "steps"],
        "properties": {
            "goal": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "revises": _nullable("string"),
            "target": target,
            "route": {"type": "string"},
            "consent": consent,
            "done": _probe_schema(),
            "question": _nullable("string"),
            "steps": {"type": "array", "items": step},
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# The plan as data
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    app: str | None = None
    window: str | None = None
    person: str | None = None
    item: str | None = None

    def fields(self) -> dict[str, str]:
        """The non-null fields (what a done probe must cover, design 2.4)."""
        return {k: v for k, v in (("app", self.app), ("window", self.window), ("person", self.person),
                                  ("item", self.item)) if v}


@dataclass(frozen=True)
class Consent:
    commit: str = "none"
    quote: str | None = None
    to: str | None = None
    text: str | None = None

    @property
    def may_commit(self) -> bool:
        return self.commit != "none"


@dataclass(frozen=True)
class Probe:
    """A probe name and its parsed arguments (``raw``: the JSON text the model wrote, kept for the log's size only)."""

    name: str = "none"
    args: Mapping[str, Any] = field(default_factory=dict)
    raw: str = "{}"

    @property
    def none(self) -> bool:
        return self.name == "none"

    def same(self, other: "Probe | None") -> bool:
        return other is not None and self.name == other.name and dict(self.args) == dict(other.args)


@dataclass(frozen=True)
class Element:
    name: str
    role: str = ""

    @property
    def ref_id(self) -> int | None:
        """An element id from a look written as the name (``[12]`` or ``#12``), else ``None``."""
        text = self.name.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        elif text.startswith("#"):
            text = text[1:]
        return int(text) if text.isdigit() else None


@dataclass(frozen=True)
class Step:
    index: int
    verb: str
    window: str | None = None
    element: Element | None = None
    value: str | None = None
    submit: bool = False
    expect: Probe | None = None
    say: str | None = None

    @property
    def is_read(self) -> bool:
        return self.verb in READ_VERBS

    @property
    def hwnd(self) -> int | None:
        """``window`` as a window handle when it is one (digits), else ``None`` (a title hint)."""
        text = (self.window or "").strip()
        return int(text) if text.isdigit() else None

    def skill_call(self, route_skill: str | None = None) -> tuple[str, dict[str, Any]] | None:
        """``(skill id, slots)`` of a ``run_skill`` step (its value's JSON; the route's skill when it names none)."""
        if self.verb != "run_skill":
            return None
        data = _json_object(self.value) if self.value else {}
        if data is None:
            return None
        skill = str(data.get("skill") or route_skill or "").strip()
        slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
        return (skill, dict(slots)) if skill else None

    def same_as(self, other: "Step | None") -> bool:
        """The same action as ``other`` (verb, window, element, value, submit): a replan that repeats a failed step
        unchanged."""
        return (other is not None and self.verb == other.verb and (self.window or "") == (other.window or "")
                and self.element == other.element and (self.value or "") == (other.value or "")
                and self.submit == other.submit)


@dataclass(frozen=True)
class Plan:
    """One validated plan. ``id`` is code's (``p1``, ``p2`` ...). ``seeded``: made in a request a skill handed over
    (rung C). ``done_by_skill``: a skill route checks itself (the engine's own done-check and line). ``seed_check``: the
    hand-off's own fixed check decides this plan (the planner wrote ``none`` for it, or the seed's probe itself); a
    seeded plan by steps that wrote a probe of its own is decided by that probe (x10)."""

    id: str
    goal: str
    kind: str
    revises: str | None
    target: Target
    route: str
    consent: Consent
    done: Probe
    question: str | None
    steps: tuple[Step, ...] = ()
    seeded: bool = False
    done_by_skill: bool = False
    seed_check: bool = False

    @property
    def skill(self) -> str | None:
        """The skill id of a ``skill:<id>`` route, else ``None``."""
        return self.route[len(SKILL_ROUTE_PREFIX):] if self.route.startswith(SKILL_ROUTE_PREFIX) else None

    @property
    def acts(self) -> bool:
        return self.kind == "act"

    def header(self) -> "Plan":
        return replace(self, steps=())

    def with_steps(self, steps: Sequence[Step]) -> "Plan":
        return replace(self, steps=tuple(steps))

    def facts(self) -> dict[str, Any]:
        """Content-free facts for the log (code words and counts only)."""
        return {"plan": self.id, "kind": self.kind, "route": "skill" if self.skill else STEPS_ROUTE,
                "commit": self.consent.commit, "probe": self.done.name, "steps": len(self.steps),
                "verbs": [s.verb for s in self.steps], "revises": bool(self.revises), "seeded": self.seeded,
                "seed_check": self.seed_check, "goal_chars": len(self.goal), "target": sorted(self.target.fields())}


class PlanError(Exception):
    """The plan (or one step of it) failed validation: ``errors`` go back to the model in plain words."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = [str(e) for e in errors if str(e)]
        super().__init__("; ".join(self.errors) or "invalid plan")

    @property
    def codes(self) -> list[str]:
        """The errors as log code words (:func:`error_codes`)."""
        return error_codes(self.errors)


#: Most code words one error becomes (its first words: enough to tell the validator's rules apart).
ERROR_CODE_WORDS = 6
#: The longest error code (the content-free log keeps a code word of at most 48 characters).
ERROR_CODE_CHARS = 48


def error_code(text: str) -> str:
    """One validator error as a log code word: the validator's own sentence without its step number and without any
    quoted value (the only place a model-written value can stand in it: ``no skill 'x' in the menu``), its first few
    words joined by ``_``. The validator writes every error, so this is its wording as a code, never the model's or
    the user's words (x10: the PlanError texts are logged, content-free)."""
    out = str(text or "").strip()
    if out.startswith("step ") and ":" in out[:12]:
        head, _, rest = out.partition(":")
        if head[5:].strip().isdigit():
            out = rest
    clean: list[str] = []
    quote = ""
    prev = " "
    for ch in out:
        if quote:
            if ch == quote:
                quote = ""
            prev = " "
            continue
        # a quote opens only where a word does not go on through it ("skill's" is one word, 'x' is a value)
        if ch in "'\"" and not prev.isalnum():
            quote = ch
            continue
        clean.append(ch.lower() if ch.isascii() and ch.isalnum() else " ")
        prev = ch
    words = "".join(clean).split()[:ERROR_CODE_WORDS]
    code = "_".join(words)[:ERROR_CODE_CHARS].strip("_")
    return code if code and code[0].isalpha() else ("e_" + code)[:ERROR_CODE_CHARS] if code else "invalid"


def error_codes(errors: Sequence[str]) -> list[str]:
    """Each validator error as a code word (:func:`error_code`), duplicates once, in order."""
    return list(dict.fromkeys(error_code(e) for e in errors if str(e).strip()))


# ---------------------------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Checks:
    """What validation needs besides the plan itself.

    Attributes:
        plan_id: The id code gives this plan.
        channel: ``voice`` | ``typed``.
        seed: The plan seed from a skill's hand-off (``{"skill", "done": {"probe", "args"}, ...}``), or ``None``.
        skills: The live catalog's skill ids (``None``: the catalog could not be read; a skill route is then refused).
        sends: ``sends(skill_id, slots) -> bool``: the catalog's own ``sends_with`` (a missing slot counts as sending).
        failed_step: The step that just failed (a replan must not repeat it unchanged, once).
        repeat_rejected: A repeat was already rejected once in this request (the next one is accepted).
        current_plan: The id of the plan a replan revises, or ``None``.
        spent_skills: Skills that already ran in this request and handed over or couldn't confirm the result (code
            ids): a plan that runs one again is invalid, as is one that runs the skill the seed handed over
            (:func:`spent_skills`).
        done_skills: Skills whose skill route ended done in this request (sBrainFix, F2: the planner was then asked
            for the rest of the request): that exact skill is not run again here (its family still may, for another
            value).
        words: The user's own words of this request (bRound2, F1c: a send's text is measured against them,
            :func:`send_text_error`); empty: not measured.
        text_bounced: A send's text that kept too few of their words was already sent back once in this request
            (the next one is accepted, and row 4 asks with that text). ``bounced_now``: it was this call's plan (one
            Checks per model call), so the same plan validated again is bounced the same.
    """

    plan_id: str
    channel: str = "typed"
    seed: Mapping[str, Any] | None = None
    skills: frozenset[str] | None = None
    sends: Callable[[str, Mapping[str, Any]], bool] | None = None
    failed_step: Step | None = None
    repeat_rejected: bool = False
    current_plan: str | None = None
    spent_skills: frozenset[str] = frozenset()
    done_skills: frozenset[str] = frozenset()
    words: tuple[str, ...] = ()
    text_bounced: bool = False
    bounced_now: bool = False


#: A hand-off whose skill never ran its own way to the end: one of its steps would send or join with no plan behind it
#: (``yuki.skills.engine.TO_PLANNER``), so the planner runs that skill with the consent it needs. Every other hand-off
#: means the skill's own way didn't get there (its routes failed, it couldn't confirm, a value it was given was
#: dropped): running it again would do the same, so the plan does it by steps.
SEED_RERUN_OK: frozenset[str] = frozenset({"to_planner"})


def skill_family(key: Any) -> str:
    """A skill's family: a learned skill (``web.open_page#c841...``) and the capability it specialises run the same
    routes, so one that just handed over stands for both."""
    return str(key or "").strip().split("#", 1)[0]


def seed_skill(seed: Mapping[str, Any] | None) -> str | None:
    """The skill a plan seed handed over when running it again would only repeat it (its hand-off's ``why`` is not in
    :data:`SEED_RERUN_OK`), else ``None``."""
    if not isinstance(seed, Mapping):
        return None
    key = str(seed.get("skill") or seed.get("key") or "").strip()
    if not key or str(seed.get("why") or "").strip() in SEED_RERUN_OK:
        return None
    return key


def spent_skills(checks: Checks) -> dict[str, str]:
    """Skill families a plan of this request may not run again, each with why in plain words: the seed's skill (it
    handed over) and :attr:`Checks.spent_skills` (they ran in this request and handed over or couldn't confirm)."""
    out = {skill_family(k): "already ran in this request without a confirmed result" for k in checks.spent_skills
           if skill_family(k)}
    handed = seed_skill(checks.seed)
    if handed:
        out[skill_family(handed)] = "just handed over: its own way didn't get there"
    return out


def _spent_error(where: str, skill: str, checks: Checks) -> str | None:
    """The error for a plan that runs a skill that just handed over or couldn't confirm (``skill``: a menu id, so the
    error holds no model-written text), else ``None``."""
    why = spent_skills(checks).get(skill_family(skill))
    if why is None and str(skill or "").strip() in checks.done_skills:
        why = "already did its part in this request"
    if why is None:
        return None
    # the id in quotes: the log's code of this error (error_code) leaves quoted values out
    return (f"{where}skill '{skill}' {why}, so don't run it again: do it by steps (look, then click or type in the "
            f"app), route steps")


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
#: The verbs whose value is a command, an address or a path, taken exactly as written (a ``\u`` there is its own: a
#: folder's name), never :func:`unescape`'d.
RAW_VALUE_VERBS: frozenset[str] = frozenset({"run_powershell", "fetch", "open_url"})


def _hex4(text: str, at: int) -> int | None:
    chunk = text[at:at + 4]
    return int(chunk, 16) if len(chunk) == 4 and all(c in _HEX_DIGITS for c in chunk) else None


def _pathlike(text: str) -> bool:
    """``text`` holds a Windows path (a drive's ``X:\\`` or a ``\\\\`` share): its ``\\u`` is a folder's name."""
    if "\\\\" in text:
        return True
    at = text.find(":\\")
    while at != -1:
        if at > 0 and text[at - 1].isascii() and text[at - 1].isalpha():
            return True
        at = text.find(":\\", at + 1)
    return False


def _latin(char: str) -> bool:
    """ASCII or a Latin letter or mark: a script the model writes as it is, never as an escape."""
    return ord(char) < 0x250 or "LATIN" in unicodedata.name(char, "")


def unescape(text: str) -> str:
    """``text`` with the JSON escapes the model sometimes writes twice decoded: each well-formed ``\\uXXXX`` (a
    surrogate pair joined) becomes its character. A format fix, never a reading of meaning (x10: in the simulation the
    planner wrote the playlist row "ネオン" as ``\\u30cd\\u30aa\\u30f3`` in an element name, a target and a
    probe's arguments, so its click would have looked for a control by that literal name). A lone surrogate or a
    control character stays as written. The text stays exactly as written when it holds a Windows path
    (``C:\\Users\\me\\ufeed``) or when any escape in it stands for ASCII or a Latin letter (``"\\u00e9"`` typed into
    code): the model writes those as they are, so such a ``\\u`` is the text's own (review r2 L2)."""
    if not isinstance(text, str) or "\\u" not in text or _pathlike(text):
        return text
    out: list[str] = []
    decoded = False
    i, n = 0, len(text)
    while i < n:
        if text.startswith("\\u", i):
            code = _hex4(text, i + 2)
            if code is not None:
                end = i + 6
                if 0xD800 <= code < 0xDC00 and text.startswith("\\u", end):
                    low = _hex4(text, end + 2)
                    if low is not None and 0xDC00 <= low < 0xE000:
                        code, end = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00), end + 6
                if code >= 0x20 and not 0xD800 <= code < 0xE000:
                    if _latin(chr(code)):
                        return text   # not the model's doubled escape: the text's own "\u"
                    out.append(chr(code))
                    decoded = True
                    i = end
                    continue
        out.append(text[i])
        i += 1
    return "".join(out) if decoded else text


def _unescape_data(value: Any) -> Any:
    """:func:`unescape` on every string inside parsed JSON data (keys too)."""
    if isinstance(value, str):
        return unescape(value)
    if isinstance(value, list):
        return [_unescape_data(v) for v in value]
    if isinstance(value, dict):
        return {unescape(str(k)): _unescape_data(v) for k, v in value.items()}
    return value


def _unescape_json_text(text: str) -> str:
    """A JSON value written as a string (a ``run_skill`` or ``memory`` step's value) with :func:`unescape` applied to
    the strings inside it, written back as JSON; the text as it was when it holds no such escape or isn't one JSON
    object (the validator then says so)."""
    if "\\\\u" not in text:
        return text
    data = _json_object(text)
    if data is None:
        return text
    fixed = _unescape_data(data)
    return json.dumps(fixed, ensure_ascii=False) if fixed != data else text


def _json_object(text: Any) -> dict[str, Any] | None:
    """``text`` parsed as one JSON object, or ``None``."""
    if isinstance(text, dict):
        return dict(text)
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text.strip() or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _opt_str(raw: Mapping[str, Any], key: str, errors: list[str], where: str, cap: int | None = None,
             cut: bool = True) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{where}{key} must be a string or null")
        return None
    value = unescape(value).strip()
    if not value:
        return None
    if cap is not None and len(value) > cap:
        if not cut:
            errors.append(f"{where}{key} is longer than {cap} characters")
            return None
        value = value[: cap - 1].rstrip() + "…"
    return value


def _probe(raw: Any, errors: list[str], where: str) -> Probe | None:
    if not isinstance(raw, Mapping):
        errors.append(f"{where} must be an object with probe and args")
        return None
    name = raw.get("probe")
    if name not in PROBES:
        errors.append(f"{where}.probe must be one of: {', '.join(PROBES)}")
        return None
    text = raw.get("args")
    text = "{}" if text is None else text
    if not isinstance(text, str):
        errors.append(f"{where}.args must be a JSON object written as a string")
        return None
    if len(text) > ARGS_CHARS:
        errors.append(f"{where}.args is longer than {ARGS_CHARS} characters")
        return None
    args = _json_object(text)
    if args is None:
        errors.append(f"{where}.args must parse as one JSON object (\"{{}}\" for none)")
        return None
    args = _unescape_data(args)
    if name == "none":
        if args:
            errors.append(f"{where}.args for none take nothing (\"{{}}\")")
            return None
    else:
        # one contract (bWire merge): the done-check reads the arguments exactly as this accepts them (its names, a
        # process probe's window as its process, the required ones present, and the value types: a string, number or
        # true/false each; uia_name's container a list of [role, name] pairs)
        parsed, why = _done.parse_args(str(name), args)
        if parsed is None:
            errors.append(f"{where}.args: {why}")
            return None
    return Probe(name=str(name), args=args, raw=text)


def probe_errors(raw: Any) -> list[str]:
    """Every probe problem in a whole plan input at once (its ``done`` and each step's ``expect``), in the validator's
    own words. The streamed validator stops at the first bad field, so when a plan comes back invalid the planner is
    also told the rest of its probe problems in the same report (one retry fixes them all). Never raises."""
    out: list[str] = []
    if not isinstance(raw, Mapping):
        return out
    try:
        done = raw.get("done")
        consent = raw.get("consent") if isinstance(raw.get("consent"), Mapping) else {}
        route = raw.get("route") if isinstance(raw.get("route"), str) else ""
        # x10: a done that code decides (an answer, a skill route, a send, a join) or a hand-off's own check written
        # out is never used as written (parse_header), so it is no problem to report
        code_done = (raw.get("kind") in ("answer", "ask") or route.strip().startswith(SKILL_ROUTE_PREFIX)
                     or consent.get("commit") in ("send", "join")
                     or (isinstance(done, Mapping) and done.get("probe") in ENGINE_ONLY_PROBES))
        if done is not None and not code_done:
            _probe(done, out, "done")
        steps = raw.get("steps")
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            if isinstance(step, Mapping) and step.get("expect") is not None:
                _probe(step.get("expect"), out, f"step {index + 1}: expect")
    except Exception:  # noqa: BLE001 - a hint only: the validator's own errors stand
        pass
    return out


def required_probe(plan: Plan) -> str | None:
    """The probe class the plan ends on (design 2.4): ``none`` (an answer or a question), ``skill`` (a skill route: the
    skill's own check), ``message_sent`` (a send), ``mic_held`` (a join), or ``None`` (the planner's pick). A seeded
    plan by steps is the planner's pick too (x10): its own probe decides, and ``none`` takes the seed's fixed check
    (:func:`_check_done`)."""
    if plan.kind in ("answer", "ask"):
        return "none"
    if plan.skill:
        return "skill"
    if plan.consent.commit == "send":
        return "message_sent"
    if plan.consent.commit == "join":
        return "mic_held"
    return None


def parse_header(raw: Mapping[str, Any], checks: Checks) -> Plan:
    """The plan's fields before ``steps``, validated (design 2.2); raises :class:`PlanError`. The returned plan has no
    steps; its done probe is normalised where code decides the check (:func:`_check_done`): a skill route, a send, a
    join, an answer or a question, and a seeded plan that wrote ``none`` (the seed's check). A route to a skill that
    just handed over or couldn't confirm in this request is refused (:func:`spent_skills`)."""
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise PlanError(["the plan must be a JSON object"])
    goal = _opt_str(raw, "goal", errors, "", GOAL_CHARS) or ""
    if not goal:
        errors.append("goal is required: one line in the user's terms")
    kind = raw.get("kind")
    if kind not in KINDS:
        errors.append(f"kind must be one of: {', '.join(KINDS)}")
        kind = "act"
    revises = _opt_str(raw, "revises", errors, "", 40)
    if checks.current_plan:
        revises = checks.current_plan   # code's own id: a replan always revises the plan that ran
    t = raw.get("target") if isinstance(raw.get("target"), Mapping) else None
    if t is None:
        errors.append("target must be an object with app, window, person and item")
        target = Target()
    else:
        target = Target(*(_opt_str(t, k, errors, "target.", TARGET_CHARS) for k in ("app", "window", "person", "item")))
    route = raw.get("route")
    route = route.strip() if isinstance(route, str) else ""
    if route[:6].casefold() == "route ":
        route = route[6:].strip()   # the field's own name written in its value ("route skill:message", 16 Oct B31)
    skill = route[len(SKILL_ROUTE_PREFIX):].strip() if route.startswith(SKILL_ROUTE_PREFIX) else None
    if skill is None and route != STEPS_ROUTE and checks.skills and route in checks.skills:
        # sBrainFix (review r1 runs): a route written as a menu id itself, without "skill:" (16 Oct B28 runs: an
        # invalid plan and a model call each time), is that skill's route: an exact catalog id, never a guess
        skill = route
    if skill is None and route != STEPS_ROUTE and kind in ("ask", "answer"):
        # a question or an answer runs no skill, so its route is steps whatever else was written ("none", "ask":
        # 16 Oct B08 runs, an invalid plan and a model call each time)
        route = STEPS_ROUTE
    if route != STEPS_ROUTE and skill is None:
        errors.append("route must be steps or skill:<id> from the menu")
    elif skill is not None:
        if checks.skills is None:
            errors.append("the skill list could not be read: use route steps")
        elif skill not in checks.skills:
            errors.append(f"no skill {skill!r} in the menu: use one listed there, or route steps")
        else:
            spent = _spent_error("route: ", skill, checks)
            if spent:
                errors.append(spent)
        route = SKILL_ROUTE_PREFIX + skill
        if kind != "act":
            errors.append("a skill route runs the skill, so it is kind act (an answer or a question routes steps)")
    c = raw.get("consent") if isinstance(raw.get("consent"), Mapping) else None
    if c is None:
        errors.append("consent must be an object with commit, quote, to and text")
        consent = Consent()
    else:
        commit = c.get("commit")
        if commit not in COMMITS:
            errors.append(f"consent.commit must be one of: {', '.join(COMMITS)}")
            commit = "none"
        consent = Consent(str(commit), _opt_str(c, "quote", errors, "consent.", QUOTE_CHARS, cut=False),
                          _opt_str(c, "to", errors, "consent.", TARGET_CHARS, cut=False),
                          _opt_str(c, "text", errors, "consent.", VALUE_CHARS, cut=False))
        if consent.commit == "send" and not (consent.quote and consent.to and consent.text):
            errors.append("a send needs consent.quote (the user's words asking for it), consent.to and consent.text")
        if consent.commit == "join" and not (consent.quote and consent.to):
            errors.append("a join needs consent.quote (the user's words asking for it) and consent.to")
        if consent.commit == "send" and consent.quote and consent.to and consent.text:
            bounce = send_text_error(consent, checks)
            if bounce:
                errors.append(bounce)
        if consent.commit != "none" and kind != "act":
            errors.append("only an act plan commits anything: consent.commit none for an answer or a question")
    raw_done = raw.get("done")
    written = raw_done.get("probe") if isinstance(raw_done, Mapping) else None
    if kind in ("answer", "ask") or skill is not None or consent.commit in ("send", "join"):
        # code decides this plan's check (nothing to check, the skill's own, the send's or the join's): whatever the
        # model wrote there is never used, so it is never a reason to refuse the plan (x10)
        done = Probe()
    elif checks.seed is not None and written in ENGINE_ONLY_PROBES:
        # the hand-off's own check written out (its arguments are the engine's, not a plan's): the seed's check
        done = Probe(name=str(written))
    else:
        done = _probe(raw_done, errors, "done") or Probe()
    question = _opt_str(raw, "question", errors, "", QUESTION_CHARS)
    if kind == "ask" and not question:
        errors.append("kind ask needs the question")
    if errors:
        raise PlanError(errors)
    plan = Plan(id=checks.plan_id, goal=goal, kind=str(kind), revises=revises, target=target, route=route,
                consent=consent, done=done, question=question, seeded=checks.seed is not None)
    return _check_done(plan, checks)


def _seed_probe(seed: Mapping[str, Any] | None) -> Probe | None:
    raw = (seed or {}).get("done")
    if not isinstance(raw, Mapping):
        return None
    name = str(raw.get("probe") or "")
    if name not in PROBES:
        return None
    args = raw.get("args")
    if args is None:
        # bSkill finding 3: the engine writes its check flat (``connection.Check.as_dict``: the probe, its arguments,
        # ``within_s``), so the arguments are its other keys
        args = {k: v for k, v in raw.items() if k not in _SEED_CHECK_KEYS}
    args = _json_object(args) if not isinstance(args, Mapping) else dict(args)
    return Probe(name=name, args=args or {}, raw=json.dumps(args or {}, ensure_ascii=False, sort_keys=True))


#: The keys of the engine's flat done-check that are not the probe's arguments (``connection.Check``).
_SEED_CHECK_KEYS = frozenset({"probe", "within_s", "after_s", "parts", "same_source", "live"})


def _check_done(plan: Plan, checks: Checks) -> Plan:
    """The done probe against the required table (design 2.4, M3), normalised wherever code decides the check (x10:
    two refusals of a seeded click plan for its done field ended tonight's requests, 10 Oct):

    * an answer or a question: nothing to check (``none``);
    * a skill route: the skill checks itself (``done_by_skill``), whatever was written;
    * a send: ``message_sent`` of ``consent.text``; a join: ``mic_held`` of the target app;
    * a seeded plan by steps: its own probe from the closed list decides it; ``none`` (or the hand-off's own probe
      written out) takes the seed's fixed check (``seed_check``), except a send's or a join's check in a plan that
      declares neither, a sending skill's own check among them (that would read the user's own doing, as
      :func:`yuki.brain.done.pick_done` refuses it): ``none``;
    * any other act plan: the planner's pick.

    Raises :class:`PlanError` only for a probe a plan may not pick: one only a skill reads, or a send's or a join's
    check without that commit."""
    need = required_probe(plan)
    done = plan.done
    if need == "none":
        return replace(plan, done=Probe()) if not done.none else plan
    if need == "skill":
        return replace(plan, done=Probe(), done_by_skill=True)
    if need == "message_sent":
        args = {"text": plan.consent.text or ""}
        if plan.target.app:
            args["app"] = plan.target.app
        return replace(plan, done=Probe("message_sent", args, json.dumps(args, ensure_ascii=False)))
    if need == "mic_held":
        args = {"process": plan.target.app} if plan.target.app else {}
        return replace(plan, done=Probe("mic_held", args, json.dumps(args, ensure_ascii=False)))
    seed = _seed_probe(checks.seed) if plan.seeded else None
    if seed is not None and (done.none or done.name in ENGINE_ONLY_PROBES or done.same(seed)):
        if _commit_probe(seed) or seed_sends(checks.seed):
            # the hand-off's check reads a send or a join this plan doesn't declare: never its check (M3; review r2
            # L3: a message skill's own check reads the send, so a draft typed by steps that the user then sends
            # themselves would be said as Yuki's "Done.")
            return replace(plan, done=Probe())
        return replace(plan, done=seed, seed_check=True)
    # the planner's pick, from the closed list, never one only a skill or a declared commit can use
    if done.name in ENGINE_ONLY_PROBES:
        raise PlanError([f"done.probe {done.name} is only for skills: pick one that reads the screen or the PC"])
    if done.name == "message_sent":
        raise PlanError(["message_sent checks a send: declare consent.commit send, or pick another probe"])
    if done.name == "mic_held":
        raise PlanError(["mic_held checks a join: declare consent.commit join, or pick another probe"])
    return plan


def _commit_probe(probe: Probe) -> bool:
    """A probe that reads a send or a join (``message_sent``, ``mic_held``, a send's or a join's moment)."""
    if probe.name in ("message_sent", "mic_held"):
        return True
    return probe.name == "moment" and str(probe.args.get("kind") or "") in _done.COMMIT_MOMENTS


def seed_sends(seed: Mapping[str, Any] | None) -> bool:
    """The hand-off is a skill that sends (the engine's ``sends`` mark, ``engine.pinned``), or its check is a capability
    driver's send check (``send: send``, the message capability's ``chat.message_done``): that check reads a send, so
    a plan by steps that declares none never takes it (review r2 L3)."""
    if not isinstance(seed, Mapping):
        return False
    if seed.get("sends"):
        return True
    probe = _seed_probe(seed)
    return probe is not None and probe.name == "driver" and str(probe.args.get("send") or "") == "send"


def _two_ints(text: str) -> bool:
    """``text`` is a pair of integers (``"512, 300"``, ``"512 300"``): a screen coordinate, a fixed format."""
    parts = [p for p in text.replace(",", " ").split() if p]
    if len(parts) != 2:
        return False
    try:
        int(parts[0])
        int(parts[1])
    except ValueError:
        return False
    return True


def _number(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_step(raw: Mapping[str, Any], index: int, header: Plan, checks: Checks,
               prior: Sequence[Step] = ()) -> Step:
    """One step object, validated against the plan's header and the steps before it; raises :class:`PlanError`."""
    errors: list[str] = []
    where = f"step {index + 1}: "
    if not isinstance(raw, Mapping):
        raise PlanError([f"{where}must be an object"])
    verb = raw.get("verb")
    if verb not in VERBS:
        raise PlanError([f"{where}verb must be one of: {', '.join(VERBS)}"])
    window = _opt_str(raw, "window", errors, where, TARGET_CHARS)
    element = None
    e = raw.get("element")
    if e is not None:
        if not isinstance(e, Mapping) or not isinstance(e.get("name"), str):
            errors.append(f"{where}element must be null or an object with name and role")
        else:
            name = " ".join(unescape(str(e.get("name") or "")).split())[:NAME_CHARS]
            role = " ".join(unescape(str(e.get("role") or "")).split())[:ROLE_CHARS]
            if not name:
                errors.append(f"{where}element.name must name the element (or [id] from a look)")
            elif _two_ints(name):
                errors.append(f"{where}no coordinates: name the element by its name and role, or [id] from a look")
            element = Element(name, role) if name else None
    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        errors.append(f"{where}value must be a string or null")
        value = None
    if isinstance(value, str) and verb not in RAW_VALUE_VERBS:
        value = _unescape_json_text(value) if verb in ("run_skill", "memory") else unescape(value)
    if verb == "look":
        # sBrainFix (review r1 runs): a look written without what to read, or without the window a window read needs
        # (16 Oct, about a third of the web runs: look_value_must_be_one_of / look_page_needs_window, an invalid plan
        # after its earlier steps already ran), reads its window (else the desktop): a read either way, so the default
        # can do nothing the plan didn't ask for
        own = " ".join((header.target.window or "").split())
        # pBrain (17 Oct e2e2 B14 r0: [ctrl+t in "Arc.exe", open_url in "Arc.exe", look page] with no window on the
        # look and a title as the target's window: look_page_needs_window, and the replan swapped its done for a
        # first-part probe): a look with no window reads the window the plan's last step before it named, else the
        # target's window (an hwnd or a title either way, resolved as a step's window is: done.window_of, else the
        # window in front). A read of a window the plan itself names, so it can do nothing the plan didn't ask for.
        before = next((p.window for p in reversed(prior) if (p.window or "").strip()), None)
        if not " ".join(str(value or "").split()):
            value = "window" if window or before or own else "desktop"
        if not window and str(value).strip() in LOOK_NEEDS_WINDOW:
            window = before or own or None
    if isinstance(value, str) and len(value) > VALUE_CHARS:
        errors.append(f"{where}value is longer than {VALUE_CHARS} characters")
    submit = raw.get("submit")
    if not isinstance(submit, bool):
        errors.append(f"{where}submit must be true or false")
        submit = False
    expect = None
    if raw.get("expect") is not None:
        expect = _probe(raw.get("expect"), errors, f"{where}expect")
        if expect is not None and expect.name in ENGINE_ONLY_PROBES:
            errors.append(f"{where}expect.probe {expect.name} is only for skills")
    say = _opt_str(raw, "say", errors, where, SAY_CHARS)
    if checks.channel != "voice":
        say = None
    if errors:
        raise PlanError(errors)
    step = Step(index=index, verb=str(verb), window=window, element=element, value=value, submit=submit,
                expect=expect, say=say)
    if step.say:
        # sBrainFix (F7): the line is heard before the step runs (the voice's step line as the step closes in the
        # stream, the executor's as it starts), so its words are code's for that step, never the model's: on 10 and
        # 16 Oct the model wrote "Playing your Japanese playlist now." and "Sent it to Hemanth." there, whatever the
        # prompt said. The model still decides which steps are narrated (it wrote a say).
        step = replace(step, say=step_line(step, header))
    _check_verb(step, header, checks, prior, where)
    if checks.failed_step is not None and not checks.repeat_rejected and step.same_as(checks.failed_step):
        checks.repeat_rejected = True
        raise PlanError([f"{where}that step just failed: change it"])
    return step


#: sBrainFix (review r1 M2): the window and tab actions whose step is said by no line: a close may still be asked
#: about first (consent's lasting row), so "Closing Arc." before that question would claim what may not happen.
UNSAID_ACTIONS: frozenset[str] = frozenset({"close"})
#: sBrainFix (review r1 L5): a run_skill step's line when its skill is a capability (shipped, never learned: a key
#: without the learned ``#`` part, :func:`skill_family`); a learned skill's is the island's "Doing it the way I
#: learned".
LINE_CAPABILITY_STEP = "Working on it."


def step_line(step: Step, header: Plan) -> str | None:
    """What Yuki says as a step starts (sBrainFix, F7): the step in the island's words (``yuki.ui.step_words``:
    built from the verb, the control's role and name, the plan's target app and the verb's closed values only, never
    the step's free text), as a sentence ("Clicking Play ネオン."): what starts, never that it is done or happening
    to work. ``None`` (nothing said) when no words come.

    The line is heard before the step runs and before its consent check (review r1 M2), so it never says more than
    what starts: typing is said without its Enter ("Typing in the Message box.", never "Typing and sending a
    message.": Enter in an address bar sends nothing, and a send the check refuses never went); a close
    (:data:`UNSAID_ACTIONS`) is said by no line; a capability's run_skill step is :data:`LINE_CAPABILITY_STEP`."""
    if step.verb in ("window_action", "tab") and " ".join(str(step.value or "").split()).casefold() in UNSAID_ACTIONS:
        return None
    if step.verb == "run_skill":
        call = step.skill_call(header.skill)
        if call is None or skill_family(call[0]) == call[0]:
            return LINE_CAPABILITY_STEP
    try:
        from yuki.ui.step_words import describe_plan_step

        said = replace(step, submit=False) if step.submit else step
        words = " ".join(str(describe_plan_step(said, app=header.target.app) or "").split()).rstrip(". ")
    except Exception:  # noqa: BLE001 - presentation only: say nothing rather than the model's words
        return None
    return f"{words}."[:SAY_CHARS] if words else None


def _check_verb(step: Step, header: Plan, checks: Checks, prior: Sequence[Step], where: str) -> None:
    """The per-verb field rules (:data:`VERB_FIELDS`), the plan-kind rule, and the raw-send rule; raises."""
    verb, value = step.verb, step.value
    errors: list[str] = []
    if header.kind != "act" and verb in ACT_VERBS:
        raise PlanError([f"{where}an {header.kind} plan only reads (look, memory, fetch, wait): no {verb}"])
    if step.index >= MAX_STEPS:
        raise PlanError([f"{where}a plan holds at most {MAX_STEPS} steps: plan the first part, then look"])
    text = (value or "").strip()
    if verb in ("launch_app", "open_url", "hotkey", "press", "run_powershell", "fetch", "open_yuki") and not text:
        errors.append(f"{where}{verb} needs value ({VERB_FIELDS[verb]})")
    if verb in ("type_text", "write_clipboard") and value is None:
        errors.append(f"{where}{verb} needs value ({VERB_FIELDS[verb]})")
    if verb in ("focus_window", "window_action", "tab") and not step.window:
        errors.append(f"{where}{verb} needs window ({VERB_FIELDS[verb]})")
    if verb in ("click", "set_slider", "tab") and step.element is None:
        errors.append(f"{where}{verb} needs element ({VERB_FIELDS[verb]})")
    if verb == "window_action" and text not in WINDOW_ACTIONS:
        errors.append(f"{where}window_action value must be one of: {', '.join(WINDOW_ACTIONS)}")
    if verb == "tab" and text not in TAB_ACTIONS:
        errors.append(f"{where}tab value must be select or close")
    if verb == "click" and value is not None and text not in CLICK_BUTTONS:
        errors.append(f"{where}click value must be null, right, middle or double (no coordinates)")
    if verb == "scroll":
        n = _number(text)
        if n is None or n != int(n) or n == 0:
            errors.append(f"{where}scroll value must be whole wheel notches, positive up")
    if verb == "set_slider":
        n = _number(text)
        if n is None or not 0 <= n <= 100:
            errors.append(f"{where}set_slider value must be a level from 0 to 100")
    if verb == "app_volume" and text not in ("mute", "unmute") and _number(text.lstrip("+")) is None:
        errors.append(f"{where}app_volume value must be a level, +N / -N, mute or unmute")
    if verb == "wait":
        n = _number(text)
        if n is None or not 0 < n <= WAIT_MAX_S:
            errors.append(f"{where}wait value must be seconds, more than 0 and at most {WAIT_MAX_S:g}")
    if verb == "look":
        if text not in LOOK_WHATS:
            errors.append(f"{where}look value must be one of: {', '.join(LOOK_WHATS)}")
        elif text in LOOK_NEEDS_WINDOW and not step.window:
            errors.append(f"{where}look {text} needs window")
    if verb == "memory":
        data = _json_object(value)
        if data is None or data.get("kind") not in MEMORY_KINDS:
            errors.append(f"{where}memory value must be a JSON object with kind one of: {', '.join(MEMORY_KINDS)}")
    if verb == "run_skill":
        data = _json_object(value) if value else {}
        call = step.skill_call(header.skill)
        if data is None or call is None:
            errors.append(f"{where}run_skill value must be JSON {{\"skill\": id, \"slots\": {{...}}}}")
        elif checks.skills is None:
            errors.append(f"{where}the skill list could not be read: do it by steps")
        elif call[0] not in checks.skills:
            errors.append(f"{where}no skill {call[0]!r} in the menu")
        elif data is not None and "slots" in data and not isinstance(data.get("slots"), dict):
            errors.append(f"{where}run_skill slots must be an object")
        else:
            # checked as the step closes, before it can run: a skill that just handed over (or couldn't confirm) is
            # never run again in the same request (its own way would only repeat; a second try may act twice)
            spent = _spent_error(where, call[0], checks)
            if spent:
                errors.append(spent)
    if errors:
        raise PlanError(errors)
    # sends (design 2.2, 2.5 row 4; decision 2): a send runs only through a sending capability, declared
    sending = False
    call = step.skill_call(header.skill)
    if call is not None and checks.sends is not None:
        try:
            sending = bool(checks.sends(call[0], call[1]))
        except Exception:   # noqa: BLE001 - the catalog could not say: treated as sending (fail closed)
            sending = True
    if sending and header.consent.commit != "send":
        raise PlanError([f"{where}this capability sends with these slots: declare consent.commit send (with quote, "
                         f"to and text), or set its slots so it leaves a draft"])
    if header.consent.commit == "send":
        raw_send = (verb == "type_text" and (step.submit or "\r" in (value or "") or "\n" in (value or "")))
        # a window left out (or named one way here and another there) counts as the same window: fail closed
        typed_before = any(p.verb == "type_text" and (not p.window or not step.window or p.window == step.window)
                           for p in prior)
        if verb in ("click", "press", "hotkey") and typed_before:
            raw_send = True
        if raw_send:
            raise PlanError([f"{where}send through the message capability (run_skill), never by typing and "
                             f"pressing send; with no capability for that app, leave a draft and tell the user"])


#: bRound2 (F1c): a send whose text keeps too few of the words the user said for it goes back to the planner once.
LINE_SEND_TEXT = ("consent.text: write the message in the words they said for it, turned to that person (him or her "
                  "becomes you), never reworded, restyled or translated (the text slot the same); if they asked for "
                  "other words or another language, send this plan again unchanged")


def send_text_error(consent: Consent, checks: Checks) -> str | None:
    """bRound2 (F1c, 16 Oct B31: "...ask him, like, when he will come and join the VC." planned as "Bhai kab aa rha VC
    join karne?" in 3 of 3 sBrainFix runs and sent so after the user's yes on main; B35 "...ask him when he will come"
    planned as "bhai kab aayega VC join karne?", which keeps none of their words, so row 4 left a draft of a send
    they plainly asked for): a send's ``consent.text`` that keeps too few of the words the user said for it
    (``consent.said_share`` below ``consent.SAID_SHARE`` in every one of their turns of this request, the recipient's
    names aside; none at all included) is sent back to the planner once in a request (:data:`LINE_SEND_TEXT`). A second
    plan, the same or not, is accepted, and row 4 decides as before (asked with the exact text, or a draft when none
    of it is theirs). A count of words in whatever language they spoke, never a reading of them; ``None`` when there
    is nothing to measure."""
    if (checks.text_bounced and not checks.bounced_now) or not checks.words:
        return None
    from yuki.brain import consent as rules

    # measured in every turn of theirs in this request, each on its own (row 4 then measures in the quote's turns and
    # sends a quote of a turn that says nothing of the message, "Yeah.", back to quote the one that does)
    turns = [str(w) for w in checks.words if str(w).strip()]
    share = rules.said_share(str(consent.text or ""), turns, [str(consent.to or "")])
    if share < rules.SAID_SHARE:
        checks.text_bounced = checks.bounced_now = True   # this call's plan read again bounces the same
        return LINE_SEND_TEXT
    return None


def validate_plan(raw: Mapping[str, Any], checks: Checks) -> Plan:
    """The whole plan, validated (header, each step, and the rules over all steps); raises :class:`PlanError` with
    every error found."""
    header = parse_header(raw, checks)
    items = raw.get("steps")
    if not isinstance(items, list):
        raise PlanError(["steps must be a list (empty for a question)"])
    steps: list[Step] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        try:
            steps.append(parse_step(item, i, header, checks, steps))
        except PlanError as exc:
            errors.extend(exc.errors)
    if errors:
        raise PlanError(errors)
    return finish_plan(header, steps, checks)


def finish_plan(header: Plan, steps: Sequence[Step], checks: Checks) -> Plan:
    """The rules that need every step (design 2.2): a skill route gets its ``run_skill`` step when it wrote none; a
    send holds a sending capability step; an act plan does something. Raises :class:`PlanError`."""
    steps = list(steps)
    if header.skill and not any(s.verb == "run_skill" for s in steps):
        if steps:
            raise PlanError([f"route {header.route} runs that skill: give its run_skill step, or route steps"])
        steps = [synth_skill_step(header)]
    if header.acts and not steps:
        raise PlanError(["an act plan needs at least one step"])
    if header.consent.commit == "send":
        sending = []
        for s in steps:
            call = s.skill_call(header.skill)
            if call is None or checks.sends is None:
                continue
            try:
                if checks.sends(call[0], call[1]):
                    sending.append(s)
            except Exception:  # noqa: BLE001
                sending.append(s)
        if not sending:
            raise PlanError(["a send runs through the message capability: a run_skill step of a skill that sends; "
                             "with none for that app, leave a draft (consent.commit none) and tell the user"])
    return header.with_steps(steps)


def synth_skill_step(header: Plan) -> Step:
    """The one ``run_skill`` step a ``skill:<id>`` route runs when the model wrote no step."""
    return Step(index=0, verb="run_skill", value=json.dumps({"skill": header.skill, "slots": {}}))


# ---------------------------------------------------------------------------------------------------------------------
# The executor seam
# ---------------------------------------------------------------------------------------------------------------------


class ExecHost(Protocol):
    request_id: str; lane: str; channel: str            # "voice" | "typed"
    user_words: list[str]                                # runtime/bridge provenance only
    memory: Any; dispatcher: Any; settings: Any
    def acting_begin(self) -> None: ...                  # publish or refresh (MemoryAccess.acting_begin) + takeover watch on
    def acting_end(self) -> None: ...                    # withdraw + watch off; idempotent
    def cancelled(self) -> bool: ...
    def paused(self) -> bool: ...
    def steer_pending(self) -> list[str] | None: ...     # the user's exact words, if any
    def emit(self, event: AgentEvent) -> None: ...
    def ask(self, question: str) -> bool: ...            # AskUser round trip (card / voice)


class StepSource(Protocol):
    """What the Brain hands the executor for one plan (bCore's side of the seam; ``yuki.brain.planner.PlanStream``).

    ``steps()`` yields each validated step the moment its object closes in the model's stream (the stream is read
    only while the executor asks for the next step); it ends at the plan's end or at a stop. ``header()`` is the plan
    without steps once its fields before ``steps`` are validated (``None`` until then). ``complete()`` reads the rest
    of the stream and returns the whole validated plan (``None``: invalid or stopped; ``errors`` / ``stop`` say why);
    after it, ``steps()`` still yields the steps not yet taken. ``stop``: ``max_tokens`` | ``refusal`` | ``invalid`` |
    ``cancelled`` | ``None``: after a stop no further step is yielded (the steps already run committed nothing: only
    non-committing steps run before the plan is complete)."""

    plan_id: str
    stop: str | None
    errors: list[str]

    def header(self) -> Plan | None: ...
    def steps(self) -> Iterator[Step]: ...
    def complete(self) -> Plan | None: ...


#: Step states in an :class:`ExecResult`.
STEP_STATES: tuple[str, ...] = ("ok", "failed", "mismatch", "refused", "drafted", "asked", "skipped", "not_run")
#: Result statuses (what the Brain does next): ``done`` (the done-check saw it), ``unconfirmed`` (it could not),
#: ``mismatch`` / ``refused`` (replan), ``steered`` (replan with the user's words), ``declined`` (the user said no:
#: end, said plainly), ``drafted`` (left as a draft: end), ``read`` (answer plan: the reads go back to the model),
#: ``stopped`` (cancelled or ended paused), ``invalid`` (the plan failed validation), ``error``.
RESULT_STATUSES: tuple[str, ...] = ("done", "unconfirmed", "mismatch", "refused", "steered", "declined", "drafted",
                                    "read", "stopped", "invalid", "error")


@dataclass
class StepStatus:
    index: int
    verb: str
    state: str = "not_run"
    #: What showed or why, in plain words or code words (no window text beyond the executor's capped diff).
    note: str = ""


@dataclass
class ExecResult:
    """What the executor reports for one plan (bCore's reading of design 2.3; see the module docstring)."""

    status: str
    statuses: list[StepStatus] = field(default_factory=list)
    step: int | None = None
    expected: str = ""
    seen: str = ""
    view: str = ""
    reads: list[str] = field(default_factory=list)
    line: str = ""
    outcome: dict[str, Any] | None = None
    acted: bool = False
    verdict: bool | None = None
    probe: str = ""
    reason: str = ""
    check_calls: int = 0
    #: The run ends here with ``line`` (a refusal such as a game in front, the desk busy): no replan.
    final: bool = False
    #: A skill's hand-off (rung C) from a ``run_skill`` step: the next plan is seeded with it. Its done-check decides
    #: that plan only when the plan writes none (or the check itself) or re-runs the skill; a plan by steps with a
    #: probe of its own is decided by that probe (x10).
    seed: dict[str, Any] | None = None
    #: ``seen`` is outside text the executor capped and fenced as untrusted data: it is never cut again.
    seen_fenced: bool = False
    #: An ``unconfirmed`` plan whose deciding ``run_skill`` step acted and couldn't confirm, when doing the rest by
    #: steps is safe (it sends and joins nothing, and its route may be followed after acting): that skill's id. The
    #: Brain then replans once by steps after a look instead of ending (x10); ``None`` otherwise.
    unconfirmed_skill: str | None = None


__all__ = [
    "send_text_error", "LINE_SEND_TEXT",
    "ACT_VERBS", "READ_VERBS", "VERBS", "KINDS", "COMMITS", "SKILL_PROBES", "GENERIC_PROBES", "PROBES", "PROBE_ARGS",
    "ENGINE_ONLY_PROBES", "LOOK_WHATS", "WINDOW_ACTIONS", "TAB_ACTIONS", "MEMORY_KINDS", "VERB_FIELDS", "PLAN_TOOL",
    "STEPS_ROUTE", "SKILL_ROUTE_PREFIX", "MAX_STEPS", "plan_schema", "Target", "Consent", "Probe", "Element", "Step",
    "Plan", "PlanError", "Checks", "required_probe", "parse_header", "parse_step", "validate_plan", "finish_plan",
    "synth_skill_step", "ExecHost", "StepSource", "StepStatus", "ExecResult", "STEP_STATES", "RESULT_STATUSES",
    "probe_args_text", "probe_errors", "error_code", "error_codes", "SEED_RERUN_OK", "skill_family", "seed_skill",
    "spent_skills", "unescape", "seed_sends", "step_line", "UNSAID_ACTIONS", "LINE_CAPABILITY_STEP",
]
