"""capC1c (report 99): the resolution walk from an unnamed container to the real control, and hovers that say nothing.

The first live recording resolved many clicks in web pages and Electron apps to "an unnamed Group in a Group". When
UI Automation answers with an unnamed container that exposes no actionable pattern, the walk looks, inside the same
lookup (one token, one hung-app check, a share of its time), for the nearest ancestor with an actionable pattern or a
name that still holds the point, else the smallest named descendant under it. A renderer's whole pane gets its
descent inside the click's own lookup too. The C1 privacy rules apply to whatever is chosen. Driven against a fake UI
Automation tree (``GetCachedPropertyValue``, ``GetParentElementBuildCache``, ``FindAllBuildCache``) and the worker
tests' rig; every name is invented and nothing touches the desktop.
"""

from __future__ import annotations

import json

import pytest
from test_trace_worker import APP, HWND, Rig, button

from yuki.memory import trace_uia as U
from yuki.memory.trace_uia import Element, TraceUia

TYPES = {name: code for code, name in U.ROLE_NAMES.items()}
PATTERN_PROPS = {word: prop for prop, word in U.ACTION_PATTERNS}


class Node:
    """A fake UI Automation element with cached properties, a parent and children."""

    def __init__(self, role: str, bounds: tuple[int, int, int, int], name: str = "", *, patterns: tuple[str, ...] = (),
                 password: bool = False, hwnd: int = 0, children: list[Node] | None = None) -> None:
        self.parent: Node | None = None
        self.children = children or []
        for kid in self.children:
            kid.parent = self
        l, t, r, b = bounds
        self.props = {U._P_TYPE: TYPES[role], U._P_NAME: name, U._P_AID: "", U._P_RUNTIME_ID: [id(self) % 997, 1],
                      U._P_BOUNDS: (l, t, r - l, b - t), U._P_PID: APP, U._P_PASSWORD: password, U._P_HWND: hwnd}
        for word in patterns:
            self.props[PATTERN_PROPS[word]] = True

    def GetCachedPropertyValue(self, prop):   # noqa: N802 - the COM name
        return self.props.get(prop, 0)

    def FindAllBuildCache(self, scope, cond, request):   # noqa: N802
        return _Array(self.children)

    def __bool__(self) -> bool:
        return True


class _Array:
    def __init__(self, items) -> None:
        self.items = items
        self.Length = len(items)

    def GetElement(self, i):   # noqa: N802
        return self.items[i]


class _Walker:
    def GetParentElementBuildCache(self, node, request):   # noqa: N802
        if node.parent is None:
            raise OSError("no parent")
        return node.parent


class _Automation:
    def __init__(self) -> None:
        self.at: Node | None = None

    def ElementFromPointBuildCache(self, point, request):   # noqa: N802
        return self.at


def client(at: Node) -> TraceUia:
    uia = TraceUia.__new__(TraceUia)       # no COM: the fake tree stands in for UI Automation
    uia._uia = _Automation()
    uia._uia.at = at
    uia._walker = _Walker()
    uia._request = uia._true = None
    return uia


WINDOW_AREA = 1000 * 800


def page(*kids: Node) -> Node:
    """A window holding a renderer's whole page (a Document covering it)."""
    doc = Node("Document", (0, 0, 1000, 800), children=list(kids))
    return Node("Window", (0, 0, 1000, 800), "Notes - Browser X", hwnd=11, children=[Node("Pane", (0, 0, 1000, 800),
                                                                                          children=[doc])])


