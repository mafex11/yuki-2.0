"""capC1 review fixes (report 96, "Review fixes"): the trace worker, driven item by item with the worker tests' rig.

Covers: nothing from a blocked span becomes a row later (Recent links), the focus race around Tab (a password never
counted under the previous field), an unreadable IsPassword fails closed, staleness decided per action in a busy app,
open/save dialogs and a save chord without a dialog (FL Studio's Ctrl+S with its project facts), and up to five
changed elements by role and position. Every app, name and file is invented; nothing touches the desktop.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from test_trace_worker import APP, BROWSER, HWND, button, rig  # noqa: F401 - the rig fixture

from yuki.memory import flp
from yuki.memory.trace_hooks import EVENT_OBJECT_FOCUS, EVENT_OBJECT_SHOW, EVENT_SYSTEM_DIALOGSTART
from yuki.memory.trace_privacy import TraceRules
from yuki.memory.trace_uia import Element


#: The temp folder is a default blocked folder: these tests block another (invented) one instead.
ON = {"record_actions": True, "blocked_folders": ["Z:/invented-vault"]}


def _recent(tmp_path, rig):
    recent = tmp_path / "Recent"
    recent.mkdir()
    rig.rules = TraceRules.from_dict(ON)
    rig.w._wall = time.time                                   # the test's clock; the links are stamped from it
    rig.w._recent = recent
    rig.w._recent_seen = time.time() - 5
    rig.w._opened_at = time.time() - 5
    return recent


def _old_file(path, data: bytes = b"x"):
    path.write_bytes(data)
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    return path


def _link(path):
    """A Recent link written now, stamped from ``time.time()``, the clock the worker's wall reads (r30: a file's real
    mtime is hours ahead of a pinned run's clock, so a link written while blocked read as opened after)."""
    path.write_bytes(b"L")
    now = time.time()
    os.utime(path, (now, now))
    return path


# -- F1: nothing from a blocked span ---------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["gate", "off", "paused"])
def test_files_opened_while_the_trace_was_blocked_never_become_rows(tmp_path, rig, how):
    """A Recent link written while gated, off or paused is passed over once the trace opens again."""
    recent = _recent(tmp_path, rig)
    note = _old_file(tmp_path / "invented-notes.txt")
    rig.links = {"invented-notes.txt.lnk": str(note)}
    if how == "gate":
        rig.gate = True
    elif how == "off":
        rig.rules = TraceRules.from_dict({**ON, "record_actions": False})
    time.sleep(0.02)
    _link(recent / "invented-notes.txt.lnk")
    if how != "paused":
        rig.feed("recent", rig.clock.t, dt=0.1)               # dropped at the first step
    # (paused: the controller has the hooks blocked, so no item comes at all)
    time.sleep(0.02)
    rig.gate = False
    rig.rules = TraceRules.from_dict(ON)
    if how == "paused":
        rig.feed("reopen", time.time())                       # the controller's mark on opening
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=0.1)
    rig.feed("recent", rig.clock.t + 1, dt=1)
    rig.settle()
    assert [r for r in rig.rows() if r["kind"].startswith("file_")] == []
    assert rig.w.counters["dropped_blocked_folder"] == 0       # passed over because it came while blocked


def test_a_file_opened_after_the_trace_opens_again_is_recorded(tmp_path, rig):
    recent = _recent(tmp_path, rig)
    note = _old_file(tmp_path / "invented-plan.txt")
    rig.links = {"invented-plan.txt.lnk": str(note)}
    rig.gate = True
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=0.1)
    rig.gate = False
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=0.1)   # the worker sees the trace open again
    time.sleep(0.02)
    _link(recent / "invented-plan.txt.lnk")
    rig.feed("recent", rig.clock.t + 1, dt=1)
    rig.settle()
    assert [r["kind"] for r in rig.rows() if r["kind"].startswith("file_")] == ["file_open"]


# -- F2: the focus race ----------------------------------------------------------------------------------------------


def _username():
    return Element(role="Edit", name="User name", rid=(7, 1), pid=APP, bounds=(100, 100, 400, 130),
                   ancestors=[("Group", "login")])


def _password():
    return Element(role="Edit", name="Password", rid=(7, 2), password=True, pid=APP, bounds=(100, 140, 400, 170),
                   ancestors=[("Group", "login")])


