"""Skill connection files: each skill as data (PLAN.md Phase S, decisions D0, D2, D3; map-skills S1.2).

One TOML per app connection in ``yuki/skills/connections/`` (bundled: ``packaging/yuki.spec`` collects
``**/*.toml``). A file declares skills: one parameterised way to reach one end state in one app, its typed slots, its
route ladder (rung A the app's own controls, links and Windows' media and volume controls; rung B a recorded UI
Automation path; rung C the full agent), the machine done-check each route ends on, a second re-check, and the line
said once the check saw it. Code knows only the closed lists below (like ``OFFER_DOS``): an unknown verb, probe, slot
type or end kind skips that skill and is reported, never guessed. App labels and URL/URI templates are data in
``yuki/connect/sites/*.toml`` and are referenced from here (``label = "spotify:controls.volume"``), never copied.

File shape (abbreviated)::

    [connection]
    app = "demo"                          # the app key (yuki.memory.store.app_key form)
    state_reader = "demo_route"           # optional: the driver module whose window_state(app) feeds game = "states"

    [skill."demo.lamp"]
    end = "volume_change"                 # D3: the moment kind its done-check proves (D1 vocabulary)
    kind = "change"                       # "open" (2 verified runs to be offered) or "change" (5, none failed: D14)
    menu = "set the demo lamp's level"    # the menu line (<= 140 chars with its slots)
    done_line = "Lamp at {level}%."
    learn_from = [["volume_change", "demo"]]
    routine_on = []
    handoff = true                        # rung C (the thinker) when every route failed; never with a game in front
    sends = false                         # true for a skill that sends to people (rule 8; never a second acting rung);
                                          # sends_when = { slot = "send", in = ["send"] } narrows it to some values

    [[skill."demo.lamp".slot]]
    name = "level"
    type = "number"                       # enum | number | points | bool | entity | text | person | app
    min = 0
    max = 100

    [[skill."demo.lamp".route]]
    rung = "A"
    variant = "in_app"
    via = "uia_invoke"                    # the closed VERBS list
    args = { pattern = "range", window = "Demo.exe", role = "Slider", name = "Lamp", value = "{level}" }
    game = "always"                       # never | always | states (with game_states): may it run with a game in front
    done = { probe = "read_back", within_s = 2 }   # required unless the verb reads its own result back (D19)
    recheck = { probe = "level", after_s = 3, same_source = true }   # missing: allowed, reported as a warning
    # retry_after_acting = false          # after this rung acted and its check said "not so", try nothing else

Driver modules (``via = "driver"``, ``probe = "driver"``, ``state_reader``) are found through the import system
(``importlib.util.find_spec``), so they load from the installed app's archive too. A driver's ``RouteResult.app``
names the app it acted on (D18).

Templates in strings: ``{slot}`` (a slot's value) and ``{slot.field}`` (a field of an entity slot's value). A fixed
machine format, parsed here without regular expressions; a brace that is not a template stays as it is.
Zero setup (D0): no field anywhere may carry or ask for an account, a key, a token or a sign-in.
"""

from __future__ import annotations

import hashlib
import importlib
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: The bundled connection files.
CONNECTIONS_DIR = Path(__file__).with_name("connections")

#: At most this many skills are loaded (decision D4 of the habits plan: skill libraries collapse as they grow). A new
#: skill enters only by replacing one.
MAX_SKILLS = 20
#: A menu line's size cap (talker menu, map-skills S3.1).
MENU_LINE_CHARS = 140


@dataclass(frozen=True)
class VerbInfo:
    """What a route verb does to the desktop. ``focus``: it brings a window forward or sends input (``None``: the
    route must declare it; a driver). ``reads_back``: the verb measures its own result (a level read back, the window
    in front, a selected tab, a checked recorded path), so a route may end on that read-back alone; every other verb
    names its done-check (D19: every route ends on a machine check)."""

    focus: bool | None
    doc: str
    reads_back: bool = False


