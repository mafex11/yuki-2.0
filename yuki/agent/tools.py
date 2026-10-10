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

The *memory* tools have no backend function either: the agent runs them against
Yuki's memory through :mod:`yuki.agent.memory`, and they are only listed when
memory is installed. Since revamp D the seven reads (recall, activity, calls,
call_timeline, threads, before, links) are one tool, ``memory(kind, ...)``; the
writes keep their own names.

Every action has fixed metadata (:class:`Traits`, :func:`tool_traits`): the
autonomy-policy kinds it falls under, what it needs (the global input, focus, one
window), whether it can destroy something or reach someone. The authorization
judge (:mod:`yuki.agent.judge`), the ``risk`` field and the input leases
(:mod:`yuki.actions.leases`) read it: a property of the tool and its operation,
never of the words.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Protocol, Sequence, runtime_checkable

from yuki.ui.own_window import PAGE_WORDS, PAGES as YUKI_PAGES

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

#: Tool names whose results are large perception snapshots. Used only by context
#: hygiene (:mod:`yuki.agent.context`) to decide what may be stubbed once stale.
#: This is bookkeeping about payload size, not a rule about behaviour.
PERCEPTION_TOOLS: frozenset[str] = frozenset(
    {"look_at_desktop", "look_at_window", "read_page", "read_tabs", "take_screenshot", "system_facts", "memory",
     "read_file", "fetch_url", "look_at_focus", "read_clipboard", "about", "check_situation"}
)

#: Tools handled by the agent loop rather than a backend function.
CONTROL_TOOLS: frozenset[str] = frozenset({"ask_user", "done", "note_to_self", "say", "tasks"})

#: Tools only one lane is shown (the reader lane's hand-off, :mod:`yuki.agent.lanes`): never in the worker's block.
LANE_ONLY_TOOLS: frozenset[str] = frozenset({"hand_over"})

#: The seven memory reads, one tool since revamp D (``memory(kind, ...)``): the old names, kept so a direct
#: ``MemoryAccess.run_tool`` with one of them still runs.
MEMORY_READ_KINDS: tuple[str, ...] = ("recall", "activity", "calls", "call_timeline", "threads", "before", "links")

#: Tools backed by Yuki's memory (:mod:`yuki.agent.memory`) rather than the
#: desktop backend. Left out of the tool block entirely when the memory API is
#: not installed (see :func:`tool_params`), like a policy-gated tool.
MEMORY_TOOLS: frozenset[str] = frozenset(
    {"memory", "remember_how", "correct_memory", "remember_rule", "forget_rule", "todo", "answer_question",
     "watch_for", "about"}
)

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
    "description": "Send only while this window is in front (else nothing is sent).",
}

#: An element of the latest tree read of a window, as an input target. Shared by
#: click, type_text and scroll for the same reason as :data:`_GUARD`.
_ELEMENT: dict[str, Any] = {
    "type": "integer",
    "description": "An element id ([id]) from the latest read of its window (expect_hwnd, else the window read "
    "last); input is guarded to that window.",
}

#: The read an element id comes from.
_READ: dict[str, Any] = {
    "type": "integer",
    "description": "The read number (read #N) the element id comes from.",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "look_at_desktop",
        "label": "Looking at the desktop",
        "description": (
            "The window list again (hwnd, title, process, bounds, which is in front, the cursor, the monitors). "
            "Every turn already starts with it, so you rarely need this."
        ),
        "input_schema": _obj({}),
    },
    {
        "name": "look_at_window",
        "label": "Reading a window",
        "description": (
            "Read one window's UI Automation tree: roles, names, values, states, click points, how elements act. Ids "
            "hold while their element exists. Status ok, busy (read again shortly) or empty. After actions that "
            "worked, the window is read for you."
        ),
        "input_schema": _obj({"hwnd": {"type": "integer"},
                              "within": {"type": "integer", "description": "An element id: only its part, in full."}},
                             ["hwnd"]),
    },
    {
        "name": "read_page",
        "label": "Reading a page",
        "description": "The full text of the page a browser or web app window shows, scrolled-out parts included, "
                       "with its title and address. For reading; look_at_window gives elements to act on.",
        "input_schema": _obj({"hwnd": {"type": "integer"}}, ["hwnd"]),
    },
    {
        "name": "read_tabs",
        "label": "Reading the tabs",
        "description": "A window's own tab strip, left to right: each tab's id (what the tab tool takes), full name and "
                       "which one shows.",
        "input_schema": _obj({"hwnd": {"type": "integer"}}, ["hwnd"]),
    },
    {
        "name": "look_at_focus",
        "label": "Looking at what you're on",
        "description": "What has keyboard focus now (role, name, value, window), the selected text and the text around "
                       "the caret: \"this\", \"what I selected\". Password fields are named, never read.",
        "input_schema": _obj({}),
    },
    {
        "name": "read_clipboard",
        "label": "Checking what you copied",
        "description": "The clipboard's text (clipped when long), which kinds of data it holds and which app put it "
                       "there. Content an app marked private is not read.",
        "input_schema": _obj({}),
    },
    {
        "name": "write_clipboard",
        "label": "Copying that",
        "description": "Put text on the clipboard for the user to paste (\"copy the link\"); read back to check.",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "window_action",
        "label": "Arranging a window",
        "description": (
            "Minimize, maximize, full screen, restore, focus or close one whole window; the result is the state "
            "read back from Windows. Maximized keeps the title bar and taskbar; fullscreen covers the monitor (F11). "
            "close is the window's X (the app may ask to save). A window is not a tab."
        ),
        "input_schema": _obj(
            {
                "hwnd": {"type": "integer"},
                "action": {"type": "string",
                           "enum": ["minimize", "maximize", "fullscreen", "restore", "focus", "close"]},
            },
            ["hwnd", "action"],
        ),
    },
    {
        "name": "open_yuki",
        "label": "Opening my window",
        "description": "Your own window. " + PAGE_WORDS,
        "input_schema": _obj({"page": {"type": "string", "enum": list(YUKI_PAGES)}}, ["page"]),
    },
    {
        "name": "take_screenshot",
        "label": "Taking a look at the screen",
        "description": (
            "Capture pixels of a window (hwnd), a screen rectangle (region) or one display (monitor), at most one; "
            "none gives the whole desktop scaled down. For when the element tree does not explain what is shown. "
            "Each shot says where its top-left is on screen and whether it is 1:1."
        ),
        "input_schema": _obj(
            {
                "hwnd": {"type": "integer"},
                "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                           "description": "[left, top, right, bottom] in screen coordinates."},
                "monitor": {"type": "integer"},
            }
        ),
    },
    {
        "name": "system_facts",
        "label": "Reading system facts",
        "description": "Local date and time, uptime, CPU load, memory totals and the processes using the most memory.",
        "input_schema": _obj({}),
    },
    {
        "name": "launch_app",
        "label": "Opening an app",
        "description": (
            "Start an installed app by name and bring its window to the front, waited on until it takes input. Several "
            "matches: the candidates come back instead. args are command-line arguments (a URL, file or folder); "
            "with args it returns once the content is ready ('content ready' or 'content still not ready')."
        ),
        "input_schema": _obj(
            {
                "query": {"type": "string", "description": "App name, e.g. 'spotify'."},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            ["query"],
        ),
    },
    {
        "name": "focus_window",
        "label": "Switching window",
        "description": "Bring a window to the front (restored if minimised) before keys meant for it.",
        "input_schema": _obj({"hwnd": {"type": "integer"}}, ["hwnd"]),
    },
    {
        "name": "click",
        "label": "Clicking",
        "description": (
            "Click an element (an id from the latest read of its window) or, when no element describes the target, "
            "x and y. A left single click on an element that supports a UI Automation pattern (invoke, toggle, "
            "select) goes through that pattern, without the mouse or taking focus; the result says which way. "
            "x, y must be on a control of the latest read; one hidden until hovered is hovered, read, then clicked."
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
                # N4 (report 47): a toggle (a read with on/off) is clicked only toward the state they want
                "toggle_to": {"type": "string", "enum": ["on", "off", "full_screen", "not_full_screen"],
                              "description": "A toggle (on/off in the read): the state they want; read live first, "
                                             "clicked only if not there, read back. A full-screen control: "
                                             "full_screen or not_full_screen, judged by the window."},
            },
        ),
    },
    {
        "name": "type_text",
        "label": "Typing",
        "description": (
            "Type into whatever has keyboard focus (with element: that element, focused first), optionally pressing "
            "Enter. The result names the control that had focus and, when it can be read, what it holds after: text "
            "missing means failed and Enter is not pressed. clear replaces the field's contents (through the field's "
            "own value when it has a writable one). When typing opens a suggestion list (a mention, an "
            "autocomplete) the result lists it and Enter is not pressed. A mention only becomes one when picked from "
            "the app's list: type its start with accept_suggestion, then the rest in a second call."
        ),
        "input_schema": _obj(
            {
                "text": {"type": "string"},
                "element": _ELEMENT,
                "read": _READ,
                "press_enter": {"type": "boolean"},
                "clear": {"type": "boolean", "description": "Empty the field first (text may be empty to only clear)."},
                "accept_suggestion": {"type": "boolean",
                                      "description": "Type by keys and accept the suggestion list the typing opens."},
                "expect_hwnd": _GUARD,
            },
            ["text"],
        ),
    },
    {
        "name": "hotkey",
        "label": "Pressing a shortcut",
        "description": (
            "Press keys held together, e.g. ['ctrl','t']. The result says where focus is after, any new window, and "
            "the window's state before -> after."
        ),
        "input_schema": _obj(
            {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "expect_hwnd": _GUARD},
            ["keys"],
        ),
    },
    {
        "name": "press",
        "label": "Pressing a key",
        "description": (
            "Press one key, optionally several times ('enter', 'tab', 'down'). Media keys ('playpause', "
            "'nexttrack', 'volumeup', 'volumemute' ...) act system-wide: no window, no expect_hwnd."
        ),
        "input_schema": _obj(
            {"key": {"type": "string"}, "times": {"type": "integer", "minimum": 1}, "expect_hwnd": _GUARD},
            ["key"],
        ),
    },
    {
        "name": "scroll",
        "label": "Scrolling",
        "description": "Scroll by wheel notches (positive dy is up) over an element (preferred: through its scroll "
                       "pattern when it has one, no mouse) or a point.",
        "input_schema": _obj(
            {
                "element": _ELEMENT,
                "read": _READ,
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "dy": {"type": "integer"},
                "dx": {"type": "integer"},
                "expect_hwnd": _GUARD,
                "until_stopped": {"type": "boolean", "description": (
                    "For \"keep scrolling until I say stop\": after this step it goes on scrolling by itself in steps "
                    "(pausing while they talk) until they say stop, take over or the page ends; returns after the "
                    "first step. Make it your last step, then done.")},
            },
        ),
    },
    {
        "name": "run_powershell",
        "label": "Running a command",
        "description": "Run a PowerShell command on this PC; stdout, stderr and the exit code come back.",
        "input_schema": _obj({"command": {"type": "string"}}, ["command"]),
    },
    {
        "name": "open_url",
        "label": "Opening a link",
        "description": (
            "Open a URL, file or folder with its default handler, or with app (an installed app, as launch_app takes "
            "it). Returns once the page is ready ('content ready' or 'content still not ready')."
        ),
        "input_schema": _obj(
            {"url": {"type": "string"}, "app": {"type": "string"}},
            ["url"],
        ),
    },
    {
        "name": "tab",
        "label": "Switching tab",
        "description": (
            "Show or close one tab by its id from read_tabs (never by title); the strip is read again after it. A "
            "closed tab comes back with the app's own reopen, checked by its title with read_tabs."
        ),
        "input_schema": _obj(
            {
                "hwnd": {"type": "integer"},
                "tab": {"type": "string", "description": "The tab's id, as read_tabs lists it."},
                "action": {"type": "string", "enum": ["select", "close"]},
            },
            ["hwnd", "tab", "action"],
        ),
    },
    {
        "name": "hover",
        "label": "Pointing at something",
        "description": "Put the pointer on an element (or x, y) without clicking, for controls that only appear "
                       "under the pointer; the window is read again after, so what appeared has an id.",
        "input_schema": _obj(
            {"element": _ELEMENT, "read": _READ, "x": {"type": "integer"}, "y": {"type": "integer"},
             "expect_hwnd": _GUARD},
        ),
    },
    {
        "name": "app_volume",
        "label": "Changing an app's volume",
        "description": (
            "One app's own volume in the Windows mixer (only Chrome, only Spotify): nothing focused or clicked, the "
            "system volume untouched. Without level, change or mute it lists the apps with sound. Read back. Not "
            "the app's own player volume (its slider: set_slider), never a stand-in for it."
        ),
        "input_schema": _obj(
            {
                "app": {"type": "string", "description": "Process name as the mixer lists it, or the app's name."},
                "change": {"type": "number", "description": "Points to move it by, e.g. -20."},
                "level": {"type": "number", "description": "Or the level to set, 0-100."},
                "mute": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "note_to_self",
        "label": "Making a note",
        "description": "Replace your short working note (you get it back every turn): what you learned, what is left.",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "ask_user",
        "label": "Asking you something",
        "description": "Ask the user one question and wait; their reply is the result.",
        "input_schema": _obj({"question": {"type": "string"}}, ["question"]),
    },
    {
        "name": "done",
        "label": "Wrapping up",
        "description": (
            "Your reply once this turn's work has succeeded, exactly once. It runs last and only if every other call "
            "in the turn succeeded, so send it alongside the actions that finish the job."
        ),
        "input_schema": _obj(
            {
                "message": {"type": "string", "description": "What you would text back, in their register."},
                "learned": {"type": "string",
                            "description": "Optional, never said: when the way was not obvious, one sentence on what "
                                           "worked (a hint for Yuki's learner)."},
            },
            ["message"],
        ),
    },
    {
        "name": "say",
        "label": "Telling you",
        "description": "Tell the user something while you keep working (a finding, why it is taking a while); returns "
                       "at once. Never the final answer.",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "memory",
        "label": "Checking memory",
        "description": (
            "Read memory of the user: text captured from their screen, chats and calls, their timeline, and your "
            "past exchanges with them. Results are dated, each with where it came from. One tool, by kind:\n"
            "- recall: facts and exchanges matching query. For when, how long or a stretch of time give since and "
            "until and with_time_use: the measured time use of that window comes back with the facts.\n"
            "- activity: time per site, app or page (group_by), longest stretches, away time and episodes in "
            "since..until (default today). An app's window in front, its calls and its background use come apart.\n"
            "- calls: the calls, meetings, game voice chats in since..until (default 7 days): app, start, end, "
            "each side's talk time, a gist. For what happened on one, open it with call_timeline.\n"
            "- call_timeline: one call (call, e.g. call:16), the whole session, in stretches with times; words true "
            "gives exact words a page at a time. Each line says who spoke; never give others' words as the "
            "user's. Written, keep the "
            "shape it comes in: each stretch's time range as its own line, then one "
            "short line per moment starting with its time, the quiet hours in a few words, for example\n"
            "**13:24-13:51** Claude work, MCP deadline\n- 13:28 said five Claude sessions were running at once\n"
            "(quiet 15:46-18:32)\nSpoken: the two or three moments that matter, with their times.\n"
            "- threads: their threads of work over days. action list (current), search (query), open (thread or "
            "query): where it stands, what is open, when and where it was worked on.\n"
            "- before: what they were doing before a moment or a break (at; event break or time; minutes); without "
            "at, before the latest break.\n"
            "- links: pages they had open and links shared with them (query, since, until, how, limit). Finds only."
        ),
        "input_schema": _obj(
            {
                "kind": {"type": "string",
                         "enum": ["recall", "activity", "calls", "call_timeline", "threads", "before", "links"]},
                "query": {"type": "string", "description": "recall, threads, links: what to look for, in plain words."},
                "since": {"type": "string",
                          "description": "Earliest local date or date-time, ISO (2026-09-20, 2026-09-20T18:00). "
                                         "A session going past midnight began the day before; 'today' begins when "
                                         "their day at the PC began (the memory block says)."},
                "until": {"type": "string", "description": "Latest local date or date-time; a date alone covers the day."},
                "app": {"type": "string", "description": "recall: only facts from this app."},
                "with_time_use": {"type": "boolean", "description": "recall: also the measured time use (needs since)."},
                "group_by": {"type": "string", "enum": ["site", "app", "page"]},
                "call": {"type": "string", "description": "call_timeline: the call's id, e.g. call:16."},
                "words": {"type": "boolean", "description": "call_timeline: the exact words instead."},
                "page": {"type": "integer"},
                "action": {"type": "string", "enum": ["list", "search", "open"]},
                "thread": {"type": "string", "description": "threads: an id, e.g. thread:12."},
                "at": {"type": "string", "description": "before: local date-time, ISO."},
                "event": {"type": "string", "enum": ["break", "time"]},
                "minutes": {"type": "integer"},
                "how": {"type": "string", "enum": ["viewed", "shared"]},
                "limit": {"type": "integer"},
            },
            ["kind"],
        ),
    },
    {
        "name": "remember_how",
        "label": "Saving how it's done",
        "description": "Keep a way of doing something on this PC that the user tells you, or which thing of theirs "
                       "their words meant when it was not literal (other jobs teach Yuki by themselves).",
        "input_schema": _obj(
            {"app": {"type": "string", "description": "The process name it applies to, if one."},
             "text": {"type": "string"}},
            ["text"],
        ),
    },
    {
        "name": "correct_memory",
        "label": "Noting that",
        "description": "Record the user's correction of a fact about their life, work or people, or where something "
                       "of theirs stands. How they want you to behave is a rule (remember_rule).",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "answer_question",
        "label": "Noting your answer",
        "description": "Save the user's reply to one of your own getting-to-know-you questions (question:<n>): their "
                       "exact words, or that they skipped it or never want it asked.",
        "input_schema": _obj(
            {
                "question": {"type": "string", "description": "e.g. question:12"},
                "outcome": {"type": "string", "enum": ["answered", "skipped", "never"]},
                "answer": {"type": "string", "description": "answered: their own words, copied exactly."},
            },
            ["question", "outcome"],
        ),
    },
    {
        "name": "watch_for",
        "label": "Keeping an eye out",
        "description": "A standing request (\"tell me when Varun replies\"): add it in that turn; memory checks what "
                       "comes in and shows one card when it happens. list, or cancel one.",
        "input_schema": _obj(
            {
                "action": {"type": "string", "enum": ["add", "list", "cancel"]},
                "condition": {"type": "string", "description": "add: who and what, in plain words."},
                "quote": {"type": "string", "description": "add: the user's own words asking for it, exactly."},
                "repeat": {"type": "boolean", "description": "add: every time, not only the next."},
                "days": {"type": "number"},
                "request": {"type": "string", "description": "cancel: its id, e.g. request:3."},
            },
            ["action"],
        ),
    },
    {
        "name": "remember_rule",
        "label": "Noting that for good",
        "description": "Save a standing instruction about how to talk to them or behave from now on, in their words; "
                       "it applies at once, starting with this reply.",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "forget_rule",
        "label": "Dropping that rule",
        "description": "Drop a rule, preference or feedback item they withdrew, by its id in brackets (rule:12, "
                       "feedback:7).",
        "input_schema": _obj({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "about",
        "label": "Checking who that is",
        "description": (
            "A person the user talks to, from memory's people index: who they are to the user, the names they go by, "
            "each place they talk (app, DM, group, channel) with counts and when last used, which is usual and how "
            "clearly, and the user's last messages to them. When the user picks a place for someone, pass "
            "prefer_route with that route's id and their words."
        ),
        "input_schema": _obj(
            {
                "name": {"type": "string"},
                "prefer_route": {"type": "string", "description": "e.g. route:12"},
                "words": {"type": "string", "description": "With prefer_route: their words that chose it, exactly."},
            },
            ["name"],
        ),
    },
    {
        "name": "todo",
        "label": "Keeping your to-do list",
        "description": "Add to, complete or list the user's to-dos. not_needed: no longer needed; wrong: never was one.",
        "input_schema": _obj(
            {
                "action": {"type": "string", "enum": ["add", "done", "not_needed", "wrong", "list"]},
                "text": {"type": "string", "description": "add: the to-do; otherwise its id in brackets (todo:3)."},
                "due": {"type": "string", "description": "add: local ISO date or date-time."},
            },
            ["action"],
        ),
    },
    {
        "name": "read_file",
        "label": "Reading a file",
        "description": "Read a text file on this PC (from start_line on); nothing opens on screen. Long files are "
                       "clipped and say where to continue.",
        "input_schema": _obj({"path": {"type": "string"}, "start_line": {"type": "integer"}}, ["path"]),
    },
    {
        "name": "fetch_url",
        "label": "Reading a web page",
        "description": "Download an http(s) page in the background and return its title and readable text, without "
                       "a browser. open_url is for showing it to the user.",
        "input_schema": _obj({"url": {"type": "string"}}, ["url"]),
    },
    {
        "name": "spawn_helpers",
        "label": "Asking helpers",
        "description": (
            "Run 1-4 read-only helpers at once, each on its own self-contained task (they see nothing of this "
            "conversation): memory, files, web pages, windows and tabs. Their short reports come back."
        ),
        "input_schema": _obj(
            {"tasks": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string"}}},
            ["tasks"],
        ),
    },
    {
        "name": "hand_over",
        "label": "Handing it to the task lane",
        "description": "Reader lane only: this request needs hands on the desktop. It is queued for the task lane "
                       "the moment you finish; then say in one line that it comes right after.",
        "input_schema": _obj({"why": {"type": "string"}}),
    },
]

#: The brain's mark that an action completes the request (v3.14, revamp D: a boolean). With ``true`` the request ends
#: the moment the call succeeds and its own report verifies the effect, with a line written by code from that call and
#: its report (:mod:`yuki.agent.finish`), not by the model: about 40 output tokens less per finishing call. The line
#: travels to the voice in this same field of the ``tool_call`` event (a string there, so voice v3.17's
#: verified-result path reads it unchanged). A string sent here by an older habit counts as ``finish_words``.
#: appOpen: the longest a launch_app call waits for a game its launcher is still starting, when a caller passes wait_s.
#: The dispatcher accepts it; it is kept out of launch_app's schema (the brain prompt's size budget), so the thinker
#: does not ask for it - its opening is confirmed by the light watch instead (``Agent._watch_launch``).
LAUNCH_WAIT_MAX_S = 60

FINISHES_FIELD = "finishes_with"
FINISHES_SCHEMA: dict[str, Any] = {
    "type": "boolean",
    "description": "true when this call alone completes what they asked: its verified report ends the request (no "
                   "done).",
}
#: The text field kept beside it: the outcome in the brain's own words, only when the report cannot name it the way the
#: user asked (a key, a click).
FINISH_WORDS_FIELD = "finish_words"
FINISH_WORDS_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "With finishes_with: the outcome in your words, only when its report cannot name it as they asked.",
}

# vfix2 (report 47): the situation check before a consequential action (N0, run by the agent loop) and the slider
# hand (N2). Appended here, after the registry's own list, so the hunks of other builders' tools stay apart.
from yuki.agent.situation import SITUATION_TOOL as _SITUATION_TOOL  # noqa: E402
from yuki.agent.slider_tool import SLIDER_TOOL as _SLIDER_TOOL  # noqa: E402

#: N10 (report 47): a request of several parts is a kept list. Live 3 Oct "open Chrome, open YouTube, scroll the
#: recommendations, then message my best friend on Discord" lost its last part when the user said "turn the volume up"
#: in between. The loop holds the list (``yuki.agent.loop``): what they add mid-run carries it, done is held while an
#: item is still to do, and it is in the run trace.
TASKS_TOOL: dict[str, Any] = {
    "name": "tasks",
    "label": "Keeping your list",
    "description": (
        "Several things asked at once: the list, done in order. Send it whole alongside your first step, and again "
        "alongside a step that finishes, changes or adds an item. What they add mid-run goes on it (a quick one "
        "first). Dropped only on their words (by), said aloud. done waits while an item is left."
    ),
    "input_schema": _obj(
        {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "what": {"type": "string"},
                        "state": {"type": "string", "enum": ["todo", "doing", "done", "dropped"]},
                        "by": {"type": "string", "description": "dropped: their words, exactly."},
                    },
                    "required": ["what", "state"],
                },
            },
        },
        ["items"],
    ),
}
#: N9 (report 47): the heard form of a name, learned as an alias when the user confirms or corrects a guess.
ABOUT_HEARD_AS: dict[str, Any] = {
    "type": "string",
    "description": "Only after they confirmed or corrected a misheard name (\"hey meant\" was Hemanth), never on "
                   "your own first guess: the words as heard, kept as that person's misheard form.",
}

