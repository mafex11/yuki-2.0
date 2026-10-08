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