def test_a_tab_right_after_a_click_never_counts_the_password_under_the_username(rig):
    """(a) Click the user name field, Tab within half a second, type a 9-letter password."""
    rig.uia.points[(150, 120)] = _username()
    rig.uia.focus = _username()
    t = rig.clock.t
    rig.click()
    rig.feed("ev", t + 0.06, EVENT_OBJECT_FOCUS, 11, -4, 1, dt=0.01)       # the click's own focus event
    rig.uia.focus = _password()                                           # the app moved the focus on Tab
    rig.feed("key", rig.clock.t, "named", "Tab", "user", 0, dt=0.2)
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_FOCUS, 11, -4, 2, dt=0.01)
    for _ in range(9):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    rig.settle()
    assert [r["typed"] for r in rig.rows() if r["kind"] == "type"] == []


def test_a_password_typed_before_the_apps_focus_event_is_dropped(rig):
    """(b) The first character reaches the worker before the app's FOCUS event."""
    rig.uia.focus = _username()
    for _ in range(5):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", "Tab", "user", 0, dt=0.3)
    rig.feed("char", rig.clock.t, 1, "user", dt=0.03)                     # read: still the user name field
    rig.uia.focus = _password()
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_FOCUS, 11, -4, 2, dt=0.02)
    for _ in range(8):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    rig.settle()
    assert [r["typed"] for r in rig.rows() if r["kind"] == "type"] == [5]
    assert rig.w.counters["dropped_focus_race"] == 1


def test_an_app_without_focus_events_has_the_field_read_again(rig):
    """No FOCUS event at all: the run that started right after Tab is read again half a second in."""
    rig.uia.focus = _username()
    for _ in range(5):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", "Tab", "user", 0, dt=0.3)
    rig.feed("char", rig.clock.t, 1, "user", dt=0.03)
    rig.uia.focus = _password()
    for _ in range(8):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    rig.settle()
    assert [r["typed"] for r in rig.rows() if r["kind"] == "type"] == [5]


def test_a_plain_field_after_tab_keeps_its_count(rig):
    """The re-read costs nothing when the field is a plain one: the run is kept, on the field read last."""
    rig.uia.focus = _username()
    rig.feed("key", rig.clock.t, "named", "Tab", "user", 0, dt=0.3)
    for _ in range(6):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.settle()
    assert [r["typed"] for r in rig.rows() if r["kind"] == "type"] == [6]


def test_auto_advancing_boxes_close_the_run_on_each_focus_event(rig):
    """An auto-advance field (invented code boxes) never carries one box's count into the next."""
    rig.uia.focus = Element(role="Edit", name="Digit 1", rid=(8, 1), pid=APP, bounds=(0, 0, 20, 20))
    rig.feed("char", rig.clock.t, 1, "user", dt=1.0)
    rig.uia.focus = Element(role="Edit", name="Digit 2", rid=(8, 2), password=True, pid=APP, bounds=(30, 0, 50, 20))
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_FOCUS, 11, -4, 3, dt=0.8)    # well after the run began
    rig.feed("char", rig.clock.t, 1, "user", dt=0.3)
    rig.settle()
    assert [r["typed"] for r in rig.rows() if r["kind"] == "type"] == [1]


# -- F8: IsPassword unreadable -----------------------------------------------------------------------------------------


def test_an_unreadable_is_password_fails_closed(rig):
    unknown = Element(role="Edit", name="Field", rid=(9, 1), password=False, password_known=False, pid=APP,
                      bounds=(100, 100, 400, 140))
    assert unknown.secret() == "password_unknown" and _password().secret() == "password"
    assert _username().secret() is None
    rig.uia.points[(150, 120)] = unknown
    rig.uia.focus = unknown
    rig.click()
    for _ in range(4):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.1)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["dropped_password_unknown"] >= 2


# -- F6: staleness in a busy app --------------------------------------------------------------------------------------


def test_staleness_holds_in_a_busy_app(rig):
    """A structure change while the lookup answered, then 80 more: the row is still stale."""
    rig.uia.points[(150, 120)] = button()
    rig.uia.slow_s = 0.2
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("out", t + 0.05, EVENT_OBJECT_SHOW, 9, 0, 0, 1)
    rig.feed("up", t + 0.25, "left", 150, 120, "user", False, dt=0.05)
    for i in range(80):
        rig.feed("out", t + 0.3 + i * 0.01, EVENT_OBJECT_SHOW, 9, 0, i + 1, 1)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


