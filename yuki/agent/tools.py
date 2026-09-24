"""Tool schemas exposed to the model, and the dispatcher that runs them.

The dispatcher never imports :mod:`yuki.perception` or :mod:`yuki.actions` at
module import time. It takes a *backend*: any object carrying the functions named
in the architecture contract. :func:`default_backend` resolves them from the real
modules on first use, so importing :mod:`yuki.agent` does not drag in the
Windows-only GUI libraries.

Three tools are *control* tools -- ``ask_user``, ``done`` and ``note_to_self``.
They have no backend function: the dispatcher validates them and hands them back
to :class:`yuki.agent.loop.Agent`, which owns pausing, finishing and the running
summary.

The four *memory* tools (``recall``, ``remember_how``, ``correct_memory``,
``activity``) have no backend function either: the agent runs them against
Yuki's memory through :mod:`yuki.agent.memory`, and they are only listed when
memory is installed.
"""

from __future__ import annotations

import base64
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

#: Tool names whose results are large perception snapshots. Used only by context
#: hygiene (:mod:`yuki.agent.context`) to decide what may be stubbed once stale.
#: This is bookkeeping about payload size, not a rule about behaviour.
PERCEPTION_TOOLS: frozenset[str] = frozenset(
    {"look_at_desktop", "look_at_window", "read_page", "take_screenshot", "system_facts", "recall", "activity"}
)

#: Tools handled by the agent loop rather than a backend function.
CONTROL_TOOLS: frozenset[str] = frozenset({"ask_user", "done", "note_to_self"})

#: Tools backed by Yuki's memory (:mod:`yuki.agent.memory`) rather than the
#: desktop backend. Left out of the tool block entirely when the memory API is
#: not installed (see :func:`tool_params`), like a policy-gated tool.
MEMORY_TOOLS: frozenset[str] = frozenset({"recall", "remember_how", "correct_memory", "activity"})

#: The tool the screenshot policy governs. When the policy is ``never`` this name
#: is dropped from the definitions sent to the model (see :func:`tool_params`)
#: rather than only refused on arrival: a tool the model can see is a tool it will
#: eventually try, and every attempt costs a full round trip to be told no.
#: Dropping a name never reorders the rest, so the tool block stays byte-stable
#: for prompt caching.
POLICY_GATED_TOOLS: dict[str, str] = {"take_screenshot": "screenshot_policy"}


