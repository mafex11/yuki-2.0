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

**Lazily built trees.**  Some providers do not have a tree until something asks
for one, and do not have it *immediately* even then.  A window whose content is
drawn by a child HWND answers a query on the top-level handle with its frame and
nothing else, and only starts building the real tree once the child window itself
has been asked (the child gets its own ``WM_GETOBJECT``).  Building is then
asynchronous: measured on this desktop, a freshly launched Spotify reported 7
elements for 2.9 s and then climbed 8 -> 24 -> 58 -> 311 over the next few
hundred milliseconds.  So a pass here is not just the top-level element's
subtree: it is that subtree *plus* the subtree of every visible descendant HWND
(``EnumChildWindows``), each spliced in right after the element that owns the
handle so depth-first order and depths hold (handles the walk never meets are
appended at depth 1), de-duplicated by UIA RuntimeId or, when a provider gives
none, by role + name + bounds.  Element ids are list positions within the one
pass that is returned, so they are stable within a snapshot.  When a pass comes
back thin, the walk is repeated until the count has grown and stopped growing,
it has failed to grow within a short grace, or the budget runs out.  This is a condition poll,
not a settle sleep, and it is keyed on window handles and element counts only:
nothing here knows the name of an application.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import comtypes
import comtypes.client
import psutil
import win32gui

from yuki.perception.windows import window_info

# ---------------------------------------------------------------------------
# UIA property ids we cache.  Names come from IUIAutomation's propid list.
# ---------------------------------------------------------------------------
_P_RUNTIME_ID = 30000
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
_P_IS_TEXT_AVAILABLE = 30040
_P_NATIVE_WINDOW_HANDLE = 30020

