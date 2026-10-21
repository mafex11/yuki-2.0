"""Capability files: what any user's apps can do, as data (phase L design §2, §6; PLAN.md D0, D2, D14, D17, D19, D24).

One TOML per app connection in ``yuki/skills/connections/`` (bundled: ``packaging/yuki.spec`` collects
``**/*.toml``). A file declares **capabilities**: one generic, parameterised way for *any* user to reach one end state
(set an app's volume, play a named Spotify playlist, open a chat with someone, open a web page, bring a site's tab
forward, join a voice channel). Each is callable on request from day one (rung A), with one neutral menu line. A
capability holds what is true for every user: its routes (rung A the app's own controls, links and Windows' media and
volume controls; rung B a recorded UI Automation path; rung C the full agent), the machine done-check each route ends
on, a second re-check, its typed slots **with roles**, what occasions it serves, its offer class, and a neutral done
line. It never holds what *this* user does: which app, site, control, person or time is theirs is learned (phase L:
:mod:`yuki.skills.catalog` adds the learned skills that specialise a capability with the user's bound values).

**The habit-key refusal** (runtime code, not a test; design §6): a shipped file that holds a ``[skill.*]`` table, a
``routine_on``, ``offerable``, ``learn_from`` or ``preview`` key, a default on a ``means`` slot, or a process or
service literal in a route's arguments (a service literal in its checks too) is refused whole: its error is reported
(``connection_error``) and none of its capabilities load. Whether a ``menu`` line is neutral is a reviewer's check.

Code knows only the closed lists below (like ``OFFER_DOS``): an unknown verb, probe, slot type, role or end kind skips
that capability and is reported, never guessed. App labels and URL/URI templates are data in
``yuki/connect/sites/*.toml`` and are referenced from here (``label = "spotify:controls.volume"``), never copied.

File shape (abbreviated)::

    [connection]
    app = "demo"                          # the app key (yuki.memory.store.app_key form) the routes belong to
    state_reader = "demo_route"           # optional: the driver module whose window_state(app) feeds game = "states"

    [capability."demo.lamp"]
    end = "volume_change"                 # D3: the moment kind its done-check proves (D1 vocabulary)
    kind = "change"                       # D14: "open" (2 verified runs before an offer) or "change" (5, none failed)
    menu = "set the demo lamp's level"    # one neutral menu line (<= 140 chars with its slots): no app order, no "their"
    done_line = "Lamp at {level}%."       # a neutral template with slots
    offer_class = "everyday"              # logistics | everyday | judged | never (default never: fail closed)
    commit = "words"                      # optional, stricter only: none < words < user (D24; the engine's end-kind
                                          # table decides, a capability can only raise it)
    pre_commit_end = ""                   # the end it stops at when it serves a commit end (browser.site_tab: site_front)
    wont = []                             # what it never does (the card's "Won't" line: SKILL_WONT_LINES words)
    handoff = true                        # rung C (the thinker) when every route failed; never with a game in front
    sends_when = { slot = "send", in = ["send"] }   # it may send to people (rule 8) under these slot values
    serves = [{ end = "volume_change", target = "level", needs = [] }]   # the occasions it serves (the learner binds
                                          # by these, design §4.1: never a list of apps)
    bind = { level = "value", target = "app" }      # where each slot's value is read from an occasion's end moment

    [[capability."demo.lamp".slot]]
    name = "level"
    type = "number"                       # enum | number | points | bool | entity | text | person | app
    role = "fill"                         # key (defines a habit) | fill (varies per request) | means (the program or
                                          # control: never part of a skill's identity, never shipped with a default)
    min = 0
    max = 100

    [[capability."demo.lamp".route]]
    rung = "A"
    variant = "in_app"
    via = "uia_invoke"                    # the closed VERBS list
    args = { pattern = "range", window = "Demo.exe", role = "Slider", name = "Lamp", value = "{level}" }
    game = "always"                       # never | always | states (with game_states): may it run with a game in front
    done = { probe = "read_back", within_s = 2 }   # required unless the verb reads its own result back (D19)
    recheck = { probe = "level", after_s = 3, same_source = true }   # missing: allowed, reported as a warning
    # retry_after_acting = false          # after this rung acted and its check said "not so", try nothing else
    # means = "browser"                   # the program kind this route serves: "app" (the app's own) or "browser"

A ``means`` slot's preference key is derived, never written: ``<capability>:<slot>`` plus ``@{<scope slot>}`` when the
slot names a ``scope`` (``volume:control@{target}``, ``web.open_page:browser@{site}``: design §5). A ``means`` slot
with ``fill = "driver"`` has none (its route fills it from its own data at run time: the people index). A ``fill``
slot's ``kind`` names its entity kind (``playlist``): the taste rows it is filled from are ``<app>_<kind>``.

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

#: At most this many capabilities load from the shipped files (one neutral menu line each: design §2 said "<= 8"; 12
#: since app.way, lane kWays, filled the eighth place: room for the next capability file).
MAX_CAPABILITIES = 12
#: At most this many live learned skills (decision D4 of the habits plan: skill libraries collapse as they grow; design
#: §4.9: ranked, the rest stay in the Playbook only). :mod:`yuki.skills.catalog` applies it.
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
#: The probes that compare a process image: their check must name ``process`` (or ``window``).
PROCESS_PROBES: tuple[str, ...] = ("foreground", "tab_shown", "audio_active", "mic_held")

#: The closed list of slot types (map-skills S3.2).
SLOT_TYPES: tuple[str, ...] = ("enum", "number", "points", "bool", "entity", "text", "person", "app")
#: The live facts a slot may be filled from (``""``: none).
LIVE_FILLS: tuple[str, ...] = ("", "playing", "front")
#: Slot roles (design §2, §4.2). ``key``: defines a habit (a calling service, a site, a page kind): a learned skill
#: binds it, and a key value splits off its own skill only when it passes the bar alone. ``fill``: varies per request
#: (a person, a video, a playlist, a level, a target app). ``means``: the program or control (a browser, a chat app,
#: in-app vs mixer): never part of a skill's identity and never shipped with a default (a preference, a learned fill
#: policy or the route's own data picks it).
SLOT_ROLES: tuple[str, ...] = ("key", "fill", "means")
#: Where a ``means`` slot's value comes from when the words give none: ``prefs`` (the user's usual and the learned fill
#: policy, under the derived preference key) or ``driver`` (the route's own data at run time: the people index).
MEANS_FILLS: tuple[str, ...] = ("prefs", "driver")

#: D1's moment vocabulary (``yuki.memory.moments.MOMENT_KINDS``; a test keeps the two equal): what a capability's
#: ``end`` proves. morning21 B9: ``display_change``, a monitor's brightness or screen power changed (the display
#: capability's end; its runs are occasions of that kind).
END_KINDS: tuple[str, ...] = ("call_join", "game_voice", "site_front", "media_play", "media_context", "page",
                              "volume_change", "message_sent", "display_change")
#: The end kinds an occasion can end on (design §3.1): ``site_front``, ``media_play`` and ``game_voice`` never are
#: (a glance is never the meeting; a new title is often autoplay), so a capability never serves them.
SERVE_ENDS: tuple[str, ...] = ("call_join", "page", "message_sent", "media_context", "volume_change",
                               "display_change")
#: What an occasion's end reaches (design §4.1): its target kind. ``*``: any.
TARGET_KINDS: tuple[str, ...] = ("call", "chat", "page", "playlist", "level", "video", "*")
#: The driver data an occasion's app or service must have for a capability to serve it (design §4.1; the learner
#: reads these, it holds no list of apps): ``browser`` the end's app is a measured browser; ``calling_site`` the
#: service is a calling site (memory's own verdict, ``CallEvidence.calling_service``); ``chat`` the app or the
#: thread's host has ``[chat]`` data in a connection file; ``chat_voice`` that ``[chat]`` data has a ``join_voice``
#: label; ``spotify`` the app has ``[spotify]`` data.
SERVE_NEEDS: tuple[str, ...] = ("browser", "calling_site", "chat", "chat_voice", "spotify")
#: Where a slot's value is read from on an occasion's end moment (design §4.1, §4.2; ``bind``): its ``app``, its
#: ``service``, its ``entity`` (a page's template value, a playlist), its ``value`` (a level), its ``means`` (in-app vs
#: mixer), the page's ``site`` and template ``page`` kind (a host-scope page: the registrable domain as ``site``), or
#: the chat's ``contact``.
BIND_SOURCES: tuple[str, ...] = ("app", "service", "entity", "value", "means", "site", "page", "contact")

#: D14: "open" capabilities open or bring something forward; "change" ones change state (play, volume, join, send).
SKILL_KINDS: tuple[str, ...] = ("open", "change")
RUNGS: tuple[str, ...] = ("A", "B", "C")
#: When a route may run with a game in front (D17): never; always (it neither takes focus nor sends input: the mixer,
#: the media session); states (only while the app's window is in one of ``game_states``, the states the live checks
#: proved, kept as data).
GAME_RULES: tuple[str, ...] = ("never", "always", "states")
#: Window states (``yuki.connect.spotify_desktop.classify_state``'s words).
WINDOW_STATES: tuple[str, ...] = ("front", "covered", "visible", "minimised")
#: The program kind a route serves (design §4.4, critique L4): ``app`` the app's own program (``slack://`` opens the
#: desktop app), ``browser`` a web browser (the chat's own https address); ``""`` not stated.
ROUTE_MEANS: tuple[str, ...] = ("", "app", "browser")

#: Offer classes (design §4.7, critique M8), generic per capability: ``logistics`` the user does the act (the skill
#: only brings it forward: offerable, the naming model's ``everyday`` not consulted); ``everyday`` offerable;
#: ``judged`` offerable only when the naming call says ``everyday`` (a page can be anything, work too); ``never``.
OFFER_CLASSES: tuple[str, ...] = ("logistics", "everyday", "judged", "never")
#: Commit words, least strict first (D24): ``none`` Yuki may reach the end itself; ``words`` only on the user's plain
#: words, through the consent check (rule 8); ``user`` never Yuki's step (the skill stops before it). The engine's
#: end-kind table decides; a capability may only make it stricter (:func:`strictest`).
COMMITS: tuple[str, ...] = ("none", "words", "user")
#: Where a catalog entry comes from: a shipped file, or the learner (a database row, :mod:`yuki.skills.catalog`).
ORIGINS: tuple[str, ...] = ("capability", "learned")

#: Words no connection file may hold anywhere (D0 zero setup: no account, key, token or sign-in). A guard over our
#: own shipped data, never a reading of the user's words.
SETUP_WORDS: tuple[str, ...] = ("token", "api_key", "apikey", "client_id", "client_secret", "password", "oauth",
                                "secret", "sign_in", "signin", "login", "account")
#: Keys no shipped file may hold (the habit-key refusal, design §6): which moments are *this user's* routine, whether
#: a skill is offered, what trains it and its card preview are learned, never shipped.
HABIT_KEYS: tuple[str, ...] = ("routine_on", "offerable", "learn_from", "preview")
#: Route argument names whose value must come from a slot (a template), never a literal: the user's program and the
#: user's service are learned (``zen.exe`` and ``Google Meet`` were E1's, written into shipped data).
HABIT_ARG_KEYS: tuple[str, ...] = ("process", "service")
#: Check argument names that must come from a slot: a service is always a key slot's value.
HABIT_CHECK_KEYS: tuple[str, ...] = ("service",)


class ConnectionError_(ValueError):
    """A connection file or one of its capabilities is invalid (the message says where and why)."""


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


def domain_like(text: str) -> bool:
    """Whether ``text`` is written as a domain name (``github.com``): labels of letters, digits and hyphens joined by
    dots, the last one letters. A fixed machine format, never a reading of meaning."""
    text = str(text or "").strip().casefold()
    if not text or len(text) > 253 or "." not in text:
        return False
    labels = text.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
           or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label) for label in labels):
        return False
    return labels[-1].isalpha() and len(labels[-1]) >= 2


def scope_value(value: Any) -> str:
    """A preference scope's value as one identity (review r1 L4), so a usual or an observed preference kept under a
    scope is found however the value came in: its name, case-folded and spaced once; a site said by its site file's
    name or by an address of one of that file's hosts, as the site file's name (``yuki/connect/sites``); any other
    domain without a leading ``www.``. Fixed formats only, never a reading of meaning; ``""`` for no value."""
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    text = " ".join(str(value if value is not None else "").split()).casefold()
    if not text or not (domain_like(text) or text.replace("_", "").isalnum()):
        return text
    try:
        from yuki.connect.templates import site as site_data, sites
    except Exception:  # noqa: BLE001 - no site data: the folded value
        return text
    names = sites()
    if text in names:
        return text
    if not domain_like(text):
        return text
    host = text[4:] if text.startswith("www.") and domain_like(text[4:]) else text
    for name in names:
        hosts = {str(h).casefold() for h in (site_data(name).get("site") or {}).get("hosts") or []}
        if text in hosts or host in hosts:
            return name
    return host


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


def fill_partial(value: Any, values: Mapping[str, Any]) -> Any:
    """``value`` with only the templates whose slot has a value in ``values`` filled; every other template stays as
    written (a learned skill binds its key values into its capability's route arguments, the rest are filled per
    request). A string that is one whole bound template keeps the value's own type."""
    if isinstance(value, str):
        names = template_names(value)
        if not names:
            return value
        if len(names) == 1 and value.strip() == "{" + names[0] + "}":
            got = lookup(values, names[0])
            return value if got is None else got
        out = value
        for name in names:
            got = lookup(values, name)
            if got is not None and got != "":
                out = out.replace("{" + name + "}", _show(got))
        return out
    if isinstance(value, Mapping):
        return {k: fill_partial(v, values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [fill_partial(v, values) for v in value]
    return value


# ---------------------------------------------------------------------------------------------------------------------
# The parsed shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One typed slot of a capability (or of a learned skill, which takes its capability's slots with its bound values
    as their defaults)."""

    name: str
    type: str
    required: bool = False
    options: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    default: Any = None
    #: The taste kind whose candidates may fill it and are listed in the menu (``tastes.kind``): derived from the
    #: slot's ``kind`` and the connection's app (``<app>_<kind>``), never written in a file.
    taste: str = ""
    #: The preference key it reads (``prefs.slot``), derived for a ``means`` slot that fills from preferences:
    #: ``"<capability>:<slot>"`` plus ``@{<scope slot>}`` (a template filled per request); ``""`` for any other slot.
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
    #: The slot's role (:data:`SLOT_ROLES`): ``key`` / ``fill`` / ``means``.
    role: str = "fill"
    #: A fill slot's entity kind (``playlist``, ``video``): the learner's and taste's fill-slot kind (design §5).
    kind: str = ""
    #: A means slot's preference scope: the name of the slot whose value scopes it (``target``, ``site``).
    scope: str = ""
    #: A means slot's fill source (:data:`MEANS_FILLS`); ``""`` for other slots.
    fill: str = ""

    def pref_key(self, skill: str, values: Mapping[str, Any]) -> str:
        """``"volume:control@spotify"``: the preference slot this one reads, its scope filled from ``values`` as one
        identity (:func:`scope_value`: "YouTube", "youtube.com" and a learned skill's bound ``youtube`` read one key)."""
        base = self.pref or f"{skill}:{self.name}"
        if self.scope and values.get(self.scope) not in (None, ""):
            values = {**values, self.scope: scope_value(values[self.scope]) or values[self.scope]}
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
    """One rung of a capability's ladder."""

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
    #: A done line for this route only (else the capability's).
    say: str = ""
    driver: str = ""
    call: str = ""
    #: Whether the next rung may run after this one acted and its check said "not so" (``false`` for a rung whose
    #: action may have landed anyway). A capability that sends never moves on after an acting rung (it would send
    #: twice).
    retry_after_acting: bool = True
    #: The program kind this route serves (:data:`ROUTE_MEANS`): the learner compiles a link route per means.
    means: str = ""

    @property
    def key(self) -> str:
        """``"A:in_app:slider"``: the route's identity in logs and ``skill_routes`` (rung, variant, verb)."""
        return f"{self.rung}:{self.variant}:{self.via}"


@dataclass(frozen=True)
class Serve:
    """One kind of occasion a capability serves (design §4.1): the end kind, the target kind, and the driver data the
    end's app or service must have (:data:`SERVE_NEEDS`, every one)."""

    end: str
    target: str = "*"
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skill:
    """One catalog entry: a capability as its connection file declares it, or a learned skill that specialises one
    (:mod:`yuki.skills.catalog`). Both run through the same engine; only a learned skill carries bound values, a name
    and (from the learner's later stages) routines, previews and offers."""

    key: str
    app: str
    end: str
    kind: str
    menu: str
    done_line: str
    slots: tuple[Slot, ...]
    routes: tuple[Route, ...]
    #: Old habit fields, kept empty on every capability (the habit-key refusal keeps them out of shipped files): the
    #: routine miner and the routine lane read them until they read learned skills (phase L, lWire).
    learn_from: tuple[tuple[str, str], ...] = ()
    routine_on: tuple[tuple[str, str], ...] = ()
    offerable: bool = False
    handoff: bool = True
    wont: tuple[str, ...] = ()
    preview: str = ""
    file: str = ""
    hash: str = ""
    #: The capability may send something to other people (a message): judged as a send at the thinker's authorizer
    #: (rule 8), and its ladder never moves to another acting rung once one acted. ``sends_when``: slot conditions (the
    #: routes' ``when`` shape) under which it sends; none listed = whenever it runs.
    sends: bool = False
    sends_when: tuple[Mapping[str, Any], ...] = ()
    #: A driver module under ``yuki.skills`` whose ``window_state(app)`` reads the app's window state for the
    #: ``game = "states"`` rule (else the first driver its routes name that has one).
    state_reader: str = ""
    #: What the validator noticed and allowed (a route with no second re-check); reported, never fatal.
    warnings: tuple[str, ...] = ()
    #: :data:`ORIGINS`: ``capability`` (a shipped file) or ``learned`` (a database row).
    origin: str = "capability"
    #: The capability this entry is (its own key) or specialises (a learned skill's).
    capability: str = ""
    #: A learned skill's name (the naming call's, validated; ``""`` when nameless or a capability).
    name: str = ""
    #: What occasions it serves, where each slot is read from on one, the end it stops at before a commit, its
    #: declared commit (stricter only), and its offer class (design §4.1-§4.7).
    serves: tuple[Serve, ...] = ()
    bind: Mapping[str, str] = field(default_factory=dict)
    pre_commit_end: str = ""
    commit: str = ""
    offer_class: str = "never"
    #: A learned skill's bound values (slot -> value; names in RAM only, kept encrypted in the database).
    bound: Mapping[str, Any] = field(default_factory=dict)

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)

    def slots_of(self, role: str) -> tuple[Slot, ...]:
        """The slots of one role (:data:`SLOT_ROLES`)."""
        return tuple(s for s in self.slots if s.role == role)

    @property
    def learned(self) -> bool:
        return self.origin == "learned"

    @property
    def connection(self) -> str:
        """``skills.connection``: the file and its content hash (a changed file retries a disabled rung A)."""
        return f"{self.file}#{self.hash}"


def strictest(*commits: str) -> str:
    """The strictest of the commit words given (:data:`COMMITS`; unknown or empty words are ignored, none -> ``""``):
    a capability's own ``commit`` can only raise the engine's end-kind table (D24), never lower it."""
    known = [c for c in commits if c in COMMITS]
    return max(known, key=COMMITS.index) if known else ""


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
# The habit-key refusal (design §6): runtime code over our own shipped data
# ---------------------------------------------------------------------------------------------------------------------


def _literal(value: Any) -> bool:
    """A value that names something itself rather than through a slot: a string with any text outside its templates
    (``"zen.exe"``, and ``"zen.exe{pick}"`` too: review r2 L6), or a number / list / table holding one."""
    if isinstance(value, str):
        rest = value
        for name in template_names(value):
            rest = rest.replace("{" + name + "}", "")
        return bool(rest.strip())
    if isinstance(value, Mapping):
        return any(_literal(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_literal(v) for v in value)
    return value is not None


def _arg_literals(args: Any, keys: tuple[str, ...], where: str) -> None:
    """Raise when ``args`` (recursively) holds one of ``keys`` with a literal value."""
    if isinstance(args, Mapping):
        for k, v in args.items():
            if str(k) in keys and _literal(v):
                raise ConnectionError_(f"{where}: habit_key: {k!r} is a literal; it comes from a slot (learned)")
            _arg_literals(v, keys, f"{where}.{k}")
    elif isinstance(args, (list, tuple)):
        for i, v in enumerate(args):
            _arg_literals(v, keys, f"{where}[{i}]")


def _habit_keys_in(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for k, v in value.items():
            if str(k) in HABIT_KEYS:
                raise ConnectionError_(f"{where}: habit_key: {k!r} is learned, never shipped")
            _habit_keys_in(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _habit_keys_in(v, f"{where}[{i}]")


def refuse_habits(data: Mapping[str, Any], name: str) -> None:
    """The habit-key refusal over one parsed shipped file (raises :class:`ConnectionError_` with ``habit_key`` in
    its message; the whole file is then skipped): a ``[skill.*]`` table; a ``routine_on``, ``offerable``,
    ``learn_from`` or ``preview`` key anywhere; an ``app`` key on a capability (its app is its file's ``[connection]``
    app: a capability pinned to one program is a habit, review r2 L6); a default on a ``means`` or ``key`` slot; a
    process or service literal in a route's arguments, or a service literal in a route's checks."""
    if "skill" in data:
        raise ConnectionError_(f"{name}: habit_key: [skill.*] tables are learned (phase L); a file declares "
                               f"[capability.*]")
    _habit_keys_in(data, name)
    caps = data.get("capability")
    if not isinstance(caps, Mapping):
        return
    for key, raw in caps.items():
        if not isinstance(raw, Mapping):
            continue
        where = f"{name}:{key}"
        if "app" in raw:
            raise ConnectionError_(f"{where}: habit_key: a capability's app is its file's [connection] app, never "
                                   f"a program of its own")
        for i, slot in enumerate(raw.get("slot") or []):
            if isinstance(slot, Mapping) and str(slot.get("role") or "") == "means" and "default" in slot:
                raise ConnectionError_(f"{where}.slot[{i}]: habit_key: a means slot ships no default (a preference "
                                       f"or a learned fill policy picks it)")
            if isinstance(slot, Mapping) and str(slot.get("role") or "") == "key" and "default" in slot:
                # review r1 L3: a key slot's value is the habit itself (a service, a site); a learned skill binds it
                raise ConnectionError_(f"{where}.slot[{i}]: habit_key: a key slot ships no default (a learned skill "
                                       f"binds it)")
        for i, route in enumerate(raw.get("route") or []):
            if not isinstance(route, Mapping):
                continue
            _arg_literals(route.get("args") or {}, HABIT_ARG_KEYS, f"{where}.route[{i}].args")
            for part in ("done", "recheck"):
                _arg_literals(route.get(part) or {}, HABIT_CHECK_KEYS, f"{where}.route[{i}].{part}")


# ---------------------------------------------------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------------------------------------------------


def _strs(raw: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in raw) if isinstance(raw, (list, tuple)) else ()


def _number(raw: Any, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConnectionError_(f"{where}: a number is needed, not {raw!r}")
    return float(raw)


def _slot(raw: Any, where: str, *, capability: str, app: str) -> Slot:
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a slot is a table")
    name = str(raw.get("name") or "")
    if not name.isidentifier():
        raise ConnectionError_(f"{where}: slot name {name!r} is not a plain name")
    typ = str(raw.get("type") or "")
    if typ not in SLOT_TYPES:
        raise ConnectionError_(f"{where}.{name}: slot type {typ!r} is not one of {', '.join(SLOT_TYPES)}")
    role = str(raw.get("role") or "")
    if role not in SLOT_ROLES:
        raise ConnectionError_(f"{where}.{name}: role {role!r} is not one of {', '.join(SLOT_ROLES)}")
    for gone, now in (("pref", "the preference key is derived (<capability>:<slot>[@<scope>]); name a scope"),
                      ("taste", "the taste kind is derived from the slot's kind")):
        if gone in raw:
            raise ConnectionError_(f"{where}.{name}: {gone!r} is not a capability field: {now}")
    options = _strs(raw.get("options"))
    if typ == "enum" and not options:
        raise ConnectionError_(f"{where}.{name}: an enum slot lists its options")
    default = raw.get("default")
    if role in ("means", "key") and default is not None:
        raise ConnectionError_(f"{where}.{name}: a {role} slot ships no default")
    if typ == "enum" and default is not None and str(default) not in options:
        raise ConnectionError_(f"{where}.{name}: default {default!r} is not one of its options")
    live = str(raw.get("live") or "")
    if live not in LIVE_FILLS:
        raise ConnectionError_(f"{where}.{name}: live {live!r} is not one of {', '.join(LIVE_FILLS[1:])}")
    kind = str(raw.get("kind") or "")
    if kind and not (kind.isidentifier() and kind == kind.casefold()):
        raise ConnectionError_(f"{where}.{name}: kind {kind!r} is a lower-case plain word")
    if kind and role != "fill":
        raise ConnectionError_(f"{where}.{name}: only a fill slot has an entity kind")
    scope = str(raw.get("scope") or "")
    means_fill = str(raw.get("fill") or "")
    if role == "means":
        means_fill = means_fill or "prefs"
        if means_fill not in MEANS_FILLS:
            raise ConnectionError_(f"{where}.{name}: fill {means_fill!r} is not one of {', '.join(MEANS_FILLS)}")
        if scope and (not scope.isidentifier() or scope == name):
            raise ConnectionError_(f"{where}.{name}: scope names another slot of this capability")
        if scope and means_fill != "prefs":
            raise ConnectionError_(f"{where}.{name}: only a slot filled from preferences has a scope")
    elif scope or means_fill:
        raise ConnectionError_(f"{where}.{name}: scope and fill belong to a means slot")
    pref = ""
    if role == "means" and means_fill == "prefs":
        pref = f"{capability}:{name}" + (f"@{{{scope}}}" if scope else "")
    taste = f"{app}_{kind}" if kind and app else ""
    return Slot(name=name, type=typ, required=bool(raw.get("required", False)), options=options,
                min=_number(raw.get("min"), f"{where}.{name}.min"), max=_number(raw.get("max"), f"{where}.{name}.max"),
                default=default, taste=taste, pref=pref, told=bool(raw.get("told", False)),
                routine_key=bool(raw.get("routine_key", False)), live=live,
                ask_in_call=bool(raw.get("ask_in_call", False)), ask=str(raw.get("ask") or ""),
                say=str(raw.get("say") or ""), role=role, kind=kind, scope=scope, fill=means_fill)


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
    if probe in PROCESS_PROBES and not str(raw.get("process") or raw.get("window") or "").strip():
        # review r2 L4: these compare process images; with none named an unreadable image ("") would match ""
        raise ConnectionError_(f"{where}: a {probe} check names the process it reads (process = \"App.exe\")")
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


def parse_route(raw: Any, where: str, slots: Mapping[str, Slot]) -> Route:
    """One route table as a :class:`Route` (a capability's, or a rung the learner compiled for a learned skill,
    checked against the slot table it runs with); raises :class:`ConnectionError_` naming what is wrong."""
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
            raise ConnectionError_(f"{where}: a when-condition names one of the capability's slots")
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
    means = str(raw.get("means") or "")
    if means not in ROUTE_MEANS:
        raise ConnectionError_(f"{where}: means {means!r} is not one of {', '.join(m for m in ROUTE_MEANS if m)}")
    done = _check(raw.get("done"), f"{where}.done")
    if done is None and not info.reads_back:
        # D19: every route ends on a machine check; only a verb that reads its own result back may rely on that
        raise ConnectionError_(f"{where}: {via} has no read-back of its own; the route names its done-check")
    return Route(rung=rung, via=via, variant=str(raw.get("variant") or ""), needs_focus=needs_focus, args=args,
                 when=tuple(when), game=game, game_states=states, done=done,
                 recheck=_check(raw.get("recheck"), f"{where}.recheck", recheck=True), say=str(raw.get("say") or ""),
                 driver=driver, call=call, retry_after_acting=bool(raw.get("retry_after_acting", True)), means=means)


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


def _serves(raw: Any, where: str) -> tuple[Serve, ...]:
    out: list[Serve] = []
    for i, item in enumerate([raw] if isinstance(raw, Mapping) else raw or []):
        if not isinstance(item, Mapping):
            raise ConnectionError_(f"{where}.serves[{i}]: each entry is a table {{ end, target, needs }}")
        end = str(item.get("end") or "")
        if end not in SERVE_ENDS:
            raise ConnectionError_(f"{where}.serves[{i}]: end {end!r} is not an occasion end ({', '.join(SERVE_ENDS)})")
        target = str(item.get("target") or "*")
        if target not in TARGET_KINDS:
            raise ConnectionError_(f"{where}.serves[{i}]: target {target!r} is not one of {', '.join(TARGET_KINDS)}")
        needs_raw = item.get("needs")
        needs = (str(needs_raw),) if isinstance(needs_raw, str) else _strs(needs_raw)
        bad = [n for n in needs if n not in SERVE_NEEDS]
        if bad:
            raise ConnectionError_(f"{where}.serves[{i}]: needs {bad[0]!r} is not one of {', '.join(SERVE_NEEDS)}")
        out.append(Serve(end=end, target=target, needs=tuple(dict.fromkeys(needs))))
    return tuple(out)


def _bind(raw: Any, where: str, slots: Mapping[str, Slot]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}.bind: a table of slot = source")
    out: dict[str, str] = {}
    for name, source in raw.items():
        if str(name) not in slots:
            raise ConnectionError_(f"{where}.bind: {name!r} is not one of the capability's slots")
        if str(source) not in BIND_SOURCES:
            raise ConnectionError_(f"{where}.bind.{name}: source {source!r} is not one of {', '.join(BIND_SOURCES)}")
        out[str(name)] = str(source)
    return out


def parse_capability(key: str, raw: Mapping[str, Any], *, app: str, file: str = "", digest: str = "",
                     state_reader: str = "") -> Skill:
    """One ``[capability."<key>"]`` table as a :class:`Skill` (origin ``capability``); raises
    :class:`ConnectionError_` naming what is wrong."""
    where = f"{file or 'connection'}:{key}"
    if not key or any(not part.isidentifier() for part in key.split(".")):
        raise ConnectionError_(f"{where}: a capability key is dotted plain names (web.open_page)")
    if not isinstance(raw, Mapping):
        raise ConnectionError_(f"{where}: a capability is a table")
    end = str(raw.get("end") or "")
    if end not in END_KINDS:
        raise ConnectionError_(f"{where}: end {end!r} is not a moment kind ({', '.join(END_KINDS)})")
    kind = str(raw.get("kind") or "")
    if kind not in SKILL_KINDS:
        raise ConnectionError_(f"{where}: kind {kind!r} is not one of {', '.join(SKILL_KINDS)}")
    menu = " ".join(str(raw.get("menu") or "").split())
    if not menu:
        raise ConnectionError_(f"{where}: a capability has one neutral menu line")
    done_line = str(raw.get("done_line") or "")
    if not done_line:
        raise ConnectionError_(f"{where}: a capability has a neutral done line")
    offer_class = str(raw.get("offer_class") or "never")
    if offer_class not in OFFER_CLASSES:
        raise ConnectionError_(f"{where}: offer_class {offer_class!r} is not one of {', '.join(OFFER_CLASSES)}")
    commit = str(raw.get("commit") or "")
    if commit and commit not in COMMITS:
        raise ConnectionError_(f"{where}: commit {commit!r} is not one of {', '.join(COMMITS)}")
    pre_commit_end = str(raw.get("pre_commit_end") or "")
    if pre_commit_end and pre_commit_end != end:
        # the end it stops at is the end its done-check proves (browser.site_tab: the tab in front, site_front)
        raise ConnectionError_(f"{where}: pre_commit_end is the capability's own end ({end!r})")
    slots = tuple(_slot(s, f"{where}.slot[{i}]", capability=key, app=app) for i, s in enumerate(raw.get("slot") or []))
    names = [s.name for s in slots]
    if len(set(names)) != len(names):
        raise ConnectionError_(f"{where}: two slots have one name")
    by_name = {s.name: s for s in slots}
    for s in slots:
        if s.scope and s.scope not in by_name:
            raise ConnectionError_(f"{where}.{s.name}: scope {s.scope!r} names no slot")
    for name in template_names(done_line):
        if name.split(".")[0] not in by_name:
            raise ConnectionError_(f"{where}: done line template {{{name}}} names no slot")
    routes = tuple(parse_route(r, f"{where}.route[{i}]", by_name) for i, r in enumerate(raw.get("route") or []))
    if not routes:
        raise ConnectionError_(f"{where}: a capability has at least one route")
    if len({r.key for r in routes}) != len(routes):
        raise ConnectionError_(f"{where}: two routes share rung, variant and verb")
    order = [RUNGS.index(r.rung) for r in routes]
    if order != sorted(order):
        raise ConnectionError_(f"{where}: routes are listed in ladder order (A, then B, then C)")
    sends_raw = raw.get("sends_when")
    sends_when: list[Mapping[str, Any]] = []
    for cond in ([sends_raw] if isinstance(sends_raw, Mapping) else sends_raw or []):
        if not isinstance(cond, Mapping) or str(cond.get("slot") or "") not in by_name:
            raise ConnectionError_(f"{where}: a sends_when condition names one of the capability's slots")
        sends_when.append(dict(cond))
    reader = str(raw.get("state_reader") or state_reader or "")
    if reader:
        reader = _driver_name(reader, f"{where}.state_reader")
    serves = _serves(raw.get("serves"), where)
    bind = _bind(raw.get("bind"), where, by_name)
    # D19: a second probe re-checks each done; a route without one is allowed and reported
    warnings = tuple(f"{where}.route[{i}]: no recheck (D19's second probe)" for i, r in enumerate(routes)
                     if r.via != "agent" and r.recheck is None)
    return Skill(key=key, app=str(raw.get("app") or app), end=end, kind=kind, menu=menu, done_line=done_line,
                 slots=slots, routes=routes, handoff=bool(raw.get("handoff", True)), wont=_strs(raw.get("wont")),
                 file=file, hash=digest, sends=bool(raw.get("sends", False)) or bool(sends_when),
                 sends_when=tuple(sends_when), state_reader=reader, warnings=warnings, origin="capability",
                 capability=key, serves=serves, bind=bind, pre_commit_end=pre_commit_end, commit=commit,
                 offer_class=offer_class)


#: The old name (sCore): a capability is parsed the same way.
parse_skill = parse_capability


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
    """Every capability of one connection file, and the errors (one per capability skipped, or one for the whole file:
    unreadable, asking for setup, or refused by the habit-key guard)."""
    try:
        raw_bytes = path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [], [f"{path.name}: {type(exc).__name__}"]
    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
    try:
        _no_setup(data, path.name)
        refuse_habits(data, path.name)
    except ConnectionError_ as exc:
        return [], [str(exc)]
    conn = data.get("connection") if isinstance(data.get("connection"), Mapping) else {}
    app = str(conn.get("app") or "")
    if not app:
        return [], [f"{path.name}: [connection] app is missing"]
    skills: list[Skill] = []
    errors: list[str] = []
    for key, raw in (data.get("capability") or {}).items():
        try:
            skills.append(parse_capability(str(key), raw, app=app, file=path.name, digest=digest,
                                           state_reader=str(conn.get("state_reader") or "")))
        except ConnectionError_ as exc:
            errors.append(str(exc))
    return skills, errors


@dataclass(frozen=True)
class Loaded:
    """The catalog entries loaded (capabilities from a set of folders, then the user's learned skills when
    :mod:`yuki.skills.catalog` added them), in load order, with the errors of the ones skipped."""

    skills: tuple[Skill, ...]
    errors: tuple[str, ...]
    version: str

    def get(self, key: str) -> Skill | None:
        return next((s for s in self.skills if s.key == key), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.skills)

    @property
    def capabilities(self) -> tuple[Skill, ...]:
        return tuple(s for s in self.skills if s.origin == "capability")

    @property
    def learned(self) -> tuple[Skill, ...]:
        return tuple(s for s in self.skills if s.origin == "learned")

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
    """The capabilities of every connection file in ``dirs`` (default: the bundled folder), validated, at most
    :data:`MAX_CAPABILITIES`, one per key and one per (app, end); cached until a file changes. Files only: callers use
    :func:`yuki.skills.catalog.load`, which adds the user's learned skills."""
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
                errors.append(f"{s.file}:{s.key}: another file already has this capability")
                continue
            if (s.app, s.end) in seen_ends:
                errors.append(f"{s.file}:{s.key}: one capability per (app, end) ({s.app}, {s.end})")
                continue
            if len(skills) >= MAX_CAPABILITIES:
                errors.append(f"{s.file}:{s.key}: more than {MAX_CAPABILITIES} capabilities")
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


__all__ = ["CONNECTIONS_DIR", "MAX_CAPABILITIES", "MAX_SKILLS", "MENU_LINE_CHARS", "VERBS", "PROBES", "SLOT_TYPES",
           "LIVE_FILLS", "SLOT_ROLES", "MEANS_FILLS", "END_KINDS", "SERVE_ENDS", "TARGET_KINDS", "SERVE_NEEDS",
           "BIND_SOURCES", "SKILL_KINDS", "RUNGS", "GAME_RULES", "WINDOW_STATES", "ROUTE_MEANS", "OFFER_CLASSES",
           "COMMITS", "ORIGINS", "SETUP_WORDS", "HABIT_KEYS", "HABIT_ARG_KEYS", "HABIT_CHECK_KEYS", "ConnectionError_",
           "VerbInfo", "Slot", "Check", "Route", "Serve", "Skill", "Loaded", "template_names", "lookup", "fill",
           "fill_value", "fill_partial", "site_label", "refuse_habits", "parse_route", "parse_capability",
           "parse_skill", "parse_file", "load", "clear_cache", "driver_exists", "sends_with", "strictest",
           "window_state_reader", "PROCESS_PROBES"]
