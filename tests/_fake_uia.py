"""A fake UI Automation tree for the reader's tests (report 58): no desktop, no COM.

It answers the calls :class:`yuki.perception.tree._ViewportPass` makes - cache requests with a tree scope and a tree
filter (conditions evaluated the way UI Automation does: an element that fails the filter is skipped and its children
take its place), cached properties and children - from plain :class:`Node` objects shaped like the trees recorded on
3-4 Oct (Spotify's player bar with its hidden range inputs, a Discord chat, a YouTube page). Names are invented.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from yuki.perception import tree as T

_ROLE_IDS = {name: control for control, name in T._ROLE_NAMES.items()}
_rids = itertools.count(1000)


@dataclass
class Node:
    role: str
    name: str = ""
    bounds: tuple[int, int, int, int] | None = None
    offscreen: bool = False
    value: str | None = None
    readonly: bool = True
    invoke: bool = False
    toggle: int | None = None          # 0 off, 1 on
    expand: int | None = None
    selected: bool | None = None
    range: tuple[float, float, float] | None = None
    focusable: bool = False
    focused: bool = False
    scroll: bool = False
    native: int = 0
    rid: tuple[int, ...] | None = None
    aid: str = ""
    enabled: bool = True
    control: bool = True
    pos: tuple[int, int] | None = None
    children: list["Node"] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.rid is None:
            self.rid = (42, next(_rids))

    def add(self, *nodes: "Node") -> "Node":
        self.children.extend(nodes)
        return self

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def prop(self, pid: int) -> Any:
        b = self.bounds
        table = {
            T._P_RUNTIME_ID: list(self.rid) if self.rid else None,
            T._P_CONTROL_TYPE: _ROLE_IDS.get(self.role, 50025),
            T._P_NAME: self.name,
            T._P_BOUNDING_RECT: (b[0], b[1], b[2] - b[0], b[3] - b[1]) if b else (0, 0, 0, 0),
            T._P_IS_OFFSCREEN: self.offscreen,
            T._P_IS_ENABLED: self.enabled,
            T._P_IS_KEYBOARD_FOCUSABLE: self.focusable,
            T._P_HAS_KEYBOARD_FOCUS: self.focused,
            T._P_ACCELERATOR_KEY: "",
            T._P_ACCESS_KEY: "",
            T._P_VALUE_VALUE: self.value or "",
            T._P_VALUE_IS_READONLY: self.readonly,
            T._P_IS_VALUE_AVAILABLE: self.value is not None,
            T._P_SCROLL_VERTICALLY_SCROLLABLE: self.scroll,
            T._P_SCROLL_HORIZONTALLY_SCROLLABLE: False,
            T._P_IS_SCROLL_AVAILABLE: self.scroll,
            T._P_IS_INVOKE_AVAILABLE: self.invoke,
            T._P_IS_TOGGLE_AVAILABLE: self.toggle is not None,
            T._P_IS_SELECTION_ITEM_AVAILABLE: self.selected is not None,
            T._P_IS_EXPAND_COLLAPSE_AVAILABLE: self.expand is not None,
            T._P_LEGACY_DEFAULT_ACTION: "",
            T._P_NATIVE_WINDOW_HANDLE: self.native,
            T._P_SELECTION_ITEM_IS_SELECTED: bool(self.selected),
            T._P_TOGGLE_STATE: self.toggle if self.toggle is not None else 0,
            T._P_EXPAND_COLLAPSE_STATE: self.expand if self.expand is not None else 3,
            T._P_IS_RANGE_VALUE_AVAILABLE: self.range is not None,
            T._P_RANGE_VALUE_VALUE: self.range[0] if self.range else 0.0,
            T._P_RANGE_VALUE_MINIMUM: self.range[1] if self.range else 0.0,
            T._P_RANGE_VALUE_MAXIMUM: self.range[2] if self.range else 0.0,
            T._P_RANGE_VALUE_IS_READONLY: False,
            T._P_AUTOMATION_ID: self.aid,
            T._P_IS_CONTROL_ELEMENT: self.control,
            T._P_POSITION_IN_SET: self.pos[0] if self.pos else 0,
            T._P_SIZE_OF_SET: self.pos[1] if self.pos else 0,
        }
        return table.get(pid)


def matches(node: Node, condition: Any) -> bool:
    kind = condition[0]
    if kind == "true":
        return True
    if kind == "control":
        return node.control
    if kind == "raw":
        return True
    if kind == "prop":
        value = node.prop(condition[1])
        return value == condition[2]
    if kind == "and":
        return matches(node, condition[1]) and matches(node, condition[2])
    if kind == "or":
        return matches(node, condition[1]) or matches(node, condition[2])
    if kind == "not":
        return not matches(node, condition[1])
    raise ValueError(condition)


class Request:
    def __init__(self) -> None:
        self.props: list[int] = []
        self.TreeScope = 1
        self.TreeFilter: Any = ("control",)
        self.AutomationElementMode = 0

    def AddProperty(self, pid: int) -> None:
        self.props.append(pid)


class _Array:
    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.Length = len(items)

    def GetElement(self, index: int) -> Any:
        return self.items[index]


def view_children(node: Node, condition: Any) -> list[Node]:
    """The node's children in a filtered view: a child that fails is replaced by its own view children."""
    out: list[Node] = []
    for child in node.children:
        if matches(child, condition):
            out.append(child)
        else:
            out.extend(view_children(child, condition))
    return out


