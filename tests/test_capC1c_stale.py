"""capC1c (report 99): staleness by machine facts, from the first live recording's taskbar clicks.

A press is stale only when something could have changed what is under its point before the lookup answered: a
structure event whose own window covers the point, another top-level window under the point at the answer than at
the press, the element's own top-level window not the one pressed, or its bounds no longer holding the point. A window
coming to the front after the press (a taskbar click's app) is the click's outcome, never staleness. Both sides are
tested: a correct taskbar click stays resolved; a menu closing under the press, a list reordering under the point and
a dialog appearing over it stay stale. Every app and name is invented; nothing touches the desktop.
"""

from __future__ import annotations

import pytest
from test_trace_worker import APP, HWND, PROCS, FakeUia, Rig, button

from yuki.memory.trace_hooks import (EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER, EVENT_OBJECT_SHOW,
                                     EVENT_SYSTEM_MENUPOPUPEND)
from yuki.memory.trace_input import Metrics, TraceInput
from yuki.memory.trace_uia import Element

SHELL = 201
PROCS[SHELL] = ("shellbar", "shellbar.exe", "Shell Bar", False, 3.0)
TASKBAR, THUMB, TOOLTIP, MENU, DIALOG = 61, 62, 63, 64, 65
RECTS = {TASKBAR: (0, 760, 1000, 800), THUMB: (100, 600, 300, 750), TOOLTIP: (120, 720, 260, 750),
         HWND[APP]: (0, 0, 1000, 760), MENU: (140, 100, 340, 300), DIALOG: (100, 50, 600, 400)}
PRESS = (150, 780)          # on a taskbar button


def pinned(name="Quillpad pinned", **kw) -> Element:
    return Element(role="Button", name=name, rid=(7, 1), bounds=(130, 762, 180, 798), pid=SHELL,
                   parent_role="ToolBar", ancestors=[("ToolBar", ""), ("Pane", ""), ("Window", "")], **kw)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.w._rect = lambda h: RECTS.get(h)
    yield r
    r.store.close()


def _taskbar_click(rig, *, events=(), lookup_s=0.2, fg_after_up=True):
    """A press on a taskbar button while the worker is behind (its lookup answers ``lookup_s`` after the press)."""
    rig.uia.points[PRESS] = pinned()
    rig.under = (TASKBAR, SHELL)
    rig.uia.slow_s = lookup_s
    t = rig.clock.t
    rig.feed("down", t, "left", *PRESS, "user", 0, False, TASKBAR)
    rig.clock.t = t + 0.08
    rig.feed("up", t + 0.08, "left", *PRESS, "user", False)
    for dt, event, hwnd in events:
        rig.feed("out", t + dt, event, hwnd, 0, 0, 1)
    if fg_after_up:
        rig.feed("fg", t + 0.1, HWND[APP], APP)     # the app the click brought up: the click's outcome
    rig.settle()
    return [r for r in rig.rows() if r["kind"] == "click"]


def test_a_correct_taskbar_click_with_its_app_coming_up_is_not_stale(rig):
    """The live shape: the right Button, a window to the front and a thumbnail and tooltip shown elsewhere."""
    (row,) = _taskbar_click(rig, events=[(0.05, EVENT_OBJECT_SHOW, THUMB), (0.06, EVENT_OBJECT_SHOW, TOOLTIP)])
    assert row["resolved"] == "point" and row["role"] == "Button"
    assert rig.store._dec(row["name_ciphertext"]) == "Quillpad pinned"
    assert row["outcome"] == "changed"
    assert any(r["kind"] == "switch" for r in rig.rows())           # the app it brought up is its own row
    assert rig.w.counters["resolved_stale"] == 0


def test_a_hidden_window_that_is_gone_cannot_make_a_press_stale_by_itself(rig):
    (row,) = _taskbar_click(rig, events=[(0.05, EVENT_OBJECT_HIDE, 999)])    # no rect any more
    assert row["resolved"] == "point"


def test_the_foreground_change_alone_never_makes_a_press_stale(rig):
    (row,) = _taskbar_click(rig, events=[], lookup_s=0.5)
    assert row["resolved"] == "point"


def test_a_menu_closing_under_the_press_before_the_worker_got_to_it_is_stale(rig):
    """The press landed on a menu (its own top-level window); by the time the worker handled it the menu was gone and
    the app's window is under the point: a lookup now would name the wrong thing."""
    rig.uia.points[(150, 120)] = button(name="Archive")
    rig.under = (HWND[APP], APP)
    t = rig.clock.t
    rig.w._pid_of = lambda h: APP if h == MENU else 0
    rig.feed("down", t, "left", 150, 120, "user", 0, False, MENU)
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] != "point" and row["element_key"] is None
    assert rig.uia.calls == []                                       # no lookup into the window that replaced it
    assert rig.w.counters["stale_window_changed"] == 1


def test_a_press_whose_window_is_gone_before_the_worker_got_to_it_keeps_nothing(rig):
    rig.under = (HWND[APP], APP)
    rig.w._pid_of = lambda h: 0                                      # the pressed window no longer exists
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, MENU)
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["skip_window_changed"] == 1 and rig.uia.calls == []


