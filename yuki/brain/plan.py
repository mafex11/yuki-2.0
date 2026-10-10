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
  :class:`PlanError` whose plain-words ``errors`` go back to the model as an ``is_error`` tool result (one retry).
* :class:`ExecHost` is the protocol the executor runs against, verbatim as pinned in design 10.1.
* :class:`StepSource` and :class:`ExecResult` are bCore's side of the executor seam (what the Brain hands the executor
  and what it reads back). They are not part of the pinned text: the merge reviewer reconciles them with bExec's
  executor in ``yuki/brain/agent.py`` (``_run_executor``), the one place that calls it.

Nothing here reads the user's words for meaning: verbs, probes, commit classes and routes are closed vocabularies, and
the checks are on types, sizes, string identity and the catalog's own data (``sends_with``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

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

#: The skill engine's probes (``yuki.skills.connection.PROBES`` minus ``all``, read through ``donecheck``).
SKILL_PROBES: tuple[str, ...] = (
    "read_back", "uia_name", "media_session", "audio_active", "level", "page_at", "tab_shown", "foreground",
    "mic_held", "chat_open", "message_sent", "moment", "driver",
)
#: The four generic UIA probes ``yuki/brain/done.py`` adds (bExec).
GENERIC_PROBES: tuple[str, ...] = ("field_holds", "window_state", "gone", "file_exists")
#: The closed probe list of ``done.probe`` and ``expect.probe``; ``none`` is allowed and is never "done".
PROBES: tuple[str, ...] = SKILL_PROBES + GENERIC_PROBES + ("none",)

#: Each probe's argument names (what ``done.args`` / ``expect.args`` may hold). The skill probes' names are the keys
#: ``yuki.skills.donecheck`` reads; the generic ones are this file's contract with ``done.py``.
PROBE_ARGS: dict[str, tuple[str, ...]] = {
    "read_back": (),
    "uia_name": ("process", "window", "role", "name", "container"),
    "media_session": ("app", "status", "changed"),
    "audio_active": ("process", "window"),
    "level": ("value", "target", "tolerance"),
    "page_at": ("site", "kind", "process", "window"),
    "tab_shown": ("process", "window", "tab_id", "name"),
    "foreground": ("process", "window"),
    "mic_held": ("process", "window"),
    "chat_open": ("app", "person"),
    "message_sent": ("text", "app", "person"),
    "moment": ("kind", "app"),
    "driver": ("driver", "call"),
    "field_holds": ("window", "name", "role", "text"),
    "window_state": ("window", "state"),
    "gone": ("window", "name", "role"),
    "file_exists": ("path",),
    "none": (),
}
#: Probes only the skill engine can read (a route's own read-back, a driver module's probe): never a plan's pick.
ENGINE_ONLY_PROBES: frozenset[str] = frozenset({"read_back", "driver"})

#: What ``look`` reads (a step's value, the ``look`` tool's ``what``) and the windows it needs.
LOOK_WHATS: tuple[str, ...] = ("desktop", "window", "page", "tabs", "focus", "clipboard", "system")
LOOK_NEEDS_WINDOW: frozenset[str] = frozenset({"window", "page", "tabs"})
WINDOW_ACTIONS: tuple[str, ...] = ("minimize", "maximize", "fullscreen", "restore", "focus", "close")
TAB_ACTIONS: tuple[str, ...] = ("select", "close")
CLICK_BUTTONS: tuple[str, ...] = ("left", "right", "middle", "double")
MEMORY_KINDS: tuple[str, ...] = (
    "recall", "activity", "calls", "call_timeline", "threads", "before", "links", "people", "skills",
)

