"""UI Automation tree for a single window.

Reading UIA properties one at a time is a cross-process COM call each
(~3 ms/element for the dozen properties we need - a Chrome window measured
~1 s for 300 elements).  Instead every read is a **cache request**
(``BuildUpdatedCache``) that brings back a batch of elements with all the
properties we need in one cross-process call, after which every property read
is in-process.

**Viewport first.**  One cached subtree per top-level child is not enough: a
web page is one child holding the whole document, thousands of nodes, almost
all of them scrolled out of view, and a single ``TreeScope_Subtree`` request
for it has to visit every one of them before it returns anything.  On
2026-09-23 such a request on a browser window showing a YouTube channel had not
returned after 6 s, eleven times in a row, while the same content read through
the renderer's own window handle took 42 ms.  So a pass decides, element by
element, how much to ask for in one call (see :class:`_ViewportPass`):

* an element that lies wholly inside the visible part of the window, is not a
  scroll container and does not host a descendant HWND gets its whole subtree
  in one request, filtered to elements that are not off-screen;
* an element that reaches beyond the visible area, scrolls, or hosts a
  descendant window gets only its children in one request, so off-screen
  branches are dropped without ever being visited;
* an element that owns a descendant HWND is read through that HWND's own UIA
  root, which asks the provider behind the handle directly instead of routing
  every node through the parent's.

That is a few dozen calls per window whatever the size of the page behind it,
and each is bounded by what is on screen.

The walk runs in a worker thread that initialises COM for itself, so a hung or
pathologically large provider can be abandoned: the caller gives up at
``timeout_s`` and returns everything the worker had collected up to then, in
tree order, marked ``truncated`` - never an older, smaller pass in its place.

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

**Windows that do not answer.**  A provider that is busy (a page loading, a
renderer mid-layout) can leave UIA's calls blocked for seconds.  Waiting out the
whole budget for that and then reporting "0 elements, truncated" is
indistinguishable from a window that has nothing in it, so the walk is given
:data:`_BUSY_S` to produce *any* content; if it has not, and the window is on
screen and its process alive, the result comes back at once with
``status="busy"`` and a note saying so.  A window that does answer keeps the
full budget and the lazy-wake polling above; one that answered with a small tree
is ``status="ok"``, and one that answered with nothing is ``status="empty"``.

**A frame is not the content.**  A browser that has not built its page tree
yet still answers with its own frame - title bar, toolbar, a sidebar full of
tab names - which is far more than :data:`_THIN_TREE_ELEMENTS`.  So a pass is
also treated as thin when a large descendant HWND has no element inside it, or
when the elements found cover only a small part of the window
(:func:`_surface_gaps`): both are facts about rectangles, not about which
program drew them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import comtypes
import comtypes.client
import psutil
import win32con
import win32gui

from yuki.perception.windows import is_cloaked, virtual_screen_bounds, window_info

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
_P_IS_PASSWORD = 30019
_P_SELECTION_ITEM_IS_SELECTED = 30079
_P_TOGGLE_STATE = 30086
_P_EXPAND_COLLAPSE_STATE = 30070
_PATTERN_TEXT = 10014
_TEXT_ENDPOINT_START = 0
_TEXT_ENDPOINT_END = 1
_TEXT_UNIT_CHARACTER = 0

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
    _P_SELECTION_ITEM_IS_SELECTED,
    _P_TOGGLE_STATE,
    _P_EXPAND_COLLAPSE_STATE,
)

_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_DESCENDANTS = 4
_TREE_SCOPE_SUBTREE = 7
_AUTOMATION_ELEMENT_MODE_NONE = 0  # cached properties only: fastest
#: Cached properties *and* a live reference, so the element can be asked for
#: its own children in a later request.
_AUTOMATION_ELEMENT_MODE_FULL = 1

#: State values, per the documented ToggleState / ExpandCollapseState enums.
_TOGGLE_STATES = {0: "off", 1: "on", 2: "mixed"}
_EXPAND_STATES = {0: "collapsed", 1: "expanded", 2: "partly expanded"}  # 3 = leaf: no state

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

#: How long a walk gets to produce its first content before a window that is on
#: screen, with a live process, is reported as busy instead of being waited on.
#: "Content" means a finished pass or any element below the window's own root
#: element: the root alone says the handle exists, not that the provider behind
#: it is answering.  A window that answers at all does so in tens of
#: milliseconds (a cold Spotify's first thin pass included), while on 2026-09-23
#: a loading browser window gave nothing for three full 6 s budgets in a row and
#: 211 elements once it had settled - time the model spent waiting, not looking.
_BUSY_S = 1.5

#: A descendant HWND this large a share of the window's visible area is a
#: *surface*: if no element at all is found inside it, whatever it draws has not
#: been exposed yet (a Chromium renderer woken moments ago answers with an empty
#: root; measured on 2026-09-23, Discord's content window had no elements for
#: several seconds after the first query, then 1191).  Small children - a
#: caption button host, an input sink - are not held to that.
_SURFACE_MIN_SHARE = 0.10

#: Below this share of the visible window covered by leaf elements, a pass is
#: treated as thin even though it has plenty of elements.  Measured from the
#: 2026-09-23 logs on a 1920x1080 browser window: frame and sidebar only (27
#: elements, the page not yet exposed) covered 8%; the same window with its page
#: read covered 95%; Notepad and Spotify with content 95-100%.
_MIN_COVERAGE = 0.25

#: Grid cell (px) used to measure coverage: coarse enough to cost nothing for
#: 400 rectangles, fine enough that a sidebar is not mistaken for the page.
_COVERAGE_CELL = 32

#: Slack (px) when comparing an element's rectangle against the viewport or a
#: window rectangle - providers round differently from ``GetWindowRect``.
_EDGE_SLACK = 2

#: What ``WindowTree.status`` can say.  ``ok``: UIA answered (however small the
#: tree).  ``busy``: the window is on screen and its process alive, but nothing
#: came back in time.  ``empty``: UIA answered with nothing usable, or the window
#: is minimised, hidden or gone and nothing came back.
TREE_STATUSES = ("ok", "busy", "empty")


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
    #: Which action patterns the element supports, in :data:`_PATTERN_ORDER`:
    #: ``invoke`` (a click activates it), ``toggle``, ``select`` (SelectionItem:
    #: a click selects it, which is not the same as activating it), ``expand``
    #: (ExpandCollapse) and ``value`` (a writable Value).  Empty for elements
    #: that are not interactive.
    patterns: tuple[str, ...] = ()
    #: What the element's patterns say about its current state, for elements
    #: that expose one: ``selected`` (SelectionItem.IsSelected - e.g. the tab
    #: that is on screen), ``on``/``off``/``mixed`` (Toggle) and
    #: ``expanded``/``collapsed``/``partly expanded`` (ExpandCollapse).  Keyboard
    #: focus stays in :attr:`is_focused`.
    states: tuple[str, ...] = ()


#: The pattern flags :class:`UIElement` reports, in the order they are listed:
#: ``(label, IsXxxPatternAvailable property id)``.  ``value`` is handled apart,
#: because only a Value that is not read-only says "this takes text".
_PATTERN_ORDER = (
    ("invoke", _P_IS_INVOKE_AVAILABLE),
    ("toggle", _P_IS_TOGGLE_AVAILABLE),
    ("select", _P_IS_SELECTION_ITEM_AVAILABLE),
    ("expand", _P_IS_EXPAND_COLLAPSE_AVAILABLE),
)


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
    #: ``"ok" | "busy" | "empty"`` - see :data:`TREE_STATUSES`.
    status: str = "ok"
    #: One sentence for the reader when ``status`` is not ``ok`` or the tree is
    #: partial; empty otherwise.
    note: str = ""
    #: Off-screen branches the returned pass dropped without reading them (only
    #: counted where they were seen: branches under an element that was read as
    #: a whole subtree are filtered by UIA itself and not counted).
    offscreen_skipped: int = 0
    #: Cross-process cache requests the returned pass made (diagnostic).
    fetches: int = 0


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
        has_value: whether the element exposes a Value pattern at all, so that
            ``value is None`` can be read as "empty" rather than "not known".
    """

    role: str = ""
    name: str = ""
    value: str | None = None
    hwnd: int = 0
    accepts_text: bool = False
    shortcut: str | None = None
    ok: bool = False
    has_value: bool = False

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


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """``GetWindowRect`` as (l, t, r, b), or ``None`` if empty or gone."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (int(left), int(top), int(right), int(bottom))


def _owned_window_handles(hwnd: int, *, limit: int = _MAX_CHILD_WINDOWS) -> list[int]:
    """Visible top-level windows owned by ``hwnd`` that overlap it.

    Not every surface drawn over a window is a descendant of it: popups, menus,
    drop-downs and some hosted content views are separate top-level windows
    *owned* by it.  They are part of what the user sees in that window, and
    ``EnumChildWindows`` never lists them.  Ownership, visibility, cloaking and
    rectangles are window-manager facts; nothing here looks at a class name.
    """
    frame = _window_rect(hwnd)
    if frame is None:
        return []
    handles: list[int] = []

    def _collect(candidate: int, _: object) -> bool:
        if len(handles) >= limit:
            return False
        try:
            if candidate == hwnd or win32gui.GetWindow(candidate, win32con.GW_OWNER) != hwnd:
                return True
            if not win32gui.IsWindowVisible(candidate) or is_cloaked(candidate):
                return True
        except Exception:
            return True
        rect = _window_rect(candidate)
        if rect is not None and _overlap(rect, frame) is not None:
            handles.append(int(candidate))
        return True

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception:
        pass
    return handles


def _surface_handles(hwnd: int) -> list[int]:
    """Every window a pass reads alongside ``hwnd``: descendants, then owned."""
    handles = child_window_handles(hwnd)
    for owned in _owned_window_handles(hwnd, limit=max(_MAX_CHILD_WINDOWS - len(handles), 0)):
        if owned not in handles:
            handles.append(owned)
    return handles


def _overlap(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """Intersection of two (l, t, r, b) rectangles, or ``None``."""
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _area(rect: tuple[int, int, int, int] | None) -> int:
    if rect is None:
        return 0
    return max(rect[2] - rect[0], 0) * max(rect[3] - rect[1], 0)


def _contains(
    outer: tuple[int, int, int, int], inner: tuple[int, int, int, int], slack: int = _EDGE_SLACK
) -> bool:
    """Whether ``inner`` lies within ``outer`` (give or take ``slack`` px)."""
    return (
        outer[0] <= inner[0] + slack
        and outer[1] <= inner[1] + slack
        and outer[2] >= inner[2] - slack
        and outer[3] >= inner[3] - slack
    )


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


def _states(get: object) -> tuple[str, ...]:
    """Current state from the cached SelectionItem/Toggle/ExpandCollapse values.

    A state is only read when its pattern is available: for an element without
    the pattern UIA returns its "not supported" sentinel, which is a COM object
    and must not be mistaken for a value.
    """
    states: list[str] = []
    try:
        if _as_bool(get(_P_IS_SELECTION_ITEM_AVAILABLE)):  # type: ignore[operator]
            selected = get(_P_SELECTION_ITEM_IS_SELECTED)  # type: ignore[operator]
            if isinstance(selected, bool) and selected:
                states.append("selected")
        if _as_bool(get(_P_IS_TOGGLE_AVAILABLE)):  # type: ignore[operator]
            toggle = get(_P_TOGGLE_STATE)  # type: ignore[operator]
            if isinstance(toggle, int) and toggle in _TOGGLE_STATES:
                states.append(_TOGGLE_STATES[toggle])
        if _as_bool(get(_P_IS_EXPAND_COLLAPSE_AVAILABLE)):  # type: ignore[operator]
            expand = get(_P_EXPAND_COLLAPSE_STATE)  # type: ignore[operator]
            if isinstance(expand, int) and expand in _EXPAND_STATES:
                states.append(_EXPAND_STATES[expand])
    except Exception:
        pass  # a state is a nicety; never lose the element over it
    return tuple(states)


class _CachedWalker:
    """Collects :class:`UIElement`s from cached UIA subtrees.

    One walker collects a whole *pass*: the top-level element's subtree plus the
    subtree of every visible descendant HWND.  Those overlap - a child window's
    element is usually also reachable from the top-level one - so every element is
    checked against :attr:`seen` before it is kept, and the first sighting wins,
    which keeps the depth-first order of the walk that found it.
    """

    def __init__(
        self,
        max_elements: int,
        deadline: float,
        cancel: threading.Event | None = None,
    ) -> None:
        self.max_elements = max_elements
        self.deadline = deadline
        #: Set by the caller once it has stopped waiting, so an abandoned walk
        #: stops at its next check instead of running on to its deadline.
        self.cancel = cancel
        self.elements: list[UIElement] = []
        self.truncated = False
        self.seen: set[tuple] = set()
        self._shapes: set[tuple] = set()
        self.passes = 1
        self.child_windows = 0
        self.first_pass_elements = -1
        #: Off-screen branches dropped unread, and cache requests made.
        self.offscreen_skipped = 0
        self.fetches = 0
        #: Set once the pass has walked everything it meant to (as opposed to
        #: being stopped by the cap, the deadline or ``cancel``).
        self.complete = False
        #: Window handles the pass read, with their rectangles, for
        #: :func:`_surface_gaps`.
        self.surfaces: dict[int, tuple[int, int, int, int]] = {}
        #: Visible part of the window (window rect clipped to the desktop).
        self.viewport: tuple[int, int, int, int] | None = None
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
        patterns = tuple(
            label for label, property_id in _PATTERN_ORDER if _as_bool(get(property_id))
        ) + (("value",) if editable else ())
        is_interactive = enabled and (
            bool(patterns)
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
        states = _states(get)
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
                patterns=patterns if is_interactive else (),
                states=states,
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
        if self.cancel is not None and self.cancel.is_set():
            self.truncated = True
            return True
        if time.monotonic() >= self.deadline:
            self.truncated = True
            return True
        return False


def _build_cache_request(
    automation: object,
    scope: int,
    *,
    tree_filter: object | None = None,
    mode: int = _AUTOMATION_ELEMENT_MODE_NONE,
) -> object:
    """A cache request for every property :class:`UIElement` needs.

    Args:
        scope: ``TreeScope`` flags - which elements come back in the one call.
        tree_filter: view the request walks (default: the control view).
        mode: ``_AUTOMATION_ELEMENT_MODE_FULL`` when the returned elements will
            be asked for their own children later.
    """
    request = automation.CreateCacheRequest()  # type: ignore[attr-defined]
    for prop in _CACHED_PROPERTIES:
        request.AddProperty(prop)
    request.TreeScope = scope
    request.TreeFilter = (
        tree_filter
        if tree_filter is not None
        else automation.ControlViewCondition  # type: ignore[attr-defined]
    )
    request.AutomationElementMode = mode
    return request


#: How long one *descendant* HWND gets to answer ``ElementFromHandle`` before the
#: pass moves on without it.  The top-level window gets the whole budget; a child
#: that has closed, or whose provider is wedged, must not eat it.
_CHILD_PATIENCE_S = 0.15


def _element_from_handle(
    automation: object,
    hwnd: int,
    deadline: float,
    cancel: threading.Event | None = None,
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
            if cancel is not None and cancel.is_set():
                return None
            time.sleep(0.04)


def _collect_pass_whole(
    automation: object,
    hwnd: int,
    child_handles: list[int],
    max_elements: int,
    deadline: float,
    *,
    cancel: threading.Event | None = None,
    on_start: object | None = None,
) -> _CachedWalker:
    """Fallback pass: one unfiltered cached subtree per top-level child.

    This is the walker from before :class:`_ViewportPass`; it is only used when
    that one fails outright (a provider that rejects its requests), because on
    a large document a single one of its requests can take longer than the
    whole budget.

    The window's own subtree plus every child HWND's subtree.

    The top-level element comes first (so the window frame keeps element id 0),
    then its UIA children one cached subtree at a time, then each descendant
    HWND.  Asking about a descendant HWND is the part that matters for a lazily
    built provider: it is a separate ``WM_GETOBJECT`` to the window that actually
    owns the content, and until something sends it there is no tree to walk.

    Elements the earlier walks already produced are dropped by the walker's own
    de-duplication, so a child HWND whose content is already reachable from the
    top-level element costs one cache build and adds nothing - measured at 6 ms
    for Spotify's render-widget window.

    Args:
        cancel: stop early once set (the caller has stopped waiting).
        on_start: called with the walker before anything is collected, so the
            caller can see whether a pass that has not finished is producing
            anything at all (see :data:`_BUSY_S`).

    Raises:
        RuntimeError: UIA never produced an element for ``hwnd`` itself.
    """
    walker = _CachedWalker(max_elements=max_elements, deadline=deadline, cancel=cancel)
    if on_start is not None:
        on_start(walker)  # type: ignore[operator]
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
            automation,
            child_hwnd,
            min(deadline, time.monotonic() + _CHILD_PATIENCE_S),
            cancel,
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

    live_root = _element_from_handle(automation, hwnd, deadline, cancel)
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


class _ViewportPass:
    """One pass over a window, reading only what is on screen, in batches.

    Every element the walk keeps comes from a cache request; the question per
    element is only how big a request to make for what lies below it:

    * **whole subtree** (``TreeScope_Descendants`` filtered to
      ``IsOffscreen == False``, one call) when the element lies entirely inside
      the viewport, is not a scroll container and contains no descendant HWND:
      everything under it is on screen or clipped away inside it, so the batch
      is bounded by what the user can see;
    * **one level** (``TreeScope_Children``, one call, live references) when it
      reaches past the viewport, scrolls, or hosts a descendant HWND: its
      off-screen children are dropped here and their subtrees never visited,
      which is what keeps a 10,000-node page to a few hundred elements;
    * **its own window** when the element *is* a descendant HWND we listed: that
      handle's UIA root is asked directly (its own ``WM_GETOBJECT`` - what wakes
      a lazily built provider - and no routing of each node through the
      parent's provider), and walked by the same rules at the element's depth.

    Elements are appended depth-first as they arrive, so whatever has been
    collected when the deadline or cap stops the walk is a true prefix of the
    window's tree - never thrown away.
    """

    def __init__(
        self,
        automation: object,
        hwnd: int,
        handles: list[int],
        max_elements: int,
        deadline: float,
        *,
        cancel: threading.Event | None = None,
        on_start: object | None = None,
    ) -> None:
        self.automation = automation
        self.hwnd = hwnd
        self.deadline = deadline
        self.cancel = cancel
        self.walker = _CachedWalker(max_elements=max_elements, deadline=deadline, cancel=cancel)
        if on_start is not None:
            on_start(self.walker)  # type: ignore[operator]
        onscreen = automation.CreateAndCondition(  # type: ignore[attr-defined]
            automation.ControlViewCondition,  # type: ignore[attr-defined]
            automation.CreatePropertyCondition(_P_IS_OFFSCREEN, False),  # type: ignore[attr-defined]
        )
        self.root_request = _build_cache_request(
            automation,
            _TREE_SCOPE_ELEMENT | _TREE_SCOPE_CHILDREN,
            mode=_AUTOMATION_ELEMENT_MODE_FULL,
        )
        self.level_request = _build_cache_request(
            automation, _TREE_SCOPE_CHILDREN, mode=_AUTOMATION_ELEMENT_MODE_FULL
        )
        self.subtree_request = _build_cache_request(
            automation, _TREE_SCOPE_DESCENDANTS, tree_filter=onscreen
        )
        frame = _window_rect(hwnd)
        self.viewport = _overlap(frame, virtual_screen_bounds()) if frame else None
        self.walker.viewport = self.viewport
        self.rects: dict[int, tuple[int, int, int, int]] = {}
        for handle in handles:
            rect = _window_rect(handle)
            if rect is not None:
                self.rects[handle] = rect
        self.walker.surfaces = dict(self.rects)
        #: Descendant/owned HWNDs not yet read, in enumeration order.
        self.pending: dict[int, None] = dict.fromkeys(handles)
        #: HWNDs already read through their own root: meeting their element
        #: again must not walk the same content a second time the slow way.
        self.read_windows: set[int] = set()
        #: RuntimeIds of every element visited this pass, kept or not.  The same
        #: element is often reachable twice - through the parent's provider and
        #: through its own window's root - and its subtree only needs reading
        #: once.
        self.visited: set[tuple[int, ...]] = set()

    # -- calls --------------------------------------------------------------
    def _fetch(self, element: object, request: object) -> object | None:
        """One cache request; ``None`` (and ``truncated``) if it failed."""
        if self.walker.out_of_budget():
            return None
        try:
            holder = element.BuildUpdatedCache(request)  # type: ignore[attr-defined]
        except Exception:
            # The element went away (a page re-rendering), or the provider
            # refused: that branch is missing, and the tree says so.
            self.walker.truncated = True
            return None
        self.walker.fetches += 1
        return holder

    def _whole_subtree(self, get: object) -> bool:
        """Whether one filtered subtree request is safe for this element."""
        if self.viewport is None:
            return False
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        if bounds is None or not _contains(self.viewport, bounds):
            return False
        if _as_bool(get(_P_IS_SCROLL_AVAILABLE)):  # type: ignore[operator]
            return False  # its content can run far beyond its own rectangle
        return not any(_contains(bounds, rect) for rect in self.rects.values())

    # -- traversal ----------------------------------------------------------
    def _walk_children(self, holder: object, depth: int, *, live: bool) -> None:
        for child in _CachedWalker._cached_children(holder):
            if self.walker.out_of_budget():
                return
            self._visit(child, depth + 1, live=live)

    def _visit(self, element: object, depth: int, *, live: bool) -> None:
        """Keep ``element`` (if it is worth keeping) and walk what is below it.

        ``live``: the element came from a one-level request, so it can be asked
        for its own children; otherwise its whole subtree is already cached.
        """
        walker = self.walker
        if walker.out_of_budget():
            return
        if depth > _MAX_DEPTH:
            walker.truncated = True
            return
        get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
        if _as_bool(get(_P_IS_OFFSCREEN)):
            walker.offscreen_skipped += 1
            return
        runtime_id = _runtime_id(get)
        if runtime_id is not None:
            if runtime_id in self.visited:
                return  # already read, subtree and all, by another route
            self.visited.add(runtime_id)
        native = _native_handle(get)
        if native and native != self.hwnd:
            if native in self.pending and self._window(
                native, depth, host=runtime_id, host_depth=depth
            ):
                return
            if native in self.read_windows:
                walker._add(element, depth)  # dropped as a duplicate if seen
                return
        walker._add(element, depth)
        if not live:
            self._walk_children(element, depth, live=False)
            return
        if not native:
            # An element exactly the size of a window we have not read yet,
            # without saying it owns it: most likely it hosts that window.
            # Reading the window through its own root first means its content
            # comes from the provider behind the handle, and the same content
            # met below through this element is then skipped as visited.
            hosted = self._hosted_window(get)
            if hosted is not None:
                self._window(hosted, depth + 1, host=runtime_id, host_depth=depth)
        if self._whole_subtree(get):
            holder = self._fetch(element, self.subtree_request)
            if holder is not None:
                self._walk_children(holder, depth, live=False)
        else:
            holder = self._fetch(element, self.level_request)
            if holder is not None:
                self._walk_children(holder, depth, live=True)

    def _hosted_window(self, get: object) -> int | None:
        """A pending window whose rectangle is this element's, if any."""
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        if bounds is None:
            return None
        for handle in self.pending:
            rect = self.rects.get(handle)
            if rect is not None and _contains(bounds, rect) and _contains(rect, bounds):
                return handle
        return None

    def _window(
        self,
        handle: int,
        depth: int,
        *,
        host: tuple[int, ...] | None = None,
        host_depth: int = 0,
    ) -> bool:
        """Read a descendant/owned HWND through its own UIA root at ``depth``.

        ``host``: RuntimeId of the element that led here (it owns the handle,
        or has its exact rectangle), at ``host_depth``; if the window's root
        turns out to be that very element, it stays at ``host_depth`` and its
        children go directly below it.

        Returns False when the handle gave no element in time, so the caller
        can fall back to reading the element it met through the parent.
        """
        self.pending.pop(handle, None)
        walker = self.walker
        if walker.out_of_budget():
            return True
        live = _element_from_handle(
            self.automation,
            handle,
            min(self.deadline, time.monotonic() + _CHILD_PATIENCE_S),
            self.cancel,
        )
        if live is None:
            return False
        root = self._fetch(live, self.root_request)
        if root is None:
            return False
        self.read_windows.add(handle)
        get = root.GetCachedPropertyValue  # type: ignore[attr-defined]
        if _as_bool(get(_P_IS_OFFSCREEN)):
            walker.offscreen_skipped += 1
            return True
        runtime_id = _runtime_id(get)
        if host is not None and runtime_id == host:
            # The window's root *is* the element that led here: one element,
            # now read through its own provider.
            walker._add(root, host_depth)  # dropped as a duplicate if already kept
            self._walk_children(root, host_depth, live=True)
            return True
        if runtime_id is not None:
            if runtime_id in self.visited:
                return True  # its content was already read through the parent
            self.visited.add(runtime_id)
        walker._add(root, depth)
        self._walk_children(root, depth, live=True)
        return True

    def run(self) -> _CachedWalker:
        """Walk the window; raises if its own handle gives no UIA element."""
        walker = self.walker
        live_root = _element_from_handle(self.automation, self.hwnd, self.deadline, self.cancel)
        if live_root is None:
            raise RuntimeError(f"UIA would not give an element for hwnd={self.hwnd}")
        # Not through _fetch: if the root itself refuses this request, the
        # caller falls back to the older whole-subtree walk.
        root = live_root.BuildUpdatedCache(self.root_request)  # type: ignore[attr-defined]
        walker.fetches += 1
        root_id = _runtime_id(root.GetCachedPropertyValue)
        if root_id is not None:
            self.visited.add(root_id)
        walker._add(root, 0)  # the frame keeps id 0, even if UIA calls it off-screen
        self._walk_children(root, 0, live=True)
        # Handles the walk never met (their owner was off-screen, padding, or
        # not exposed by the parent's provider) are asked regardless - that is
        # what wakes a lazily built provider - at depth 1, the honest floor.
        for handle in list(self.pending):
            if walker.out_of_budget():
                break
            self._window(handle, 1)
        stopped = (
            len(walker.elements) >= walker.max_elements
            or (self.cancel is not None and self.cancel.is_set())
            or time.monotonic() >= self.deadline
        )
        walker.complete = not stopped
        return walker