_CACHED_PROPERTIES = (
    _P_RUNTIME_ID,
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
    _P_NATIVE_WINDOW_HANDLE,
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

#: Ceiling on how many descendant HWNDs one pass will ask UIA about.  Chrome with
#: six tabs exposes eight; the cap only exists so a window that has spawned
#: hundreds of child controls cannot turn one walk into hundreds of cross-process
#: calls.
_MAX_CHILD_WINDOWS = 96

#: Below this many elements a window has told us almost nothing, so it is worth
#: giving its provider a moment to finish building (see :func:`_walk_window`).
#: Measured on this desktop: a Chromium window that has not built its tree yet
#: reports 6-8 elements, while the smallest *real* content tree seen was 28
#: (Discord), then 76 (Settings), 149 (Chrome) and 311 (Spotify).  A count of UIA
#: nodes is a fact about a snapshot, not about an application.
_THIN_TREE_ELEMENTS = 16

#: Gap between re-walks of a thin tree.  The walk itself costs tens of
#: milliseconds, so this only keeps a tight loop from spinning on a provider that
#: is busy building.
_WAKE_POLL_S = 0.05

#: How long a thin window is watched for a descendant HWND to appear before it is
#: accepted as having no separate content surface at all.  This is the timeout on
#: a condition ("a child window showed up"), not a settle sleep: a window that has
#: one already never waits, and a window that grows a tree never stops at it.
#: Measured on this desktop: Spotify's first descendant HWND exists 227 ms after
#: its top-level window does and its render-widget window 977 ms after, while
#: warp's GPU-drawn terminal has none and never will.  Without this bound, a
#: window that genuinely exposes four elements would spend the whole budget being
#: re-walked in the hope of a fifth.
_WAKE_GRACE_S = 0.4

#: How long the element count has to hold still before a tree that was being built
#: is called done.  A lazily built tree does not arrive in one piece: a cold
#: Spotify, sampled every 50 ms from the moment its window appeared, went
#: 7 -> 8 (1.6 s) -> 9 (2.5 s) -> 25 (2.9 s) -> 26 -> 85 -> 158 -> 161 (4.9 s) ->
#: 311 (6.3 s) -> 312 (7.5 s), with 0.4-0.6 s between most steps.  Stopping at the
#: first pass that did not grow would have reported 25 elements as if that were
#: the window; waiting for the count to stand still this long gets past the
#: middle of that climb without spending seven seconds on it, and any tree that
#: took more than one pass says so (see :func:`format_window_tree`) so the reader
#: knows a second look may show more.
_SETTLE_S = 0.6

#: A process younger than this is treated as possibly still building its first
#: tree: a thin, non-growing tree is then watched until it grows or the budget
#: runs out, instead of being accepted after :data:`_WAKE_GRACE_S`.  The cold
#: Spotify climb above sat at 7-9 elements for 2.9 s before it grew, so an old
#: window (a small dialog: a handful of elements, several child HWNDs, never
#: growing) is answered in half a second, while a window launched moments ago
#: gets the patience it needs.  Process age is a fact about the process, not
#: about which application it is.
_YOUNG_PROCESS_S = 10.0

#: Slice of ``timeout_s`` reserved for handing the result back: the worker aims
#: to finish this much before the caller stops waiting, so a walk that runs to
#: the deadline returns its partial tree instead of nothing.
_HANDOFF_S = 0.15


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
    """Depth-first snapshot of one window's UIA subtree.

    ``passes``, ``child_windows`` and ``first_pass_elements`` are diagnostics for
    the lazily-built-tree problem: how many walks were made, how many descendant
    HWNDs were walked alongside the top-level one, and how many elements the first
    walk found (``-1`` when unknown).  A tree that grew after its first pass was
    still being built when we first looked, which is worth having in the log next
    to the element count.
    """

    hwnd: int
    title: str
    process_name: str
    elements: list[UIElement] = field(default_factory=list)
    truncated: bool = False
    captured_at: float = 0.0
    elapsed_ms: float = 0.0
    passes: int = 1
    child_windows: int = 0
    first_pass_elements: int = -1


#: Roles whose whole purpose is to hold text the user types.  A control with a
#: Value or Text pattern is the general answer; these three cover the ones that
#: take typing without advertising a pattern (a Chromium search box is a
#: ComboBox with neither).  UIA control types are structure, not application
#: names - nothing here knows what program it is looking at.
_TEXT_INPUT_ROLES = frozenset({"Edit", "ComboBox", "Document"})


@dataclass
class FocusInfo:
    """What has keyboard focus right now, as far as UIA will say.

    Attributes:
        role: UIA ControlType name of the focused element, or ``""``.
        name: its UIA Name, or ``""``.
        value: its ValuePattern value, clipped, or ``None``.
        hwnd: the focused element's own window handle when it has one (a
            Chromium control has none: the whole page lives in one HWND).
        accepts_text: whether typing now would go into something that holds text -
            it has a Value or Text pattern, or it is an Edit/ComboBox/Document.
        shortcut: AcceleratorKey/AccessKey if exposed.
        ok: False when UIA would not answer at all, in which case every other
            field is empty and the caller should say "unknown" rather than "none".
    """

    role: str = ""
    name: str = ""
    value: str | None = None
    hwnd: int = 0
    accepts_text: bool = False
    shortcut: str | None = None
    ok: bool = False

    def describe(self) -> str:
        """One short phrase: ``Edit "What do you want to play?"``."""
        if not self.ok:
            return "unknown"
        if not self.role and not self.name:
            return "nothing"
        parts = [self.role or "element"]
        if self.name:
            parts.append(f'"{_one_line(self.name, 60)}"')
        if self.accepts_text:
            parts.append("(takes text)")
        return " ".join(parts)

    def same_as(self, other: "FocusInfo | None") -> bool:
        """Whether this is focus on the same thing as ``other``.

        Compared on what a reader would compare: the control's role, its text and
        its window handle.  Deliberately not on RuntimeId - the point is to answer
        "did my click move the focus", and a provider that rebuilds an equivalent
        element in place has not moved it.
        """
        if other is None or not (self.ok and other.ok):
            return False
        return (self.role, self.name, self.hwnd) == (other.role, other.name, other.hwnd)


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


def child_window_handles(hwnd: int, *, limit: int = _MAX_CHILD_WINDOWS) -> list[int]:
    """Every visible descendant HWND of ``hwnd``, outermost first.

    These are the handles a lazily-built provider is waiting to be asked about.
    ``EnumChildWindows`` walks the whole descendant chain in z-order, which is
    also the order the user sees them stacked, so the list needs no sorting.

    Invisible children are dropped: the user cannot see them, their UIA elements
    come back off-screen and would be skipped by the walk anyway, and a single
    Chromium window can carry several of them.

    Args:
        hwnd: window whose descendants to list.
        limit: stop after this many handles.

    Returns:
        Handles, possibly empty.  A window that has died mid-enumeration yields
        whatever was collected before it went.
    """
    handles: list[int] = []

    def _collect(child: int, _: object) -> bool:
        if len(handles) >= limit:
            return False
        if win32gui.IsWindowVisible(child):
            handles.append(int(child))
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _collect, None)
    except Exception:
        pass  # the window closed, or a child died mid-enumeration
    return handles


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
    """Collects :class:`UIElement`s from cached UIA subtrees.

    One walker collects a whole *pass*: the top-level element's subtree plus the
    subtree of every visible descendant HWND.  Those overlap - a child window's
    element is usually also reachable from the top-level one - so every element is
    checked against :attr:`seen` before it is kept, and the first sighting wins,
    which keeps the depth-first order of the walk that found it.
    """

    def __init__(self, max_elements: int, deadline: float) -> None:
        self.max_elements = max_elements
        self.deadline = deadline
        self.elements: list[UIElement] = []
        self.truncated = False
        self.seen: set[tuple] = set()
        self._shapes: set[tuple] = set()
        self.passes = 1
        self.child_windows = 0
        self.first_pass_elements = -1
        #: Called as ``on_window(native_hwnd, depth)`` after an element that owns
        #: a window handle has been walked, so the pass can splice that HWND's own
        #: subtree in right there - keeping depth-first order and real depths -
        #: instead of appending it after the whole top-level walk.
        self.on_window: object | None = None

    # -- identity -----------------------------------------------------------
    @staticmethod
    def _identity(
        get: object, role: str, name: str, bounds: tuple[int, int, int, int]
    ) -> tuple:
        """A key that is the same element twice and never two different ones.

        UIA's own RuntimeId is exactly this, when the provider supplies one; it is
        an array of ints unique to the element for as long as it lives.  Providers
        are allowed to leave it empty, so the fallback is the tuple a human would
        use to say "that is the same thing": same role, same text, same rectangle.
        """
        raw = get(_P_RUNTIME_ID)  # type: ignore[operator]
        if raw is not None:
            try:
                runtime_id = tuple(int(part) for part in raw)
            except Exception:
                runtime_id = ()
            if runtime_id:
                return ("rid", runtime_id)
        return ("shape", role, name, bounds)

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
        role = _role_name(get(_P_CONTROL_TYPE))
        identity = self._identity(get, role, name, bounds)
        shape = ("shape", role, name, bounds)
        # An element without a RuntimeId is also matched against the shape of
        # everything already kept (the same element reached through another
        # HWND's provider may come back without one).  Elements that do carry a
        # RuntimeId are matched on it alone, so two genuinely different controls
        # that happen to look identical are both kept.
        if identity in self.seen or (identity == shape and shape in self._shapes):
            return  # already collected, via the top-level walk or another child
        self.seen.add(identity)
        self._shapes.add(shape)
        shortcut = _as_text(get(_P_ACCELERATOR_KEY)) or _as_text(get(_P_ACCESS_KEY))
        left, top, right, bottom = bounds
        self.elements.append(
            UIElement(
                id=len(self.elements),
                role=role,
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
        if self.on_window is not None:
            try:
                native = int(
                    element.GetCachedPropertyValue(_P_NATIVE_WINDOW_HANDLE)  # type: ignore[attr-defined]
                    or 0
                )
            except Exception:
                native = 0
            if native:
                self.on_window(native, depth)  # type: ignore[operator]

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


#: How long one *descendant* HWND gets to answer ``ElementFromHandle`` before the
#: pass moves on without it.  The top-level window gets the whole budget; a child
#: that has closed, or whose provider is wedged, must not eat it.
_CHILD_PATIENCE_S = 0.15


def _element_from_handle(
    automation: object, hwnd: int, deadline: float
) -> object | None:
    """UIA element for a window handle, retried inside ``deadline``.

    A provider that is busy (or was just left mid-walk by an abandoned call) can
    answer ``ElementFromHandle`` with E_FAIL for a moment, so failure is retried
    while the window still exists - a condition poll, bounded by ``deadline``.
    ``None`` means it never answered, or the window is gone, and the caller should
    move on - which is normal for a descendant HWND that closed between being
    enumerated and being asked.
    """
    while True:
        try:
            return automation.ElementFromHandle(hwnd)  # type: ignore[attr-defined]
        except Exception:
            if time.monotonic() + 0.05 >= deadline or not win32gui.IsWindow(hwnd):
                return None
            time.sleep(0.04)


def _collect_pass(
    automation: object,
    hwnd: int,
    child_handles: list[int],
    max_elements: int,
    deadline: float,
) -> _CachedWalker:
    """One full pass: the window's own subtree plus every child HWND's subtree.

    The top-level element comes first (so the window frame keeps element id 0),
    then its UIA children one cached subtree at a time, then each descendant
    HWND.  Asking about a descendant HWND is the part that matters for a lazily
    built provider: it is a separate ``WM_GETOBJECT`` to the window that actually
    owns the content, and until something sends it there is no tree to walk.

    Elements the earlier walks already produced are dropped by the walker's own
    de-duplication, so a child HWND whose content is already reachable from the
    top-level element costs one cache build and adds nothing - measured at 6 ms
    for Spotify's render-widget window.

    Raises:
        RuntimeError: UIA never produced an element for ``hwnd`` itself.
    """
    walker = _CachedWalker(max_elements=max_elements, deadline=deadline)
    root_request = _build_cache_request(automation, _TREE_SCOPE_ELEMENT)
    subtree_request = _build_cache_request(automation, _TREE_SCOPE_SUBTREE)
    tree_walker = automation.ControlViewWalker  # type: ignore[attr-defined]
    # Descendant HWNDs not yet asked about, in EnumChildWindows order.  Each is
    # asked exactly once per pass: either spliced in where the walk meets the
    # element that owns it, or, if the walk never meets it, afterwards.
    pending: dict[int, None] = dict.fromkeys(child_handles)

    def walk_child_window(child_hwnd: int, depth: int) -> None:
        """Ask one descendant HWND for its own subtree and walk it at ``depth``.

        ``depth`` is the depth of the element that owns the handle, since the
        handle's root element *is* that element (and is dropped as a duplicate).
        """
        if child_hwnd not in pending or walker.out_of_budget():
            return
        del pending[child_hwnd]
        child_root = _element_from_handle(
            automation, child_hwnd, min(deadline, time.monotonic() + _CHILD_PATIENCE_S)
        )
        if child_root is None:
            return
        try:
            walker.walk(child_root.BuildUpdatedCache(subtree_request), depth)
        except Exception:
            walker.truncated = True

    walker.on_window = walk_child_window

    def walk_children_of(element: object, base_depth: int) -> None:
        """Each UIA child of ``element`` as its own cached subtree.

        Per-child cache builds mean a timeout still leaves us with the children
        that finished, instead of nothing.
        """
        try:
            child = tree_walker.GetFirstChildElement(element)
        except Exception:
            return
        while child:
            if walker.out_of_budget():
                return
            try:
                walker.walk(child.BuildUpdatedCache(subtree_request), base_depth + 1)
            except Exception:
                walker.truncated = True
            try:
                child = tree_walker.GetNextSiblingElement(child)
            except Exception:
                return

    live_root = _element_from_handle(automation, hwnd, deadline)
    if live_root is None:
        raise RuntimeError(f"UIA would not give an element for hwnd={hwnd}")
    try:
        walker._add(live_root.BuildUpdatedCache(root_request), 0)
    except Exception:
        walker.truncated = True
    walk_children_of(live_root, 0)

    # Descendant HWNDs the walk never met (their owning element was off-screen,
    # structural padding, or simply not exposed by the parent's provider).  Asking
    # them is the part that wakes a lazily built provider, so they are asked
    # regardless.  Depth 1 is the honest floor for what they contribute: deeper
    # than the frame, without pretending to know where in the frame's own tree
    # they would have hung.
    for child_hwnd in list(pending):
        if walker.out_of_budget():
            break
        walk_child_window(child_hwnd, 1)
    walker.on_window = None
    return walker


def _walk_window(
    hwnd: int,
    max_elements: int,
    deadline: float,
    *,
    young: bool = False,
    publish: object | None = None,
) -> _CachedWalker:
    """Collect the window's elements, waiting out a tree that is still building.

    Runs on the worker thread.  The first pass - the top-level subtree plus every
    descendant HWND's, which is itself what wakes a lazily built provider - is
    usually the whole story.  When it comes back thin (fewer than
    :data:`_THIN_TREE_ELEMENTS`, or no more elements than there are descendant
    HWNDs to host content), the pass is repeated as a condition poll with a
    deadline, never a settle sleep:

    * the count grew past the first pass and then stood still for
      :data:`_SETTLE_S` - the tree has populated; stop;
    * the count never exceeded the first pass within :data:`_WAKE_GRACE_S` -
      that is simply the truth about this window; stop.  (Skipped for a
      ``young`` process, whose first tree can take seconds to start growing: it
      is watched until it grows or ``deadline``, and while it is still below
      thin, a pause in growth does not count as settled.)
    * ``deadline``.

    Two facts drive that decision - an element count and a list of window
    handles - and nothing here knows the name of an application.

    Args:
        publish: called with every pass that becomes the best so far, so the
            caller can return it even if a later pass is abandoned at the
            deadline.
    """
    # A fresh IUIAutomation for this thread's apartment: COM interface pointers
    # cannot be shared across apartments, and an abandoned thread must not
    # leave a poisoned shared client behind.
    module = _uia_core()
    automation = comtypes.client.CreateObject(
        _CUIAUTOMATION_CLSID, interface=module.IUIAutomation
    )
    child_handles = child_window_handles(hwnd)
    passes = 1

    def adopt(walker: _CachedWalker) -> _CachedWalker:
        walker.passes = passes
        walker.child_windows = len(child_handles)
        walker.first_pass_elements = first_count
        if publish is not None:
            publish(walker)  # type: ignore[operator]
        return walker

    best = _collect_pass(automation, hwnd, child_handles, max_elements, deadline)
    first_count = len(best.elements)
    best = adopt(best)

    thin = first_count < _THIN_TREE_ELEMENTS or (
        bool(child_handles) and first_count <= len(child_handles)
    )
    if not thin or len(best.elements) >= max_elements or time.monotonic() >= deadline:
        return best

    poll_started = time.monotonic()
    grace_deadline = min(deadline, poll_started + _WAKE_GRACE_S)
    last_growth = poll_started
    while time.monotonic() < deadline:
        time.sleep(min(_WAKE_POLL_S, max(deadline - time.monotonic(), 0.0)))
        now = time.monotonic()
        if now >= deadline:
            break
        grown = len(best.elements) > first_count
        if not grown and not young and now >= grace_deadline:
            break  # woken and re-read: it did not grow, so this is the window
        child_handles = child_window_handles(hwnd)
        if not child_handles and not grown and now >= grace_deadline:
            break  # nothing to wake, and nothing arrived to wake
        try:
            attempt = _collect_pass(
                automation, hwnd, child_handles, max_elements, deadline
            )
        except Exception:
            break  # the window went away mid-poll: keep what we have
        passes += 1
        best.passes = passes
        if len(attempt.elements) > len(best.elements):
            best = adopt(attempt)
            last_growth = time.monotonic()
            if len(best.elements) >= max_elements:
                break  # hit the cap: more passes cannot show more
        elif (
            len(best.elements) > first_count
            and (not young or len(best.elements) >= _THIN_TREE_ELEMENTS)
            and time.monotonic() - last_growth >= _SETTLE_S
        ):
            break  # populated, and has stopped growing
    best.passes = passes
    return best


def get_window_tree(
    hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0
) -> WindowTree:
    """Snapshot the UIA subtree of one window.

    The snapshot is the union of the top-level element's subtree and the subtree
    of every visible descendant HWND, de-duplicated and depth-first.  If that
    comes back thin the walk is repeated until the element count stops growing or
    ``timeout_s`` runs out, because a Chromium/Electron/CEF window does not build
    its tree until its renderer window is asked and does not finish building it
    straight away.  ``passes`` says how many walks it took.

    Args:
        hwnd: top-level window handle.
        max_elements: hard cap on reported elements; hitting it sets
            ``truncated``.
        timeout_s: wall-clock budget for everything: the walk, and any re-walks
            spent waiting for a lazily built tree.  The UIA work happens on a
            worker thread with its own COM apartment; if it overruns, the partial
            result is returned with ``truncated`` set and the thread is abandoned.
            A window that has just been launched needs the best part of four
            seconds before its tree exists, so callers that have just opened
            something should raise this.

    Raises:
        ValueError: the handle is not a window.
        RuntimeError: UIA failed before a single element was collected.
    """
    if not win32gui.IsWindow(hwnd):
        raise ValueError(f"not a window: hwnd={hwnd}")
    info = window_info(hwnd)
    started = time.perf_counter()
    # The worker aims a little short of the caller's own wait so that a walk
    # which runs to the deadline still hands back what it collected.
    deadline = time.monotonic() + max(timeout_s - min(_HANDOFF_S, timeout_s * 0.1), 0.0)
    young = False
    if info is not None and info.pid:
        try:
            young = time.time() - psutil.Process(info.pid).create_time() < _YOUNG_PROCESS_S
        except Exception:
            young = False

    result: dict[str, object] = {}

    def _publish(walker: _CachedWalker) -> None:
        result["best"] = walker

    def _worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass  # already initialised for this thread
        try:
            result["walker"] = _walk_window(
                hwnd, max_elements, deadline, young=young, publish=_publish
            )
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
    if walker is None and result.get("best") is not None:
        # Overran mid-poll: the best complete pass so far is still a true answer.
        walker = result["best"]
        assert isinstance(walker, _CachedWalker)
        walker.truncated = True
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
            passes=0,
            child_windows=len(child_window_handles(hwnd)),
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
        passes=walker.passes,
        child_windows=walker.child_windows,
        first_pass_elements=walker.first_pass_elements,
    )


#: Budget for :func:`focused_element`.  It is a handful of cross-process property
#: reads on one element, measured in single-digit milliseconds; the timeout only
#: exists so a wedged provider cannot hold up the action that asked.
_FOCUS_TIMEOUT_S = 0.6


def _read_focus(automation: object) -> FocusInfo:
    """Describe the focused element.  Runs on the worker thread."""
    element = automation.GetFocusedElement()  # type: ignore[attr-defined]
    if not element:
        return FocusInfo(ok=True)
    request = _build_cache_request(automation, _TREE_SCOPE_ELEMENT)
    request.AddProperty(_P_IS_TEXT_AVAILABLE)
    cached = element.BuildUpdatedCache(request)
    get = cached.GetCachedPropertyValue
    role = _role_name(get(_P_CONTROL_TYPE))
    name = _as_text(get(_P_NAME))
    value: str | None = None
    if _as_bool(get(_P_IS_VALUE_AVAILABLE)):
        raw = get(_P_VALUE_VALUE)
        if isinstance(raw, str) and raw:
            value = raw[:_MAX_VALUE_CHARS] + "…" if len(raw) > _MAX_VALUE_CHARS else raw
    editable = _as_bool(get(_P_IS_VALUE_AVAILABLE)) and not _as_bool(
        get(_P_VALUE_IS_READONLY)
    )
    accepts_text = (
        editable or _as_bool(get(_P_IS_TEXT_AVAILABLE)) or role in _TEXT_INPUT_ROLES
    )
    try:
        hwnd = int(get(_P_NATIVE_WINDOW_HANDLE) or 0)
    except Exception:
        hwnd = 0
    return FocusInfo(
        role=role,
        name=name,
        value=value,
        hwnd=hwnd,
        accepts_text=accepts_text,
        shortcut=(_as_text(get(_P_ACCELERATOR_KEY)) or _as_text(get(_P_ACCESS_KEY)))
        or None,
        ok=True,
    )


def focused_element(*, timeout_s: float = _FOCUS_TIMEOUT_S) -> FocusInfo:
    """What UIA says has keyboard focus, anywhere on the desktop.

    ``GetFocusedElement`` is the only way to see *inside* a window that keeps its
    whole UI in one HWND: ``GetGUIThreadInfo`` can say that Spotify's render-widget
    window has focus, but only UIA can say that the thing focused within it is the
    search box.  That is the difference between "the click hit the window" and
    "the click hit what I aimed at".

    Runs on its own worker thread with its own COM apartment, like
    :func:`get_window_tree`, so a provider that will not answer costs
    ``timeout_s`` and not the calling thread.

    Returns:
        A :class:`FocusInfo`.  ``ok`` is False if UIA never answered; the function
        does not raise, because it is called to *describe* an action's outcome and
        must never be the reason that action reports failure.
    """
    result: dict[str, object] = {}

    def _worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass  # already initialised for this thread
        try:
            module = _uia_core()
            automation = comtypes.client.CreateObject(
                _CUIAUTOMATION_CLSID, interface=module.IUIAutomation
            )
            result["focus"] = _read_focus(automation)
        except BaseException:  # noqa: BLE001 - reported as ok=False
            pass
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name="yuki-uia-focus", daemon=True)
    thread.start()
    thread.join(timeout_s)
    focus = result.get("focus")
    return focus if isinstance(focus, FocusInfo) else FocusInfo()


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
    if tree.passes > 1 and 0 <= tree.first_pass_elements < len(tree.elements):
        # The tree was being built while it was read, which is worth saying: the
        # app had only just been opened or navigated, and a second look may show
        # more than this one did.
        header += (
            f" [built lazily: grew from {tree.first_pass_elements} over "
            f"{tree.passes} passes - read it again if something you expect is missing]"
        )
    if tree.truncated:
        header += " [TRUNCATED: partial tree]"
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
