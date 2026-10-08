"""capC5 second verification (report 102, "Second verification"): N1-N6.

N1: the RuntimeId is a re-find, never a tie-break - several controls of the step's name stop the run as ambiguous, and
    a run that picks an item by the user's words never re-finds anything by RuntimeId.
N2: a UWP app's window (the frame host's process in the window list) is compared by the app it hosts, as the trace
    records it; the words to the user name the app, never a process file.
N3: a dialog that came up after the step before was looked at is checked just before a step acts.
N4: the trace says whether the window that came to the front was owned; only an owned one recorded after a step lets
    an owned window through on replay (a new top-level window recorded does not).
N5: a shortcut goes into a window that came up during the run only when the step just before brought it up (its own
    report saw it) and, when recorded, its recording had a window change too.
N6: the live check's pass rests on 10 requests with learning on; off requests are other requests, compared by the
    time against the user's usual.

Offline: the stand-in desk of test_capC5_guard, the trace worker's rig, synthetic analysed trials; invented apps and
names.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC4_skills import el, item_of
from test_capC5_guard import Desk5, _chat_way, btn, clicks, path_of, rec, run, sheets_desk, way
from test_capC5_live_check import C5
from test_capC5_shortcuts import hotkeys, keys
from test_trace_worker import BROWSER, HWND, button, rig  # noqa: F401 - the rig fixture
from yuki.memory import trace as T
from yuki.memory import trace_replay as R
from yuki.voice import skills as S


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    for name, value in (("FIRST_FIND_WAIT_S", 0.0), ("FIND_WAIT_S", 0.0), ("AFTER_OPEN_WAIT_S", 0.0),
                        ("FOCUS_WAIT_S", 0.0), ("EXPECT_WAIT_S", 0.0), ("END_WAIT_S", 0.0), ("POLL_S", 0.0)):
        monkeypatch.setattr(S, name, value)
    S._RIDS.clear()
    R.set_use_learned(True)
    yield
    R.set_use_learned(None)
    S._RIDS.clear()


# -- N1: the RuntimeId never chooses among several -----------------------------------------------------------------------

def _device_row(row: int, name: str) -> list[dict[str, Any]]:
    return [el(row, "ListItem", name, parent=1),
            {**el(row + 1, "Button", "More options", parent=row), "runtime_id": [5, row + 1]}]


def _device_way() -> dict[str, Any]:
    pick = {"do": "pick", "in": {"role": "List"}, "role": "ListItem", "app": "devices", "slot": "device"}
    return item_of([{"run": [pick, btn("More options")], "end": {"kind": "selected", "app": "devices"}}],
                   apps=["devices"])


def test_a_click_after_a_pick_by_name_never_goes_to_the_row_an_earlier_request_used():
    desk = Desk5({100: {"title": "Devices", "process_name": "devices"}},
                 {100: [el(1, "List", "Devices"), *_device_row(10, "Earbuds One")]}, 100)
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Earbuds One"}]}, item=_device_way(),
                                         words=["open Earbuds One options"])
    assert ok and clicks(desk) == [10, 11]
    # the same session: two rows now, each with its own "More options"
    desk.trees[100] = [el(1, "List", "Devices"), *_device_row(10, "Earbuds One"), *_device_row(20, "Speaker Two")]
    ok, report, facts = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Speaker Two"}]},
                                                item=_device_way(), words=["open Speaker Two options"])
    assert not ok and clicks(desk) == [10, 11, 20]           # Speaker Two picked; never Earbuds One's button
    assert facts["deviation"] == "not_found" and facts["deviated_at"] == 2 and "2 controls fit" in report


def test_a_run_with_a_pick_by_name_never_re_finds_by_runtime_id():
    desk = Desk5({100: {"title": "Devices", "process_name": "devices"}},
                 {100: [el(1, "List", "Devices"), *_device_row(10, "Earbuds One")]}, 100)
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Earbuds One"}]}, item=_device_way(),
                                         words=["open Earbuds One options"])
    assert ok
    # the button's name grew (a count); without the pick its RuntimeId would find it again - with it, never
    desk.trees[100] = [el(1, "List", "Devices"), el(10, "ListItem", "Earbuds One", parent=1),
                       {**el(11, "Button", "More options 2", parent=10), "runtime_id": [5, 11]}]
    ok, _r, facts = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Earbuds One"}]}, item=_device_way(),
                                            words=["open Earbuds One options"])
    assert not ok and facts["deviation"] == "not_found" and clicks(desk) == [10, 11, 10]


# -- N2: a UWP app is its hosted app ------------------------------------------------------------------------------------

def _settings_desk(monkeypatch, path: dict[str, Any]) -> Desk5:
    monkeypatch.setattr(S, "hosted_app", lambda hwnd, pid: "SystemSettings.exe" if pid == 77 else "")
    windows = {100: {"title": "Settings", "process_name": "ApplicationFrameHost.exe", "pid": 77}}
    return Desk5(windows, {100: [el(1, "ListItem", "Bluetooth & devices"), el(2, "Button", "Add device")]}, 100,
                 path=path)


@pytest.mark.parametrize("recorded", [True, False])
def test_a_way_in_a_uwp_app_runs_in_its_frame_window(monkeypatch, recorded):
    path = path_of(rec("systemsettings"), rec("systemsettings", same=True)) if recorded else None
    desk = _settings_desk(monkeypatch, path)
    item = way({"do": "click", "role": "ListItem", "name": "Bluetooth & devices"}, btn("Add device"),
               apps=("systemsettings",))
    ok, report, facts = run(desk, item, ["add a bluetooth device"])
    assert ok and clicks(desk) == [1, 2] and facts["deviation"] is None, report
    # a plain way (not learned from the trace) that names the app, too
    plain = item_of([{"run": [{"do": "click", "role": "ListItem", "name": "Bluetooth & devices"}, btn("Add device")],
                      "end": {"kind": "selected", "app": "systemsettings"}}], apps=["systemsettings"])
    ok, _r, _f = run(desk, plain, ["add a bluetooth device"])
    assert ok and clicks(desk) == [1, 2, 1, 2]


def test_the_words_name_the_app_never_its_process_file(monkeypatch):
    desk = _settings_desk(monkeypatch, path_of(rec("sheets")))
    ok, report, facts = run(desk, way({"do": "click", "role": "ListItem", "name": "Bluetooth & devices"}),
                            ["open bluetooth"])
    assert not ok and facts["deviation"] == "app" and ".exe" not in report and "SystemSettings" in report


def test_hosted_app_is_empty_for_a_window_that_is_not_a_uwp_frame():
    assert S.hosted_app(0, 0) == "" and S.hosted_app(100, 0) == ""
    assert S.hosted_app(100, 999_999_999) == ""                # not that process's window: not told


# -- N3: a dialog that came up late --------------------------------------------------------------------------------------

class LateDesk(Desk5):
    """A Desk5 whose owned dialog comes up only after a given number of further looks at the desktop."""

    def __init__(self, *a: Any, after: int, title: str = "Replace the old export?", **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.after, self.title, self.armed = after, title, None

    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        if self.armed is not None:
            self.armed -= 1
            if self.armed <= 0:
                self.windows[101] = {"title": self.title, "process_name": "sheets", "owner_hwnd": 100}
                self.front, self.armed = 101, None
        return super().overview_text()


def _late(path: dict[str, Any] | None, after: int, *, export_in: tuple[int, ...] = (101,)) -> LateDesk:
    trees = {100: [el(1, "Button", "Reports")], 101: []}
    for h in export_in:
        trees[h].append(el(5 + h, "Button", "Export"))
    desk = LateDesk({100: {"title": "Reports - Sheets", "process_name": "sheets"}}, trees, 100,
                    path=path or {"steps": [], "recorded": False}, after=after)
    desk.effects[1] = lambda: setattr(desk, "armed", desk.after)
    return desk


@pytest.mark.parametrize("after", [1, 2, 3])
def test_a_late_dialog_the_recording_never_had_stops_before_the_next_step_acts(after):
    desk = _late(path_of(rec(dialog=False), rec(same=True)), after)
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog", (after, report)
    assert "never came up when you did it" in report


def test_a_late_dialog_in_front_while_the_step_acts_in_the_main_window_stops():
    desk = _late(path_of(rec(dialog=False), rec(same=True)), 2, export_in=(100,))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog"


def test_a_late_dialog_the_recording_had_is_on_the_path():
    desk = _late(path_of(rec(dialog=True), rec(same=True)), 3)
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 106], report


def test_a_late_dialog_with_no_recording_is_taken_only_when_the_step_acts_inside_it():
    desk = _late(None, 3)
    ok, report, _f = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 106], report
    desk = _late(None, 2, export_in=(100,))
    desk.trees[101] = []
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog" and "the way never goes through" in report


class _JudgeDialogDesk(Desk5):
    """A dialog comes up while Yuki's own check looks at step 2."""

    def authorize(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any], earlier: list[str],
                  force: bool = False) -> None:
        super().authorize(tool, tool_input, target, earlier, force=force)
        if len(self.judged) == 2:
            self.windows[101] = {"title": "Replace the old export?", "process_name": "sheets", "owner_hwnd": 100}
            self.front = 101
        return None