class Cached:
    """An element with its cached properties and, per the request's scope, its cached children."""

    def __init__(self, node: Node, request: Request, *, depth_left: int, automation: "Automation") -> None:
        self.node = node
        self.request = request
        self.depth_left = depth_left
        self.automation = automation

    def GetCachedPropertyValue(self, pid: int) -> Any:
        return self.node.prop(pid)

    def GetCachedChildren(self) -> Any:
        if self.depth_left <= 0:
            return None
        kids = view_children(self.node, self.request.TreeFilter)
        if not kids:
            return None
        return _Array([Cached(k, self.request, depth_left=self.depth_left - 1, automation=self.automation)
                       for k in kids])

    def BuildUpdatedCache(self, request: Request) -> "Cached":
        return self.automation.build(self.node, request)

    def FindFirstBuildCache(self, scope: int, condition: Any, request: Request) -> Any:
        return None

    def FindAllBuildCache(self, scope: int, condition: Any, request: Request) -> Any:
        """Every descendant (and the element itself with the element scope bit) in the request's view that meets
        ``condition``, as UI Automation's ``FindAll`` gives them (percFix: the slider sweep)."""
        view = request.TreeFilter
        found = [n for n in self.node.walk() if (n is not self.node or scope & 1) and matches(n, view)
                 and matches(n, condition)]
        return _Array([Cached(n, request, depth_left=0, automation=self.automation) for n in found])

    @property
    def CachedBoundingRectangle(self) -> Any:
        raise AttributeError


class Automation:
    """Enough of IUIAutomation for the reader: conditions, cache requests, ElementFromHandle."""

    ControlViewCondition = ("control",)
    RawViewCondition = ("raw",)

    def __init__(self, roots: dict[int, Node]) -> None:
        self.roots = roots
        self.fetches = 0
        self.parents: dict[int, Node] = {}
        for root in roots.values():
            for node in root.walk():
                for child in node.children:
                    self.parents[id(child)] = node

    @property
    def ControlViewWalker(self) -> "_Walker":
        return _Walker(self)

    def CreateCacheRequest(self) -> Request:
        return Request()

    def CreatePropertyCondition(self, pid: int, value: Any) -> tuple:
        return ("prop", pid, value)

    def CreateAndCondition(self, a: Any, b: Any) -> tuple:
        return ("and", a, b)

    def CreateOrCondition(self, a: Any, b: Any) -> tuple:
        return ("or", a, b)

    def CreateNotCondition(self, a: Any) -> tuple:
        return ("not", a)

    def ElementFromHandle(self, hwnd: int) -> "Live":
        if hwnd not in self.roots:
            raise OSError("no such window")
        return Live(self.roots[hwnd], self)

    def build(self, node: Node, request: Request) -> Cached:
        self.fetches += 1
        scope = request.TreeScope
        depth = 0
        if scope & T._TREE_SCOPE_DESCENDANTS:
            depth = 10_000
        elif scope & T._TREE_SCOPE_CHILDREN:
            depth = 1
        return Cached(node, request, depth_left=depth, automation=self)


class _Walker:
    """The control view's tree walker: an element's nearest ancestor that is a control element."""

    def __init__(self, automation: "Automation") -> None:
        self.automation = automation

    def GetParentElementBuildCache(self, element: Cached, request: Request) -> Any:
        node = self.automation.parents.get(id(element.node))
        while node is not None and not node.control:
            node = self.automation.parents.get(id(node))
        return Cached(node, request, depth_left=0, automation=self.automation) if node is not None else None


