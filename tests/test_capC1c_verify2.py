"""capC1c (report 99, "Second verification"): the five findings of the adversarial second verifier.

* V2-1: a Value pattern counts as actionable only when it can be set (``ValueIsReadOnly``). Every Chromium / Electron
  page Document exposes a read-only Value (its address): it stays low-information for the event guard, the hover drop
  and the go-tos.
* V2-2: the press whose window was covered or gone reads the window's process as the rest of the trace does: for a
  UWP frame, the hosted app's, so a blocked frame-hosted app never reaches the store as its host.
* V2-3: the walk starts from any unnamed container, also one that exposes Invoke (Chromium and Electron wrap named
  controls in clickable unnamed Groups): a named element under the point wins, the wrapper stands only when there is
  none.
* V2-4: the window under the point is read right when ``ElementFromPoint`` answers, and decides only for an element
  whose own window is unknown: a window the click itself brings over the point later is its outcome, never staleness.
* V2-5 is in ``test_capC1c_drag.py`` (Windows does not scale ``SM_CXDRAG`` with DPI).

Fake UI Automation trees and the worker tests' rig; every app and name is invented and nothing touches the desktop.
"""

from __future__ import annotations

import pytest
import win32gui
import win32process
from test_c_practice import NOW, store  # noqa: F401 - the store fixture
from test_capC1c_gotos import act, labels
from test_capC1c_walk import WINDOW_AREA, Node, WalkUia, _hover, client, page
from test_trace_worker import APP, BROWSER, HWND, PROCS, VAULT, FakeUia, Rig, button

from yuki.memory import trace as T
from yuki.memory import trace_habits as H
from yuki.memory import trace_uia as U
from yuki.memory.extract import uia as X
from yuki.memory.trace_input import Metrics, TraceInput
from yuki.memory.trace_uia import Element


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.store.close()


@pytest.fixture
def walk_rig(tmp_path):
    r = Rig(tmp_path)
    r.uia = r.w.uia = WalkUia()
    yield r
    r.store.close()


def read_only_page(bounds=(100, 100, 400, 400), readonly=True) -> Node:
    """A page Document as Chromium exposes it: a Value pattern holding the page's address, read-only."""
    doc = Node("Document", bounds, patterns=("value",))
    doc.props[U._P_VALUE_READONLY] = readonly
    return doc


# -- V2-1: a read-only Value is nothing a person acts on -----------------------------------------------------------


def test_a_read_only_value_is_not_an_actionable_pattern():
    uia = client(None)
    doc = uia._facts(read_only_page(), 0.0)
    assert doc.patterns == () and doc.low_info()
    field = Node("Edit", (100, 100, 300, 130), patterns=("value",))       # ValueIsReadOnly false: you type in it
    assert uia._facts(field, 0.0).patterns == ("value",)
    writable = uia._facts(read_only_page(readonly=False), 0.0)
    assert writable.patterns == ("value",) and not writable.low_info()
    unknown = read_only_page()
    unknown.props[U._P_VALUE_READONLY] = None                               # unreadable: not counted
    assert uia._facts(unknown, 0.0).patterns == ()


def test_focus_on_a_read_only_page_document_keeps_the_control_the_press_found(walk_rig):
    """The verifier's shape: a click on a page button while focus moves to the page; Chromium fires FOCUS on the
    Document. The Document (read-only Value) is low-information, so the press's Button is kept."""
    doc = client(None)._facts(read_only_page(bounds=(0, 0, 900, 700)), 0.0)
    doc.pid = APP
    walk_rig.uia.points[(150, 120)] = button(patterns=("invoke",))
    walk_rig.uia.events[(HWND[APP], -4, 9)] = doc
    t = walk_rig.clock.t
    walk_rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    walk_rig.feed("ev", t + 0.05, 0x8005, HWND[APP], -4, 9, dt=0.05)
    walk_rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.03)
    walk_rig.settle()
    (row,) = walk_rig.rows()
    assert row["role"] == "Button" and row["resolved"] == "point"
    assert walk_rig.w.counters["event_container_kept_point"] == 1


def test_a_hover_that_stays_on_a_read_only_page_document_is_not_stored(walk_rig):
    doc = client(None)._facts(read_only_page(), 0.0)
    doc.pid = APP
    assert _hover(walk_rig, doc) == [] and walk_rig.w.counters["skip_hover_container"] == 1