def test_a_dialog_that_comes_up_while_the_step_is_checked_stops_it_before_it_acts():
    desk = _JudgeDialogDesk({100: {"title": "Reports - Sheets", "process_name": "sheets"}},
                            {100: [el(1, "Button", "Reports"), el(105, "Button", "Export")], 101: []}, 100,
                            path=path_of(rec(dialog=False), rec(same=True)))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog", report


# -- N4: owned and top-level windows apart -------------------------------------------------------------------------------

def test_a_new_top_level_window_recorded_never_lets_an_owned_dialog_through():
    desk = sheets_desk(path_of({**rec(), "new_window": True}, rec()), export_in=(101,),
                       trees={100: [el(1, "Button", "New report")]})
    desk.trees[100] = [el(1, "Button", "Reports")]
    desk.effects[1] = lambda: (desk.windows.__setitem__(101, {"title": "Save changes to Report 1?",
                                                              "process_name": "sheets", "owner_hwnd": 100}),
                               setattr(desk, "front", 101))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog", report


def test_a_dialog_elsewhere_in_the_apps_recordings_no_longer_allows_one_after_this_step():
    desk = sheets_desk(path_of(rec(dialog=False), rec(same=True), dialog_apps=["sheets"]), export_in=(101,),
                       trees={100: [el(1, "Button", "Reports")]})
    desk.effects[1] = lambda: (desk.windows.__setitem__(101, {"title": "Unsaved", "process_name": "sheets",
                                                              "owner_hwnd": 100}), setattr(desk, "front", 101))
    ok, _r, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "dialog" and clicks(desk) == [1]