#: The closed list of route verbs (PLAN.md D.2). A verb that takes focus or sends input is never run with a game in
#: front (D17); the engine re-reads the game check right before each such step.
VERBS: dict[str, VerbInfo] = {
    "uia_invoke": VerbInfo(False, "a control by role and name inside a container path, searched from the window root "
                                  "(no focus): Invoke, SelectionItem.Select, Value.SetValue or RangeValue.SetValue"),
    "media_command": VerbInfo(False, "the media session's own play/pause/next/previous/stop on one app's session"),
    "open_url": VerbInfo(True, "open an address in a named app (launch arguments, nothing typed)"),
    "open_uri": VerbInfo(True, "an app URI (spotify:, discord://, whatsapp://, slack://) through launch.open_url"),
    "focus": VerbInfo(True, "bring one app window forward", reads_back=True),
    "tab_select": VerbInfo(False, "select one tab of a window's strip by its identity (no keys, no pointer)",
                           reads_back=True),
    "uia_steps": VerbInfo(True, "a recorded UI Automation path, each step checked (rung B)", reads_back=True),
    "slider": VerbInfo(False, "a slider by RangeValue only (press, hover and hotkey are never passed)",
                       reads_back=True),
    "app_volume": VerbInfo(False, "one app's mixer level, read back (Core Audio)", reads_back=True),
    "endpoint_volume": VerbInfo(False, "the whole PC's level, read back (Core Audio)", reads_back=True),
    "agent": VerbInfo(True, "rung C: hand the request to the thinker with the skill pinned", reads_back=True),
    "driver": VerbInfo(None, "a route function of a module under yuki.skills (its ROUTES table)"),
}

#: The closed list of done-check probes (PLAN.md D19, D.2). Each returns True (seen), False (contradicted) or None
#: (could not read).
PROBES: tuple[str, ...] = (
    "read_back",       # the route's own measured read-back (a slider's or a mixer level's)
    "uia_name",        # an element with this role and name exists inside its container ("Pause {name}" in a grid)
    "media_session",   # one app's media session: status, or a track change since the run began
    "audio_active",    # the app's process has an active render session on this PC (Core Audio)
    "level",           # a mixer / endpoint level read now
    "page_at",         # the window in front shows a page of this template, titled
    "tab_shown",       # the app's window is in front with a known tab identity selected
    "foreground",      # the app's window is in front
    "mic_held",        # the app took the microphone after the run began (held before and still held: unconfirmed)
    "chat_open",       # the chat named by its stored digests is open
    "message_sent",    # memory captured this text, sent by the user there, after the run began (none yet: None)
    "moment",          # a moment of this kind and app was written after the run began (yuki.memory.moments)
    "driver",          # a probe function of a driver module (its PROBES table)
    "all",             # every part True -> True; any False -> False; else None
)

#: The closed list of slot types (map-skills S3.2).
SLOT_TYPES: tuple[str, ...] = ("enum", "number", "points", "bool", "entity", "text", "person", "app")
#: The live facts a slot may be filled from (``""``: none).
LIVE_FILLS: tuple[str, ...] = ("", "playing", "front")

#: D1's moment vocabulary (``yuki.memory.moments.MOMENT_KINDS``; a test keeps the two equal): what a skill's ``end``
#: proves and what ``learn_from`` / ``routine_on`` name.
END_KINDS: tuple[str, ...] = ("call_join", "game_voice", "site_front", "media_play", "media_context", "page",
                              "volume_change", "message_sent")

#: D14: "open" skills open or bring something forward; "change" skills change state (play, volume, join, send).
SKILL_KINDS: tuple[str, ...] = ("open", "change")
RUNGS: tuple[str, ...] = ("A", "B", "C")
#: When a route may run with a game in front (D17): never; always (it neither takes focus nor sends input: the mixer,
#: the media session); states (only while the app's window is in one of ``game_states``, the states the live checks
#: proved, kept as data).
GAME_RULES: tuple[str, ...] = ("never", "always", "states")
#: Window states (``yuki.connect.spotify_desktop.classify_state``'s words).
WINDOW_STATES: tuple[str, ...] = ("front", "covered", "visible", "minimised")

#: Words no connection file may hold anywhere (D0 zero setup: no account, key, token or sign-in). A guard over our
#: own shipped data, never a reading of the user's words.
SETUP_WORDS: tuple[str, ...] = ("token", "api_key", "apikey", "client_id", "client_secret", "password", "oauth",
                                "secret", "sign_in", "signin", "login", "account")


class ConnectionError_(ValueError):
    """A connection file or one of its skills is invalid (the message says where and why)."""


# ---------------------------------------------------------------------------------------------------------------------
# Templates: "{slot}" and "{slot.field}", a fixed machine format
# ---------------------------------------------------------------------------------------------------------------------


def _is_name(text: str) -> bool:
    return bool(text) and all(part.isidentifier() for part in text.split("."))


def template_names(text: str) -> list[str]:
    """The template names in ``text`` (``"Play {playlist.name}"`` -> ``["playlist.name"]``)."""
    out: list[str] = []
    i = 0
    while True:
        a = text.find("{", i)
        if a < 0:
            return out
        b = text.find("}", a + 1)
        if b < 0:
            return out
        inner = text[a + 1:b]
        if _is_name(inner):
            out.append(inner)
            i = b + 1
        else:
            i = a + 1


