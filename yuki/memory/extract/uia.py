"""Read-only structured UI Automation read of one window, for extraction.

:func:`yuki.perception.tree.get_window_tree` flattens a window for the model:
it keeps only elements with a name/value/pattern (so the unnamed ``<div
class="c-message_list">`` that *is* the message list disappears) and caches no
ClassName, AutomationId, ARIA role or landmark.  Extraction needs exactly those
facts, and the real parent/child structure, so this module reads them itself -
reusing perception's page finder for the part it is good at (which page a
browser window is showing, through the renderer's own window handle, with
background tabs of Chromium browsers left out).

The read, all on a worker thread with its own COM apartment, bounded by
``timeout_s``:

1. **Find the page.**  A page-only pass (:class:`yuki.perception.tree._ViewportPass`
   with ``stop_at_documents``) reads the window frame and keeps every page
   Document it meets without reading inside them.  Gecko (Firefox, Zen) needs a
   second look: its native UIA provider reports *every* Document - the tab on
   screen included - as off-screen while the window is covered by other
   windows, and keeps all tabs' Documents in the one top-level HWND with the
   same rectangle, so a pass that trusts ``IsOffscreen`` finds no page at all
   and child-window facts cannot tell tabs apart (measured 2026-09-24: ten
   Documents, all ``IsOffscreen``, the selected one differing only by MSAA
   STATE_FOCUSED).  When the first pass found no page, a shallow scan that
   ignores ``IsOffscreen`` lists the Documents instead, and the main page is
   chosen from facts: not off-screen, then its title leading the window title
   (a browser titles its window after the page in front), then keyboard focus,
   then area, then tree order.
2. **Read the content** of the main page (or of the whole window when there is
   no page) as a tree of :class:`~yuki.memory.extract.model.Node`, only what
   is visible: an element wholly inside the visible clip and not a scroll
   container is fetched with its whole subtree in one cache request; anything
   else one level at a time, so off-screen branches (a virtualised list's
   scrolled-away rows) are never visited.  Visibility is geometry (the
   element's rectangle against the window and every scrolling ancestor) plus
   ``IsOffscreen`` - except under a page that reports itself off-screen (the
   covered Gecko window above), where only geometry counts.  Covering a
   Chromium window does not make its rows report ``IsOffscreen`` (measured
   2026-09-24 on covered WhatsApp and Discord windows: every row read; the few
   in-view elements flagged off-screen were visually hidden labels and
   layers); a *minimized* window has no on-screen rectangle, so there
   ``IsOffscreen`` is all there is (the watcher reads only the foreground
   window, never a minimized one).
3. **Hidden labels** (``hidden_labels=True``, a profile's choice): an element
   reporting ``IsOffscreen`` while wholly inside the visible area is a
   screen-reader-only element - Chromium flags visually hidden text so, e.g.
   WhatsApp's "<sender>:" bubble labels.  It is then kept as a leaf marked
   ``sr_only`` (its subtree not read): anchors may use its name, text
   extraction never does.

Nothing here sends input, focuses, or changes anything: UIA property reads
only.  Password fields' values are never kept.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import comtypes
import comtypes.client
import win32gui

from yuki.memory.extract.model import Node, Rect, Snapshot, number

# Perception internals reused on purpose (see the module docstring).  Kept to
# this one import block so a change there has one place to be followed here.
from yuki.perception import tree as _tree
from yuki.perception.windows import virtual_screen_bounds, window_info

# ---------------------------------------------------------------------------
# UIA property ids (UIAutomationClient.h)
# ---------------------------------------------------------------------------
P_RUNTIME_ID = 30000
P_BOUNDS = 30001
P_CONTROL_TYPE = 30003
P_LOCALIZED_TYPE = 30004
P_NAME = 30005
P_HAS_FOCUS = 30008
P_AUTOMATION_ID = 30011
P_CLASS_NAME = 30012
P_HELP_TEXT = 30013
P_IS_PASSWORD = 30019
P_NATIVE_HWND = 30020
P_IS_OFFSCREEN = 30022
P_ITEM_STATUS = 30026
P_IS_INVOKE = 30031
P_IS_SCROLL = 30034
P_IS_SELECTION_ITEM = 30036
P_IS_TEXT = 30040
P_IS_VALUE = 30043
P_VALUE = 30045
P_SCROLL_H = 30057
P_SCROLL_V = 30058
P_IS_SELECTED = 30079
P_ARIA_ROLE = 30101
P_ARIA_PROPS = 30102
P_POS_IN_SET = 30152
P_SIZE_OF_SET = 30153
P_LEVEL = 30154
P_LANDMARK = 30157
P_LOCALIZED_LANDMARK = 30158
P_FULL_DESCRIPTION = 30159
P_HEADING_LEVEL = 30173

_PROPERTIES = (
    P_RUNTIME_ID, P_BOUNDS, P_CONTROL_TYPE, P_LOCALIZED_TYPE, P_NAME, P_HAS_FOCUS,
    P_AUTOMATION_ID, P_CLASS_NAME, P_HELP_TEXT, P_IS_PASSWORD, P_NATIVE_HWND,
    P_IS_OFFSCREEN, P_ITEM_STATUS, P_IS_INVOKE, P_IS_SCROLL, P_IS_SELECTION_ITEM,
    P_IS_TEXT, P_IS_VALUE, P_VALUE, P_SCROLL_H, P_SCROLL_V, P_IS_SELECTED,
    P_ARIA_ROLE, P_ARIA_PROPS, P_POS_IN_SET, P_SIZE_OF_SET, P_LEVEL, P_LANDMARK,
    P_LOCALIZED_LANDMARK, P_FULL_DESCRIPTION, P_HEADING_LEVEL,
)

_PATTERN_TEXT = 10014
_DOCUMENT = 50030
_SCOPE_ELEMENT, _SCOPE_CHILDREN, _SCOPE_DESCENDANTS = 1, 2, 4
_MODE_NONE, _MODE_FULL = 0, 1

#: LandmarkType ids -> names (UIA_*LandmarkTypeId).  Custom (80000) landmarks
#: take their LocalizedLandmarkType instead ("banner", "complementary", ...).
_LANDMARKS = {80001: "form", 80002: "main", 80003: "navigation", 80004: "search"}
_HEADING_BASE = 80050  # HeadingLevel_None; Level1..9 = 80051..80059

_CUIAUTOMATION8 = "{e22ad333-b25f-460c-83d0-0581107395c9}"
_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

# ---------------------------------------------------------------------------
# Budgets (plumbing)
# ---------------------------------------------------------------------------

#: Default wall-clock budget for one structured read.
READ_TIMEOUT_S = 1.5
#: Nodes kept per read; a chat or inbox on screen is a few hundred to ~2000.
MAX_NODES = 4000
#: Frame elements the page-finding pass may read.
_FRAME_ELEMENTS = 300
#: Longest the page-finding pass may take.
_FRAME_S = 0.8
#: Shallow Document scan (the Gecko case): levels and nodes.
_SCAN_DEPTH = 8
_SCAN_NODES = 500
#: Values kept per element; Edit/Document values (an editor's whole buffer) get more.
_VALUE_CHARS = 1000
_DOCUMENT_VALUE_CHARS = 30000
#: Below this many characters of element text, the page's Text pattern is read too.
_THIN_TEXT = 200
_TEXT_PATTERN_CHARS = 30000
_HANDOFF_S = 0.15


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bool(value: object) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value != 0)


def _rect(value: object) -> Rect | None:
    """Cached BoundingRectangle (left, top, width, height) -> (l, t, r, b)."""
    try:
        left, top, width, height = (int(v) for v in value)  # type: ignore[union-attr]
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def _overlap(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None or b is None:
        return None
    left, top, right, bottom = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _inside(outer: Rect, inner: Rect, slack: int = 2) -> bool:
    return (
        outer[0] <= inner[0] + slack
        and outer[1] <= inner[1] + slack
        and outer[2] >= inner[2] - slack
        and outer[3] >= inner[3] - slack
    )


def _runtime_id(value: object) -> tuple[int, ...] | None:
    try:
        rid = tuple(int(v) for v in value) if value is not None else ()  # type: ignore[union-attr]
    except Exception:
        return None
    return rid or None


def _cached_children(element: object) -> list[object]:
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
    out = []
    for i in range(count):
        try:
            out.append(array.GetElement(i))
        except Exception:
            break
    return out


def _node_from(get: object) -> Node:
    """A Node from a cached element's ``GetCachedPropertyValue``."""
    control = get(P_CONTROL_TYPE)  # type: ignore[operator]
    role = _tree._role_name(control)
    is_password = _bool(get(P_IS_PASSWORD))  # type: ignore[operator]
    value: str | None = None
    if _bool(get(P_IS_VALUE)) and not is_password:  # type: ignore[operator]
        raw = get(P_VALUE)  # type: ignore[operator]
        if isinstance(raw, str) and raw.strip():
            limit = _DOCUMENT_VALUE_CHARS if role in ("Edit", "Document") else _VALUE_CHARS
            value = raw[:limit]
    landmark_id = _int(get(P_LANDMARK))  # type: ignore[operator]
    landmark = _LANDMARKS.get(landmark_id, "")
    if not landmark and landmark_id:
        landmark = _text(get(P_LOCALIZED_LANDMARK)).lower()  # type: ignore[operator]
    heading_raw = _int(get(P_HEADING_LEVEL))  # type: ignore[operator]
    heading = heading_raw - _HEADING_BASE if _HEADING_BASE < heading_raw <= _HEADING_BASE + 9 else 0
    scrollable = _bool(get(P_IS_SCROLL)) and (  # type: ignore[operator]
        _bool(get(P_SCROLL_V)) or _bool(get(P_SCROLL_H))  # type: ignore[operator]
    )
    return Node(
        role=role,
        name=_text(get(P_NAME)),  # type: ignore[operator]
        value=value,
        bounds=_rect(get(P_BOUNDS)),  # type: ignore[operator]
        class_name=_text(get(P_CLASS_NAME)),  # type: ignore[operator]
        automation_id=_text(get(P_AUTOMATION_ID)),  # type: ignore[operator]
        aria_role=_text(get(P_ARIA_ROLE)).lower(),  # type: ignore[operator]
        aria_props=_text(get(P_ARIA_PROPS)),  # type: ignore[operator]
        landmark=landmark,
        help=_text(get(P_HELP_TEXT)),  # type: ignore[operator]
        description=_text(get(P_FULL_DESCRIPTION)),  # type: ignore[operator]
        localized_type=_text(get(P_LOCALIZED_TYPE)),  # type: ignore[operator]
        heading=heading,
        level=_int(get(P_LEVEL)),  # type: ignore[operator]
        pos_in_set=_int(get(P_POS_IN_SET)),  # type: ignore[operator]
        size_of_set=_int(get(P_SIZE_OF_SET)),  # type: ignore[operator]
        is_password=is_password,
        is_scrollable=scrollable,
        is_focused=_bool(get(P_HAS_FOCUS)),  # type: ignore[operator]
        is_selected=_bool(get(P_IS_SELECTION_ITEM)) and _bool(get(P_IS_SELECTED)),  # type: ignore[operator]
        is_invokable=_bool(get(P_IS_INVOKE)),  # type: ignore[operator]
        offscreen=_bool(get(P_IS_OFFSCREEN)),  # type: ignore[operator]
        item_status=_text(get(P_ITEM_STATUS)),  # type: ignore[operator]
        runtime_id=_runtime_id(get(P_RUNTIME_ID)),  # type: ignore[operator]
    )