def test_go_tos_never_show_a_page_document_the_worker_stored_from_a_read_only_value(store):  # noqa: F811
    doc = client(None)._facts(read_only_page(), 0.0)
    for i in range(9):
        act(store, NOW + i, "webchat", "click", role="Document", key="k-doc", position="Window",
            patterns=list(doc.patterns))
    for i in range(2):
        act(store, NOW + 20 + i, "webchat", "click", role="Button", name="Send", key="k-send", patterns=["invoke"])
    H.update(store, now=NOW + 100)
    (got,) = H.go_tos(store, ["webchat"], now=NOW + 100)
    assert labels(got) == ['Button "Send"']


# -- V2-2: a covered or gone UWP frame is its hosted app's ----------------------------------------------------------


FRAME, HOST = 70, 205
PROCS[HOST] = ("applicationframehost", "applicationframehost.exe", "Application Frame Host", False, 2.0)


@pytest.fixture
def frames(monkeypatch):
    """The OS facts for a frame: its class, its own (host) process, and the process its content shows."""
    state = {"content": VAULT}

    def content_pid(h):
        return state["content"] if h == FRAME else {HWND[APP]: APP, HWND[BROWSER]: BROWSER}.get(h, 0)

    monkeypatch.setattr(X, "content_pid", content_pid)
    monkeypatch.setattr(win32gui, "GetClassName", lambda h: X._UWP_FRAME_CLASS if h == FRAME else "Plain")
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda h: (1, HOST if h == FRAME else 0))
    return state


def test_the_pressed_windows_process_is_the_hosted_apps_and_a_host_only_frame_is_unknown(frames):
    assert T._window_pid(FRAME) == VAULT
    assert T._window_pid(HWND[BROWSER]) == BROWSER
    frames["content"] = HOST                    # the app's window detached: only the host is known
    assert T._window_pid(FRAME) == 0
    frames["content"] = 0                       # gone
    assert T._window_pid(FRAME) == 0


def test_the_sink_and_the_worker_read_the_pressed_window_that_way(frames):
    items: list = []
    m = Metrics(read=False, drag_w=4, drag_h=4, double_s=0.5, double_w=4, double_h=4, hover_w=4, hover_h=4,
                hover_s=0.4)
    s = TraceInput(items.append, metrics=m, cursor=lambda: (150, 120), clock=lambda: 0.0,
                   root_at=lambda x, y: FRAME, pid_of=T._window_pid)
    s.blocked = False
    s.mouse(0.0, 0, 0x1, 0, 0, 0, 0, 1)
    assert items[-1][8:] == (FRAME, VAULT)
    import inspect

    assert "pid_of=_window_pid" in inspect.getsource(T.Trace.start)        # the service's sink uses it
    assert "pid_of or _window_pid" in inspect.getsource(T.TraceWorker.__init__)


@pytest.mark.parametrize("gone", [False, True])
def test_a_covered_or_gone_frame_of_a_blocked_app_never_reaches_the_store(rig, frames, gone):
    rig.under = (HWND[APP], APP)                # another window is under the point when the worker gets there
    rig.w._pid_of = T._window_pid
    if gone:
        frames["content"] = 0
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, FRAME, VAULT)   # the sink read the hosted app
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["dropped_blocked_app"] == 1 and rig.uia.calls == []


def test_a_gone_frame_whose_hosted_app_was_never_known_keeps_nothing(rig, frames):
    rig.under = (HWND[APP], APP)
    rig.w._pid_of = T._window_pid
    frames["content"] = 0
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, FRAME, 0)       # host only at the press: 0
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["skip_window_changed"] == 1


# -- V2-3: the walk starts from any unnamed container ---------------------------------------------------------------


def test_an_unnamed_clickable_wrapper_walks_down_to_the_named_control_under_it():
    item = Node("TreeItem", (120, 108, 300, 132), "Design team", patterns=("select",))
    wrapper = Node("Group", (110, 104, 310, 136), patterns=("invoke",), children=[item])
    page(Node("Group", (100, 100, 400, 300), children=[wrapper]))
    el = client(wrapper).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "TreeItem" and el.name == "Design team" and el.refined == "down"


def test_an_unnamed_clickable_wrapper_with_nothing_named_under_it_takes_the_named_ancestor():
    wrapper = Node("Group", (110, 104, 190, 136), patterns=("invoke",), children=[Node("Image", (112, 106, 130, 130))])
    link = Node("Hyperlink", (100, 100, 200, 140), "Pricing", patterns=("invoke",), children=[wrapper])
    page(Node("Group", (50, 50, 500, 300), children=[link]))
    el = client(wrapper).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Hyperlink" and el.refined == "up"


