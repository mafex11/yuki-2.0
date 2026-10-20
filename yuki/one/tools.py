"""The ONE agent's tools (phase 4 design §3.2, addendum must-fix 2, 3, 5): one schema list for voice and typed.

**Shape.** Each tool is a Pipecat standard function schema (``name``, ``description``, ``properties``, ``required``),
the shape the voice registers today (``voice.pipeline``: ``FunctionSchema(**t)``); :func:`anthropic` gives the typed
driver's Anthropic shape (``input_schema``) and is what the ceiling measures (what is sent).

**The list.** Generic verbs (``window`` with move / focus_back / undo, ``tab``, ``open_url``, ``open_app``,
``click`` and ``type`` by item number or UIA ``[id]``, ``scroll``, ``slider``, ``media_key``, ``app_volume``,
``open_yuki``), ``run_skill`` (the fast routes, with ``place`` and the send declaration ``consent``), ``look``,
``quick_memory``,
``memory``, ``remember``. Typed only: ``keys``, ``shell``, ``fetch``, ``ask_user`` (addendum decision 5: shell, fetch
and keys stay off voice, which keeps the voice block under its ceiling). Voice only: ``keep_usual`` and the four voice
controls (``stay_quiet``, ``end_conversation``, ``go_quiet``, ``more_of_result``: ``voice.mouth``'s own schemas).
Reused as they are: ``look``/``memory``/``fetch``/``ask_user``/``remember`` (``brain.tools``), ``quick_memory``
(``voice.recall``), the voice controls (``voice.mouth``), ``run_skill``'s base (``skills.menu.talker_tool``).

**``then`` is REQUIRED on every acting tool** (addendum must-fix 2): ``""`` = nothing left. Code settles a turn only
when every call of the reply verified, nothing was flagged and every ``then`` is empty (:func:`then_of`,
:data:`ACTING`).

**The verb table** (:data:`SPECS`, for the gate): every tool, and every Dispatcher verb the skill engine's authorizer
passes, maps to the Dispatcher / hands verb that runs it and its class (act, sound, skill, read, write, control);
:func:`as_verb` turns a call into the Dispatcher call the consent table judges (``voice.hands.judged_call``'s mapping).

**Ceiling** (addendum should-fix): the voice block is at most :data:`ONE_TOOLS_CHARS` (15,000) characters, checked at
import with :data:`SKILL_KEYS_ROOM` kept for the conversation's skill keys; a menu whose keys don't fit drops the
``skill`` enum to a plain string (the keys stay on the Skills list in the prompt), never the ceiling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from yuki.brain.tools import ASK_USER_TOOL, FETCH_TOOL, LOOK_TOOL, MEMORY_TOOL, REMEMBER_TOOL
from yuki.skills.menu import talker_tool
from yuki.ui.own_window import PAGES as YUKI_PAGES
from yuki.voice.mouth import END_CONVERSATION, GO_QUIET, MORE_OF_RESULT, STAY_QUIET
from yuki.voice.recall import QUICK_MEMORY

VOICE, TYPED = "voice", "typed"
CHANNELS: tuple[str, ...] = (VOICE, TYPED)

#: The voice tool block's ceiling (design §3.1 ``ONE_TOOLS_CHARS``), in characters of the Anthropic-shaped list.
ONE_TOOLS_CHARS = 15_000
#: Room kept under the ceiling for one conversation's skill keys (the ``run_skill`` enum: the 8 capabilities are 124
#: characters; with the 7 learned skills on the 18 Oct database copy, 15 keys are 280; this is twice that).
SKILL_KEYS_ROOM = 600
#: The skill that takes every volume request (``voice.hands.VOLUME_SKILL``): with it on the menu ``app_volume`` and the
#: media_key volume keys are off, as on the talker today.
VOLUME_SKILL = "volume"
#: Windows' media keys (a fixed set: ``voice.hands.MEDIA_KEYS``), and the volume ones the volume skill takes over.
MEDIA_KEYS: tuple[str, ...] = ("playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown", "volumemute")
VOLUME_KEYS: tuple[str, ...] = ("volumeup", "volumedown", "volumemute")
#: The window verb's actions (``move`` is the place verb, ``actions.place``).
WINDOW_ACTIONS: tuple[str, ...] = ("focus", "minimize", "maximize", "fullscreen", "restore", "close", "move",
                                   "focus_back", "undo")
TAB_ACTIONS: tuple[str, ...] = ("show", "close", "new", "reopen")
#: ``look``'s kinds on the ONE agent: the screen only (the clipboard and system facts stay the planner's).
LOOK_KINDS: tuple[str, ...] = ("desktop", "window", "page", "tabs", "focus")

# ---------------------------------------------------------------------------------------------------------------------
# Shared fields
# ---------------------------------------------------------------------------------------------------------------------

#: ``then``, ``say_when_done`` and ``say_first`` are explained once, in the prompt's tools paragraph
#: (``yuki.one.prompt``), never in each schema (qVoice's measure: the same words in ten schemas cost ~1.3k tokens of
#: prefix and first-token time).
_THEN = {"type": "string"}
_DONE = {"type": "string"}
_SAY_FIRST = {"type": "string"}
_WINDOW = {"type": "integer", "description": "[hwnd]"}
_ID = {"type": "integer", "description": "Or its [id] from look window."}


def _tool(name: str, description: str, properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "properties": properties, "required": list(required)}


def _from_anthropic(tool: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    """A ``brain.tools`` schema (Anthropic shape) in the Pipecat shape."""
    schema = dict(tool.get("input_schema") or {})
    out = _tool(str(tool["name"]), str(tool.get("description") or ""), dict(schema.get("properties") or {}),
                list(schema.get("required") or []))
    out.update(changes)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------------------------------------------------

#: fb (final-F B8, contract K-A, writer side): ``own_step`` true marks a window call that is Yuki's own means (to read
#: or check), not their ask; missing or false: their ask (the hands' ``arranged_windows`` reads it: only their asked
#: placements are kept from code's tidy step). The intent is the model's structured field, never a word of theirs.
_OWN_STEP = {"type": "boolean", "description": "true: your own step to read or check, not their ask."}

WINDOW_TOOL = _tool(
    "window",
    "A window, read back: focus, minimize, maximize, fullscreen, restore, close (only when they asked; named: their "
    "words), move (to monitor; how), focus_back (the front back to what it was before your last act), undo (your last "
    "move or focus_back). By [hwnd] or app as they named it (minimize, close: all its windows). Not a tab; a message "
    "in a chat is run_skill message.",
    {"window": _WINDOW,
     "app": {"type": "string"},
     "action": {"type": "string", "enum": list(WINDOW_ACTIONS)},
     "monitor": {"type": "string", "description": "\"other\" or a number."},
     "how": {"type": "string", "enum": ["keep", "maximize", "left", "right"]},
     "named": {"type": "string"},
     "own_step": _OWN_STEP,
     "say_when_done": _DONE, "then": _THEN},
    ["action", "then"])

TAB_TOOL = _tool(
    "tab",
    "A browser's tabs in any of its windows. show (default): the tab named, its window forward. close: exactly the "
    "tabs named, verified gone. new: a new tab (url: that address). reopen: the last closed (expect: its title). "
    "named: their word for it (\"YouTube\"), never a title you read; app: only a browser they named.",
    {"named": {"type": "string"},
     "app": {"type": "string"},
     "action": {"type": "string", "enum": list(TAB_ACTIONS)},
     "window": {"type": "integer", "description": "[hwnd]; new and reopen need it."},
     "every": {"type": "boolean", "description": "close: only when they said all of them."},
     "url": {"type": "string"}, "expect": {"type": "string"},
     "say_when_done": _DONE, "then": _THEN},
    ["then"])

OPEN_URL_TOOL = _tool(
    "open_url",
    "Open an address in a new tab of the browser they named or their usual (also a web search), or a Windows address "
    "(ms-settings:bluetooth). Verified at the address (seconds).",
    {"say_first": _SAY_FIRST, "url": {"type": "string", "description": "The full address."},
     "app": {"type": "string", "description": "Optional browser."},
     "say_when_done": _DONE, "then": _THEN},
    ["say_first", "url", "then"])

OPEN_APP_TOOL = _tool(
    "open_app",
    "Open or bring up an app by its Start menu name, as they said it. Verified once its window is in front (seconds).",
    {"say_first": _SAY_FIRST, "name": {"type": "string"},
     "say_when_done": _DONE, "then": _THEN},
    ["say_first", "name", "then"])

CLICK_TOOL = _tool(
    "click",
    "Click one item: its number on the items line, or its [id] from look window. Refused if the window changed since. "
    "Verified when the page, focus or window visibly changes.",
    {"window": _WINDOW, "item": {"type": "integer"}, "id": {"type": "integer"},
     "say_when_done": _DONE, "then": _THEN},
    ["window", "then"])

TYPE_TOOL = _tool(
    "type",
    "Type exact text into the field that has the keyboard (or a field's [id] from look window); never in a chat "
    "(refused there): a message or draft is run_skill message. enter: press Enter after it. Verified by reading the "
    "field back.",
    {"window": _WINDOW, "text": {"type": "string"}, "id": {"type": "integer"}, "enter": {"type": "boolean"},
     "say_when_done": _DONE, "then": _THEN},
    ["window", "text", "then"])

SCROLL_TOOL = _tool(
    "scroll",
    "Scroll one window's page or list (brought forward first); verified only if it moved. amount: notches (default "
    "5, a screenful ~10). keep start: go on scrolling until they say stop (keep stop).",
    {"window": _WINDOW, "direction": {"type": "string", "enum": ["up", "down"]},
     "amount": {"type": "integer"},
     "keep": {"type": "string", "enum": ["start", "stop"]},
     "pace": {"type": "string", "enum": ["slow", "normal", "fast"]},
     "say_when_done": _DONE, "then": _THEN},
    ["window", "direction", "then"])

SLIDER_TOOL = _tool(
    "slider",
    "Set a slider ([id] from look window: a player's own volume or position) to a percent of its range, or by "
    "points; read back, never dragged.",
    {"window": _WINDOW, "id": {"type": "integer"}, "to": {"type": "number"}, "by": {"type": "number"},
     "say_when_done": _DONE, "then": _THEN},
    ["window", "id", "then"])

_MEDIA_DESCRIPTION = (
    "A media key for the media session playing, else the current one (Windows' media controls; never a game's). "
    "volumeup, volumedown, volumemute: the SYSTEM volume (every app, 2% a press); one app's is app_volume. Verified "
    "from the session's status or the volume.")
#: Final-B (contract C6, all-spotify RC4): the same principle sentence as ``voice.hands.MEDIA_KEY_TRANSPORT_DESCRIPTION``.
_MEDIA_DESCRIPTION_SKILL = (
    "A media key for the media session playing, else the current one (Windows' media controls; never a game's). "
    "Volume is run_skill volume; muting or silencing one app (its sound off while it plays on) is the volume skill "
    "with mute on, never a pause. Verified from the session's status.")
MEDIA_KEY_TOOL = _tool(
    "media_key", _MEDIA_DESCRIPTION,
    {"key": {"type": "string", "enum": list(MEDIA_KEYS)},
     "want": {"type": "string", "enum": ["playing", "paused"], "description": "playpause: the state they want."},
     "times": {"type": "integer"},
     "say_when_done": _DONE, "then": _THEN},
    ["key", "then"])

APP_VOLUME_TOOL = _tool(
    "app_volume",
    "ONE app's own mixer volume (the app as the Sound line names it); nothing on screen is touched. change: points "
    "(-20), or level 0-100; mute true silences that app alone (it plays on). Verified by reading it back.",
    {"app": {"type": "string"}, "change": {"type": "integer"}, "level": {"type": "integer"},
     "mute": {"type": "boolean"},
     "say_when_done": _DONE, "then": _THEN},
    ["app", "then"])

OPEN_YUKI_TOOL = _tool(
    "open_yuki", "Open your own window (Yuki's) in front, on one page. Verified once it is open.",
    {"page": {"type": "string", "enum": list(YUKI_PAGES)}, "say_when_done": _DONE, "then": _THEN},
    ["page", "then"])

#: The send declaration on ``run_skill`` (design §4.1 row 5): a message goes out only with it; without it the gate
#: leaves a draft (addendum must-fix 4).
CONSENT_FIELD = {
    "type": "object",
    "description": "Only for a message they asked to send: to, text (the text slot's exact words), quote (their "
                   "words asking for it). Without it: a draft.",
    "properties": {"to": {"type": "string"}, "text": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["to", "text", "quote"],
}
_PLACE = {"type": "string", "description": "\"other\" or a monitor's number, for its app's window."}
#: ``run_skill``'s slots (the talker's words, shorter: picks are the prompt's). final-G2 E1 (contract K-H, writer side):
#: a real name the model read from a misheard word of theirs carries the run's note ``from`` as a slot -> ``heard`` map
#: inside the slots (``skills.engine.note_args`` takes it out for every origin; ONE's runner passes only ``slots`` on, so
#: a top-level ``from`` would be lost); a pick for what they only described carries none and stays code's guess.
_SLOTS = {"type": "object", "description": "From their words (a misheard name: also \"from\": {\"<slot>\": \"heard\"}); "
                                           "a pick left to you: choose true, context; the rest left out."}

KEEP_USUAL_TOOL = _tool("keep_usual", "Their answer to \"Keep this as your usual?\".",
                        {"answer": {"type": "string", "enum": ["yes", "just_this_time"]}}, ["answer"])

LOOK_ONE_TOOL = _from_anthropic(
    LOOK_TOOL,
    description="Read the screen as text when the Situation isn't enough: window (its controls, with [id]s; within: "
                "one part), page (its text), tabs, focus (what has the keyboard), desktop (every window).")
LOOK_ONE_TOOL["properties"] = {**LOOK_ONE_TOOL["properties"],
                               "what": {"type": "string", "enum": list(LOOK_KINDS)}}

# ---------------------------------------------------------------------------------------------------------------------
# Typed only
# ---------------------------------------------------------------------------------------------------------------------

KEYS_TOOL = _tool(
    "keys",
    "Press keys in a window: key (one key, times) or keys (a combination, [\"ctrl\", \"l\"]). Keys that can close or "
    "submit are checked against their words first. Enter in a chat, or where Yuki's text waits unsent, is refused: "
    "a chat is reached and drafted with run_skill message. Escape closes what your own act opened there.",
    {"window": _WINDOW, "key": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}},
     "times": {"type": "integer"}, "then": _THEN},
    ["window", "then"])

#: The most characters of a command the commit check is shown whole (``brain.check.VALUE_CHARS``); past it, a plain
#: refusal (addendum must-fix 10). The PowerShell session persists, so a longer job goes in several commands.
SHELL_CHARS = 300

SHELL_TOOL = _tool(
    "shell",
    f"Run one PowerShell command on this PC in a persistent session (variables carry over between calls); what it "
    f"prints comes back. Checked against their words first; at most {SHELL_CHARS} characters a command. Never with a "
    f"game in front.",
    {"command": {"type": "string"}, "then": _THEN},
    ["command", "then"])

FETCH_ONE_TOOL = _from_anthropic(FETCH_TOOL)
ASK_USER_ONE_TOOL = _from_anthropic(ASK_USER_TOOL)
#: ``memory`` (``brain.tools``' schema, its description shortened: the kinds and their fields, deep reads after
#: ``quick_memory``).
MEMORY_ONE_TOOL = _from_anthropic(
    MEMORY_TOOL,
    description="Memory of the user beyond quick_memory, dated, with sources. recall: facts for query (since, until). "
                "activity: time per app, site or page (group_by). calls; call_timeline: one call (call, words). "
                "threads: their work. before: what came before a moment (at, event, minutes). links. people: a "
                "person (name). skills.")
#: One22 (A13, contract C13): ``remember`` kind ``way`` on the ONE agent: "remember how I do this" keeps the way the
#: turn's own acts just took as the user's own told way (the runner's ``_keep_way``: ``skills.learn.ways``). Added to
#: the planner's schema here only when it doesn't carry it yet (a later merge that adds it there stays one kind).
WAY_KIND = "way"
_WAY_SENTENCE = " way: keep how your acts just did it as their own way (text: its name, their words)."
REMEMBER_ONE_TOOL = _from_anthropic(REMEMBER_TOOL)
_kinds = dict(REMEMBER_ONE_TOOL["properties"].get("kind") or {})
if WAY_KIND not in list(_kinds.get("enum") or []):
    REMEMBER_ONE_TOOL["properties"] = {**REMEMBER_ONE_TOOL["properties"],
                                       "kind": {**_kinds, "enum": [*list(_kinds.get("enum") or []), WAY_KIND]}}
if " way:" not in str(REMEMBER_ONE_TOOL["description"]):
    REMEMBER_ONE_TOOL["description"] = str(REMEMBER_ONE_TOOL["description"]).rstrip() + _WAY_SENTENCE
del _kinds


def _plain(tool: Mapping[str, Any]) -> dict[str, Any]:
    """A Pipecat-shaped copy (the voice's own schemas already are)."""
    return _tool(str(tool["name"]), str(tool["description"]), dict(tool.get("properties") or {}),
                 list(tool.get("required") or []))


# ---------------------------------------------------------------------------------------------------------------------
# The verb table (for the gate)
# ---------------------------------------------------------------------------------------------------------------------

ACT, SOUND, SKILL, READ, WRITE, CONTROL = "act", "sound", "skill", "read", "write", "control"


@dataclass(frozen=True)
class Spec:
    """One tool's place in the gate. ``verb``: the Dispatcher tool (or hands verb) that runs it; several for a tool
    whose input picks one (``look``, ``keys``, ``fetch``: :func:`as_verb`). ``kind``: :data:`ACT` (a desktop act:
    the game, the Act switch and the consent table), :data:`SOUND` (the media session or the mixer: no window, no
    input, fine with a game in front, as the hands do today), :data:`SKILL`, :data:`READ`, :data:`WRITE` (memory writes
    of their own words), :data:`CONTROL` (the conversation's own controls). ``channels``: where the tool is offered.
    ``slow``: it takes seconds (``say_first``; Pipecat async, never cancelled by a barge-in)."""

    verb: str
    kind: str
    channels: tuple[str, ...] = CHANNELS
    slow: bool = False

    @property
    def acts(self) -> bool:
        return self.kind in (ACT, SOUND, SKILL)


#: Every ONE tool -> its verb and class.
SPECS: dict[str, Spec] = {
    "window": Spec("window_action", ACT),
    "tab": Spec("tab", ACT),
    "open_url": Spec("open_url", ACT, slow=True),
    "open_app": Spec("launch_app", ACT, slow=True),
    "click": Spec("click", ACT),
    "type": Spec("type_text", ACT),
    "scroll": Spec("scroll", ACT),
    "slider": Spec("set_slider", ACT),
    "media_key": Spec("media_key", SOUND),
    "app_volume": Spec("app_volume", SOUND),
    "open_yuki": Spec("open_yuki", ACT),
    "run_skill": Spec("run_skill", SKILL, slow=True),
    "look": Spec("look_at_window", READ, slow=True),
    "quick_memory": Spec("quick_memory", READ, slow=True),
    "memory": Spec("memory", READ, slow=True),
    "remember": Spec("remember", WRITE),
    "keep_usual": Spec("keep_usual", WRITE, (VOICE,)),
    "keys": Spec("hotkey", ACT, (TYPED,)),
    "shell": Spec("run_powershell", ACT, (TYPED,), slow=True),
    "fetch": Spec("fetch_url", READ, (TYPED,), slow=True),
    "ask_user": Spec("ask_user", CONTROL, (TYPED,)),
    "stay_quiet": Spec("stay_quiet", CONTROL, (VOICE,)),
    "end_conversation": Spec("end_conversation", CONTROL, (VOICE,)),
    "go_quiet": Spec("go_quiet", CONTROL, (VOICE,)),
    "more_of_result": Spec("more_of_result", CONTROL, (VOICE,)),
}
#: The Dispatcher verbs the skill engine's authorizer (``SkillRunner._held``, ``chat._judged``, rung B) may pass to
#: the gate by their own names (``agent.tools.ACTION_TOOL_NAMES`` and the reads). Anything else: unknown, refused.
DISPATCHER_SPECS: dict[str, Spec] = {
    "launch_app": Spec("launch_app", ACT), "focus_window": Spec("focus_window", ACT), "click": Spec("click", ACT),
    "type_text": Spec("type_text", ACT), "hotkey": Spec("hotkey", ACT), "press": Spec("press", ACT),
    "scroll": Spec("scroll", ACT), "run_powershell": Spec("run_powershell", ACT), "open_url": Spec("open_url", ACT),
    "tab": Spec("tab", ACT), "hover": Spec("hover", ACT), "app_volume": Spec("app_volume", SOUND),
    "window_action": Spec("window_action", ACT), "write_clipboard": Spec("write_clipboard", ACT),
    "set_slider": Spec("set_slider", ACT), "run_skill": Spec("run_skill", SKILL), "open_yuki": Spec("open_yuki", ACT),
    "look_at_desktop": Spec("look_at_desktop", READ), "look_at_window": Spec("look_at_window", READ),
    "read_page": Spec("read_page", READ), "read_tabs": Spec("read_tabs", READ),
    "look_at_focus": Spec("look_at_focus", READ), "read_clipboard": Spec("read_clipboard", READ),
    "read_file": Spec("read_file", READ), "fetch_url": Spec("fetch_url", READ),
}
#: ``look``'s kinds -> the Dispatcher's read (``brain.tools.LOOK_DISPATCH``).
LOOK_VERBS: dict[str, str] = {"desktop": "look_at_desktop", "window": "look_at_window", "page": "read_page",
                              "tabs": "read_tabs", "focus": "look_at_focus"}
#: The ONE tools that act (``then`` required; the settle rule reads them).
ACTING: frozenset[str] = frozenset(n for n, s in SPECS.items() if s.acts)
#: The window actions the place verb runs (``actions.place``; consent's ``window_action`` ops).
PLACE_VERB_ACTIONS: dict[str, str] = {"move": "place", "focus_back": "focus_back", "undo": "undo"}


def spec_of(name: str, *, inner: bool = False) -> Spec | None:
    """The tool's :class:`Spec`; ``None``: unknown. The agent's own call (``inner`` false) is one of its ONE tools and
    nothing else: a Dispatcher verb's name, or any other, from the model is unknown (fail closed). The skill engine's
    steps (``inner``) are Dispatcher verbs by their own names (:data:`DISPATCHER_SPECS`), a ONE name after that."""
    key = str(name or "")
    if not inner:
        return SPECS.get(key)
    return DISPATCHER_SPECS.get(key) or SPECS.get(key)


def then_of(args: Mapping[str, Any] | None) -> str:
    """The call's ``then`` (what of their request it leaves for a later call), tidied; ``""``: nothing."""
    return " ".join(str((args or {}).get("then") or "").split())


def _hwnd(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_verb(name: str, args: Mapping[str, Any] | None, *, tab_names: Sequence[str] | None = None,
            inner: bool = False) -> tuple[str, dict[str, Any]] | None:
    """The Dispatcher call the consent table judges for one ONE call: ``(verb, input)``, the words for the user
    (``say_*``, ``named``, ``then``, ``consent``) left out (``voice.hands.judged_call``'s mapping, plus the new verbs).
    A Dispatcher verb passes as it is: the skill engine's step (``inner``) by its Dispatcher name with its Dispatcher
    input (``click``'s ``hwnd`` and ``element`` kept), never read as the ONE tool of the same name. ``None``: an
    unknown tool."""
    args = dict(args or {})
    w = _hwnd(args.get("window"))
    if name in DISPATCHER_SPECS and (inner or name not in SPECS):
        return name, {k: v for k, v in args.items() if k not in ("say_first", "say_when_done", "then")}
    if name == "window":
        action = str(args.get("action") or "")
        out: dict[str, Any] = {"hwnd": w, "action": PLACE_VERB_ACTIONS.get(action, action)}
        if action == "move":
            out.update(monitor=str(args.get("monitor") or "other"), how=str(args.get("how") or "keep"))
        return "window_action", out
    if name == "tab":
        action = str(args.get("action") or "show")
        if action == "new" and str(args.get("url") or "").strip():
            # a new tab with an address is an opening in that browser (judged as the opening, as the hands do)
            return "open_url", {"url": str(args["url"]).strip(), **({"app": args["app"]} if args.get("app") else {})}
        tab: dict[str, Any] = {"hwnd": w, "action": action}
        if action == "close":
            tab["tabs"] = list(tab_names or [])
        elif action == "reopen":
            tab["expect"] = str(args.get("expect") or "")
        elif tab_names:
            tab["tab"] = tab_names[0]
        return "tab", tab
    if name == "open_url":
        return "open_url", {"url": args.get("url"), **({"app": args["app"]} if args.get("app") else {})}
    if name == "open_app":
        return "launch_app", {"query": args.get("name")}
    if name == "click":
        out = {"hwnd": w}
        if args.get("id") is not None:
            out["element"] = args.get("id")
        if args.get("item") is not None:
            out["item"] = args.get("item")
        return "click", out
    if name == "type":
        out = {"hwnd": w, "text": str(args.get("text") or "")}
        if args.get("id") is not None:
            out["element"] = args.get("id")
        if args.get("enter") is True:
            out["press_enter"] = True
        return "type_text", out
    if name == "scroll":
        return "scroll", {"hwnd": w, "direction": args.get("direction"), "amount": args.get("amount"),
                          **({"until_stopped": True} if args.get("keep") == "start" else {})}
    if name == "slider":
        return "set_slider", {"hwnd": w, "element": args.get("id"),
                              **{k: args[k] for k in ("to", "by") if args.get(k) is not None}}
    if name == "media_key":
        return "media_key", {k: args[k] for k in ("key", "want", "times") if args.get(k) is not None}
    if name == "app_volume":
        return "app_volume", {k: args[k] for k in ("app", "change", "level", "mute") if args.get(k) is not None}
    if name == "open_yuki":
        return "open_yuki", {"page": args.get("page")}
    if name == "run_skill":
        return "run_skill", {k: args[k] for k in ("skill", "slots", "again", "corrects", "place")
                             if args.get(k) is not None}
    if name == "keys":
        keys = args.get("keys")
        if isinstance(keys, list) and keys:
            return "hotkey", {"hwnd": w, "keys": [str(k) for k in keys]}
        return "press", {"hwnd": w, "key": str(args.get("key") or ""),
                         **({"times": args["times"]} if isinstance(args.get("times"), int) else {})}
    if name == "shell":
        return "run_powershell", {"command": str(args.get("command") or "")}
    if name == "fetch":
        if args.get("path"):
            return "read_file", {"path": str(args["path"]),
                                 **({"start_line": args["start_line"]} if isinstance(args.get("start_line"), int)
                                    else {})}
        return "fetch_url", {"url": str(args.get("url") or "")}
    if name == "look":
        what = str(args.get("what") or "")
        return LOOK_VERBS.get(what, "look_at_window"), {k: v for k, v in args.items() if k != "what"}
    spec = SPECS.get(name)
    return (spec.verb, args) if spec is not None else None


# ---------------------------------------------------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------------------------------------------------


def run_skill_tool(skill_keys: Sequence[str]) -> dict[str, Any]:
    """``run_skill`` on the ONE agent: the talker's (``skills.menu.talker_tool``) with ``place``, the send
    declaration ``consent``, and a required ``then``."""
    base = talker_tool(list(skill_keys))
    required = [r for r in base.get("required") or [] if r != "then"]
    props = {**base["properties"], "slots": _SLOTS, "place": _PLACE, "consent": CONSENT_FIELD, "then": _THEN}
    return _tool("run_skill", str(base["description"]), props, [*required, "then"])


def schemas(channel: str, skill_keys: Sequence[str] = (), volume_skill: bool | None = None) -> list[dict[str, Any]]:
    """The tool list for one conversation (voice) or request (typed), in a fixed order (it heads the cached prefix):
    the verbs, ``run_skill`` and ``keep_usual`` when the Skills menu has keys, the reads and writes, then the channel's
    own (typed: keys, shell, fetch, ask_user; voice: the four controls). ``volume_skill`` (default: ``volume`` among
    the keys) takes ``app_volume`` and the media_key volume keys off, as on the talker. Pipecat shape (:func:`anthropic`
    for the typed driver). A voice block over :data:`ONE_TOOLS_CHARS` with these keys gets a plain-string ``skill``."""
    voice = channel == VOICE
    keys = [str(k) for k in (skill_keys or ()) if str(k or "").strip()]
    volume = (VOLUME_SKILL in keys) if volume_skill is None else bool(volume_skill)
    out: list[dict[str, Any]] = [WINDOW_TOOL, TAB_TOOL, OPEN_URL_TOOL, OPEN_APP_TOOL, CLICK_TOOL, TYPE_TOOL,
                                 SCROLL_TOOL, SLIDER_TOOL]
    if volume:
        props = dict(MEDIA_KEY_TOOL["properties"])
        props["key"] = {**props["key"], "enum": [k for k in MEDIA_KEYS if k not in VOLUME_KEYS]}
        out.append({**MEDIA_KEY_TOOL, "description": _MEDIA_DESCRIPTION_SKILL, "properties": props})
    else:
        out += [MEDIA_KEY_TOOL, APP_VOLUME_TOOL]
    out.append(OPEN_YUKI_TOOL)
    if keys:
        out.append(run_skill_tool(keys))
        if voice:
            out.append(KEEP_USUAL_TOOL)
    out += [LOOK_ONE_TOOL, QUICK_MEMORY, MEMORY_ONE_TOOL, REMEMBER_ONE_TOOL]
    if voice:
        out += [STAY_QUIET, END_CONVERSATION, GO_QUIET, MORE_OF_RESULT]
    else:
        out += [KEYS_TOOL, SHELL_TOOL, FETCH_ONE_TOOL, ASK_USER_ONE_TOOL]
    out = [_plain(t) for t in out]
    if voice and keys and block_chars(out) > ONE_TOOLS_CHARS:
        for i, tool in enumerate(out):
            if tool["name"] == "run_skill":
                props = dict(tool["properties"])
                props["skill"] = {"type": "string", "description": "A key from the Skills list."}
                out[i] = {**tool, "properties": props}
    return out


def anthropic(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Anthropic ``tools`` parameter for a Pipecat-shaped list (what Pipecat's adapter sends, and the typed
    driver's own shape)."""
    return [{"name": t["name"], "description": t["description"],
             "input_schema": {"type": "object", "properties": dict(t.get("properties") or {}),
                              "required": list(t.get("required") or [])}} for t in tools]


def block_chars(tools: Sequence[Mapping[str, Any]]) -> int:
    """A block's size as sent (the Anthropic shape, JSON), the number the ceiling reads."""
    return len(json.dumps(anthropic(tools), ensure_ascii=False))


class ToolBudgetError(RuntimeError):
    """The voice tool block grew past its ceiling: a tool or description must replace text, never add beside it."""


def check_budget() -> int:
    """The voice block's size with no skill keys, plus :data:`SKILL_KEYS_ROOM`, against :data:`ONE_TOOLS_CHARS`
    (enforced at import). Returns the size measured with the shipped capability keys' room."""
    probe = ["k" * 8]   # one key: run_skill and keep_usual are in the block; the room covers the real enum
    size = block_chars(schemas(VOICE, probe, volume_skill=False)) - len(json.dumps(probe))
    if size + SKILL_KEYS_ROOM > ONE_TOOLS_CHARS:
        raise ToolBudgetError(f"the ONE voice tool block is {size} chars (+{SKILL_KEYS_ROOM} for skill keys), over its "
                              f"{ONE_TOOLS_CHARS} ceiling")
    for name in ACTING:   # must-fix 2: every acting tool requires then
        tool = next((t for t in schemas(TYPED, probe, volume_skill=False) + schemas(VOICE, probe, volume_skill=False)
                     if t["name"] == name), None)
        if tool is not None and "then" not in tool["required"]:
            raise ToolBudgetError(f"the ONE tool {name} must require then")
    return size


VOICE_BLOCK_CHARS = check_budget()


__all__ = ["ACT", "ACTING", "CHANNELS", "CONSENT_FIELD", "CONTROL", "DISPATCHER_SPECS", "LOOK_KINDS", "LOOK_VERBS",
           "MEDIA_KEYS", "ONE_TOOLS_CHARS", "PLACE_VERB_ACTIONS", "READ", "SHELL_CHARS", "SKILL", "SKILL_KEYS_ROOM",
           "SOUND", "SPECS", "Spec", "TAB_ACTIONS", "TYPED", "ToolBudgetError", "VOICE", "VOICE_BLOCK_CHARS",
           "VOLUME_KEYS", "VOLUME_SKILL", "WINDOW_ACTIONS", "WRITE", "anthropic", "as_verb", "block_chars",
           "check_budget", "run_skill_tool", "schemas", "spec_of", "then_of"]