def _runtime_id(get: object) -> tuple[int, ...] | None:
    """The cached RuntimeId as a tuple, or ``None`` if the provider gave none."""
    try:
        raw = get(_P_RUNTIME_ID)  # type: ignore[operator]
        runtime_id = tuple(int(part) for part in raw) if raw is not None else ()
    except Exception:
        return None
    return runtime_id or None


def _native_handle(get: object) -> int:
    try:
        return int(get(_P_NATIVE_WINDOW_HANDLE) or 0)  # type: ignore[operator]
    except Exception:
        return 0


def _collect_pass(
    automation: object,
    hwnd: int,
    child_handles: list[int],
    max_elements: int,
    deadline: float,
    *,
    cancel: threading.Event | None = None,
    on_start: object | None = None,
) -> _CachedWalker:
    """One pass over the window (see :class:`_ViewportPass`).

    Falls back to :func:`_collect_pass_whole` only if the viewport pass cannot
    even read the window's root element with its requests.

    Raises:
        RuntimeError: UIA never produced an element for ``hwnd`` itself.
    """
    viewport_pass = _ViewportPass(
        automation,
        hwnd,
        child_handles,
        max_elements,
        deadline,
        cancel=cancel,
        on_start=on_start,
    )
    try:
        return viewport_pass.run()
    except RuntimeError:
        raise
    except Exception:
        if viewport_pass.walker.elements:
            viewport_pass.walker.truncated = True
            return viewport_pass.walker  # it was reading: keep what it read
    walker = _collect_pass_whole(
        automation,
        hwnd,
        child_handles,
        max_elements,
        deadline,
        cancel=cancel,
        on_start=on_start,
    )
    walker.surfaces = viewport_pass.walker.surfaces
    walker.viewport = viewport_pass.viewport
    walker.complete = not (
        len(walker.elements) >= max_elements
        or (cancel is not None and cancel.is_set())
        or time.monotonic() >= deadline
    )
    return walker


