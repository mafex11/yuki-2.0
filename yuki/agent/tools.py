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
    {"look_at_desktop", "look_at_window", "take_screenshot", "system_facts"}
)

#: Tools handled by the agent loop rather than a backend function.
CONTROL_TOOLS: frozenset[str] = frozenset({"ask_user", "done", "note_to_self"})


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

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "look_at_desktop",
        "label": "Looking at the desktop",
        "description": (
            "List every visible top-level window with its hwnd, title, process, "
            "bounds and which one is in the foreground, plus cursor position and "
            "screen size. Cheap and fast. You already receive this at the start of "
            "every turn, so call it only to re-check after you have acted."
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
            "result reports how many elements it found and whether it was truncated; "
            "some windows (especially browser-based apps) expose little or nothing, "
            "in which case take a screenshot instead."
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
            "Capture pixels: one window if you pass an hwnd, otherwise the whole "
            "screen. Downscaled. Use it when the element tree is empty, truncated or "
            "does not explain what you are looking at, or when you need to read "
            "something only rendering shows. A window's image is relative to that "
            "window and scaled down, so the result tells you the rectangle it covers "
            "and the arithmetic for turning image pixels into screen coordinates."
        ),
        "input_schema": _obj(
            {
                "hwnd": {
                    "type": "integer",
                    "description": "Window to capture; omit for the full screen.",
                }
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
            "so you can pick one or ask the user."
        ),
        "input_schema": _obj(
            {"query": {"type": "string", "description": "Application name, e.g. 'spotify'."}},
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
        "description": "Click at a screen coordinate, usually an element's centre point.",
        "input_schema": _obj(
            {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "description": "2 for a double click."},
                "expect_hwnd": _GUARD,
            },
            ["x", "y"],
        ),
    },
    {
        "name": "type_text",
        "label": "Typing",
        "description": (
            "Type text into whatever currently has keyboard focus, optionally pressing "
            "Enter afterwards. Make sure the right field is focused first."
        ),
        "input_schema": _obj(
            {
                "text": {"type": "string"},
                "press_enter": {"type": "boolean"},
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
            "['ctrl','t'] or ['win','r'] or ['volume_mute']."
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
        "description": "Press a single key, optionally several times, e.g. 'enter', 'tab', 'down'.",
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
        "description": "Scroll at a point by wheel notches; positive dy scrolls up.",
        "input_schema": _obj(
            {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "dy": {"type": "integer"},
                "dx": {"type": "integer"},
                "expect_hwnd": _GUARD,
            },
            ["x", "y"],
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
        "description": "Open a URL in the default browser.",
        "input_schema": _obj({"url": {"type": "string"}}, ["url"]),
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
        "input_schema": _obj({"message": {"type": "string"}}, ["message"]),
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
    *, cacheable: bool = True, names: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Return the tool definitions for a request.

    Args:
        cacheable: Mark the last tool definition with ``cache_control`` so the
            whole (byte-stable) tool block is cached by the API.
        names: Restrict the list to these tools (plus the control tools), as
            resolved by :func:`resolve_tool_names`. ``None`` sends all of them.

    Returns:
        A fresh list of tool dicts, carrying only what the API accepts -- the
        human ``label`` is dropped here, since it exists for the UI and an
        unexpected key would be rejected. The caller may not mutate the module
        copy.
    """
    allowed = resolve_tool_names(names)
    tools = [
        {k: v for k, v in t.items() if k != "label"}
        for t in TOOL_SCHEMAS
        if allowed is None or t["name"] in allowed
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
    def screenshot(self, hwnd: int | None = None, *, max_width: int = 1280) -> bytes: ...
    def capture_bounds(self, hwnd: int | None) -> tuple[int, int, int, int]: ...
    def system_facts(self) -> dict[str, Any]: ...
    def launch_app(self, query: str, *, timeout_s: float = 8.0) -> Any: ...
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
        self, text: str, *, press_enter: bool = False, expect_hwnd: int | None = None
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
    def open_url(self, url: str) -> Any: ...

    # Optional, and not part of the architecture contract: a backend without it
    # simply never gets pre-warmed. See :meth:`Dispatcher.prewarm_shell`.
    def prewarm_shell(self) -> bool: ...  # pragma: no cover - protocol only


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
        "screenshot",
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


class ToolError(Exception):
    """Raised inside a handler to report a clean, model-readable failure."""


class Dispatcher:
    """Maps ``tool_use`` blocks onto backend calls.

    Args:
        backend: Object providing the contract functions. Defaults to the real
            desktop backend.
        screenshot_policy: ``never`` refuses screenshots outright, ``ask`` returns
            a failure telling the model to ask the user first, ``auto`` allows
            them. This is the consent gate, not behaviour steering.
        tool_timeout_s: Timeout handed to :func:`run_powershell`.
        max_tree_elements: Cap for :func:`get_window_tree`.
    """

    def __init__(
        self,
        backend: Backend | None = None,
        *,
        screenshot_policy: str = "auto",
        tool_timeout_s: float = 20.0,
        max_tree_elements: int = 400,
    ) -> None:
        self.backend = backend if backend is not None else default_backend()
        self.screenshot_policy = screenshot_policy
        self.tool_timeout_s = tool_timeout_s
        self.max_tree_elements = max_tree_elements

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
        tree = self.backend.get_window_tree(hwnd, max_elements=self.max_tree_elements)
        text = self.backend.format_window_tree(tree)
        payload = _plain(tree)
        elements = payload.get("elements") or [] if isinstance(payload, dict) else []
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        header = (
            f"window {hwnd} \"{payload.get('title', '')}\" ({payload.get('process_name', '')}): "
            f"{len(elements)} elements{', truncated' if truncated else ''}"
        )
        return ToolOutcome(
            name="look_at_window",
            ok=True,
            summary=f"{len(elements)} elements{' (truncated)' if truncated else ''}",
            content=[{"type": "text", "text": f"{header}\n{text}"}],
            payload=payload,
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
        started = time.perf_counter()
        png = self.backend.screenshot(hwnd)
        if not isinstance(png, (bytes, bytearray)):
            raise ToolError(f"screenshot() returned {type(png).__name__}, expected PNG bytes")
        png = bytes(png)
        b64 = base64.standard_b64encode(png).decode("ascii")
        target = "the screen" if hwnd is None else f"window {hwnd}"
        geometry = self._capture_geometry(hwnd, png)
        return ToolOutcome(
            name="take_screenshot",
            ok=True,
            summary=f"{target}, {len(png)} bytes",
            content=[
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": f"Screenshot of {target}. {geometry['text']}"},
            ],
            payload={"target": target, "bytes": len(png), **geometry["payload"]},
            elapsed_ms=(time.perf_counter() - started) * 1000,
            screenshot_png=png,
            screenshot_b64=b64,
        )

    def _capture_geometry(self, hwnd: int | None, png: bytes) -> dict[str, Any]:
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
                left, top, right, bottom = reader(hwnd)
                bounds = (int(left), int(top), int(right), int(bottom))
            except Exception:
                bounds = None
        if size is None or bounds is None:
            return {"text": "", "payload": {"image_size": size, "capture_bounds": bounds}}
        width, height = size
        left, top, right, bottom = bounds
        source_width = max(1, right - left)
        scale = width / source_width
        text = (
            f"The image is {width}x{height} and covers the screen rectangle "
            f"({left},{top})-({right},{bottom}), which is {source_width}x{bottom - top} "
            f"real pixels, so it is at {scale:.3f} scale. To act on something you see "
            f"here: screen_x = {left} + image_x / {scale:.3f}, "
            f"screen_y = {top} + image_y / {scale:.3f}."
        )
        return {
            "text": text,
            "payload": {
                "image_size": [width, height],
                "capture_bounds": [left, top, right, bottom],
                "scale": round(scale, 4),
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

    def _do_launch_app(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "launch_app", self.backend.launch_app(self._need_str(tool_input, "query"))
        )

    def _do_focus_window(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "focus_window", self.backend.focus_window(self._need_int(tool_input, "hwnd"))
        )

    def _do_click(self, tool_input: dict[str, Any]) -> ToolOutcome:
        button = tool_input.get("button") or "left"
        if button not in {"left", "right", "middle"}:
            raise ToolError(f"button must be left, right or middle, got {button!r}")
        return self._from_action(
            "click",
            self.backend.click(
                self._need_int(tool_input, "x"),
                self._need_int(tool_input, "y"),
                button=button,
                clicks=self._opt_int(tool_input, "clicks", 1),
                expect_hwnd=self._opt_hwnd(tool_input),
            ),
        )

    def _do_type_text(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = tool_input.get("text")
        if not isinstance(text, str):
            raise ToolError(f"'text' must be a string, got {text!r}")
        return self._from_action(
            "type_text",
            self.backend.type_text(
                text,
                press_enter=bool(tool_input.get("press_enter")),
                expect_hwnd=self._opt_hwnd(tool_input),
            ),
        )

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
        return self._from_action(
            "scroll",
            self.backend.scroll(
                self._need_int(tool_input, "x"),
                self._need_int(tool_input, "y"),
                dy=self._opt_int(tool_input, "dy", 0),
                dx=self._opt_int(tool_input, "dx", 0),
                expect_hwnd=self._opt_hwnd(tool_input),
            ),
        )

    def _do_run_powershell(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action(
            "run_powershell",
            self.backend.run_powershell(
                self._need_str(tool_input, "command"), timeout_s=self.tool_timeout_s
            ),
        )

    def _do_open_url(self, tool_input: dict[str, Any]) -> ToolOutcome:
        return self._from_action("open_url", self.backend.open_url(self._need_str(tool_input, "url")))

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