def _obj(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """Build a strict JSON-Schema object for a tool's ``input_schema``."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


#: The input guard, shared by every tool that sends keys or mouse events. One
#: object rather than five copies of the same paragraph: it is the same promise in
#: each of them, and the tool block is a cached prefix, so wording that drifted
#: between tools would be both misleading and dead weight.
_GUARD: dict[str, Any] = {
    "type": "integer",
    "description": "Send only while this window is still in front. If focus has "
    "moved, nothing is sent and you are told what has it instead.",
}

#: An element of the latest tree read of a window, as an input target. Shared by
#: click, type_text and scroll for the same reason as :data:`_GUARD`.
_ELEMENT: dict[str, Any] = {
    "type": "integer",
    "description": "Id of an element in the latest read of the window (the [id] in "
    "its tree, from look_at_window or the view attached after your actions). It is "
    "resolved to that element's centre and input is guarded to that window: "
    "expect_hwnd names the window, or else the window read most recently is used.",
}

#: The read an element id comes from.
_READ: dict[str, Any] = {
    "type": "integer",
    "description": "The read number of the tree the element id comes from (read #N "
    "in its header). If that window has been read again since, nothing is sent.",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "look_at_desktop",
        "label": "Looking at the desktop",
        "description": (
            "List every visible top-level window with its hwnd, title, process, "
            "bounds and which one is in the foreground, plus the cursor, the size of "
            "the whole desktop and, when there is more than one display, each "
            "monitor's index and rectangle. All coordinates are in one space "
            "spanning every monitor, which is the same space click, scroll and "
            "element centres use. Cheap and fast. You already receive this at the "
            "start of every turn, already fresh, so you rarely need to call it."
        ),
        "input_schema": _obj({}),
    },
    {
        "name": "look_at_window",
        "label": "Reading a window",
        "description": (
            "Read the UI Automation element tree of one window: roles, names, text "
            "field values, click points, keyboard shortcuts and whether each element "
            "is interactive. This is how you find things to click or type into. The "
            "result reports how many elements it found, whether it was truncated, and "
            "whether the window was still building its tree while it was read, in "
            "which case reading it again can show more. Its status is ok when the "
            "window answered (however small the tree), busy when the window is on "
            "screen but did not answer in time, which usually means it is loading or "
            "rendering and can be read again shortly, and empty when it answered "
            "with nothing usable. After a turn whose actions all succeeded, the window "
            "you acted on is read for you and attached to your next turn as 'Window "
            "after your actions', so there is no need to call this just to see what "
            "an action did; call it for other windows, or when that attached view is "
            "not enough."
        ),
        "input_schema": _obj(
            {"hwnd": {"type": "integer", "description": "Window handle from look_at_desktop."}},
            ["hwnd"],
        ),
    },
    {
        "name": "read_page",
        "label": "Reading a page",
        "description": (
            "Read the full text of the page currently showing in a browser or "
            "web-based app window, including parts scrolled out of view, with its "
            "title and address. Use it to read, summarise or find information on a "
            "page; use look_at_window when you need elements to click or type into."
        ),
        "input_schema": _obj(
            {"hwnd": {"type": "integer", "description": "Window handle from look_at_desktop."}},
            ["hwnd"],
        ),
    },
    {
        "name": "take_screenshot",
        "label": "Taking a look at the screen",
        "description": (
            "Capture pixels. Name at most one target: a window (hwnd), a screen "
            "rectangle (region), or one display (monitor). With none you get the "
            "whole desktop across all monitors, which is the only shot that is "
            "scaled down. Use it when the element tree is empty, truncated or does "
            "not explain what you are looking at, or when you need to read something "
            "only rendering shows. Every shot tells you where its top-left corner is "
            "on screen and whether it is 1:1; prefer a window, a monitor or a region "
            "so that it is, and zoom in on a small region when you need to read or "
            "aim at something precisely."
        ),
        "input_schema": _obj(
            {
                "hwnd": {
                    "type": "integer",
                    "description": "Window to capture.",
                },
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "Screen rectangle [left, top, right, bottom] in the "
                    "same coordinates as window bounds and element centres.",
                },
                "monitor": {
                    "type": "integer",
                    "description": "Index of one display, as listed by look_at_desktop.",
                },
            }
        ),
    },
    {
        "name": "system_facts",
        "label": "Reading system facts",
        "description": (
            "Current local date and time, uptime, CPU load, memory totals and the "
            "processes using the most memory."
        ),
        "input_schema": _obj({}),
    },
    {
        "name": "launch_app",
        "label": "Opening an app",
        "description": (
            "Start an installed application by name and wait for its window. If the "
            "name matches several apps it returns the candidates instead of guessing, "
            "so you can pick one or ask the user. The window that appeared or changed "
            "is brought to the front and waited on until it accepts input, so there "
            "is no need to focus it afterwards; the result says whether that worked. "
            "With args the application is started with those command-line arguments, "
            "such as a URL, a file or a folder for it to open; passing a URL as an "
            "argument to a browser opens it there without using the address bar. "
            "With args it also returns only once the content is ready - the result "
            "says 'content ready' with the page or document now shown, or 'content "
            "still not ready' after a few seconds' wait. An application that is "
            "already running usually opens the arguments in the window it has, and "
            "the result names the window that appeared or changed. If the application "
            "cannot be given arguments, nothing is started and the result says why."
        ),
        "input_schema": _obj(
            {
                "query": {"type": "string", "description": "Application name, e.g. 'spotify'."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments, one item per argument, "
                    "e.g. a URL or a file path. Omit to start the application plainly.",
                },
            },
            ["query"],
        ),
    },
    {
        "name": "focus_window",
        "label": "Switching window",
        "description": (
            "Bring a window to the foreground, restoring it first if minimised. Do "
            "this before typing or using keyboard shortcuts aimed at that window."
        ),
        "input_schema": _obj({"hwnd": {"type": "integer"}}, ["hwnd"]),
    },
    {
        "name": "click",
        "label": "Clicking",
        "description": (
            "Click an element or a screen coordinate. Prefer an element id from the "
            "latest tree read of the window; give x and y only when no element "
            "describes the target."
        ),
        "input_schema": _obj(
            {
                "element": _ELEMENT,
                "read": _READ,
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "description": "2 for a double click."},
                "expect_hwnd": _GUARD,
            },
        ),
    },
    {
        "name": "type_text",
        "label": "Typing",
        "description": (
            "Type text into whatever currently has keyboard focus, optionally pressing "
            "Enter afterwards. Make sure the right field is focused first. The result "
            "says which control had keyboard focus when typing began. When the "
            "control's contents can be read, the result says what it holds after "
            "typing; if the typed text is not in it, the action fails and Enter is "
            "not pressed. With clear, the field's existing contents are selected and "
            "deleted first, and the result says whether it read empty. The result "
            "also names any new window the application showed meanwhile, such as a "
            "suggestion list or menu. With element, that element is clicked first "
            "to focus it."
        ),
        "input_schema": _obj(
            {
                "text": {"type": "string"},
                "element": _ELEMENT,
                "read": _READ,
                "press_enter": {"type": "boolean"},
                "clear": {
                    "type": "boolean",
                    "description": "Select all and delete in the focused field before "
                    "typing. Text may be empty to only clear.",
                },
                "expect_hwnd": _GUARD,
            },
            ["text"],
        ),
    },
    {
        "name": "hotkey",
        "label": "Pressing a shortcut",
        "description": (
            "Press a key combination, given as the keys held together, e.g. "
            "['ctrl','t'] or ['win','r'] or ['volume_mute']. The result says where "
            "keyboard focus is afterwards and whether it moved, and names any new "
            "window the application showed."
        ),
        "input_schema": _obj(
            {
                "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "expect_hwnd": _GUARD,
            },
            ["keys"],
        ),
    },
    {
        "name": "press",
        "label": "Pressing a key",
        "description": (
            "Press a single key, optionally several times, e.g. 'enter', 'tab', "
            "'down'. The result says where keyboard focus is afterwards and whether "
            "it moved, and names any new window the application showed."
        ),
        "input_schema": _obj(
            {
                "key": {"type": "string"},
                "times": {"type": "integer", "minimum": 1},
                "expect_hwnd": _GUARD,
            },
            ["key"],
        ),
    },
    {
        "name": "scroll",
        "label": "Scrolling",
        "description": (
            "Scroll by wheel notches over an element (preferred) or a point; "
            "positive dy scrolls up."
        ),
        "input_schema": _obj(
            {
                "element": _ELEMENT,
                "read": _READ,
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "dy": {"type": "integer"},
                "dx": {"type": "integer"},
                "expect_hwnd": _GUARD,
            },
        ),
    },
    {
        "name": "run_powershell",
        "label": "Running a command",
        "description": (
            "Run a PowerShell command on this PC and get stdout, stderr and the exit "
            "code back. Often the fastest and most reliable way to inspect or change "
            "the system, far better than clicking through a UI."
        ),
        "input_schema": _obj({"command": {"type": "string"}}, ["command"]),
    },
    {
        "name": "open_url",
        "label": "Opening a link",
        "description": (
            "Open a URL, file or folder. Without app it opens with the default "
            "handler for that kind of target. With app it is handed to that installed "
            "application as an argument instead, the same as launch_app with args; "
            "for a browser that loads the page there directly, without using the "
            "address bar. Either way the window showing it is brought to the front "
            "and, when it shows a page or document, the call returns once that "
            "content is ready: the result says "
            "'content ready' with the page or document now shown, or 'content still "
            "not ready' after a few seconds' wait."
        ),
        "input_schema": _obj(
            {
                "url": {"type": "string", "description": "URL, file path or folder path."},
                "app": {
                    "type": "string",
                    "description": "Installed application name, as launch_app takes "
                    "it, to open the target with.",
                },
            },
            ["url"],
        ),
    },
    {
        "name": "note_to_self",
        "label": "Making a note",
        "description": (
            "Replace your short working note for this conversation. You get it back at "
            "the start of every turn. Use it to carry forward what you learned and what "
            "is left to do, so you stay oriented after older observations have aged out "
            "of view. Keep it a few lines at most."
        ),
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "ask_user",
        "label": "Asking you something",
        "description": (
            "Put one question to the user and wait for their answer. Their reply comes "
            "back as this tool's result."
        ),
        "input_schema": _obj({"question": {"type": "string"}}, ["question"]),
    },
    {
        "name": "done",
        "label": "Wrapping up",
        "description": (
            "What to tell the user once this turn's work has succeeded. Call it "
            "exactly once. It is evaluated last, after every other tool in the turn, "
            "and only if all of them succeeded -- so calling it alongside the actions "
            "that finish the job is the normal thing to do, not a gamble: if one of "
            "them fails your message is discarded and you get the results back to "
            "react to instead. You can never end up having claimed something that "
            "did not happen."
        ),
        "input_schema": _obj(
            {
                "message": {
                    "type": "string",
                    "description": "What you would text back: usually one short "
                    "sentence, in the user's own register. No restating the request "
                    "or narrating your steps.",
                }
            },
            ["message"],
        ),
    },
    {
        "name": "recall",
        "label": "Remembering",
        "description": (
            "Search the user's memory journal — dated facts about what they did, "
            "watched, read, and who said what to them — for anything you need to "
            "know about their past activity, people or plans. Returns the matching "
            "facts as dated lines."
        ),
        "input_schema": _obj(
            {
                "query": {"type": "string", "description": "What to look for, in plain words."},
                "since": {
                    "type": "string",
                    "description": "Earliest local date or date-time to include, ISO "
                    "format, e.g. 2026-09-20 or 2026-09-20T18:00.",
                },
                "until": {
                    "type": "string",
                    "description": "Latest local date or date-time to include, same "
                    "format; a date alone includes that whole day.",
                },
                "app": {
                    "type": "string",
                    "description": "Only facts from this application, as recall results "
                    "name it.",
                },
            },
            ["query"],
        ),
    },
    {
        "name": "remember_how",
        "label": "Saving how it's done",
        "description": (
            "Save a short procedure that worked on this PC, so next time you can do "
            "it directly — e.g. how an app accepts a URL, where something lives. "
            "Only record what you actually confirmed."
        ),
        "input_schema": _obj(
            {
                "app": {
                    "type": "string",
                    "description": "The program it applies to, by its process name as "
                    "the window list shows it. Omit when it is not about one program.",
                },
                "text": {"type": "string", "description": "The procedure, in a sentence or two."},
            },
            ["text"],
        ),
    },
    {
        "name": "correct_memory",
        "label": "Noting that",
        "description": (
            "Record a correction the user gave you about themselves or their "
            "preferences."
        ),
        "input_schema": _obj(
            {"text": {"type": "string", "description": "The correction, as the user meant it."}},
            ["text"],
        ),
    },
    {
        "name": "activity",
        "label": "Looking at how your time went",
        "description": (
            "How the user spent their time: time per app, site or page, visits, "
            "longest stretches, and episode summaries of what they were doing."
        ),
        "input_schema": _obj(
            {
                "since": {
                    "type": "string",
                    "description": "Earliest local date or date-time, ISO format, e.g. "
                    "2026-09-20 or 2026-09-20T18:00. Default: the start of today.",
                },
                "until": {
                    "type": "string",
                    "description": "Latest local date or date-time, same format; a date "
                    "alone includes that whole day. Default: now.",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["site", "app", "page"],
                    "description": "Count time per site (a window without a web page "
                    "counts as its app), per app, or per page. Default: site.",
                },
            }
        ),
    },
]

#: Name -> schema, for validation and lookups.
TOOLS_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOL_SCHEMAS}

#: Every tool name, in registry order. Immutable on purpose: the order is part of
#: the cached prefix, so a caller that reordered it would silently cost cache hits.
ALL_TOOL_NAMES: tuple[str, ...] = tuple(TOOLS_BY_NAME)

#: The tools that reach out and change something on the machine, as opposed to the
#: ones that only look. Handy as a ready-made argument for a look-but-do-not-touch
#: Yuki (``tool_names=[n for n in ALL_TOOL_NAMES if n not in ACTION_TOOL_NAMES]``).
#: It is a list of names, not a rule: nothing here decides what the model does.
ACTION_TOOL_NAMES: tuple[str, ...] = (
    "launch_app",
    "focus_window",
    "click",
    "type_text",
    "hotkey",
    "press",
    "scroll",
    "run_powershell",
    "open_url",
)

#: Actions after which the loop attaches a fresh read of the window acted on to
#: the next turn (see :meth:`Dispatcher.window_view`), so the model does not
#: spend a whole round trip calling ``look_at_window`` only to see the result.
#: ``run_powershell`` is not here: it acts on the system, not on a window. A list
#: of names for plumbing, not a rule about what the model should do.
AUTO_VIEW_TOOLS: frozenset[str] = frozenset(
    {"launch_app", "focus_window", "open_url", "click", "type_text", "hotkey", "press", "scroll"}
)

#: How long a window's element count and covered area must hold still before the
#: automatic view after an action is taken as settled.
AUTO_VIEW_QUIET_S = 0.3

#: Ceiling on the whole automatic view, reads included: a window still changing
#: after this is attached as it is, and says so.
AUTO_VIEW_CAP_S = 1.5

#: Gap between re-reads while waiting for the window to settle. Each read is a
#: full tree walk (tens of milliseconds on a built window), so this only keeps
#: the poll from spinning; the wait itself ends on the condition, not the clock.
_AUTO_VIEW_POLL_S = 0.1

#: Grid cell (px) for the covered-area part of the settle signature, the same
#: coarse measure the tree module uses for coverage.
_VIEW_CELL = 32


def effective_screenshot_policy(policy: str | None = None) -> str:
    """The screenshot policy in force, resolving ``None`` from :class:`Settings`.

    :func:`tool_params` has to know the policy in order to leave
    ``take_screenshot`` out of the block it builds, and its caller in
    :mod:`yuki.agent.loop` passes only a tool subset. Rather than have every
    caller thread the value through, ``None`` means "whatever a default
    :class:`yuki.config.Settings` says", which is the process-wide configured
    answer. A caller holding a modified ``Settings`` should pass its value
    explicitly.

    Args:
        policy: an explicit policy, or ``None`` to read the configured default.

    Returns:
        One of ``"never"``, ``"ask"``, ``"auto"``; anything unrecognised is
        returned unchanged so a typo surfaces rather than silently meaning
        ``auto``.
    """
    if policy is not None:
        return policy
    try:
        from yuki.config import Settings

        return str(Settings().screenshot_policy)
    except Exception:  # pragma: no cover - config must never break tool building
        return "never"


def available_tool_names(
    names: Iterable[str] | None = None,
    *,
    screenshot_policy: str | None = None,
    memory: bool = True,
) -> tuple[str, ...]:
    """Every tool the model may be shown, in registry order.

    Args:
        names: a requested subset, as understood by :func:`resolve_tool_names`.
        screenshot_policy: the policy in force; ``None`` reads the configured
            default.
        memory: whether Yuki's memory is installed; without it the
            :data:`MEMORY_TOOLS` are left out.

    Returns:
        The names, minus any whose policy switches them off entirely.
    """
    allowed = resolve_tool_names(names)
    dropped: set[str] = set()
    if effective_screenshot_policy(screenshot_policy) == "never":
        dropped |= set(POLICY_GATED_TOOLS)
    if not memory:
        dropped |= set(MEMORY_TOOLS)
    return tuple(
        name
        for name in ALL_TOOL_NAMES
        if (allowed is None or name in allowed) and name not in dropped
    )


def resolve_tool_names(names: Iterable[str] | None) -> tuple[str, ...] | None:
    """Validate a requested tool subset and return it in registry order.

    The three control tools are always added: without ``done`` a turn can never
    end, without ``ask_user`` the agent cannot pause, and without
    ``note_to_self`` it loses its memory as older context ages out.

    Args:
        names: The tools to expose, or ``None`` for all of them.

    Returns:
        ``None`` when ``names`` is ``None``, otherwise the resolved names in
        :data:`ALL_TOOL_NAMES` order, so the cached tool block stays byte-stable
        for a given subset however the caller happened to order it.

    Raises:
        ValueError: If any name is not in the registry.
    """
    if names is None:
        return None
    requested = list(names)
    unknown = [name for name in requested if name not in TOOLS_BY_NAME]
    if unknown:
        raise ValueError(
            f"unknown tool name(s): {', '.join(sorted(set(unknown)))}; "
            f"available: {', '.join(ALL_TOOL_NAMES)}"
        )
    wanted = set(requested) | set(CONTROL_TOOLS)
    return tuple(name for name in ALL_TOOL_NAMES if name in wanted)


def tool_params(
    *,
    cacheable: bool = True,
    names: Iterable[str] | None = None,
    screenshot_policy: str | None = None,
    memory: bool = True,
) -> list[dict[str, Any]]:
    """Return the tool definitions for a request.

    Args:
        cacheable: Mark the last tool definition with ``cache_control`` so the
            whole (byte-stable) tool block is cached by the API.
        names: Restrict the list to these tools (plus the control tools), as
            resolved by :func:`resolve_tool_names`. ``None`` sends all of them.
        screenshot_policy: The screenshot policy in force. Under ``never``,
            ``take_screenshot`` is left out of the block entirely instead of being
            offered and then refused -- a refusal the model has to spend a whole
            round trip discovering. ``None`` reads the configured default.
        memory: Whether Yuki's memory is installed. Without it the memory tools
            are left out the same way. Fixed per process, so the block stays
            byte-stable.

    Returns:
        A fresh list of tool dicts in registry order, carrying only what the API
        accepts -- the human ``label`` is dropped here, since it exists for the UI
        and an unexpected key would be rejected. The caller may not mutate the
        module copy.
    """
    allowed = set(
        available_tool_names(names, screenshot_policy=screenshot_policy, memory=memory)
    )
    tools = [
        {k: v for k, v in t.items() if k != "label"}
        for t in TOOL_SCHEMAS
        if t["name"] in allowed
    ]
    if cacheable and tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def tool_label(name: str) -> str:
    """The one-line human phrase for a tool, for the UI to show while it runs.

    Args:
        name: Tool name as the model called it.

    Returns:
        The tool's ``label``, or an empty string for a name that is not a tool --
        the UI is not the place to raise over an unknown tool.
    """
    schema = TOOLS_BY_NAME.get(name)
    return str(schema.get("label") or "") if schema else ""


def unavailable_tool(name: str, available: Iterable[str]) -> "ToolOutcome":
    """The outcome for a tool the model asked for but cannot have.

    Covers both "there is no such tool" and "that tool is not in this instance's
    subset": from the model's side they are the same situation, and the way out is
    the same -- pick something from the list it is given.
    """
    return ToolOutcome(
        name=name,
        ok=False,
        summary=f"tool {name!r} is not available",
        content=[
            {
                "type": "text",
                "text": f"There is no tool named {name!r} available to you. "
                "Available: " + ", ".join(sorted(available)),
            }
        ],
        payload={"error": "unknown_tool", "name": name},
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """The perception/action surface the dispatcher needs.

    The names and signatures are exactly those in the architecture contract,
    which is what :mod:`yuki.perception` and :mod:`yuki.actions` export.
    """

    def get_desktop_overview(self) -> Any: ...
    def format_overview(self, overview: Any) -> str: ...
    def get_window_tree(
        self, hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0
    ) -> Any: ...
    def format_window_tree(self, tree: Any) -> str: ...
    # The text of the page a window shows (a PageText: hwnd, title, url, text,
    # chars, truncated, source, found, note, error) and its rendering.
    def page_text(
        self, hwnd: int, *, max_chars: int = 30000, timeout_s: float = 6.0
    ) -> Any: ...
    def format_page_text(self, result: Any) -> str: ...
    def screenshot(
        self,
        hwnd: int | None = None,
        *,
        region: tuple[int, int, int, int] | None = None,
        monitor: int | None = None,
        max_width: int = 2560,
    ) -> Any: ...  # a Capture (png, origin, scale, ...); bare PNG bytes also accepted
    def capture_bounds(
        self,
        hwnd: int | None = None,
        *,
        region: tuple[int, int, int, int] | None = None,
        monitor: int | None = None,
    ) -> tuple[int, int, int, int]: ...
    # Optional: a backend that has it reports the image's origin and scale itself
    # instead of the dispatcher reconstructing them. See
    # :meth:`Dispatcher._do_take_screenshot`.
    def capture(
        self,
        hwnd: int | None = None,
        *,
        region: tuple[int, int, int, int] | None = None,
        monitor: int | None = None,
        max_width: int = 2560,
    ) -> Any: ...  # pragma: no cover - protocol only
    def system_facts(self) -> dict[str, Any]: ...
    def launch_app(
        self, query: str, *, args: list[str] | None = None, timeout_s: float = 8.0
    ) -> Any: ...
    def focus_window(self, hwnd: int, *, timeout_s: float = 2.0) -> Any: ...
    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        expect_hwnd: int | None = None,
    ) -> Any: ...
    def type_text(
        self,
        text: str,
        *,
        press_enter: bool = False,
        clear: bool = False,
        expect_hwnd: int | None = None,
    ) -> Any: ...
    def hotkey(self, *keys: str, expect_hwnd: int | None = None) -> Any: ...
    def press(
        self, key: str, *, times: int = 1, expect_hwnd: int | None = None
    ) -> Any: ...
    def scroll(
        self,
        x: int,
        y: int,
        *,
        dy: int = 0,
        dx: int = 0,
        expect_hwnd: int | None = None,
    ) -> Any: ...
    def run_powershell(self, command: str, *, timeout_s: float = 20.0) -> Any: ...
    def open_url(self, url: str, *, app: str | None = None) -> Any: ...

    # Optional, and not part of the architecture contract: a backend without it
    # simply never gets pre-warmed. See :meth:`Dispatcher.prewarm_shell`.
    def prewarm_shell(self) -> bool: ...  # pragma: no cover - protocol only
    # Optional too: what the user is engaged in (see
    # :func:`yuki.perception.system.activity_facts`), and its warm-up.
    def activity_facts(self) -> dict[str, Any]: ...  # pragma: no cover - protocol only
    def warm_activity(self) -> None: ...  # pragma: no cover - protocol only


class _LazyRealBackend:
    """Backend that resolves attributes from the real perception/action modules.

    Import is deferred to first use so importing :mod:`yuki.agent` does not drag
    in the Windows-only libraries (UI Automation, pyautogui, Pillow) until
    something actually looks at or touches the desktop.
    """

    _PERCEPTION = {
        "get_desktop_overview",
        "format_overview",
        "get_window_tree",
        "format_window_tree",
        "page_text",
        "format_page_text",
        "screenshot",
        "capture",
        "capture_bounds",
        "system_facts",
    }
    _ACTIONS = {
        "launch_app",
        "focus_window",
        "click",
        "type_text",
        "hotkey",
        "press",
        "scroll",
        "run_powershell",
        "open_url",
    }

    def prewarm_shell(self) -> bool:
        """Open the persistent PowerShell session now, before anything needs it.

        A real method rather than something resolved through ``__getattr__``: it
        is not one of the contract's action functions and is not re-exported from
        :mod:`yuki.actions`, so it reaches straight into
        :mod:`yuki.actions.shell`, which owns the session.
        """
        import importlib

        return bool(importlib.import_module("yuki.actions.shell").prewarm())

    def activity_facts(self) -> dict[str, Any]:
        """:func:`yuki.perception.system.activity_facts` (not a contract function)."""
        import importlib

        return importlib.import_module("yuki.perception.system").activity_facts()

    def warm_activity(self) -> None:
        """:func:`yuki.perception.system.warm_activity`: returns at once."""
        import importlib

        importlib.import_module("yuki.perception.system").warm_activity()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        import importlib

        if name in self._PERCEPTION:
            module = importlib.import_module("yuki.perception")
        elif name in self._ACTIONS:
            module = importlib.import_module("yuki.actions")
        else:
            raise AttributeError(name)
        function = getattr(module, name)
        setattr(self, name, function)  # cache on the instance
        return function


def default_backend() -> Backend:
    """The production backend, wired to :mod:`yuki.perception` / :mod:`yuki.actions`."""
    return _LazyRealBackend()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Dispatch results
# ---------------------------------------------------------------------------

Kind = Literal["normal", "ask_user", "done", "note"]


@dataclass
class ToolOutcome:
    """Everything the loop needs after one tool ran.

    Attributes:
        name: Tool name as the model called it.
        ok: False for any failure; the loop stops dispatching and marks the
            ``tool_result`` block ``is_error``.
        summary: One line for the console and the event stream.
        content: ``tool_result`` content blocks to send back to the model. Empty
            for control tools, whose content the loop supplies.
        payload: Full structured result, for the JSONL log.
        elapsed_ms: Wall time of the tool itself.
        kind: ``normal`` for perception/actions; otherwise which control tool
            was called.
        control_input: Validated input of a control tool.
        screenshot_png: Raw PNG bytes when a screenshot was taken, so the logger
            can write the file.
        screenshot_b64: The base64 form actually sent to the model.
    """

    name: str
    ok: bool
    summary: str
    content: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    kind: Kind = "normal"
    control_input: dict[str, Any] = field(default_factory=dict)
    screenshot_png: bytes | None = None
    screenshot_b64: str | None = None

    @property
    def is_perception(self) -> bool:
        """Whether this result is a large snapshot eligible for later stubbing."""
        return self.name in PERCEPTION_TOOLS


@dataclass
class WindowView:
    """One settled read of a window, for the view attached after actions.

    Attributes:
        hwnd: The window read.
        text: Header plus formatted tree, the same text ``look_at_window`` gives.
        summary: The one-line summary ``look_at_window`` would report.
        payload: The tree as plain data, for the log.
        reads: Tree reads made while waiting for it to settle.
        settled: The signature held still for the quiet period.
        reason: Why the wait ended, in words.
        elapsed_ms: Wall time of the whole view.
    """

    hwnd: int
    text: str
    summary: str
    payload: dict[str, Any]
    reads: int
    settled: bool
    reason: str
    elapsed_ms: float


class ToolError(Exception):
    """Raised inside a handler to report a clean, model-readable failure."""


class Dispatcher:
    """Maps ``tool_use`` blocks onto backend calls.

    Args:
        backend: Object providing the contract functions. Defaults to the real
            desktop backend.
        screenshot_policy: ``never`` (the default, matching
            :class:`yuki.config.Settings`) refuses screenshots outright -- a
            backstop, since :func:`tool_params` already leaves ``take_screenshot``
            out of the block under ``never`` -- ``ask`` returns a failure telling
            the model to ask the user first, ``auto`` allows them. This is the
            consent gate, not behaviour steering.
        tool_timeout_s: Timeout handed to :func:`run_powershell`.
        max_tree_elements: Cap for :func:`get_window_tree`.
        tree_timeout_s: Budget handed to :func:`get_window_tree`. Deliberately
            larger than that function's own default: a window whose tree is built
            lazily is thin for the first few seconds of its life and the walk
            spends the budget polling for it, and ``look_at_window`` is exactly the
            call that follows ``launch_app``. Measured on this desktop, a cold
            Spotify goes from 7 usable elements to 137 by spending 4.9 s of it, and
            a window that is already built still answers in one pass.
    """

    def __init__(
        self,
        backend: Backend | None = None,
        *,
        screenshot_policy: str = "never",
        tool_timeout_s: float = 20.0,
        max_tree_elements: int = 400,
        tree_timeout_s: float = 6.0,
    ) -> None:
        self.backend = backend if backend is not None else default_backend()
        self.screenshot_policy = screenshot_policy
        self.tool_timeout_s = tool_timeout_s
        self.max_tree_elements = max_tree_elements
        self.tree_timeout_s = tree_timeout_s
        #: Reads handed to the model so far (``read #N`` in a tree's header).
        self._reads = 0
        #: hwnd -> ``(read number, {element id: element})`` of the latest tree
        #: of that window the model was given, for element-id input targets.
        self._latest_trees: dict[int, tuple[int, dict[int, dict[str, Any]]]] = {}
        #: The window of the most recent tree read, the default for an element id.
        self._latest_tree_hwnd: int | None = None

    # -- warm-up -----------------------------------------------------------

    def prewarm_shell(self) -> bool:
        """Ask the backend to start its persistent PowerShell session now.

        The audit measured 2-4.7 s of cold start for that session, which the
        first ``run_powershell`` of a session would otherwise pay in front of the
        user. Safe to call on any backend: one with no ``prewarm_shell`` (every
        test double, and any backend without a real shell) returns ``False``
        instead of raising. Blocks, so callers run it off the main thread --
        :class:`yuki.agent.loop.Agent` does exactly that at construction.

        Returns:
            True if the backend reported a live session.
        """
        prewarm = getattr(self.backend, "prewarm_shell", None)
        if not callable(prewarm):
            return False
        return bool(prewarm())

    def activity_facts(self) -> dict[str, Any]:
        """What the user is engaged in (media, microphone/camera, foreground).

        Read from the backend's optional ``activity_facts``
        (:func:`yuki.perception.system.activity_facts`); a backend without one
        gives ``{}``. Failures inside it are reported in its ``errors``.
        """
        read = getattr(self.backend, "activity_facts", None)
        if not callable(read):
            return {}
        facts = read()
        return facts if isinstance(facts, dict) else {}

    def warm_activity(self) -> bool:
        """Start the backend's activity-facts warm-up (WinRT import) if it has one."""
        warm = getattr(self.backend, "warm_activity", None)
        if not callable(warm):
            return False
        warm()
        return True

    # -- entry point -------------------------------------------------------

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        """Run one tool and return its outcome.

        Never raises for tool-level problems: unknown names, bad arguments and
        backend exceptions all come back as ``ok=False`` outcomes so the model can
        read the error and adapt.
        """
        started = time.perf_counter()
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            outcome = unavailable_tool(name, TOOLS_BY_NAME)
            outcome.elapsed_ms = (time.perf_counter() - started) * 1000
            return outcome
        try:
            outcome = handler(tool_input)
        except ToolError as exc:
            outcome = ToolOutcome(
                name=name,
                ok=False,
                summary=str(exc),
                content=[{"type": "text", "text": str(exc)}],
                payload={"error": str(exc)},
            )
        except Exception as exc:  # backend blew up; tell the model plainly
            message = f"{type(exc).__name__}: {exc}"
            outcome = ToolOutcome(
                name=name,
                ok=False,
                summary=message,
                content=[{"type": "text", "text": message}],
                payload={"error": message, "exception": type(exc).__name__},
            )
        outcome.name = name
        if not outcome.elapsed_ms:
            outcome.elapsed_ms = (time.perf_counter() - started) * 1000
        return outcome

    # -- argument helpers --------------------------------------------------

    @staticmethod
    def _need_int(tool_input: dict[str, Any], key: str) -> int:
        value = tool_input.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{key!r} must be an integer, got {value!r}")
        return int(value)

    @staticmethod
    def _need_str(tool_input: dict[str, Any], key: str) -> str:
        value = tool_input.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{key!r} must be a non-empty string, got {value!r}")
        return value

    @staticmethod
    def _opt_int(tool_input: dict[str, Any], key: str, default: int) -> int:
        value = tool_input.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{key!r} must be an integer, got {value!r}")
        return int(value)

    def _opt_hwnd(self, tool_input: dict[str, Any]) -> int | None:
        """The optional ``expect_hwnd`` guard, or ``None`` when it was omitted."""
        if tool_input.get("expect_hwnd") is None:
            return None
        return self._need_int(tool_input, "expect_hwnd")

    def _from_action(self, name: str, result: Any) -> ToolOutcome:
        """Wrap an :class:`ActionResult`-shaped object into a :class:`ToolOutcome`."""
        ok = bool(getattr(result, "ok", False))
        summary = str(getattr(result, "summary", ""))
        details = getattr(result, "details", {}) or {}
        elapsed = float(getattr(result, "elapsed_ms", 0.0) or 0.0)
        text = summary
        if details:
            text = f"{summary}\n{_render(details)}"
        return ToolOutcome(
            name=name,
            ok=ok,
            summary=summary,
            content=[{"type": "text", "text": text}],
            payload=_plain(result),
            elapsed_ms=elapsed,
        )

    # -- perception --------------------------------------------------------

    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        """Capture and format a desktop overview.

        Returns:
            ``(text, payload, elapsed_ms)`` -- used both by the ``look_at_desktop``
            tool and by the loop's per-turn automatic overview.
        """
        started = time.perf_counter()
        overview = self.backend.get_desktop_overview()
        text = self.backend.format_overview(overview)
        return text, _plain(overview), (time.perf_counter() - started) * 1000

    def _do_look_at_desktop(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text, payload, elapsed = self.overview_text()
        count = len(payload.get("windows") or []) if isinstance(payload, dict) else 0
        return ToolOutcome(
            name="look_at_desktop",
            ok=True,
            summary=f"{count} windows",
            content=[{"type": "text", "text": text}],
            payload=payload,
            elapsed_ms=elapsed,
        )

    def _do_look_at_window(self, tool_input: dict[str, Any]) -> ToolOutcome:
        hwnd = self._need_int(tool_input, "hwnd")
        started = time.perf_counter()
        tree = self.backend.get_window_tree(
            hwnd, max_elements=self.max_tree_elements, timeout_s=self.tree_timeout_s
        )
        return self._tree_outcome(hwnd, tree, started)

    def _tree_outcome(self, hwnd: int, tree: Any, started: float) -> ToolOutcome:
        """Format one tree read exactly as ``look_at_window`` returns it.

        The text is the backend's own :func:`format_window_tree` behind a
        one-line header, so whatever that formatter reports (page title and
        address, selected tab, status and notes) reaches the model unchanged,
        for explicit looks and the automatic view after actions alike.
        """
        text = self.backend.format_window_tree(tree)
        payload = _plain(tree)
        elements = payload.get("elements") or [] if isinstance(payload, dict) else []
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        status = str(payload.get("status") or "ok") if isinstance(payload, dict) else "ok"
        note = str(payload.get("note") or "") if isinstance(payload, dict) else ""
        self._reads += 1
        self._latest_trees[int(hwnd)] = (
            self._reads,
            {
                int(element["id"]): element
                for element in elements
                if isinstance(element, dict) and isinstance(element.get("id"), int)
            },
        )
        self._latest_tree_hwnd = int(hwnd)
        header = (
            f"window {hwnd} \"{payload.get('title', '')}\" ({payload.get('process_name', '')}): "
            f"{len(elements)} elements{', truncated' if truncated else ''}, status {status}, "
            f"read #{self._reads}"
        )
        summary = f"{len(elements)} elements{' (truncated)' if truncated else ''}"
        if status != "ok":
            summary += f", {status}"
        if note:
            summary += f": {note}"
        return ToolOutcome(
            name="look_at_window",
            ok=True,
            summary=summary,
            content=[{"type": "text", "text": f"{header}\n{text}"}],
            payload=payload,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_read_page(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """The whole text of the page a window shows, with its title and address.

        ``ok`` is False when the window shows no page (no Document with an
        address on screen) or the read failed: the result then says why, and the
        element tree is the way to look at that window instead.
        """
        hwnd = self._need_int(tool_input, "hwnd")
        read = getattr(self.backend, "page_text", None)
        if not callable(read):
            raise ToolError("this backend cannot read page text")
        started = time.perf_counter()
        result = read(hwnd)
        formatter = getattr(self.backend, "format_page_text", None)
        payload = _plain(result)
        if not isinstance(payload, dict):
            payload = {"result": payload}
        text = formatter(result) if callable(formatter) else _render(payload)
        found = bool(payload.get("found"))
        error = str(payload.get("error") or "")
        note = str(payload.get("note") or "")
        chars = int(payload.get("chars") or 0)
        if found and not error:
            title = str(payload.get("title") or payload.get("url") or "")
            summary = f'{chars} chars of "{title}"'
            if payload.get("truncated"):
                summary += " (clipped)"
        else:
            summary = error or note or "no page read"
        return ToolOutcome(
            name="read_page",
            ok=found and not error,
            summary=summary,
            content=[{"type": "text", "text": text}],
            payload=payload,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def foreground_hwnd(self) -> int | None:
        """The foreground window right now, from a (cheap) desktop overview."""
        overview = self.backend.get_desktop_overview()
        value = (
            overview.get("foreground_hwnd")
            if isinstance(overview, dict)
            else getattr(overview, "foreground_hwnd", None)
        )
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def window_view(
        self,
        hwnd: int,
        *,
        settle: bool = True,
        quiet_s: float = AUTO_VIEW_QUIET_S,
        cap_s: float = AUTO_VIEW_CAP_S,
    ) -> "WindowView":
        """Read a window the way ``look_at_window`` does, once it has settled.

        Used by the loop for the view it attaches after a turn of actions. Every
        read is the backend's own :func:`get_window_tree`, patience included (a
        thin tree of a young or freshly opened window is re-polled inside it,
        and a window that does not answer comes back ``busy``). On top of that
        the window is re-read until its element count and the area its leaf
        elements cover have not changed for ``quiet_s``, bounded by ``cap_s``
        for the whole view. A condition poll, never a fixed wait: a window that
        is already still returns after two equal reads ``quiet_s`` apart.

        Args:
            hwnd: Window to read.
            settle: ``False`` takes one read and returns it -- for when the
                action already reported the window's content ready.
            quiet_s: How long the signature must hold still.
            cap_s: Bound on the whole view.

        Returns:
            A :class:`WindowView` holding the newest good read.

        Raises:
            Whatever the first read raises (``ValueError`` for a handle that is
            no longer a window); a later read that fails just ends the wait.
        """
        started = time.perf_counter()
        deadline = started + max(cap_s, 0.0)
        tree = self.backend.get_window_tree(
            hwnd, max_elements=self.max_tree_elements, timeout_s=max(cap_s, 0.1)
        )
        reads = 1
        signature = _tree_signature(tree)
        changed_at = time.perf_counter()
        settled = False
        if not settle:
            reason = "one read: the action had already reported the content ready"
        elif signature[0] != "ok":
            reason = f"one read: the window's status is {signature[0]}"
        else:
            reason = f"still changing when the {cap_s:g} s limit was reached"
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                time.sleep(min(_AUTO_VIEW_POLL_S, remaining))  # poll gap, not a settle sleep
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                try:
                    newer = self.backend.get_window_tree(
                        hwnd,
                        max_elements=self.max_tree_elements,
                        timeout_s=max(remaining, quiet_s),
                    )
                except Exception as exc:  # gone or wedged: keep the last good read
                    reason = f"re-reading stopped: {type(exc).__name__}: {exc}"
                    break
                reads += 1
                newer_signature = _tree_signature(newer)
                if newer_signature[0] != "ok":
                    reason = f"re-reading stopped: the window turned {newer_signature[0]}"
                    break
                tree = newer
                now = time.perf_counter()
                if newer_signature != signature:
                    signature, changed_at = newer_signature, now
                    continue
                if now - changed_at >= quiet_s:
                    settled = True
                    reason = f"unchanged for {(now - changed_at) * 1000:.0f} ms"
                    break
        outcome = self._tree_outcome(hwnd, tree, started)
        return WindowView(
            hwnd=hwnd,
            text=str(outcome.content[0]["text"]) if outcome.content else "",
            summary=outcome.summary,
            payload=outcome.payload if isinstance(outcome.payload, dict) else {},
            reads=reads,
            settled=settled,
            reason=reason,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_take_screenshot(self, tool_input: dict[str, Any]) -> ToolOutcome:
        if self.screenshot_policy == "never":
            raise ToolError("Screenshots are disabled in this session; work from the element tree.")
        if self.screenshot_policy == "ask":
            raise ToolError(
                "Screenshots need the user's permission in this session. "
                "Ask the user first, then try again."
            )
        hwnd = tool_input.get("hwnd")
        hwnd = None if hwnd is None else self._need_int(tool_input, "hwnd")
        monitor = tool_input.get("monitor")
        monitor = None if monitor is None else self._need_int(tool_input, "monitor")
        region = self._opt_region(tool_input)
        named = [
            label
            for label, value in (("hwnd", hwnd), ("region", region), ("monitor", monitor))
            if value is not None
        ]
        if len(named) > 1:
            raise ToolError(
                f"name at most one target, got {', '.join(named)}. One shot covers one "
                f"thing: a window, a rectangle, or a display."
            )

        started = time.perf_counter()
        shot = self._capture(hwnd, region, monitor)
        png, geometry = shot["png"], shot["geometry"]
        b64 = base64.standard_b64encode(png).decode("ascii")
        target = shot["target"]
        return ToolOutcome(
            name="take_screenshot",
            ok=True,
            summary=f"{target}, {len(png)} bytes",
            content=[
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": f"Screenshot of {target}. {geometry['text']}".strip()},
            ],
            payload={"target": target, "bytes": len(png), **geometry["payload"]},
            elapsed_ms=(time.perf_counter() - started) * 1000,
            screenshot_png=png,
            screenshot_b64=b64,
        )

    def _opt_region(
        self, tool_input: dict[str, Any]
    ) -> tuple[int, int, int, int] | None:
        """Validate the optional ``region`` argument, or ``None`` when omitted."""
        region = tool_input.get("region")
        if region is None:
            return None
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ToolError(
                f"'region' must be [left, top, right, bottom], got {region!r}"
            )
        values = []
        for value in region:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolError(f"'region' must be four integers, got {region!r}")
            values.append(int(value))
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise ToolError(
                f"region {values} is empty; it is [left, top, right, bottom] and needs "
                f"right > left and bottom > top"
            )
        return (left, top, right, bottom)

    def _capture(
        self,
        hwnd: int | None,
        region: tuple[int, int, int, int] | None,
        monitor: int | None,
    ) -> dict[str, Any]:
        """Take the shot and work out what to say about its coordinates.

        Prefers the backend's ``capture``, which reports the image's origin and
        scale as facts it measured rather than numbers reconstructed afterwards.
        Falls back to ``screenshot``, which in the contract returns the same
        ``Capture``; a backend whose ``screenshot`` returns bare PNG bytes (older
        test doubles) gets its geometry reconstructed from ``capture_bounds``.
        """
        capture = getattr(self.backend, "capture", None)
        if not callable(capture):
            capture = self.backend.screenshot
        shot = capture(hwnd, region=region, monitor=monitor)
        if not isinstance(shot, (bytes, bytearray)):
            png = getattr(shot, "png", None)
            if not isinstance(png, (bytes, bytearray)):
                raise ToolError(
                    f"capture returned {type(shot).__name__} without PNG bytes in .png"
                )
            mapping = getattr(shot, "mapping_text", None)
            left, top = getattr(shot, "origin", (0, 0))
            scale = float(getattr(shot, "scale", 1.0) or 1.0)
            return {
                "png": bytes(png),
                "target": str(getattr(shot, "target", "") or "the screen"),
                "geometry": {
                    "text": mapping() if callable(mapping) else "",
                    "payload": {
                        "image_size": list(getattr(shot, "image_size", ()) or []),
                        "capture_bounds": list(getattr(shot, "bounds", ()) or []),
                        "origin": [int(left), int(top)],
                        "scale": scale,
                        "one_to_one": scale == 1.0,
                        "rendered_by_window": bool(
                            getattr(shot, "rendered_by_window", False)
                        ),
                    },
                },
            }
        # A backend written to the older bytes-only contract: reconstruct the
        # geometry from capture_bounds.
        png = shot
        png = bytes(png)
        if hwnd is not None:
            target = f"window {hwnd}"
        elif monitor is not None:
            target = f"monitor {monitor}"
        elif region is not None:
            target = "region"
        else:
            target = "the screen"
        return {
            "png": png,
            "target": target,
            "geometry": self._capture_geometry(hwnd, png, region=region, monitor=monitor),
        }

    def _capture_geometry(
        self,
        hwnd: int | None,
        png: bytes,
        *,
        region: tuple[int, int, int, int] | None = None,
        monitor: int | None = None,
    ) -> dict[str, Any]:
        """Say where a screenshot's pixels are, in screen coordinates.

        Without this a window shot is a trap: it is rendered by the window, so its
        top-left is the window's top-left and not the screen's, and it is then
        downscaled. A model that reads a coordinate off the image and clicks it
        lands somewhere else entirely -- which is exactly what happened on
        2026-09-22, six clicks in a row into another app's window while the
        dialog it was aiming at sat untouched.

        Facts only: the rectangle captured, the size sent, the scale between them,
        and the arithmetic. It says nothing about what to do with them.

        Returns:
            ``{"text": str, "payload": dict}``; the text is empty-safe -- if the
            geometry cannot be read, the caller still gets a usable result.
        """
        size = _png_size(png)
        bounds: tuple[int, int, int, int] | None = None
        reader = getattr(self.backend, "capture_bounds", None)
        if callable(reader):
            try:
                left, top, right, bottom = reader(hwnd, region=region, monitor=monitor)
                bounds = (int(left), int(top), int(right), int(bottom))
            except TypeError:  # a backend written before region/monitor existed
                try:
                    left, top, right, bottom = reader(hwnd)
                    bounds = (int(left), int(top), int(right), int(bottom))
                except Exception:
                    bounds = None
            except Exception:
                bounds = None
        if size is None or bounds is None:
            return {"text": "", "payload": {"image_size": size, "capture_bounds": bounds}}
        width, height = size
        left, top, right, bottom = bounds
        source_width = max(1, right - left)
        scale = width / source_width
        if scale == 1.0 and (left, top) == (0, 0):
            text = (
                f"The image is {width}x{height} at 1:1 with the screen and starts at "
                f"the screen origin, so a point in this image IS the screen point - "
                f"click it as you read it, no conversion."
            )
        elif scale == 1.0:
            text = (
                f"The image is {width}x{height} at 1:1 with the screen, covering "
                f"({left},{top}) to ({right},{bottom}). To click something here: "
                f"screen_x = {left} + image_x, screen_y = {top} + image_y."
            )
        else:
            text = (
                f"The image is {width}x{height}, a {scale:.4g}x scaling of the "
                f"{source_width}x{bottom - top} screen rectangle ({left},{top}) to "
                f"({right},{bottom}). To click something here: "
                f"screen_x = {left} + image_x / {scale:.4g}, "
                f"screen_y = {top} + image_y / {scale:.4g}."
            )
        return {
            "text": text,
            "payload": {
                "image_size": [width, height],
                "capture_bounds": [left, top, right, bottom],
                "origin": [left, top],
                "scale": round(scale, 6),
                "one_to_one": scale == 1.0,
            },
        }

    def _do_system_facts(self, tool_input: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()
        facts = self.backend.system_facts()
        payload = _plain(facts)
        return ToolOutcome(
            name="system_facts",
            ok=True,
            summary=f"{len(payload) if isinstance(payload, dict) else 0} fields",
            content=[{"type": "text", "text": _render(payload)}],
            payload=payload if isinstance(payload, dict) else {"value": payload},
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    # -- actions -----------------------------------------------------------

    @staticmethod
    def _opt_str_list(tool_input: dict[str, Any], key: str) -> list[str] | None:
        """An optional list of strings, or ``None`` when omitted."""
        value = tool_input.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ToolError(f"{key!r} must be a list of strings, got {value!r}")
        return value

    def _do_launch_app(self, tool_input: dict[str, Any]) -> ToolOutcome:
        query = self._need_str(tool_input, "query")
        args = self._opt_str_list(tool_input, "args")
        # Only pass args when there are some, so a backend written before the
        # parameter existed keeps working for plain launches.
        result = (
            self.backend.launch_app(query, args=args)
            if args
            else self.backend.launch_app(query)
        )
        return self._from_action("launch_app", result)

    def _do_focus_window(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "focus_window", self.backend.focus_window(self._need_int(tool_input, "hwnd"))
        )

    def _element_target(self, tool_input: dict[str, Any]) -> tuple[int, int, int, str] | None:
        """``(x, y, hwnd, label)`` of the ``element`` an input names, or ``None``.

        The element comes from the latest tree of its window the model was given
        (``expect_hwnd``, else the window read most recently). Plumbing that
        turns an id into the coordinates it stood for, with the guard set to its
        window; every way it could point at the wrong thing is refused instead.

        Raises:
            ToolError: no such element, no read of that window, or ``read`` names
                an older read than the latest.
        """
        if tool_input.get("element") is None:
            return None
        element_id = self._need_int(tool_input, "element")
        hwnd = self._opt_hwnd(tool_input)
        if hwnd is None:
            hwnd = self._latest_tree_hwnd
        if hwnd is None or hwnd not in self._latest_trees:
            raise ToolError(
                f"element {element_id} refers to no tree: "
                + (f"window {hwnd} has not been read" if hwnd is not None else "no window has been read yet")
                + " - look at the window first, or give x and y"
            )
        read, elements = self._latest_trees[hwnd]
        if tool_input.get("read") is not None:
            asked = self._need_int(tool_input, "read")
            if asked != read:
                raise ToolError(
                    f"element ids from read #{asked} are out of date: the latest read of "
                    f"window {hwnd} is #{read}; nothing was sent. Use an id from read "
                    f"#{read}, or look at the window again"
                )
        element = elements.get(element_id)
        if element is None:
            raise ToolError(
                f"read #{read} of window {hwnd} has no element [{element_id}] (its ids "
                f"run 0-{max(elements) if elements else 0}); nothing was sent"
            )
        center = element.get("center") or ()
        try:
            x, y = int(center[0]), int(center[1])
        except (TypeError, ValueError, IndexError):
            raise ToolError(f"element [{element_id}] of read #{read} has no position") from None
        label = f"[{element_id}] {element.get('role') or 'element'}"
        if element.get("name"):
            label += f" \"{str(element['name'])[:60]}\""
        return x, y, hwnd, f"{label} of read #{read} at ({x},{y})"

    def _point(self, tool_input: dict[str, Any]) -> tuple[int, int, int | None, str]:
        """``(x, y, expect_hwnd, label)``: an element id's centre, else x and y."""
        target = self._element_target(tool_input)
        if target is not None:
            return target
        if tool_input.get("x") is None or tool_input.get("y") is None:
            raise ToolError("give an element id from the latest tree, or both x and y")
        return (
            self._need_int(tool_input, "x"),
            self._need_int(tool_input, "y"),
            self._opt_hwnd(tool_input),
            "",
        )

    @staticmethod
    def _labelled(outcome: ToolOutcome, label: str) -> ToolOutcome:
        """Say which element an element-id input went to, ahead of the result."""
        if label:
            outcome.summary = f"on {label}: {outcome.summary}"
            if outcome.content and outcome.content[0].get("type") == "text":
                outcome.content[0]["text"] = f"on {label}: {outcome.content[0]['text']}"
            if isinstance(outcome.payload, dict):
                outcome.payload = {**outcome.payload, "element_target": label}
        return outcome

    def _do_click(self, tool_input: dict[str, Any]) -> ToolOutcome:
        button = tool_input.get("button") or "left"
        if button not in {"left", "right", "middle"}:
            raise ToolError(f"button must be left, right or middle, got {button!r}")
        x, y, guard, label = self._point(tool_input)
        return self._labelled(
            self._from_action(
                "click",
                self.backend.click(
                    x,
                    y,
                    button=button,
                    clicks=self._opt_int(tool_input, "clicks", 1),
                    expect_hwnd=guard,
                ),
            ),
            label,
        )

    def _do_type_text(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = tool_input.get("text")
        if not isinstance(text, str):
            raise ToolError(f"'text' must be a string, got {text!r}")
        target = self._element_target(tool_input)
        options: dict[str, Any] = {
            "press_enter": bool(tool_input.get("press_enter")),
            "expect_hwnd": target[2] if target is not None else self._opt_hwnd(tool_input),
        }
        if tool_input.get("clear"):
            # Only when asked, so a backend written before the option existed
            # keeps working for plain typing.
            options["clear"] = True
        if target is None:
            return self._from_action("type_text", self.backend.type_text(text, **options))
        # Focus the element first by clicking it, guarded to its window; type
        # only if that click went through.
        x, y, guard, label = target
        focus = self._from_action("type_text", self.backend.click(x, y, expect_hwnd=guard))
        if not focus.ok:
            return self._labelled(focus, f"{label} (clicked to focus it; nothing typed)")
        typed = self._from_action("type_text", self.backend.type_text(text, **options))
        typed.payload = {"focus_click": focus.payload, "type_text": typed.payload}
        typed.elapsed_ms += focus.elapsed_ms
        return self._labelled(typed, f"{label} (clicked to focus it)")

    def _do_hotkey(self, tool_input: dict[str, Any]) -> ToolOutcome:
        keys = tool_input.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ToolError(f"'keys' must be a non-empty list of strings, got {keys!r}")
        return self._from_action(
            "hotkey", self.backend.hotkey(*keys, expect_hwnd=self._opt_hwnd(tool_input))
        )

    def _do_press(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "press",
            self.backend.press(
                self._need_str(tool_input, "key"),
                times=self._opt_int(tool_input, "times", 1),
                expect_hwnd=self._opt_hwnd(tool_input),
            ),
        )

    def _do_scroll(self, tool_input: dict[str, Any]) -> ToolOutcome:
        x, y, guard, label = self._point(tool_input)
        return self._labelled(
            self._from_action(
                "scroll",
                self.backend.scroll(
                    x,
                    y,
                    dy=self._opt_int(tool_input, "dy", 0),
                    dx=self._opt_int(tool_input, "dx", 0),
                    expect_hwnd=guard,
                ),
            ),
            label,
        )

    def _do_run_powershell(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "run_powershell",
            self.backend.run_powershell(
                self._need_str(tool_input, "command"), timeout_s=self.tool_timeout_s
            ),
        )

    def _do_open_url(self, tool_input: dict[str, Any]) -> ToolOutcome:
        url = self._need_str(tool_input, "url")
        app = tool_input.get("app")
        if app is not None and not isinstance(app, str):
            raise ToolError(f"'app' must be a string, got {app!r}")
        if app and app.strip():
            return self._from_action("open_url", self.backend.open_url(url, app=app))
        return self._from_action("open_url", self.backend.open_url(url))

    # -- control tools -----------------------------------------------------

    def _do_ask_user(self, tool_input: dict[str, Any]) -> ToolOutcome:
        question = self._need_str(tool_input, "question")
        return ToolOutcome(
            name="ask_user",
            ok=True,
            summary=question,
            kind="ask_user",
            control_input={"question": question},
            payload={"question": question},
        )

    def _do_done(self, tool_input: dict[str, Any]) -> ToolOutcome:
        message = self._need_str(tool_input, "message")
        return ToolOutcome(
            name="done",
            ok=True,
            summary=message,
            kind="done",
            control_input={"message": message},
            payload={"message": message},
        )

    def _do_note_to_self(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = tool_input.get("text")
        if not isinstance(text, str):
            raise ToolError(f"'text' must be a string, got {text!r}")
        return ToolOutcome(
            name="note_to_self",
            ok=True,
            summary=f"note updated ({len(text)} chars)",
            kind="note",
            control_input={"text": text},
            payload={"text": text},
        )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _tree_signature(tree: Any) -> tuple[str, int, bool, int]:
    """What has to hold still for a window to count as settled.

    ``(status, element count, truncated, covered cells)``. The covered cells are
    the :data:`_VIEW_CELL` grid cells under leaf elements (elements with nothing
    kept below them) -- the same coarse measure as the tree module's coverage --
    so a page that fills in without adding elements, or swaps its content at the
    element cap, still reads as changing.
    """
    payload = _plain(tree)
    if not isinstance(payload, dict):
        return ("ok", 0, False, 0)
    elements = [e for e in payload.get("elements") or [] if isinstance(e, dict)]
    return (
        str(payload.get("status") or "ok"),
        len(elements),
        bool(payload.get("truncated")),
        _leaf_cells(elements),
    )


def _leaf_cells(elements: list[dict[str, Any]]) -> int:
    """Number of :data:`_VIEW_CELL` grid cells covered by leaf elements."""
    rects: list[tuple[int, int, int, int]] = []
    for index, element in enumerate(elements):
        depth = int(element.get("depth") or 0)
        if index + 1 < len(elements) and int(elements[index + 1].get("depth") or 0) > depth:
            continue  # has something below it: not a leaf
        bounds = element.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        try:
            left, top, right, bottom = (int(v) for v in bounds)
        except (TypeError, ValueError):
            continue
        if right > left and bottom > top:
            rects.append((left, top, right, bottom))
    if not rects:
        return 0
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    cols = max((max(r[2] for r in rects) - x0 + _VIEW_CELL - 1) // _VIEW_CELL, 1)
    rows = max((max(r[3] for r in rects) - y0 + _VIEW_CELL - 1) // _VIEW_CELL, 1)
    grid = bytearray(cols * rows)
    for left, top, right, bottom in rects:
        c0, c1 = (left - x0) // _VIEW_CELL, (right - x0 - 1) // _VIEW_CELL + 1
        r0, r1 = (top - y0) // _VIEW_CELL, (bottom - y0 - 1) // _VIEW_CELL + 1
        run = b"\x01" * (c1 - c0)
        for row in range(r0, r1):
            grid[row * cols + c0 : row * cols + c1] = run
    return grid.count(1)


def _png_size(png: bytes) -> tuple[int, int] | None:
    """Pixel size from a PNG's IHDR chunk, or ``None`` if it is not a PNG.

    Reading eight bytes of header beats decoding the image just to learn how big
    it is, and keeps this module free of Pillow.
    """
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return (width, height) if width and height else None


def _plain(value: Any) -> Any:
    """Convert a contract dataclass into a plain dict/list structure."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _render(value: Any, indent: int = 0) -> str:
    """Render structured data as compact indented text for the model."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_render(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(
            _render(item, indent) if isinstance(item, (dict, list)) else f"{pad}- {_scalar(item)}"
            for item in value
        )
    return f"{pad}{_scalar(value)}"


def _scalar(value: Any) -> str:
    """Single-line rendering of a leaf value."""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_scalar(v) for v in value) + ")"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in value.items()) + "}"
    return str(value)