# -- files: dialogs and a save chord ----------------------------------------------------------------------------------


def test_a_file_from_an_open_dialog_belongs_to_the_dialogs_app(tmp_path, rig):
    recent = _recent(tmp_path, rig)
    doc = _old_file(tmp_path / "invented-report.txt")
    rig.links = {"invented-report.txt.lnk": str(doc)}
    rig.feed("fg", rig.clock.t, HWND[APP], APP)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=0.1)
    rig.feed("ev", rig.clock.t, EVENT_SYSTEM_DIALOGSTART, 77, 0, 0, dt=0.1)
    rig.feed("fg", rig.clock.t, 77, APP, dt=0.05)                        # the dialog in front
    rig.uia.points[(150, 120)] = button(name="Open")
    rig.click()
    rig.feed("fg", rig.clock.t, HWND[APP], APP, dt=0.1)                   # the dialog closed
    rig.fg = (HWND[BROWSER], BROWSER)                                     # something else in front by the scan
    time.sleep(0.02)
    (recent / "invented-report.txt.lnk").write_bytes(b"L")
    rig.feed("recent", rig.clock.t, dt=0.3)
    rig.settle()
    (row,) = [r for r in rig.rows() if r["kind"].startswith("file_")]
    assert row["kind"] == "file_open" and row["via"] == "dialog" and row["process"] == "fieldnotes"


def test_ctrl_s_without_a_dialog_saves_a_known_project_and_reads_its_facts(tmp_path, rig):
    """FL Studio's usual save: Ctrl+S, no dialog, no new Recent entry: the project's own write time says so."""
    recent = _recent(tmp_path, rig)
    project = _old_file(tmp_path / "Invented Groove.flp",
                        flp.build([flp.event(flp.EV_TITLE, "Invented Groove"), flp.event(flp.EV_TEMPO, 140000)]))
    rig.links = {"Invented Groove.flp.lnk": str(project)}
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=0.1)
    time.sleep(0.02)
    (recent / "Invented Groove.flp.lnk").write_bytes(b"L")
    rig.feed("recent", rig.clock.t, dt=0.5)
    rig.settle()
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=1.0)
    project.write_bytes(flp.build([flp.event(flp.EV_TITLE, "Invented Groove"), flp.event(flp.EV_TEMPO, 150000)]))
    rig.settle()
    files = [r for r in rig.rows() if r["kind"].startswith("file_")]
    assert [(r["kind"], r["via"]) for r in files] == [("file_open", "recent"), ("file_save", "save_chord")]
    facts = [json.loads(rig.store._dec(f["data_ciphertext"])) for f in rig.store._query(
        "SELECT * FROM ui_files ORDER BY id")]
    assert [f["tempo"] for f in facts] == [140.0, 150.0]
    os.utime(project, (time.time() - 60, time.time() - 60))
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=5.0)    # nothing written this time
    rig.settle()
    assert len([r for r in rig.rows() if r["kind"] == "file_save"]) == 1


# -- outcome elements -------------------------------------------------------------------------------------------------


def test_up_to_five_changed_elements_get_role_and_position_with_spare_budget(rig):
    """Report 78 §4.2 step 3: outcome elements by role, parent role and position; never names."""
    rig.uia.points[(150, 120)] = button()
    for i in range(7):
        rig.uia.events[(9, -4, i)] = Element(role="ListItem", name=f"Invented row {i}", pid=APP,
                                             parent_role="List", bounds=(10, 10 * i, 110, 10 * i + 8))
    t = rig.clock.t
    rig.click()
    for i in range(7):
        rig.feed("out", t + 0.1 + i * 0.01, EVENT_OBJECT_SHOW, 9, -4, i, 1)
    rig.settle()
    summary = json.loads(rig.store._dec(rig.rows()[0]["outcome_ciphertext"]))
    assert 1 <= len(summary["elements"]) <= 5 and summary["shown"] == 7
    assert summary["elements"][0] == {"role": "ListItem", "parent": "List", "at": "60,4"}
    assert "Invented" not in json.dumps(summary)
    assert len([c for c in rig.uia.calls if c[0] == "event"]) <= 5
