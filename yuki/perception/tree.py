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

**Only the page in front.**  A browser keeps the page of every open tab
exposed and reported on screen, each in a child window of its own, so a walk
that reads every child window mixes all tabs' content (on 2026-09-23 a 400-element
read of a Chrome window was mostly other tabs').  A page Document whose window
is not the one on top at its centre - another page's window is - is left out,
subtree and all, and counted (:meth:`_ViewportPass._background_page`; the tree
header says "N background tab page(s) skipped").  The page that is shown leads
the header as ``page: "<title>" <url>`` (its Document's Name and Value), which
is the fact about what the window shows: a browser's window title can stay the
same whatever the page, and its tab names lag behind.  :func:`wait_for_page`
reads only that (a page-only pass, nothing inside the pages) and
:func:`page_text` reads a page's whole text in one Text-pattern call.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
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

#: ``UIA_DocumentControlTypeId``: a page (browser, PDF, rich document).
_DOCUMENT_CONTROL_TYPE = 50030
#: ``UIA_HyperlinkControlTypeId``.
_HYPERLINK_CONTROL_TYPE = 50005
#: ``UIA_TextControlTypeId``.
_TEXT_CONTROL_TYPE = 50020

#: Bounds on a scan for page Documents that looks past ``IsOffscreen`` (see
#: :meth:`_ViewportPass._scan_pages`): levels below where it starts, elements
#: it may look at, and seconds it may take.  The frame around a browser's pages
#: is a handful of nested containers; measured 2026-09-24 on a Gecko window with
#: 18 tabs, its six page Documents sat three levels below the element that
#: holds them all and were found with 15 cache requests.
_PAGE_SCAN_DEPTH = 8
_PAGE_SCAN_NODES = 300
_PAGE_SCAN_S = 0.3
#: Large off-screen elements a pass remembers as places to scan.
_MAX_SUSPECTS = 8

#: A page whose content, read through the UIA control view, is at least
#: :data:`_HIDDEN_TEXT_MIN_NODES` elements and either has named Text elements
#: fewer than :data:`_HIDDEN_TEXT_SHARE` of them, or has at least
#: :data:`_BLANK_LEAF_SHARE` of its leaves saying nothing (no name, no value,
#: nothing below them - where the view left a container's text out), may keep
#: its text leaves out of that view, and is read again through the raw view.
#: Measured 2026-09-24, whole page subtrees, control view vs raw view:
#: a Gecko Gmail inbox 23 named Text of 1843 elements, 74% blank leaves, raw
#: text 1.8x; a Gecko Fireflies page 12% Text, 61% blank leaves, raw text
#: 5.1x; a Gecko Meet call 71% blank leaves, raw text 1.1x; Chromium and
#: Electron pages (Chrome, Discord, Slack, Claude) 11-39% Text and 4-37% blank
#: leaves, raw text 1.0-1.6x.
_HIDDEN_TEXT_MIN_NODES = 50
_HIDDEN_TEXT_SHARE = 0.05
_BLANK_LEAF_SHARE = 0.5
#: The raw-view read replaces the control-view one only when it carries at
#: least this many times as much text (names and values), so a page whose
#: control view was merely terse keeps the leaner read.  In the tree walk,
#: which keeps every named element, what counts is the text the raw read has
#: that the control read does not: a raw view repeats a row's or a link's name
#: in the Text leaves below it.  Measured 2026-09-24 on the visible part of
#: three Gecko pages: the raw read added 6 new characters to the control
#: read's 47,352 (Gmail), 5 to 4,952 (Fireflies), 1 to 777 (Meet).
_RAW_TEXT_GAIN = 1.5
#: ``UIA_IsControlElementPropertyId``: False for an element the control view
#: leaves out (what a raw-view-only text leaf is).
_P_IS_CONTROL_ELEMENT = 30016


def _text_hidden(nodes: int, texts: int, leaves: int, blank_leaves: int) -> bool:
    """Whether a control-view read looks like one that kept text leaves out
    (see :data:`_HIDDEN_TEXT_MIN_NODES`).  Counts of elements, nothing else."""
    if nodes < _HIDDEN_TEXT_MIN_NODES:
        return False
    return texts < _HIDDEN_TEXT_SHARE * nodes or (
        leaves > 0 and blank_leaves >= _BLANK_LEAF_SHARE * leaves
    )

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

#: Windows an action has just opened or handed something to open (a URL, a
#: file), with when (``time.monotonic``).  Process age alone misses the common
#: case where an app keeps a background process alive: on 2026-09-23 a browser
#: window created seconds earlier belonged to a process far older than
#: :data:`_YOUNG_PROCESS_S`, so its half-loaded frame (7% of the window covered)
#: was returned after 0.6 s as if it were the page.  A window noted here is read
#: with the same patience as a young process for :data:`_YOUNG_PROCESS_S`.
_fresh_windows: dict[int, float] = {}
_fresh_lock = threading.Lock()


def note_window_fresh(hwnd: int) -> None:
    """Record that ``hwnd`` was just opened, or just handed a target to open.

    Called by the actions that do that (``launch_app``, ``open_url``).  For the
    next :data:`_YOUNG_PROCESS_S` a thin or mostly blank read of that window is
    re-polled until its content is exposed or the budget ends, exactly as for a
    window of a process that has only just started.  A fact about what this
    program did to the window, not about which application it is.
    """
    if not hwnd:
        return
    now = time.monotonic()
    with _fresh_lock:
        for stale in [h for h, at in _fresh_windows.items() if now - at >= _YOUNG_PROCESS_S]:
            del _fresh_windows[stale]
        _fresh_windows[int(hwnd)] = now


def _window_is_fresh(hwnd: int) -> bool:
    with _fresh_lock:
        at = _fresh_windows.get(int(hwnd))
    return at is not None and time.monotonic() - at < _YOUNG_PROCESS_S

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
    #: ``id`` of the nearest kept ancestor in the UIA tree, ``-1`` for the root
    #: (or when not known).  Depth alone cannot say this: an ancestor that was
    #: not kept (layout padding) leaves two unrelated branches at the same depths.
    parent: int = -1


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
    #: Page Documents left out of the tree, subtree and all, because another
    #: page's window is on top of them: a browser's background tabs (see
    #: :meth:`_ViewportPass._background_page`).
    background_pages: int = 0


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
    Chromium window can carry several of them.  DWM cloaking is *not* a reason
    to drop a child: measured on this desktop, the child window a WinUI
    browser draws its page in reports ``DWMWA_CLOAKED`` 1 (and its own child 5)
    while that page is on screen - cloaking only means something for top-level
    windows.

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
        #: ``[depth, kept id or -1, window handle]`` of every element on the
        #: path from the root to the one being added, kept or not, so each kept
        #: element can name its nearest kept ancestor exactly (see
        #: :attr:`UIElement.parent`) and the window it was drawn in.
        self._lineage: list[list[int]] = []
        #: Page Documents left out, subtree and all, because another page's
        #: window is on top of them (see :meth:`_ViewportPass._background_page`).
        self.background_pages = 0
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
        #: Pages read by geometry alone because they - and everything around
        #: them - reported ``IsOffscreen`` while lying inside the window's
        #: visible rectangle (a window covered by other windows, see
        #: :meth:`_ViewportPass._offscreen_pages`).
        self.offscreen_pages = 0
        #: Pages whose content was read through the raw view because the
        #: control view kept their text out (see :meth:`_ViewportPass._read_page`).
        self.raw_pages = 0
        #: Scans for page Documents made past ``IsOffscreen`` (diagnostic).
        self.page_scans = 0
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
    def _add(self, element: object, depth: int, *, window: int = 0) -> None:
        # Every element met, kept or not, closes the branches at its depth and
        # below: the walk is depth-first, so whatever sits deeper on the path
        # belongs to a sibling's subtree that is finished.
        lineage = self._lineage
        while lineage and lineage[-1][0] >= depth:
            lineage.pop()
        get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
        # The window it is drawn in: its own handle, else the handle it was read
        # through (a window root that reports none), else its parent's.
        native = _native_handle(get) or window or (lineage[-1][2] if lineage else 0)
        lineage.append([depth, -1, native])
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
                parent=next((entry[1] for entry in reversed(lineage[:-1]) if entry[1] >= 0), -1),
            )
        )
        lineage[-1][1] = len(self.elements) - 1

    def window_at(self, depth: int) -> int:
        """The window an element about to be added at ``depth`` inherits: that of
        its nearest ancestor on the current path (0 when none is known)."""
        for entry in reversed(self._lineage):
            if entry[0] < depth:
                return entry[2]
        return 0

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


def _page_facts(get: object) -> tuple[str, str, tuple[int, int, int, int] | None] | None:
    """``(name, address, bounds)`` of a page Document (a Document whose Value
    is an address, see :func:`is_address`), else ``None``."""
    try:
        if get(_P_CONTROL_TYPE) != _DOCUMENT_CONTROL_TYPE:  # type: ignore[operator]
            return None
        raw = get(_P_VALUE_VALUE) if _as_bool(get(_P_IS_VALUE_AVAILABLE)) else None  # type: ignore[operator]
        if not isinstance(raw, str) or not is_address(raw):
            return None
        return _as_text(get(_P_NAME)), raw, _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
    except Exception:
        return None


def _title_rank(name: str, window_title: str) -> int:
    """2: the window title starts with the page title; 1: contains it; 0: no.

    A browser titles its window after the page in front ("<page> - <app>").
    Comparing two titles the browser itself wrote, not naming any app.
    """
    name, title = name.strip(), window_title.strip()
    if not name or not title:
        return 0
    if title.startswith(name):
        return 2
    return 1 if name in title else 0