def _set_summary(store: Any, action: int, **counts: int) -> None:
    row = store._query("SELECT outcome_ciphertext FROM ui_actions WHERE id = ?", (action,))[0][0]
    summary = json.loads(store._dec(row) or "{}") if row else {}
    summary.update(counts)
    store._write(lambda conn: conn.execute("UPDATE ui_actions SET outcome = 'changed', outcome_ciphertext = ? WHERE"
                                           " id = ?", (store._enc(json.dumps(summary)), action)))


def test_the_recorded_path_keeps_an_owned_window_as_a_dialog_and_a_top_level_one_as_a_new_window(store):
    o = _chat_way(store)
    actions = o.body["trace"]["actions"]
    _set_summary(store, actions[1], window_changed=1, owned_window=1)
    _set_summary(store, actions[2], window_changed=1, top_window=1)
    path = R.way_path(store, o.id)
    assert path["steps"][1]["dialog"] is True and path["steps"][1]["new_window"] is False
    assert path["steps"][2]["dialog"] is False and path["steps"][2]["new_window"] is True


@pytest.mark.parametrize("owned", [True, False])
def test_the_trace_records_whether_the_window_brought_to_the_front_was_owned(rig, monkeypatch, owned):
    monkeypatch.setattr(T, "_owned", lambda hwnd: owned and hwnd == HWND[BROWSER])
    rig.uia.points[(150, 120)] = button()
    rig.click()
    rig.feed("fg", rig.clock.t + 0.3, HWND[BROWSER], BROWSER, dt=0.3)
    rig.settle()
    row = next(r for r in rig.rows() if r["kind"] == "click")
    summary = json.loads(rig.store._dec(row["outcome_ciphertext"]))
    assert summary["window_changed"] == 1
    assert summary.get("owned_window", 0) == (1 if owned else 0) and summary.get("top_window", 0) == (0 if owned else 1)


def test_owned_is_false_for_no_window():
    assert T._owned(0) is False


# -- N5: a chord only into a window a step brought up -------------------------------------------------------------------