def test_an_unnamed_clickable_wrapper_with_nothing_named_around_stands():
    wrapper = Node("Group", (110, 104, 190, 136), patterns=("invoke",))
    page(wrapper)                                # its parent is the whole page: never the control
    el = client(wrapper).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Group" and el.refined is None and el.patterns == ("invoke",) and not el.low_info()


def test_an_unnamed_actionable_wrapper_never_wins_over_a_named_actionable_ancestor_on_the_way_up():
    """Without a name below, the upward walk from an actionable start passes unnamed actionable Groups by."""
    wrapper = Node("Group", (110, 104, 190, 136), patterns=("invoke",))
    outer = Node("Group", (105, 102, 195, 138), patterns=("invoke",), children=[wrapper])
    btn = Node("Button", (100, 100, 200, 140), "Publish", patterns=("invoke",), children=[outer])
    page(Node("Group", (50, 50, 500, 300), children=[btn]))
    el = client(wrapper).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Button" and el.name == "Publish"


def test_a_renderer_descent_ending_on_a_clickable_wrapper_keeps_the_named_control_above_it():
    wrapper = Node("Group", (140, 110, 220, 130), patterns=("invoke",))
    link = Node("Hyperlink", (140, 110, 220, 130), "Pricing", patterns=("invoke",), children=[wrapper])
    win = page(Node("Group", (100, 100, 400, 200), children=[link]))
    doc = win.children[0].children[0]
    el = client(doc).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Hyperlink" and el.refined == "path"


# -- V2-4: the window under the point at the answer --------------------------------------------------------------------


def test_the_client_reads_the_window_under_the_point_right_when_element_from_point_answers(monkeypatch):
    seen: list = []

    def root_at(x, y):
        seen.append((x, y))
        return 11 if len(seen) == 1 else 99            # a later read would see the window the click brought up

    monkeypatch.setattr(U, "_root_at", root_at)
    inner = Node("Group", (110, 105, 190, 135))
    Node("Button", (100, 100, 200, 140), "Publish", patterns=("invoke",), children=[inner])
    el = client(inner).at_point(150, 120, budget_s=0.15, refine=True, max_area=WINDOW_AREA)
    assert el.role == "Button" and el.root == 11 and seen == [(150, 120)]


class _BringsAWindowUp(FakeUia):
    """The lookup answers from the pressed window; while the walk and the ancestry still run, the click's own outcome
    (another app's window) appears over the point."""

    def __init__(self, clock, rig) -> None:
        super().__init__(clock)
        self.rig = rig

    def at_point(self, x, y, *, budget_s, index=False):
        el = super().at_point(x, y, budget_s=budget_s, index=index)
        self.clock.t += 0.14
        self.rig.under = (HWND[BROWSER], BROWSER)
        return el


def _press_bringing_a_window_up(rig, el):
    rig.uia = rig.w.uia = _BringsAWindowUp(rig.clock, rig)
    rig.uia.points[(150, 120)] = el
    rig.w._root_of = lambda h: h
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False)
    rig.feed("fg", t + 0.12, HWND[BROWSER], BROWSER)
    rig.settle()
    return [r for r in rig.rows() if r["kind"] == "click"]


def test_a_window_the_click_brings_over_its_point_is_its_outcome_when_the_elements_window_is_known(rig):
    (row,) = _press_bringing_a_window_up(rig, button(window=HWND[APP]))
    assert row["resolved"] == "point" and not rig.w.counters["stale_root_changed"]


def test_with_the_elements_window_unknown_the_window_read_at_the_answer_decides(rig):
    (row,) = _press_bringing_a_window_up(rig, button(root=HWND[APP]))      # read when ElementFromPoint answered
    assert row["resolved"] == "point"


def test_another_window_under_the_point_when_the_lookup_answered_is_still_stale(rig):
    (row,) = _press_bringing_a_window_up(rig, button(root=HWND[VAULT]))
    assert row["resolved"] == "stale" and rig.w.counters["stale_root_changed"] == 1


def test_another_top_level_window_owning_the_element_is_still_stale(rig):
    (row,) = _press_bringing_a_window_up(rig, button(window=HWND[BROWSER]))
    assert row["resolved"] == "stale" and rig.w.counters["stale_other_window"] == 1


def test_an_element_without_a_window_or_an_answer_root_falls_back_to_reading_now(rig):
    (row,) = _press_bringing_a_window_up(rig, button())
    assert row["resolved"] == "stale" and rig.w.counters["stale_root_changed"] == 1