@dataclass
class _StackedPage:
    """A page Document met by :meth:`_ViewportPass._scan_pages`."""

    element: object
    key: tuple
    name: str
    value: str
    bounds: tuple[int, int, int, int]
    offscreen: bool
    focused: bool
    depth: int
    window: int
    order: int


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

    Three provider facts are not taken at their word (measured 2026-09-24 on a
    Gecko window; nothing here knows which program drew a window):

    * **``IsOffscreen`` on a covered window.**  A provider can report every
      page Document - the one on screen included - and the containers around
      it as off-screen while its window is merely covered by other windows.
      When a pass found no page but dropped a large off-screen element inside
      the window's visible rectangle, those elements are scanned for page
      Documents past ``IsOffscreen`` (:meth:`_offscreen_pages`), and the page
      chosen is read by geometry alone: an element is visible when its
      rectangle meets the viewport clipped by every scrolling ancestor.
    * **Stacked pages.**  A browser can keep every tab's Document in one
      window with one rectangle, so the child-window stacking test cannot tell
      them apart (:meth:`_background_page`).  When a page Document shares its
      window and rectangle with others (:meth:`_stacked_behind`), the one in
      front is chosen from facts: not off-screen, then its Name leading the
      window title (a browser titles its window after the page in front), then
      the selected TabItem naming it, then keyboard focus, then tree order.
      The rest count as background pages.
    * **Text kept out of the control view.**  A page whose control-view read
      looks like one that left its text leaves out (few named Text elements,
      or mostly leaves that say nothing) is probed with one search of the raw
      view; only if that finds visible text the control read lacks is the page
      read again through the raw view, and that read kept when the text it
      adds is substantial (:meth:`_read_page`).
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
        stop_at_documents: bool = False,
    ) -> None:
        self.automation = automation
        self.hwnd = hwnd
        self.deadline = deadline
        self.cancel = cancel
        #: Keep a Document element but read nothing below it: the frame of
        #: the window plus the identity of every page on screen, without the
        #: pages themselves (see :func:`wait_for_page`).
        self.stop_at_documents = stop_at_documents
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
            automation,
            _TREE_SCOPE_DESCENDANTS,
            tree_filter=onscreen,
            # A page-only pass keeps live references to what it reads, so a
            # page Document found in a batch can be asked for its text.
            mode=_AUTOMATION_ELEMENT_MODE_FULL if stop_at_documents else _AUTOMATION_ELEMENT_MODE_NONE,
        )
        #: Live UIA elements of the page Documents kept by a page-only pass,
        #: by element id (see :func:`page_text`).
        self.page_elements: dict[int, object] = {}
        #: The same for pages found past ``IsOffscreen`` in a covered window
        #: (:meth:`_offscreen_pages`), kept apart because such a page, or the
        #: containers around it, *report* off-screen: a reader that honours
        #: ``IsOffscreen`` below it may find nothing, so it must know.
        self.offscreen_page_elements: dict[int, object] = {}
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
        #: Window handle -> the page Documents its own UIA root is or directly
        #: holds, as ``(RuntimeId, name, value)`` (see :meth:`_pages_in_window`),
        #: asked once per pass.
        self.page_hosts: dict[int, list[tuple]] = {}
        try:
            self.window_title = _as_text(win32gui.GetWindowText(hwnd))
        except Exception:
            self.window_title = ""
        #: Whether ``IsOffscreen`` is believed.  Off while reading a page that
        #: reported itself off-screen inside the visible window, where only
        #: rectangles decide (see :meth:`_offscreen_pages`).
        self.trust_offscreen = True
        #: What rectangles are held to while ``trust_offscreen`` is off: the
        #: viewport narrowed by every scrolling ancestor on the current path.
        self.clip = self.viewport
        #: Reading through the raw view instead of the control view.
        self.raw = False
        self._requests: dict[tuple[str, bool, bool], object] = {}
        #: Elements that reported ``IsOffscreen`` although they cover a large
        #: part of the visible window, as ``(element, depth, window)``: where
        #: the pages of a covered window are.
        self.suspects: list[tuple[object, int, int]] = []
        #: Page Documents stacked in one window and rectangle behind the one in
        #: front (see :meth:`_page_key`), and the stacks already settled.
        self.stacked_behind: set[tuple] = set()
        self.stacks_settled: set[tuple] = set()
        #: Live elements of the pages a scan met, by :meth:`_page_key`, for a
        #: page that arrived in a cached batch without a live reference.
        self.page_live: dict[tuple, object] = {}
        #: Live UIA root of every window read, by handle (scans start there).
        self.window_roots: dict[int, object] = {}
        #: Elements that passed the visibility test, the named Text elements
        #: among them, those with nothing visible below them (leaves), and the
        #: leaves with no name and no value (see :meth:`_read_page`).
        self.nodes_seen = 0
        self.texts_seen = 0
        self.leaves_seen = 0
        self.blank_leaves = 0
        #: Page reads in progress (a page inside a page is part of it).
        self._in_page = 0

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

    def _request(self, kind: str) -> object:
        """The ``"level"`` or ``"subtree"`` request for the current view and mode.

        The control view trusting ``IsOffscreen`` is the default pair built in
        ``__init__``; the raw view (:meth:`_read_page`) and the geometry-only
        mode (:meth:`_offscreen_pages`, whose subtree batches cannot be
        filtered on ``IsOffscreen``) get their own, built once when needed.
        """
        if not self.raw and self.trust_offscreen:
            return self.level_request if kind == "level" else self.subtree_request
        key = (kind, self.raw, self.trust_offscreen)
        request = self._requests.get(key)
        if request is None:
            automation = self.automation
            view = (
                automation.RawViewCondition  # type: ignore[attr-defined]
                if self.raw
                else automation.ControlViewCondition  # type: ignore[attr-defined]
            )
            if kind == "level":
                request = _build_cache_request(
                    automation,
                    _TREE_SCOPE_CHILDREN,
                    tree_filter=view,
                    mode=_AUTOMATION_ELEMENT_MODE_FULL,
                )
            else:
                tree_filter = (
                    automation.CreateAndCondition(  # type: ignore[attr-defined]
                        view,
                        automation.CreatePropertyCondition(_P_IS_OFFSCREEN, False),  # type: ignore[attr-defined]
                    )
                    if self.trust_offscreen
                    else view
                )
                request = _build_cache_request(
                    automation,
                    _TREE_SCOPE_DESCENDANTS,
                    tree_filter=tree_filter,
                    mode=(
                        _AUTOMATION_ELEMENT_MODE_FULL
                        if self.stop_at_documents
                        else _AUTOMATION_ELEMENT_MODE_NONE
                    ),
                )
            self._requests[key] = request
        return request

    def _whole_subtree(self, get: object) -> bool:
        """Whether one filtered subtree request is safe for this element."""
        area = self.viewport if self.trust_offscreen else self.clip
        if area is None:
            return False
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        if bounds is None or not _contains(area, bounds):
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
        if not self._shown(element, get, depth, live=live):
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
        own = native or walker.window_at(depth)
        if self._background_page(get, own) or self._stacked_behind(get, own, runtime_id):
            walker.background_pages += 1
            return
        self._count(get)
        walker._add(element, depth)
        if self._stops_here(get):
            self._keep_page(element)
            return
        seen = self.nodes_seen
        live_element = (
            element
            if live
            else self.page_live.get(self._page_key(get, runtime_id)) if self.page_live else None
        )
        self._content(
            get,
            depth,
            live_element,
            lambda: self._below(element, get, depth, live=live, native=native, runtime_id=runtime_id),
        )
        if self.nodes_seen == seen:
            self._leaf(get)

    def _below(
        self,
        element: object,
        get: object,
        depth: int,
        *,
        live: bool,
        native: int,
        runtime_id: tuple[int, ...] | None,
    ) -> None:
        """Walk what is below an element :meth:`_visit` has just kept."""
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
            holder = self._fetch(element, self._request("subtree"))
            if holder is not None:
                self._walk_children(holder, depth, live=False)
        else:
            holder = self._fetch(element, self._request("level"))
            if holder is not None:
                self._walk_children(holder, depth, live=True)

    # -- facts the provider does not state reliably -------------------------
    def _shown(
        self, element: object, get: object, depth: int, *, live: bool, window: int = 0
    ) -> bool:
        """Whether the walk reads this element.

        Normally: it does not report ``IsOffscreen`` (a large one that does is
        remembered, see :attr:`suspects`).  Under a page read by geometry
        alone: its rectangle, if it has one, meets :attr:`clip`.
        """
        if self.trust_offscreen:
            if not _as_bool(get(_P_IS_OFFSCREEN)):  # type: ignore[operator]
                return True
            if live:
                self._note_suspect(element, get, depth, window or self.walker.window_at(depth))
            return False
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        return bounds is None or self.clip is None or _overlap(bounds, self.clip) is not None

    def _note_suspect(self, element: object, get: object, depth: int, window: int) -> None:
        """Remember an off-screen element that covers much of the visible window."""
        if self.viewport is None or len(self.suspects) >= _MAX_SUSPECTS:
            return
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        visible = _overlap(bounds, self.viewport) if bounds else None
        if visible is not None and _area(visible) >= _SURFACE_MIN_SHARE * _area(self.viewport):
            self.suspects.append((element, depth, window or self.hwnd))

    def _child_clip(self, get: object) -> tuple[int, int, int, int] | None:
        """:attr:`clip` for the children of an element (narrowed if it scrolls)."""
        if self.trust_offscreen:
            return self.clip
        bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        scrolls = _as_bool(get(_P_IS_SCROLL_AVAILABLE)) and (  # type: ignore[operator]
            _as_bool(get(_P_SCROLL_VERTICALLY_SCROLLABLE))  # type: ignore[operator]
            or _as_bool(get(_P_SCROLL_HORIZONTALLY_SCROLLABLE))  # type: ignore[operator]
        )
        if bounds is None or not scrolls:
            return self.clip
        return (_overlap(bounds, self.clip) if self.clip else None) or bounds

    def _count(self, get: object) -> None:
        self.nodes_seen += 1
        try:
            if get(_P_CONTROL_TYPE) == _TEXT_CONTROL_TYPE and _as_text(get(_P_NAME)):  # type: ignore[operator]
                self.texts_seen += 1
        except Exception:
            pass

    def _leaf(self, get: object) -> None:
        """Count an element with nothing visible below it (see :meth:`_read_page`)."""
        self.leaves_seen += 1
        try:
            if _as_text(get(_P_NAME)):  # type: ignore[operator]
                return
            if _as_bool(get(_P_IS_VALUE_AVAILABLE)):  # type: ignore[operator]
                value = get(_P_VALUE_VALUE)  # type: ignore[operator]
                if isinstance(value, str) and value.strip():
                    return
        except Exception:
            return
        self.blank_leaves += 1

    @staticmethod
    def _page_key(get: object, runtime_id: tuple[int, ...] | None) -> tuple:
        """A page Document's identity: its RuntimeId, else its title and address."""
        if runtime_id is not None:
            return runtime_id
        facts = _page_facts(get)
        return ("page", facts[0], facts[1]) if facts else ("page",)

    def _content(
        self,
        get: object,
        depth: int,
        live_element: object | None,
        walk: Callable[[], None],
    ) -> None:
        """Run ``walk`` (the walk below an element just kept) with the clip its
        children are held to, as a page read (:meth:`_read_page`) if it is one.

        A top-level page may use the element budget only up to
        :data:`_AFTER_PAGE_RESERVE` elements short of the cap, so the window's
        own controls that come after the page in tree order - a browser's tab
        strip - are still read when the page alone could fill the cap (seen
        live 2026-09-24: a 400-element read of Chrome ended inside the page,
        and "list my Chrome tabs" had no tabs to list).
        """
        saved = self.clip
        self.clip = self._child_clip(get)
        walker = self.walker
        saved_cap = walker.max_elements
        page = not self.stop_at_documents and _page_facts(get) is not None
        try:
            if page:
                if not self._in_page:
                    walker.max_elements = max(len(walker.elements) + 1, saved_cap - _AFTER_PAGE_RESERVE)
                self._in_page += 1
                try:
                    self._read_page(depth, live_element, walk)
                finally:
                    self._in_page -= 1
            else:
                walk()
        finally:
            self.clip = saved
            walker.max_elements = saved_cap

    def _mark(self) -> dict:
        """Everything a re-read of one page must put back (see :meth:`_read_page`)."""
        walker = self.walker
        return {
            "count": len(walker.elements),
            "seen": set(walker.seen),
            "shapes": set(walker._shapes),
            "lineage": [list(entry) for entry in walker._lineage],
            "visited": set(self.visited),
            "read_windows": set(self.read_windows),
            "pending": dict(self.pending),
            "offscreen_skipped": walker.offscreen_skipped,
            "background_pages": walker.background_pages,
            "truncated": walker.truncated,
        }

    def _restore(self, mark: dict, base: int, tail: list[UIElement]) -> None:
        walker = self.walker
        del walker.elements[base:]
        walker.elements.extend(tail)
        walker.seen = set(mark["seen"])
        walker._shapes = set(mark["shapes"])
        walker._lineage = [list(entry) for entry in mark["lineage"]]
        self.visited = set(mark["visited"])
        self.read_windows = set(mark["read_windows"])
        self.pending = dict(mark["pending"])
        walker.offscreen_skipped = mark["offscreen_skipped"]
        walker.background_pages = mark["background_pages"]
        walker.truncated = mark["truncated"]

    def _read_page(self, depth: int, live_element: object | None, walk: Callable[[], None]) -> None:
        """Walk a page's content; re-read it in the raw view if the control view
        kept its text out.

        The control view is read first (``walk``).  When that read looks like
        one that kept text leaves out (:func:`_text_hidden`: few named Text
        elements, or mostly leaves that say nothing), the page's children are
        probed with one search for a named Text element the control view
        leaves out (:meth:`_raw_probe`).  Only if that text is not in the
        control read already is the page read again through the raw view,
        from the same place in the walk, and that read is kept when the text
        it has that the control read does not comes to at least
        ``_RAW_TEXT_GAIN - 1`` times the control read's text; otherwise the
        control-view read is put back exactly.  Both reads share the pass's
        deadline and element cap (a control read that filled the cap is still
        re-read: the raw one is held to the same cap).
        """
        walker = self.walker
        before = self._mark()
        base = before["count"]
        counts = (self.nodes_seen, self.texts_seen, self.leaves_seen, self.blank_leaves)
        walk()
        nodes, texts, leaves, blank = (
            self.nodes_seen - counts[0],
            self.texts_seen - counts[1],
            self.leaves_seen - counts[2],
            self.blank_leaves - counts[3],
        )
        if (
            self.raw
            or live_element is None
            or not _text_hidden(nodes, texts, leaves, blank)
            or (self.cancel is not None and self.cancel.is_set())
            or time.monotonic() >= self.deadline
        ):
            return
        control = self._mark()
        control_tail = list(walker.elements[base:])
        known = "\n".join(f"{e.name}\n{e.value or ''}" for e in control_tail)
        wanted = (_RAW_TEXT_GAIN - 1.0) * max(len(known), 1)

        def new_text(elements: list[UIElement]) -> int:
            return sum(len(e.name) for e in elements if e.name and e.name not in known)

        if not self._raw_probe(live_element, known):
            return
        self._restore(before, base, [])
        self._raw_walk(live_element, depth, walker.max_elements)
        if new_text(walker.elements[base:]) >= wanted:
            walker.raw_pages += 1
            return
        self._restore(control, base, control_tail)

    def _raw_probe(self, live_element: object, known: str) -> bool:
        """Whether the first named Text element below ``live_element`` that the
        control view leaves out holds text not in ``known``.

        One ``FindFirst`` (it stops at the first match).  On the Gecko pages
        measured for :data:`_RAW_TEXT_GAIN` such a leaf repeats the name of a
        row or link the control read already has, so the page is not read
        twice for nothing.
        """
        if (self.cancel is not None and self.cancel.is_set()) or time.monotonic() >= self.deadline:
            return False
        automation = self.automation
        try:
            condition = automation.CreateAndCondition(  # type: ignore[attr-defined]
                automation.CreateAndCondition(  # type: ignore[attr-defined]
                    automation.CreatePropertyCondition(_P_CONTROL_TYPE, _TEXT_CONTROL_TYPE),  # type: ignore[attr-defined]
                    automation.CreatePropertyCondition(_P_IS_CONTROL_ELEMENT, False),  # type: ignore[attr-defined]
                ),
                automation.CreateNotCondition(  # type: ignore[attr-defined]
                    automation.CreatePropertyCondition(_P_NAME, "")  # type: ignore[attr-defined]
                ),
            )
            # The search runs in the view of the request's TreeFilter: raw.
            found = live_element.FindFirstBuildCache(  # type: ignore[attr-defined]
                _TREE_SCOPE_DESCENDANTS,
                condition,
                _build_cache_request(
                    automation,
                    _TREE_SCOPE_ELEMENT,
                    tree_filter=automation.RawViewCondition,  # type: ignore[attr-defined]
                ),
            )
            self.walker.fetches += 1
            if not found:
                return False
            get = found.GetCachedPropertyValue
            name = _as_text(get(_P_NAME))
            bounds = _rect(get(_P_BOUNDING_RECT))
            hidden = self.trust_offscreen and _as_bool(get(_P_IS_OFFSCREEN))
        except Exception:
            return False
        # Text the reader could not see anyway (a skip link parked off the
        # page) says nothing about what the control read missed.
        area = self.clip if not self.trust_offscreen else self.viewport
        visible = bounds is not None and not hidden and (area is None or _overlap(bounds, area) is not None)
        return bool(name) and visible and name not in known

    def _raw_walk(self, live_element: object, depth: int, cap: int) -> None:
        """Walk ``live_element``'s children through the raw view, up to ``cap`` elements."""
        walker = self.walker
        saved_cap = walker.max_elements
        walker.max_elements = cap
        self.raw = True
        try:
            holder = self._fetch(live_element, self._request("level"))
            if holder is not None:
                self._walk_children(holder, depth, live=True)
        finally:
            self.raw = False
            walker.max_elements = saved_cap

    def _stacked_behind(self, get: object, own: int, runtime_id: tuple[int, ...] | None) -> bool:
        """Whether this page Document shares its window and rectangle with
        others and is not the one in front (see the class docstring).

        Settled once per window and rectangle, by a scan of that window from
        its own root that only enters elements containing the rectangle
        (:meth:`_scan_pages`), so the cost is the few containers around the
        pages.  Not asked when a descendant window has exactly the page's
        rectangle (each page drawn in a window of its own, which
        :meth:`_background_page` settles - measured 2026-09-24 on Chromium and
        Electron windows), nor for a Document that is its window's own root
        (nothing can be stacked with it in that window).
        """
        facts = _page_facts(get)
        if facts is None:
            return False
        key = self._page_key(get, runtime_id)
        if key in self.stacked_behind:
            return True
        bounds = facts[2]
        if bounds is None or _native_handle(get):
            return False
        if any(_contains(rect, bounds) and _contains(bounds, rect) for rect in self.rects.values()):
            # A window of exactly the page's size: each page has a window of
            # its own, which the stacking test above has already settled.
            return False
        window = own or self.hwnd
        if (window, bounds) in self.stacks_settled:
            return False
        self.stacks_settled.add((window, bounds))
        root = self.window_roots.get(window)
        if root is None:
            return False
        stack = self._scan_pages([(root, 0, window)], target=bounds)
        if len(stack) < 2:
            return False
        front = self._front(stack)
        self.stacked_behind.update(page.key for page in stack if page is not front)
        return key in self.stacked_behind

    def _scan_pages(
        self,
        starts: list[tuple[object, int, int]],
        *,
        target: tuple[int, int, int, int] | None,
    ) -> list["_StackedPage"]:
        """Page Documents below ``starts``, ``IsOffscreen`` ignored.

        Breadth first through the control view, one cache request per element
        entered, never inside a Document (a page's frames are part of it).
        With ``target``, only Documents with exactly that rectangle are
        collected and only elements containing it are entered; without, every
        Document meeting the viewport, entering elements that meet it.
        Bounded by :data:`_PAGE_SCAN_DEPTH`, :data:`_PAGE_SCAN_NODES`,
        :data:`_PAGE_SCAN_S` and the pass's own deadline.
        """
        walker = self.walker
        walker.page_scans += 1
        deadline = min(self.deadline, time.monotonic() + _PAGE_SCAN_S)
        found: list[_StackedPage] = []
        frontier: list[tuple[object, int, int]] = []
        for element, depth, window in starts:
            page = self._scanned_page(element, depth, window, target, len(found))
            if page is not None:
                found.append(page)
            else:
                frontier.append((element, depth, window))
        looked = 0
        for _level in range(_PAGE_SCAN_DEPTH):
            deeper: list[tuple[object, int, int]] = []
            for element, depth, window in frontier:
                if (
                    (self.cancel is not None and self.cancel.is_set())
                    or time.monotonic() >= deadline
                    or looked >= _PAGE_SCAN_NODES
                ):
                    return found
                try:
                    holder = element.BuildUpdatedCache(self.level_request)  # type: ignore[attr-defined]
                except Exception:
                    continue
                walker.fetches += 1
                for child in _CachedWalker._cached_children(holder):
                    looked += 1
                    get = child.GetCachedPropertyValue
                    try:
                        control = get(_P_CONTROL_TYPE)
                        bounds = _rect(get(_P_BOUNDING_RECT))
                    except Exception:
                        continue
                    if control == _DOCUMENT_CONTROL_TYPE:
                        page = self._scanned_page(child, depth + 1, window, target, len(found))
                        if page is not None:
                            found.append(page)
                        continue
                    if bounds is None:
                        continue
                    if target is not None:
                        if not _contains(bounds, target):
                            continue
                    elif self.viewport is None or _overlap(bounds, self.viewport) is None:
                        continue
                    deeper.append((child, depth + 1, _native_handle(get) or window))
            frontier = deeper
            if not frontier:
                break
        return found

    def _scanned_page(
        self,
        element: object,
        depth: int,
        window: int,
        target: tuple[int, int, int, int] | None,
        order: int,
    ) -> "_StackedPage | None":
        try:
            get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
            facts = _page_facts(get)
        except Exception:
            return None  # a live root with nothing cached: not a page itself
        if facts is None or facts[2] is None:
            return None
        name, value, bounds = facts
        if target is not None:
            if not (_contains(bounds, target) and _contains(target, bounds)):
                return None
        elif self.viewport is None or _overlap(bounds, self.viewport) is None:
            return None
        key = self._page_key(get, _runtime_id(get))
        self.page_live.setdefault(key, element)
        return _StackedPage(
            element=element,
            key=key,
            name=name,
            value=value,
            bounds=bounds,
            offscreen=_as_bool(get(_P_IS_OFFSCREEN)),
            focused=_as_bool(get(_P_HAS_KEYBOARD_FOCUS)),
            depth=depth,
            window=window,
            order=order,
        )

    def _front(self, stack: list["_StackedPage"]) -> "_StackedPage":
        """The page in front among Documents stacked in one rectangle."""
        tabs = [
            element.name
            for element in self.walker.elements
            if element.role == "TabItem" and "selected" in element.states and element.name
        ]

        def rank(page: _StackedPage) -> tuple:
            named = bool(page.name) and any(tab.startswith(page.name) for tab in tabs)
            return (
                not page.offscreen,
                _title_rank(page.name, self.window_title),
                named,
                page.focused,
                -page.order,
            )

        return max(stack, key=rank)

    def _offscreen_pages(self) -> None:
        """Read the pages of a window that reports them all off-screen.

        Runs after the walk, only when it kept no page Document but dropped a
        large off-screen element inside the visible window (:attr:`suspects`).
        Those elements are scanned for page Documents meeting the viewport
        (:meth:`_scan_pages`); of each group sharing one rectangle the one in
        front is kept (:meth:`_front`), the rest counted as background pages.
        A kept page that itself reports off-screen is read by geometry alone.
        It is added below the window's root (the containers in between are
        unnamed layout, which the tree never lists), at its real depth.
        """
        walker = self.walker
        if not self.suspects or walker.out_of_budget():
            return
        if any(e.role == "Document" and is_address(e.value) for e in walker.elements):
            return
        found = self._scan_pages(self.suspects, target=None)
        stacks: dict[tuple[int, int, int, int], list[_StackedPage]] = {}
        for page in found:
            stacks.setdefault(page.bounds, []).append(page)
        for stack in stacks.values():
            if walker.out_of_budget():
                return
            front = self._front(stack)
            walker.background_pages += len(stack) - 1
            self.stacked_behind.update(page.key for page in stack if page is not front)
            element = front.element
            get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
            if self._background_page(get, front.window):
                walker.background_pages += 1
                continue
            runtime_id = _runtime_id(get)
            if runtime_id is not None:
                if runtime_id in self.visited:
                    continue
                self.visited.add(runtime_id)
            # Below the root: whatever the walk left on the path is another branch.
            del walker._lineage[1:]
            before = len(walker.elements)
            walker._add(element, front.depth, window=front.window)
            if len(walker.elements) == before:
                continue  # already listed
            walker.offscreen_pages += 1
            if self.stop_at_documents:
                self._keep_page(element, offscreen=True)
                continue
            trust, clip = self.trust_offscreen, self.clip
            self.trust_offscreen = not front.offscreen
            self.clip = self.viewport
            try:
                self._content(
                    get,
                    front.depth,
                    element,
                    lambda: self._below(
                        element,
                        get,
                        front.depth,
                        live=True,
                        native=_native_handle(get),
                        runtime_id=runtime_id,
                    ),
                )
            finally:
                self.trust_offscreen, self.clip = trust, clip

    def _stops_here(self, get: object) -> bool:
        """Whether the walk ends at this element (a Document, in a page-only pass)."""
        if not self.stop_at_documents:
            return False
        try:
            return get(_P_CONTROL_TYPE) == _DOCUMENT_CONTROL_TYPE  # type: ignore[operator]
        except Exception:
            return False

    def _keep_page(self, element: object, *, offscreen: bool = False) -> None:
        """Remember the live element of a page Document just kept."""
        walker = self.walker
        if walker.elements and walker.elements[-1].role == "Document":
            kept = self.offscreen_page_elements if offscreen else self.page_elements
            kept.setdefault(walker.elements[-1].id, element)

    def _background_page(self, get: object, own: int) -> bool:
        """Whether this element is the page of a tab that is not in front.

        A browser keeps the page of every open tab exposed, reported on screen,
        each drawn in a child window of its own; only the tab in front has its
        window on top.  So a Document whose Value is an address (a page, not an
        editor's text) is a background page when

        * its own window (``own``: the handle it was read through, else its
          nearest ancestor's) is a child window that is hidden, or its
          rectangle does not reach into the window at all; or
        * at the centre of its visible part, the child window on top in the
          window's own stacking order (``ChildWindowFromPointEx``) is not its
          own window (nor inside it, nor containing it) and holds a page that
          is a *different* Document (:meth:`_pages_in_window`; compared by
          RuntimeId, else by title and address).  That comparison is what
          settles a page the window's frame exposes without saying which child
          window draws it.

        A window on top that holds no page (a frame composited over the page)
        says nothing, and neither does a page drawn in the top-level window
        itself with nothing over it.  Window and UIA facts only - nothing here
        knows which program drew them.
        """
        try:
            if get(_P_CONTROL_TYPE) != _DOCUMENT_CONTROL_TYPE:  # type: ignore[operator]
                return False
            raw = get(_P_VALUE_VALUE) if _as_bool(get(_P_IS_VALUE_AVAILABLE)) else None  # type: ignore[operator]
            if not is_address(raw if isinstance(raw, str) else None):
                return False
            bounds = _rect(get(_P_BOUNDING_RECT))  # type: ignore[operator]
        except Exception:
            return False
        visible = _overlap(bounds, self.viewport) if bounds and self.viewport else None
        if visible is None:
            return bounds is not None and self.viewport is not None
        child = bool(own) and own != self.hwnd
        try:
            # Hidden, not cloaked: a child window's cloak state does not say
            # whether it is on screen (see :func:`child_window_handles`).
            if child and not win32gui.IsWindowVisible(own):
                return True
        except Exception:
            return True  # its window is gone
        on_top = _child_window_at(
            self.hwnd, (visible[0] + visible[2]) // 2, (visible[1] + visible[3]) // 2
        )
        if on_top == self.hwnd or (child and _related(on_top, own)):
            return False
        on_top_pages = self._pages_in_window(on_top)
        if not on_top_pages:
            return False
        runtime_id = _runtime_id(get)
        name = _as_text(get(_P_NAME))  # type: ignore[operator]
        for other_id, other_name, other_value in on_top_pages:
            if runtime_id is not None and other_id is not None:
                if other_id == runtime_id:
                    return False  # the page on top is this very Document
            elif (other_name, other_value) == (name, raw):
                return False
        return True

    def _pages_in_window(self, handle: int) -> list[tuple]:
        """The page Documents ``handle``'s own UIA root is or directly holds,
        as ``(RuntimeId or None, name, value)``."""
        if handle in self.page_hosts:
            return self.page_hosts[handle]
        pages: list[tuple] = []
        live = _element_from_handle(
            self.automation,
            handle,
            min(self.deadline, time.monotonic() + _CHILD_PATIENCE_S),
            self.cancel,
        )
        if live is not None:
            try:
                root = live.BuildUpdatedCache(self.root_request)  # type: ignore[attr-defined]
                self.walker.fetches += 1
                for candidate in [root, *_CachedWalker._cached_children(root)]:
                    get = candidate.GetCachedPropertyValue
                    if get(_P_CONTROL_TYPE) != _DOCUMENT_CONTROL_TYPE:
                        continue
                    raw = get(_P_VALUE_VALUE) if _as_bool(get(_P_IS_VALUE_AVAILABLE)) else None
                    if is_address(raw if isinstance(raw, str) else None):
                        pages.append((_runtime_id(get), _as_text(get(_P_NAME)), raw))
            except Exception:
                pages = []
        self.page_hosts[handle] = pages
        return pages

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
        self.window_roots[handle] = live
        root = self._fetch(live, self.root_request)
        if root is None:
            return False
        self.read_windows.add(handle)
        get = root.GetCachedPropertyValue  # type: ignore[attr-defined]
        if not self._shown(root, get, depth, live=True, window=handle):
            walker.offscreen_skipped += 1
            return True
        runtime_id = _runtime_id(get)
        if self._background_page(get, handle) or self._stacked_behind(get, handle, runtime_id):
            walker.background_pages += 1
            return True
        if host is not None and runtime_id == host:
            # The window's root *is* the element that led here: one element,
            # now read through its own provider.
            self._count(get)
            walker._add(root, host_depth, window=handle)  # dropped as a duplicate if already kept
            if self._stops_here(get):
                self._keep_page(live)
            else:
                self._content(
                    get, host_depth, live, lambda: self._walk_children(root, host_depth, live=True)
                )
            return True
        if runtime_id is not None:
            if runtime_id in self.visited:
                return True  # its content was already read through the parent
            self.visited.add(runtime_id)
        self._count(get)
        walker._add(root, depth, window=handle)
        if self._stops_here(get):
            self._keep_page(live)
        else:
            self._content(get, depth, live, lambda: self._walk_children(root, depth, live=True))
        return True

    def run(self) -> _CachedWalker:
        """Walk the window; raises if its own handle gives no UIA element."""
        walker = self.walker
        live_root = _element_from_handle(self.automation, self.hwnd, self.deadline, self.cancel)
        if live_root is None:
            raise RuntimeError(f"UIA would not give an element for hwnd={self.hwnd}")
        self.window_roots[self.hwnd] = live_root
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
        # No page, but large parts of the visible window called off-screen:
        # a covered window whose provider says so of everything (see the
        # class docstring).
        self._offscreen_pages()
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
    * for a ``young`` window, a pass that still leaves most of the window
      uncovered or a large child window empty (:func:`_surface_gaps`) does not
      count as settled either, however many elements it has: a page that has
      just been opened is exposed in pieces, and its frame alone is not the
      answer.  An old window keeps the short behaviour above, since a video or
      canvas that never exposes anything must not cost every read the budget.
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
    #: A later pass has replaced the first (it grew, or closed a young
    #: window's gaps): the tree is changing, so it can settle.
    changed = False
    while time.monotonic() < deadline:
        time.sleep(min(_WAKE_POLL_S, max(deadline - time.monotonic(), 0.0)))
        now = time.monotonic()
        if now >= deadline or (cancel is not None and cancel.is_set()):
            break
        grown = changed
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
        # A young window's page can replace its frame with fewer, larger
        # elements; a pass that closes the gaps beats a bigger one that does not.
        closes_gaps = young and _surface_gaps(best).any and not _surface_gaps(attempt).any
        if len(attempt.elements) > len(best.elements) or closes_gaps:
            best = adopt(attempt)
            last_growth = time.monotonic()
            changed = True
            if len(best.elements) >= max_elements:
                break  # hit the cap: more passes cannot show more
        elif (
            changed
            and (not young or len(best.elements) >= _THIN_TREE_ELEMENTS)
            and not (young and _surface_gaps(best).any)
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
    young = _window_is_fresh(hwnd)
    if not young and info is not None and info.pid:
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
            waited_s = time.perf_counter() - started
            if young and chosen.passes > 1:
                # It was watched for most of the budget: say so, so that "look
                # again" is weighed against a wait that has already happened.
                parts.append(
                    f"still so after re-reading it for {waited_s:.1f} s since it was "
                    f"opened moments ago"
                )
            note = (
                "; ".join(parts)
                + " - the rest is drawn without accessibility (video, canvas) or has "
                "not been exposed yet; look again shortly if you expected content there"
            )
    if elements and chosen.offscreen_pages:
        # Said because it changes what the reader can do: the window is on
        # screen but under others, so pointing at it means bringing it up.
        covered = (
            "the window reports its page off-screen (probably covered by other "
            "windows); the page is listed from its rectangles inside the window"
        )
        note = f"{note}; {covered}" if note else covered
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
        background_pages=chosen.background_pages,
    )