class Live(Cached):
    def __init__(self, node: Node, automation: Automation) -> None:
        super().__init__(node, Request(), depth_left=0, automation=automation)


def read(root: Node, *, hwnd: int = 7001, viewport: tuple[int, int, int, int] | None = None,
         max_elements: int = 400, monkeypatch: Any = None, part: tuple[int, ...] | None = None) -> T.WindowTree:
    """One pass of the real :class:`~yuki.perception.tree._ViewportPass` over ``root``, returned as a WindowTree the
    way :func:`~yuki.perception.tree.get_window_tree` builds one (no thread, no desktop)."""
    import time

    view = viewport or root.bounds
    assert monkeypatch is not None, "pass pytest's monkeypatch"
    monkeypatch.setattr(T, "_window_rect", lambda h: view if h == hwnd else None)
    monkeypatch.setattr(T, "virtual_screen_bounds", lambda: (-10_000, -10_000, 10_000, 10_000))
    monkeypatch.setattr(T, "_child_window_at", lambda h, x, y: h)
    automation = Automation({hwnd: root})
    T._no_sliders.clear()   # the slider sweep's memo is per window handle; every read here is a new window

    def start(walker: Any) -> None:
        walker.part = tuple(part) if part else None

    walker = T._collect_pass(automation, hwnd, [], max_elements, time.monotonic() + 5.0, on_start=start)
    elements = list(walker.elements)
    return T.WindowTree(
        hwnd=hwnd, title=root.name, process_name="App.exe", elements=elements,
        truncated=walker.truncated, captured_at=time.time(), offscreen_skipped=walker.offscreen_skipped,
        fetches=walker.fetches, left_out=T._left_out_list(walker.left_out, len(elements)),
        hidden_controls=sum(1 for e in elements if e.hidden),
        stopped_by=walker.stopped_by if walker.truncated else "", part=bool(part),
        part_missing=bool(part) and not walker.part_found,
    )


# ---------------------------------------------------------------------------
# Shapes from the recorded trees (invented names)
# ---------------------------------------------------------------------------


def player_window(*, volume: float = 0.6, lyrics_on: bool = False, slider_box: str = "none",
                  bar_offscreen: bool = False) -> Node:
    """Spotify's window as read on 3-4 Oct: a Document holding the top bar, the library, and the "Now playing bar"
    whose volume is a Text label with the real range input hidden inside it.

    ``slider_box``: ``none`` (no box, as a clipped input can report), ``pixel`` (1x1 inside the label, off-screen) or
    ``full`` (a normal visible slider).  ``bar_offscreen``: the bar reported off-screen (a full-size view hides it).
    """
    window = Node("Window", "Example Artist - Night Walk", (105, 121, 1507, 951))
    pane = Node("Pane", "Example Artist - Night Walk", (105, 121, 1507, 951))
    doc = Node("Document", "Night Walk - Example Artist", (105, 121, 1507, 951),
               value="https://xpui.app.spotify.com/index.html", focusable=True)
    shell = Node("Group", "", (105, 121, 1507, 951), invoke=True)
    top = [Node("Button", "Home", (604, 127, 641, 164), invoke=True),
           Node("ComboBox", "What do you want to play?", (646, 127, 1008, 164), value="", readonly=False,
                expand=0)]
    bar = Node("Group", "Now playing bar", (111, 890, 1501, 945), offscreen=bar_offscreen)
    controls = Node("Group", "Player controls", (531, 895, 1081, 940), offscreen=bar_offscreen)
    controls.add(
        Node("Button", "Previous", (757, 895, 782, 921), invoke=True, offscreen=bar_offscreen),
        Node("Button", "Pause", (793, 895, 819, 921), invoke=True, offscreen=bar_offscreen),
        Node("Button", "Next", (830, 895, 855, 921), invoke=True, offscreen=bar_offscreen),
    )
    if slider_box == "full":
        volume_input = Node("Slider", "Change volume", (1374, 925, 1418, 935), range=(volume, 0.0, 1.0),
                            focusable=True, offscreen=bar_offscreen)
    elif slider_box == "pixel":
        volume_input = Node("Slider", "Change volume", (1380, 928, 1381, 929), range=(volume, 0.0, 1.0),
                            focusable=True, offscreen=True)
    else:
        volume_input = Node("Slider", "Change volume", None, range=(volume, 0.0, 1.0), focusable=True,
                            offscreen=True)
    progress_input = Node("Slider", "Change progress", None, range=(73.0, 0.0, 216.0), focusable=True,
                          offscreen=True)
    progress = Node("Text", "Change progress", (574, 927, 624, 962), offscreen=bar_offscreen).add(progress_input)
    controls.add(progress)
    bar.add(
        controls,
        Node("Button", "Lyrics", (1278, 905, 1303, 930), toggle=1 if lyrics_on else 0, offscreen=bar_offscreen),
        Node("Button", "Queue", (1302, 905, 1327, 930), toggle=0, offscreen=bar_offscreen),
        Node("Button", "Mute", (1351, 905, 1376, 930), invoke=True, offscreen=bar_offscreen),
        Node("Text", "Change volume", (1374, 912, 1418, 947), offscreen=bar_offscreen).add(volume_input),
    )
    library = Node("Group", "Your Library", (147, 227, 467, 880))
    for i in range(6):
        library.add(Node("Button", f"Playlist {i + 1}", (153, 341 + 50 * i, 461, 391 + 50 * i), invoke=True))
    shell.add(*top, library, bar)
    doc.add(shell)
    pane.add(doc)
    window.add(pane)
    return window