def _request(automation: object, scope: int, mode: int, *, raw: bool = False) -> object:
    request = automation.CreateCacheRequest()  # type: ignore[attr-defined]
    for prop in _PROPERTIES:
        try:
            request.AddProperty(prop)
        except Exception:
            pass  # an older UIA without that property: it reads as absent
    request.TreeScope = scope
    request.TreeFilter = (
        automation.RawViewCondition if raw else automation.ControlViewCondition  # type: ignore[attr-defined]
    )
    request.AutomationElementMode = mode
    return request


def _automation() -> tuple[object, object]:
    """``(module, IUIAutomation)`` for this thread; transaction timeouts when available."""
    module = _tree._uia_core()
    try:
        automation = comtypes.client.CreateObject(_CUIAUTOMATION8, interface=module.IUIAutomation2)
        automation.ConnectionTimeout = 1000
        automation.TransactionTimeout = 1000
    except Exception:
        automation = comtypes.client.CreateObject(_CUIAUTOMATION, interface=module.IUIAutomation)
    return module, automation


# ---------------------------------------------------------------------------
# Content walk
# ---------------------------------------------------------------------------


class _Walk:
    """Visible-only structured walk below one live element (see module docstring)."""

    def __init__(
        self,
        automation: object,
        *,
        deadline: float,
        cancel: threading.Event,
        max_nodes: int,
        viewport: Rect | None,
        honor_offscreen: bool,
        window_rects: list[Rect],
        raw: bool = False,
        hidden_labels: bool = False,
    ) -> None:
        self.hidden_labels = hidden_labels
        self.deadline = deadline
        self.cancel = cancel
        self.max_nodes = max_nodes
        self.viewport = viewport
        self.honor_offscreen = honor_offscreen
        self.window_rects = window_rects
        self.count = 0
        self.truncated = False
        self.fetches = 0
        self.seen: set[tuple[int, ...]] = set()
        self.element_req = _request(automation, _SCOPE_ELEMENT | _SCOPE_CHILDREN, _MODE_FULL, raw=raw)
        self.level_req = _request(automation, _SCOPE_CHILDREN, _MODE_FULL, raw=raw)
        self.subtree_req = _request(automation, _SCOPE_DESCENDANTS, _MODE_NONE, raw=raw)

    def out(self) -> bool:
        if self.count >= self.max_nodes or self.cancel.is_set() or time.monotonic() >= self.deadline:
            self.truncated = True
            return True
        return False

    def _fetch(self, element: object, request: object) -> object | None:
        if self.out():
            return None
        try:
            holder = element.BuildUpdatedCache(request)  # type: ignore[attr-defined]
        except Exception:
            self.truncated = True
            return None
        self.fetches += 1
        return holder

    def _visible(self, node: Node, clip: Rect | None) -> bool:
        if self.honor_offscreen and node.offscreen:
            # Wholly inside the visible area yet "off-screen": visually hidden
            # (screen-reader-only), kept as a leaf when asked; else it is gone.
            if (
                self.hidden_labels
                and node.bounds is not None
                and clip is not None
                and _inside(clip, node.bounds, slack=0)
            ):
                node.sr_only = True
                return True
            return False
        if node.bounds is None or clip is None:
            return True  # no rectangle: structure only, its children decide
        return _overlap(node.bounds, clip) is not None

    def _whole(self, node: Node, clip: Rect | None) -> bool:
        if node.bounds is None or clip is None or node.is_scrollable:
            return False
        if not _inside(clip, node.bounds):
            return False
        return not any(_inside(node.bounds, rect) for rect in self.window_rects)

    def run(self, live: object) -> Node | None:
        holder = self._fetch(live, self.element_req)
        if holder is None:
            return None
        root = _node_from(holder.GetCachedPropertyValue)  # type: ignore[attr-defined]
        if root.runtime_id:
            self.seen.add(root.runtime_id)
        self.count += 1
        clip = _overlap(root.bounds, self.viewport) if root.bounds else self.viewport
        self._live_children(holder, root, clip)
        return root

    def _child_clip(self, node: Node, clip: Rect | None) -> Rect | None:
        if node.is_scrollable and node.bounds is not None:
            return _overlap(node.bounds, clip) if clip else node.bounds
        return clip

    def _keep(self, node: Node) -> bool:
        if node.runtime_id:
            if node.runtime_id in self.seen:
                return False
            self.seen.add(node.runtime_id)
        self.count += 1
        return True

    def _live_children(self, holder: object, parent: Node, clip: Rect | None) -> None:
        for child in _cached_children(holder):
            if self.out():
                return
            node = _node_from(child.GetCachedPropertyValue)  # type: ignore[attr-defined]
            if not self._visible(node, clip) or not self._keep(node):
                continue
            parent.children.append(node)
            if node.sr_only:
                continue
            inner = self._child_clip(node, clip)
            if self._whole(node, clip):
                sub = self._fetch(child, self.subtree_req)
                if sub is not None:
                    self._cached_children_of(sub, node, inner)
            else:
                sub = self._fetch(child, self.level_req)
                if sub is not None:
                    self._live_children(sub, node, inner)

    def _cached_children_of(self, holder: object, parent: Node, clip: Rect | None) -> None:
        for child in _cached_children(holder):
            if self.count >= self.max_nodes:
                self.truncated = True
                return
            node = _node_from(child.GetCachedPropertyValue)  # type: ignore[attr-defined]
            if not self._visible(node, clip) or not self._keep(node):
                continue
            parent.children.append(node)
            if not node.sr_only:
                self._cached_children_of(child, node, self._child_clip(node, clip))