#: How long :func:`wait_for_content` waits by default for a window to expose
#: the content it was just handed (a page, a file, a folder).  On 2026-09-23 the
#: model spent two rounds looking at a browser frame whose page was not exposed
#: yet, then navigated again by hand; a page needs a second or two once its
#: window exists.  A bound on a condition, not a settle time.
CONTENT_WAIT_S = 6.0

#: How many Document elements a :class:`ContentCheck` keeps (a split view or an
#: embedded frame can show several).
_MAX_DOCUMENTS = 3


@dataclass
class ContentCheck:
    """Whether a window has exposed its content, per :func:`wait_for_content`.

    Attributes:
        ready: the content is there (see ``why``).
        why: ``"coverage"`` - leaf elements cover at least :data:`_MIN_COVERAGE`
            of the visible window and no large child window is empty (the same
            test that makes :func:`get_window_tree` call a read mostly blank);
            ``"document"`` - a Document with a title appeared that was not there
            on the first look (the page has committed, even if its body is still
            being exposed); ``""`` when not ready.
        waited_ms: time from the call to the answer.
        polls: passes read.
        answered: at least one pass came back; False means the window never
            answered accessibility queries within the budget.
        coverage: share of the visible window covered by leaf elements on the
            last pass, ``None`` when unknown.
        elements: element count of the last pass.
        empty_surfaces: large descendant/owned windows with nothing in them.
        documents: ``[{"name", "value"}]`` of the Document elements on screen in
            the last pass (value is usually the address), at most
            :data:`_MAX_DOCUMENTS`.
        skipped: set when the caller only wanted a wait for a window that shows
            a document and this one shows none: why nothing was waited for.
        error: set when the reader failed outright.
    """

    ready: bool = False
    why: str = ""
    waited_ms: float = 0.0
    polls: int = 0
    answered: bool = False
    coverage: float | None = None
    elements: int = 0
    empty_surfaces: int = 0
    documents: list[dict] = field(default_factory=list)
    skipped: str = ""
    error: str = ""

    def _document_phrase(self) -> str:
        if not self.documents:
            return "no Document exposed"
        first = self.documents[0]
        phrase = "Document"
        if first.get("name"):
            phrase += f' "{_one_line(first["name"], 80)}"'
        if first.get("value"):
            phrase += f" at {_one_line(first['value'], 120)}"
        if len(self.documents) > 1:
            phrase += f" (+{len(self.documents) - 1} more)"
        return phrase

    def _coverage_phrase(self) -> str:
        if self.coverage is None:
            return "coverage of the window unknown"
        return f"{self.coverage:.0%} of the window's visible area holds accessible elements"

    def describe(self) -> str:
        """One clause for an action summary."""
        waited = f"{self.waited_ms:.0f} ms"
        if self.error and not self.answered:
            return f"content not checked: reading the window failed ({self.error})"
        if not self.answered:
            return (
                f"content still not ready after {waited}: the window did not answer "
                f"accessibility queries (probably still loading)"
            )
        if self.skipped:
            return f"did not wait for content: {self.skipped}"
        if self.ready and self.why == "coverage":
            return (
                f"content ready after {waited} ({self._document_phrase()}; "
                f"{self._coverage_phrase()}, {self.elements} elements)"
            )
        if self.ready:
            if self.empty_surfaces or self.coverage is None:
                so_far = "its body is not exposed yet"
            else:
                so_far = f"{self._coverage_phrase()} so far, the rest may still be loading"
            return f"content ready after {waited} ({self._document_phrase()} appeared; {so_far})"
        parts = [self._document_phrase()]
        if self.coverage is not None and self.coverage < _MIN_COVERAGE:
            parts.append(f"only {self._coverage_phrase()}")
        if self.empty_surfaces:
            parts.append(f"{self.empty_surfaces} large child window(s) exposed nothing")
        return f"content still not ready after {waited} ({'; '.join(parts)})"