def chat_window(*, messages: int = 60, visible_from: int = 40, offset: int = 0, doc_scrolls: bool = False,
                positions: bool = False, rids: dict | None = None) -> Node:
    """A Discord-like DM: a scrolling List of message ListItems (rendered beyond the view, as a virtualised list keeps
    a margin), oldest first, and the message box.  Items before ``visible_from`` lie above the list's box (off-screen);
    ``offset`` shifts every item up by that many px (a scroll).  ``doc_scrolls``: the Document reports a scroll
    pattern, as Chromium's did on 3-4 Oct (the walk then reads it level by level).  ``positions``: the items carry
    the app's own numbering (k of n).  ``rids``: RuntimeIds by item index, kept between two builds."""
    window = Node("Window", "@Sam - Discord", (229, 138, 1645, 890), rid=(42, 1))
    doc = Node("Document", "@Sam", (237, 138, 1637, 882), value="https://discord.com/channels/@me/1",
               scroll=doc_scrolls, rid=(42, 2))
    chat = Node("List", "Messages in @Sam", (500, 200, 1600, 800), scroll=True, focusable=True, rid=(42, 3))
    rids = rids if rids is not None else {}
    for i in range(messages):
        y = 200 + (i - visible_from) * 40 - offset
        box = (500, y, 1600, y + 40)
        off = box[3] <= 200 or box[1] >= 800
        item_rid = rids.setdefault(i, (42, 100 + 2 * i))
        chat.add(Node("ListItem", f"Sam: invented message number {i + 1}", box, offscreen=off, rid=item_rid,
                      pos=(i + 1, messages) if positions else None).add(
            Node("Text", f"invented message number {i + 1}", (560, y + 10, 1200, y + 30), offscreen=off,
                 rid=(item_rid[0], item_rid[1] + 1))))
    box = Node("Edit", "Message @Sam", (500, 820, 1600, 860), value="", readonly=False, focusable=True, rid=(42, 4))
    doc.add(chat, box)
    window.add(doc)
    return window


def feed_window(*, offset: int = 0, rows: int = 30) -> Node:
    """A YouTube-like home page in a browser window: the page Document scrolls (as Chromium reports it), holding a
    grid of video links four to a row; ``offset`` px scrolled down.  Items wholly outside the view report
    off-screen.  RuntimeIds are fixed per item, so two builds are two reads of one page."""
    window = Node("Window", "Arc", (-8, -8, 1928, 1088), rid=(7, 1))
    pane = Node("Pane", "Arc", (0, 0, 1920, 1080), rid=(7, 2))
    doc = Node("Document", "(12) YouTube", (0, 32, 1920, 1072), value="https://www.youtube.com/", scroll=True,
               rid=(7, 3))
    grid = Node("Group", "", (0, 32, 1920, 1072), rid=(7, 4))
    for r in range(rows):
        for c in range(4):
            top = 100 + r * 300 - offset
            box = (40 + c * 470, top, 470 + c * 470, top + 260)
            off = box[3] <= 32 or box[1] >= 1072
            grid.add(Node("Hyperlink", f"Invented video {r * 4 + c + 1}", box, invoke=True, offscreen=off,
                          value=f"https://www.youtube.com/watch?v=v{r * 4 + c + 1}", rid=(7, 1000 + r * 4 + c)))
    doc.add(grid)
    pane.add(doc)
    window.add(pane)
    return window
