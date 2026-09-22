"""UI Automation tree for a single window.

Reading UIA properties one at a time is a cross-process COM call each
(~3 ms/element for the dozen properties we need - a Chrome window measured
~1 s for 300 elements).  Instead we ask UIA for a **cached subtree**
(``BuildUpdatedCache`` with ``TreeScope_Subtree``): one cross-process call per
top-level child, after which every property read is in-process
(~0.2 ms/element for 22 properties).

The walk runs in a worker thread that initialises COM for itself, so a hung or
pathologically large provider can be abandoned: the caller gives up at
``timeout_s`` and returns whatever the worker had already collected, marked
``truncated``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import comtypes
import comtypes.client
import win32gui

from yuki.perception.windows import window_info

# ---------------------------------------------------------------------------
# UIA property ids we cache.  Names come from IUIAutomation's propid list.
# ---------------------------------------------------------------------------
_P_CONTROL_TYPE = 30003
_P_NAME = 30005
_P_BOUNDING_RECT = 30001
_P_IS_OFFSCREEN = 30022
_P_IS_ENABLED = 30010
_P_IS_KEYBOARD_FOCUSABLE = 30009
_P_HAS_KEYBOARD_FOCUS = 30008
_P_ACCELERATOR_KEY = 30006
_P_ACCESS_KEY = 30007
_P_VALUE_VALUE = 30045
_P_VALUE_IS_READONLY = 30046
_P_SCROLL_VERTICALLY_SCROLLABLE = 30058
_P_SCROLL_HORIZONTALLY_SCROLLABLE = 30057
_P_IS_INVOKE_AVAILABLE = 30031
_P_IS_TOGGLE_AVAILABLE = 30041
_P_IS_SELECTION_ITEM_AVAILABLE = 30036
_P_IS_EXPAND_COLLAPSE_AVAILABLE = 30028
_P_IS_VALUE_AVAILABLE = 30043
_P_IS_SCROLL_AVAILABLE = 30034
_P_LEGACY_DEFAULT_ACTION = 30100

_CACHED_PROPERTIES = (
    _P_CONTROL_TYPE,
    _P_NAME,
    _P_BOUNDING_RECT,
    _P_IS_OFFSCREEN,
    _P_IS_ENABLED,
    _P_IS_KEYBOARD_FOCUSABLE,
    _P_HAS_KEYBOARD_FOCUS,
    _P_ACCELERATOR_KEY,
    _P_ACCESS_KEY,
    _P_VALUE_VALUE,
    _P_VALUE_IS_READONLY,
    _P_SCROLL_VERTICALLY_SCROLLABLE,
    _P_SCROLL_HORIZONTALLY_SCROLLABLE,
    _P_IS_INVOKE_AVAILABLE,
    _P_IS_TOGGLE_AVAILABLE,
    _P_IS_SELECTION_ITEM_AVAILABLE,
    _P_IS_EXPAND_COLLAPSE_AVAILABLE,
    _P_IS_VALUE_AVAILABLE,
    _P_IS_SCROLL_AVAILABLE,
    _P_LEGACY_DEFAULT_ACTION,
)

_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_SUBTREE = 7
_AUTOMATION_ELEMENT_MODE_NONE = 0  # cached properties only: fastest

_CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

#: UIA ControlType id -> role name, per the documented control-type ids.  Kept
#: local so the tree walk needs nothing but comtypes.
_ROLE_NAMES = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

#: Text fields can hold megabytes (a Notepad document exposes its whole buffer
#: through ValuePattern).  Keep the snapshot - and therefore the log and the
#: model context - bounded.
_MAX_VALUE_CHARS = 500

#: Defensive cap: a cyclic or absurdly deep provider must not spin forever.
_MAX_DEPTH = 60


@dataclass
class UIElement:
    """One UIA element, flattened for the model."""

    id: int
    role: str
    name: str
    value: str | None
    center: tuple[int, int]
    bounds: tuple[int, int, int, int]
    is_interactive: bool
    is_scrollable: bool
    is_focused: bool
    shortcut: str | None
    depth: int


@dataclass
class WindowTree:
    """Depth-first snapshot of one window's UIA subtree."""

    hwnd: int
    title: str
    process_name: str
    elements: list[UIElement] = field(default_factory=list)
    truncated: bool = False
    captured_at: float = 0.0
    elapsed_ms: float = 0.0