#: Phase S (sCore, map-skills S3.5): the thinker's ``run_skill``, one of the user's usual ways (yuki.skills). Its
#: description carries the thinker menu, set per request by ``tool_params(skill_keys=, skills_menu=)``; with skills
#: off or none loaded it is left out of the block (and ``app_volume`` stays), so the cached block is unchanged.
from yuki.skills.menu import thinker_tool as _thinker_tool  # noqa: E402

RUN_SKILL_TOOL: dict[str, Any] = _thinker_tool("", ())

TOOL_SCHEMAS.extend([dict(_SITUATION_TOOL), dict(_SLIDER_TOOL), TASKS_TOOL, RUN_SKILL_TOOL])
for _t in TOOL_SCHEMAS:
    if _t.get("name") == "about":
        _t["input_schema"]["properties"]["heard_as"] = ABOUT_HEARD_AS

#: Name -> schema, for validation and lookups.
TOOLS_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOL_SCHEMAS}

#: Every tool name, in registry order. Immutable on purpose: the order is part of
#: the cached prefix, so a caller that reordered it would silently cost cache hits.
ALL_TOOL_NAMES: tuple[str, ...] = tuple(TOOLS_BY_NAME)

#: The tools that reach out and change something on the machine, as opposed to the
#: ones that only look. A list of names, not a rule: nothing here decides what the model does.
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
    "tab",
    "hover",
    "app_volume",
    "window_action",
    "write_clipboard",
    "set_slider",
    "run_skill",
)


# ---------------------------------------------------------------------------
# Tool metadata: what a call can do, a fixed property of the tool and its operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Traits:
    """What one tool call can do, fixed per tool and operation (revamp D: the authorization judge, the risk field,
    the input leases). Never read from words: the operation is a fixed argument of the tool (``window_action``'s and
    ``tab``'s ``action`` enum, ``type_text``'s ``press_enter`` boolean).

    Attributes:
        kinds: The autonomy-policy kinds it falls under (``yuki.ui.policy`` KINDS: read_screen, open_apps,
            click_type, send_messages, files, money); empty for reads and Yuki's own notes.
        needs: ``input`` (the global mouse and keyboard), ``focus`` (brings a window forward), ``window`` (acts on one
            window through UI Automation, nothing global), ``none``.
        destroys: It can close, end, delete or overwrite something (a window, an app, a tab, files).
        reaches: It can send, post or submit something to another person, or reach anything at all (a shell).
    """

    kinds: tuple[str, ...] = ()
    needs: str = "none"
    destroys: bool = False
    reaches: bool = False


#: Per tool. Keys and hotkeys can close or send anything (Ctrl+W closed the user's tabs; Enter sends), so they destroy
#: and reach whatever the window; a shell can do anything.
TOOL_TRAITS: dict[str, Traits] = {
    "launch_app": Traits(("open_apps",), "focus"),
    "focus_window": Traits(("open_apps",), "focus"),
    "open_url": Traits(("open_apps",), "focus"),
    "open_yuki": Traits((), "focus"),
    "click": Traits(("click_type",), "input"),
    "type_text": Traits(("click_type",), "input"),
    "hotkey": Traits(("click_type", "send_messages"), "input", destroys=True, reaches=True),
    "press": Traits(("click_type", "send_messages"), "input", destroys=True, reaches=True),
    "scroll": Traits(("read_screen",), "input"),
    "hover": Traits(("read_screen",), "input"),
    "run_powershell": Traits(("files",), "none", destroys=True, reaches=True),
    "tab": Traits(("open_apps",), "window"),
    "app_volume": Traits(("open_apps",), "none"),
    "window_action": Traits(("open_apps",), "window"),
    "write_clipboard": Traits(("click_type",), "none"),
    "set_slider": Traits(("click_type",), "input"),
    # phase S: a skill's rungs are gated by the engine itself (no focus or input with a game in front); a message
    # skill with text that sends is judged as a send (:func:`tool_traits`)
    "run_skill": Traits(("open_apps",), "input"),
}
#: Phase S: a ``run_skill`` that sends a message (the user's plain words, rule 8).
SKILL_SEND_TRAITS = Traits(("click_type", "send_messages"), "input", reaches=True)
#: Per operation, where one tool's operations differ (the fixed enum or boolean argument that names it).
OP_TRAITS: dict[tuple[str, str], Traits] = {
    ("window_action", "close"): Traits(("open_apps",), "window", destroys=True),
    ("window_action", "focus"): Traits(("open_apps",), "focus"),
    ("tab", "close"): Traits(("open_apps",), "window", destroys=True),
    ("type_text", "submit"): Traits(("click_type", "send_messages"), "input", reaches=True),
}


#: Facts of a call's input or target that make an otherwise quiet call one that can destroy or reach someone (review
#: 34: H2, M1, M2). Kept out of :data:`OP_TRAITS` so the tools' schemas (the ``risk`` field) do not change.
#: ``open_url`` of anything but an http(s) address: a file or a scheme handler runs through its default program.
LOCAL_TARGET_TRAITS = Traits(("open_apps",), "focus", destroys=True, reaches=True)
#: A click on a control whose UIA role commits something (Send, Delete, Sign out, Approve, Buy are buttons and menu
#: items): it can close, delete or send like a key can.
COMMITTING_CLICK_TRAITS = Traits(("click_type", "send_messages"), "input", destroys=True, reaches=True)
#: A click while text Yuki typed is waiting in that window: the click can send it.
PENDING_TEXT_CLICK_TRAITS = Traits(("click_type", "send_messages"), "input", reaches=True)
#: ``fetch_url`` after ``read_file`` in the same request: the address can carry what the file says out.
OUTBOUND_FETCH_TRAITS = Traits((), "none", reaches=True)
#: The UIA roles (``yuki.perception.tree`` control type names) whose click commits something. List items, tree items,
#: tabs and links only move around or select, so they stay ungated at the default policy.
COMMITTING_ROLES: frozenset[str] = frozenset({"Button", "MenuItem", "SplitButton"})
#: Schemes ``open_url`` opens in a browser (anything else is a file, a folder or another program's handler).
WEB_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _line_break_in(text: Any) -> bool:
    """A carriage return or line feed in typed text: typed key by key it is Enter (``\\r``) or Ctrl+Enter (``\\n``),
    which sends in most chats and mail (review 34 M1). A character test, not words."""
    return isinstance(text, str) and ("\r" in text or "\n" in text)


def _web_address(url: Any) -> bool:
    """``url`` is an http(s) address (its scheme parsed, a fixed machine format)."""
    from urllib.parse import urlparse

    try:
        return urlparse(str(url or "").strip()).scheme.casefold() in WEB_SCHEMES
    except ValueError:
        return False


def _skill_sends(tool_input: dict[str, Any] | None) -> bool:
    """Phase S (review L3): whether a ``run_skill`` call may send something to other people, from the skill's own data
    (``sends`` / ``sends_when`` in its connection file; :func:`yuki.skills.connection.sends_with`), never from slot
    names. A slot the model left out counts as a sending value (the resolver may fill it so). An unknown key is not
    a send (the engine refuses it)."""
    if not isinstance(tool_input, dict):
        return False
    try:
        from yuki.skills.connection import load, sends_with

        skill = load().get(str(tool_input.get("skill") or ""))
        slots = tool_input.get("slots") if isinstance(tool_input.get("slots"), dict) else {}
        return bool(skill is not None and sends_with(skill, slots, partial=True))
    except Exception:  # noqa: BLE001 - the files couldn't be read: judged as the plain tool
        return False


def tool_traits(name: str, tool_input: dict[str, Any] | None = None) -> Traits:
    """The traits of one call: its operation's, else its tool's, else none (a read, a note, a memory tool). Text with
    a line break types as Enter, so it submits like ``press_enter``; ``open_url`` of a file or a non-web scheme runs
    something (both facts of the input itself)."""
    tool_input = tool_input or {}
    op = None
    if name in ("window_action", "tab"):
        op = str(tool_input.get("action") or "")
    elif name == "type_text" and (tool_input.get("press_enter") is True or _line_break_in(tool_input.get("text"))):
        op = "submit"
    elif name == "open_url" and not _web_address(tool_input.get("url")):
        return LOCAL_TARGET_TRAITS
    elif name == "set_slider" and tool_input.get("keys"):
        # vfix3 review F1: the shortcut set_slider presses is a key combination like any hotkey (alt+f4 closes, enter
        # sends), so it is judged as one: the hotkey tool's own traits (the same object: keys by their tool alone)
        return TOOL_TRAITS["hotkey"]
    elif name == "run_skill" and _skill_sends(tool_input):
        return SKILL_SEND_TRAITS
    if op is not None and (name, op) in OP_TRAITS:
        return OP_TRAITS[(name, op)]
    return TOOL_TRAITS.get(name, Traits())


def call_traits(name: str, tool_input: dict[str, Any] | None = None, *, role: str | None = None,
                typed_pending: bool = False, files_read: bool = False) -> Traits:
    """:func:`tool_traits` plus the machine facts of the call's target the caller knows (review 34): a click on a
    control whose UIA role (from the latest read) is in :data:`COMMITTING_ROLES`, or a click while text Yuki typed
    waits in that window, can destroy or send; ``fetch_url`` after a ``read_file`` in the same request can carry the
    file out. Roles, a flag and the tool sequence only, never words."""
    traits = tool_traits(name, tool_input)
    if name == "click":
        if role and str(role) in COMMITTING_ROLES:
            return COMMITTING_CLICK_TRAITS
        if typed_pending:
            return PENDING_TEXT_CLICK_TRAITS
    if name == "fetch_url" and files_read:
        return OUTBOUND_FETCH_TRAITS
    return traits


def sends_for_certain(traits: Traits) -> bool:
    """The call sends something to someone for certain, not only might (revamp 38 §8c: the return card's "Nothing
    was sent."): text typed with Enter, or a click while Yuki's typed text waits in that window. A key or a click on
    a button only may send, so it does not count. The traits' identity, never words."""
    return traits is OP_TRAITS[("type_text", "submit")] or traits is PENDING_TEXT_CLICK_TRAITS


#: Tools some operation of which can destroy or reach someone: they carry the ``risk`` field (revamp D, 07 E1: the
#: rest no longer spend output tokens on it).
RISK_TOOLS: tuple[str, ...] = tuple(
    n for n in ACTION_TOOL_NAMES
    if TOOL_TRAITS.get(n, Traits()).destroys or TOOL_TRAITS.get(n, Traits()).reaches
    or any(t.destroys or t.reaches for (tool, _op), t in OP_TRAITS.items() if tool == n)
)

#: The brain's own declaration of what an action risks (``risk``): whether it can be undone and whether the user asked
#: for exactly this. Enforced in code on one point only: declared irreversible and not asked for -> the user confirms
#: first. Required on run_powershell; only on :data:`RISK_TOOLS`; ``effect`` only when irreversible.
RISK_FIELD = "risk"
RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Before it runs: reversible false when its effect cannot be undone from here (sent, deleted "
                   "outright, unsaved work lost, data overwritten).",
    "properties": {
        "reversible": {"type": "boolean"},
        "user_asked": {"type": "boolean", "description": "They asked for this very effect in this request."},
        "effect": {"type": "string", "description": "Only when not reversible: what it changes, in a few words."},
    },
    "required": ["reversible", "user_asked"],
    "additionalProperties": False,
}

#: The brain's own declaration that an action follows a playbook the task frame showed (``playbook``: "pb:12"), so its
#: use is measured (revamp Phase C, 03 §7.4).
PLAYBOOK_FIELD = "playbook"
PLAYBOOK_SCHEMA: dict[str, Any] = {"type": "string", "description": "The frame playbook (pb:N) this follows, if one."}

for _name in ACTION_TOOL_NAMES:  # plumbing: the same optional fields on each action's schema
    _props = TOOLS_BY_NAME[_name]["input_schema"]["properties"]
    _props[FINISHES_FIELD] = FINISHES_SCHEMA
    _props[FINISH_WORDS_FIELD] = FINISH_WORDS_SCHEMA
    if _name in RISK_TOOLS:
        _props[RISK_FIELD] = RISK_SCHEMA
    _props[PLAYBOOK_FIELD] = PLAYBOOK_SCHEMA

#: The step in the brain's own plain words (revamp 38 §8c, the user's 3 Oct feedback 12 and 14: "no technical things
#: for users"): what a call whose input says nothing a person reads (a shell command, keys, typed text) does for the
#: user, verb first, as they would say it ("close Slack", "open the address bar"). Every place the user sees a step
#: (the strip, the island, the chat's steps, the return card, the Activity ledger) shows it instead of the input; a
#: note, not what the call does, so it is left out wherever calls are compared (``yuki.agent.loopguard.NOTE_FIELDS``).
#: Required on run_powershell, optional on the key and typing tools.
WHAT_FIELD = "what"
WHAT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "What this does for them, in plain words, verb first, at most 6 words (\"close Slack\", \"open "
                   "the address bar\"): never a command, key name, path, id or number.",
}
WHAT_TOOLS: tuple[str, ...] = ("run_powershell", "type_text", "hotkey", "press")
for _name in WHAT_TOOLS:
    TOOLS_BY_NAME[_name]["input_schema"]["properties"][WHAT_FIELD] = WHAT_SCHEMA
TOOLS_BY_NAME["run_powershell"]["input_schema"]["required"] = ["command", WHAT_FIELD, RISK_FIELD]

#: What the agent knows about a call's target that a person can read, added by the loop to the ``tool_call`` event's
#: input (never sent by the model, never run): the control's role and name from the latest read, the app's name, the
#: tab's name, the call's traits (sends / destroys / reaches) and the brain's declared ``risk.effect``. The UI builds
#: its step words from this and the call's ``what`` only (``yuki.ui.status.describe_tool``), never from the
#: command, typed text or address.
STEP_ABOUT_FIELD = "_about"

#: The voice's step line (docs/VOICE.md v3.14): the few words Yuki's voice says as a step starts. Only on a spoken
#: request's tools (:func:`with_step_lines`), optional there since revamp D (07 E1: heard 0 of 9 times when required;
#: a typed request never carried it usefully). The brain's words about the call, not what it does: left out wherever
#: calls are compared (yuki.agent.loop).
STEP_FIELD = "say_now"
#: The tools that may carry it: every action, and the reads that can take a while.
STEP_TOOLS: tuple[str, ...] = ACTION_TOOL_NAMES + (
    "look_at_window", "read_page", "read_tabs", "memory", "read_file", "fetch_url", "spawn_helpers",
)
STEP_TOOLS = tuple(n for n in STEP_TOOLS if n in TOOLS_BY_NAME)   # (a build without one of them)
STEP_SCHEMA_VOICE: dict[str, Any] = {
    "type": "string",
    "description": "Optional, for a step they would wait on in silence: the few words you say as it starts, first "
                   "person, present tense (\"Opening your Warp tabs.\"), never an outcome or a guess.",
}
#: Kept for callers that import it (the typed schema no longer carries the field).
STEP_SCHEMA: dict[str, Any] = STEP_SCHEMA_VOICE

#: appPT (the user, 3 Oct: "will it show the real thinking? it would be nice if it shows the real thinking"): the
#: brain's own one-line note of what it is checking or doing next and why, shown live in the island's thinking and
#: working lines and on the run card's current step, in place of a canned "Thinking…". Chosen over streaming the
#: model's summarised thinking: a spoken request runs with thinking off (nothing would show), a summary is paragraphs
#: about "the user" that would need another model call to become one line, and this note costs a few output tokens
#: only on the calls that carry it. Optional, on the step tools and the situation check; a note, never run or compared
#: (``yuki.agent.loopguard.NOTE_FIELDS``, so the judge never reads it either).
NOW_FIELD = "now"
NOW_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "Optional, shown live: what you do and why, at most 12 plain words, no tool names or ids",
}
#: The actions, the situation check and the reads a step usually waits on (the rarer reads go without it).
NOW_TOOLS: tuple[str, ...] = tuple(dict.fromkeys(ACTION_TOOL_NAMES + (
    "check_situation", "look_at_window", "read_page", "memory")))
for _name in NOW_TOOLS:
    if _name in TOOLS_BY_NAME:
        TOOLS_BY_NAME[_name]["input_schema"]["properties"][NOW_FIELD] = NOW_SCHEMA