def test_a_press_on_a_window_now_covered_by_another_app_is_that_windows_apps_row(rig):
    """The press was in Browser X's window; a Field Notes dialog covered it before the worker got there: the row is
    Browser X's (its own gates decide), stale and point-only, never Field Notes' element."""
    from test_trace_worker import BROWSER, VAULT

    rig.under = (HWND[APP], APP)
    rig.w._pid_of = lambda h: {HWND[BROWSER]: BROWSER, HWND[VAULT]: VAULT}.get(h, 0)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[BROWSER])
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    (row,) = rig.rows()
    assert row["process"] == "browserx" and row["element_key"] is None and rig.uia.calls == []
    assert rig.w.counters["stale_window_changed"] == 1
    rig.feed("down", rig.clock.t, "left", 150, 120, "user", 0, False, HWND[VAULT])   # a blocked app's window
    rig.feed("up", rig.clock.t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert len(rig.rows()) == 1 and rig.w.counters["dropped_blocked_app"] >= 1


class _ClosingMenu(FakeUia):
    """The menu under the press closes while the lookup answers: the window under the point changes."""

    def __init__(self, clock, rig) -> None:
        super().__init__(clock)
        self.rig = rig

    def at_point(self, x, y, *, budget_s, index=False):
        el = super().at_point(x, y, budget_s=budget_s, index=index)
        self.rig.under = (HWND[APP], APP)
        return el


def test_a_menu_closing_while_the_lookup_answered_is_stale(rig):
    rig.uia = rig.w.uia = _ClosingMenu(rig.clock, rig)
    rig.uia.points[(150, 120)] = Element(role="MenuItem", name="Rename", bounds=(140, 110, 340, 130), pid=APP)
    rig.under = (MENU, APP)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, MENU)
    rig.feed("out", t + 0.01, EVENT_SYSTEM_MENUPOPUPEND, MENU, 0, 0, 1)
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] == "stale"


def test_a_list_reordering_under_the_point_is_stale(rig):
    rig.uia.points[(150, 120)] = Element(role="ListItem", name="Weekly notes", bounds=(100, 110, 400, 130), pid=APP,
                                         parent_role="List", ancestors=[("List", "")])
    rig.uia.slow_s = 0.2
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("out", t + 0.05, EVENT_OBJECT_REORDER, HWND[APP], -4, 0, 1)   # the list's own window holds the point
    rig.feed("up", t + 0.25, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


def test_a_dialog_appearing_over_the_point_is_stale(rig):
    rig.uia.points[(150, 120)] = button()
    rig.uia.slow_s = 0.2
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("out", t + 0.05, EVENT_OBJECT_SHOW, DIALOG, 0, 0, 1)          # its window covers the point
    rig.feed("up", t + 0.25, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


def test_a_dialog_of_another_app_now_under_the_point_is_stale(rig):
    """No event reaches the trace from another process; the window under the point at the answer tells."""
    rig.uia = rig.w.uia = _ClosingMenu(rig.clock, rig)            # (any window change during the lookup)
    rig.uia.points[(150, 120)] = button()
    rig.under = (DIALOG, APP)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, DIALOG)
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale" and rig.w.counters["stale_root_changed"] == 1


def test_an_element_of_another_top_level_window_is_stale(rig):
    rig.uia.points[(150, 120)] = button(window=555)
    rig.w._root_of = lambda h: {555: DIALOG}.get(h, h)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"
    assert rig.w.counters["stale_other_window"] == 1


def test_an_element_in_the_pressed_window_is_not_stale(rig):
    rig.uia.points[(150, 120)] = button(window=556)
    rig.w._root_of = lambda h: {556: HWND[APP]}.get(h, h)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "point"


def test_an_element_whose_bounds_no_longer_hold_the_point_is_stale(rig):
    rig.uia.points[(150, 120)] = Element(role="Button", name="Send", bounds=(300, 300, 400, 340), pid=APP,
                                         parent_role="ToolBar")
    rig.click()
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale" and rig.w.counters["stale_bounds"] == 1


def test_a_structure_event_elsewhere_in_the_same_app_before_the_answer_is_not_stale(rig):
    """A tooltip window of the app itself shown away from the point while the lookup answered."""
    rig.uia.points[(150, 120)] = button()
    rig.uia.slow_s = 0.2
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("out", t + 0.05, EVENT_OBJECT_SHOW, TOOLTIP, 0, 0, 1)
    rig.feed("up", t + 0.25, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "point"


def test_the_sink_takes_the_window_under_the_press_at_button_down():
    items: list = []
    asked: list = []
    s = TraceInput(items.append, metrics=Metrics(read=False, drag_w=4, drag_h=4, double_s=0.5, double_w=4,
                                                 double_h=4, hover_w=4, hover_h=4, hover_s=0.4),
                   cursor=lambda: (150, 780), clock=lambda: 0.0,
                   root_at=lambda x, y: asked.append((x, y)) or TASKBAR)
    s.blocked = False
    s.mouse(0.0, 0, 0x1, 0, 0, 0, 0, 1)
    assert items[-1][0] == "down" and items[-1][8] == TASKBAR and asked == [(150, 780)]
    s.mouse(0.05, 0, 0x2, 0, 0, 0, 0, 1)
    assert asked == [(150, 780)]                                     # only at the press
    s2 = TraceInput(items.append, metrics=s.metrics, cursor=lambda: (1, 1), clock=lambda: 0.0,
                    root_at=lambda x, y: 1 / 0)
    s2.blocked = False
    s2.mouse(0.0, 0, 0x1, 0, 0, 0, 0, 1)
    assert items[-1][0] == "down" and items[-1][8] == 0             # a failing read never breaks the reader
