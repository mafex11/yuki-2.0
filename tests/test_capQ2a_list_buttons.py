"""capQ2a (report 103): a button inside a list, tree or grid is a content item; a real command button keeps its name.

The 8 Oct audit: apps that draw a sidebar's threads or servers as Buttons put that content into the element registry
and the model's step lines, because C1 keeps a command control's name on first use. By structure alone: a command
control with a list, tree, data grid or one of their items among its ancestors keeps a digest only on its row, and its
name reaches the registry only after 3 distinct days, like a tab's or a title's. Send, Save and Settings in a toolbar
keep their names. Existing registry rows keep working and follow the new class from their next use. Every app and
name is invented; nothing touches the desktop.
"""

from __future__ import annotations

import pytest
from test_trace_worker import APP, Rig

from yuki.memory import trace_store
from yuki.memory.trace_privacy import COMMAND, CONTENT, TITLE, content_item, name_class
from yuki.memory.trace_uia import Element

DAY = 86400.0


def test_by_structure_alone():
    assert name_class("Button", "Group", [("Group", ""), ("ListItem", ""), ("List", "")]) == TITLE
    assert name_class("Button", "TreeItem") == TITLE
    assert name_class("CheckBox", "Group", [("DataItem", ""), ("DataGrid", "")]) == TITLE
    assert name_class("Button", "ToolBar", [("ToolBar", ""), ("Pane", ""), ("Window", "")]) == COMMAND
    assert name_class("Button", "Group", [("Group", ""), ("Document", "")]) == COMMAND
    assert name_class("ListItem", "List", [("List", "")]) == CONTENT             # list items stay content
    assert name_class("Button") == COMMAND                                      # no structure known: as before
    assert content_item("Button", ["ListItem"]) and not content_item("Text", ["ListItem"])


def thread_button(name: str) -> Element:
    return Element(role="Button", name=name, rid=(4, 2), bounds=(10, 100, 300, 160), pid=APP, parent_role="Group",
                   ancestors=[("Group", ""), ("ListItem", ""), ("List", "threads"), ("Group", "")], index=2)


def send_button() -> Element:
    return Element(role="Button", name="Send", rid=(4, 9), bounds=(800, 700, 860, 740), pid=APP,
                   parent_role="ToolBar", ancestors=[("ToolBar", ""), ("Group", ""), ("Window", "")])


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.store.close()


def _click_on(rig, el, x, y):
    rig.uia.points[(x, y)] = el
    rig.click(x, y)
    rig.settle()


def test_a_thread_drawn_as_a_button_in_a_list_keeps_a_digest_only(rig):
    _click_on(rig, thread_button("Invented plan for the garden, Working, home"), 150, 120)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert row["role"] == "Button" and row["name_digest"] and row["name_ciphertext"] is None
    assert row["position"] == "2/Group"                                       # where it was, not what it said
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None
    assert rig.w.counters["content_item_named"] == 1


def test_a_toolbar_send_button_keeps_its_name(rig):
    _click_on(rig, send_button(), 820, 720)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert rig.store._dec(row["name_ciphertext"]) == "Send"
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == COMMAND and rig.store._dec(el["name_ciphertext"]) == "Send"


def _use(store, key, cls, name, at):
    trace_store.write_batch(store, [], [{"element_key": key, "process": "fieldnotes", "role": "Button",
                                         "name_class": cls, "name": name, "aid": None, "at": at}])


def test_its_name_reaches_the_registry_only_after_three_distinct_days(rig):
    base = 1_780_000_000.0
    for day in range(3):
        _use(rig.store, "k-thread", TITLE, "Invented daily standup", base + day * DAY)
        (el,) = rig.store._query("SELECT * FROM ui_elements")
        assert (el["name_ciphertext"] is None) == (day < 2)


def test_an_existing_registry_row_kept_as_a_command_follows_the_new_class_from_its_next_use(rig):
    base = 1_780_000_000.0
    _use(rig.store, "k-old", COMMAND, "Invented server name", base)              # kept by C1 on first use
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert rig.store._dec(el["name_ciphertext"]) == "Invented server name"
    _use(rig.store, "k-old", TITLE, "Invented server name", base + 60)           # the same element, now known
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None and el["n"] == 2
    _use(rig.store, "k-old", TITLE, "Invented server name", base + DAY)
    _use(rig.store, "k-old", TITLE, "Invented server name", base + 2 * DAY)
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert rig.store._dec(el["name_ciphertext"]) == "Invented server name"     # promoted on its third day