# ---------------------------------------------------------------------------
# Finding the page
# ---------------------------------------------------------------------------


@dataclass
class _PageCandidate:
    live: object
    name: str
    value: str
    bounds: Rect | None
    offscreen: bool
    focused: bool
    order: int


def _scan_documents(automation: object, root: object, deadline: float, cancel: threading.Event) -> list[_PageCandidate]:
    """Documents in the top levels of the window, ``IsOffscreen`` ignored."""
    request = _request(automation, _SCOPE_CHILDREN, _MODE_FULL)
    found: list[_PageCandidate] = []
    frontier = [root]
    visited = 0
    for _depth in range(_SCAN_DEPTH):
        nxt = []
        for element in frontier:
            if cancel.is_set() or time.monotonic() >= deadline or visited >= _SCAN_NODES:
                return found
            try:
                holder = element.BuildUpdatedCache(request)  # type: ignore[attr-defined]
            except Exception:
                continue
            for child in _cached_children(holder):
                visited += 1
                get = child.GetCachedPropertyValue
                if get(P_CONTROL_TYPE) == _DOCUMENT:
                    found.append(
                        _PageCandidate(
                            live=child,
                            name=_text(get(P_NAME)),
                            value=_text(get(P_VALUE)) if _bool(get(P_IS_VALUE)) else "",
                            bounds=_rect(get(P_BOUNDS)),
                            offscreen=_bool(get(P_IS_OFFSCREEN)),
                            focused=_bool(get(P_HAS_FOCUS)),
                            order=len(found),
                        )
                    )
                    continue  # a page's own frames are part of it
                if _rect(get(P_BOUNDS)) is None and not _cached_children(child):
                    continue
                nxt.append(child)
        frontier = nxt
        if not frontier:
            break
    return found