_uia_module_lock = threading.Lock()
_uia_module: object | None = None


def _uia_core() -> object:
    """The comtypes wrapper for UIAutomationCore.dll (generated once).

    Generating the wrapper is slow the first time, so it is done at import on
    the main thread; worker threads reuse the cached module object.
    """
    global _uia_module
    with _uia_module_lock:
        if _uia_module is None:
            _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
        return _uia_module


def _role_name(control_type: object) -> str:
    """UIA ControlType id -> short role name ("Button", "Edit", "TabItem")."""
    if isinstance(control_type, int) and control_type in _ROLE_NAMES:
        return _ROLE_NAMES[control_type]
    return f"ControlType{control_type}"


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value: object) -> bool:
    return bool(value) if value is not None else False


def _rect(value: object) -> tuple[int, int, int, int] | None:
    """Convert a cached BoundingRectangle variant to (l, t, r, b).

    The raw UIA property is (left, top, **width**, **height**) - unlike
    ``CurrentBoundingRectangle``, which is a RECT.  Returns ``None`` for empty
    rectangles.
    """
    if value is None:
        return None
    try:
        left, top, width, height = (int(v) for v in value)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


class _CachedWalker:
    """Collects :class:`UIElement`s from cached UIA subtrees."""

    def __init__(self, max_elements: int, deadline: float) -> None:
        self.max_elements = max_elements
        self.deadline = deadline
        self.elements: list[UIElement] = []
        self.truncated = False

    # -- element decoding ---------------------------------------------------
    def _add(self, element: object, depth: int) -> None:
        get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
        bounds = _rect(get(_P_BOUNDING_RECT))
        if bounds is None:
            return
        name = _as_text(get(_P_NAME))
        enabled = _as_bool(get(_P_IS_ENABLED))
        value_available = _as_bool(get(_P_IS_VALUE_AVAILABLE))
        value: str | None = None
        if value_available:
            raw = get(_P_VALUE_VALUE)
            text = raw if isinstance(raw, str) else ""
            if text:
                value = (
                    text[:_MAX_VALUE_CHARS] + "…"
                    if len(text) > _MAX_VALUE_CHARS
                    else text
                )
        editable = value_available and not _as_bool(get(_P_VALUE_IS_READONLY))
        is_interactive = enabled and (
            _as_bool(get(_P_IS_INVOKE_AVAILABLE))
            or _as_bool(get(_P_IS_TOGGLE_AVAILABLE))
            or _as_bool(get(_P_IS_SELECTION_ITEM_AVAILABLE))
            or _as_bool(get(_P_IS_EXPAND_COLLAPSE_AVAILABLE))
            or editable
            or _as_bool(get(_P_IS_KEYBOARD_FOCUSABLE))
            or bool(_as_text(get(_P_LEGACY_DEFAULT_ACTION)))
        )
        is_scrollable = _as_bool(get(_P_IS_SCROLL_AVAILABLE)) and (
            _as_bool(get(_P_SCROLL_VERTICALLY_SCROLLABLE))
            or _as_bool(get(_P_SCROLL_HORIZONTALLY_SCROLLABLE))
        )
        if not name and value is None and not is_interactive and not is_scrollable:
            # Structural padding: no name, nothing to read, nothing to do.
            return
        shortcut = _as_text(get(_P_ACCELERATOR_KEY)) or _as_text(get(_P_ACCESS_KEY))
        left, top, right, bottom = bounds
        self.elements.append(
            UIElement(
                id=len(self.elements),
                role=_role_name(get(_P_CONTROL_TYPE)),
                name=name,
                value=value,
                center=((left + right) // 2, (top + bottom) // 2),
                bounds=bounds,
                is_interactive=is_interactive,
                is_scrollable=is_scrollable,
                is_focused=_as_bool(get(_P_HAS_KEYBOARD_FOCUS)),
                shortcut=shortcut or None,
                depth=depth,
            )
        )

    # -- traversal ----------------------------------------------------------
    @staticmethod
    def _cached_children(element: object) -> list[object]:
        """Cached children of an element (empty for leaves).

        UIA hands back a NULL array for leaf elements, which comtypes turns
        into a pointer that raises on access.
        """
        try:
            array = element.GetCachedChildren()  # type: ignore[attr-defined]
        except Exception:
            return []
        if not array:
            return []
        try:
            count = array.Length
        except Exception:
            return []
        children = []
        for index in range(count):
            try:
                children.append(array.GetElement(index))
            except Exception:
                break
        return children

    def walk(self, element: object, depth: int) -> None:
        """Depth-first walk of a cached subtree, honouring cap and deadline."""
        if self.out_of_budget():
            return
        if depth > _MAX_DEPTH:
            self.truncated = True
            return
        offscreen = _as_bool(
            element.GetCachedPropertyValue(_P_IS_OFFSCREEN)  # type: ignore[attr-defined]
        )
        if offscreen:
            # Scrolled-out or hidden: the whole subtree is invisible to the
            # user, and in Chromium that subtree can be thousands of nodes.
            return
        self._add(element, depth)
        for child in self._cached_children(element):
            if self.out_of_budget():
                return
            self.walk(child, depth + 1)

    def out_of_budget(self) -> bool:
        if len(self.elements) >= self.max_elements:
            self.truncated = True
            return True
        if time.monotonic() >= self.deadline:
            self.truncated = True
            return True
        return False


def _build_cache_request(automation: object, scope: int) -> object:
    request = automation.CreateCacheRequest()  # type: ignore[attr-defined]
    for prop in _CACHED_PROPERTIES:
        request.AddProperty(prop)
    request.TreeScope = scope
    request.TreeFilter = automation.ControlViewCondition  # type: ignore[attr-defined]
    request.AutomationElementMode = _AUTOMATION_ELEMENT_MODE_NONE
    return request


def _walk_window(hwnd: int, max_elements: int, deadline: float) -> _CachedWalker:
    """Collect the window's elements.  Runs on the worker thread."""
    # A fresh IUIAutomation for this thread's apartment: COM interface pointers
    # cannot be shared across apartments, and an abandoned thread must not
    # leave a poisoned shared client behind.
    module = _uia_core()
    automation = comtypes.client.CreateObject(
        _CUIAUTOMATION_CLSID, interface=module.IUIAutomation
    )
    walker = _CachedWalker(max_elements=max_elements, deadline=deadline)

    # A provider that is busy (or was just left mid-walk by an abandoned call)
    # can answer ElementFromHandle with E_FAIL for a moment; retry inside the
    # deadline before giving up.
    live_root = None
    last_error: Exception | None = None
    while live_root is None:
        try:
            live_root = automation.ElementFromHandle(hwnd)
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            if time.monotonic() + 0.05 >= deadline:
                raise
            time.sleep(0.04)
    del last_error

    # The root element itself, then each of its top-level children as a
    # separate cached subtree.  Per-child builds mean a timeout still leaves us
    # with the children that finished, instead of nothing.
    root_request = _build_cache_request(automation, _TREE_SCOPE_ELEMENT)
    walker._add(live_root.BuildUpdatedCache(root_request), 0)

    subtree_request = _build_cache_request(automation, _TREE_SCOPE_SUBTREE)
    tree_walker = automation.ControlViewWalker
    child = tree_walker.GetFirstChildElement(live_root)
    while child:
        if walker.out_of_budget():
            break
        try:
            walker.walk(child.BuildUpdatedCache(subtree_request), 1)
        except Exception:
            walker.truncated = True
        try:
            child = tree_walker.GetNextSiblingElement(child)
        except Exception:
            break
    return walker


def get_window_tree(
    hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0
) -> WindowTree:
    """Snapshot the UIA subtree of one window.

    Args:
        hwnd: top-level window handle.
        max_elements: hard cap on reported elements; hitting it sets
            ``truncated``.
        timeout_s: wall-clock budget.  The UIA walk happens on a worker thread
            with its own COM apartment; if it overruns, the partial result is
            returned with ``truncated`` set and the thread is abandoned.

    Raises:
        ValueError: the handle is not a window.
        RuntimeError: UIA failed before a single element was collected.
    """
    if not win32gui.IsWindow(hwnd):
        raise ValueError(f"not a window: hwnd={hwnd}")
    info = window_info(hwnd)
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_s

    result: dict[str, object] = {}

    def _worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass  # already initialised for this thread
        try:
            result["walker"] = _walk_window(hwnd, max_elements, deadline)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            result["error"] = exc
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(
        target=_worker, name=f"yuki-uia-{hwnd}", daemon=True
    )
    thread.start()
    thread.join(timeout_s)

    walker = result.get("walker")
    if walker is None:
        error = result.get("error")
        if error is not None:
            raise RuntimeError(f"UIA walk of hwnd={hwnd} failed: {error}") from error
        # Still running: abandon the thread and report an empty, truncated tree.
        return WindowTree(
            hwnd=hwnd,
            title=info.title if info else "",
            process_name=info.process_name if info else "",
            elements=[],
            truncated=True,
            captured_at=time.time(),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
    assert isinstance(walker, _CachedWalker)
    return WindowTree(
        hwnd=hwnd,
        title=info.title if info else "",
        process_name=info.process_name if info else "",
        elements=walker.elements,
        truncated=walker.truncated,
        captured_at=time.time(),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


try:  # pay the one-off wrapper-generation cost on the importing thread
    _uia_core()
except Exception:  # pragma: no cover - retried lazily inside the worker
    pass


_FORMAT_NAME_CHARS = 90
_FORMAT_VALUE_CHARS = 160


def _one_line(text: str, limit: int) -> str:
    """Flatten newlines/tabs and clip, so one element stays one line."""
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    flat = flat.replace("\t", " ")
    return flat[:limit] + "…" if len(flat) > limit else flat


def format_window_tree(tree: WindowTree) -> str:
    """Compact text rendering of a window tree, one line per element.

    Elements with no name, no value and no interactivity are skipped - they are
    layout scaffolding the model cannot use.  Indentation carries ``depth``.
    """
    header = (
        f"window {tree.hwnd} \"{tree.title}\" ({tree.process_name or 'unknown'}) - "
        f"{len(tree.elements)} element(s), {tree.elapsed_ms:.0f} ms"
    )
    if tree.truncated:
        header += " [TRUNCATED: partial tree, consider a screenshot]"
    lines = [header]
    shown = 0
    for element in tree.elements:
        if not element.name and element.value is None and not element.is_interactive:
            continue
        shown += 1
        indent = " " * min(element.depth, 12)
        parts = [f"{indent}[{element.id}] {element.role}"]
        if element.name:
            parts.append(f'"{_one_line(element.name, _FORMAT_NAME_CHARS)}"')
        if element.value is not None:
            parts.append(f'value="{_one_line(element.value, _FORMAT_VALUE_CHARS)}"')
        parts.append(f"@({element.center[0]},{element.center[1]})")
        if element.shortcut:
            parts.append(f"[kb: {element.shortcut}]")
        flags = []
        if element.is_focused:
            flags.append("focused")
        if element.is_scrollable:
            flags.append("scrollable")
        if flags:
            parts.append(f"[{' '.join(flags)}]")
        lines.append(" ".join(parts))
    if shown == 0:
        lines.append("(no named or interactive elements - UIA exposes nothing usable here)")
    return "\n".join(lines)