def test_the_step_lines_show_role_and_position_until_then(rig):
    from yuki.memory.trace_tasks import element_label, element_names

    _click_on(rig, thread_button("Invented plan for the garden, Working, home"), 150, 120)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    names = element_names(rig.store, [row])
    assert names[row["element_key"]] is None
    label = element_label(row, None)
    assert "Invented" not in label and "Button" in label


def test_readers_that_judge_a_row_by_its_role_alone_find_no_name_on_it(rig):
    from yuki.memory.practice_learn import _element_names

    _click_on(rig, thread_button("Invented plan for the garden, Working, home"), 150, 120)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert _element_names(rig.store, [row]) == {row["id"]: None}


# -- review F3 and the sidebar drawn outside any list (9 Oct) ----------------------------------------------------------


def test_a_toolbar_nearer_than_the_list_item_keeps_a_command(rig):
    """Review F3: a message's own action bar drawn as a ToolBar inside the message's ListItem: its buttons are commands
    (the nearest container of either kind decides). A plain Group bar inside a list item stays a content item: the
    structure says nothing more, and its name comes back after 3 distinct days of use."""
    assert content_item("Button", [("ToolBar", ""), ("Group", ""), ("ListItem", ""), ("List", "")], "ToolBar") is False
    assert name_class("Button", "Group", [("Group", ""), ("ToolBar", ""), ("ListItem", "")]) == COMMAND
    assert name_class("Button", "Group", [("Group", ""), ("ListItem", ""), ("ToolBar", "")]) == TITLE
    reply = Element(role="Button", name="Reply", rid=(4, 11), bounds=(700, 300, 730, 320), pid=APP,
                    parent_role="ToolBar", ancestors=[("ToolBar", ""), ("Group", ""), ("ListItem", ""), ("List", "")])
    _click_on(rig, reply, 710, 310)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert rig.store._dec(row["name_ciphertext"]) == "Reply" and not rig.w.counters["content_item_named"]


def test_one_of_a_set_with_a_current_item_is_a_content_item(rig):
    """A chat app's sidebar of threads drawn as Buttons in plain Groups (no list): the set marks its current thread
    (ARIA ``current``), read from the siblings by the client (``Element.current_set``)."""
    assert name_class("Button", "Group", [("Group", ""), ("Group", "")], current_set=True) == TITLE
    assert name_class("Button", "ToolBar", [("ToolBar", "")], current_set=True) == COMMAND
    card = Element(role="Button", name="Invented trip budget, 2h", rid=(4, 12), bounds=(10, 200, 300, 240), pid=APP,
                   parent_role="Group", ancestors=[("Group", ""), ("Group", "nav"), ("Group", "")], index=3,
                   current_set=True)
    _click_on(rig, card, 150, 220)
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert row["name_ciphertext"] is None and row["position"] == "3/Group"
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None


def test_aria_properties_are_read_with_their_escapes():
    from yuki.memory.trace_uia import aria_properties

    assert aria_properties("current=page;level=2") == {"current": "page", "level": "2"}
    assert aria_properties(r"label=a\;b\=c;current=false") == {"label": "a;b=c", "current": "false"}
    assert aria_properties("") == {} and aria_properties(None) == {}


class _Raw:
    """A UI Automation element as the client sees it (cached properties only)."""

    def __init__(self, props: dict, kids: list | None = None, parent=None):
        self.props, self.kids, self.parent = props, kids or [], parent

    def GetCachedPropertyValue(self, prop):  # noqa: N802 - COM's name
        return self.props.get(prop)

    def FindAllBuildCache(self, scope, cond, request):  # noqa: N802
        kids = self.kids

        class _Arr:
            Length = len(kids)

            @staticmethod
            def GetElement(i):  # noqa: N802
                return kids[i]

        return _Arr()


class _Walker:
    @staticmethod
    def GetParentElementBuildCache(el, request):  # noqa: N802
        return el.parent