def _title_rank(name: str, window_title: str) -> int:
    """2: the window title starts with the page title; 1: contains it; 0: no."""
    name, title = name.strip(), window_title.strip()
    if not name or not title:
        return 0
    if title.startswith(name):
        return 2
    return 1 if name in title else 0


def _choose(candidates: list[_PageCandidate], window_title: str) -> tuple[_PageCandidate | None, int]:
    visible = [c for c in candidates if c.bounds is not None]
    if not visible:
        return None, 0

    def area(c: _PageCandidate) -> int:
        b = c.bounds or (0, 0, 0, 0)
        return (b[2] - b[0]) * (b[3] - b[1])

    ranked = sorted(
        visible,
        key=lambda c: (not c.offscreen, _title_rank(c.name, window_title), c.focused, area(c), -c.order),
        reverse=True,
    )
    return ranked[0], len(visible) - 1


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def read_window(
    hwnd: int,
    *,
    timeout_s: float = READ_TIMEOUT_S,
    max_nodes: int = MAX_NODES,
    text_pattern: bool = False,
    hidden_labels: bool = False,
) -> Snapshot:
    """Structured, visible-only read of one window (never raises; see module docstring).

    ``hidden_labels``: keep screen-reader-only labels as ``sr_only`` leaves
    (module docstring, step 3).

    ``text_pattern``: also read the visible text of the largest element with a
    Text pattern (a terminal's buffer, an editor's document) and add it as a
    ``Text`` node with class ``yuki:text-pattern`` under the content root.
    """
    started = time.perf_counter()
    info = window_info(hwnd) if win32gui.IsWindow(hwnd) else None
    snap = Snapshot(
        hwnd=int(hwnd),
        title=info.title if info else "",
        process_name=info.process_name if info else "",
        root=None,
    )
    if info is None:
        snap.notes.append("not a window (it may have closed)")
        return snap
    deadline = time.monotonic() + max(timeout_s - _HANDOFF_S, 0.1)
    cancel = threading.Event()
    shared: dict[str, object] = {}

    def worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        try:
            _read(hwnd, snap, shared, deadline, cancel, max_nodes, text_pattern, hidden_labels)
        except BaseException as exc:  # noqa: BLE001 - reported in notes
            shared["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=worker, name=f"yuki-uia-extract-{hwnd}", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0.1))
    if thread.is_alive():
        cancel.set()
        snap.truncated = True
        snap.notes.append(f"read stopped at the {timeout_s:g} s budget")
    root = shared.get("root")
    if isinstance(root, Node):
        snap.root = root
        snap.nodes = number(root)
        page = shared.get("page")
        snap.page = page if isinstance(page, Node) else None
    if shared.get("error"):
        snap.notes.append(f"read error: {shared['error']}")
    snap.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return snap


def _read(
    hwnd: int,
    snap: Snapshot,
    shared: dict[str, object],
    deadline: float,
    cancel: threading.Event,
    max_nodes: int,
    text_pattern: bool = False,
    hidden_labels: bool = False,
) -> None:
    module, automation = _automation()
    handles = _tree._surface_handles(hwnd)
    frame_pass = _tree._ViewportPass(
        automation,
        hwnd,
        handles,
        _FRAME_ELEMENTS,
        min(deadline, time.monotonic() + _FRAME_S),
        cancel=cancel,
        stop_at_documents=True,
    )
    live_root = None
    offscreen_seen = True
    try:
        walker = frame_pass.run()
        frame = list(walker.elements)
        offscreen_seen = walker.offscreen_skipped > 0
    except Exception as exc:  # noqa: BLE001
        frame = []
        snap.notes.append(f"page finding failed: {type(exc).__name__}")
    by_id = {e.id: e for e in frame}
    candidates: list[_PageCandidate] = []
    for order, (element_id, live) in enumerate(frame_pass.page_elements.items()):
        element = by_id.get(element_id)
        if element is None:
            continue
        candidates.append(
            _PageCandidate(
                live=live,
                name=element.name,
                value=element.value or "",
                bounds=element.bounds,
                offscreen=False,
                focused=element.is_focused,
                order=order,
            )
        )
    if not candidates and offscreen_seen:
        # Only when the pass dropped something as off-screen: that is where a
        # covered Gecko window's pages went.
        live_root = _tree._element_from_handle(automation, hwnd, deadline, cancel)
        if live_root is not None:
            scanned = _scan_documents(automation, live_root, min(deadline, time.monotonic() + 0.4), cancel)
            if scanned:
                candidates = scanned
                snap.notes.append(
                    f"{len(scanned)} page(s) found by a scan that ignores IsOffscreen"
                    + (" (all reported off-screen)" if all(c.offscreen for c in scanned) else "")
                )
    chosen, others = _choose(candidates, snap.title)
    snap.other_pages = others
    snap.frame_chars = sum(
        len(e.name) + len(e.value or "") for e in frame if e.role != "Document"
    )
    frame_rect = _tree._window_rect(hwnd)
    viewport = _overlap(frame_rect, virtual_screen_bounds()) if frame_rect else None
    snap.viewport = viewport
    window_rects = [r for r in (_tree._window_rect(h) for h in handles) if r is not None]
    if chosen is not None:
        snap.page_title = chosen.name
        snap.url = chosen.value if _tree.is_address(chosen.value) else None
        def page_walk(raw: bool) -> _Walk:
            return _Walk(
                automation,
                deadline=deadline,
                cancel=cancel,
                max_nodes=max_nodes,
                viewport=viewport,
                honor_offscreen=not chosen.offscreen,
                # The page's own window is not a reason to read level by level.
                window_rects=[r for r in window_rects if not (chosen.bounds and _inside(r, chosen.bounds))],
                raw=raw,
                hidden_labels=hidden_labels,
            )

        if chosen.offscreen:
            snap.notes.append("page reports itself off-screen (window covered?): visibility by geometry only")
        walk = page_walk(False)
        page = walk.run(chosen.live)
        if page is not None and _text_leaves_hidden(page) and time.monotonic() < deadline:
            # Gecko keeps text leaves out of the control view (measured on a
            # Gmail inbox: 28 Text elements in the control view, 467 in the
            # raw view); the raw view is where the page's text is.
            raw_walk = page_walk(True)
            raw_page = raw_walk.run(chosen.live)
            if raw_page is not None and not raw_walk.truncated:
                walk, page = raw_walk, raw_page
                snap.notes.append("text leaves hidden from the control view: read the raw view")
        if page is not None:
            shared["root"] = page
            shared["page"] = page
            snap.truncated = snap.truncated or walk.truncated
            if text_pattern:
                _visible_text_node(module, automation, chosen.live, page, deadline)
            else:
                _maybe_text_pattern(module, chosen.live, page)
            return
        snap.notes.append("the page did not answer; reading the whole window")
    if live_root is None:
        live_root = _tree._element_from_handle(automation, hwnd, deadline, cancel)
    if live_root is None:
        snap.notes.append("UIA gave no element for this window")
        return
    walk = _Walk(
        automation,
        deadline=deadline,
        cancel=cancel,
        max_nodes=max_nodes,
        viewport=viewport,
        honor_offscreen=True,
        window_rects=window_rects,
        hidden_labels=hidden_labels,
    )
    shared["root"] = walk.run(live_root)
    snap.truncated = snap.truncated or walk.truncated
    if isinstance(shared["root"], Node):
        if text_pattern:
            _visible_text_node(module, automation, live_root, shared["root"], deadline)
        else:
            _maybe_text_pattern(module, live_root, shared["root"])


#: A page this large whose named Text leaves are fewer than this share of its
#: elements keeps its text out of the UIA control view.
_HIDDEN_TEXT_MIN_NODES = 50
_HIDDEN_TEXT_SHARE = 0.05


def _text_leaves_hidden(root: Node) -> bool:
    nodes = named_text = 0
    for node in root.walk():
        nodes += 1
        if node.role == "Text" and node.name:
            named_text += 1
    return nodes >= _HIDDEN_TEXT_MIN_NODES and named_text < _HIDDEN_TEXT_SHARE * nodes


def _maybe_text_pattern(module: object, live: object, root: Node) -> None:
    """A page or window whose elements say almost nothing: ask its Text pattern.

    The text goes into the root's ``value`` (a Document's own value is its
    address for a browser page, kept in ``Snapshot.url`` already).
    """
    chars = 0
    for node in root.walk():
        chars += len(node.name)
        if chars >= _THIN_TEXT:
            return
    try:
        pattern = live.GetCurrentPattern(_PATTERN_TEXT)  # type: ignore[attr-defined]
        if not pattern:
            return
        text_pattern = pattern.QueryInterface(module.IUIAutomationTextPattern)  # type: ignore[attr-defined]
        text = str(text_pattern.DocumentRange.GetText(_TEXT_PATTERN_CHARS) or "")
    except Exception:
        return
    text = "\n".join(line.replace("\ufffc", "").rstrip() for line in text.splitlines() if line.strip())
    if text:
        root.children.append(Node(role="Text", name=text, bounds=root.bounds, class_name="yuki:text-pattern"))


def _range_text(ranges: object) -> str:
    parts = []
    try:
        for i in range(ranges.Length):  # type: ignore[attr-defined]
            parts.append(str(ranges.GetElement(i).GetText(-1) or ""))  # type: ignore[attr-defined]
    except Exception:
        pass
    return "\n".join(parts)


def _visible_text_node(module: object, automation: object, live: object, root: Node, deadline: float) -> None:
    """The visible text of the largest Text-pattern element below ``live``."""
    if time.monotonic() >= deadline:
        return
    try:
        condition = automation.CreatePropertyCondition(P_IS_TEXT, True)  # type: ignore[attr-defined]
        request = _request(automation, _SCOPE_ELEMENT, _MODE_FULL)
        found = live.FindAllBuildCache(7, condition, request)  # type: ignore[attr-defined]  # TreeScope_Subtree
    except Exception:
        return
    best, best_area, best_node = None, -1, None
    try:
        count = found.Length
    except Exception:
        return
    for i in range(count):
        element = found.GetElement(i)
        node = _node_from(element.GetCachedPropertyValue)
        if node.bounds is None or node.offscreen and node.role != "Document":
            continue
        area = node.width() * node.height()
        if area > best_area:
            best, best_area, best_node = element, area, node
    if best is None or best_node is None:
        return
    try:
        pattern = best.GetCurrentPattern(_PATTERN_TEXT).QueryInterface(module.IUIAutomationTextPattern)  # type: ignore[attr-defined]
        text = _range_text(pattern.GetVisibleRanges())
        if not text.strip():
            text = str(pattern.DocumentRange.GetText(_TEXT_PATTERN_CHARS) or "")
    except Exception:
        return
    text = "\n".join(line.replace("\ufffc", "").rstrip() for line in text.splitlines())
    text = text.strip("\n")
    if text.strip():
        root.children.append(
            Node(
                role="Text",
                name=text[-_TEXT_PATTERN_CHARS:],
                bounds=best_node.bounds,
                class_name="yuki:text-pattern",
                automation_id=best_node.automation_id,
                help=best_node.class_name,  # which control it came from (diagnostics)
            )
        )


def snapshot_from_tree(tree: object) -> Snapshot:
    """A Snapshot built from a :class:`yuki.perception.tree.WindowTree` (no class names).

    For callers that already hold a tree and want no second read.  The
    flattened tree has no unnamed containers, so profile anchors on classes
    cannot match and the generic extractors work from roles, names and
    positions alone.
    """
    elements = list(getattr(tree, "elements", []) or [])
    nodes: dict[int, Node] = {}
    root = Node(role="Window", name=getattr(tree, "title", ""))
    for element in elements:
        node = Node(
            role=element.role,
            name=element.name or "",
            value=element.value,
            bounds=tuple(element.bounds) if element.bounds else None,  # type: ignore[arg-type]
            is_scrollable=element.is_scrollable,
            is_focused=element.is_focused,
            is_selected="selected" in (element.states or ()),
            is_invokable="invoke" in (element.patterns or ()),
        )
        nodes[element.id] = node
        parent = nodes.get(getattr(element, "parent", -1))
        (parent.children if parent is not None else root.children).append(node)
    number(root)
    identity = _tree.page_identity(elements)
    snap = Snapshot(
        hwnd=int(getattr(tree, "hwnd", 0)),
        title=getattr(tree, "title", ""),
        process_name=getattr(tree, "process_name", ""),
        root=root,
        source="tree",
        truncated=bool(getattr(tree, "truncated", False)),
    )
    if identity is not None:
        page = nodes.get(identity.element_id)
        if page is not None:
            snap.page = page
            snap.url = identity.url or None
            snap.page_title = identity.title
            snap.frame_chars = sum(
                len(n.name) for n in root.walk() if n is not root and not page.contains(n)
            )
            snap.root = page
    snap.nodes = number(snap.root)
    return snap