def _leaf_coverage(
    elements: list[UIElement], viewport: tuple[int, int, int, int]
) -> float:
    """Share of ``viewport`` covered by leaf elements (0..1), on a coarse grid.

    A leaf is an element with no kept descendant (the next element is not
    deeper).  Containers are left out on purpose: a frame or a page wrapper
    covers everything and says nothing about whether anything is inside it.
    """
    left, top, right, bottom = viewport
    cols = max((right - left + _COVERAGE_CELL - 1) // _COVERAGE_CELL, 1)
    rows = max((bottom - top + _COVERAGE_CELL - 1) // _COVERAGE_CELL, 1)
    grid = bytearray(cols * rows)
    for index, element in enumerate(elements):
        if index + 1 < len(elements) and elements[index + 1].depth > element.depth:
            continue  # has something below it: not a leaf
        clipped = _overlap(element.bounds, viewport)
        if clipped is None:
            continue
        c0 = (clipped[0] - left) // _COVERAGE_CELL
        c1 = min((clipped[2] - left - 1) // _COVERAGE_CELL + 1, cols)
        r0 = (clipped[1] - top) // _COVERAGE_CELL
        r1 = min((clipped[3] - top - 1) // _COVERAGE_CELL + 1, rows)
        if c1 <= c0:
            continue
        run = b"\x01" * (c1 - c0)
        for row in range(r0, r1):
            grid[row * cols + c0 : row * cols + c1] = run
    return grid.count(1) / len(grid)


@dataclass
class _Gaps:
    """What a pass left unexplained (see :func:`_surface_gaps`)."""

    #: Large descendant/owned HWNDs with no element inside them.
    empty_surfaces: list[int] = field(default_factory=list)
    #: Share of the visible window covered by leaf elements, ``None`` if unknown.
    coverage: float | None = None

    @property
    def any(self) -> bool:
        return bool(self.empty_surfaces) or (
            self.coverage is not None and self.coverage < _MIN_COVERAGE
        )


def _surface_gaps(walker: _CachedWalker) -> _Gaps:
    """Parts of the window a pass found nothing in.

    Two facts about rectangles, neither about which program drew them:

    * a descendant or owned HWND covering at least :data:`_SURFACE_MIN_SHARE`
      of the visible window with no element inside it (containers the size of
      the handle itself do not count - a root with nothing below it is exactly
      what an unbuilt provider returns);
    * leaf elements covering less than :data:`_MIN_COVERAGE` of the visible
      window, which is what a browser frame looks like around a page that has
      not been exposed yet.
    """
    viewport = walker.viewport
    if viewport is None:
        return _Gaps()
    elements = list(walker.elements)
    view_area = _area(viewport)
    empty: list[int] = []
    for handle, rect in walker.surfaces.items():
        visible = _overlap(rect, viewport)
        if visible is None or _area(visible) < _SURFACE_MIN_SHARE * view_area:
            continue
        limit = 0.9 * _area(rect)
        if not any(
            _contains(rect, element.bounds) and _area(element.bounds) < limit
            for element in elements
        ):
            empty.append(handle)
    return _Gaps(empty_surfaces=empty, coverage=_leaf_coverage(elements, viewport))


def _walk_window(
    hwnd: int,
    max_elements: int,
    deadline: float,
    *,
    young: bool = False,
    publish: object | None = None,
    on_pass: object | None = None,
    cancel: threading.Event | None = None,
) -> _CachedWalker:
    """Collect the window's elements, waiting out a tree that is still building.

    Runs on the worker thread.  The first pass - the top-level subtree plus every
    descendant HWND's, which is itself what wakes a lazily built provider - is
    usually the whole story.  When it comes back thin (fewer than
    :data:`_THIN_TREE_ELEMENTS`, no more elements than there are descendant
    HWNDs to host content, or - however many elements it has - a large child
    window with nothing in it or most of the window uncovered, see
    :func:`_surface_gaps`), the pass is repeated as a condition poll with a
    deadline, never a settle sleep:

    * the count grew past the first pass and then stood still for
      :data:`_SETTLE_S` - the tree has populated; stop;
    * the count never exceeded the first pass within :data:`_WAKE_GRACE_S` -
      that is simply the truth about this window; stop.  (Skipped for a
      ``young`` process, whose first tree can take seconds to start growing: it
      is watched until it grows or ``deadline``, and while it is still below
      thin, a pause in growth does not count as settled.)
    * ``deadline``.

    Facts about a snapshot drive that decision - an element count, a list of
    window handles, rectangles - and nothing here knows the name of an
    application.

    Args:
        publish: called with every pass that becomes the best so far, so the
            caller can return it even if a later pass is abandoned at the
            deadline.
        on_pass: handed to :func:`_collect_pass` as ``on_start`` for every pass.
        cancel: once set, stop at the next check and return what there is.
    """
    # A fresh IUIAutomation for this thread's apartment: COM interface pointers
    # cannot be shared across apartments, and an abandoned thread must not
    # leave a poisoned shared client behind.
    module = _uia_core()
    automation = comtypes.client.CreateObject(
        _CUIAUTOMATION_CLSID, interface=module.IUIAutomation
    )
    child_handles = _surface_handles(hwnd)
    passes = 1

    def adopt(walker: _CachedWalker) -> _CachedWalker:
        walker.passes = passes
        walker.child_windows = len(child_handles)
        walker.first_pass_elements = first_count
        if publish is not None:
            publish(walker)  # type: ignore[operator]
        return walker

    best = _collect_pass(
        automation,
        hwnd,
        child_handles,
        max_elements,
        deadline,
        cancel=cancel,
        on_start=on_pass,
    )
    first_count = len(best.elements)
    best = adopt(best)

    thin = (
        first_count < _THIN_TREE_ELEMENTS
        or (bool(child_handles) and first_count <= len(child_handles))
        # A populated frame around content that has not been exposed yet: a
        # large child window with nothing in it, or most of the window blank.
        or _surface_gaps(best).any
    )
    if not thin or len(best.elements) >= max_elements or time.monotonic() >= deadline:
        return best

    poll_started = time.monotonic()
    grace_deadline = min(deadline, poll_started + _WAKE_GRACE_S)
    last_growth = poll_started
    while time.monotonic() < deadline:
        time.sleep(min(_WAKE_POLL_S, max(deadline - time.monotonic(), 0.0)))
        now = time.monotonic()
        if now >= deadline or (cancel is not None and cancel.is_set()):
            break
        grown = len(best.elements) > first_count
        if not grown and not young and now >= grace_deadline:
            break  # woken and re-read: it did not grow, so this is the window
        child_handles = _surface_handles(hwnd)
        if not child_handles and not grown and now >= grace_deadline:
            break  # nothing to wake, and nothing arrived to wake
        try:
            attempt = _collect_pass(
                automation,
                hwnd,
                child_handles,
                max_elements,
                deadline,
                cancel=cancel,
                on_start=on_pass,
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


def _has_content(walker: object) -> bool:
    """Whether an unfinished pass has collected anything below the root element.

    The root is the window handle's own element; everything at depth 1 and below
    came from the provider actually answering.  The list is copied first because
    the worker thread may still be appending to it.
    """
    elements = list(getattr(walker, "elements", None) or [])
    return any(element.depth > 0 for element in elements)


def _answered(result: dict) -> bool:
    """Whether the worker has produced anything worth waiting on."""
    if any(key in result for key in ("walker", "best", "error")):
        return True
    live = result.get("live")
    return live is not None and _has_content(live)


def _on_screen(hwnd: int, pid: int | None) -> bool:
    """Visible, not minimised, not cloaked, and its process still running.

    The conditions under which silence from UIA means "busy" rather than "there
    is nothing here to see".  Window-state facts only.
    """
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.IsIconic(hwnd) or is_cloaked(hwnd):
            return False
    except Exception:
        return False
    if pid:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return True
    return True


def _silent_note(waited_s: float, on_screen: bool) -> tuple[str, str]:
    """``(status, note)`` for a window from which nothing came back in time."""
    if on_screen:
        return (
            "busy",
            f"window did not answer accessibility queries within {waited_s:.1f} s "
            f"(probably loading or rendering); look again shortly",
        )
    return (
        "empty",
        f"window did not answer accessibility queries within {waited_s:.1f} s, and "
        f"it is minimised, hidden or gone",
    )


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

    A window that produces no content at all within :data:`_BUSY_S` while it is
    on screen and its process is alive is not waited on further: the result is
    returned then, with no elements, ``truncated`` set, ``status="busy"`` and a
    ``note`` saying the window did not answer.  ``status`` is ``"ok"`` whenever
    UIA answered, however small the tree, and ``"empty"`` when it answered with
    nothing usable (or the window is not on screen and never answered).

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
    cancel = threading.Event()

    def _publish(walker: _CachedWalker) -> None:
        result["best"] = walker

    def _pass_started(walker: _CachedWalker) -> None:
        result["live"] = walker

    def _worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass  # already initialised for this thread
        try:
            result["walker"] = _walk_window(
                hwnd,
                max_elements,
                deadline,
                young=young,
                publish=_publish,
                on_pass=_pass_started,
                cancel=cancel,
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
    pid = info.pid if info is not None else None

    def _silent(waited_s: float) -> WindowTree:
        """Nothing came back: abandon the worker and say why there is no tree."""
        cancel.set()
        status, note = _silent_note(waited_s, _on_screen(hwnd, pid))
        return WindowTree(
            hwnd=hwnd,
            title=info.title if info else "",
            process_name=info.process_name if info else "",
            elements=[],
            truncated=True,
            captured_at=time.time(),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            passes=0,
            child_windows=len(_surface_handles(hwnd)),
            status=status,
            note=note,
        )

    # First, a short wait for *any* content.  A window that answers keeps the
    # whole budget below; one that is on screen and silent is reported busy now
    # rather than after the full timeout, which it would only have spent silent.
    busy_after = min(_BUSY_S, timeout_s)
    thread.join(busy_after)
    if thread.is_alive() and not _answered(result) and _on_screen(hwnd, pid):
        return _silent(busy_after)
    thread.join(max(timeout_s - (time.perf_counter() - started), 0.0))
    if thread.is_alive():
        cancel.set()  # stop the abandoned walk at its next check

    finished = result.get("walker")
    best = result.get("best")
    live = result.get("live")
    partial = False
    if isinstance(finished, _CachedWalker):
        chosen = finished
    else:
        # Overran.  Everything collected so far is a true answer: the pass that
        # was in flight is a depth-first prefix of the window's tree, so it is
        # used whenever it holds more than the last complete pass - a pass cut
        # off 90% of the way through a large page beats a small earlier one.
        candidates = [w for w in (best, live) if isinstance(w, _CachedWalker)]
        chosen = max(candidates, key=lambda w: len(w.elements)) if candidates else None
        if chosen is None or not (chosen is best or _has_content(chosen)):
            error = result.get("error")
            if error is not None:
                raise RuntimeError(f"UIA walk of hwnd={hwnd} failed: {error}") from error
            # Still running and nothing below the root: abandon the thread.
            return _silent(timeout_s)
        partial = True
    elements = list(chosen.elements)
    if not chosen.complete and chosen.truncated and len(elements) < max_elements:
        partial = True  # the worker returned, but only because its deadline stopped it
    note = ""
    if partial and elements:
        last = elements[-1]
        where = f"[{last.id}] {last.role}"
        if last.name:
            where += f' "{_one_line(last.name, 40)}"'
        note = (
            f"reading stopped at the {timeout_s:g} s limit; these {len(elements)} "
            f"elements are everything read until then, in tree order from the window "
            f"frame down to {where} - what comes after that in the tree was not read"
        )
    status = "ok" if elements else "empty"
    if status == "empty":
        note = "the window answered but exposes no elements with a size on screen"
    elif not partial:
        gaps = _surface_gaps(chosen)
        if gaps.any:
            parts = []
            if gaps.coverage is not None:
                parts.append(
                    f"only {gaps.coverage:.0%} of the window's visible area holds "
                    f"accessible elements"
                )
            if gaps.empty_surfaces:
                parts.append(
                    f"{len(gaps.empty_surfaces)} large child window(s) exposed nothing"
                )
            note = (
                "; ".join(parts)
                + " - the rest is drawn without accessibility (video, canvas) or has "
                "not been exposed yet; look again shortly if you expected content there"
            )
    passes = max(
        chosen.passes, best.passes if isinstance(best, _CachedWalker) else 1
    )
    reference = best if isinstance(best, _CachedWalker) else chosen
    return WindowTree(
        hwnd=hwnd,
        title=info.title if info else "",
        process_name=info.process_name if info else "",
        elements=elements,
        truncated=chosen.truncated or partial,
        captured_at=time.time(),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        passes=passes,
        child_windows=reference.child_windows or len(_surface_handles(hwnd)),
        first_pass_elements=(
            reference.first_pass_elements
            if reference.first_pass_elements >= 0
            else len(elements)
        ),
        status=status,
        note=note,
        offscreen_skipped=chosen.offscreen_skipped,
        fetches=chosen.fetches,
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
    has_value = _as_bool(get(_P_IS_VALUE_AVAILABLE))
    if has_value:
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
        has_value=has_value,
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


@dataclass
class FocusText:
    """What the focused control holds, read back through UIA.

    Attributes:
        ok: UIA answered at all.
        readable: ``text`` is the control's contents (or the part just before
            its caret); False when there is no Value or Text pattern, it is a
            password field, or reading failed - ``reason`` says which.
        source: ``"value"`` (ValuePattern, the whole value), ``"caret"``
            (TextPattern, the characters just before the caret or selection) or
            ``"document"`` (TextPattern document range); ``""`` when unreadable.
        text: what was read.
        complete: ``text`` is all of it (not clipped, not just a caret window).
        role, name, hwnd: which control was read, to compare with the one input
            was aimed at.
        reason: why it is not readable, when it is not.
    """

    ok: bool = False
    readable: bool = False
    source: str = ""
    text: str = ""
    complete: bool = False
    role: str = ""
    name: str = ""
    hwnd: int = 0
    reason: str = ""

    def same_control(self, focus: "FocusInfo | None") -> bool:
        """Whether this was read from the control ``focus`` describes."""
        if focus is None or not (self.ok and focus.ok):
            return False
        return (self.role, self.name, self.hwnd) == (focus.role, focus.name, focus.hwnd)


def _read_focused_text(automation: object, caret_chars: int, max_chars: int) -> FocusText:
    """Read the focused control's contents.  Runs on the worker thread."""
    element = automation.GetFocusedElement()  # type: ignore[attr-defined]
    if not element:
        return FocusText(ok=True, reason="nothing has keyboard focus")
    request = _build_cache_request(automation, _TREE_SCOPE_ELEMENT)
    request.AddProperty(_P_IS_TEXT_AVAILABLE)
    request.AddProperty(_P_IS_PASSWORD)
    cached = element.BuildUpdatedCache(request)
    get = cached.GetCachedPropertyValue
    try:
        hwnd = int(get(_P_NATIVE_WINDOW_HANDLE) or 0)
    except Exception:
        hwnd = 0
    who = {"role": _role_name(get(_P_CONTROL_TYPE)), "name": _as_text(get(_P_NAME)), "hwnd": hwnd}
    if _as_bool(get(_P_IS_PASSWORD)):
        return FocusText(ok=True, reason="it is a password field", **who)
    if _as_bool(get(_P_IS_VALUE_AVAILABLE)):
        raw = get(_P_VALUE_VALUE)
        text = raw if isinstance(raw, str) else ""
        return FocusText(
            ok=True,
            readable=True,
            source="value",
            text=text[:max_chars],
            complete=len(text) <= max_chars,
            **who,
        )
    if not _as_bool(get(_P_IS_TEXT_AVAILABLE)):
        return FocusText(ok=True, reason="it exposes neither a Value nor a Text pattern", **who)
    module = _uia_core()
    pattern = element.GetCurrentPattern(_PATTERN_TEXT).QueryInterface(
        module.IUIAutomationTextPattern  # type: ignore[attr-defined]
    )
    # The characters just before the caret (or before the start of a selection,
    # which is where an inline completion begins): exactly where typed text
    # lands, and bounded however large the document is.
    try:
        selection = pattern.GetSelection()
        if selection is not None and selection.Length > 0:
            caret = selection.GetElement(0)
            window = caret.Clone()
            window.MoveEndpointByRange(_TEXT_ENDPOINT_END, caret, _TEXT_ENDPOINT_START)
            window.MoveEndpointByUnit(
                _TEXT_ENDPOINT_START, _TEXT_UNIT_CHARACTER, -max(int(caret_chars), 1)
            )
            text = window.GetText(max(int(caret_chars), 1) + 1)
            return FocusText(
                ok=True, readable=True, source="caret", text=str(text or ""), **who
            )
    except Exception:
        pass  # no caret to read from: fall back to the whole document
    text = str(pattern.DocumentRange.GetText(max_chars + 1) or "")
    return FocusText(
        ok=True,
        readable=True,
        source="document",
        text=text[:max_chars],
        complete=len(text) <= max_chars,
        **who,
    )


def focused_text(
    *, caret_chars: int = 200, max_chars: int = 20000, timeout_s: float = _FOCUS_TIMEOUT_S
) -> FocusText:
    """What the control with keyboard focus holds, read back through UIA.

    ``SendInput`` accepting a keystroke says nothing about whether the app kept
    it; this is how the text that actually arrived is checked.  A Value pattern
    gives the whole value; failing that a Text pattern gives the
    ``caret_chars`` characters before the caret (where typing lands), or the
    document's first ``max_chars``.

    Same worker-thread shape as :func:`focused_element`, and like it never raises.
    """
    result: dict[str, object] = {}

    def _worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        try:
            module = _uia_core()
            automation = comtypes.client.CreateObject(
                _CUIAUTOMATION_CLSID, interface=module.IUIAutomation
            )
            result["text"] = _read_focused_text(automation, caret_chars, max_chars)
        except BaseException as exc:  # noqa: BLE001 - reported as unreadable
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name="yuki-uia-text", daemon=True)
    thread.start()
    thread.join(timeout_s)
    read = result.get("text")
    if isinstance(read, FocusText):
        return read
    return FocusText(
        reason=str(result.get("error") or f"UIA did not answer within {timeout_s:g} s")
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
    layout scaffolding the model cannot use.  Indentation carries ``depth``.  An
    interactive element's supported patterns follow its position in braces,
    ``{invoke,expand}``, so an element a click activates (``invoke``/``toggle``)
    can be told from one a click only selects (``select``).  Current state
    follows in brackets with the other flags - ``selected`` (the tab or item that
    is chosen), ``on``/``off``/``mixed``, ``expanded``/``collapsed``,
    ``focused``, ``scrollable``: ``[selected focused]``.
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
    status = getattr(tree, "status", "ok") or "ok"
    note = getattr(tree, "note", "") or ""
    if status != "ok":
        header += f" [status: {status}]"
    if tree.truncated:
        header += " [TRUNCATED: partial tree]"
    skipped = getattr(tree, "offscreen_skipped", 0) or 0
    if skipped:
        # Said so the reader knows the list is the visible part on purpose:
        # scrolling brings the rest into view (and into the next read).
        header += f" [on-screen only: {skipped} off-screen branch(es) not read]"
    lines = [header]
    if note:
        lines.append(f"note: {note}")
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
        patterns = getattr(element, "patterns", ())
        if patterns:
            parts.append("{" + ",".join(patterns) + "}")
        if element.shortcut:
            parts.append(f"[kb: {element.shortcut}]")
        flags = list(getattr(element, "states", ()) or ())
        if element.is_focused:
            flags.append("focused")
        if element.is_scrollable:
            flags.append("scrollable")
        if flags:
            parts.append(f"[{' '.join(flags)}]")
        lines.append(" ".join(parts))
    if shown == 0 and status != "busy":  # a busy window's note already says why
        lines.append("(no named or interactive elements - UIA exposes nothing usable here)")
    return "\n".join(lines)