def with_step_lines(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool list for a spoken request: the optional ``say_now`` last on every step tool (a copy; plumbing).

    Last, not first (v3.14b): written first it primed the fast pass's choice of key (the Warp bench); the line is only
    said while its step runs. Optional since revamp D (07 E1)."""
    out = []
    for tool in tools:
        if tool.get("name") in STEP_TOOLS:
            schema = dict(tool["input_schema"])
            props = {**{k: v for k, v in schema.get("properties", {}).items() if k != STEP_FIELD},
                     STEP_FIELD: STEP_SCHEMA_VOICE}
            schema["properties"] = props
            schema["required"] = [r for r in schema.get("required", []) if r != STEP_FIELD]
            tool = {**tool, "input_schema": schema}
        out.append(tool)
    return out

#: What a ``done`` message says about the PC as it is now (docs/VOICE.md v3.14): the windows whose present state it
#: asserts; the loop checks, from this request's own calls and their verified reports, that each was looked at or
#: acted on with a verified result (never by reading the message).
ABOUT_FIELD = "about"
ABOUT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "integer"},
    "description": "hwnds whose present state your message tells (open, full screen, which tab shows, what plays); "
                   "each read or acted on with a verified result in this request. [] for none.",
}
TOOLS_BY_NAME["done"]["input_schema"]["properties"][ABOUT_FIELD] = ABOUT_SCHEMA

#: What the request achieved, in the brain's plain words, past tense (the user's 3 Oct return-card feedback: "it just
#: says done ... it should have said something which it achieved"): the return card's title, the strip's final word
#: and the thread's final line (``yuki.ui.step_words.achievement``). Never run, never compared, never spoken; the
#: ``message`` stays the reply. A line under two words, or one that looks like a command or path, is not shown (the
#: card falls back to the run's own effect steps).
ACHIEVED_FIELD = "achieved"
ACHIEVED_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "What you got done for them, plain past tense, verb first, at most 8 words, naming who or what "
                   "(\"Messaged Ana on Discord\", \"Turned Spotify up to 60%\", \"Closed Slack\"); for an answer, what "
                   "you found (\"Found when your call started\"). Never just \"Done\", never a command, path or id.",
}
TOOLS_BY_NAME["done"]["input_schema"]["properties"][ACHIEVED_FIELD] = ACHIEVED_SCHEMA
#: N3 (report 47): each state the done message tells, with the step of this request whose result showed it; the loop
#: checks the step ran and showed something (never by reading the words). Live 3 Oct "Spotify's own slider is already
#: at 100%" was said before any look at the window, then again after reading only the Windows mixer.
STATES_FIELD = "states"
STATES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "says": {"type": "string"},
            "seen_in": {"type": "string", "description": "The tool whose result in this request showed it."},
            "hwnd": {"type": "integer", "description": "Its window, 0 for none."},
        },
        "required": ["says", "seen_in"],
    },
    "description": "Each present state your message tells (a level, on/off, open, sent, already at its top) and the "
                   "step that showed it; unseen is not yours to tell. The mixer level (app_volume) is not an app's "
                   "own player volume. [] for none.",
}
TOOLS_BY_NAME["done"]["input_schema"]["properties"][STATES_FIELD] = STATES_SCHEMA
#: fix6 (report 73, release 6: two replies said "checked" / "confirmed" below that level, one claimed a queue opened and
#: closed when no action ran): the brain classes its own message; the loop holds a class its steps do not back
#: (``yuki.agent.finish.claim_gap``) and, below likely, the user gets the verdict's line unless it is ``tried``
#: (``finish.honest_text``). Judgement with worked examples, never a reading of the words.
CLAIMS_FIELD = "claims"
CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["checked", "done", "tried", "answer"],
    "description": "What your message claims. checked: you saw or confirmed the result (\"It's at 30% now, read back\"); "
                   "done: it got done (\"Closed Slack\"); tried: what you tried and that it failed or could not be "
                   "confirmed (\"I clicked Queue but couldn't see it open\"); answer: only what you found, nothing "
                   "done. Never past what your steps showed.",
}
TOOLS_BY_NAME["done"]["input_schema"]["properties"][CLAIMS_FIELD] = CLAIMS_SCHEMA
TOOLS_BY_NAME["done"]["input_schema"]["required"] = ["message", ABOUT_FIELD, ACHIEVED_FIELD, STATES_FIELD,
                                                     CLAIMS_FIELD]

#: Tools the agent loop runs itself (not the dispatcher, not memory): the helper team.
AGENT_TOOLS: frozenset[str] = frozenset({"spawn_helpers"})

#: What a read-only helper (yuki.agent.subagents) may use: looks and reads only. Enforced in code
#: twice: the helper is only shown these, a call to anything else is refused without running, and
#: its backend exposes only the reading functions.
HELPER_TOOLS: tuple[str, ...] = (
    "look_at_desktop", "look_at_window", "read_page", "read_tabs", "memory", "read_file", "fetch_url",
)

#: Actions after which the loop attaches a fresh read of the window acted on to
#: the next turn (see :meth:`Dispatcher.window_view`), so the model does not
#: spend a whole round trip calling ``look_at_window`` only to see the result.
#: ``run_powershell`` is not here: it acts on the system, not on a window. A list
#: of names for plumbing, not a rule about what the model should do.
AUTO_VIEW_TOOLS: frozenset[str] = frozenset(
    {"launch_app", "focus_window", "open_url", "click", "type_text", "hotkey", "press", "scroll", "tab", "hover",
     "set_slider"}
)

#: Input actions after which the window they went to may look different from its latest read, so a click
#: at raw x,y there is refused until it is read again (:meth:`Dispatcher._check_raw_point`). Plumbing.
_MARKS_ACTED: frozenset[str] = frozenset(
    {"type_text", "hotkey", "press", "scroll", "hover", "tab", "window_action", "set_slider"}
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

#: Report 58: the thinker's read of a window keeps up to this many elements (was 400: every 4 Oct Spotify read stopped
#: at 324 and lost what came last); the text stays bounded by the formatter's budget, which folds long lists.
TREE_ELEMENTS = 1000
#: ... and a read of one part asked for in full (``look_at_window`` with ``within``) up to this many.
WITHIN_TREE_ELEMENTS = 1500
#: A re-read while the view after actions settles gets at least this budget (s): on 3 Oct one got 0.3 s and replaced
#: a whole read with "these 189 elements are everything read until then".
_REREAD_MIN_S = 1.5

#: Outcome verification (report 58): around an action on a window, the window is read before (unless its events say
#: the latest read is still current) and after; the after-read waits for the action's first event at most this long,
#: then for the window to be quiet this long, at most the cap (s). Without events it reads at once (the input
#: actions already watched focus and page for most of a second).
OUTCOME_FIRST_EVENT_S = 0.6
OUTCOME_QUIET_S = 0.25
OUTCOME_CAP_S = 1.5
#: Read budgets for those two reads (s).
OUTCOME_READ_S = 2.0
#: Lines of "what changed" put into an action's result.
OUTCOME_LINES = 4
#: A read of the window at most this old (s), compared with a fresh read just before the action, tells which
#: elements were changing on their own (a clock, a progress bar), so the account leaves them out.
REFERENCE_FRESH_S = 6.0
#: A read stays current by the window's events for at most this long (s): past it the window is read again
#: (report 58 review, F4: a provider may move content without any event Yuki subscribes to).
REUSE_MAX_S = 2.0
#: percFix: an action that expects its target to change (a toggle asked for a state) waits up to this long (s) after
#: acting for the window's events and reads it again on each new event until the target reads as asked. Live 5 Oct
#: Spotify's Lyrics toggle showed nothing for over a second after it was pressed (no event within 0.6 s, the read
#: after it unchanged), and the view opened a few seconds later; the read 1.4 s after the click called it
#: "contradicted".
OUTCOME_TARGET_WAIT_S = 3.0
#: percFix: a mouse click at a raw point first brings the pointer there and waits (on the events, bounded) for what
#: the pointer alone does - a hover card, a video preview ending - so the click's account holds only what the click
#: did (live 5 Oct a click on an empty margin was "verified" by a video preview the pointer's move had closed).
HOVER_SETTLE_S = 1.0
#: The hover's first event is waited for at most this long (s; second review V5: the move usually raises none, and the
#: hover effects that matter - a preview ending, a card opening - raise one at once, so the full 0.6 s of an action's
#: first event was a fixed cost on every raw click). A window that had an event this recently before the move (s) is
#: busy on its own (a playing video): its quiet would not come before the cap, so it is given a shorter one (s), time
#: for a preview the pointer ended to go.
HOVER_FIRST_EVENT_S = 0.2
HOVER_BUSY_S = 0.25
HOVER_BUSY_CAP_S = 0.4


def _toggle_in(read: list[Any], element_id: int | None, was: Any = None) -> Any:
    """The toggle a click acted on, in a read after it (percFix): by its carried id; else by the RuntimeId it had
    just before the click; else the control the app drew again in its place - the same role with a Toggle pattern,
    its box over most of the old one (live 5 Oct T3 Code drew "Toggle right panel" anew under a longer name, and the
    read-back said "not in the window now" while the panel had closed). ``None`` when there is none. Facts about ids,
    roles, patterns and boxes; never names."""
    if element_id is not None:
        same = next((e for e in read if _field(e, "id") == element_id), None)
        if same is not None:
            return same
    if was is None:
        return None
    rid = _field(was, "runtime_id")
    if rid:
        same = next((e for e in read if _field(e, "runtime_id") and list(_field(e, "runtime_id")) == list(rid)), None)
        if same is not None:
            return same
    old = _field(was, "bounds")
    if not old or len(old) != 4 or _field(was, "hidden"):
        return None

    def area(b: Any) -> int:
        return max(0, int(b[2]) - int(b[0])) * max(0, int(b[3]) - int(b[1]))

    best, best_share = None, 0.0
    for e in read:
        box = _field(e, "bounds")
        if (_field(e, "role") != _field(was, "role") or "toggle" not in (_field(e, "patterns") or ())
                or not box or len(box) != 4 or _field(e, "hidden")):
            continue
        overlap = area((max(old[0], box[0]), max(old[1], box[1]), min(old[2], box[2]), min(old[3], box[3])))
        share = overlap / max(1, max(area(old), area(box)))
        if share > best_share:
            best, best_share = e, share
    return best if best_share >= _SAME_PLACE_SHARE else None


def _box_area(b: Any) -> int:
    """The area of a ``(left, top, right, bottom)`` box, 0 for anything else."""
    if not b or len(b) != 4:
        return 0
    return max(0, int(b[2]) - int(b[0])) * max(0, int(b[3]) - int(b[1]))


def _container_share() -> float:
    """:data:`yuki.agent.situation.CONTAINER_SHARE`: a box larger than this share of the window is a container."""
    from yuki.agent.situation import CONTAINER_SHARE

    return float(CONTAINER_SHARE)


#: How much of the larger of two boxes they must share to be one control drawn again in place (:func:`_toggle_in`).
_SAME_PLACE_SHARE = 0.6


def _on_a_control(read: list[Any], x: int, y: int) -> bool:
    """Whether point (x, y) lies on something a click acts on in ``read`` (percFix): an interactive element whose box
    holds the point and that is not a container covering much of the window (the window itself, a page's document,
    a pane: they take the keyboard, so they read as interactive, but clicking their empty part does nothing of its
    own; the same rule as the raw-click guard). Facts about boxes and patterns only."""
    from yuki.agent.situation import CONTAINER_SHARE

    def area(b: Any) -> int:
        return max(0, int(b[2]) - int(b[0])) * max(0, int(b[3]) - int(b[1]))

    boxes = [e for e in read if _field(e, "bounds") and len(_field(e, "bounds")) == 4 and not _field(e, "hidden")]
    whole = max((area(_field(e, "bounds")) for e in boxes), default=0) or 1
    for e in boxes:
        b = _field(e, "bounds")
        if area(b) > CONTAINER_SHARE * whole or not _field(e, "is_interactive"):
            continue
        if int(b[0]) <= x < int(b[2]) and int(b[1]) <= y < int(b[3]):
            return True
    return False


#: Roles of a surface a click opens (second review V6), and how far from the click's point (px) its box may lie and
#: still be the click's: a menu or dialog appearing there is the click's effect even when the pointer's arrival had
#: taken away the control that was at the point.
_OPENED_ROLES = frozenset({"Menu", "Dialog", "Window"})
_OPENED_NEAR_PX = 300


def _surface_opened_near(change: Any, x: int, y: int) -> bool:
    """Whether a menu, dialog or window appeared in ``change`` with its box within :data:`_OPENED_NEAR_PX` of point
    (x, y) (second review V6). Facts about roles and boxes only."""
    for element, _size in getattr(change, "appeared", None) or ():
        box = _field(element, "bounds")
        if _field(element, "role") not in _OPENED_ROLES or not box or len(box) != 4:
            continue
        dx = max(int(box[0]) - x, 0, x - int(box[2]))
        dy = max(int(box[1]) - y, 0, y - int(box[3]))
        if max(dx, dy) <= _OPENED_NEAR_PX:
            return True
    return False


def _own_toggle_state(via: Any) -> str | None:
    """``"on"``/``"off"``: the state a toggle's own Toggle pattern reported right after it was toggled, when that
    state differs from the one before (percFix; :func:`yuki.actions.patterns.element_action`); ``None`` when it was
    clicked by the mouse, did not read as changed, or is indeterminate."""
    payload = getattr(via, "payload", None)
    details = payload.get("details") if isinstance(payload, dict) else None
    own = details.get("toggle_state") if isinstance(details, dict) else None
    if not isinstance(own, dict) or own.get("before") == own.get("after"):
        return None
    return {0: "off", 1: "on"}.get(own.get("after"))


#: fix6: ``click``'s ``toggle_to`` values for a control that switches its window's full screen: judged by the window
#: (measured from Windows), never by the control's own on/off.
SCREEN_WANTS: tuple[str, str] = ("full_screen", "not_full_screen")


def _toggle_keys(element: Any) -> list[tuple]:
    """fix6: what identifies a toggle across its two states: its RuntimeId, and its role with its name (a toggle
    whose state is in its name is known by either name it had). Facts of the read only."""
    if element is None:
        return []
    keys: list[tuple] = []
    rid = _field(element, "runtime_id")
    if rid:
        keys.append(("rid", tuple(rid)))
    name = " ".join(str(_field(element, "name") or "").split())
    if name:
        keys.append(("name", str(_field(element, "role") or ""), name))
    return keys


def _renamed_same_state(was: Any, now: Any) -> bool:
    """fix6: the same toggle (the same RuntimeId) under another name with the same on/off in both reads: its state
    shows in its name, not in its on/off (Spotify's "Enter full screen" / "Exit full screen", off in both). Facts of
    the reads (ids, names, states); the names are compared, never read for meaning."""
    if was is None or now is None:
        return False
    rid0, rid1 = _field(was, "runtime_id"), _field(now, "runtime_id")
    if not rid0 or not rid1 or list(rid0) != list(rid1):
        return False
    if " ".join(str(_field(was, "name") or "").split()) == " ".join(str(_field(now, "name") or "").split()):
        return False

    def state(e: Any) -> str | None:
        states = [str(s) for s in (_field(e, "states") or ())]
        return "on" if "on" in states else "off" if "off" in states else None

    return state(was) is not None and state(was) == state(now)


def _without_point_verdict(text: str) -> str:
    """A wheel scroll's summary without its single-point "it did not scroll" ending (percFix: said only when the
    window's reads show its content did move, so the point's verdict was wrong)."""
    try:
        from yuki.actions.scrolling import POINT_STILL
    except Exception:  # noqa: BLE001
        return text
    return text.replace(POINT_STILL, "", 1)


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
    lane_tools: Iterable[str] = (),
) -> tuple[str, ...]:
    """Every tool the model may be shown, in registry order.

    Args:
        names: a requested subset, as understood by :func:`resolve_tool_names`.
        screenshot_policy: the policy in force; ``None`` reads the configured
            default.
        memory: whether Yuki's memory is installed; without it the
            :data:`MEMORY_TOOLS` are left out.
        lane_tools: the :data:`LANE_ONLY_TOOLS` this lane is shown (the reader's
            ``hand_over``); every other lane-only tool is left out.

    Returns:
        The names, minus any whose policy switches them off entirely.
    """
    allowed = resolve_tool_names(names)
    extra = set(lane_tools or ())
    if allowed is not None and extra:
        allowed = tuple(n for n in ALL_TOOL_NAMES if n in set(allowed) | extra)
    dropped: set[str] = set(LANE_ONLY_TOOLS) - extra
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

    The control tools are always added: without ``done`` a turn can never
    end, without ``ask_user`` the agent cannot pause, without
    ``note_to_self`` it loses its memory as older context ages out, and without
    ``say`` it cannot keep a listening user in the loop.

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
    lane_tools: Iterable[str] = (),
    skill_keys: Sequence[str] | None = None,
    skills_menu: str = "",
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
        skill_keys: Phase S: the skills loaded (``Settings.skills`` on). Empty or ``None``: no ``run_skill`` (the
            block is as before). With a ``volume`` skill the thinker's ``app_volume`` is left out (the volume skill
            covers the mixer too); it comes back with ``skills = false``.
        skills_menu: The thinker menu, the ``run_skill`` description's tail (it changes only with the menu's
            version, so the block stays byte-stable between requests).

    Returns:
        A fresh list of tool dicts in registry order, carrying only what the API
        accepts -- the human ``label`` is dropped here, since it exists for the UI
        and an unexpected key would be rejected. The caller may not mutate the
        module copy.
    """
    allowed = set(
        available_tool_names(names, screenshot_policy=screenshot_policy, memory=memory, lane_tools=lane_tools)
    )
    keys = [str(k) for k in (skill_keys or ()) if str(k)]
    if not keys:
        allowed.discard("run_skill")
    elif "volume" in keys:
        allowed.discard("app_volume")
    tools = [
        {k: v for k, v in (_skill_schema(t, keys, skills_menu) if t["name"] == "run_skill" else t).items()
         if k != "label"}
        for t in TOOL_SCHEMAS
        if t["name"] in allowed
    ]
    if cacheable and tools:
        from yuki.agent.prompt import STABLE_CACHE_CONTROL

        tools[-1] = {**tools[-1], "cache_control": dict(STABLE_CACHE_CONTROL)}
    return tools


def _skill_schema(base: dict[str, Any], keys: Sequence[str], menu: str) -> dict[str, Any]:
    """The ``run_skill`` schema of this request: the registry's one (with the fields added to every action tool at
    import, such as ``say_now``), its skill enum and description from the loaded skills and the thinker menu."""
    fresh = _thinker_tool(menu, list(keys))
    schema = json.loads(json.dumps(base.get("input_schema") or {}))
    props = schema.setdefault("properties", {})
    props["skill"] = fresh["input_schema"]["properties"]["skill"]
    return {**base, "description": fresh["description"], "input_schema": schema}


#: The phrase for each kind of the one memory tool (and for its old tool names, which the voice and the UI may still
#: be handed): what the step is doing, in plain words.
MEMORY_KIND_LABELS: dict[str, str] = {
    "recall": "Remembering", "activity": "Looking at how your time went", "calls": "Checking your calls",
    "call_timeline": "Going through the call", "threads": "Looking at your work", "before": "Looking back",
    "links": "Finding that link",
}


def tool_label(name: str, tool_input: dict[str, Any] | None = None) -> str:
    """The one-line human phrase for a tool, for the UI to show while it runs.

    Args:
        name: Tool name as the model called it.
        tool_input: Its arguments, when the caller has them (the memory tool's phrase follows its kind).

    Returns:
        The tool's ``label``, or an empty string for a name that is not a tool --
        the UI is not the place to raise over an unknown tool.
    """
    if name == "memory" and isinstance(tool_input, dict) and tool_input.get("kind") in MEMORY_KIND_LABELS:
        return MEMORY_KIND_LABELS[str(tool_input["kind"])]
    schema = TOOLS_BY_NAME.get(name)
    if schema:
        return str(schema.get("label") or "")
    return MEMORY_KIND_LABELS.get(name, "")


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
        accept_suggestion: bool = False,
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
    # Optional (the voice's hands, yuki.voice.hands): minimize / maximize / restore / close /
    # focus one window, verified. A backend without it simply has no such hand.
    def window_action(self, hwnd: int, action: str) -> Any: ...  # pragma: no cover - protocol only
    # Optional (the brain's read_tabs and tab tools): a window's own tab strip (a TabStrip whose
    # tabs carry their UIA RuntimeId as tab_id), and one tab selected or closed by that id.
    def read_tabs(self, hwnd: int, *, budget_s: float = 0.2) -> Any: ...  # pragma: no cover - protocol only
    def tab(self, hwnd: int, tab_id: str, action: str) -> Any: ...  # pragma: no cover - protocol only

    # Optional, and not part of the architecture contract: a backend without it
    # simply never gets pre-warmed. See :meth:`Dispatcher.prewarm_shell`.
    def prewarm_shell(self) -> bool: ...  # pragma: no cover - protocol only
    # Optional too: what the user is engaged in (see
    # :func:`yuki.perception.system.activity_facts`), and its warm-up.
    def activity_facts(self) -> dict[str, Any]: ...  # pragma: no cover - protocol only
    # Optional too (v3.5): a pointer move that reports what appeared, and one app's own volume.
    def hover(self, x: int, y: int, *, expect_hwnd: int | None = None, label: str = "") -> Any: ...  # pragma: no cover
    def app_volume(self, app: str, *, level: float | None = None, change: float | None = None,
                   mute: bool | None = None) -> Any: ...  # pragma: no cover - protocol only
    def warm_activity(self) -> None: ...  # pragma: no cover - protocol only
    # Optional too (2026-09-29, Phase 2 "eyes"): what has keyboard focus with its selection and caret text
    # (a yuki.perception.tree.FocusSnapshot), the clipboard read / written, and the top-level window at a point.
    def focus_snapshot(self, *, timeout_s: float = 0.6,
                       ready_wait_s: float | None = None) -> Any: ...  # pragma: no cover - protocol only
    def read_clipboard(self) -> Any: ...  # pragma: no cover - protocol only
    def write_clipboard(self, text: str) -> Any: ...  # pragma: no cover - protocol only
    def window_at(self, x: int, y: int) -> int: ...  # pragma: no cover - protocol only
    # Optional too (fix6): how a window is shown, measured (minimized / fullscreen / maximized / normal).
    def window_state(self, hwnd: int) -> str | None: ...  # pragma: no cover - protocol only


#: The tool input fields that name a window (``look_at_window``'s ``hwnd``, an action's ``expect_hwnd`` guard).
WINDOW_KEYS: tuple[str, ...] = ("hwnd", "expect_hwnd")
#: The opacity threshold as a whole percent, for the words of a refused click (:mod:`yuki.core.opacity`).
from yuki.core.opacity import OPAQUE_COVERAGE as _OPAQUE_COVERAGE  # noqa: E402

OPAQUE_PERCENT = int(round(_OPAQUE_COVERAGE * 100))
#: A window takes a vanished one's place when it is of the same process and scores at least this on
#: ``SUCCESSOR_TITLE_WEIGHT * title likeness + (1 - weight) * overlap of the two windows' rectangles`` (both 0-1,
#: measured), and the next best is at least :data:`SUCCESSOR_MARGIN` behind (else it is not clear which).
SUCCESSOR_MIN_SCORE = 0.55
SUCCESSOR_TITLE_WEIGHT = 0.6
SUCCESSOR_MARGIN = 0.10


def _rect_overlap(a: Any, b: Any) -> float:
    """Intersection over union of two ``(left, top, right, bottom)`` rectangles (0 when either is unknown)."""
    try:
        al, at, ar, ab = (float(v) for v in a)
        bl, bt, br, bb = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    iw, ih = max(0.0, min(ar, br) - max(al, bl)), max(0.0, min(ab, bb) - max(at, bt))
    inter = iw * ih
    union = max(0.0, ar - al) * max(0.0, ab - at) + max(0.0, br - bl) * max(0.0, bb - bt) - inter
    return inter / union if union > 0 else 0.0


def find_successor(was: dict[str, Any], windows: Sequence[dict[str, Any]], *, gone: int) -> dict[str, Any] | None:
    """The window that took a vanished one's place, or ``None``: of the same process (``was["process"]``, the
    image name), the best by title likeness (``difflib``'s ratio over the two titles, a measured similarity of two
    window titles) and overlap of their rectangles, scoring at least :data:`SUCCESSOR_MIN_SCORE` and clear of the
    next by :data:`SUCCESSOR_MARGIN`. Returns the window with ``title_match``, ``overlap`` and ``score``."""
    import difflib

    process = str(was.get("process") or "").casefold()
    if not process:
        return None
    title = str(was.get("title") or "").casefold()
    scored: list[dict[str, Any]] = []
    for w in windows:
        try:
            hwnd = int(w.get("hwnd"))
        except (TypeError, ValueError):
            continue
        if hwnd == int(gone) or str(w.get("process_name") or "").casefold() != process:
            continue
        other = str(w.get("title") or "").casefold()
        likeness = difflib.SequenceMatcher(None, title, other).ratio() if (title or other) else 0.0
        overlap = _rect_overlap(was.get("rect"), w.get("bounds"))
        score = SUCCESSOR_TITLE_WEIGHT * likeness + (1.0 - SUCCESSOR_TITLE_WEIGHT) * overlap
        scored.append({**w, "title_match": round(likeness, 3), "overlap": round(overlap, 3), "score": round(score, 3)})
    scored.sort(key=lambda s: -s["score"])
    if not scored or scored[0]["score"] < SUCCESSOR_MIN_SCORE:
        return None
    if len(scored) > 1 and scored[0]["score"] - scored[1]["score"] < SUCCESSOR_MARGIN:
        return None
    return scored[0]


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
        "window_action",
        "watch_launch",
    }

    def window_exists(self, hwnd: int) -> bool:
        """Whether ``hwnd`` is still a window (``IsWindow``; microseconds, no UI Automation)."""
        import ctypes

        return bool(ctypes.windll.user32.IsWindow(int(hwnd)))

    def prewarm_apps(self) -> int:
        """Read the Start menu list now (it is cached for the process): 1-3 s of PowerShell
        that the first ``launch_app`` / ``open_url(app=...)`` would otherwise pay in front of
        the user (the 25 Sept live "open YouTube on Chrome" did). Returns how many apps."""
        import importlib

        launch = importlib.import_module("yuki.actions.launch")
        apps = launch.get_start_apps()
        try:
            launch._start_links(apps)   # appOpen: the shortcuts' targets and arguments (~0.15 s), read once too
        except Exception:  # noqa: BLE001 - a launch reads them itself then
            pass
        return len(apps)

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

    # Optional, for the voice's hands (docs/VOICE.md §5.6), not contract functions.
    def read_tabs(self, hwnd: int, *, budget_s: float = 0.2) -> Any:
        """:func:`yuki.perception.tabs.read_tabs`: a window's own tab strip, read-only."""
        import importlib

        return importlib.import_module("yuki.perception.tabs").read_tabs(hwnd, budget_s=budget_s)

    def element_action(self, hwnd: int, element: dict[str, Any], verb: str, **kwargs: Any) -> Any:
        """:func:`yuki.actions.patterns.element_action`: a read element acted on through its UI Automation pattern
        (revamp D, S7), or ``None`` when it cannot be (the dispatcher then uses the mouse or keys)."""
        import importlib

        return importlib.import_module("yuki.actions.patterns").element_action(hwnd, element, verb, **kwargs)

    def select_tab(self, hwnd: int, index: int, *, expect_name: str | None = None) -> Any:
        """:func:`yuki.actions.tabs.select_tab`: one tab of a window made the one showing, verified."""
        import importlib

        return importlib.import_module("yuki.actions.tabs").select_tab(hwnd, index, expect_name=expect_name)

    def tab(self, hwnd: int, tab_id: str, action: str) -> Any:
        """:func:`yuki.actions.tabs.tab_action`: the brain's ``tab`` tool. Named as the tool, like every
        action, so a look-only backend (``HandsBlockedBackend``) refuses it by that name."""
        import importlib

        return importlib.import_module("yuki.actions.tabs").tab_action(hwnd, tab_id, action)

    def hover(self, x: int, y: int, *, expect_hwnd: int | None = None, label: str = "") -> Any:
        """:func:`yuki.actions.hover.hover`: the pointer onto a point, and what appeared there."""
        import importlib

        return importlib.import_module("yuki.actions.hover").hover(x, y, expect_hwnd=expect_hwnd, label=label)

    def point_at(self, x: int, y: int, *, expect_hwnd: int | None = None) -> Any:
        """:func:`yuki.actions.input.point_at`: the pointer onto a point, nothing else (the move a click makes, taken
        first so the click's account holds only the click's effect)."""
        import importlib

        return importlib.import_module("yuki.actions.input").point_at(x, y, expect_hwnd=expect_hwnd)

    def app_volume(self, app: str, *, level: float | None = None, change: float | None = None,
                   mute: bool | None = None) -> Any:
        """:func:`yuki.actions.app_volume.set_app_volume`: one app's own volume in the mixer, read back."""
        import importlib

        return importlib.import_module("yuki.actions.app_volume").set_app_volume(
            app, level=level, change=change, mute=mute)

    def set_slider(self, hwnd: int, element: dict[str, Any], *, to: float | None = None, by: float | None = None,
                   press: Callable[..., Any] | None = None, **reveal_and_keys: Any) -> Any:
        """:func:`yuki.actions.slider.set_slider`: a slider set by RangeValue or its own keys, read back (N2); vfix3:
        ``hover``, ``hotkey`` and ``keys`` (a bar shown by the pointer, a learned shortcut read back)."""
        import importlib

        return importlib.import_module("yuki.actions.slider").set_slider(hwnd, element, to=to, by=by, press=press,
                                                                        **reveal_and_keys)

    def audio_apps(self) -> list[dict[str, Any]]:
        """:func:`yuki.actions.app_volume.audio_apps`: the apps with sound and their own levels, read-only."""
        import importlib

        return importlib.import_module("yuki.actions.app_volume").audio_apps()

    def master_volume(self) -> dict[str, Any] | None:
        """:func:`yuki.perception.audio.master_volume`: the output volume, read-only."""
        import importlib

        return importlib.import_module("yuki.perception.audio").master_volume()

    # Optional (2026-09-29): "this", the clipboard, and which window is at a point.
    def focus_snapshot(self, *, timeout_s: float = 0.6, ready_wait_s: float | None = None) -> Any:
        """:func:`yuki.perception.tree.focus_snapshot`: focus, selection and caret text, read-only."""
        import importlib

        return importlib.import_module("yuki.perception.tree").focus_snapshot(timeout_s=timeout_s,
                                                                               ready_wait_s=ready_wait_s)

    def read_clipboard(self) -> Any:
        """:func:`yuki.perception.clipboard.read_clipboard`: text and formats, private content never read."""
        import importlib

        return importlib.import_module("yuki.perception.clipboard").read_clipboard()

    def write_clipboard(self, text: str) -> Any:
        """:func:`yuki.actions.clipboard.write_clipboard`: text put on the clipboard, read back."""
        import importlib

        return importlib.import_module("yuki.actions.clipboard").write_clipboard(text)

    def window_state(self, hwnd: int) -> str | None:
        """:func:`yuki.perception.windows.window_state`: how a window is shown, measured from Windows (minimized,
        fullscreen: it covers its whole monitor with no caption, maximized, normal), ``None`` when gone. Read-only."""
        import importlib

        return importlib.import_module("yuki.perception.windows").window_state(int(hwnd))

    def window_at(self, x: int, y: int) -> int:
        """The top-level window at a screen point (``WindowFromPoint``, its root), 0 for none. Read-only."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")  # its own instance: the argtypes set here leak nowhere
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        return int(user32.GetAncestor(hwnd, 2) or hwnd or 0) if hwnd else 0  # GA_ROOT

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

Kind = Literal["normal", "ask_user", "done", "note", "say", "tasks"]


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


#: The view after actions lists only what changed since the window's previous read (v3.15: live 30 Sept every
#: step re-sent a ~21.8k-character Discord tree) when that list fits in this many characters ...
CHANGES_VIEW_CHARS = 4000
#: ... and at most this share of the window's elements is new or changed (more is a new view: read whole).
CHANGES_VIEW_SHARE = 0.5
#: Named elements that went away listed by name in a list of changes, at most.
_CHANGES_GONE_NAMED = 6


def _field(element: Any, key: str, default: Any = None) -> Any:
    """A tree element's field, whether the element is a plain dict (a stub, a log) or a dataclass."""
    if isinstance(element, dict):
        return element.get(key, default)
    return getattr(element, key, default)