def test_an_unnamed_group_inside_a_button_walks_up_to_the_button():
    inner = Node("Group", (110, 105, 190, 135))
    btn = Node("Button", (100, 100, 200, 140), "Publish", patterns=("invoke",), children=[inner])
    page(Node("Group", (50, 50, 500, 300), children=[btn]))
    el = client(inner).at_point(150, 120, budget_s=0.15, index=True, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Button" and el.name == "Publish" and el.refined == "up"
    assert el.patterns == ("invoke",) and el.parent_role == "Group"          # the ancestry is the button's own


def test_with_no_actionable_or_named_ancestor_it_walks_down_to_the_smallest_named_descendant():
    text = Node("Text", (140, 112, 170, 128), "Inbox")
    mid = Node("Group", (120, 105, 300, 135), children=[text])
    outer = Node("Group", (100, 100, 400, 160), children=[mid])
    page(outer)
    el = client(outer).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Text" and el.name == "Inbox" and el.refined == "down"


def test_an_ancestor_that_no_longer_holds_the_point_ends_the_upward_walk():
    inner = Node("Group", (110, 105, 190, 135))
    holder = Node("Group", (100, 100, 200, 140), children=[inner])
    Node("Button", (600, 600, 700, 640), "Elsewhere", patterns=("invoke",), children=[holder])  # does not hold it
    el = client(inner).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Group" and el.refined is None and el.low_info()       # nothing better: kept, counted


def test_a_whole_window_ancestor_is_never_taken_for_the_control():
    inner = Node("Group", (110, 105, 190, 135))
    page(inner)                       # its parent is the page Document (unnamed, whole window), then a named Window
    el = client(inner).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Group" and el.refined is None


def test_a_renderer_pane_is_descended_inside_the_clicks_own_lookup():
    link = Node("Hyperlink", (140, 110, 220, 130), "Pricing", patterns=("invoke",),
                children=[Node("Group", (140, 110, 220, 130))])
    win = page(Node("Group", (100, 100, 400, 200), children=[link]))
    doc = win.children[0].children[0]
    el = client(doc).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Hyperlink" and el.refined == "path"                   # not the unnamed Group inside it
    el2 = client(doc).at_point(150, 120, budget_s=0.15, refine=False)
    assert el2.role == "Document" and el2.refined is None                    # without the walk: the page itself


def test_the_walk_stays_within_the_lookups_time():
    inner = Node("Group", (110, 105, 190, 135))
    Node("Button", (100, 100, 200, 140), "Publish", patterns=("invoke",), children=[inner])
    el = client(inner).at_point(150, 120, budget_s=0.0, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Group" and el.refined is None                         # no time left: the answer as it was


def test_low_information_is_read_from_ui_automations_own_facts():
    assert Element(role="Group").low_info()
    assert Element(role="Group", patterns=()).low_info()
    assert not Element(role="Group", name="Composer").low_info()
    assert not Element(role="Custom", patterns=("invoke",)).low_info()       # a container you can press
    assert not Element(role="ListItem", patterns=()).low_info()              # not a container type
    assert Element(role="Text", patterns=()).actionable() is False
    assert Element(role="Text").actionable() is True                        # not read: judged by its type


# -- the worker ----------------------------------------------------------------------------------------------------


class WalkUia:
    """A worker-side fake of a client that has the walk: it records the walk's arguments."""

    refines = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.points: dict[tuple[int, int], Element] = {}
        self.events: dict[tuple, Element] = {}

    def at_point(self, x, y, *, budget_s, index=False, refine=False, max_area=0):
        self.calls.append(("point", x, y, refine, max_area))
        return self.points.get((x, y))

    def from_event(self, hwnd, oid, cid, *, budget_s, index=False):
        self.calls.append(("event", hwnd, oid, cid))
        return self.events.get((hwnd, oid, cid))

    def descend(self, *a, **kw):
        self.calls.append(("descend",))
        return None

    def focused(self, *, budget_s):
        return None

    def reread(self, el, value_length=False):
        return {}


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.uia = r.w.uia = WalkUia()
    yield r
    r.store.close()


def _press(rig, x=150, y=120):
    t = rig.clock.t
    rig.feed("down", t, "left", x, y, "user", 0, False, HWND[APP])
    rig.feed("up", t + 0.08, "left", x, y, "user", False, dt=0.08)
    rig.settle()


def test_the_worker_asks_for_the_walk_with_the_windows_area_and_keeps_the_patterns(rig):
    rig.uia.points[(150, 120)] = Element(role="Button", name="Publish", bounds=(100, 100, 200, 140), pid=APP,
                                         parent_role="Group", ancestors=[("Group", "")], patterns=("invoke",),
                                         refined="up")
    _press(rig)
    assert rig.uia.calls[0] == ("point", 150, 120, True, 1000 * 800)
    (row,) = rig.rows()
    assert row["resolved"] == "point" and rig.store._dec(row["name_ciphertext"]) == "Publish"
    assert json.loads(row["facts"])["patterns"] == ["invoke"]
    assert rig.w.counters["walk_up"] == 1


def test_a_descent_inside_the_lookup_is_the_snapshot_path_and_never_a_second_lookup(rig):
    rig.uia.points[(150, 120)] = Element(role="Hyperlink", name="Pricing", bounds=(140, 110, 220, 130), pid=APP,
                                         parent_role="Group", ancestors=[("Group", "")], patterns=("invoke",),
                                         refined="path")
    _press(rig)
    (row,) = rig.rows()
    assert row["resolved"] == "snapshot" and not any(c[0] == "descend" for c in rig.uia.calls)
    assert row["name_ciphertext"] is None                       # a link is a content control: never a name


def test_a_descent_that_stopped_on_an_unnamed_container_counts_as_a_container_left(rig):
    """Review C1c-5: the walk_* counters count only containers that became real controls; a descent that ended on an
    unnamed Group (its deadline, no named node) is counted as a container left."""
    rig.uia.points[(150, 120)] = Element(role="Group", bounds=(140, 110, 220, 130), pid=APP, patterns=(),
                                         refined="descent")
    _press(rig)
    assert rig.w.counters["walk_container_left"] == 1 and not rig.w.counters["walk_descent"]
    rig.uia.points[(150, 120)] = Element(role="Button", name="Send", bounds=(140, 110, 220, 130), pid=APP,
                                         patterns=("invoke",), refined="descent")
    rig.clock.t += 5
    _press(rig)
    assert rig.w.counters["walk_descent"] == 1 and rig.w.counters["walk_container_left"] == 1


def test_a_whole_pane_nothing_deeper_answers_for_is_point_only_without_a_second_lookup(rig):
    rig.uia.points[(150, 120)] = Element(role="Pane", bounds=(0, 0, 1000, 800), pid=APP, patterns=())
    _press(rig)
    (row,) = rig.rows()
    assert row["resolved"] == "unresolved" and row["role"] == "Pane" and row["point"] == "150,120"
    assert [c[0] for c in rig.uia.calls] == ["point"]


def test_the_privacy_rules_hold_for_the_chosen_element(rig):
    rig.uia.points[(150, 120)] = Element(role="Edit", name="Vault code", bounds=(100, 100, 300, 140), pid=APP,
                                         password=True, patterns=("value",), refined="down")
    _press(rig)
    assert rig.rows() == [] and rig.w.counters["dropped_password"] == 1
    rig.uia.points[(150, 120)] = Element(role="ListItem", name="Ravi Kumar", bounds=(100, 100, 300, 140), pid=APP,
                                         parent_role="List", ancestors=[("List", "")], patterns=("select",),
                                         refined="up")
    _press(rig)
    (row,) = rig.rows()
    assert row["role"] == "ListItem" and row["name_ciphertext"] is None and row["name_digest"]
    assert "Ravi" not in json.dumps(row)


def test_an_event_naming_only_a_container_keeps_the_control_the_press_found(rig):
    rig.uia.points[(150, 120)] = button(patterns=("invoke",))
    rig.uia.events[(HWND[APP], -4, 9)] = Element(role="Group", bounds=(0, 0, 900, 700), pid=APP, patterns=())
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("ev", t + 0.05, 0x8005, HWND[APP], -4, 9, dt=0.05)          # FOCUS on the page region
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.03)
    rig.settle()
    (row,) = rig.rows()
    assert row["role"] == "Button" and row["resolved"] == "point"
    assert rig.w.counters["event_container_kept_point"] == 1


def test_an_event_naming_a_real_control_still_wins(rig):
    rig.uia.points[(150, 120)] = Element(role="Group", bounds=(100, 100, 300, 200), pid=APP, patterns=())
    rig.uia.events[(HWND[APP], -4, 9)] = button(name="Archive", patterns=("invoke",))
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("ev", t + 0.05, 0x8013, HWND[APP], -4, 9, dt=0.05)          # INVOKED
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.03)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] == "event" and rig.store._dec(row["name_ciphertext"]) == "Archive"