class _ArrivesDesk(Desk5):
    """A window of the app comes to the front by itself, a few looks after step 1 (never in its report)."""

    def __init__(self, *a: Any, owned: bool = False, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.armed, self.owned = None, owned

    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        if self.armed is not None:
            self.armed -= 1
            if self.armed <= 0:
                self.windows[300] = {"title": "Incoming call", "process_name": "chatter",
                                     **({"owner_hwnd": 100} if self.owned else {})}
                self.front, self.armed = 300, None
        return super().overview_text()


def _chat_desk(path: dict[str, Any] | None, **kw: Any) -> _ArrivesDesk:
    desk = _ArrivesDesk({100: {"title": "Team room - Chatter", "process_name": "chatter"}},
                        {100: [el(1, "Button", "Team room")], 300: []}, 100,
                        path=path or {"steps": [], "recorded": False}, **kw)
    desk.effects[1] = lambda: setattr(desk, "armed", 1)
    return desk


@pytest.mark.parametrize("path", [None, path_of({**rec("chatter"), "new_window": True}, rec("chatter"))])
def test_a_shortcut_never_goes_into_a_window_that_came_up_by_itself(path):
    desk = _chat_desk(path)
    ok, report, facts = run(desk, way(btn("Team room"), keys("ctrl", "enter"), apps=("chatter",)), ["post it"])
    assert not ok and hotkeys(desk) == [] and facts["deviation"] == "window", report
    assert "a Chatter window this way had not worked in was in front" in report


def test_a_shortcut_never_goes_into_a_window_a_step_brought_up_when_its_recording_had_none():
    desk = Desk5({100: {"title": "Reports - Sheets", "process_name": "sheets"}}, {100: [el(1, "Button", "Reports")]},
                 100, path=path_of(rec(), rec()))

    def opened() -> None:
        desk.windows[400] = {"title": "Export - Sheets", "process_name": "sheets"}
        desk.front = 400
    desk.effects[1] = opened
    ok, report, facts = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert not ok and hotkeys(desk) == [] and facts["deviation"] == "window", report


def test_a_shortcut_goes_into_a_window_a_step_brought_up_with_no_recording_or_a_dialog_of_its_window():
    desk = Desk5({100: {"title": "Reports - Sheets", "process_name": "sheets"}}, {100: [el(1, "Button", "Reports")]},
                 100, path={"steps": [], "recorded": False})

    def opened() -> None:
        desk.windows[400] = {"title": "Export - Sheets", "process_name": "sheets"}
        desk.front = 400
    desk.effects[1] = opened
    ok, report, _f = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert ok and hotkeys(desk) == [{"keys": ["ctrl", "e"], "expect_hwnd": 400}], report
    # an owned window of the window the run acted in, recorded as a dialog after the step
    desk = _chat_desk(path_of({**rec("chatter"), "dialog": True}, rec("chatter")), owned=True)
    ok, report, _f = run(desk, way(btn("Team room"), keys("ctrl", "enter"), apps=("chatter",)), ["post it"])
    assert ok and hotkeys(desk) == [{"keys": ["ctrl", "enter"], "expect_hwnd": 300}], report


# -- N6: the live check's pass ---------------------------------------------------------------------------------------------

def _trials(on_done: list[bool], off_done: list[bool], *, on_ratio: float = 1.0, off_ratio: float = 1.2
            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plan = C5.make_plan([f"invented on request {i}" for i in range(len(on_done))],
                        off_requests=[f"invented off request {i}" for i in range(len(off_done))], now=NOW)
    done = {True: iter(on_done), False: iter(off_done)}
    out = []
    for t in plan["trials"]:
        d = next(done[t["on"]])
        ratio = (on_ratio if t["on"] else off_ratio) if d else None
        out.append({"n": t["n"], "request": t["request"], "on": t["on"], "done": d, "ratio": ratio,
                    "seconds": 60.0 if d else None, "deviations": 0, "handed_over": 0})
    return out, plan


def test_the_default_plan_passes_only_on_seven_of_ten_requests_with_learning_on(monkeypatch):
    monkeypatch.setattr(C5, "DEFAULT_REQUESTS", 10)
    plan = C5.make_plan([f"invented request {i}" for i in range(10)], now=NOW)
    assert all(t["on"] for t in plan["trials"]) and len(plan["trials"]) == 10
    # the verifier's case: 4 of 5 "on" requests done is no pass any more
    trials, plan = _trials([True, True, True, True, False], [False] * 5)
    v = C5.verdict(trials, plan)
    assert not v["parts"]["enough"] and not v["parts"]["done"] and v["need"] == 7 and not v["passed"]
    trials, plan = _trials([True] * 7 + [False] * 3, [True] * 5 + [False] * 5)
    v = C5.verdict(trials, plan)
    assert v["parts"]["enough"] and v["parts"]["done"] and v["parts"]["better"] and v["passed"]
    assert "other requests of yours, so compared by the time against your usual" in C5.report(
        {"trials": [], "verdict": v, "requests": plan["requests"]})


def test_better_than_off_compares_shares_then_time_against_the_usual(monkeypatch):
    monkeypatch.setattr(C5, "DEFAULT_REQUESTS", 10)
    # the same share done: the lower time against the usual decides, never raw seconds of other requests
    trials, plan = _trials([True] * 8 + [False] * 2, [True] * 4 + [False], on_ratio=1.4, off_ratio=0.9)
    assert not C5.verdict(trials, plan)["parts"]["better"]
    trials, plan = _trials([True] * 8 + [False] * 2, [True] * 4 + [False], on_ratio=0.8, off_ratio=0.9)
    assert C5.verdict(trials, plan)["parts"]["better"]
    # no requests with learning off: better cannot be told
    trials, plan = _trials([True] * 10, [])
    v = C5.verdict(trials, plan)
    assert not v["parts"]["better"] and not v["passed"]
    assert "no requests were done with learning off" in C5.report({"trials": [], "verdict": v,
                                                                     "requests": plan["requests"]})