def lookup(values: Mapping[str, Any], name: str) -> Any:
    """A template name's value: ``"level"`` -> ``values["level"]``; ``"playlist.name"`` -> the ``name`` field of the
    playlist slot's value (a dict) or, for a plain value, the value itself when the field is ``name``. ``None`` when
    missing."""
    head, _, rest = name.partition(".")
    value = values.get(head)
    for part in [p for p in rest.split(".") if p] if rest else []:
        if isinstance(value, Mapping):
            value = value.get(part)
        elif part == "name" and value is not None:
            value = value
        else:
            return None
    return value


def _show(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return str(value)


def fill(text: str, values: Mapping[str, Any], *, strict: bool = False) -> str | None:
    """``text`` with every template replaced by its value. A missing value: ``None`` when ``strict`` (the route can't
    run as written), else the template is left out (a done line names what it knows)."""
    out: list[str] = []
    i = 0
    while True:
        a = text.find("{", i)
        if a < 0:
            out.append(text[i:])
            break
        b = text.find("}", a + 1)
        if b < 0:
            out.append(text[i:])
            break
        inner = text[a + 1:b]
        if not _is_name(inner):
            out.append(text[i:a + 1])
            i = a + 1
            continue
        out.append(text[i:a])
        value = lookup(values, inner)
        if value is None or value == "":
            if strict:
                return None
        else:
            out.append(_show(value))
        i = b + 1
    return " ".join("".join(out).split()) if not strict else "".join(out)


def fill_value(value: Any, values: Mapping[str, Any]) -> Any:
    """``value`` (a route argument: a string, number, list or table) with its templates filled. A string that is one
    whole template keeps the slot's own type (``"{level}"`` -> 40.0). Raises :class:`KeyError` when a template has no
    value (the route can't run)."""
    if isinstance(value, str):
        names = template_names(value)
        if len(names) == 1 and value.strip() == "{" + names[0] + "}":
            got = lookup(values, names[0])
            if got is None:
                raise KeyError(names[0])
            return got
        filled = fill(value, values, strict=True)
        if filled is None:
            raise KeyError(",".join(names))
        return filled
    if isinstance(value, Mapping):
        return {k: fill_value(v, values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [fill_value(v, values) for v in value]
    return value


# ---------------------------------------------------------------------------------------------------------------------
# The parsed shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One typed slot of a skill."""

    name: str
    type: str
    required: bool = False
    options: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    default: Any = None
    #: A taste kind whose candidates may fill it and are listed in the menu (``tastes.kind``).
    taste: str = ""
    #: The preference slot key (``prefs.slot``): ``"<skill>:<slot>"`` plus ``@<scope>`` (a template: ``@{target}``).
    pref: str = ""
    told: bool = False
    routine_key: bool = False
    #: A live fact that may fill it (``playing``: the app keys of the media sessions playing now; ``front``: the app
    #: in front), between the told value and the preference (map-skills S2.4 target, S2.2 browser).
    live: str = ""
    #: Ask (never guess) when a call is live and the user's words gave no value (map-skills S2.4: EarTrumpet mid-call).
    ask_in_call: bool = False
    #: The question when it is required and has no value (data; default "Which <name>?").
    ask: str = ""
    #: What the done line adds when the value was a guess (a preference that is not the usual, taste, a default), so
    #: one word corrects it: ``"in {browser}"`` (map-skills S3.2 step 3).
    say: str = ""

    def pref_key(self, skill: str, values: Mapping[str, Any]) -> str:
        """``"volume:control@spotify"``: the preference slot this one reads, its scope filled from ``values``."""
        base = self.pref or f"{skill}:{self.name}"
        return fill(base, values) or base


@dataclass(frozen=True)
class Check:
    """A done-check or a re-check: a probe of the closed list, its arguments (templates filled at run time), and how
    long it may wait (``within_s``) or how long after the done it runs (``after_s``)."""

    probe: str
    args: Mapping[str, Any] = field(default_factory=dict)
    within_s: float = 4.0
    after_s: float = 4.0
    parts: tuple["Check", ...] = ()
    #: The re-check reads the same source as the done-check (logged as such: no independent second source).
    same_source: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"probe": self.probe, **dict(self.args)}
        if self.parts:
            out["parts"] = [p.as_dict() for p in self.parts]
        out["within_s"] = self.within_s
        return out


@dataclass(frozen=True)
class Route:
    """One rung of a skill's ladder."""

    rung: str
    via: str
    variant: str = ""
    needs_focus: bool = True
    args: Mapping[str, Any] = field(default_factory=dict)
    #: Slot conditions: ``{slot = "control", in = ["in_app"]}`` or ``{slot = "uri", present = true}``.
    when: tuple[Mapping[str, Any], ...] = ()
    game: str = "never"
    game_states: tuple[str, ...] = ()
    done: Check | None = None
    recheck: Check | None = None
    #: A done line for this route only (else the skill's).
    say: str = ""
    driver: str = ""
    call: str = ""
    #: Whether the next rung may run after this one acted and its check said "not so" (``false`` for a rung whose
    #: action may have landed anyway). A skill that sends never moves on after an acting rung (it would send twice).
    retry_after_acting: bool = True

    @property
    def key(self) -> str:
        """``"A:in_app:slider"``: the route's identity in logs and ``skill_routes`` (rung, variant, verb)."""
        return f"{self.rung}:{self.variant}:{self.via}"


@dataclass(frozen=True)
class Skill:
    """One skill as its connection file declares it."""

    key: str
    app: str
    end: str
    kind: str
    menu: str
    done_line: str
    slots: tuple[Slot, ...]
    routes: tuple[Route, ...]
    learn_from: tuple[tuple[str, str], ...] = ()
    routine_on: tuple[tuple[str, str], ...] = ()
    offerable: bool = False
    handoff: bool = True
    wont: tuple[str, ...] = ()
    preview: str = ""
    file: str = ""
    hash: str = ""
    #: The skill may send something to other people (a message): judged as a send at the thinker's authorizer (rule
    #: 8), and its ladder never moves to another acting rung once one acted. ``sends_when``: slot conditions (the
    #: routes' ``when`` shape) under which it sends; none listed = whenever it runs.
    sends: bool = False
    sends_when: tuple[Mapping[str, Any], ...] = ()
    #: A driver module under ``yuki.skills`` whose ``window_state(app)`` reads the app's window state for the
    #: ``game = "states"`` rule (else the first driver its routes name that has one).
    state_reader: str = ""
    #: What the validator noticed and allowed (a route with no second re-check); reported, never fatal.
    warnings: tuple[str, ...] = ()

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)

    @property
    def connection(self) -> str:
        """``skills.connection``: the file and its content hash (a changed file retries a disabled rung A)."""
        return f"{self.file}#{self.hash}"


# ---------------------------------------------------------------------------------------------------------------------
# Site labels (yuki/connect/sites/*.toml), referenced, never copied
# ---------------------------------------------------------------------------------------------------------------------


def site_label(ref: str, *, lang: str = "en") -> dict[str, Any]:
    """A label of ``yuki/connect/sites/<site>.toml`` by reference: ``"spotify:controls.volume"`` -> ``{"role":
    "Slider", "name": "Change volume", "container": [["Group", "Now playing bar"]]}``; ``"spotify:rows.play"`` ->
    ``{"name": "Play {name}"}`` (a row wording). A ``[role, name, container_key]`` entry gets its container path from
    the same language table. Raises :class:`ConnectionError_` for a reference that names nothing."""
    site_name, _, path = str(ref or "").partition(":")
    if not site_name or not path:
        raise ConnectionError_(f"label {ref!r}: write it as <site>:<path>")
    from yuki.connect.templates import site

    table = site(site_name).get("labels", {}).get(lang)
    if not isinstance(table, Mapping):
        raise ConnectionError_(f"label {ref!r}: no [labels.{lang}] in sites/{site_name}.toml")
    node: Any = table
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise ConnectionError_(f"label {ref!r}: {part!r} is not in sites/{site_name}.toml")
        node = node[part]
    if isinstance(node, str):
        return {"name": node}
    if isinstance(node, list) and len(node) == 3 and all(isinstance(x, str) for x in node):
        container = table.get(node[2])
        steps = [[str(s[0]), str(s[1])] for s in container] if isinstance(container, list) else []
        return {"role": node[0], "name": node[1], "container": steps}
    if isinstance(node, list) and all(isinstance(s, list) and len(s) == 2 for s in node):
        return {"container": [[str(s[0]), str(s[1])] for s in node]}
    raise ConnectionError_(f"label {ref!r}: not a label, a control or a container")


# ---------------------------------------------------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------------------------------------------------


def _strs(raw: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in raw) if isinstance(raw, (list, tuple)) else ()


def _pairs(raw: Any, where: str) -> tuple[tuple[str, str], ...]:
    out = []
    for item in raw or []:
        if not (isinstance(item, (list, tuple)) and len(item) >= 2):
            raise ConnectionError_(f"{where}: each entry is [kind, app] (or [kind, app, service])")
        kind = str(item[0])
        if kind not in END_KINDS:
            raise ConnectionError_(f"{where}: {kind!r} is not a moment kind ({', '.join(END_KINDS)})")
        out.append((kind, str(item[1])))
    return tuple(out)


def _number(raw: Any, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConnectionError_(f"{where}: a number is needed, not {raw!r}")
    return float(raw)


def _slot(raw: Any, where: str) -> Slot:
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a slot is a table")
    name = str(raw.get("name") or "")
    if not name.isidentifier():
        raise ConnectionError_(f"{where}: slot name {name!r} is not a plain name")
    typ = str(raw.get("type") or "")
    if typ not in SLOT_TYPES:
        raise ConnectionError_(f"{where}.{name}: slot type {typ!r} is not one of {', '.join(SLOT_TYPES)}")
    options = _strs(raw.get("options"))
    if typ == "enum" and not options:
        raise ConnectionError_(f"{where}.{name}: an enum slot lists its options")
    default = raw.get("default")
    if typ == "enum" and default is not None and str(default) not in options:
        raise ConnectionError_(f"{where}.{name}: default {default!r} is not one of its options")
    live = str(raw.get("live") or "")
    if live not in LIVE_FILLS:
        raise ConnectionError_(f"{where}.{name}: live {live!r} is not one of {', '.join(LIVE_FILLS[1:])}")
    return Slot(name=name, type=typ, required=bool(raw.get("required", False)), options=options,
                min=_number(raw.get("min"), f"{where}.{name}.min"), max=_number(raw.get("max"), f"{where}.{name}.max"),
                default=default, taste=str(raw.get("taste") or ""), pref=str(raw.get("pref") or ""),
                told=bool(raw.get("told", False)), routine_key=bool(raw.get("routine_key", False)), live=live,
                ask_in_call=bool(raw.get("ask_in_call", False)), ask=str(raw.get("ask") or ""),
                say=str(raw.get("say") or ""))


def _check(raw: Any, where: str, *, recheck: bool = False) -> Check | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a check is a table")
    probe = str(raw.get("probe") or "")
    if probe not in PROBES:
        raise ConnectionError_(f"{where}: probe {probe!r} is not one of {', '.join(PROBES)}")
    parts: tuple[Check, ...] = ()
    if probe == "all":
        items = raw.get("parts")
        if not isinstance(items, list) or len(items) < 2:
            raise ConnectionError_(f"{where}: an 'all' check lists at least two parts")
        parts = tuple(c for c in (_check(p, f"{where}.parts[{i}]") for i, p in enumerate(items)) if c is not None)
    if probe == "driver":
        _driver_name(raw.get("driver"), f"{where}.driver")
        if not str(raw.get("call") or "").isidentifier():
            raise ConnectionError_(f"{where}: a driver probe names its call")
    if probe == "message_sent" and not str(raw.get("text") or ""):
        raise ConnectionError_(f"{where}: a message_sent check names the text it matches (text = \"{{text}}\")")
    within = _number(raw.get("within_s"), f"{where}.within_s")
    after = _number(raw.get("after_s"), f"{where}.after_s")
    if within is not None and not 0.0 <= within <= 6.0:
        raise ConnectionError_(f"{where}: within_s must be 0-6 s (done-checks poll only while a skill waits)")
    if after is not None and not 0.0 <= after <= 6.0:
        raise ConnectionError_(f"{where}: after_s must be 0-6 s")
    args = {k: v for k, v in raw.items() if k not in ("probe", "parts", "within_s", "after_s", "same_source")}
    return Check(probe=probe, args=args, within_s=4.0 if within is None else within,
                 after_s=(4.0 if recheck else 0.0) if after is None else after, parts=parts,
                 same_source=bool(raw.get("same_source", False)))


def driver_exists(name: str) -> bool:
    """Whether ``yuki.skills.<name>`` can be imported, asked of the import system (``importlib.util.find_spec``), not
    of the file system: the installed app keeps its modules inside PyInstaller's archive, where no ``.py`` file
    exists (review H1)."""
    import importlib.util

    try:
        return importlib.util.find_spec(f"yuki.skills.{name}") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _driver_name(raw: Any, where: str) -> str:
    name = str(raw or "")
    if not name.isidentifier() or name.startswith("_"):
        raise ConnectionError_(f"{where}: driver {name!r} is not a module name")
    if not driver_exists(name):
        raise ConnectionError_(f"{where}: no driver module yuki.skills.{name}")
    return name


def _resolve_labels(args: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Route arguments with ``label = "<site>:<path>"`` (and ``container_label``) expanded from the site files."""
    out = dict(args)
    lang = str(out.pop("lang", "en") or "en")
    if "label" in out:
        got = site_label(str(out.pop("label")), lang=lang)
        for k, v in got.items():
            out.setdefault(k, v)
    if "container_label" in out:
        got = site_label(str(out.pop("container_label")), lang=lang)
        if "container" not in got:
            raise ConnectionError_(f"{where}: container_label names no container")
        out.setdefault("container", got["container"])
    return out


def _route(raw: Any, where: str, slots: Mapping[str, Slot]) -> Route:
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a route is a table")
    rung = str(raw.get("rung") or "")
    if rung not in RUNGS:
        raise ConnectionError_(f"{where}: rung {rung!r} is not one of {', '.join(RUNGS)}")
    via = str(raw.get("via") or "")
    info = VERBS.get(via)
    if info is None:
        raise ConnectionError_(f"{where}: verb {via!r} is not one of {', '.join(VERBS)}")
    declared = raw.get("needs_focus")
    if info.focus is None:
        needs_focus = True if declared is None else bool(declared)
    elif info.focus:
        if declared is False:
            raise ConnectionError_(f"{where}: {via} takes focus; it can't be declared needs_focus = false")
        needs_focus = True
    else:
        needs_focus = bool(declared) if declared is not None else False
    if via == "agent" and rung != "C":
        raise ConnectionError_(f"{where}: the agent is rung C")
    game = str(raw.get("game") or ("never" if needs_focus else "always"))
    if game not in GAME_RULES:
        raise ConnectionError_(f"{where}: game {game!r} is not one of {', '.join(GAME_RULES)}")
    if needs_focus and game != "never":
        raise ConnectionError_(f"{where}: a route that takes focus never runs with a game in front (D17)")
    states = _strs(raw.get("game_states"))
    if any(s not in WINDOW_STATES for s in states):
        raise ConnectionError_(f"{where}: game_states are among {', '.join(WINDOW_STATES)}")
    if game == "states" and "game_states" not in raw:
        # the states the live checks proved, kept as data; [] (none proven yet) means never with a game in front
        raise ConnectionError_(f"{where}: game = 'states' lists the proven game_states ([] while none is proven)")
    when_raw = raw.get("when")
    when: list[Mapping[str, Any]] = []
    for cond in ([when_raw] if isinstance(when_raw, Mapping) else when_raw or []):
        if not isinstance(cond, Mapping) or str(cond.get("slot") or "") not in slots:
            raise ConnectionError_(f"{where}: a when-condition names one of the skill's slots")
        when.append(dict(cond))
    args = raw.get("args") or {}
    if not isinstance(args, Mapping):
        raise ConnectionError_(f"{where}: args is a table")
    args = _resolve_labels(args, where)
    driver = call = ""
    if via == "driver":
        driver = _driver_name(raw.get("driver"), f"{where}.driver")
        call = str(raw.get("call") or "")
        if not call.isidentifier():
            raise ConnectionError_(f"{where}: a driver route names its call")
    for text in _template_strings(args):
        for name in template_names(text):
            if name.split(".")[0] not in slots:
                raise ConnectionError_(f"{where}: template {{{name}}} names no slot")
    done = _check(raw.get("done"), f"{where}.done")
    if done is None and not info.reads_back:
        # D19: every route ends on a machine check; only a verb that reads its own result back may rely on that
        raise ConnectionError_(f"{where}: {via} has no read-back of its own; the route names its done-check")
    return Route(rung=rung, via=via, variant=str(raw.get("variant") or ""), needs_focus=needs_focus, args=args,
                 when=tuple(when), game=game, game_states=states, done=done,
                 recheck=_check(raw.get("recheck"), f"{where}.recheck", recheck=True), say=str(raw.get("say") or ""),
                 driver=driver, call=call, retry_after_acting=bool(raw.get("retry_after_acting", True)))


def _template_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _template_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _template_strings(v)


def _address_like(text: str) -> bool:
    """A URL or URI template (``https://...``, ``spotify:...``, ``...?x=1``): the values the setup guard reads too (a
    question such as an ``ask`` line is not one)."""
    head = text.split(":", 1)[0]
    return "://" in text or ("?" in text and "=" in text and " " not in text) or (
        ":" in text and " " not in text and head.isalpha() and head == head.casefold())


def _no_setup(value: Any, where: str) -> None:
    """D0: no key of any table in a connection file, and no URL or URI template among its values, names an account,
    a key, a token or a sign-in (a ``?access_token=`` inside an address is refused like an ``api_key`` field)."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            low = str(k).casefold()
            if any(w in low for w in SETUP_WORDS):
                raise ConnectionError_(f"{where}: {k!r} asks for setup; zero setup (D0) allows none")
            _no_setup(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _no_setup(v, f"{where}[{i}]")
    elif isinstance(value, str) and _address_like(value):
        low = value.casefold()
        if any(w in low for w in SETUP_WORDS):
            raise ConnectionError_(f"{where}: an address that asks for setup; zero setup (D0) allows none")


def parse_skill(key: str, raw: Mapping[str, Any], *, app: str, file: str = "", digest: str = "",
                state_reader: str = "") -> Skill:
    """One ``[skill."<key>"]`` table as a :class:`Skill`; raises :class:`ConnectionError_` naming what is wrong."""
    where = f"{file or 'connection'}:{key}"
    if not key or any(not part.isidentifier() for part in key.split(".")):
        raise ConnectionError_(f"{where}: a skill key is dotted plain names (spotify.playlist)")
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a skill is a table")
    end = str(raw.get("end") or "")
    if end not in END_KINDS:
        raise ConnectionError_(f"{where}: end {end!r} is not a moment kind ({', '.join(END_KINDS)})")
    kind = str(raw.get("kind") or "")
    if kind not in SKILL_KINDS:
        raise ConnectionError_(f"{where}: kind {kind!r} is not one of {', '.join(SKILL_KINDS)}")
    menu = " ".join(str(raw.get("menu") or "").split())
    if not menu:
        raise ConnectionError_(f"{where}: a skill has a menu line")
    done_line = str(raw.get("done_line") or "")
    if not done_line:
        raise ConnectionError_(f"{where}: a skill has a done line")
    slots = tuple(_slot(s, f"{where}.slot[{i}]") for i, s in enumerate(raw.get("slot") or []))
    names = [s.name for s in slots]
    if len(set(names)) != len(names):
        raise ConnectionError_(f"{where}: two slots have one name")
    by_name = {s.name: s for s in slots}
    for name in template_names(done_line):
        if name.split(".")[0] not in by_name:
            raise ConnectionError_(f"{where}: done line template {{{name}}} names no slot")
    routes = tuple(_route(r, f"{where}.route[{i}]", by_name) for i, r in enumerate(raw.get("route") or []))
    if not routes:
        raise ConnectionError_(f"{where}: a skill has at least one route")
    if len({r.key for r in routes}) != len(routes):
        raise ConnectionError_(f"{where}: two routes share rung, variant and verb")
    order = [RUNGS.index(r.rung) for r in routes]
    if order != sorted(order):
        raise ConnectionError_(f"{where}: routes are listed in ladder order (A, then B, then C)")
    sends_raw = raw.get("sends_when")
    sends_when: list[Mapping[str, Any]] = []
    for cond in ([sends_raw] if isinstance(sends_raw, Mapping) else sends_raw or []):
        if not isinstance(cond, Mapping) or str(cond.get("slot") or "") not in by_name:
            raise ConnectionError_(f"{where}: a sends_when condition names one of the skill's slots")
        sends_when.append(dict(cond))
    reader = str(raw.get("state_reader") or state_reader or "")
    if reader:
        reader = _driver_name(reader, f"{where}.state_reader")
    # D19: a second probe re-checks each done; a route without one is allowed and reported
    warnings = tuple(f"{where}.route[{i}]: no recheck (D19's second probe)" for i, r in enumerate(routes)
                     if r.via != "agent" and r.recheck is None)
    return Skill(key=key, app=str(raw.get("app") or app), end=end, kind=kind, menu=menu, done_line=done_line,
                 slots=slots, routes=routes, learn_from=_pairs(raw.get("learn_from"), f"{where}.learn_from"),
                 routine_on=_pairs(raw.get("routine_on"), f"{where}.routine_on"),
                 offerable=bool(raw.get("offerable", False)), handoff=bool(raw.get("handoff", True)),
                 wont=_strs(raw.get("wont")), preview=str(raw.get("preview") or ""), file=file, hash=digest,
                 sends=bool(raw.get("sends", False)) or bool(sends_when), sends_when=tuple(sends_when),
                 state_reader=reader, warnings=warnings)


def sends_with(skill: Skill, values: Mapping[str, Any], *, partial: bool = False) -> bool:
    """Whether running ``skill`` with these slot values sends something to other people (its ``sends`` /
    ``sends_when`` data). ``partial``: ``values`` are only what the model gave, so a condition on a slot it left out
    still counts as met (the resolver may fill it with a sending value). Data only, never the words."""
    if not skill.sends:
        return False
    for cond in skill.sends_when:
        name = str(cond.get("slot") or "")
        v = values.get(name)
        if isinstance(v, Mapping):
            v = v.get("name")
        missing = v is None or v == ""
        if partial and missing:
            continue
        if "in" in cond and (missing or str(v) not in [str(x) for x in cond.get("in") or []]):
            return False
        if "present" in cond and bool(cond.get("present")) == missing:
            return False
    return True


def window_state_reader(skill: Skill | None) -> Callable[[str], str | None] | None:
    """The ``window_state(app)`` hook of the skill's driver module (its ``state_reader``, else the first driver its
    routes name that has one), for the ``game = "states"`` rule. ``None`` when none provides it (such rungs then stay
    off with a game in front: fails safe)."""
    if skill is None:
        return None
    names = [skill.state_reader] + [r.driver for r in skill.routes]
    for name in dict.fromkeys(n for n in names if n):
        try:
            fn = getattr(importlib.import_module(f"yuki.skills.{name}"), "window_state", None)
        except Exception:  # noqa: BLE001
            continue
        if callable(fn):
            return fn
    return None


def parse_file(path: Path) -> tuple[list[Skill], list[str]]:
    """Every skill of one connection file, and the errors (one per skill skipped, or one for the whole file)."""
    try:
        raw_bytes = path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [], [f"{path.name}: {type(exc).__name__}"]
    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
    try:
        _no_setup(data, path.name)
    except ConnectionError_ as exc:
        return [], [str(exc)]
    conn = data.get("connection") if isinstance(data.get("connection"), Mapping) else {}
    app = str(conn.get("app") or "")
    if not app:
        return [], [f"{path.name}: [connection] app is missing"]
    skills: list[Skill] = []
    errors: list[str] = []
    for key, raw in (data.get("skill") or {}).items():
        try:
            skills.append(parse_skill(str(key), raw, app=app, file=path.name, digest=digest,
                                      state_reader=str(conn.get("state_reader") or "")))
        except ConnectionError_ as exc:
            errors.append(str(exc))
    return skills, errors


@dataclass(frozen=True)
class Loaded:
    """The skills loaded from a set of folders, in load order, with the errors of the ones skipped."""

    skills: tuple[Skill, ...]
    errors: tuple[str, ...]
    version: str

    def get(self, key: str) -> Skill | None:
        return next((s for s in self.skills if s.key == key), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.skills)

    @property
    def warnings(self) -> tuple[str, ...]:
        """What the validator allowed and reports (a route with no second re-check)."""
        return tuple(w for s in self.skills for w in s.warnings)


_cache_lock = threading.Lock()
_cache: dict[tuple[str, ...], tuple[tuple[tuple[str, float, int], ...], Loaded]] = {}


def _stamp(dirs: tuple[Path, ...]) -> tuple[tuple[str, float, int], ...]:
    out = []
    for d in dirs:
        try:
            for p in sorted(d.glob("*.toml")):
                st = p.stat()
                out.append((str(p), st.st_mtime, st.st_size))
        except OSError:
            continue
    return tuple(out)


def load(dirs: Iterable[str | Path] | None = None) -> Loaded:
    """The skills of every connection file in ``dirs`` (default: the bundled folder), validated, at most
    :data:`MAX_SKILLS`, one per key and one per (app, end); cached until a file changes."""
    folders = tuple(Path(d) for d in (dirs if dirs is not None else [CONNECTIONS_DIR]))
    key = tuple(str(f) for f in folders)
    stamp = _stamp(folders)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    skills: list[Skill] = []
    errors: list[str] = []
    seen_keys: set[str] = set()
    seen_ends: set[tuple[str, str]] = set()
    for path_text, _m, _s in stamp:
        got, errs = parse_file(Path(path_text))
        errors.extend(errs)
        for s in got:
            if s.key in seen_keys:
                errors.append(f"{s.file}:{s.key}: another file already has this skill")
                continue
            if (s.app, s.end) in seen_ends:
                errors.append(f"{s.file}:{s.key}: one skill per (app, end) ({s.app}, {s.end})")
                continue
            if len(skills) >= MAX_SKILLS:
                errors.append(f"{s.file}:{s.key}: more than {MAX_SKILLS} skills; a new skill replaces one")
                continue
            seen_keys.add(s.key)
            seen_ends.add((s.app, s.end))
            skills.append(s)
    version = hashlib.sha256("|".join(f"{s.key}#{s.hash}" for s in skills).encode()).hexdigest()[:12]
    loaded = Loaded(tuple(skills), tuple(errors), version)
    with _cache_lock:
        _cache[key] = (stamp, loaded)
    return loaded


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = ["CONNECTIONS_DIR", "MAX_SKILLS", "MENU_LINE_CHARS", "VERBS", "PROBES", "SLOT_TYPES", "LIVE_FILLS", "END_KINDS",
           "SKILL_KINDS", "RUNGS", "GAME_RULES", "WINDOW_STATES", "SETUP_WORDS", "ConnectionError_", "VerbInfo",
           "Slot", "Check", "Route", "Skill", "Loaded", "template_names", "lookup", "fill", "fill_value",
           "site_label", "parse_skill", "parse_file", "load", "clear_cache", "driver_exists", "sends_with",
           "window_state_reader"]