# -- hovers ----------------------------------------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.anchor_at = 0.0
        self.anchor_seq = 1
        self.jump_seq = -1
        self.moving = True
        self.travel = 0.0
        self.move_source = "user"


def _hover(rig, el: Element | None):
    rig.w.sink = _Sink()
    rig.uia.points[(150, 120)] = el
    rig.w.sink.anchor_at = rig.clock.t
    rig.feed("moving", rig.clock.t)
    rig.clock.t += 0.5
    rig.w.tick()
    rig.feed("down", rig.clock.t + 1.0, "left", 600, 600, "user", 0, False, HWND[APP], dt=1.0)   # ends the rest
    rig.feed("up", rig.clock.t + 0.05, "left", 600, 600, "user", False, dt=0.05)
    rig.settle()
    return [r for r in rig.rows() if r["kind"] == "hover"]


def test_a_hover_that_stays_an_unnamed_container_or_list_is_not_stored_but_counted(rig):
    assert _hover(rig, Element(role="List", bounds=(100, 100, 400, 400), pid=APP, patterns=())) == []
    assert rig.w.counters["skip_hover_container"] == 1
    assert rig.uia.calls[0][3] is True                          # the walk was asked first


def test_a_hover_on_a_named_control_is_stored_with_its_walk_asked_at_low_priority(rig):
    rows = _hover(rig, Element(role="Button", name="Library", bounds=(100, 100, 200, 140), pid=APP,
                               parent_role="ToolBar", patterns=("invoke",)))
    assert len(rows) == 1 and rows[0]["role"] == "Button"
    rig.w._bucket.tokens = 1.0                                  # only the clicks' reserve left, and no refill
    rig.w._bucket._fill = lambda: None
    before = len(rig.uia.calls)
    rig.w._last_hover_xy = None
    assert _hover(rig, Element(role="Button", name="Library", bounds=(100, 100, 200, 140), pid=APP,
                               patterns=("invoke",))) == rows   # no new hover row
    assert [c for c in rig.uia.calls[before:] if c[:3] == ("point", 150, 120)] == []   # a hover never takes it
    assert rig.w.counters["skip_hover_unresolved"] == 1 and rig.w.counters["lookup_over_budget"] >= 1