#: How each verb's fields are read (the executor's contract; the tool description says it in short).
VERB_FIELDS: dict[str, str] = {
    "launch_app": "value: app name",
    "focus_window": "window",
    "open_url": "value: address, file or folder",
    "window_action": "window; value: minimize|maximize|fullscreen|restore|focus|close",
    "tab": "window; element: the tab; value: select|close",
    "click": "element; value: null|right|middle|double",
    "type_text": "element or the focus; value: text; submit: Enter after",
    "hotkey": "value: chord, e.g. ctrl+t",
    "press": "value: one key",
    "scroll": "element or window; value: notches, + is up",
    "set_slider": "element; value: 0-100",
    "app_volume": "target.app; value: 0-100, +N, -N, mute, unmute",
    "run_powershell": "value: command",
    "write_clipboard": "value: text",
    "run_skill": "value: JSON {\"skill\": id, \"slots\": {}}",
    "open_yuki": "value: page",
    "look": "value: desktop|window|page|tabs|focus|clipboard|system; window",
    "memory": "value: JSON memory-tool arguments with kind",
    "fetch": "value: http(s) address or file path",
    "wait": "value: seconds, at most 10",
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
            "probe": {"type": "string", "enum": list(PROBES)},
            "args": {"type": "string", "description": "Its arguments: a JSON object as a string."},
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
            "text": _nullable("string", "The exact text: their own words."),
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
            "window": _nullable("string", "An hwnd, or a title."),
            "element": element,
            "value": _nullable("string"),
            "submit": {"type": "boolean"},
            "expect": {"anyOf": [_probe_schema(), {"type": "null"}]},
            "say": _nullable("string", "Voice: words for a slow step."),
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["goal", "kind", "revises", "target", "route", "consent", "done", "question", "steps"],
        "properties": {
            "goal": {"type": "string", "description": "One line, in the user's terms."},
            "kind": {"type": "string", "enum": list(KINDS)},
            "revises": _nullable("string", "The plan id it replaces."),
            "target": target,
            "route": {"type": "string", "description": "steps, or skill:<id> from the menu."},
            "consent": consent,
            "done": _probe_schema(),
            "question": _nullable("string", "kind ask: the one question."),
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
    """One validated plan. ``id`` is code's (``p1``, ``p2`` ...). ``seeded``: built from a skill's hand-off (its done
    check is fixed). ``done_by_skill``: a skill route checks itself (the engine's own done-check and line)."""

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
                "goal_chars": len(self.goal), "target": sorted(self.target.fields())}


class PlanError(Exception):
    """The plan (or one step of it) failed validation: ``errors`` go back to the model in plain words."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = [str(e) for e in errors if str(e)]
        super().__init__("; ".join(self.errors) or "invalid plan")


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
    """

    plan_id: str
    channel: str = "typed"
    seed: Mapping[str, Any] | None = None
    skills: frozenset[str] | None = None
    sends: Callable[[str, Mapping[str, Any]], bool] | None = None
    failed_step: Step | None = None
    repeat_rejected: bool = False
    current_plan: str | None = None


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
    value = value.strip()
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
    allowed = PROBE_ARGS[name]
    extra = sorted(k for k in args if k not in allowed)
    if extra:
        errors.append(f"{where}.args for {name} take only: {', '.join(allowed) or 'nothing'} (not {', '.join(extra)})")
        return None
    if name != "none":
        # one contract: the done-check reads the arguments exactly as this accepts them (required ones present)
        try:
            from yuki.brain.done import parse_args
        except Exception:  # noqa: BLE001 - the done-check not installed: the names above stand
            parse_args = None
        if parse_args is not None:
            parsed, why = parse_args(str(name), text)
            if parsed is None:
                errors.append(f"{where}.args: {why}")
                return None
    for key, value in args.items():
        ok = isinstance(value, (str, int, float, bool)) or value is None or (
            isinstance(value, list) and all(isinstance(v, str) for v in value))
        if not ok:
            errors.append(f"{where}.args.{key} must be a string, number, true/false or a list of strings")
            return None
    return Probe(name=str(name), args=args, raw=text)


def required_probe(plan: Plan) -> str | None:
    """The probe class the plan must end on (design 2.4): ``skill`` (the skill's own check), ``message_sent`` (a
    send), ``mic_held`` (a join), ``none`` (an answer or a question), or ``None`` (the planner's pick). A seed's fixed
    check applies to its act plans only: an answer or a question in a seeded request changes nothing to check."""
    if plan.kind in ("answer", "ask"):
        return "none"
    if plan.seeded or plan.skill:
        return "skill"
    if plan.consent.commit == "send":
        return "message_sent"
    if plan.consent.commit == "join":
        return "mic_held"
    return None


def parse_header(raw: Mapping[str, Any], checks: Checks) -> Plan:
    """The plan's fields before ``steps``, validated (design 2.2); raises :class:`PlanError`. The returned plan has no
    steps; its done probe is normalised to the required one when the model wrote ``none`` for a send, a join, a skill
    route or a seeded plan."""
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
    skill = route[len(SKILL_ROUTE_PREFIX):].strip() if route.startswith(SKILL_ROUTE_PREFIX) else None
    if route != STEPS_ROUTE and skill is None:
        errors.append("route must be steps or skill:<id> from the menu")
    elif skill is not None:
        if checks.skills is None:
            errors.append("the skill list could not be read: use route steps")
        elif skill not in checks.skills:
            errors.append(f"no skill {skill!r} in the menu: use one listed there, or route steps")
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
        if consent.commit != "none" and kind != "act":
            errors.append("only an act plan commits anything: consent.commit none for an answer or a question")
    done = _probe(raw.get("done"), errors, "done") or Probe()
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
    args = _json_object(args) if not isinstance(args, Mapping) else dict(args)
    return Probe(name=name, args=args or {}, raw=json.dumps(args or {}, ensure_ascii=False, sort_keys=True))


def _check_done(plan: Plan, checks: Checks) -> Plan:
    """The done probe against the required table (design 2.4, M3); raises :class:`PlanError`."""
    need = required_probe(plan)
    done = plan.done
    if need == "skill":
        seed = _seed_probe(checks.seed) if plan.seeded else None
        if seed is not None:
            if done.none:
                done = seed
            elif not done.same(seed):
                raise PlanError(["this plan comes from a skill's hand-off: its done check is fixed (write done as "
                                 "the seed gives it, or probe none)"])
            return replace(plan, done=done)
        if plan.skill:
            if not done.none:
                raise PlanError(["a skill route checks itself: done.probe none"])
            return replace(plan, done=Probe(), done_by_skill=True)
        # a hand-off that carried no done check of its own: the rules of the plan's own class below
        # (an act plan here: answers and questions never need "skill")
        need = {"send": "message_sent", "join": "mic_held"}.get(plan.consent.commit)
    if need == "none":
        if not done.none:
            raise PlanError(["an answer or a question changes nothing to check: done.probe none"])
        return plan
    if need == "message_sent":
        if done.none:
            args = {"text": plan.consent.text or ""}
            if plan.target.app:
                args["app"] = plan.target.app
            return replace(plan, done=Probe("message_sent", args, json.dumps(args, ensure_ascii=False)))
        if done.name != "message_sent":
            raise PlanError(["a send is checked by message_sent (the message seen in the chat): done.probe "
                             "message_sent"])
        if str(done.args.get("text") or "") != (plan.consent.text or ""):
            raise PlanError(["done.args.text must be consent.text exactly"])
        return plan
    if need == "mic_held":
        if done.none:
            args = {"process": plan.target.app} if plan.target.app else {}
            return replace(plan, done=Probe("mic_held", args, json.dumps(args, ensure_ascii=False)))
        if done.name != "mic_held":
            raise PlanError(["a join is checked by mic_held (the app took the microphone): done.probe mic_held"])
        return plan
    # other act plans: the planner's pick, from the closed list, never one only a skill or a declared commit can use
    if done.name in ENGINE_ONLY_PROBES:
        raise PlanError([f"done.probe {done.name} is only for skills: pick one that reads the screen or the PC"])
    if done.name == "message_sent":
        raise PlanError(["message_sent checks a send: declare consent.commit send, or pick another probe"])
    if done.name == "mic_held":
        raise PlanError(["mic_held checks a join: declare consent.commit join, or pick another probe"])
    return plan


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
            name = " ".join(str(e.get("name") or "").split())[:NAME_CHARS]
            role = " ".join(str(e.get("role") or "").split())[:ROLE_CHARS]
            if not name:
                errors.append(f"{where}element.name must name the element (or [id] from a look)")
            elif _two_ints(name):
                errors.append(f"{where}no coordinates: name the element by its name and role, or [id] from a look")
            element = Element(name, role) if name else None
    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        errors.append(f"{where}value must be a string or null")
        value = None
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
    _check_verb(step, header, checks, prior, where)
    if checks.failed_step is not None and not checks.repeat_rejected and step.same_as(checks.failed_step):
        checks.repeat_rejected = True
        raise PlanError([f"{where}that step just failed: change it"])
    return step


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
    #: A skill's hand-off (rung C) from a ``run_skill`` step: the next plan is seeded with it (its done-check fixed).
    seed: dict[str, Any] | None = None
    #: ``seen`` is outside text the executor capped and fenced as untrusted data: it is never cut again.
    seen_fenced: bool = False


__all__ = [
    "ACT_VERBS", "READ_VERBS", "VERBS", "KINDS", "COMMITS", "SKILL_PROBES", "GENERIC_PROBES", "PROBES", "PROBE_ARGS",
    "ENGINE_ONLY_PROBES", "LOOK_WHATS", "WINDOW_ACTIONS", "TAB_ACTIONS", "MEMORY_KINDS", "VERB_FIELDS", "PLAN_TOOL",
    "STEPS_ROUTE", "SKILL_ROUTE_PREFIX", "MAX_STEPS", "plan_schema", "Target", "Consent", "Probe", "Element", "Step",
    "Plan", "PlanError", "Checks", "required_probe", "parse_header", "parse_step", "validate_plan", "finish_plan",
    "synth_skill_step", "ExecHost", "StepSource", "StepStatus", "ExecResult", "STEP_STATES", "RESULT_STATUSES",
]