def _documents(elements: list[UIElement]) -> list[tuple[str, str]]:
    """``(name, value)`` of the Document elements, in tree order."""
    return [
        (element.name, element.value or "")
        for element in elements
        if element.role == "Document"
    ][:_MAX_DOCUMENTS]


def _content_verdict(
    walker: _CachedWalker, baseline: set[tuple[str, str]] | None
) -> tuple[str, _Gaps, list[tuple[str, str]]]:
    """``(why, gaps, documents)`` for one pass; ``why`` is ``""`` when not ready.

    Rectangles and UIA control types only - nothing about which application.
    A titled Document counts when it was not there on the first look
    (``baseline``); a title that is just the address - what a page reports
    before its own title arrives - does not.
    """
    elements = list(walker.elements)
    gaps = _surface_gaps(walker)
    documents = _documents(elements)
    if elements and not gaps.any:
        return "coverage", gaps, documents
    if baseline is not None:
        for name, value in documents:
            if name and name != value and (name, value) not in baseline:
                return "document", gaps, documents
    return "", gaps, documents


def wait_for_content(
    hwnd: int,
    *,
    timeout_s: float = CONTENT_WAIT_S,
    max_elements: int = 400,
    require_document: bool = False,
) -> ContentCheck:
    """Wait until a window has exposed the content it was just handed.

    Re-reads the window with the same on-screen pass as :func:`get_window_tree`
    (one pass per look, no lazy-wake polling inside it) until either the visible
    area is covered by accessible elements (at least :data:`_MIN_COVERAGE`, and
    no large child window empty - the test behind the "only N% of the window"
    note), or a titled Document appears that the first look did not have.  A
    condition poll bounded by ``timeout_s``.  Runs on its own worker thread with
    its own COM apartment; a provider that blocks past the budget is abandoned.

    Args:
        hwnd: top-level window.
        timeout_s: bound on the whole wait.
        max_elements: cap per pass.
        require_document: only wait for a window that shows a document: if the
            first pass has no Document element and no empty large child window,
            return at once with ``skipped`` set (used after opening a target
            with whatever handles it, which may be an app with no page at all).

    Returns:
        A :class:`ContentCheck`; never raises.
    """
    started = time.perf_counter()
    deadline = time.monotonic() + max(timeout_s, 0.0)
    shared: dict[str, object] = {}
    cancel = threading.Event()

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
            baseline: set[tuple[str, str]] | None = None
            polls = 0
            while not cancel.is_set():
                try:
                    walker = _collect_pass(
                        automation,
                        hwnd,
                        _surface_handles(hwnd),
                        max_elements,
                        deadline,
                        cancel=cancel,
                    )
                except Exception as exc:  # noqa: BLE001 - not answering, or gone
                    if not win32gui.IsWindow(hwnd):
                        shared["error"] = "the window closed"
                        return
                    shared["error"] = f"{type(exc).__name__}: {exc}"
                else:
                    polls += 1
                    why, gaps, documents = _content_verdict(walker, baseline)
                    if baseline is None:
                        baseline = set(documents)
                    check = ContentCheck(
                        ready=bool(why),
                        why=why,
                        polls=polls,
                        answered=True,
                        coverage=gaps.coverage,
                        elements=len(walker.elements),
                        empty_surfaces=len(gaps.empty_surfaces),
                        documents=[{"name": n, "value": v} for n, v in documents],
                    )
                    if (
                        not why
                        and require_document
                        and polls == 1
                        and not documents
                        and not gaps.empty_surfaces
                    ):
                        check.skipped = (
                            "the window shows no Document and no empty content area, "
                            "so there is no page to wait for"
                        )
                    shared["check"] = check
                    if why or check.skipped:
                        return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(_WAKE_POLL_S, remaining))
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            shared["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name=f"yuki-uia-content-{hwnd}", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0.0) + _HANDOFF_S)
    if thread.is_alive():
        cancel.set()  # a pass blocked in the provider: stop it at its next check
    found = shared.get("check")
    result = (
        ContentCheck(**{**found.__dict__, "documents": list(found.documents)})
        if isinstance(found, ContentCheck)
        else ContentCheck()
    )
    if not result.answered and shared.get("error"):
        result.error = str(shared["error"])
    result.waited_ms = (time.perf_counter() - started) * 1000.0
    return result


# ---------------------------------------------------------------------------
# Which page a window is showing
# ---------------------------------------------------------------------------
def is_address(value: str | None) -> bool:
    """Whether a Document's Value is an address (``https://...``, ``file:///...``,
    ``about:blank``) rather than document text.

    Browsers (Chromium, Edge, Firefox) expose the page URL as the Value of the
    page's Document element; an editor's Document holds its text instead.  An
    address is one token with a scheme of two or more letters (a single letter
    is a drive: ``C:\\...``).  Parsing the value's shape, not naming any app.
    """
    if not value or any(ch.isspace() for ch in value):
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return len(parts.scheme) > 1 and len(value) > len(parts.scheme) + 1


@dataclass
class PageIdentity:
    """The page a window shows, read from its Document element.

    Attributes:
        title: the Document's Name (the page title; may still be the address,
            or empty, while the page loads).
        url: the Document's Value.
        element_id: the Document's element id in the snapshot (-1: not from one).
        bounds: the Document's rectangle.
        other_pages: further top-level pages on screen (a split view, an
            owned pop-up window with a page of its own).
        selected_tab: Name of the selected TabItem (else ListItem) outside any
            page - the browser's own tab strip or sidebar.  It can lag behind
            the page; the page is the fact.
    """

    title: str
    url: str
    element_id: int = -1
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    other_pages: int = 0
    selected_tab: str = ""

    def phrase(self) -> str:
        """``"MrBeast - YouTube" https://www.youtube.com/@MrBeast/videos``."""
        if self.title and self.title != self.url:
            return f'"{_one_line(self.title, 90)}" {_one_line(self.url, 200)}'
        return f"{_one_line(self.url, 200)} (no title yet)"


#: Elements of the cap a top-level page leaves for what follows it in the
#: window (see :meth:`_ViewportPass._content`). Chrome's tab strip, toolbar
#: buttons and side panel after the page came to 20-70 elements (2026-09-24).
_AFTER_PAGE_RESERVE = 80


def _inside_document(element: UIElement, by_id: dict[int, UIElement]) -> bool:
    parent = getattr(element, "parent", -1)
    hops = 0
    while parent >= 0 and hops <= _MAX_DEPTH:
        above = by_id.get(parent)
        if above is None:
            return False
        if above.role == "Document":
            return True
        parent = getattr(above, "parent", -1)
        hops += 1
    return False


def _page_documents(elements: list[UIElement]) -> list[UIElement]:
    """Documents whose Value is an address and that are not inside another one."""
    by_id = {element.id: element for element in elements}
    return [
        element
        for element in elements
        if element.role == "Document"
        and is_address(element.value)
        and not _inside_document(element, by_id)
    ]


def pages_on_screen(
    elements: list[UIElement], window_title: str | None = None
) -> list[PageIdentity]:
    """Every top-level page in a snapshot, the visible main one first.

    A page is a Document whose Value is an address (:func:`is_address`) and
    that is not inside another Document (an embedded frame is part of its
    page).  Background tabs' pages never get into a snapshot (the walk leaves
    them out, see :meth:`_ViewportPass._background_page` and
    :meth:`_ViewportPass._stacked_behind`).  The main page is the one the
    selected tab names, when a selected tab outside the pages names one of
    them; else a titled page before an untitled one (an app's own shell page,
    drawn under the page it hosts, has no title), then the largest (ties:
    tree order).  When several pages share the main one's rectangle exactly -
    tabs stacked in one place, which rectangles cannot tell apart - the one
    whose title leads the window title goes first (a browser titles its window
    after the page in front), the selected tab settling a tie.  The selected
    tab is carried on the first.

    ``window_title``: the window's title; by default the Name of the
    snapshot's root element, which for a top-level window is its title.
    """
    by_id = {element.id: element for element in elements}
    documents = _page_documents(elements)
    if not documents:
        return []
    if window_title is None:
        root = elements[0] if elements else None
        window_title = root.name if root is not None and root.depth == 0 else ""

    def area(element: UIElement) -> int:
        left, top, right, bottom = element.bounds
        return max(right - left, 0) * max(bottom - top, 0)

    def titled(element: UIElement) -> bool:
        return bool(element.name) and element.name != element.value

    # A page with a title before one without (an app's own shell page under
    # the page it hosts has none), then the largest; ties keep tree order.
    ordered = sorted(documents, key=lambda e: (titled(e), area(e)), reverse=True)
    selected = [
        element
        for element in elements
        if element.role in ("TabItem", "ListItem")
        and "selected" in (getattr(element, "states", ()) or ())
        and element.name
        and not _inside_document(element, by_id)
    ]
    tab = next((e for e in selected if e.role == "TabItem"), None) or (
        selected[0] if selected else None
    )
    if tab is not None and len(ordered) > 1:
        # A tab's name is its page's title, sometimes with more after it
        # ("Title - Memory usage - 98 MB"); the page it names goes first.
        named = [e for e in ordered if e.name and tab.name.startswith(e.name)]
        if named:
            ordered = [named[0], *[e for e in ordered if e is not named[0]]]
        elif tab.role == "TabItem":
            # The selected tab names none of several pages: its own page is not
            # in the snapshot yet (still loading, so its window holds no page
            # and the background test above could not tell the others apart),
            # and every page found belongs to a tab behind it. Seen live
            # 2026-09-24: during a navigation Chrome's hidden tabs came through
            # and the largest (another tab's page) was taken as the one in front
            # for up to a minute of the timeline.
            return []
    elif (
        tab is not None and tab.role == "TabItem" and len(ordered) == 1 and ordered[0].name and window_title
        and not tab.name.startswith(ordered[0].name) and not _title_rank(ordered[0].name, window_title)
    ):
        # One page found, and neither the selected tab nor the window title
        # names it: it is a tab behind the one in front, whose own page is not
        # in the snapshot yet (a new tab loading). Seen live 2026-09-24 night 2:
        # a new tab opening Instagram was captured as another tab's billing page.
        return []
    stacked =[e for e in ordered if e.bounds == ordered[0].bounds]
    if len(stacked) > 1:
        # Pages in one rectangle: the window title names the one in front;
        # the selected tab (already first if it named one) breaks a tie.
        front = max(
            stacked,
            key=lambda e: (_title_rank(e.name, window_title or ""), -stacked.index(e)),
        )
        ordered = [front, *[e for e in ordered if e is not front]]
    pages = [
        PageIdentity(
            title=element.name,
            url=element.value or "",
            element_id=element.id,
            bounds=element.bounds,
        )
        for element in ordered
    ]
    pages[0].other_pages = len(pages) - 1
    pages[0].selected_tab = tab.name if tab is not None else ""
    return pages


def page_identity(
    elements: list[UIElement], window_title: str | None = None
) -> PageIdentity | None:
    """The main page of a snapshot (see :func:`pages_on_screen`), or ``None``."""
    pages = pages_on_screen(elements, window_title)
    return pages[0] if pages else None


#: ``CWP_SKIPINVISIBLE`` for ``ChildWindowFromPointEx``.  Transparent children
#: are *not* skipped: a browser's page windows are ``WS_EX_TRANSPARENT``.
_CWP_SKIPINVISIBLE = 0x1


def _child_window_at(hwnd: int, x: int, y: int) -> int:
    """The deepest visible descendant of ``hwnd`` at screen point (x, y), in
    the window's own stacking order (other top-level windows do not matter),
    or ``hwnd`` itself when no child is there."""
    current = hwnd
    for _ in range(_MAX_DEPTH):
        try:
            point = win32gui.ScreenToClient(current, (x, y))
            child = win32gui.ChildWindowFromPointEx(current, point, _CWP_SKIPINVISIBLE)
        except Exception:
            break
        if not child or child == current:
            break
        current = int(child)
    return current


def _related(a: int, b: int) -> bool:
    """``a`` and ``b`` are the same window, or one contains the other."""
    if a == b:
        return True
    try:
        return bool(win32gui.IsChild(a, b) or win32gui.IsChild(b, a))
    except Exception:
        return False


@dataclass
class PageSeen:
    """One page seen by :func:`wait_for_page`: which window, which page."""

    hwnd: int
    title: str
    url: str
    #: The window's main (largest) page, as opposed to a second one beside it.
    main: bool = True
    selected_tab: str = ""


@dataclass
class PageWait:
    """What :func:`wait_for_page` found.

    Attributes:
        matched: the first page whose address the caller accepted, or ``None``.
        seen: every page on screen in the windows read by the last poll, each
            window's main page first, windows in the order given.
        windows: the windows the last poll read.
        polls: polls made.
        answered: some window answered at all (False: none was read).
        waited_ms: time from the call to the answer.
        error: the last failure, when nothing answered.
    """

    matched: PageSeen | None = None
    seen: list[PageSeen] = field(default_factory=list)
    windows: list[int] = field(default_factory=list)
    polls: int = 0
    answered: bool = False
    waited_ms: float = 0.0
    error: str = ""


#: Per-window cap on one page-only read: the window's own frame (toolbars,
#: tabs, a sidebar) plus its Document elements, never the pages' content.
_PAGE_READ_ELEMENTS = 300

#: Longest one window's page-only read may take inside a :func:`wait_for_page`
#: poll, so a window that does not answer cannot starve the others.
_PAGE_READ_S = 1.5


def _read_pages(automation: object, hwnd: int, deadline: float, cancel: threading.Event) -> list[PageSeen]:
    """Page-only pass over one window (runs on the worker thread)."""
    viewport_pass = _ViewportPass(
        automation,
        hwnd,
        _surface_handles(hwnd),
        _PAGE_READ_ELEMENTS,
        min(deadline, time.monotonic() + _PAGE_READ_S),
        cancel=cancel,
        stop_at_documents=True,
    )
    walker = viewport_pass.run()
    return [
        PageSeen(
            hwnd=hwnd,
            title=page.title,
            url=page.url,
            main=index == 0,
            selected_tab=page.selected_tab,
        )
        for index, page in enumerate(
            pages_on_screen(list(walker.elements), viewport_pass.window_title)
        )
    ]


def wait_for_page(
    windows: Callable[[], list[int]],
    accept: Callable[[str], bool],
    *,
    timeout_s: float = CONTENT_WAIT_S,
) -> PageWait:
    """Wait until one of ``windows()`` shows a page whose address ``accept`` takes.

    Each poll calls ``windows()`` again (so a window that appears meanwhile is
    read too) and reads each window with a page-only pass: the on-screen walk of
    :func:`get_window_tree` that keeps Document elements but reads nothing
    inside them, a few dozen milliseconds for a browser window however large its
    page.  Every top-level page on screen counts, not only the main one.  A
    condition poll bounded by ``timeout_s``, on its own worker thread with its
    own COM apartment; a provider that blocks past the budget is abandoned.

    Returns:
        A :class:`PageWait`; never raises.
    """
    started = time.perf_counter()
    deadline = time.monotonic() + max(timeout_s, 0.0)
    shared: dict[str, object] = {}
    cancel = threading.Event()

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
            polls = 0
            answered = False
            while not cancel.is_set():
                try:
                    handles = [int(h) for h in windows()]
                except Exception as exc:  # noqa: BLE001 - reported, never raised
                    shared["error"] = f"listing windows failed: {type(exc).__name__}: {exc}"
                    handles = []
                seen: list[PageSeen] = []
                for handle in handles:
                    if cancel.is_set() or time.monotonic() >= deadline:
                        break
                    try:
                        pages = _read_pages(automation, handle, deadline, cancel)
                    except Exception as exc:  # noqa: BLE001 - not answering, or gone
                        shared["error"] = f"{type(exc).__name__}: {exc}"
                        continue
                    answered = True
                    seen.extend(pages)
                    hit = next((page for page in pages if accept(page.url)), None)
                    if hit is not None:
                        shared["result"] = PageWait(
                            matched=hit, seen=seen, windows=handles, polls=polls + 1,
                            answered=True,
                        )
                        return
                polls += 1
                shared["result"] = PageWait(
                    seen=seen, windows=handles, polls=polls, answered=answered
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(_WAKE_POLL_S, remaining))
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            shared["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name="yuki-uia-page", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0.0) + _HANDOFF_S)
    if thread.is_alive():
        cancel.set()  # a read blocked in a provider: stop it at its next check
    found = shared.get("result")
    result = (
        PageWait(
            matched=found.matched,
            seen=list(found.seen),
            windows=list(found.windows),
            polls=found.polls,
            answered=found.answered,
        )
        if isinstance(found, PageWait)
        else PageWait()
    )
    if not result.answered and shared.get("error"):
        result.error = str(shared["error"])
    result.waited_ms = (time.perf_counter() - started) * 1000.0
    return result


@dataclass
class PageText:
    """The text of the page a window is showing (see :func:`page_text`).

    Attributes:
        hwnd: the window read.
        title: the page's title (its Document's Name).
        url: the page's address (its Document's Value).
        text: the page's text, at most ``max_chars``.
        chars: ``len(text)``.
        truncated: the page holds more than ``max_chars`` characters.
        source: ``"text pattern"`` (the Document's whole text in one call,
            parts scrolled out of view included), ``"elements"`` (names and
            values of the Document's descendants, when it has no Text pattern),
            or ``""`` when nothing was read.
        background_pages: background tabs' pages left out while finding it.
        found: a page was found in the window at all.
        elapsed_ms: time from the call to the answer.
        note: one sentence when the result is partial or empty.
        error: set when reading failed outright.
    """

    hwnd: int
    title: str = ""
    url: str = ""
    text: str = ""
    chars: int = 0
    truncated: bool = False
    source: str = ""
    background_pages: int = 0
    found: bool = False
    elapsed_ms: float = 0.0
    note: str = ""
    error: str = ""


#: Budget for :func:`page_text`: finding the page is a page-only pass (tens of
#: milliseconds), reading its text one call that grows with the page.
PAGE_TEXT_TIMEOUT_S = 6.0

#: Elements a descendant read may collect when a page has no Text pattern.
_PAGE_TEXT_ELEMENTS = 5000


def _text_from_elements(automation: object, element: object, max_chars: int) -> tuple[str, bool]:
    """Names and values of a Document's descendants, in tree order, one per line.

    One cache request for the whole subtree, off-screen parts included.  Only
    leaves contribute their name (a link's name repeats the text inside it),
    plus every non-empty Value except a link's (its address).  Returns
    ``(text, truncated)``.

    Read through the control view; when that read looks like one that kept
    the page's text leaves out (:func:`_text_hidden`), read again through the
    raw view, which is kept if it holds at least :data:`_RAW_TEXT_GAIN` times
    the text.
    """
    text, more, counts = _element_text(automation, element, max_chars, raw=False)
    if _text_hidden(*counts):
        raw_text, raw_more, _ = _element_text(automation, element, max_chars, raw=True)
        if len(raw_text) >= _RAW_TEXT_GAIN * max(len(text), 1):
            return raw_text, raw_more
    return text, more


def _element_text(
    automation: object, element: object, max_chars: int, *, raw: bool
) -> tuple[str, bool, tuple[int, int, int, int]]:
    """``(text, truncated, (elements, named Text, leaves, blank leaves))``
    for :func:`_text_from_elements`, through the raw or the control view."""
    request = automation.CreateCacheRequest()  # type: ignore[attr-defined]
    for prop in (_P_NAME, _P_VALUE_VALUE, _P_IS_VALUE_AVAILABLE, _P_CONTROL_TYPE):
        request.AddProperty(prop)
    request.TreeScope = _TREE_SCOPE_SUBTREE
    request.TreeFilter = (
        automation.RawViewCondition  # type: ignore[attr-defined]
        if raw
        else automation.ControlViewCondition  # type: ignore[attr-defined]
    )
    request.AutomationElementMode = _AUTOMATION_ELEMENT_MODE_NONE
    root = element.BuildUpdatedCache(request)  # type: ignore[attr-defined]
    lines: list[str] = []
    size = 0
    count = 0
    texts = leaves = blank = 0
    stack = [root]
    while stack and size <= max_chars and count < _PAGE_TEXT_ELEMENTS:
        node = stack.pop()
        count += 1
        get = node.GetCachedPropertyValue
        children = _CachedWalker._cached_children(node)
        parts = []
        name = _as_text(get(_P_NAME))
        control = get(_P_CONTROL_TYPE)
        if name and control == _TEXT_CONTROL_TYPE:
            texts += 1
        if not children:
            leaves += 1
        if name and not children and node is not root:
            parts.append(name)
        # A link's Value is its address, not text on the page.
        if _as_bool(get(_P_IS_VALUE_AVAILABLE)) and control != _HYPERLINK_CONTROL_TYPE:
            value = get(_P_VALUE_VALUE)
            if isinstance(value, str) and value.strip() and value.strip() != name and node is not root:
                parts.append(value.strip())
        if not children and not name and not parts:
            blank += 1
        for part in parts:
            lines.append(part)
            size += len(part) + 1
        stack.extend(reversed(children))
    text = "\n".join(lines)
    return text[:max_chars], len(text) > max_chars or bool(stack), (count, texts, leaves, blank)


#: U+FFFC OBJECT REPLACEMENT CHARACTER: what a Text pattern puts where an
#: embedded object (an image, a button, a frame) sits in the text.
_OBJECT_REPLACEMENT = "\ufffc"


def _clean_page_text(text: str) -> str:
    """Drop object placeholders and the blank lines they leave behind."""
    lines = (line.replace(_OBJECT_REPLACEMENT, "").rstrip() for line in text.splitlines())
    return "\n".join(line for line in lines if line.strip())


def page_text(
    hwnd: int, *, max_chars: int = 30000, timeout_s: float = PAGE_TEXT_TIMEOUT_S
) -> PageText:
    """Read the whole text of the page ``hwnd`` is showing, with its title and address.

    The page is found exactly as the tree header's ``page:`` is: a page-only
    pass (Document elements kept, nothing inside them read; background tabs'
    pages left out), then the main visible page.  Its text comes from the
    Document's UIA Text pattern (``DocumentRange.GetText``) in one call, which
    covers the parts of the page scrolled out of view; a Document with no Text
    pattern falls back to the names and values of its descendants, read in one
    cache request, off-screen ones included.  At most ``max_chars`` characters
    are returned; ``truncated`` says when the page holds more.

    Runs on its own worker thread with its own COM apartment, bounded by
    ``timeout_s``; never raises.
    """
    started = time.perf_counter()
    result = PageText(hwnd=int(hwnd))
    if not win32gui.IsWindow(hwnd):
        result.error = "not a window (it may have closed)"
        return result
    deadline = time.monotonic() + max(timeout_s, 0.0)
    shared: dict[str, object] = {}
    cancel = threading.Event()
    limit = max(int(max_chars), 1)

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
            viewport_pass = _ViewportPass(
                automation,
                hwnd,
                _surface_handles(hwnd),
                _PAGE_READ_ELEMENTS,
                min(deadline, time.monotonic() + _PAGE_READ_S),
                cancel=cancel,
                stop_at_documents=True,
            )
            walker = viewport_pass.run()
            found = PageText(hwnd=int(hwnd), background_pages=walker.background_pages)
            pages = pages_on_screen(list(walker.elements), viewport_pass.window_title)
            if not pages:
                shared["result"] = found
                return
            page = pages[0]
            found.found = True
            found.title, found.url = page.title, page.url
            shared["result"] = found
            element = viewport_pass.page_elements.get(
                page.element_id
            ) or viewport_pass.offscreen_page_elements.get(page.element_id)
            if element is None:
                found.note = "the page was found but could not be asked for its text"
                return
            text = ""
            try:
                pattern = element.GetCurrentPattern(_PATTERN_TEXT)  # type: ignore[attr-defined]
                if pattern:
                    text_pattern = pattern.QueryInterface(module.IUIAutomationTextPattern)  # type: ignore[attr-defined]
                    text = str(text_pattern.DocumentRange.GetText(limit + 1) or "")
                    found.source = "text pattern"
            except Exception:
                found.source = ""
            if not found.source:
                text, more = _text_from_elements(automation, element, limit + 1)
                found.source = "elements"
                if more:
                    text = text + " "  # force the truncation flag below
            found.truncated = len(text) > limit
            found.text = _clean_page_text(text[:limit])
            found.chars = len(found.text)
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            shared["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name=f"yuki-uia-page-text-{hwnd}", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0.0) + _HANDOFF_S)
    if thread.is_alive():
        cancel.set()
    found = shared.get("result")
    if isinstance(found, PageText):
        result = PageText(**found.__dict__)
    if shared.get("error"):
        result.error = str(shared["error"])
    if thread.is_alive() and not result.text:
        result.note = (
            f"the window did not hand over the page text within {timeout_s:g} s "
            f"(probably still loading); try again shortly"
        )
    elif not result.found and not result.error:
        result.note = "no page is showing in this window (no Document with an address on screen)"
    elif result.found and not result.text and not result.note:
        result.note = "the page exposes no text"
    elif result.truncated:
        result.note = f"clipped at {limit} characters; the page holds more"
    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return result


def format_page_text(result: PageText) -> str:
    """Text rendering for the model: one header line, then the page text."""
    facts = []
    if result.found:
        page = PageIdentity(title=result.title, url=result.url)
        facts.append(f"page: {page.phrase()}")
    facts.append(f"window {result.hwnd}")
    if result.found:
        via = f" via {result.source}" if result.source else ""
        facts.append(f"{result.chars} chars{via} in {result.elapsed_ms:.0f} ms")
    if result.background_pages:
        facts.append(f"{result.background_pages} background tab page(s) skipped")
    if result.note:
        facts.append(f"note: {result.note}")
    if result.error:
        facts.append(f"error: {result.error}")
    header = " | ".join(facts)
    return header + ("\n" + result.text if result.text else "")


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

#: Deepest indentation :func:`format_window_tree` draws.
_FORMAT_MAX_INDENT = 12

#: Shorter spellings for role names in the text the model reads.  Only where the
#: short form cannot be mistaken for another UIA control type; the structured
#: :class:`UIElement` keeps the full name.
_FORMAT_ROLE = {"Hyperlink": "Link"}


def _one_line(text: str, limit: int) -> str:
    """Flatten newlines/tabs and clip, so one element stays one line."""
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    flat = flat.replace("\t", " ")
    return flat[:limit] + "…" if len(flat) > limit else flat


def _listed(element: UIElement) -> bool:
    """Whether the text rendering shows ``element`` at all."""
    return bool(element.name) or element.value is not None or element.is_interactive


def _has_marks(element: UIElement) -> bool:
    """Whether the element says more than a role, a name and a position.

    ``is_interactive`` alone does not count: Chromium gives almost every element
    under a clickable one a default action, so a list item or a table cell that
    wraps a link reads as interactive while the link is what a click would use.
    """
    return bool(
        element.value is not None
        or getattr(element, "patterns", ())
        or element.is_scrollable
        or element.is_focused
        or element.shortcut
        or getattr(element, "states", ())
    )


def _parents(
    elements: list[UIElement], everything: list[UIElement] | None = None
) -> list[int]:
    """Index (into ``elements``) of each element's nearest ancestor among them, or -1.

    Uses :attr:`UIElement.parent` when the snapshot has it (``everything`` is the
    whole snapshot, so a parent that is not in ``elements`` is looked through to
    its own parent).  A snapshot without it (read from an older log) falls back
    to depth order, which cannot tell a child from the first element of a
    neighbouring branch whose container was not kept.
    """
    everything = everything if everything is not None else elements
    if any(getattr(element, "parent", -1) >= 0 for element in everything):
        by_id = {element.id: element for element in everything}
        position = {element.id: index for index, element in enumerate(elements)}
        parents: list[int] = []
        for element in elements:
            parent = getattr(element, "parent", -1)
            hops = 0
            while parent >= 0 and parent not in position and hops <= _MAX_DEPTH:
                above = by_id.get(parent)
                parent = getattr(above, "parent", -1) if above is not None else -1
                hops += 1
            parents.append(position.get(parent, -1))
        return parents
    parents = []
    stack: list[int] = []
    for index, element in enumerate(elements):
        while stack and elements[stack[-1]].depth >= element.depth:
            stack.pop()
        parents.append(stack[-1] if stack else -1)
        stack.append(index)
    return parents


#: Two click points this close (px) are the same place for :func:`_drop_redundant`.
_SAME_SPOT_PX = 4


def _same_spot(a: UIElement, b: UIElement) -> bool:
    return (
        abs(a.center[0] - b.center[0]) <= _SAME_SPOT_PX
        and abs(a.center[1] - b.center[1]) <= _SAME_SPOT_PX
    )


def _mark_set(element: UIElement) -> set[str]:
    """Patterns, states and flags of an element, as one set to compare."""
    marks = set(getattr(element, "patterns", ()) or ()) | set(getattr(element, "states", ()) or ())
    if element.is_focused:
        marks.add("focused")
    if element.is_scrollable:
        marks.add("scrollable")
    if element.shortcut:
        marks.add("kb:" + element.shortcut)
    return marks


def _drop_redundant(
    elements: list[UIElement], everything: list[UIElement] | None = None
) -> list[UIElement]:
    """Leave out elements whose line would only repeat a neighbour's.

    Three shapes, all judged on names, values, patterns and click points - never
    on which application drew them:

    * **an echo of a name**: a plain element (no patterns, value, state or
      shortcut - see :func:`_has_marks`) with the same name as the element
      right after it, when that one is its child or next sibling and carries
      more (a list item or table cell wrapping a link of the same name), or as
      the element kept just before it, when that is its parent or previous
      sibling (a label repeating the pane it is in);
    * **a duplicate on the same spot**: an element with the same name and click
      point as its parent or previous sibling, whose value, patterns and state
      add nothing to that element's (a link wrapped in a link of the same name);
    * **an unnamed wrapper**: an element with no name, value, state or
      shortcut whose first child can be acted on at the same click point (the
      clickable group around a button).

    The element that stays has the name and the click point, and one that can
    be acted on only gives way to another that can, at the same place or
    carrying the same name.
    """
    parents = _parents(elements, everything)
    dropped = [False] * len(elements)
    kept_index: list[int] = []

    def related(index: int, other: int) -> bool:
        """``other`` is ``index``'s parent or sibling, looking through elements
        already left out between them."""
        parent = parents[index]
        while parent >= 0 and dropped[parent] and parent != other:
            parent = parents[parent]
        if parent == other:
            return True
        other_parent = parents[other]
        while other_parent >= 0 and dropped[other_parent]:
            other_parent = parents[other_parent]
        return other_parent == parent

    for index, element in enumerate(elements):
        following = elements[index + 1] if index + 1 < len(elements) else None
        follows_inside = following is not None and (
            parents[index + 1] == index or parents[index + 1] == parents[index]
        )
        last = kept_index[-1] if kept_index else -1
        previous = elements[last] if last >= 0 else None
        drop = False
        if element.name and not _has_marks(element):
            if follows_inside and following.name == element.name and _has_marks(following):
                drop = True
            elif (
                previous is not None
                and previous.name == element.name
                and related(index, last)
                and (not element.is_interactive or previous.is_interactive)
            ):
                drop = True
        if (
            not drop
            and element.name
            and previous is not None
            and previous.name == element.name
            and _same_spot(element, previous)
            and element.value in (None, previous.value)
            and _mark_set(element) <= _mark_set(previous)
            and (not element.is_interactive or previous.is_interactive)
            and related(index, last)
        ):
            drop = True
        if (
            not drop
            and not element.name
            and element.value is None
            and not (_mark_set(element) - set(getattr(element, "patterns", ()) or ()))
            and following is not None
            and parents[index + 1] == index
            and (following.is_interactive or getattr(following, "patterns", ()))
            and set(getattr(element, "patterns", ()) or ()) <= _mark_set(following)
            and _same_spot(element, following)
        ):
            drop = True
        if drop:
            dropped[index] = True
            continue
        kept_index.append(index)
    return [elements[index] for index in kept_index]


def _origin(address: str) -> str:
    """``scheme://host`` of an address, or ``""`` when it has no host."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(address)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def format_window_tree(tree: WindowTree) -> str:
    """Compact text rendering of a window tree, one line per element.

    The header is one line.  When the window shows a page (a Document whose
    Value is an address, see :func:`page_identity`) it starts with
    ``page: "<title>" <url>`` and, when there is one outside the page, the
    selected tab (``selected tab: "..."``); then the window, the element count,
    how long the read took, and - when there are any - how the tree was built,
    its status, that it is partial, what was left off screen, and the note.

    Element lines: ``[id] Role "name" value="..." @(x,y) {patterns} [kb: key]
    [flags]``.  Indentation is nesting among the listed elements (layout
    padding that is not listed does not indent).  An interactive element's
    supported patterns follow its position in braces, ``{invoke,expand}``, so
    one a click activates (``invoke``/``toggle``) can be told from one a click
    only selects (``select``).  State follows in brackets: ``selected``,
    ``on``/``off``/``mixed``, ``expanded``/``collapsed``, ``focused``,
    ``scrollable``.

    To keep it short without losing what can be used:

    * elements with no name, no value and nothing to do are skipped (layout);
    * a plain element that only repeats the name of the element next to it is
      left out (see :func:`_drop_redundant`);
    * a value that equals the name is not repeated, and an address on the same
      host as the page it is in is written from its path (``/watch?v=...``);
    * the click point ``@(x,y)`` is given for everything that can be acted on
      or carries a value or state, and for every leaf of its own; it is left
      off a plain container (aim at what is inside it) and off a plain label
      whose parent is a control that can be acted on and smaller than half the
      window each way (aim at the control);
    * consecutive unnamed siblings that are identical but for position are one
      line: ``Button ×3 {invoke}: [9]@(242,17) [10]@(892,17) [11]@(966,17)``;
    * ``Hyperlink`` is written ``Link``.
    """
    status = getattr(tree, "status", "ok") or "ok"
    note = getattr(tree, "note", "") or ""
    facts: list[str] = []
    page = page_identity(tree.elements, tree.title or None)
    if page is not None:
        # The page is the fact about what the window shows: a browser's own
        # window title can stay the same whatever page is on screen, and its
        # tab names can lag behind the page.
        facts.append(
            f"page: {page.phrase()}"
            + (f" (+{page.other_pages} other page(s) on screen)" if page.other_pages else "")
        )
        if page.selected_tab:
            facts.append(f'selected tab: "{_one_line(page.selected_tab, _FORMAT_NAME_CHARS)}"')
    # The window's own tabs, counted: tab controls inside a page (a site's
    # "Browse | Chat" switch) are not among them, and a long list is easy to miscount.
    by_id = {element.id: element for element in tree.elements}
    own_tabs = sum(
        1 for element in tree.elements if element.role == "TabItem" and not _inside_document(element, by_id)
    )
    if own_tabs >= 2:
        facts.append(f"{own_tabs} tabs outside the page" if page is not None else f"{own_tabs} tabs")
    facts += [
        f"window {tree.hwnd} \"{tree.title}\" ({tree.process_name or 'unknown'})",
        f"{len(tree.elements)} elements in {tree.elapsed_ms:.0f} ms",
    ]
    if tree.passes > 1 and 0 <= tree.first_pass_elements < len(tree.elements):
        # The tree was being built while it was read, which is worth saying: the
        # app had only just been opened or navigated, and a second look may show
        # more than this one did.
        facts.append(
            f"built lazily: grew from {tree.first_pass_elements} over {tree.passes} "
            f"passes, read again if something you expect is missing"
        )
    if status != "ok":
        facts.append(f"status: {status}")
    if tree.truncated:
        facts.append("TRUNCATED: partial tree")
    skipped = getattr(tree, "offscreen_skipped", 0) or 0
    if skipped:
        # Said so the reader knows the list is the visible part on purpose:
        # scrolling brings the rest into view (and into the next read).
        facts.append(f"on-screen only: {skipped} off-screen branch(es) not read")
    background = getattr(tree, "background_pages", 0) or 0
    if background:
        facts.append(f"{background} background tab page(s) skipped")
    if note:
        facts.append(f"note: {note}")
    lines = [" | ".join(facts)]

    listed = _drop_redundant(
        [element for element in tree.elements if _listed(element)], tree.elements
    )
    parents = _parents(listed, tree.elements)
    has_child = [False] * len(listed)
    for parent in parents:
        if parent >= 0:
            has_child[parent] = True

    # Per element: listed nesting depth, and the origin of the page (Document)
    # it is in.
    levels: list[int] = []
    origins: list[str] = []
    relative_used = False
    for index, element in enumerate(listed):
        parent = parents[index]
        levels.append(levels[parent] + 1 if parent >= 0 else 0)
        if element.role == "Document" and element.value:
            origins.append(_origin(element.value))
        else:
            origins.append(origins[parent] if parent >= 0 else "")

    def point(element: UIElement) -> str:
        return f"@({element.center[0]},{element.center[1]})"

    frame = tree.elements[0].bounds if tree.elements else None

    def small_target(element: UIElement) -> bool:
        """Less than half the window each way: a control, not a page region."""
        if frame is None:
            return False
        left, top, right, bottom = element.bounds
        return (right - left) * 2 < frame[2] - frame[0] and (bottom - top) * 2 < frame[3] - frame[1]

    def wants_point(index: int) -> bool:
        element = listed[index]
        if element.is_interactive or _has_marks(element):
            return True
        if has_child[index]:
            return False  # a plain container: aim at what is inside it
        parent = parents[index]
        # A label inside a control that can be acted on: the control's own
        # click point is the one to use.
        return not (
            parent >= 0
            and getattr(listed[parent], "patterns", ())
            and small_target(listed[parent])
        )

    def marks(element: UIElement) -> list[str]:
        parts: list[str] = []
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
        return parts

    def run_key(index: int) -> tuple | None:
        """What an unnamed leaf looks like apart from its id and position."""
        element = listed[index]
        if element.name or element.value is not None or has_child[index]:
            return None
        return (parents[index], element.role, tuple(marks(element)), wants_point(index))

    index = 0
    while index < len(listed):
        element = listed[index]
        indent = " " * min(levels[index], _FORMAT_MAX_INDENT)
        role = _FORMAT_ROLE.get(element.role, element.role)
        key = run_key(index)
        end = index + 1
        if key is not None:
            while end < len(listed) and run_key(end) == key:
                end += 1
        if end - index > 1:
            members = listed[index:end]
            parts = [f"{indent}{role} ×{len(members)}", *marks(element)]
            where = " ".join(
                f"[{member.id}]{point(member)}" if key[3] else f"[{member.id}]"
                for member in members
            )
            lines.append(" ".join(parts) + ": " + where)
            index = end
            continue
        parts = [f"{indent}[{element.id}] {role}"]
        if element.name:
            parts.append(f'"{_one_line(element.name, _FORMAT_NAME_CHARS)}"')
        if element.value is not None and element.value != element.name:
            value = element.value
            origin = origins[parents[index]] if parents[index] >= 0 else ""
            if (
                element.role != "Document"
                and origin
                and value.startswith(origin)
                and value[len(origin) : len(origin) + 1] in ("/", "?", "#")
            ):
                value = value[len(origin) :]
                relative_used = True
            parts.append(f'value="{_one_line(value, _FORMAT_VALUE_CHARS)}"')
        if wants_point(index):
            parts.append(point(element))
        parts.extend(marks(element))
        lines.append(" ".join(parts))
        index += 1
    if relative_used:
        lines[0] += (
            " | values starting with / are addresses on the same host as the "
            "Document they are in"
        )
    if not listed and status != "busy":  # a busy window's note already says why
        lines.append("(no named or interactive elements - UIA exposes nothing usable here)")
    return "\n".join(lines)
