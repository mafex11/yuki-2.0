"""capC5 review fixes (report 102, "Review fixes"): the RuntimeId re-find, takeover inside a step, dialogs of the
way only, a switch whose report could not tell, an app's own modal window in the recording.

F1: a pick by the user's words never falls back to the item picked for another name earlier in the session.
F2: a RuntimeId hit must still bear the name it had (a count added is the same control; a toggle renamed is not);
    with several name matches it only chooses among them.
F3: a takeover while a step looks for its control or is being checked stops the step before it acts.
F4: a new owned window of another app, owned by no window the run acted in, never stops the way.
F5: a way with no recording whose last step opens its dialog is a verified run, as before capC5.
F6: after a switch whose report could not tell, the next "same window" step is not held to the earlier app's window.
F7: an app's own modal window, recorded as a window change after the step, is on the path on replay.

Offline: the stand-in desk of test_capC5_guard; invented apps and names.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC4_skills import el, item_of
from test_capC5_guard import (Desk5, Watch, _chat_way, btn, clicks, path_of, rec, run, sheets_desk, way)
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


def _item(el_id: int, name: str, rid: list[int]) -> dict[str, Any]:
    return {**el(el_id, "ListItem", name, parent=1), "runtime_id": rid}


# -- F1: a pick by the user's words matches the name said now --------------------------------------------------------

def test_a_pick_by_name_never_falls_back_to_the_item_picked_for_another_name_in_the_session():
    tree = [el(1, "List", "Chats"), _item(11, "Alice Moss", [7, 1]), _item(12, "Carol Pine", [7, 2])]
    desk = Desk5({100: {"title": "Chatter", "process_name": "chatter"}}, {100: tree}, 100)
    pick = {"do": "pick", "in": {"role": "List"}, "role": "ListItem", "app": "chatter", "slot": "person or chat"}
    item = item_of([{"run": [pick], "end": {"kind": "selected", "app": "chatter"}}], apps=["chatter"])
    runner = S.SkillRunner(desk)
    ok, _r, _f = runner.run({"steps": [{"do": "pick", "text": "Alice Moss"}]}, item=item, words=["open Alice Moss"])
    assert ok and clicks(desk) == [11]
    # the same voice session, a name not in the visible list: nothing is clicked (never Alice's chat again)
    ok, report, facts = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Bob Lane"}]}, item=item,
                                                words=["open Bob Lane"])
    assert not ok and clicks(desk) == [11] and facts["deviation"] == "not_found"
    # nothing of a pick by name is kept for a re-find
    assert not S._RIDS
    step = S.SkillStep(i=1, do="pick", role="ListItem", app="chatter", box_role="List", slot="person or chat",
                       text="Bob Lane")
    S.remember_rid("s", step, 100, "chatter", tree[1], now=NOW)
    assert not S._RIDS and S.recall_rid("s", step, 100, "chatter", tree, now=NOW) is None


# -- F2: the RuntimeId hit must still bear its name -------------------------------------------------------------------

def test_a_toggle_whose_name_flipped_is_not_clicked_again_by_its_runtime_id():
    windows = {100: {"title": "Talky", "process_name": "talky"}}
    desk = Desk5(windows, {100: [{**el(5, "Button", "Mute"), "runtime_id": [3, 5]}]}, 100)
    item = way(btn("Mute"), apps=("talky",))
    ok, _r, _f = run(desk, item, ["mute me"])
    assert ok and clicks(desk) == [5]
    desk.trees[100] = [{**el(5, "Button", "Unmute"), "runtime_id": [3, 5]}]   # already muted: the name flipped
    ok, report, facts = run(desk, item, ["mute me"])
    assert not ok and clicks(desk) == [5] and facts["deviation"] == "not_found"


def test_a_count_added_to_the_name_is_still_the_same_control_and_several_matches_still_stop():
    step = S.SkillStep(i=1, do="click", role="Button", name="Inbox")
    S.remember_rid("s", step, 100, "mailer", {"runtime_id": [1, 2], "role": "Button", "name": "Inbox"}, now=NOW)
    grown = {"id": 5, "runtime_id": [1, 2], "role": "Button", "name": "Inbox 3 unread"}
    flipped = {"id": 5, "runtime_id": [1, 2], "role": "Button", "name": "Outbox"}
    assert S.recall_rid("s", step, 100, "mailer", [grown], now=NOW + 5) == grown
    assert S.recall_rid("s", step, 100, "mailer", [flipped], now=NOW + 5) is None
    # several controls of that name: ambiguous, the run stops (second verification N1: the RuntimeId is a re-find,
    # never a tie-break)
    windows = {100: {"title": "Sheets", "process_name": "sheets"}}
    desk = Desk5(windows, {100: [{**el(8, "Button", "Export"), "runtime_id": [9, 8]}]}, 100)
    ok, _r, _f = run(desk, way(btn("Export")))
    assert ok and clicks(desk) == [8]
    desk.trees[100] = [{**el(7, "Button", "Export"), "runtime_id": [9, 7]},
                       {**el(8, "Button", "Export"), "runtime_id": [9, 8]}]
    ok, report, facts = run(desk, way(btn("Export")))
    assert not ok and clicks(desk) == [8] and facts["deviation"] == "not_found" and "2 controls fit" in report
    # one control, its name grown by a count: the RuntimeId finds it again
    desk.trees[100] = [{**el(8, "Button", "Export 2 new"), "runtime_id": [9, 8]}]
    ok, _r, facts = run(desk, way(btn("Export")))
    assert ok and clicks(desk) == [8, 8] and "an earlier step of this session" in facts["results"][0].why


# -- F3: a takeover inside a step stops it before it acts ---------------------------------------------------------------

class _TakeDesk(Desk5):
    """Fires the takeover watch on a chosen read of a window or a chosen judge call."""

    def __init__(self, *a: Any, fire_on: str, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.fire_on, self.watch, self.reads = fire_on, None, 0

    def _read_tree(self, hwnd: int) -> Any:
        if self.fire_on == "read" and clicks(self) and self.watch is not None:
            self.watch.fired = "mouse"            # the user grabs the mouse while step 2 looks for its control
        return super()._read_tree(hwnd)

    def authorize(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any], earlier: list[str],
                  force: bool = False) -> None:
        super().authorize(tool, tool_input, target, earlier, force=force)
        if self.fire_on == "judge" and len(self.judged) == 2 and self.watch is not None:
            self.watch.fired = "keys"             # the user starts typing while step 2 is being checked
        return None


@pytest.mark.parametrize("fire_on", ["read", "judge"])
def test_a_takeover_while_a_step_looks_or_is_checked_stops_it_before_it_acts(fire_on):
    windows = {100: {"title": "Reports - Sheets", "process_name": "sheets"}}
    desk = _TakeDesk(windows, {100: [el(1, "Button", "Reports"), el(2, "Button", "Export")]}, 100,
                     path=path_of(rec(), rec(same=True)), fire_on=fire_on)

    def factory(on_takeover: Any) -> Watch:
        desk.watch = Watch(on_takeover)
        return desk.watch

    ok, report, facts = S.SkillRunner(desk, takeover=factory).run(
        {"steps": []}, item=way(btn("Reports"), btn("Export")), words=["export the reports"])
    assert not ok and clicks(desk) == [1]                     # step 2 never clicked
    assert facts["took_over_at"] == 2 and facts["deviation"] is None and facts["settled"] is True
    assert report.startswith("I stopped at step 2: you took over.") and desk.watch.stopped


# -- F4: another app's popup is not the way's dialog --------------------------------------------------------------------

def test_another_apps_popup_elsewhere_never_stops_a_good_run():
    desk = sheets_desk(path_of(rec(dialog=False), rec(same=True)))
    desk.effects[1] = lambda: desk.windows.__setitem__(201, {"title": "Reminder", "process_name": "notesy",
                                                             "owner_hwnd": 200})
    ok, _r, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and facts["verified"] is True and clicks(desk) == [1, 105]
    # a way with no recording: the same popup does not stop its next step either
    desk = sheets_desk({"steps": [], "recorded": False})
    desk.effects[1] = lambda: desk.windows.__setitem__(201, {"title": "Reminder", "process_name": "notesy",
                                                             "owner_hwnd": 200})
    ok, _r, _f = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 105]


# -- F5: a way with no recording may end on the dialog it opens --------------------------------------------------------

def test_a_way_with_no_recording_whose_last_step_opens_its_dialog_is_verified():
    desk = Desk5({100: {"title": "Notes", "process_name": "notesy"}}, {100: [el(3, "Button", "Print")]}, 100)

    def opens() -> None:
        desk.windows[101] = {"title": "Print", "process_name": "notesy", "owner_hwnd": 100}
        desk.front = 101
    desk.effects[3] = opens
    item = item_of([{"run": [btn("Print")], "end": {"kind": "selected", "app": "notesy"}}], apps=["notesy"])
    assert not item.get("trace") and not item.get("demo")
    ok, report, facts = run(desk, item, ["open the print dialog"])
    assert ok and facts["verified"] is True and facts["deviation"] is None and "dialog" not in report


# -- F6: a switch whose report could not tell leaves no stale window ----------------------------------------------------

class _UnsureDesk(Desk5):
    def _act(self, name: str, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ok, report, facts = super()._act(name, args, facts)
        facts["verified"] = None                  # the launcher's report could not confirm the window
        return ok, report, facts


def test_after_a_switch_whose_report_could_not_tell_the_next_step_is_not_held_to_the_old_window():
    windows = {100: {"title": "Reports - Sheets", "process_name": "sheets"},
               200: {"title": "Notes", "process_name": "notesy"}}
    desk = _UnsureDesk(windows, {100: [el(1, "Button", "Reports")], 200: [el(5, "Button", "Save")]}, 100,
                       path=path_of(rec("sheets"), rec("notesy"), rec("notesy", same=True)))
    item = way(btn("Reports"), {"do": "open_app", "name": "notesy"}, btn("Save"), apps=("sheets", "notesy"))
    ok, report, facts = run(desk, item, ["save the notes"])
    assert ok and facts["deviation"] is None and clicks(desk) == [1, 5], report


# -- F7: an app's own modal window recorded as a window change ------------------------------------------------------------

def test_an_owned_window_after_a_step_recorded_as_an_owned_window_is_on_the_path():
    # second verification N4: the trace now says the window that came up was owned (the way_path's "dialog"); a new
    # top-level window recorded ("new_window") no longer lets an owned window through
    def desk_for(path: dict[str, Any]) -> Desk5:
        desk = sheets_desk(path, export_in=(101,), trees={100: [el(1, "Button", "Reports")]})
        desk.effects[1] = lambda: (desk.windows.__setitem__(101, {"title": "Settings", "process_name": "sheets",
                                                                  "owner_hwnd": 100}), setattr(desk, "front", 101))
        return desk

    desk = desk_for(path_of({**rec(), "dialog": True}, rec(same=True)))
    ok, _r, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 106]
    desk = desk_for(path_of({**rec(), "new_window": True}, rec(same=True)))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and clicks(desk) == [1] and facts["deviation"] == "dialog"


def test_the_recorded_path_marks_a_window_change_after_a_step(store):
    o = _chat_way(store)
    actions = o.body["trace"]["actions"]
    row = store._query("SELECT outcome_ciphertext FROM ui_actions WHERE id = ?", (actions[2],))[0][0]
    summary = json.loads(store._dec(row) or "{}") if row else {}
    summary["window_changed"] = 1             # an app's own modal window came to the front after it
    store._write(lambda conn: conn.execute("UPDATE ui_actions SET outcome_ciphertext = ? WHERE id = ?",
                                           (store._enc(json.dumps(summary)), actions[2])))
    path = R.way_path(store, o.id)
    assert path["steps"][2]["new_window"] is True and path["steps"][2]["dialog"] is False
    assert path["steps"][3]["same_window"] is False           # something came to the front between them