def _client():
    from yuki.memory.trace_uia import TraceUia

    c = object.__new__(TraceUia)
    c._walker, c._true, c._request, c._set_request, c.counts = _Walker(), object(), "plain", "set", {}
    return c


def _button(rid, aria="", parent=None):
    return _Raw({30003: 50000, 30000: rid, 30005: "Invented", 30102: aria}, parent=parent)


@pytest.mark.parametrize("current, expected", [("current=page", True), ("current=false", False), ("", False)])
def test_the_client_reads_the_siblings_current_mark_in_the_index_call(current, expected):
    import time

    window = _Raw({30003: 50032, 30020: 5})
    group = _Raw({30003: 50026}, parent=window)
    me = _button((1, 1), parent=group)
    group.kids = [_button((1, 0), current, parent=group), me, _button((1, 2), parent=group)]
    c = _client()
    el = c._facts(me, time.perf_counter())
    c._ancestry(el, time.perf_counter() + 5, index=True)
    assert el.current_set is expected and el.index == 1


def test_a_button_already_in_a_list_is_not_asked_about_its_set():
    import time

    lst = _Raw({30003: 50008})
    item = _Raw({30003: 50007}, parent=lst)
    me = _button((2, 1), parent=item)
    asked: list = []
    item.FindAllBuildCache = lambda scope, cond, request: asked.append(request) or _Raw({}).FindAllBuildCache(0, 0, 0)
    c = _client()
    el = c._facts(me, time.perf_counter())
    c._ancestry(el, time.perf_counter() + 5, index=False)
    assert asked == [] and el.current_set is False


# -- second verify V2-1: the content mark is sticky --------------------------------------------------------------------


def _card(current: bool) -> Element:
    return Element(role="Button", name="Invented thread about a kettle", rid=(4, 12), bounds=(10, 200, 300, 240),
                   pid=APP, parent_role="Group", ancestors=[("Group", ""), ("Group", "nav"), ("Group", "")], index=3,
                   current_set=current)


def _card_clicks(rig, *marks: bool) -> list:
    for current in marks:
        _click_on(rig, _card(current), 150, 220)
        rig.clock.t += 5
    return [r for r in rig.rows() if r["kind"] == "click"]


def test_a_card_seen_as_an_item_stays_one_when_no_thread_is_open(rig):
    """V2-1: the user clicks a thread card while a thread is open (an item: no name), later the same day from a view
    with no current thread. The second use still keeps no name, in the registry or in its raw row."""
    rows = _card_clicks(rig, True, False, False)
    assert len(rows) == 3 and len({r["element_key"] for r in rows}) == 1
    assert [r["name_ciphertext"] for r in rows] == [None, None, None]
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None and el["days"] == 1


def test_a_card_first_taken_for_a_command_loses_its_name_everywhere_once_known(rig):
    """V2-1 reversed: no thread open first (taken for a command: named), then seen as an item, then again with no
    current thread. The registry and every raw row of the card end with no name."""
    rows = _card_clicks(rig, False)
    assert rig.store._dec(rows[0]["name_ciphertext"]) == "Invented thread about a kettle"
    rows = _card_clicks(rig, True, False)
    assert [r["name_ciphertext"] for r in rows] == [None, None, None]
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None


def test_a_sticky_item_still_gets_its_name_on_its_third_day(rig):
    base = 1_780_000_000.0
    _use(rig.store, "k-card", TITLE, "Invented thread about a kettle", base)
    _use(rig.store, "k-card", COMMAND, "Invented thread about a kettle", base + 60)       # no current thread then
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None
    _use(rig.store, "k-card", COMMAND, "Invented thread about a kettle", base + DAY)
    _use(rig.store, "k-card", COMMAND, "Invented thread about a kettle", base + 2 * DAY)
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and rig.store._dec(el["name_ciphertext"]) == "Invented thread about a kettle"


def test_a_real_command_button_is_untouched_by_the_sticky_rule(rig):
    _click_on(rig, send_button(), 820, 720)
    rig.clock.t += 5
    _click_on(rig, send_button(), 820, 720)
    rows = [r for r in rig.rows() if r["kind"] == "click"]
    assert [rig.store._dec(r["name_ciphertext"]) for r in rows] == ["Send", "Send"]