def _renumbered(element: Any, new_id: int, parent: int) -> Any:
    if isinstance(element, dict):
        return {**element, "id": new_id, "parent": parent}
    return dataclasses.replace(element, id=new_id, parent=parent)


def _tree_with(tree: Any, elements: list[Any]) -> Any:
    """The same tree read with these elements (the read's own header facts unchanged)."""
    if isinstance(tree, dict):
        return {**tree, "elements": elements}
    import copy

    clone = copy.copy(tree)
    clone.elements = elements
    return clone


def _shown_as(element: Any) -> tuple:
    """What a tree line shows of an element, besides its id and position: a change here is a change to list."""
    return (_field(element, "role"), _field(element, "name") or "", _field(element, "value"),
            tuple(_field(element, "states") or ()), bool(_field(element, "is_focused")),
            tuple(_field(element, "patterns") or ()), _field(element, "description") or "")


def carry_ids(previous: dict[int, Any], elements: list[Any], ceiling: int) -> tuple[dict[int, int], int]:
    """Ids for a new read of a window that keep the previous read's ids for the same elements (v3.15; report 58:
    by RuntimeId first, then AutomationId, then structure - :func:`yuki.perception.model.carry_refs`). Everything
    else gets an id above ``ceiling`` (never one used before in this window's reads), so an id the model holds from
    an earlier read either still means the same element or is gone.

    Returns ``(new read's id -> carried id, new ceiling)``.
    """
    from yuki.perception.model import carry_refs

    return carry_refs(previous, elements, ceiling)


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
    #: ``text`` lists only what changed since the window's previous read (v3.15); ``full_chars`` is the whole
    #: tree's length, for the log.
    changes: bool = False
    full_chars: int = 0


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
        max_tree_elements: int = TREE_ELEMENTS,
        tree_timeout_s: float = 6.0,
        verify_outcomes: bool | None = None,
    ) -> None:
        self.backend = backend if backend is not None else default_backend()
        self._verify_choice = verify_outcomes
        self.screenshot_policy = screenshot_policy
        self.tool_timeout_s = tool_timeout_s
        self.max_tree_elements = max_tree_elements
        self.tree_timeout_s = tree_timeout_s
        #: Reads handed to the model so far (``read #N`` in a tree's header).
        self._reads = 0
        #: hwnd -> ``(read number, {element id: element})`` of the latest tree
        #: of that window the model was given, for element-id input targets.
        self._latest_trees: dict[int, tuple[int, dict[int, dict[str, Any]]]] = {}
        #: hwnd -> the reads whose ids still hold in the latest read (ids carried over, v3.15), and the highest id
        #: any of them used (a new element never reuses an id).
        self._id_lineage: dict[int, set[int]] = {}
        self._id_ceiling: dict[int, int] = {}
        #: hwnd -> the window's screen model (report 58): ids carried across every read of it by RuntimeId, then
        #: AutomationId, then structure, and what each id was once it is gone (yuki.perception.model.WindowModel).
        self._models: dict[int, Any] = {}
        #: The latest read handed to the model, as the backend's own tree object with its ids carried.
        self.last_tree: Any = None
        #: Report 58: compare the window before and after each click, key and scroll and say what changed (on for
        #: the real desktop; a stub desk turns it on to test it). ``events``: the UI Automation event hub that says
        #: whether a read is still current and when a window has settled (yuki.perception.events; the process's
        #: shared one on the real desktop, None: reads instead).
        self.verify_outcomes = (isinstance(self.backend, _LazyRealBackend) if self._verify_choice is None
                                else bool(self._verify_choice))
        #: Whether reads use the event hub: unset, it follows ``verify_outcomes`` (report 58 review, F8: the voice's
        #: quick hands, which do not bracket their actions, never subscribe a window), set it to force either way.
        self._use_events: bool | None = None
        self.events: Any = None
        #: hwnd -> (event generation, monotonic time, opaque) when the model's latest observation of it was taken,
        #: and that read itself (reused by the view after actions while the window's events show no change).
        self._observed: dict[int, tuple[int | None, float, bool]] = {}
        self._observed_trees: dict[int, Any] = {}
        #: The window of the most recent tree read, the default for an element id.
        self._latest_tree_hwnd: int | None = None
        #: hwnd -> facts of the latest read of it given to the model (read number, rectangle, title, opaque,
        #: whether Yuki has acted on it since): what a click at raw x,y is checked against.
        self._read_facts: dict[int, dict[str, Any]] = {}
        #: fix6 (report 73, release 6): toggles whose own on/off does not follow what they switch, learned from a
        #: click this session: ``(hwnd, key) -> "full_screen"`` (a click flipped the window's full screen while its
        #: state read the same) or ``"name"`` (its name changed, its state did not). ``key`` is its RuntimeId, else
        #: its role and a name it had. Never "already" from such a toggle's own state.
        self._untracked_toggles: dict[tuple[int, tuple], str] = {}
        #: What each window was (process, title, place) as the overviews showed it: a call on one that vanished goes
        #: to the one that took its place (:meth:`_follow_vanished`).
        self._windows_seen: dict[int, dict[str, Any]] = {}
        #: Refuse a click at raw x,y on a window that is opaque, was never read, or changed since its latest
        #: read (the element click's staleness rule, for coordinates). On for every dispatcher.
        self.guard_raw_clicks = True
        #: Phase S (D17(1), fGate's hand-on L5): re-read "is a game in front?" right before every action that takes
        #: focus or sends input (``run_powershell`` too: a script may start a window), and refuse it then (nothing is
        #: sent; ``refused = "game"``). ``None``: on for the real desktop only (a stand-in desk never reads the live
        #: foreground); a test turns it on with the game check stubbed.
        self.game_guard: bool | None = None
        #: Phase S: what the thinker's ``run_skill`` needs from the request (``memory``, the user's ``words``,
        #: ``request_id``, ``log``, ``settings``), set by the agent loop at each request's start.
        self.skill_context: dict[str, Any] = {}
        #: Phase S (sWire, sVoice r1 M2): where the per-app "Act" switches are read for ``run_skill``'s window-touching
        #: rungs (``yuki.agent.judge.PolicyReader``); ``None``: the judge's shared reader of the stored policy.
        self.act_policy: Any = None
        #: Append the live picture of the screen (yuki.memory.now: what the windows on screen show, popups,
        #: the focus line) to :meth:`overview_text`, i.e. the brain's per-turn situation block. Off unless the
        #: owner of the dispatcher turns it on (the agent loop, from ``Settings.now_in_situation``).
        self.include_now = False
        #: Wait bounds (s) of that picture: the memory service's answer, and the focus read.
        self.now_wait_s = 0.05
        self.focus_wait_s = 0.05
        #: The memory service's picture of the screen as the last situation block read it (``None``: none).
        self.last_now_board: dict[str, Any] | None = None
        #: The focus read of the last situation block (a ``FocusSnapshot``; ``None``: none, or focus in Yuki's own
        #: window): the task frame's "this" (``yuki.agent.frame``) reuses it instead of reading again.
        self.last_focus: Any = None
        #: Quieter hands (revamp D, S7): the gate that waits for the user to stop typing or moving the mouse before
        #: global input (``yuki.actions.leases.InputGate``; ``None``: no waiting, the default for stub desks and the
        #: voice's hands), the lines it wants said (the agent loop drains them into ``Say`` events), and the cancel
        #: check it polls while it waits.
        self.input_gate: Any = None
        self.notices: Any = None
        self.cancel_check: Callable[[], bool] | None = None
        #: Tab names by window and id, from the latest read of each strip (the finish line names the tab).
        self.tab_names: dict[int, dict[str, str]] = {}

    def enable_now(self, settings: Any = None) -> bool:
        """Turn the per-turn Now picture on from ``settings`` (``now_in_situation``, ``now_wait_ms``,
        ``focus_wait_ms``; a default :class:`~yuki.config.Settings` when ``None``) and start the focus reader
        in the background so the first turn already has a focus line. Returns whether it is on. The agent
        loop calls this once for the brain's own dispatcher (the voice's hands and the helpers keep theirs off)."""
        if settings is None:
            from yuki.config import Settings

            settings = Settings()
        self.include_now = bool(getattr(settings, "now_in_situation", True))
        self.now_wait_s = float(getattr(settings, "now_wait_ms", 50.0)) / 1000.0
        self.focus_wait_s = float(getattr(settings, "focus_wait_ms", 50.0)) / 1000.0
        if self.include_now and isinstance(self.backend, _LazyRealBackend):
            import threading

            def warm() -> None:
                try:
                    import importlib

                    importlib.import_module("yuki.perception.tree").warm_focus_reader()
                    from yuki.memory.now import shared_client

                    client = shared_client()
                    if client is not None:
                        client.now(wait_s=0.5)  # connects the pipe ahead of the first turn
                except Exception:  # noqa: BLE001 - the first turn just goes without
                    pass

            threading.Thread(target=warm, name="yuki-now-warm", daemon=True).start()
        return self.include_now

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

    def prewarm_apps(self) -> int:
        """Ask the backend to read the Start menu list now (off the main thread). ``0`` without one."""
        prewarm = getattr(self.backend, "prewarm_apps", None)
        return int(prewarm()) if callable(prewarm) else 0

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
        """Start the backend's activity-facts warm-up (WinRT import) if it has one.

        Only the brain's own agents call this (their prewarm, :class:`yuki.agent.loop.Agent`), so it is also
        where their dispatcher turns the per-turn Now picture on (:meth:`enable_now`, from the configured
        ``Settings.now_in_situation``); the voice's hands and the read-only helpers never call it and keep it off.
        """
        if isinstance(self.backend, _LazyRealBackend) and not self.include_now:
            try:
                self.enable_now()
            except Exception:  # noqa: BLE001 - the brain works without it, as before
                pass
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
        moved: dict[str, Any] | None = None
        try:
            tool_input, moved = self._follow_vanished(tool_input)
        except ToolError as exc:
            return ToolOutcome(name=name, ok=False, summary=str(exc), content=[{"type": "text", "text": str(exc)}],
                               payload={"error": str(exc), "window_gone": True},
                               elapsed_ms=(time.perf_counter() - started) * 1000)
        refused = self._game_refusal(name, tool_input)
        if refused is not None:
            return ToolOutcome(name=name, ok=False, summary=refused, content=[{"type": "text", "text": refused}],
                               payload={"error": refused, "refused": "game", "details": {"refused": "game"}},
                               elapsed_ms=(time.perf_counter() - started) * 1000)
        try:
            outcome = handler(tool_input)
        except ToolError as exc:
            # small8: a ToolError is a refusal before anything was sent (a bad argument, a guard: "not clicked: nothing
            # you can click is at ..."), so the run's outcome can tell it from an action that ran and failed
            outcome = ToolOutcome(
                name=name,
                ok=False,
                summary=str(exc),
                content=[{"type": "text", "text": str(exc)}],
                payload={"error": str(exc), "refused": True},
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
        if moved is not None:
            note = moved["note"]
            outcome.summary = f"{note} {outcome.summary}".strip()
            outcome.content = [{"type": "text", "text": note}, *(outcome.content or [])]
            if isinstance(outcome.payload, dict):
                outcome.payload = {**outcome.payload, "successor": {k: v for k, v in moved.items() if k != "note"}}
        if not outcome.elapsed_ms:
            outcome.elapsed_ms = (time.perf_counter() - started) * 1000
        if outcome.ok and name in _MARKS_ACTED and self._read_facts:
            # the window it acted on may look different now: coordinates from its last read are stale
            hwnd = tool_input.get("expect_hwnd") or tool_input.get("hwnd")
            if hwnd is None and tool_input.get("x") is not None and tool_input.get("y") is not None:
                try:
                    hwnd = self._pointed_window(int(tool_input["x"]), int(tool_input["y"]))
                except (TypeError, ValueError):
                    hwnd = None
            if hwnd is None and tool_input.get("element") is not None:
                hwnd = self._latest_tree_hwnd
            try:
                self._mark_acted(int(hwnd) if hwnd is not None else None, name)
            except (TypeError, ValueError):
                pass
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

    # -- windows that vanished (revamp A8, 01 §8.1 G) ------------------------

    def _remember_windows(self, payload: Any) -> None:
        """Keep what each window was (process, title, place) from an overview, so a call on one that vanished can
        find the window that took its place."""
        if not isinstance(payload, dict):
            return
        now = time.time()
        for w in payload.get("windows") or []:
            if not isinstance(w, dict) or w.get("hwnd") is None:
                continue
            try:
                hwnd = int(w["hwnd"])
            except (TypeError, ValueError):
                continue
            self._windows_seen[hwnd] = {"process": str(w.get("process_name") or ""), "title": str(w.get("title") or ""),
                                        "rect": list(w.get("bounds") or []) or None, "at": now}
        if len(self._windows_seen) > 256:
            for old in sorted(self._windows_seen, key=lambda h: self._windows_seen[h]["at"])[:64]:
                self._windows_seen.pop(old, None)

    def _follow_vanished(self, tool_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """When the window a call names (``hwnd`` / ``expect_hwnd``) no longer exists - the app closed it or made it
        again (a reload, a restart: T3 Code's 132798 became 1312414 on 1 Oct and the request died) - the call goes to
        its successor: the window of the same process now most like it by title and place (:func:`find_successor`,
        measured), said in the result. With none, a plain error saying so. Unchanged when the backend cannot tell
        whether a window exists."""
        keys = [k for k in WINDOW_KEYS if isinstance(tool_input.get(k), int) and not isinstance(tool_input.get(k), bool)]
        if not keys:
            return tool_input, None
        try:   # a look-only wrapper (the helpers') refuses names it does not know: then there is no lookup
            exists = getattr(self.backend, "window_exists", None)
        except Exception:
            return tool_input, None
        if not callable(exists):
            return tool_input, None
        old = int(tool_input[keys[0]])
        try:
            if exists(old):
                return tool_input, None
        except Exception:
            return tool_input, None
        was = dict(self._windows_seen.get(old) or {})
        facts = self._read_facts.get(old) or {}
        was.setdefault("title", facts.get("title") or "")
        was.setdefault("rect", facts.get("rect"))
        if not was.get("process") and facts.get("process"):
            was["process"] = facts["process"]
        try:
            overview = _plain(self.backend.get_desktop_overview())
        except Exception:
            overview = {}
        self._remember_windows(overview)
        windows = [w for w in (overview or {}).get("windows") or [] if isinstance(w, dict)]
        found = find_successor(was, windows, gone=old)
        name = f"{was.get('process') or 'its app'} \"{(was.get('title') or '')[:80]}\"" if was.get("title") else \
            str(was.get("process") or "")
        if found is None:
            raise ToolError(
                f"Window {old}{(' (' + name + ')') if name else ''} no longer exists: it closed, or its app made it again "
                f"with a new id, and no window of {was.get('process') or 'that app'} took its place. look_at_desktop "
                "shows the windows there are now.")
        new = int(found["hwnd"])
        changed = {k: (new if k in keys and int(tool_input[k]) == old else tool_input[k]) for k in tool_input}
        if old in self._latest_trees:   # element ids read from the old window do not exist in the new one
            self._latest_trees.pop(old, None)
        note = (f"[Window {old} is gone (its app made it again); this ran on the window that took its place: "
                f"[{new}] \"{str(found.get('title') or '')[:80]}\" - the same app ({found.get('process_name')}), "
                f"title {found['title_match']:.0%} alike, {found['overlap']:.0%} the same place on screen. Element "
                f"ids from the old window do not apply to it.]")
        return changed, {"note": note, "from": old, "to": new, "title_match": found["title_match"],
                         "overlap": found["overlap"], "score": found["score"]}

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
        payload = _plain(overview)
        self._remember_windows(payload)
        if self.include_now:
            foreground = payload.get("foreground_hwnd") if isinstance(payload, dict) else None
            now_text, now_facts = self.now_text(foreground)
            if now_text:
                text = f"{text}\n\n{now_text}"
            if isinstance(payload, dict):
                payload = {**payload, "now": now_facts}
        return text, payload, (time.perf_counter() - started) * 1000

    # -- the live picture of the screen (yuki.memory.now) -------------------

    def _now_board(self, wait_s: float | None = None) -> dict[str, Any] | None:
        """The memory service's picture of the screen, or ``None`` (no service, or no answer in time)."""
        try:
            from yuki.memory.now import shared_client

            client = shared_client()
            return client.now(wait_s=self.now_wait_s if wait_s is None else wait_s) if client is not None else None
        except Exception:  # noqa: BLE001 - the brain goes on without it
            return None

    def now_text(self, foreground: Any = None) -> tuple[str, dict[str, Any]]:
        """The Now block for the per-turn situation, and content-free facts about it for the log.

        What the memory service read of every visible window (the one in front first), the popups that
        appeared lately, and a live one-line focus read - each bounded by its own wait
        (:attr:`now_wait_s`, :attr:`focus_wait_s`, ~50 ms each), left out when it does not answer in time.
        A read of the window in front that is older than a change the service saw is said to be so, and the
        service is asked to read it again (the next turn has it).
        """
        started = time.perf_counter()
        facts: dict[str, Any] = {}
        try:
            fg = int(foreground) if foreground else None
        except (TypeError, ValueError):
            fg = None
        # the focus read runs alongside the board's answer (each has its own bound)
        focus_box: dict[str, Any] = {}
        snapshot = getattr(self.backend, "focus_snapshot", None)
        focus_thread = None
        if callable(snapshot):
            import threading

            def read_focus() -> None:
                f0 = time.perf_counter()
                try:
                    focus_box["focus"] = snapshot(timeout_s=self.focus_wait_s)
                except Exception as exc:  # noqa: BLE001
                    focus_box["error"] = type(exc).__name__
                focus_box["ms"] = round((time.perf_counter() - f0) * 1000.0, 2)

            focus_thread = threading.Thread(target=read_focus, name="yuki-now-focus", daemon=True)
            focus_thread.start()
        board = self._now_board()
        #: The last picture this turn had: the agent anchors its memory lookup on the window in front from it.
        self.last_now_board = board
        facts["board_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        facts["board"] = board is not None
        focus_line = ""
        self.last_focus = None
        if focus_thread is not None:
            focus_thread.join(self.focus_wait_s + 0.02)
            focus = focus_box.get("focus")
            if getattr(focus, "ok", False) and not _is_own_window(getattr(focus, "window", 0)):
                # (focus in Yuki's own ask bar or chat says nothing about "this")
                focus_line = focus.line()
                self.last_focus = focus
            facts["focus_ok"] = bool(getattr(focus, "ok", False))
            facts["focus_ms"] = focus_box.get("ms")
            if focus_box.get("error"):
                facts["focus_error"] = focus_box["error"]
        if board is None:
            facts["ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            return (focus_line, facts)
        from yuki.memory.now import format_now, shared_client

        windows = board.get("windows") or []
        front = next((w for w in windows if w.get("hwnd") == (fg or board.get("foreground"))), None)
        if front is not None and (front.get("stale") or front.get("read_at") is None) and not front.get("blocked"):
            try:
                client = shared_client()
                facts["refresh_asked"] = bool(client and client.refresh(fg, wait_s=self.now_wait_s))
            except Exception:  # noqa: BLE001
                facts["refresh_asked"] = False
        text = format_now(board, foreground=fg, focus_line=focus_line)
        facts.update(windows=len(windows), popups=len(board.get("popups") or []), chars=len(text),
                     front_age_s=front.get("age_s") if front else None,
                     front_stale=bool(front.get("stale")) if front else None,
                     ms=round((time.perf_counter() - started) * 1000.0, 2))
        return text, facts

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
        within = tool_input.get("within")
        if within is not None and (isinstance(within, bool) or not isinstance(within, int)):
            raise ToolError(f"'within' must be an element id, got {within!r}")
        started = time.perf_counter()
        generation, at = self._generation(hwnd), time.monotonic()
        if within is not None:
            # report 58 review (F2): the part is walked from its own element, found again by its RuntimeId, so the
            # whole cap goes to it; a provider without RuntimeIds (or a backend without part reads) gets the whole
            # window with the larger cap, and only that part listed
            cap = max(self.max_tree_elements, WITHIN_TREE_ELEMENTS)
            rid = self._runtime_id_of(hwnd, within)
            tree = None
            if rid:
                try:
                    tree = self.backend.get_window_tree(hwnd, max_elements=cap, timeout_s=self.tree_timeout_s,
                                                        part=rid)
                except TypeError:
                    tree = None
            if tree is not None and _field(tree, "part_missing"):
                seen = self._models[int(hwnd)].seen.get(int(within)) if int(hwnd) in self._models else None
                what = (f' ({seen[0]} "{seen[1][:60]}")' if seen and seen[1] else f" ({seen[0]})" if seen else "")
                text = (f"element [{within}]{what} is not in window {hwnd} now: it went away, or it is out of view. "
                        f"Read the whole window")
                return ToolOutcome(name="look_at_window", ok=True, summary=text,
                                   content=[{"type": "text", "text": text}],
                                   payload={"status": "ok", "part_missing": True},
                                   elapsed_ms=(time.perf_counter() - started) * 1000)
            if tree is not None and _field(tree, "part"):
                # a part is no observation of the whole window: it is never the read an action is compared with
                return self._tree_outcome(hwnd, tree, started, within=within, part=True)
            tree = self.backend.get_window_tree(hwnd, max_elements=cap, timeout_s=self.tree_timeout_s)
        else:
            tree = self.backend.get_window_tree(hwnd, max_elements=self.max_tree_elements,
                                                timeout_s=self.tree_timeout_s)
        outcome = self._tree_outcome(hwnd, tree, started, within=within)
        self._note_observed(hwnd, generation, at, tree)
        return outcome

    def _runtime_id_of(self, hwnd: int, element_id: int) -> tuple[int, ...] | None:
        """The RuntimeId of element ``element_id`` of ``hwnd`` as the screen model holds it (``None``: unknown)."""
        model = self._models.get(int(hwnd))
        element = model.latest.get(int(element_id)) if model is not None else None
        raw = _field(element, "runtime_id") if element is not None else None
        try:
            return tuple(int(x) for x in raw) if raw else None
        except (TypeError, ValueError):
            return None

    # -- the screen model: events and observations (report 58) ---------------

    @property
    def use_events(self) -> bool:
        """Whether reads use the event hub (report 58 review, F8): as set, else whatever ``verify_outcomes`` is."""
        return self.verify_outcomes if self._use_events is None else self._use_events

    @use_events.setter
    def use_events(self, value: bool) -> None:
        self._use_events = bool(value)

    def _hub(self) -> Any:
        """The event hub (``None``: no events; reads stand in): the process's shared one on the real desktop."""
        if not self.use_events:
            return None
        if self.events is None and isinstance(self.backend, _LazyRealBackend):
            try:
                from yuki.perception.events import shared_hub

                self.events = shared_hub()
            except Exception:  # noqa: BLE001 - reads stand in
                self.use_events = False
                return None
        return self.events

    def _generation(self, hwnd: int) -> int | None:
        """Keep ``hwnd`` in play and return its event generation (``None`` while its events are not flowing)."""
        hub = self._hub()
        if hub is None:
            return None
        try:
            hub.watch(int(hwnd))
            return hub.generation(int(hwnd))
        except Exception:  # noqa: BLE001
            return None

    def _note_observed(self, hwnd: int, generation: int | None, at: float, tree: Any) -> None:
        if str(_field(tree, "status") or "ok") == "ok":
            self._observed[int(hwnd)] = (generation, at, bool(_field(tree, "opaque")))
            self._observed_trees[int(hwnd)] = tree
            if len(self._observed_trees) > 16:
                for old in sorted(self._observed_trees, key=lambda h: self._observed.get(h, (None, 0.0))[1])[:4]:
                    if old != int(hwnd):
                        self._observed_trees.pop(old, None)

    def _observe(self, hwnd: int, *, timeout_s: float = OUTCOME_READ_S) -> tuple[list[Any] | None, bool]:
        """A read of ``hwnd`` for the screen model only (not handed to the model as a read): ``(elements, partial)``,
        its ids carried, so the same id is the same element as in what the thinker saw; ``partial`` when the read
        stopped at its cap.  ``(None, False)`` when the window gave no usable read."""
        generation, at = self._generation(hwnd), time.monotonic()
        try:
            tree = self.backend.get_window_tree(int(hwnd), max_elements=self.max_tree_elements, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001 - gone or wedged
            return None, False
        if str(_field(tree, "status") or "ok") != "ok" or self._cut_short(tree):
            return None, False   # a read the clock cut short cannot say what is gone or what changed
        elements = list(_field(tree, "elements") or [])
        if not elements or not all(isinstance(_field(e, "id"), int) for e in elements):
            return None, False
        partial = bool(_field(tree, "truncated"))
        carried = self.window_model(int(hwnd)).carry(elements, self._reads, partial=partial)
        self._note_observed(hwnd, generation, at, tree)
        return carried, partial

    def _reusable(self, hwnd: int, generation: int | None) -> Any:
        """The latest observation of ``hwnd`` when it is still current: the window's events show nothing since it
        was taken, and it is at most :data:`REUSE_MAX_S` old (report 58 review, F4: an event a provider does not
        raise - content moved with no scroll, name or structure event - must not keep an old read current for
        long).  ``None`` otherwise."""
        seen = self._observed.get(int(hwnd))
        tree = self._observed_trees.get(int(hwnd))
        if generation is None or seen is None or tree is None or seen[0] != generation:
            return None
        if time.monotonic() - seen[1] > REUSE_MAX_S:
            return None
        return tree

    def _current(self, hwnd: int) -> tuple[list[Any] | None, bool, bool]:
        """The window as it is now, for the read before an action: ``(elements, fresh, partial)``. The model's
        latest observation while it is still current (:meth:`_reusable`); else a new read."""
        last = self._reusable(hwnd, self._generation(hwnd))
        if last is not None:
            # the same read again, its ids carried (an identity carry: the model already holds it)
            elements = list(_field(last, "elements") or [])
            if elements:
                partial = bool(_field(last, "truncated"))
                return self.window_model(int(hwnd)).carry(elements, self._reads, partial=partial), False, partial
        # Without events a fresh read is the only way to know the window as it is now, and comparing it with the
        # thinker's read is what shows the elements that change on their own (a clock, a progress bar).
        elements, partial = self._observe(hwnd)
        return elements, True, partial

    def _settled_after(self, hwnd: int, generation: int | None, *, first_s: float = OUTCOME_FIRST_EVENT_S) -> None:
        """Wait for the action's effect to show and the window to go quiet, on its events (bounded); without events
        return at once. ``first_s``: how long the first event is waited for."""
        hub = self._hub()
        if hub is None or generation is None:
            return
        try:
            hub.wait_change(int(hwnd), generation, first_s)
            hub.wait_quiet(int(hwnd), quiet_s=OUTCOME_QUIET_S, cap_s=OUTCOME_CAP_S)
        except Exception:  # noqa: BLE001
            return

    def _read_again_until(self, hwnd: int, after: list[Any] | None, after_partial: bool,
                          settled: Callable[[list[Any] | None], bool], deadline: float,
                          generation: int | None = None) -> tuple[list[Any] | None, bool, int]:
        """percFix: while the read after an action does not show what it expects of its target (``settled``), wait
        for the window's next event (never past ``deadline``) and read it again: the way a person looks once more
        when something moves, never a timed poll. Without events nothing is waited for. Returns the last read, its
        partial flag and how many extra reads were taken.

        ``generation``: the window's event generation taken just *before* ``after`` was read (review F3). An event
        that lands while a read is running is newer than that, so it is not waited for again: the window is read once
        more at once. Each re-read takes its generation before it reads, the same way."""
        hub = self._hub()
        extra = 0
        while not settled(after) and hub is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if generation is None:
                generation = self._generation(hwnd)
            if generation is None:
                break
            try:
                if not hub.wait_change(int(hwnd), generation, remaining):
                    break   # nothing more happened in the window: the read stands
                hub.wait_quiet(int(hwnd), quiet_s=OUTCOME_QUIET_S, cap_s=max(0.0, min(OUTCOME_CAP_S, deadline
                                                                                    - time.monotonic())))
            except Exception:  # noqa: BLE001
                break
            generation = self._generation(hwnd)
            newer, newer_partial = self._observe(hwnd)
            extra += 1
            if newer is None:
                break
            after, after_partial = newer, newer_partial
        return after, after_partial, extra

    def _hover_first(self, hwnd: int | None, x: int, y: int, guard: int | None) -> None:
        """percFix: before a mouse click at a raw point, bring the pointer there and let the window show what the
        pointer alone does (on its events, bounded by :data:`HOVER_SETTLE_S`), so the read before the click already
        has it. Nothing is clicked; a pointer already there costs nothing."""
        point_at = getattr(self.backend, "point_at", None)
        if not callable(point_at):
            return
        try:
            from yuki.perception.windows import cursor_position

            if tuple(cursor_position()) == (int(x), int(y)):
                return
        except Exception:  # noqa: BLE001 - not known: move it
            pass
        generation = self._generation(int(hwnd)) if hwnd else None
        busy = False
        if generation is not None:
            try:
                hub = self._hub()
                activity = hub.activity(int(hwnd)) if hub is not None else None
                last_at = float(getattr(activity, "last_at", 0.0) or 0.0)
                busy = bool(last_at) and hub.clock() - last_at < HOVER_BUSY_S
            except Exception:  # noqa: BLE001 - not known: treat as quiet
                busy = False
        try:
            self._before_input("click")
            moved = point_at(int(x), int(y), expect_hwnd=guard)
        except ToolError:
            raise
        except Exception:  # noqa: BLE001 - the click goes on without it
            return
        if not getattr(moved, "ok", isinstance(moved, dict) and moved.get("ok")):
            return
        hub = self._hub()
        if hub is None or generation is None:
            return
        try:
            if hub.wait_change(int(hwnd), generation, HOVER_FIRST_EVENT_S):
                hub.wait_quiet(int(hwnd), quiet_s=OUTCOME_QUIET_S, cap_s=HOVER_BUSY_CAP_S if busy else HOVER_SETTLE_S)
        except Exception:  # noqa: BLE001
            return

    def _bracketed(self, name: str, hwnd: int | None, act: Callable[[], ToolOutcome], *, target: int | None = None,
                   target_label: str = "",
                   before_hook: Callable[[list[Any] | None], ToolOutcome | None] | None = None,
                   after_hook: Callable[..., ToolOutcome] | None = None,
                   prepare: Callable[[], None] | None = None,
                   settled: Callable[[list[Any] | None], bool] | None = None,
                   point: tuple[int, int] | None = None, axes: str = "v") -> ToolOutcome:
        """Run ``act`` between two reads of ``hwnd`` and say what it changed there (report 58).

        The account (:mod:`yuki.perception.outcome`) goes into the result as "What changed: ..." and its verdict
        into ``details["verified"]`` together with the action's own facts (a new window, the foreground, the focus,
        the page): something changed is verified; nothing changed in a window whose content is exposed is not; a
        window that could not be read twice keeps the action's own verdict. A scroll the window's content shows
        moving is a scroll that worked, whatever a single point under it said.

        ``before_hook`` is given the read before the action (``None`` when there is none) and may return an outcome
        that stands instead of acting; ``after_hook`` is given the action's outcome, the read after it and the
        change between the two reads (``None`` when there is none) and returns the outcome to report (report 58
        review, F3: a toggle reads its own state from these same two reads, so its side effects are in the account
        too). percFix: ``prepare`` runs before the read before the action (a raw click's pointer move, so what the
        pointer alone does is not taken for the click's effect); ``settled`` says whether a read after the action
        shows what the action expects of its target (a toggle in the state asked for): until it does, the first
        event is waited for longer and the window is read again on each new event, up to
        :data:`OUTCOME_TARGET_WAIT_S` (an app that takes a second or more to show it is not "contradicted");
        ``settled(None)`` says whether the control itself already confirmed it (then no long wait). ``point``: for a
        scroll, the point it was made at (the content under it is what must have been replaced, review F1); ``axes`` its
        direction (``"v"``, ``"h"`` or ``"vh"``).
        """
        if not hwnd or not self.verify_outcomes:
            return self._hooked(act, before_hook, after_hook)
        if prepare is not None:
            prepare()
        prepare_started = time.monotonic()
        before: list[Any] | None = None
        fresh = before_partial = False
        generation: int | None = None
        reference: list[Any] | None = None
        busy_rids: set[tuple] = set()
        try:
            hwnd = int(hwnd)
            model = self.window_model(hwnd)
            seen = self._observed.get(hwnd)
            # the read the thinker acted from, when recent: what changed between it and the read before the action
            # was changing on its own
            reference = (list(model.latest.values()) if model.latest and seen is not None
                         and time.monotonic() - seen[1] < REFERENCE_FRESH_S else None)
            before, fresh, before_partial = self._current(hwnd)
            started_at = time.monotonic()
            generation = self._generation(hwnd)
            # elements already changing on their own before the action (a clock, a progress bar), from the events
            hub = self._hub()
            if hub is not None and generation is not None:
                busy_rids = set(hub.busy_since(hwnd, started_at - OUTCOME_CAP_S))
        except Exception:  # noqa: BLE001 - the action runs either way
            before = None
        if before is None:
            return self._hooked(act, before_hook, after_hook)
        if before_hook is not None:
            instead = before_hook(before)
            if instead is not None:
                return instead
        prepared_ms = round((time.monotonic() - prepare_started) * 1000.0, 1)
        outcome = act()
        acted_at = time.monotonic()
        if not outcome.ok and name != "scroll":
            return after_hook(outcome, None) if after_hook is not None else outcome
        after: list[Any] | None = None
        try:
            waited = time.monotonic()
            # the long first wait only while the control has not already said it reached the state asked for
            # (review F4: a toggle confirmed by its own pattern does not wait 3 s for a window event)
            self._settled_after(hwnd, generation,
                                first_s=(OUTCOME_TARGET_WAIT_S if settled is not None and not settled(None)
                                         else OUTCOME_FIRST_EVENT_S))
            settle_ms = round((time.monotonic() - waited) * 1000.0, 1)
            read_started = time.monotonic()
            read_generation = self._generation(hwnd)
            after, after_partial = self._observe(hwnd)
            rereads = 0
            if settled is not None and after is not None:
                after, after_partial, rereads = self._read_again_until(hwnd, after, after_partial, settled,
                                                                       acted_at + OUTCOME_TARGET_WAIT_S,
                                                                       read_generation)
            # content-free facts for the log: what the bracket cost and whether events spared the read before
            diag = {"before_read": "fresh" if fresh else "current by events", "events": generation is not None,
                    "before_ms": prepared_ms, "settle_ms": settle_ms,
                    "after_read_ms": round((time.monotonic() - read_started) * 1000.0, 1)}
            if rereads:
                diag["rereads"] = rereads
            if before_partial or after_partial:
                diag["partial"] = True
            if after is None:
                if after_hook is not None:
                    outcome = after_hook(outcome, None)
                return self._with_change(outcome, None, [], None, "the window could not be read after it", diag)
            from yuki.perception.model import fact
            from yuki.perception.outcome import describe, diff, verdict

            busy = {fact(e, "id") for e in before if tuple(fact(e, "runtime_id") or ()) in busy_rids}
            change = diff(before, after, target=target, reference=reference if fresh else None, busy=busy,
                          before_partial=before_partial, after_partial=after_partial, scroll=name == "scroll",
                          point=point if name == "scroll" else None, axes=axes)
            if after_hook is not None:
                outcome = after_hook(outcome, after, change)
            opaque = bool((self._observed.get(hwnd) or (None, 0.0, False))[2])
            verified, why = verdict(change, opaque=opaque)
            lines = describe(change, target_label=target_label)
            return self._with_change(outcome, verified, lines, change, why, diag)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - the action's own report stands
            if isinstance(outcome.payload, dict):
                outcome.payload = {**outcome.payload, "screen_change_error": f"{type(exc).__name__}: {exc}"}
            return outcome

    @staticmethod
    def _hooked(act: Callable[[], ToolOutcome], before_hook: Callable[[list[Any] | None], ToolOutcome | None] | None,
                after_hook: Callable[[ToolOutcome, list[Any] | None], ToolOutcome] | None) -> ToolOutcome:
        """``act`` with its hooks and no reads of the bracket's own (outcome checks off, or no read before)."""
        if before_hook is not None:
            instead = before_hook(None)
            if instead is not None:
                return instead
        outcome = act()
        return after_hook(outcome, None) if after_hook is not None else outcome

    def _with_change(self, outcome: ToolOutcome, verified: bool | None, lines: list[str], change: Any,
                     why: str, diag: dict[str, Any] | None = None) -> ToolOutcome:
        """The action's result with what changed on screen added (text, details, verdict)."""
        outer = outcome.payload if isinstance(outcome.payload, dict) else {}
        # an element-targeted type_text nests the typing's own report (action_verdict reads it there)
        nested = outcome.name == "type_text" and isinstance(outer.get("type_text"), dict)
        payload = outer["type_text"] if nested else outer
        details = dict(payload.get("details") or {}) if isinstance(payload.get("details"), dict) else {}
        effects = [details.get(k) for k in ("foreground_changed", "focus_changed", "foreground_title_changed",
                                            "page_changed", "window_state_changed")]
        own = details.get("verified")
        if details.get("toggle_to"):
            # a toggle's own read-back of its state is the verdict (did it reach the state asked for; percFix: None
            # when it still reads the old state but the window did change); what else changed is the account
            final: bool | None = own if isinstance(own, bool) else None
        elif details.get("readback") == "missing" or details.get("enter_withheld"):
            final = own if isinstance(own, bool) else None   # the text did not arrive: no send to see
            if details.get("readback") == "missing":
                final = False
        elif verified is True or own is True or any(e is True for e in effects) or details.get("new_windows"):
            final = True
        elif verified is False:
            final = False
        else:
            final = own if isinstance(own, bool) else None
        if verified is False and final is True:
            # the effect is outside the window read (a new window, another window in front, the focus): say so,
            # never "nothing changed"
            lines = ["nothing changed inside this window itself; what changed is in the facts above (the window in "
                     "front, the focus, a new window)"]
        undecided = on_nothing = False
        if details.get("point_on_nothing") and final is True and not (any(e is True for e in effects)
                                                                     or details.get("new_windows")):
            # percFix: a raw click on a point with no control under it (in the read just before it, the pointer
            # already there) is never verified by what changed in the window: live 5 Oct a click on an empty margin
            # was "verified" by things that change on their own. Unverified, with the account below
            final, verified, undecided, on_nothing = None, None, True, True
            why = ("nothing you can click was at that point, so what changed in the window may be its own movement, "
                   "not the click")
        if details.get("toggle_unconfirmed") and final is None:
            # percFix: the toggle still reads its old state but the window changed: neither verified nor contradicted
            undecided = True
            why = "the toggle still reads its old state, though the window changed"
        record = {"verified": verified, "why": why, "lines": lines[:8], **(diag or {})}
        if undecided:
            record["undecided"] = True
        if change is not None:
            record["counts"] = dict(change.counts)
        details["screen_change"] = record
        if final is not None:
            details["verified"] = final
        else:
            details.pop("verified", None)
        if outcome.name == "scroll" and change is not None and getattr(change, "content_moved", change.moved):
            # percFix: a scroll is judged by the window's content (its scroll position, items moving together, or
            # the items in view replaced as it scrolled), never by one point under it
            details["verified"] = True
            details.pop("unverifiable", None)
            if not outcome.ok:
                outcome.ok = True
                said = "the window's content did move"
                outcome.summary = f"{_without_point_verdict(outcome.summary)}; {said}"
                if outcome.content and outcome.content[0].get("type") == "text":
                    outcome.content[0]["text"] = f"{_without_point_verdict(outcome.content[0]['text'])}; {said}"
        if nested:
            outcome.payload = {**outer, "type_text": {**payload, "details": details}}
        else:
            outcome.payload = {**payload, "details": details}
        if lines:
            # the account goes to the model with the result; the one-line summary (run trace, cards) stays as it was,
            # so what was on screen is not copied into practice memory's traces
            said = "What changed: " + "; ".join(lines[:OUTCOME_LINES]) + "."
            if on_nothing:
                said += f" Not taken as the click's effect: {why}."
            if outcome.content and outcome.content[0].get("type") == "text":
                outcome.content[0]["text"] = f"{outcome.content[0]['text']}\n{said}"
            else:
                outcome.content = [{"type": "text", "text": said}, *(outcome.content or [])]
        return outcome

    def window_model(self, hwnd: int) -> Any:
        """The screen model of ``hwnd`` (created on first use): ids carried across reads, what a gone id was."""
        from yuki.perception.model import WindowModel

        model = self._models.get(int(hwnd))
        if model is None:
            model = self._models[int(hwnd)] = WindowModel(int(hwnd))
            if len(self._models) > 64:
                for old in sorted(self._models, key=lambda h: self._models[h].read)[:16]:
                    if old != int(hwnd):
                        self._models.pop(old, None)
        return model

    def _carry_tree(self, hwnd: int, tree: Any, *, partial: bool = False) -> Any:
        """``tree`` with its ids carried from the window's earlier reads (report 58: every read, explicit or not);
        ``partial``: the read covers only part of the window whatever its own flags say (a part read)."""
        if str(_field(tree, "status") or "ok") != "ok":
            return tree   # a busy or empty read says nothing about which elements are gone
        elements = list(_field(tree, "elements") or [])
        if not elements or not all(isinstance(_field(e, "id"), int) for e in elements):
            return tree
        model = self.window_model(hwnd)
        carried = _tree_with(tree, model.carry(elements, self._reads,
                                               partial=partial or bool(_field(tree, "truncated"))))
        left_out = _field(tree, "left_out")
        if left_out:
            # the read's notes of what it left out name their parent by the read's own ids: carry them too
            moved = [{**entry, "parent": model.mapping.get(int(entry.get("parent", -1)), -1)
                      if int(entry.get("parent", -1)) >= 0 else -1} for entry in left_out if isinstance(entry, dict)]
            if isinstance(carried, dict):
                carried["left_out"] = moved
            else:
                carried.left_out = moved
        return carried

    def _format_tree(self, tree: Any, within: int | None = None) -> str:
        """The backend's own text for a tree; ``within`` lists one element's part in full (a backend whose formatter
        takes no ``within``, a stub's, gives the whole tree)."""
        if within is None:
            return self.backend.format_window_tree(tree)
        try:
            return self.backend.format_window_tree(tree, within=within)
        except TypeError:
            return self.backend.format_window_tree(tree)

    def _tree_outcome(self, hwnd: int, tree: Any, started: float, *, carried: bool = False,
                      within: int | None = None, part: bool = False) -> ToolOutcome:
        """Format one tree read exactly as ``look_at_window`` returns it.

        The text is the backend's own :func:`format_window_tree` behind a
        one-line header, so whatever that formatter reports (page title and
        address, selected tab, status and notes) reaches the model unchanged,
        for explicit looks and the automatic view after actions alike.

        Every read's ids are carried from the window's earlier reads (report 58), so an id from any earlier read
        still means the same element, or is reported gone; ``carried`` is kept for callers that pass it.

        ``part``: the read holds one element's subtree only (``within``, report 58 review F2): the rest of the
        window keeps its ids and its latest facts, so nothing outside the part is taken for gone.
        """
        self._reads += 1
        previous = self._latest_trees.get(int(hwnd)) if part else None
        tree = self._carry_tree(int(hwnd), tree, partial=part)
        #: The latest read handed to the model, as the backend's own tree object with its ids carried.
        self.last_tree = tree
        text = self._format_tree(tree, within)
        payload = _plain(tree)
        elements = payload.get("elements") or [] if isinstance(payload, dict) else []
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        status = str(payload.get("status") or "ok") if isinstance(payload, dict) else "ok"
        note = str(payload.get("note") or "") if isinstance(payload, dict) else ""
        self._latest_trees[int(hwnd)] = (
            self._reads,
            {
                **(previous[1] if previous is not None else {}),
                **{
                    int(element["id"]): element
                    for element in elements
                    if isinstance(element, dict) and isinstance(element.get("id"), int)
                },
            },
        )
        self._latest_tree_hwnd = int(hwnd)
        model = self.window_model(int(hwnd))
        if model.read == self._reads:
            self._id_lineage[int(hwnd)] = set(model.lineage)
            self._id_ceiling[int(hwnd)] = model.ceiling
        else:   # a busy or empty read: nothing to carry, the earlier ids keep their meaning
            self._id_lineage[int(hwnd)] = self._id_lineage.get(int(hwnd), set()) | {self._reads}
        opaque = bool(payload.get("opaque")) if isinstance(payload, dict) else False
        self._note_read(int(hwnd), payload if isinstance(payload, dict) else {}, opaque)
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
        if opaque:
            known = self._opaque_window_facts(int(hwnd), payload if isinstance(payload, dict) else {})
            if known:
                text = f"{text}\n{known}"
                payload = {**payload, "app_data": True} if isinstance(payload, dict) else payload
        return ToolOutcome(
            name="look_at_window",
            ok=True,
            summary=summary,
            content=[{"type": "text", "text": f"{header}\n{text}"}],
            payload=payload,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _note_read(self, hwnd: int, payload: dict[str, Any], opaque: bool) -> None:
        """What a raw x,y click on ``hwnd`` is checked against: this read's number and the window as it was."""
        rect = _window_rect_now(hwnd)
        self._read_facts[int(hwnd)] = {
            "read": self._reads, "at": time.time(), "title": str(payload.get("title") or ""),
            "rect": list(rect) if rect else None, "opaque": opaque, "acted": None,
            "process": str(payload.get("process_name") or ""),
        }
        if len(self._read_facts) > 64:
            oldest = sorted(self._read_facts, key=lambda h: self._read_facts[h]["at"])[:16]
            for old in oldest:
                self._read_facts.pop(old, None)

    def _opaque_window_facts(self, hwnd: int, payload: dict[str, Any]) -> str:
        """For a window UI Automation cannot see into: what the app's own data says about it (Warp's tabs,
        from the memory service's picture, else read here read-only), as lines after its tree."""
        from yuki.memory.now import _app_data_line, app_data_for

        data = None
        board = self._now_board()
        if board is not None:
            entry = next((w for w in board.get("windows") or [] if int(w.get("hwnd") or 0) == hwnd), None)
            data = (entry or {}).get("app_data")
        if data is None:
            try:
                from yuki.memory.privacy import PrivacyConfig
                from yuki.perception.visibility import frame_bounds

                rect = frame_bounds(hwnd)
                if rect is not None:
                    data = app_data_for(str(payload.get("process_name") or ""), rect, str(payload.get("title") or ""),
                                        privacy=PrivacyConfig())
            except Exception:  # noqa: BLE001 - the tree is still the answer
                data = None
        line = _app_data_line(data)
        if not line:
            return ""
        return (f"What this window holds, {line}. To switch to one of these, use the app's own keyboard "
                f"shortcuts (its tab-switching keys) and read the window title after each step until it names "
                f"the one wanted; if two fit about equally, ask which, naming them.")

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
        changes: bool = False,
        near: int | None = None,
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

        With ``changes`` (the view after actions, v3.15) and an earlier read of
        the window given to the model, the new read keeps that read's ids for
        the same elements (:func:`carry_ids`) and the text lists only what
        changed (new, gone, or shown differently), with the elements they sit in
        and ``near`` (the element acted on) and its parts, when that fits in
        :data:`CHANGES_VIEW_CHARS` and at most :data:`CHANGES_VIEW_SHARE` of the
        window changed; otherwise the whole tree, as before. The whole tree is
        always there through ``look_at_window``.

        Args:
            hwnd: Window to read.
            settle: ``False`` takes one read and returns it -- for when the
                action already reported the window's content ready.
            quiet_s: How long the signature must hold still.
            cap_s: Bound on the whole view.
            changes: List only what changed since the previous read when it fits.
            near: The id (in the previous read) of the element the action was aimed at.

        Returns:
            A :class:`WindowView` holding the newest good read.

        Raises:
            Whatever the first read raises (``ValueError`` for a handle that is
            no longer a window); a later read that fails just ends the wait.
        """
        started = time.perf_counter()
        deadline = started + max(cap_s, 0.0)
        generation, observed_at = self._generation(hwnd), time.monotonic()
        reused = self._still_current(hwnd, generation)
        tree = reused if reused is not None else self.backend.get_window_tree(
            hwnd, max_elements=self.max_tree_elements, timeout_s=max(cap_s, 0.1)
        )
        reads = 0 if reused is not None else 1
        signature = _tree_signature(tree)
        changed_at = time.perf_counter()
        settled = False
        hub = self._hub()
        if reused is not None:
            # report 58: the read taken after the last action (it already waited for the window to go quiet) is
            # still current by the window's events: nothing is read again
            settled = True
            reason = "the read after the last action, still current by the window's events"
            observed_at = self._observed[int(hwnd)][1]
        elif not settle:
            reason = "one read: the action had already reported the content ready"
        elif signature[0] != "ok":
            reason = f"one read: the window's status is {signature[0]}"
        elif hub is not None and generation is not None and hub.generation(int(hwnd)) is not None:
            # report 58: the window's events say whether it is still changing; nothing is re-read while it is not
            quiet, waited = hub.wait_quiet(int(hwnd), quiet_s=quiet_s,
                                           cap_s=max(deadline - time.perf_counter(), 0.0))
            if hub.generation(int(hwnd)) == generation:
                settled = True
                reason = f"no change in the window's events for {waited * 1000:.0f} ms after the read"
            else:
                newer_generation, newer_at = hub.generation(int(hwnd)), time.monotonic()
                try:
                    newer = self.backend.get_window_tree(
                        hwnd, max_elements=self.max_tree_elements,
                        timeout_s=max(deadline - time.perf_counter(), _REREAD_MIN_S))
                except Exception as exc:  # gone or wedged: keep the read there is
                    newer = None
                    reason = f"re-reading stopped: {type(exc).__name__}: {exc}"
                if newer is not None and _tree_signature(newer)[0] == "ok" and not (
                        self._cut_short(newer) and not self._cut_short(tree)):
                    tree, generation, observed_at = newer, newer_generation, newer_at
                    signature = _tree_signature(tree)
                    reads += 1
                settled = quiet
                if newer is not None:
                    reason = (f"read again once its events went quiet ({waited * 1000:.0f} ms)" if quiet else
                              f"still changing when the {cap_s:g} s limit was reached; read as it was then")
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
                        # report 58: never a read so short it is cut off (3 Oct: 0.3 s, 189 elements kept)
                        timeout_s=max(remaining, _REREAD_MIN_S),
                    )
                except Exception as exc:  # gone or wedged: keep the last good read
                    reason = f"re-reading stopped: {type(exc).__name__}: {exc}"
                    break
                reads += 1
                newer_signature = _tree_signature(newer)
                if newer_signature[0] != "ok":
                    reason = f"re-reading stopped: the window turned {newer_signature[0]}"
                    break
                if self._cut_short(newer) and not self._cut_short(tree):
                    # a re-read the clock cut short never replaces a whole read
                    reason = "re-reading stopped: a re-read was cut short by the time limit; the whole read is kept"
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
        previous = self._latest_trees.get(int(hwnd)) if changes else None
        changes_text = None
        outcome = self._tree_outcome(hwnd, tree, started)
        self._note_observed(hwnd, generation, observed_at, tree)
        if previous is not None and signature[0] == "ok":
            carried = self.last_tree   # the same read with its ids carried
            renumbered = list(_field(carried, "elements") or [])
            try:
                changes_text = self._changes_text(int(hwnd), previous, carried, renumbered, near)
            except Exception:  # noqa: BLE001 - the whole tree then, as before
                changes_text = None
        full = str(outcome.content[0]["text"]) if outcome.content else ""
        return WindowView(
            hwnd=hwnd,
            text=changes_text if changes_text is not None else full,
            summary=outcome.summary,
            payload=outcome.payload if isinstance(outcome.payload, dict) else {},
            reads=reads,
            settled=settled,
            reason=reason,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            changes=changes_text is not None,
            full_chars=len(full),
        )

    def _still_current(self, hwnd: int, generation: int | None) -> Any:
        """The tree of the latest observation of ``hwnd`` (the read after an action) while it is still current
        (:meth:`_reusable`), else ``None``."""
        return self._reusable(hwnd, generation)

    def _cut_short(self, tree: Any) -> bool:
        """Whether a read was stopped by the clock.  The reader says why it stopped (report 58 review, F6: a page's
        share of the cap also stops a read below the cap, and that is no clock); a tree that does not say (a stub,
        an older reader) is judged as before: truncated with fewer elements than the cap."""
        if not _field(tree, "truncated"):
            return False
        stopped = _field(tree, "stopped_by")
        if isinstance(stopped, str) and stopped:
            return stopped == "clock"
        return len(_field(tree, "elements") or []) < self.max_tree_elements

    def _changes_text(self, hwnd: int, previous: tuple[int, dict[int, Any]], tree: Any, elements: list[Any],
                      near: int | None) -> str | None:
        """The list of what changed in ``tree`` (ids carried) since ``previous``, or None when the whole is due."""
        before_read, before = previous
        by_id = {_field(e, "id"): e for e in elements}
        changed = [i for i, e in by_id.items() if i not in before or _shown_as(before[i]) != _shown_as(e)]
        gone = [before[i] for i in before if i not in by_id]
        if len(changed) > CHANGES_VIEW_SHARE * max(1, len(elements)):
            return None
        keep = set(changed)
        keep |= {i for i, e in by_id.items() if _field(e, "is_focused")}
        if near is not None and near in by_id:
            keep.add(near)
            keep |= {i for i, e in by_id.items() if _field(e, "parent", -1) == near}
        for i in list(keep):
            parent = _field(by_id[i], "parent", -1)
            hops = 0
            while isinstance(parent, int) and parent >= 0 and parent in by_id and hops < 64:
                keep.add(parent)
                parent = _field(by_id[parent], "parent", -1)
                hops += 1
        subset = [e for e in elements if _field(e, "id") in keep]
        body = self.backend.format_window_tree(_tree_with(tree, subset)) if subset else ""
        body = body.split("\n", 1)[1] if "\n" in body else ""
        read = self._latest_trees[hwnd][0]
        named = [f'{_field(g, "role")} "{str(_field(g, "name"))[:40]}"' for g in gone if _field(g, "name")]
        gone_text = (f", {len(gone)} gone" + (": " + ", ".join(named[:_CHANGES_GONE_NAMED]) if named else "")
                     + (" ..." if len(named) > _CHANGES_GONE_NAMED else "")) if gone else ""
        header = (
            f"window {hwnd} \"{_field(tree, 'title', '')}\" ({_field(tree, 'process_name', '')}): "
            f"{len(elements)} elements, read #{read}. Only what changed since read #{before_read} is listed "
            f"({len(changed)} new or changed{gone_text}), with the elements they sit in"
            + (" and the element acted on" if near is not None and near in by_id else "")
            + f"; every other element is as it was and keeps its id from read #{before_read}. "
            f"look_at_window gives the whole window."
        )
        text = header + ("\n" + body if body else "\n(nothing on it changed)")
        return text if len(text) <= CHANGES_VIEW_CHARS else None

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
        # appOpen: a game still starting through its launcher is waited on longer when asked (bounded)
        wait_s = max(0, min(LAUNCH_WAIT_MAX_S, self._opt_int(tool_input, "wait_s", 0)))
        # Only pass args when there are some, so a backend written before the
        # parameter existed keeps working for plain launches.
        kwargs: dict[str, Any] = {"timeout_s": float(wait_s)} if wait_s else {}
        result = (
            self.backend.launch_app(query, args=args, **kwargs)
            if args
            else self.backend.launch_app(query, **kwargs)
        )
        return self._from_action("launch_app", result)

    def _do_focus_window(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "focus_window", self.backend.focus_window(self._need_int(tool_input, "hwnd"))
        )

    def element_info(self, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        """The role and name of the element an input names (``element``, from the latest read of its window, as
        :meth:`_element_target` resolves it), or ``None``: what the run trace keeps as the target instead of an id or a
        point (practice memory, Phase C). Never raises."""
        try:
            element_id = tool_input.get("element")
            if element_id is None or isinstance(element_id, bool):
                return None
            hwnd = self._opt_hwnd(tool_input)
            if hwnd is None:
                hwnd = self._latest_tree_hwnd
            if hwnd is None or hwnd not in self._latest_trees:
                return None
            element = self._latest_trees[hwnd][1].get(int(element_id))
            if not element:
                return None
            return {"role": str(element.get("role") or ""), "name": str(element.get("name") or "")[:80]}
        except Exception:  # noqa: BLE001 - a trace detail, never an error
            return None

    def pointed_element(self, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        """The role and name of the element under a click's raw ``x``, ``y`` in the latest read of its window (the
        smallest interactive element whose bounds hold the point, else the smallest element), or ``None``: so a click
        at a Send button's coordinates is gated like a click on its id (review 34 H2). Bounds from the read only (no
        desktop call beyond which window is at the point); never raises."""
        try:
            if tool_input.get("element") is not None or tool_input.get("x") is None or tool_input.get("y") is None:
                return None
            x, y = int(tool_input["x"]), int(tool_input["y"])
            hwnd = self._opt_hwnd(tool_input) or self._pointed_window(x, y) or self._latest_tree_hwnd
            if hwnd is None or int(hwnd) not in self._latest_trees:
                return None
            best: tuple[bool, int, dict[str, Any]] | None = None
            for element in self._latest_trees[int(hwnd)][1].values():
                b = element.get("bounds") if isinstance(element, dict) else None
                if not b or len(b) != 4 or not (int(b[0]) <= x < int(b[2]) and int(b[1]) <= y < int(b[3])):
                    continue
                rank = (not element.get("is_interactive"), (int(b[2]) - int(b[0])) * (int(b[3]) - int(b[1])), element)
                if best is None or rank[:2] < best[:2]:
                    best = rank
            if best is None:
                return None
            element = best[2]
            return {"role": str(element.get("role") or ""), "name": str(element.get("name") or "")[:80]}
        except Exception:  # noqa: BLE001 - a gate detail, never an error
            return None

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
            if asked != read and not (asked in self._id_lineage.get(hwnd, ()) and element_id in elements):
                raise ToolError(
                    f"element ids from read #{asked} are out of date: the latest read of "
                    f"window {hwnd} is #{read}; nothing was sent. Use an id from read "
                    f"#{read}, or look at the window again"
                )
        element = self._current_element(hwnd, element_id, elements)
        if element is None:
            gone = self._models[hwnd].gone(element_id) if hwnd in self._models else None
            if gone is not None:
                role, name, last = gone
                what = f'{role} "{name[:60]}"' if name else role
                raise ToolError(
                    f"element [{element_id}] ({what}, seen in read #{last}) is not in window {hwnd} now: it went away "
                    f"or the app hid it; nothing was sent. Read the window again (a control hidden until the "
                    f"pointer comes near it shows after a hover)"
                )
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

    def _current_element(self, hwnd: int, element_id: int, shown: dict[int, Any]) -> dict[str, Any] | None:
        """The element ``element_id`` of ``hwnd`` as the window is now (report 58): the screen model's latest
        observation when one was taken after the read the thinker saw (the same id is the same element, at its
        place now), else that read's. ``None`` when the window no longer has it."""
        model = self._models.get(int(hwnd))
        if model is not None and model.latest:
            current = model.latest.get(int(element_id))
            if current is not None:
                return current if isinstance(current, dict) else _plain(current)
            if int(element_id) in model.seen:
                return None   # the window had it once and its latest observation does not: gone
        return shown.get(int(element_id))

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

    # -- quieter hands (revamp D, S7) ----------------------------------------

    def _read_element(self, tool_input: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """``(hwnd, element)`` of the read element an input names (after :meth:`_element_target` validated it)."""
        try:
            element_id = tool_input.get("element")
            if element_id is None or isinstance(element_id, bool):
                return None
            hwnd = self._opt_hwnd(tool_input)
            hwnd = self._latest_tree_hwnd if hwnd is None else hwnd
            if hwnd is None or hwnd not in self._latest_trees:
                return None
            element = self._current_element(int(hwnd), int(element_id), self._latest_trees[hwnd][1])
            return (int(hwnd), element) if isinstance(element, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _pattern_first(self, name: str, tool_input: dict[str, Any], verb: str, **kwargs: Any) -> ToolOutcome | None:
        """The action through the element's UI Automation pattern (no mouse, no keys, no focus taken), or ``None`` when
        the backend has no such hand, the element offers no pattern for it or it could not be found again."""
        act = getattr(self.backend, "element_action", None)
        found = self._read_element(tool_input)
        if not callable(act) or found is None:
            return None
        hwnd, element = found
        try:
            result = act(hwnd, element, verb, **kwargs)
        except Exception:  # noqa: BLE001 - the mouse or keys then
            return None
        if result is None:
            return None
        outcome = self._from_action(name, result)
        if isinstance(outcome.payload, dict):
            outcome.payload = {**outcome.payload, "via": "uia_pattern"}
        if outcome.ok:
            self._mark_acted(hwnd, name)
        return outcome

    #: Keys that act system-wide and type nothing (the press tool's media keys, a fixed key vocabulary): they need no
    #: wait for the user to stop typing.
    SYSTEM_KEYS: frozenset[str] = frozenset({"playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown",
                                             "volumemute"})

    def _before_input(self, name: str) -> None:
        """Wait while the user is typing or using the mouse before global input (:attr:`input_gate`); one line goes
        to :attr:`notices`. Raises :class:`ToolError` (nothing sent) when they keep going past the gate's limit.

        A page scrolling on by itself (V7) stops first: Yuki now acts on the desktop, and a page moving under the next
        click or read would make its positions stale (review F6)."""
        try:
            from yuki.actions.scrolling import stop_continuous

            stop_continuous("acting", wait_s=0.0)
        except Exception:  # noqa: BLE001 - the action goes on either way (the input lease keeps them apart)
            pass
        gate = self.input_gate
        if gate is None:
            return
        notices = self.notices
        result = gate.wait(notify=(notices.put if notices is not None else None), cancelled=self.cancel_check)
        if not result.get("ok", True):
            raise ToolError(f"Not sent: {result.get('why') or 'the user was using the keyboard or mouse'}; nothing "
                            f"was typed or clicked. Try again in a moment, or tell them you are waiting.")

    def _do_click(self, tool_input: dict[str, Any]) -> ToolOutcome:
        button = tool_input.get("button") or "left"
        if button not in {"left", "right", "middle"}:
            raise ToolError(f"button must be left, right or middle, got {button!r}")
        x, y, guard, label = self._point(tool_input)
        target = None
        if not label and self.guard_raw_clicks:
            target = self._check_raw_point(x, y, tool_input)
            self._check_point_on_control(x, y, target)
        found = self._read_element(tool_input) if label else None
        if found is not None and found[1].get("hidden") and "range" in (found[1].get("patterns") or ()):
            # report 58: a slider the app hides behind its own drawing has no point of its own to click
            raise ToolError(f"not clicked: {label} is a slider the app keeps out of sight; set it with set_slider "
                            f"on this id (it reads back the new value)")
        toggle = self._toggle_state(tool_input) if label else None
        if toggle is not None:
            # N4: a toggle is read live first and clicked only toward the wanted state, then read back
            return self._click_toggle(tool_input, toggle, button=button, guard=guard, label=label, x=x, y=y)
        window = guard or target or (found[0] if found is not None else None) or self._pointed_window(x, y)
        element_id = tool_input.get("element") if label else None

        def act() -> ToolOutcome:
            if label and button == "left" and self._opt_int(tool_input, "clicks", 1) == 1:
                via = self._pattern_first("click", tool_input, "click")
                if via is not None:
                    return self._labelled(via, label)
            self._before_input("click")
            aim_x, aim_y = self._aim(window, element_id, x, y)
            outcome = self._labelled(
                self._from_action(
                    "click",
                    self.backend.click(
                        aim_x,
                        aim_y,
                        button=button,
                        clicks=self._opt_int(tool_input, "clicks", 1),
                        expect_hwnd=guard,
                    ),
                ),
                label,
            )
            if outcome.ok:
                self._mark_acted(guard or target or self._pointed_window(x, y), "click")
            return outcome

        # percFix: a click at a raw point goes by the mouse; the pointer is brought there first and the window let
        # settle, so the read before the click already shows what the pointer alone did
        prepare = (lambda: self._hover_first(window, x, y, guard)) if not label else None
        point: dict[str, bool] = {}

        def before(read: list[Any] | None) -> None:
            # percFix: what is under a raw point in the read just before the click (the pointer already there)
            if read is not None:
                point["on_control"] = _on_a_control(read, x, y)
            return None

        def after(outcome: ToolOutcome, read: list[Any] | None, change: Any = None) -> ToolOutcome:
            if (point.get("on_control") is False and isinstance(outcome.payload, dict)
                    and not _surface_opened_near(change, x, y)):
                details = dict(outcome.payload.get("details") or {})
                details["point_on_nothing"] = True
                outcome.payload = {**outcome.payload, "details": details}
            return outcome

        return self._bracketed("click", window, act, target=element_id if isinstance(element_id, int) else None,
                               target_label=self._short_label(found[1]) if found is not None else "",
                               prepare=prepare, before_hook=before if not label else None,
                               after_hook=after if not label else None)

    def _aim(self, hwnd: int | None, element_id: Any, x: int, y: int) -> tuple[int, int]:
        """Where a click on element ``element_id`` lands now (report 58 review, F4): its centre in the screen
        model's latest read of ``hwnd`` - the outcome bracket's read just before the action, when it took one - so a
        page that scrolled since the thinker's read is not clicked at the old place; ``(x, y)`` otherwise.

        Raises:
            ToolError: the latest read shows the element gone (nothing is sent).
        """
        if not hwnd or not isinstance(element_id, int) or isinstance(element_id, bool):
            return x, y
        model = self._models.get(int(hwnd))
        if model is None or not model.latest:
            return x, y
        current = self._current_element(int(hwnd), element_id, {})
        if current is None:
            if element_id in model.seen:
                raise ToolError(f"not clicked: element [{element_id}] is not in window {hwnd} now (a read just before "
                                f"the click no longer has it); read the window again")
            return x, y
        centre = current.get("center") or ()
        if current.get("hidden"):
            # percFix: a control the app keeps out of sight stands at the box of what hides it (a switch's label). When
            # that is a container covering much of the window (the whole bar it sits in is hidden too, as in a
            # full-size view), the mouse would land on whatever is drawn there: nothing is sent
            box = current.get("bounds") or ()
            areas = [_box_area(_field(e, "bounds")) for e in model.latest.values()]
            if len(box) == 4 and _box_area(box) > _container_share() * max(areas or [1]):
                raise ToolError(f"not clicked: element [{element_id}] is kept out of sight by the app and has no place "
                                f"of its own on screen to click. Hover where it appears, read the window again, then "
                                f"click it from that read")
            return x, y
        if len(centre) < 2:
            return x, y
        return int(centre[0]), int(centre[1])

    def _check_point_on_control(self, x: int, y: int, target: int | None) -> None:
        """N4 (report 47): a click at raw x,y must land on a control of the latest read of the window there. Live 3 Oct
        the thinker clicked the coordinates of a player bar that a full-screen view had hidden: nothing of the latest
        read was there. Bounds from the read only; a window never read (refused before this), a read with no elements
        or a backend that cannot say which window is at the point is not checked."""
        if not target or int(target) not in self._latest_trees:
            return
        number, elements = self._latest_trees[int(target)]
        boxes = [e for e in elements.values() if isinstance(e, dict) and e.get("bounds") and len(e["bounds"]) == 4]
        if not boxes:
            return
        from yuki.agent.situation import CONTAINER_SHARE

        def area(b: Any) -> int:
            return max(0, int(b[2]) - int(b[0])) * max(0, int(b[3]) - int(b[1]))

        whole = max(area(e["bounds"]) for e in boxes) or 1
        for e in boxes:
            b = e["bounds"]
            # review F3: the window's root, a page's document or a pane covering much of the window takes the keyboard
            # (so it reads as interactive) but is no control: only something smaller under the point counts
            if area(b) > CONTAINER_SHARE * whole:
                continue
            if e.get("is_interactive") and int(b[0]) <= x < int(b[2]) and int(b[1]) <= y < int(b[3]):
                return
        raise ToolError(
            f"not clicked: nothing you can click is at ({x},{y}) in read #{number} of window {target}. A control hidden "
            f"until the pointer comes near it (a player bar in a full-screen view) is not there yet: hover where it "
            f"appears, read the window again, then click it by its element id from that read")

    def _toggle_state(self, tool_input: dict[str, Any]) -> tuple[int, dict[str, Any], str] | None:
        """``(hwnd, element, state)`` when the element a click names is a toggle with an on/off state in its read
        (UI Automation's Toggle pattern, as the read lists it), else ``None``."""
        found = self._read_element(tool_input)
        if found is None:
            return None
        hwnd, element = found
        states = [str(s) for s in element.get("states") or []]
        if "toggle" not in (element.get("patterns") or []) or not ({"on", "off"} & set(states)):
            return None
        return hwnd, element, "on" if "on" in states else "off"

    def _live_element(self, hwnd: int, element: dict[str, Any]) -> dict[str, Any] | None:
        """The same control in a fresh read of its window (same role and name, the box nearest the read's), or
        ``None`` when it is not there now. The fresh read does not replace the latest read (its ids stay valid)."""
        try:
            tree = _plain(self.backend.get_window_tree(int(hwnd), max_elements=self.max_tree_elements,
                                                       timeout_s=self.tree_timeout_s))
        except Exception:  # noqa: BLE001
            return None
        want = element.get("bounds") or (0, 0, 0, 0)
        rid = element.get("runtime_id")
        if rid:   # report 58: the very element, when the provider gives it an identity
            exact = [e for e in (tree or {}).get("elements") or [] if isinstance(e, dict)
                     and e.get("runtime_id") and list(e["runtime_id"]) == list(rid) and e.get("role") == element.get(
                         "role")]
            if exact:
                return exact[0]
        same = [e for e in (tree or {}).get("elements") or [] if isinstance(e, dict)
                and e.get("role") == element.get("role") and e.get("name") == element.get("name") and e.get("bounds")]
        if not same:
            return None
        return min(same, key=lambda e: sum(abs(int(a) - int(b)) for a, b in zip(e["bounds"], want)))

    def _window_shown(self, hwnd: int | None) -> str | None:
        """fix6: how a window is shown, measured from Windows (``fullscreen``: it covers its monitor with no caption;
        ``maximized``, ``normal``, ``minimized``), or ``None`` when the backend cannot say. A few Win32 calls."""
        measure = getattr(self.backend, "window_state", None)
        if not hwnd or not callable(measure):
            return None
        try:
            shown = measure(int(hwnd))
        except Exception:  # noqa: BLE001
            return None
        return str(shown) if shown else None

    #: fix6: how long a full-screen control's window is watched for its full screen to switch after the click (s),
    #: and how often it is measured meanwhile (a few Win32 calls; only while that click's result is pending).
    SCREEN_WAIT_S = 1.5
    SCREEN_POLL_S = 0.05

    def _await_window(self, hwnd: int, done: Callable[[str | None], bool]) -> str | None:
        """fix6: the window's shown state once ``done`` says so, else the last measure after :attr:`SCREEN_WAIT_S`.
        Windows sends no event for a window's own full screen switch that this process listens to, so it is measured
        for a bounded moment (as a window action's own full-screen check does)."""
        deadline = time.monotonic() + self.SCREEN_WAIT_S
        shown = self._window_shown(hwnd)
        while not done(shown) and time.monotonic() < deadline:
            time.sleep(self.SCREEN_POLL_S)
            shown = self._window_shown(hwnd)
        return shown

    def _untracked_toggle(self, hwnd: int, element: Any) -> str | None:
        """fix6: what this session learned about a toggle whose own on/off does not follow what it switches
        (``full_screen`` or ``name``), or ``None``."""
        found = [self._untracked_toggles.get((int(hwnd), key)) for key in _toggle_keys(element)]
        return "full_screen" if "full_screen" in found else "name" if "name" in found else None

    def _learn_toggle(self, hwnd: int, element: Any, now: Any, how: str) -> str:
        """fix6: remember (this session) that a toggle's own on/off does not show its state, under every key it had in
        both reads (its RuntimeId, its role and either name). ``full_screen`` is never downgraded to ``name``."""
        for key in _toggle_keys(element) + _toggle_keys(now):
            slot = (int(hwnd), key)
            if self._untracked_toggles.get(slot) != "full_screen":
                self._untracked_toggles[slot] = how
        return how

    def _click_toggle(self, tool_input: dict[str, Any], toggle: tuple[int, dict[str, Any], str], *, button: str,
                      guard: int | None, label: str, x: int, y: int) -> ToolOutcome:
        """N4: read the toggle's live state, click only when it is not the wanted one, read it back. Live 3 Oct
        Spotify's Lyrics toggle was clicked six times from stale reads and the lyrics view flipped open and shut.

        Report 58 review (F3): the live reads are the outcome bracket's own two reads of the window, so the result
        also says what else the toggle changed there ("Group "Lyrics view" appeared ..."); without outcome checks
        (or when the window cannot be read twice) it reads the toggle alone, as before."""
        hwnd, element, read_state = toggle
        want = tool_input.get("toggle_to")
        if want not in ("on", "off", *SCREEN_WANTS):
            raise ToolError(f"not clicked: {label} is a toggle (it was {read_state} in the read): give toggle_to, on "
                            f"or off, the state they want; it is clicked only when it is not there already")
        element_id = tool_input.get("element")
        element_id = element_id if isinstance(element_id, int) and not isinstance(element_id, bool) else None
        held: dict[str, Any] = {}
        # fix6 (report 73, release 6): Spotify's "Enter/Exit full screen" reads off in both views, so "already off"
        # from its own state was a false verified. A full-screen control is judged by the window itself (its size
        # against the monitor, from Windows): asked as full_screen / not_full_screen, or learned this session from a
        # click that flipped the window's full screen while the control's own state read the same.
        learned = self._untracked_toggle(hwnd, element)
        screen_want = want if want in SCREEN_WANTS else (
            ("full_screen" if want == "on" else "not_full_screen") if learned == "full_screen" else None)
        shown_before = self._window_shown(hwnd)
        held["screen_before"] = shown_before

        def state_of(found: Any) -> str | None:
            states = [str(s) for s in (_field(found, "states") or ())]
            return "on" if "on" in states else "off" if "off" in states else None

        def screen_reached(shown: str | None) -> bool | None:
            """Whether the window shows the full screen asked for (``None``: it cannot be measured)."""
            if shown is None or screen_want is None:
                return None
            return (shown == "fullscreen") == (screen_want == "full_screen")

        def in_read(read: list[Any] | None) -> tuple[bool, Any]:
            """``(read, the toggle in it)``: from the bracket's read (by its carried id, its RuntimeId, else the
            control the app drew again in its place), else a live read of its own."""
            if read is not None and element_id is not None:
                return True, _toggle_in(read, element_id, held.get("was"))
            return False, self._live_element(hwnd, element)

        def before(read: list[Any] | None) -> ToolOutcome | None:
            _from_bracket, live = in_read(read)
            if live is None:
                raise ToolError(f"not clicked: {label} is not in a fresh read of its window: it may be hidden until "
                                f"the pointer comes near it, or gone. Hover where it appears, read again, and click "
                                f"it from that read")
            held["was"] = live
            now = held["now"] = state_of(live)
            if screen_want is not None:
                reached = screen_reached(shown_before)
                if reached is None:
                    raise ToolError(f"not clicked: whether the window is full screen cannot be measured here, so "
                                    f"{label} cannot be judged by it; give toggle_to on or off")
                if reached:
                    words = "full screen" if screen_want == "full_screen" else "not full screen"
                    text = (f"the window is already {words} (measured from Windows: its size against the monitor): "
                            f"{label} not clicked, nothing changed")
                    outcome = ToolOutcome(name="click", ok=True, summary=text, content=[{"type": "text", "text": text}],
                                          payload={"details": {"verified": True, "noop": True, "state": now,
                                                               "window_state": shown_before, "toggle_to": want}})
                    return self._labelled(outcome, label)
                return None
            if learned == "name" and now == want:
                raise ToolError(f"not clicked: {label}'s own on/off reads the same in both its states (its name "
                                f"changes, its on/off does not), so whether it is already {want} cannot be read from "
                                f"it. Judge by what it switches: for a full-screen control give full_screen or "
                                f"not_full_screen; otherwise read the window first")
            if learned is None and now == want and _renamed_same_state(element, live):
                # review F6: renamed since the thinker's read with the same on/off. For this call only (a passive
                # difference between two reads is no evidence that its on/off never moves: a like button's name
                # carries a live count), so nothing is learned; a fresh read makes the names agree again
                raise ToolError(f"not clicked: {label}'s name changed since your read while its on/off stayed {now}, "
                                f"so whether it is already {want} is not clear from it. Read the window again and "
                                f"click from that read; for a full-screen control give full_screen or not_full_screen")
            if now == want and shown_before == "fullscreen":
                # review F4: the window itself measures full screen, and a full-screen control's own on/off can read
                # the same in both views (Spotify's "Exit full screen", off either way): "already" from it is not
                # verified while the window is full screen. Said as the measured fact; judged by the window on ask
                text = (f"{label} reads {want} (read live just now): not clicked, nothing changed; but the window "
                        f"itself is full screen (measured from Windows: its size against the monitor), so its own "
                        f"on/off does not confirm it. If this control switches full screen, give toggle_to full_screen "
                        f"or not_full_screen to judge it by the window")
                outcome = ToolOutcome(name="click", ok=True, summary=text, content=[{"type": "text", "text": text}],
                                      payload={"details": {"noop": True, "state": now, "toggle_unconfirmed": True,
                                                           "window_state": shown_before, "toggle_to": want}})
                return self._labelled(outcome, label)
            if now == want:
                outcome = ToolOutcome(name="click", ok=True, summary=f"{label} is already {want} (read live just "
                                                                     f"now): not clicked, nothing changed",
                                      content=[{"type": "text", "text": f"{label} is already {want} (read live just "
                                                                        f"now): not clicked, nothing changed"}],
                                      payload={"details": {"verified": True, "noop": True, "state": now}})
                return self._labelled(outcome, label)
            return None

        def act() -> ToolOutcome:
            via = self._pattern_first("click", tool_input, "click") if button == "left" else None
            if via is None:
                self._before_input("click")
                aim_x, aim_y = self._aim(hwnd, element_id, x, y)
                via = self._from_action("click", self.backend.click(aim_x, aim_y, button=button, clicks=1,
                                                                    expect_hwnd=guard))
            if via.ok:
                self._mark_acted(hwnd, "click")
            held["own"] = _own_toggle_state(via)
            return via

        def settled(read: list[Any] | None) -> bool:
            """percFix: the read after the click shows the toggle in the state asked for, or the control itself
            already said so right after the pattern call (until then the bracket reads the window again on its next
            event, bounded)."""
            if screen_want is not None:
                return screen_reached(self._window_shown(hwnd)) is True
            if held.get("own") == want:
                return True
            return read is not None and state_of(_toggle_in(read, element_id, held.get("was"))) == want

        def after(via: ToolOutcome, read: list[Any] | None, change: Any = None) -> ToolOutcome:
            if screen_want is not None:
                return screen_after(via, read)
            _from_bracket, found = in_read(read)
            state = None if found is None else state_of(found)
            # percFix: the toggle's own state is the verdict. When it still reads the old state (or was drawn again
            # without one) but the window did change - something appeared or went, it was renamed - that is not a
            # failure the click can be blamed for: unverified, and the account says what changed. Only a toggle
            # that reads the old state in a window where nothing changed, after the wait, is "did not take".
            moved_on = bool(change is not None and getattr(change, "evidence", False))
            # the control's own ToggleState, read from it right after the pattern call (and after its own change
            # event): the most direct read-back there is, when the window's tree is slower to show it
            own_state = _own_toggle_state(via)
            if state == want:
                verified: bool | None = True
                note = f"; read back: now {state}"
            elif own_state == want:
                verified = True
                note = f"; read back from the control itself: now {want}"
            elif moved_on:
                verified = None
                note = ((f"; read back: still {state} (wanted {want})" if state else
                         "; read back: its new state is not shown")
                        + ", though the window did change (below): it may have taken effect in a way its own state "
                          "does not show. Read the window before clicking it again")
            else:
                verified = False
                note = (f"; read back: now {state}" if state else "; read back: it is not in the window now")
                if via.ok:
                    note += f" (wanted {want}): do not click it again from this read; read the window first"
            shown_after = self._window_shown(hwnd) if via.ok else shown_before

            def flips(shown: str | None) -> bool:
                return (shown_before is not None and shown is not None
                        and (shown_before == "fullscreen") != (shown == "fullscreen"))

            if (via.ok and verified is not True and shown_before == "fullscreen" and not flips(shown_after)
                    and _renamed_same_state(held.get("was"), found)):
                # renamed with the same on/off: a control whose state is in its name; a full-screen switch can take
                # a moment longer than the window's tree to show on the window itself (bounded). Second review V4:
                # only when the window was full screen before the click (leaving it); otherwise one measure, so a
                # Show/Hide or Mute/Unmute toggle never waits for a switch that is not coming
                shown_after = self._await_window(hwnd, flips) or shown_after
            flipped = flips(shown_after)
            if verified is not True and via.ok and state is not None and state == held.get("now") and flipped:
                # fix6: its own on/off did not move, the window's full screen did: the window is the verdict, and
                # this control is judged by it from now on (never "already" from its own state)
                self._learn_toggle(hwnd, element, found, "full_screen")
                now_full = shown_after == "fullscreen"
                verified = now_full == (want == "on")
                note = (f"; its own on/off still reads {state}, but the window itself is "
                        f"{'now full screen' if now_full else 'no longer full screen'} (measured from Windows: its size "
                        f"against the monitor): this control switches full screen, so judge it by the window "
                        f"(toggle_to full_screen or not_full_screen)")
            elif verified is not True and via.ok and _renamed_same_state(held.get("was"), found):
                # fix6: renamed by the click while its on/off read the same: its state is in its name
                self._learn_toggle(hwnd, element, found, "name")
                note += "; its name changed but its on/off did not, so its on/off does not show its state"
            via.summary += note
            if via.content and via.content[0].get("type") == "text":
                via.content[0]["text"] += note
            if isinstance(via.payload, dict):
                details = (dict(via.payload.get("details") or {}) if isinstance(via.payload.get("details"), dict)
                           else {})
                details.update(verified=verified, state_before=held.get("now"), state_after=state, toggle_to=want)
                if flipped:
                    details.update(window_state_before=shown_before, window_state_after=shown_after)
                if verified is None:
                    details.pop("verified", None)
                    details["toggle_unconfirmed"] = True
                via.payload = {**via.payload, "details": details}
            return self._labelled(via, label)

        def screen_after(via: ToolOutcome, read: list[Any] | None) -> ToolOutcome:
            """fix6: a full-screen control's verdict from the window itself, measured from Windows (its size against
            the monitor, no caption), waited for a moment (bounded) after the click; never its own on/off."""
            shown = self._window_shown(hwnd)
            if via.ok and screen_reached(shown) is False:
                shown = self._await_window(hwnd, lambda s: screen_reached(s) is True) or shown
            reached = screen_reached(shown)
            words = ("full screen" if shown == "fullscreen" else
                     f"not full screen ({shown})" if shown else "")
            if reached:
                verified: bool | None = True
                note = f"; the window is now {words} (measured from Windows: its size against the monitor)"
                _from_bracket, found = in_read(read)
                if found is not None and state_of(found) == held.get("now"):
                    self._learn_toggle(hwnd, element, found, "full_screen")
            elif reached is None:
                verified = None
                note = "; whether the window is full screen could not be measured after it"
            else:
                verified = False
                note = (f"; the window is still {words} (measured from Windows): it did not switch" if via.ok
                        else "")
            via.summary += note
            if via.content and via.content[0].get("type") == "text":
                via.content[0]["text"] += note
            if isinstance(via.payload, dict):
                details = (dict(via.payload.get("details") or {}) if isinstance(via.payload.get("details"), dict)
                           else {})
                details.update(verified=verified, toggle_to=want, window_state_before=shown_before,
                               window_state_after=shown, window_state_changed=shown != shown_before)
                if verified is None:
                    details.pop("verified", None)
                    details["toggle_unconfirmed"] = True
                via.payload = {**via.payload, "details": details}
            return self._labelled(via, label)

        return self._bracketed("click", hwnd, act, target=element_id,
                               target_label=self._short_label(element), before_hook=before, after_hook=after,
                               settled=settled)

    def _pointed_window(self, x: int, y: int) -> int | None:
        locate = getattr(self.backend, "window_at", None)
        if not callable(locate):
            return None
        try:
            return int(locate(x, y) or 0) or None
        except Exception:  # noqa: BLE001
            return None

    def _mark_acted(self, hwnd: int | None, name: str) -> None:
        """Yuki acted on ``hwnd`` after its latest read: coordinates from that read may be stale now."""
        if hwnd and int(hwnd) in self._read_facts:
            self._read_facts[int(hwnd)]["acted"] = name

    def _check_raw_point(self, x: int, y: int, tool_input: dict[str, Any]) -> int | None:
        """The staleness rule of element clicks, for a click at raw x,y; returns the window at the point.

        Refused (nothing is sent) when the point is in a window that is opaque to UI Automation (a point
        there is a guess), in a user window never read in this conversation, or in one that changed since
        its latest read: moved or resized, retitled, or acted on by Yuki since (the view after the turn's
        actions is a new read).  ``read``, when given, must be that window's latest read.  A backend that
        cannot say which window is at a point, and the shell's own surfaces (taskbar, desktop), are not
        checked.  Facts about windows only, never about what the model meant.

        Raises:
            ToolError: the refusal, saying why and what to do instead.
        """
        target = self._pointed_window(x, y)
        if not target:
            return None
        facts = self._read_facts.get(target)
        title = _window_title_now(target)
        name = f"window {target} \"{(title or '')[:60]}\""
        try:
            from yuki.perception.tree import measured_opaque

            opaque_now = measured_opaque(target)
        except Exception:  # noqa: BLE001
            opaque_now = False
        if opaque_now or (facts is not None and facts.get("opaque")):
            raise ToolError(
                f"not clicked: ({x},{y}) is in {name}, which draws its own interface - its UI Automation "
                f"elements cover under {OPAQUE_PERCENT}% of it, so any point there is a guess (the text it exposes "
                f"can still be read). Use the app's own keyboard shortcuts and read its title after each step "
                f"(or the app's own data given with its read), or ask the user naming the options you know"
            )
        if facts is None:
            try:
                from yuki.perception.windows import is_user_window

                user_window = is_user_window(target)
            except Exception:  # noqa: BLE001
                user_window = False
            if not user_window:
                return target
            raise ToolError(
                f"not clicked: ({x},{y}) is in {name}, which has not been read in this conversation, so the "
                f"point is a guess. Read it with look_at_window and click an element id from that read"
            )
        if tool_input.get("read") is not None:
            asked = self._need_int(tool_input, "read")
            if asked != facts["read"]:
                raise ToolError(
                    f"not clicked: coordinates from read #{asked} are out of date - the latest read of {name} "
                    f"is #{facts['read']}. Use a point or an element id from read #{facts['read']}"
                )
        changed = []
        rect_now = _window_rect_now(target)
        if facts.get("rect") and rect_now and list(rect_now) != list(facts["rect"]):
            changed.append("it moved or was resized")
        if title is not None and title != facts.get("title"):
            changed.append("its title changed")
        if facts.get("acted"):
            changed.append(f"you acted on it since ({facts['acted']})")
        if changed:
            raise ToolError(
                f"not clicked: {name} changed since read #{facts['read']} ({'; '.join(changed)}), so ({x},{y}) "
                f"may point at something else now. Read it again, then click an element id from the new read"
            )
        return target

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
        if tool_input.get("accept_suggestion"):
            options["accept_suggestion"] = True   # likewise (v3.15)
        if target is not None and options.get("clear") and not options["press_enter"] and not options.get(
                "accept_suggestion"):
            # Replacing a field's contents is what its writable Value does: no click, no keys, no clipboard.
            via = self._pattern_first("type_text", tool_input, "set_value", value=text)
            if via is not None:
                return self._labelled(via, target[3])
        self._before_input("type_text")

        def act() -> ToolOutcome:
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

        if not options["press_enter"]:
            return act()   # the field's own read-back says whether the text arrived
        # report 58: Enter sends or submits; what that did shows in the window (the message in the chat, the box empty)
        return self._bracketed("type_text", target[2] if target is not None else self._key_window(tool_input), act)

    def _do_hotkey(self, tool_input: dict[str, Any]) -> ToolOutcome:
        keys = tool_input.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ToolError(f"'keys' must be a non-empty list of strings, got {keys!r}")
        system = len(keys) == 1 and keys[0].strip().casefold() in self.SYSTEM_KEYS
        if not system:
            self._before_input("hotkey")

        def act() -> ToolOutcome:
            return self._from_action("hotkey", self.backend.hotkey(*keys, expect_hwnd=self._opt_hwnd(tool_input)))

        # a system key (media, volume) acts outside any window: nothing in one to compare
        return act() if system else self._bracketed("hotkey", self._key_window(tool_input), act)

    def _do_press(self, tool_input: dict[str, Any]) -> ToolOutcome:
        key = self._need_str(tool_input, "key")
        system = key.strip().casefold() in self.SYSTEM_KEYS
        if not system:
            self._before_input("press")

        def act() -> ToolOutcome:
            return self._from_action(
                "press",
                self.backend.press(
                    key,
                    times=self._opt_int(tool_input, "times", 1),
                    expect_hwnd=self._opt_hwnd(tool_input),
                ),
            )

        return act() if system else self._bracketed("press", self._key_window(tool_input), act)

    def _scroller_at(self, hwnd: int | None, x: int, y: int) -> tuple[int, str] | None:
        """``(id, label)`` of the smallest scrollable element of the window's latest read whose box holds (x, y), when
        it has a RuntimeId to be found again by; ``None`` otherwise."""
        if not hwnd or int(hwnd) not in self._latest_trees:
            return None
        model = self._models.get(int(hwnd))
        pool = model.latest.values() if model is not None and model.latest else self._latest_trees[int(hwnd)][1].values()
        best: tuple[int, int, dict[str, Any]] | None = None
        for raw in pool:
            element = raw if isinstance(raw, dict) else _plain(raw)
            b = element.get("bounds")
            if not element.get("is_scrollable") or not element.get("runtime_id") or not b or len(b) != 4:
                continue
            if not (int(b[0]) <= x < int(b[2]) and int(b[1]) <= y < int(b[3])):
                continue
            area = (int(b[2]) - int(b[0])) * (int(b[3]) - int(b[1]))
            if best is None or area < best[0]:
                best = (area, int(element["id"]), element)
        if best is None:
            return None
        return best[1], f"[{best[1]}] {self._short_label(best[2])} (the scrolling area at ({x},{y}))"

    def _key_window(self, tool_input: dict[str, Any]) -> int | None:
        """The window keys go to: ``expect_hwnd``, else the one in front (``None`` when it cannot be told)."""
        hwnd = self._opt_hwnd(tool_input)
        if hwnd is not None or not self.verify_outcomes:
            return hwnd
        try:
            return self.foreground_hwnd()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _short_label(element: dict[str, Any] | None) -> str:
        """``Button "Lyrics"`` for the account of what changed."""
        if not isinstance(element, dict):
            return ""
        name = " ".join(str(element.get("name") or "").split())[:50]
        role = str(element.get("role") or "element")
        return f'{role} "{name}"' if name else role

    def _do_scroll(self, tool_input: dict[str, Any]) -> ToolOutcome:
        x, y, guard, label = self._point(tool_input)
        found = self._read_element(tool_input) if label else None
        window = guard or (found[0] if found is not None else None) or self._pointed_window(x, y)

        def act() -> ToolOutcome:
            if label:
                via = self._pattern_first("scroll", tool_input, "scroll", dy=self._opt_int(tool_input, "dy", 0),
                                          dx=self._opt_int(tool_input, "dx", 0))
                if via is not None:
                    return self._labelled(via, label)
            else:
                # report 58: at a raw point, the scrolling area the latest read shows there is scrolled through its
                # pattern, found again by its RuntimeId (on Chromium the point itself answers with the renderer pane)
                area = self._scroller_at(window, x, y)
                if area is not None:
                    via = self._pattern_first("scroll", {**tool_input, "element": area[0], "expect_hwnd": window},
                                              "scroll", dy=self._opt_int(tool_input, "dy", 0),
                                              dx=self._opt_int(tool_input, "dx", 0))
                    if via is not None and via.ok:
                        return self._labelled(via, area[1])
                    # it would not scroll that way: the wheel at the point, as before
            self._before_input("scroll")
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

        # report 58: a scroll is judged by the window's content moving, not by one point under it
        dy, dx = self._opt_int(tool_input, "dy", 0), self._opt_int(tool_input, "dx", 0)
        axes = ("v" if dy else "") + ("h" if dx else "")
        first = self._bracketed("scroll", window, act, point=(int(x), int(y)), axes=axes or "v")
        return self._continue_scrolling(tool_input, first, x, y, guard)

    def _continue_scrolling(self, tool_input: dict[str, Any], first: ToolOutcome, x: int, y: int,
                            guard: int | None) -> ToolOutcome:
        """V7: ``until_stopped`` - after a first step that worked, the page goes on scrolling by itself in steps
        (:class:`yuki.actions.scrolling.ContinuousScroll`) until Yuki's voice stops it, the user takes over, the Stop
        hotkey cancels, or the page ends. The request ends; the scroll does not."""
        if tool_input.get("until_stopped") is not True or not first.ok:
            return first
        from yuki.actions.scrolling import ContinuousScroll, input_watch, start_continuous, step_result, voice_hold

        dy, dx = self._opt_int(tool_input, "dy", 0), self._opt_int(tool_input, "dx", 0)
        if not dy and not dx:
            return first
        # Review F6: by raw x,y with no expect_hwnd the steps had no window guard and wheeled into whatever window came
        # to that point later. The scroll goes on only in the window it started in (the one at the point, else the
        # one in front); once another window is in front its steps are refused and it stops.
        if not guard:
            try:
                guard = self._pointed_window(x, y) or self.foreground_hwnd()
            except Exception:  # noqa: BLE001 - no window known: it does not go on (below)
                guard = None
        if not guard:
            note = "It could not tell which window that was, so it did not go on scrolling by itself."
            return dataclasses.replace(first, summary=f"{first.summary}; {note}",
                                       content=[*first.content, {"type": "text", "text": note}])
        backend = self.backend

        def step() -> Any:
            return step_result(backend.scroll(x, y, dy=dy, dx=dx, expect_hwnd=guard))

        where = "the window"
        try:
            from yuki.agent.finish import app_name
            from yuki.perception.windows import window_info

            info = window_info(int(guard)) if guard else None
            if info is not None:
                where = f"{app_name(info.process_name)} \"{(info.title or '')[:60]}\""
        except Exception:  # noqa: BLE001 - a label only
            pass
        notches = abs(dy or dx)
        pace = "fast" if notches >= 6 else "slow" if notches <= 3 else "normal"
        watch = input_watch()
        start_continuous(ContinuousScroll(step, where=where, direction="up" if dy > 0 else "down", pace=pace,
                                          hold=voice_hold, took_over=watch, moving=getattr(watch, "moving", None),
                                          steps_done=1, started_by="thinker"))
        note = ("It now goes on scrolling by itself in steps until they say stop (Yuki's voice stops it), press a key "
                "or click, or the page ends: nothing more to do for the scrolling; finish with done, and say in a few "
                "words how it stops (say stop, or press any key).")
        return dataclasses.replace(first, summary=f"{first.summary}; {note}",
                                   content=[*first.content, {"type": "text", "text": note}])

    def _game_refusal(self, name: str, tool_input: dict[str, Any]) -> str | None:
        """D17(1) on the thinker's side: why this action may not run now (a game is in front, or it is aimed at a
        game's window), else ``None``. ``run_skill`` gates its own rungs (its no-focus ones may run mid-game)."""
        guard = self.game_guard if self.game_guard is not None else isinstance(self.backend, _LazyRealBackend)
        if not guard or name not in ACTION_TOOL_NAMES or name == "run_skill":
            return None
        from yuki.memory import gamegate

        traits = tool_traits(name, tool_input)
        if traits.needs in ("input", "focus") or name == "run_powershell":
            return gamegate.refuse_input(f"tool_{name}")
        hwnd = tool_input.get("hwnd") or tool_input.get("expect_hwnd")
        if traits.needs == "window" and hwnd:
            try:
                return gamegate.refuse_input(f"tool_{name}", hwnd=int(hwnd), front_ok=True)
            except (TypeError, ValueError):
                return None
        return None

    def _do_run_skill(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """Phase S: one of the user's usual ways (yuki.skills.engine.run_for_thinker): its own rungs, checked by its
        done-check; verified only when that check saw the result (D19). A hand-off (its rungs didn't get there) comes
        back as the pinned skill: the thinker does it itself, the loop pins it. Held with a game in front (D17)."""
        key = tool_input.get("skill")
        if not isinstance(key, str) or not key.strip():
            raise ToolError("give the skill's key from the list")
        slots = tool_input.get("slots")
        if slots is not None and not isinstance(slots, dict):
            raise ToolError("slots must be an object of slot names and values")
        from yuki.skills.engine import run_for_thinker

        ctx = self.skill_context or {}
        state: dict[str, Any] = {}
        result = run_for_thinker(tool_input, list(ctx.get("words") or []), memory=ctx.get("memory"),
                                 log=ctx.get("log"), request_id=ctx.get("request_id"), settings=ctx.get("settings"),
                                 verbs=self._skill_act_verbs(state))
        off = state.get("act_off")
        acted = any(t.get("outcome") == "contradicted" for t in result.tried or [])
        if off and (result.outcome == "handed_over" or (result.outcome == "failed" and not acted)):
            return self._skill_act_off(result, off, ctx.get("memory"), acted)
        verified = True if result.done else (False if result.outcome == "failed" else None)
        details: dict[str, Any] = {"verified": verified}
        if result.held:
            details["refused"] = "game"
        text = result.report()
        if result.handoff:
            from yuki.agent.frame import pinned_lines

            text += "\n" + "\n".join(pinned_lines(result.handoff))
        ok = result.outcome in ("done", "already", "unverified", "handed_over", "asked", "other_device")
        payload: dict[str, Any] = {"details": details, "skill": result.skill, "outcome": result.outcome,
                                   "route": result.route, "handoff": result.handoff}
        if result.held:
            payload["refused"] = "game"
        return ToolOutcome(name="run_skill", ok=ok, summary=text.split("\n", 1)[0][:200],
                           content=[{"type": "text", "text": text}], payload=payload)

    #: The skill verbs that touch an app's window (press a control in it, set its own slider, bring it forward, pick its
    #: tab, a driver): refused in an app whose Settings "Act" switch is off (the voice hands' ``ACT_VIAS``).
    SKILL_ACT_VIAS = ("uia_invoke", "slider", "focus", "tab_select", "driver")

    def _skill_act_verbs(self, state: dict[str, Any]) -> dict[str, Any]:
        """Phase S (sWire, sVoice r1 M2): the engine's verbs for the thinker's ``run_skill``, each refusing a
        window-touching rung (:data:`SKILL_ACT_VIAS`) in an app whose "Act" switch is off (``act_off``; the app's name
        kept in ``state["act_off"]``). A rung whose data says it neither takes focus nor sends input (``game =
        "always"`` and ``needs_focus = false``: the mixer, the PC's level) runs, as ``app_volume`` did. Looked up when
        called, so the engine's own table stays the one source."""
        from yuki.skills import engine

        def wrap(via: str) -> Any:
            def run(call: Any) -> Any:
                route = call.route
                window_free = getattr(route, "game", None) == "always" and not getattr(route, "needs_focus", True)
                if via in self.SKILL_ACT_VIAS and not window_free:
                    try:
                        from yuki.agent.judge import _shared_reader

                        policy = self.act_policy if self.act_policy is not None else _shared_reader()
                        off = policy.app_off(engine.acted_app(call.skill, route, call.args))
                    except Exception:  # noqa: BLE001 - an unreadable policy is the backstop's
                        off = None
                    if off:
                        state.setdefault("act_off", off)
                        return engine.RouteResult(ran=False, outcome="act_off")
                fn = engine.VERB_FUNCS.get(via)
                return fn(call) if fn is not None else engine.RouteResult(ran=False, outcome="no_verb")
            return run

        return {via: wrap(via) for via in list(engine.VERB_FUNCS)}

    def _skill_act_off(self, result: Any, off: str, memory: Any, acted: bool) -> ToolOutcome:
        """A ``run_skill`` refused in an app whose "Act" switch is off: the judge's plain line, never pinned for the
        thinker to do by hand, and the ledger row says ``refused`` (a rung that acted before keeps its moment)."""
        from yuki.agent.judge import app_off_line

        run_key = getattr(result, "run_key", None)
        if run_key and memory is not None:
            def go(client: Any) -> Any:
                from yuki.skills import registry

                return registry.finish_run(getattr(client, "store", client), run_key, outcome="refused",
                                           checked=None, acted=acted)

            try:
                call = getattr(memory, "call", None)
                call("skill_run_finish", go, timeout_s=0.5) if callable(call) else go(memory)
            except Exception:  # noqa: BLE001 - the ledger never breaks a tool
                pass
        line = app_off_line(off)
        text = (f"Not done: Act is switched off for {off} in Yuki's settings, so Yuki does not click, type or press "
                f"keys there; nothing was done there, and do not do it by hand either. Say: {line}")
        return ToolOutcome(name="run_skill", ok=False, summary=text[:200], content=[{"type": "text", "text": text}],
                           payload={"details": {"verified": None, "refused": "act_off"}, "skill": result.skill,
                                    "outcome": "refused", "route": result.route, "handoff": None,
                                    "refused": "act_off"})

    def _do_run_powershell(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "run_powershell",
            self.backend.run_powershell(
                self._need_str(tool_input, "command"), timeout_s=self.tool_timeout_s
            ),
        )

    def _do_hover(self, tool_input: dict[str, Any]) -> ToolOutcome:
        x, y, guard, label = self._point(tool_input)
        self._before_input("hover")
        return self._labelled(self._from_action("hover", self.backend.hover(x, y, expect_hwnd=guard, label=label)),
                              label)

    def _do_app_volume(self, tool_input: dict[str, Any]) -> ToolOutcome:
        from yuki.actions.app_volume import run_tool

        return self._from_action("app_volume", run_tool(tool_input, set_volume=self.backend.app_volume))

    def _do_set_slider(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """N2 (report 47): a slider set by its RangeValue, else its own arrow keys, read back; never dragged."""
        _x, _y, guard, label = self._point(tool_input)
        found = self._read_element(tool_input)
        if found is None:
            raise ToolError("give the slider's element id from the latest read of its window")
        hwnd, element = found
        to, by = tool_input.get("to"), tool_input.get("by")
        for key, v in (("to", to), ("by", by)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ToolError(f"'{key}' must be a number, got {v!r}")
        if to is None and by is None:
            raise ToolError("give to (0-100 percent of its range) or by (percent points)")
        shortcut = tool_input.get("keys")
        if shortcut is not None and (not isinstance(shortcut, list) or not shortcut
                                     or not all(isinstance(k, str) and k.strip() for k in shortcut)):
            raise ToolError(f"'keys' must be a list of key names (the app's own shortcut), got {shortcut!r}")
        act = getattr(self.backend, "set_slider", None)
        if not callable(act):
            raise ToolError("this desktop has no slider hand")
        press = getattr(self.backend, "press", None)
        hotkey = getattr(self.backend, "hotkey", None)
        hover = getattr(self.backend, "hover", None)

        def waited(stopped: Callable[[], bool] | None) -> None:
            self._before_input("set_slider")
            if stopped is not None and stopped():   # review F7: the result went back while the gate waited
                raise TimeoutError("out of time while waiting for the keyboard")

        def keys(key: str, times: int, stopped: Callable[[], bool] | None = None) -> Any:
            waited(stopped)
            return press(key, times=int(times), expect_hwnd=guard or hwnd)

        def combo(names: list[str], stopped: Callable[[], bool] | None = None) -> Any:
            waited(stopped)
            return hotkey(*names, expect_hwnd=guard or hwnd)

        def point(x: int, y: int, stopped: Callable[[], bool] | None = None) -> Any:
            # vfix3: a player bar hidden until the pointer is over it (no click)
            waited(stopped)
            return hover(int(x), int(y), expect_hwnd=guard or hwnd, label="the slider's bar")

        extra: dict[str, Any] = {}
        if callable(hover):
            extra["hover"] = point
        if shortcut and callable(hotkey):
            extra.update(hotkey=combo, keys=[k.strip() for k in shortcut])
        try:
            report = act(hwnd, element, to=to, by=by, press=keys if callable(press) else None, **extra)
        except TypeError:   # an older slider hand (a test desk's) without the reveal and shortcut
            report = act(hwnd, element, to=to, by=by, press=keys if callable(press) else None)
        outcome = self._from_action("set_slider", report)
        if outcome.ok:
            self._mark_acted(hwnd, "set_slider")
        return self._labelled(outcome, label)

    def _do_open_url(self, tool_input: dict[str, Any]) -> ToolOutcome:
        url = self._need_str(tool_input, "url")
        app = tool_input.get("app")
        if app is not None and not isinstance(app, str):
            raise ToolError(f"'app' must be a string, got {app!r}")
        if app and app.strip():
            return self._from_action("open_url", self.backend.open_url(url, app=app))
        return self._from_action("open_url", self.backend.open_url(url))

    # -- reading without the screen ------------------------------------------

    #: Most characters of a file or page handed to the model in one result.
    READ_MAX_CHARS = 60_000
    #: Most bytes of a web page downloaded, and how long the download may take.
    FETCH_MAX_BYTES = 3_000_000
    FETCH_TIMEOUT_S = 12.0

    def _do_read_file(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """A text file's contents (clipped), read in-process: nothing opens, nothing changes."""
        from pathlib import Path

        started = time.perf_counter()
        path = Path(self._need_str(tool_input, "path").strip().strip('"'))
        start_line = max(1, self._opt_int(tool_input, "start_line", 1))
        if not path.is_file():
            raise ToolError(f"no such file: {path}")
        raw = path.read_bytes()[: self.READ_MAX_CHARS * 4 + 4096]
        if b"\x00" in raw[:8192]:
            raise ToolError(f"{path.name} is a binary file, not text")
        text = raw.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        body = "\n".join(lines[start_line - 1:])
        clipped = len(body) > self.READ_MAX_CHARS
        if clipped:
            body = body[: self.READ_MAX_CHARS]
            next_line = start_line + body.count("\n") + 1
            body += f"\n[clipped: continue with start_line={next_line}]"
        size = path.stat().st_size
        summary = f"{len(body)} chars of {path.name} ({size} bytes)" + (" (clipped)" if clipped else "")
        return ToolOutcome(
            name="read_file", ok=True, summary=summary,
            content=[{"type": "text", "text": f"file {path} ({size} bytes), from line {start_line}:\n{body}"}],
            payload={"path": str(path), "bytes": size, "start_line": start_line, "chars": len(body),
                     "clipped": clipped},
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_fetch_url(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """A web page's title and readable text, downloaded in the background (no browser, no screen)."""
        import urllib.request
        from urllib.parse import urlparse

        started = time.perf_counter()
        url = self._need_str(tool_input, "url").strip()
        if urlparse(url).scheme not in ("http", "https"):
            raise ToolError(f"only http and https addresses can be fetched, got {url!r}")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Yuki reader)"})
        with urllib.request.urlopen(request, timeout=self.FETCH_TIMEOUT_S) as response:  # noqa: S310 - scheme checked
            status = getattr(response, "status", 200)
            ctype = response.headers.get_content_type() or ""
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(self.FETCH_MAX_BYTES)
            final = response.geturl()
        decoded = raw.decode(charset, errors="replace")
        if ctype in ("text/html", "application/xhtml+xml"):
            title, text = html_text(decoded)
        elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            title, text = "", decoded
        else:
            raise ToolError(f"{final} is {ctype or 'not text'}, not a page that can be read as text")
        clipped = len(text) > self.READ_MAX_CHARS
        text = text[: self.READ_MAX_CHARS] + ("\n[clipped]" if clipped else "")
        summary = f'{len(text)} chars of "{title or final}"' + (" (clipped)" if clipped else "")
        return ToolOutcome(
            name="fetch_url", ok=200 <= int(status) < 400, summary=summary,
            content=[{"type": "text", "text": f"{final} (HTTP {status}) {title}\n{text}"}],
            payload={"url": url, "final_url": final, "status": status, "content_type": ctype,
                     "chars": len(text), "clipped": clipped},
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    # -- tabs --------------------------------------------------------------

    #: Budget of the brain's strip read (the voice's own read keeps its 0.2 s).
    TABS_READ_S = 1.0

    def _do_read_tabs(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """A window's tab strip with each tab's id (its UIA RuntimeId), full name and place. A look."""
        hwnd = self._need_int(tool_input, "hwnd")
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read):
            raise ToolError("this backend cannot read a window's tabs")
        from yuki.perception.tabs import format_tabs

        started = time.perf_counter()
        strip = read(hwnd, budget_s=self.TABS_READ_S)
        payload = _plain(strip)
        tabs = list(getattr(strip, "tabs", None) or [])
        if tabs:
            self.tab_names[int(hwnd)] = {str(getattr(t, "tab_id", "") or ""): str(getattr(t, "name", "") or "")
                                         for t in tabs if getattr(t, "tab_id", None)}
        error = getattr(strip, "error", None)
        ok = bool(tabs) or not error
        summary = (f"{len(tabs)} tab{'' if len(tabs) == 1 else 's'}" if tabs
                   else (f"could not read the tabs: {error}" if error else "no tabs"))
        return ToolOutcome(
            name="read_tabs",
            ok=ok,
            summary=summary,
            content=[{"type": "text", "text": format_tabs(strip)}],
            payload=payload if isinstance(payload, dict) else {"result": payload},
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_tab(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """Select or close one tab by its id; the result carries the strip as read again after it."""
        hwnd = self._need_int(tool_input, "hwnd")
        tab_id = self._need_str(tool_input, "tab").strip().strip("[]").strip()
        action = self._need_str(tool_input, "action")
        if action not in ("select", "close"):
            raise ToolError(f"'action' must be select or close, got {action!r}")
        act = getattr(self.backend, "tab", None)
        if not callable(act):
            raise ToolError("this backend cannot act on a window's tabs")
        result = act(hwnd, tab_id, action)
        outcome = self._from_action("tab", result)
        details = getattr(result, "details", {}) or {}
        after = details.get("tabs_after") if isinstance(details, dict) else None
        if isinstance(after, list):
            lines = [f"Tabs of window {hwnd} now, read again after it (* = the one showing):"]
            lines += [f"- [{t.get('id') or 'no id'}] {t.get('index')}{'*' if t.get('selected') else ''} "
                      f"\"{t.get('name')}\"" for t in after if isinstance(t, dict)]
            if not after:
                lines.append("(none read)")
            if isinstance(details, dict) and details.get("tabs_after_complete") is False:
                lines.append("(that read was cut short; read_tabs again to be sure)")
            outcome.content = [{"type": "text", "text": f"{outcome.summary}\n" + "\n".join(lines)}]
        return outcome

    # -- "this", the clipboard, whole windows (2026-09-29) ---------------------

    #: Budget of an explicit look at the focus (the per-turn line keeps :attr:`focus_wait_s`), and how long
    #: it waits for the focus reader to start on its first use in this process.
    FOCUS_LOOK_S = 0.6
    FOCUS_READY_S = 3.0

    def _do_look_at_focus(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """What has keyboard focus, its selection and the text around the caret. A look, never an action."""
        read = getattr(self.backend, "focus_snapshot", None)
        if not callable(read):
            raise ToolError("this backend cannot read the focus")
        started = time.perf_counter()
        try:
            focus = read(timeout_s=self.FOCUS_LOOK_S, ready_wait_s=self.FOCUS_READY_S)
        except TypeError:  # a backend written before ready_wait_s
            focus = read(timeout_s=self.FOCUS_LOOK_S)
        ok = bool(getattr(focus, "ok", False))
        lines = [focus.line(limit=600)] if hasattr(focus, "line") else [str(focus)]
        if ok and getattr(focus, "role", "") and getattr(focus, "selection", ""):
            # the line shows the selection; the text on each side of it says where it sits
            if getattr(focus, "before", "") or getattr(focus, "after", ""):
                lines.append(f'text before the selection: "{focus.before}"')
                lines.append(f'text after the selection: "{focus.after}"')
        payload = {k: v for k, v in _plain(focus).items() if k not in ("value", "selection", "before", "after")} \
            if isinstance(_plain(focus), dict) else {}
        payload.update(selection_chars=len(getattr(focus, "selection", "") or ""),
                       value_chars=len(getattr(focus, "value", "") or ""))
        return ToolOutcome(
            name="look_at_focus",
            ok=ok,
            summary=lines[0] if ok else f"could not read the focus: {getattr(focus, 'error', '') or 'no answer'}",
            content=[{"type": "text", "text": "\n".join(lines)}],
            payload=payload,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_read_clipboard(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """The clipboard's text and formats (private content never read). A look; its text is not logged."""
        read = getattr(self.backend, "read_clipboard", None)
        if not callable(read):
            raise ToolError("this backend cannot read the clipboard")
        started = time.perf_counter()
        clip = read()
        formats = list(getattr(clip, "formats", []) or [])
        owner = getattr(clip, "owner_process", "") or "an unknown app"
        if not getattr(clip, "ok", False):
            raise ToolError(f"could not read the clipboard: {getattr(clip, 'error', '') or 'no answer'}")
        if getattr(clip, "private", False):
            text = (f"The clipboard holds something {owner} marked private (a password manager does this), so "
                    f"it was not read. Formats: {', '.join(formats[:8]) or 'none'}.")
            summary = "private content, not read"
        elif getattr(clip, "text", None) is None:
            text = f"The clipboard holds no text (formats: {', '.join(formats[:8]) or 'nothing'}), put there by {owner}."
            summary = "no text on the clipboard"
        else:
            clipped = " (clipped: the start only)" if getattr(clip, "truncated", False) else ""
            text = (f"Clipboard text from {owner}, {getattr(clip, 'chars', 0)} characters{clipped}:\n"
                    f"{clip.text}")
            summary = f"{getattr(clip, 'chars', 0)} characters of text from {owner}"
        return ToolOutcome(
            name="read_clipboard",
            ok=True,
            summary=summary,
            content=[{"type": "text", "text": text}],
            # content-free: the text itself only goes to the model (whose request log filters secrets)
            payload={"chars": getattr(clip, "chars", 0), "formats": formats[:12], "owner": owner,
                     "private": bool(getattr(clip, "private", False)), "truncated": bool(getattr(clip, "truncated", False)),
                     "sequence": getattr(clip, "sequence", 0)},
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _do_write_clipboard(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = tool_input.get("text")
        if not isinstance(text, str):
            raise ToolError(f"'text' must be a string, got {text!r}")
        write = getattr(self.backend, "write_clipboard", None)
        if not callable(write):
            raise ToolError("this backend cannot write the clipboard")
        outcome = self._from_action("write_clipboard", write(text))
        return outcome

    def _do_window_action(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """One whole window minimized / maximized / full screen / restored / focused / closed, verified."""
        hwnd = self._need_int(tool_input, "hwnd")
        action = self._need_str(tool_input, "action")
        if action not in ("minimize", "maximize", "fullscreen", "restore", "focus", "close"):
            raise ToolError(f"'action' must be minimize, maximize, fullscreen, restore, focus or close, got {action!r}")
        act = getattr(self.backend, "window_action", None)
        if not callable(act):
            raise ToolError("this backend cannot act on whole windows")
        outcome = self._from_action("window_action", act(hwnd, action))
        payload = outcome.payload if isinstance(outcome.payload, dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        was_min = bool(details.get("was_minimized")) or details.get("window_state_before") == "minimized"
        if (action == "restore" and outcome.ok and details.get("window_state_after") == "fullscreen"
                and not was_min):
            # fix6 (report 73): "restored" means neither minimized nor maximized, which an app's own full screen also
            # is; measured from Windows the window still covers its monitor, so it was not put back to normal. Review
            # F5: a minimized window brought back to its own full screen (a borderless game) was restored as asked
            note = ("; but the window still covers its whole monitor with no title bar (measured from Windows): it "
                    "is still full screen. Restore does not leave an app's own full screen: its own full-screen "
                    "control or key does")
            outcome.ok = False
            if "ok" in payload:
                payload = {**payload, "ok": False}
            outcome.summary += note
            if outcome.content and outcome.content[0].get("type") == "text":
                outcome.content[0]["text"] += note
            outcome.payload = {**payload, "details": {**details, "verified": False, "still_fullscreen": True}}
        return outcome

    # -- control tools -----------------------------------------------------

    def _do_open_yuki(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """Yuki's own window on a page (v3.15), through the tray's opener (:mod:`yuki.ui.own_window`)."""
        from yuki.ui.own_window import open_page

        page = self._need_str(tool_input, "page")
        ok, report = open_page(page)
        return ToolOutcome(name="open_yuki", ok=ok, summary=report, content=[{"type": "text", "text": report}],
                           payload={"page": page, "opened": ok})

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

    def _do_tasks(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """N10: the request's kept list (the loop holds it; see :data:`TASKS_TOOL`)."""
        raw = tool_input.get("items")
        if not isinstance(raw, list) or not raw:
            raise ToolError("give items: the list, each with what and state (todo, doing, done or dropped)")
        items = []
        for item in raw[:12]:
            if not isinstance(item, dict) or not str(item.get("what") or "").strip():
                raise ToolError("each item needs what (a few words) and state")
            state = str(item.get("state") or "todo")
            if state not in ("todo", "doing", "done", "dropped"):
                raise ToolError(f"state must be todo, doing, done or dropped, got {state!r}")
            by = " ".join(str(item.get("by") or "").split())[:200]
            if state == "dropped" and not by:
                raise ToolError(f"\"{item['what']}\" is dropped only on their words: give by, their words that "
                                f"dropped it, exactly; otherwise keep it on the list")
            items.append({"what": " ".join(str(item["what"]).split())[:120], "state": state, **({"by": by} if by
                                                                                               else {})})
        left = [i["what"] for i in items if i["state"] in ("todo", "doing")]
        text = ("Kept. Still to do, in order: " + "; ".join(left) + ".") if left else "Kept: nothing left to do."
        return ToolOutcome(name="tasks", ok=True, summary=text, kind="tasks", control_input={"items": items},
                           content=[{"type": "text", "text": text}], payload={"items": items})

    def _do_say(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = self._need_str(tool_input, "text")
        return ToolOutcome(
            name="say",
            ok=True,
            summary=text,
            kind="say",
            control_input={"text": text},
            content=[{"type": "text", "text": "Said. Keep going."}],
            payload={"text": text},
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


def html_text(markup: str) -> tuple[str, str]:
    """``(title, readable text)`` of an HTML document: text nodes outside script/style, blocks on new lines.

    Parsing a fixed machine format (HTML), not judging content.
    """
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        SKIP = {"script", "style", "noscript", "template", "svg", "head"}
        BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
                 "header", "footer", "pre", "blockquote", "table", "ul", "ol"}

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.title = ""
            self._skip = 0
            self._in_title = False

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag == "title":
                self._in_title = True
            elif tag in self.SKIP:
                self._skip += 1
            elif tag in self.BLOCK:
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag == "title":
                self._in_title = False
            elif tag in self.SKIP and self._skip:
                self._skip -= 1
            elif tag in self.BLOCK:
                self.parts.append("\n")

        def handle_data(self, data: str) -> None:
            if self._in_title:
                self.title += data
            elif not self._skip:
                self.parts.append(data)

    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page still gives what was read
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split()), text


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


def _window_rect_now(hwnd: int) -> list[int] | None:
    """``GetWindowRect`` of a live window, ``None`` when it is not one (a test double's fake handle)."""
    try:
        import win32gui

        if not win32gui.IsWindow(int(hwnd)):
            return None
        return list(win32gui.GetWindowRect(int(hwnd)))
    except Exception:
        return None


def _is_own_window(hwnd: Any) -> bool:
    """Whether a window belongs to Yuki itself (this process, or a program in Yuki's own folder)."""
    try:
        hwnd = int(hwnd or 0)
        if not hwnd:
            return False
        import os
        import sys
        from pathlib import Path

        import psutil
        import win32process

        pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
        if pid == os.getpid():
            return True
        from yuki.core.paths import app_dir

        exe = Path(psutil.Process(pid).exe()).resolve()
        return any(exe.is_relative_to(root) for root in (app_dir().resolve(), Path(sys.prefix).resolve()))
    except Exception:
        return False


def _window_title_now(hwnd: int) -> str | None:
    try:
        import win32gui

        return win32gui.GetWindowText(int(hwnd)) if win32gui.IsWindow(int(hwnd)) else None
    except Exception:
        return None


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
