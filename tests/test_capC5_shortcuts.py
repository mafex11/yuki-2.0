"""capC5 (report 78 §7.3, §7.5; report 102): the user's shortcuts.

Where the trace shows the user reaches a control with a chord, the way's step is a keys step. The thinker sees each
chord with what it did, worded from the chord rows' own recorded outcomes ("a dialog opens", "then you use ..."), so
the choice of the shortcut is its judgement. Chords only from the user's own rows, never Yuki's; a chord is sent only
into the app's verified window (one the run acted in, one that came up during it, or the one in front when it began).

Offline: temp databases and a stand-in desk; invented apps and names.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_capC1b_gotos import act
from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC4_skills import el
from test_capC5_guard import Desk5, btn, clicks, path_of, rec, run, way
from yuki.agent import frame as F
from yuki.memory import trace_demos as TD
from yuki.memory import trace_habits as H
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


def outcome(store: Any, rid: int, summary: dict[str, Any]) -> None:
    store._write(lambda conn: conn.execute("UPDATE ui_actions SET outcome_ciphertext = ? WHERE id = ?",
                                           (store._enc(json.dumps(summary)), rid)))


# -- what each shortcut did, worded from its own recorded outcomes ----------------------------------------------------

def test_each_chord_is_shown_with_what_it_did_from_its_recorded_outcomes(store):
    for k in range(3):
        t = NOW + k * 60
        outcome(store, act(store, t, "chatter", "keys", resolved="focus", chord="Ctrl+K"),
                {"dialog": 1, "focus_moved": 1, "shown": 4})
        act(store, t + 1.5, "chatter", "click", role="Button", name="Jump to", key="k-jump")
    outcome(store, act(store, NOW + 400, "chatter", "keys", resolved="focus", chord="Ctrl+Shift+M"), {"state": 2})
    H.update(store, now=NOW + 500)
    [entry] = H.go_tos(store, ["chatter"], now=NOW + 500)
    by = {c["chord"]: c for c in entry["chords"]}
    did = by["Ctrl+K"]["did"]
    assert did.startswith("a dialog opens (3 of 3); the keyboard moves to another control (3 of 3)")
    assert len(did) <= H.DID_CHARS
    assert "did" not in by["Ctrl+Shift+M"]        # nothing it did that the trace's classes word: no words made up
    lines = F.go_to_lines({"go_tos": [entry]})
    assert lines[0] == F.GOTO_HEAD and "send it only into that app's window" in F.GOTO_HEAD
    assert "presses Ctrl+K (3 times: a dialog opens (3 of 3)" in lines[1]
    # the control used right after it, named only as the registry keeps it
    assert H.chord_did({"n": 3, "on": 3, "x": {"k-jump": {"n": 3, "r": "Button"}}}, {"k-jump": "Jump to"}) == \
        'then you use Button "Jump to" (3 of 3)'
    assert H.chord_did({"n": 4, "on": 4, "o": {"dialog": 1}}, {}) == ""   # seen once in four: not what it does


def test_chords_and_what_followed_come_only_from_the_users_own_rows(store):
    for k in range(4):
        t = NOW + k * 60
        outcome(store, act(store, t, "chatter", "keys", source="yuki", resolved="focus", chord="Ctrl+J"),
                {"dialog": 1})
        outcome(store, act(store, t + 10, "chatter", "keys", resolved="focus", chord="Ctrl+K"), {"menu_opened": 1})
        act(store, t + 11, "chatter", "click", source="yuki", role="Button", name="Yukis own", key="k-yuki")
    H.update(store, now=NOW + 500)
    [entry] = H.go_tos(store, ["chatter"], now=NOW + 500)
    assert [c["chord"] for c in entry["chords"]] == ["Ctrl+K"]          # never Yuki's Ctrl+J
    assert entry["chords"][0]["did"] == "a menu opens (4 of 4)"           # never "then you use" Yuki's click
    # the demo learner never makes a step of Yuki's row; the user's chord is a keys step
    rows = {r["id"]: dict(r) for r in store._query(f"SELECT {TD.COLUMNS} FROM ui_actions")}
    yukis = [r for r in rows.values() if r["chord"] == "Ctrl+J"]
    users = [r for r in rows.values() if r["chord"] == "Ctrl+K"]
    assert TD.row_step(store, yukis[0], None) is None and TD.usable(yukis[0]) == "not the user's own action"
    step = TD.row_step(store, users[0], None)
    assert step is not None and step["do"] == "keys" and step["keys"] == ["ctrl", "k"]


# -- a chord goes only into the app's verified window --------------------------------------------------------------

def keys(*k: str) -> dict[str, Any]:
    return {"do": "keys", "keys": list(k)}


def hotkeys(desk: Any) -> list[dict[str, Any]]:
    return [c[1] for c in desk.calls if c[0] == "hotkey"]


def _desk(path: dict[str, Any] | None = None, **extra: dict[str, Any]) -> Desk5:
    windows = {100: {"title": "Reports - Sheets", "process_name": "sheets"},
               200: {"title": "Notes", "process_name": "notesy"},
               300: {"title": "Budget - Sheets", "process_name": "sheets"}}
    return Desk5({**windows, **extra}, {100: [el(1, "Button", "Reports")], 300: []}, 100,
                 path=path or path_of(rec(), rec()))


def test_a_shortcut_goes_into_the_window_the_run_acted_in():
    desk = _desk()
    ok, _r, facts = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert ok and hotkeys(desk) == [{"keys": ["ctrl", "e"], "expect_hwnd": 100}]


def test_a_shortcut_as_the_first_step_goes_into_the_apps_window_in_front_when_it_began():
    desk = _desk()
    ok, _r, _f = run(desk, way(keys("ctrl", "e")))
    assert ok and hotkeys(desk) == [{"keys": ["ctrl", "e"], "expect_hwnd": 100}]


def test_a_shortcut_never_goes_into_another_window_of_the_app_the_run_never_worked_in():
    desk = _desk()
    desk.effects[1] = lambda: setattr(desk, "front", 300)    # another sheets window, open before the run, comes up
    ok, report, facts = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert not ok and hotkeys(desk) == [] and facts["deviated_at"] == 2
    assert "a Sheets window this way had not worked in was in front" in report
    assert "no key was sent" in facts["results"][-1].report


def test_a_shortcut_goes_into_a_window_that_came_up_during_the_run():
    # second verification N5: brought up by the step just before (its own report saw it) whose recording had a new
    # window after it
    desk = _desk(path=path_of({**rec(), "new_window": True}, rec()))

    def opened() -> None:
        desk.windows[400] = {"title": "Export - Sheets", "process_name": "sheets"}
        desk.front = 400
    desk.effects[1] = opened
    ok, _r, _f = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert ok and hotkeys(desk) == [{"keys": ["ctrl", "e"], "expect_hwnd": 400}]


def test_a_shortcut_never_goes_into_another_app():
    desk = _desk()
    desk.effects[1] = lambda: setattr(desk, "front", 200)
    ok, report, facts = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert not ok and hotkeys(desk) == [] and facts["deviation"] == "window"
    assert "Notesy was in front, not the app the shortcut is for" in report


def test_a_shortcut_recorded_in_the_same_window_never_goes_into_a_new_one():
    desk = Desk5({100: {"title": "Reports - Sheets", "process_name": "sheets"}}, {100: [el(1, "Button", "Reports")]},
                 100, path=path_of(rec(), rec(same=True)))

    def opened() -> None:
        desk.windows[400] = {"title": "Export - Sheets", "process_name": "sheets"}
        desk.front = 400
    desk.effects[1] = opened
    ok, report, facts = run(desk, way(btn("Reports"), keys("ctrl", "e")))
    assert not ok and hotkeys(desk) == [] and facts["deviation"] == "window" and clicks(desk) == [1]
