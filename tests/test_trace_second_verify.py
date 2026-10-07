"""capC1 second verification (report 96, "Second verification"): one test per confirmed finding, V1 to V9.

V1 the page gate fails closed for a window the timeline has no settled reading of; V2 a drag's drop window is checked
before anything in it is looked up; V3 a blocked app brought to the front is never named; V4 software pointer jumps,
Yuki's runs and Warp commands get the right source; V5 a blind window's click is point-only; V6 folded repeats share one
outcome cap; V7 the .flp reader stays bounded in memory; V8 a save chord re-checks only that app's files; V9 the
acceptance analyzer's log, chord and FL Studio checks. Every app, name, site and file is invented.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import time
import tracemalloc
from pathlib import Path

import pytest
from test_trace_controller import _consent, env  # noqa: F401 - the env fixture
from test_trace_worker import APP, BROWSER, HWND, OWN, PROCS, VAULT, WALL0, FakeSink, button, rig  # noqa: F401

from yuki.memory import flp, trace_hooks, trace_store
from yuki.memory.store import Store
from yuki.memory.trace import PAGE_UNKNOWN, timeline_page
from yuki.memory.trace_hooks import EVENT_OBJECT_SHOW, OUTCOME_CAP
from yuki.memory.trace_input import Metrics, TraceInput
from yuki.memory.trace_privacy import TraceRules
from yuki.memory.trace_uia import Element

ROOT = Path(__file__).resolve().parents[1]
OTHER = 105                     # a second browser (its own process)
WARP = 106
LAUNCHED = 107                  # an app the shell started for a file


@pytest.fixture(autouse=True)
def _more_apps(monkeypatch):
    monkeypatch.setitem(PROCS, OTHER, ("browsery", "browsery.exe", "Browser Y", False, 9.0))
    monkeypatch.setitem(PROCS, WARP, ("warp", "warp.exe", "Warp", False, 10.0))
    monkeypatch.setitem(PROCS, LAUNCHED, ("beatmaker", "beatmaker.exe", "Beat Maker", False, time.time() + 1.0))


class St:
    """A timeline stretch as the trace reads it."""

    def __init__(self, pid, hwnd, host=None, path=None, *, settled=True, withheld=None) -> None:
        self.pid, self.hwnd, self.host, self.path, self.settled, self.withheld = pid, hwnd, host, path, settled, withheld
        self.seen_host, self.seen_path = host, path
        self.process, self.title, self.fullscreen, self.meeting = PROCS[pid][1], "Invented page", False, None


class Tl:
    def __init__(self) -> None:
        self._current = None
        self._no_page: dict[int, tuple[int, float]] = {}
        self._title_first = None


def _real_pages(rig) -> Tl:
    tl = Tl()
    rig.w._page = timeline_page(lambda: tl, rig.store)
    return tl


# -- V1: the page gate ------------------------------------------------------------------------------------------------


def test_the_timeline_page_answers_only_for_the_settled_window_itself(rig):
    tl = Tl()
    page = timeline_page(lambda: tl, rig.store)
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/")
    key, url, withheld = page(BROWSER, HWND[BROWSER])
    assert key and url == "https://news.example/" and withheld is None
    assert page(BROWSER, 77) == (None, None, PAGE_UNKNOWN)       # another window of the same browser
    assert page(APP, HWND[APP]) == (None, None, None)            # never showed a page: no page, not unknown
    tl._no_page[OTHER] = (0, 0.0)                                # the timeline's last probe of it found a page
    assert page(OTHER, 55) == (None, None, PAGE_UNKNOWN)
    tl._no_page[OTHER] = (3, 0.0)
    assert page(OTHER, 55) == (None, None, None)
    tl._title_first = 1.0                                        # Ctrl+Tab: a title change still settling
    assert page(BROWSER, HWND[BROWSER]) == (None, None, PAGE_UNKNOWN)
    tl._title_first = None
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/", settled=False)   # probe not answered yet
    assert page(BROWSER, HWND[BROWSER]) == (None, None, PAGE_UNKNOWN)
    tl._current = St(BROWSER, HWND[BROWSER])                     # settled, but the probe found no page
    assert page(BROWSER, HWND[BROWSER]) == (None, None, PAGE_UNKNOWN)


def test_scrolling_or_resting_over_a_browser_window_behind_records_and_looks_up_nothing(rig):
    tl = _real_pages(rig)
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/")
    tl._no_page[OTHER] = (0, 0.0)
    rig.fg = (HWND[BROWSER], BROWSER)
    rig.w._page(BROWSER, HWND[BROWSER])                          # the trace saw it on a page
    rig.uia.points[(300, 300)] = Element(role="Button", name="Transfer funds", pid=BROWSER, bounds=(250, 250, 350, 350))
    rig.under = (77, BROWSER)                                    # (a) same browser, the window behind
    rig.feed("wheel", rig.clock.t, -1, False, 300, 300, "user", 0)
    sink = FakeSink()
    rig.w.sink = sink
    rig.under = (66, OTHER)                                      # (b) another browser, not in front
    rig.uia.points[(150, 120)] = Element(role="Button", name="Transfer funds", pid=OTHER, bounds=(100, 100, 200, 140))
    rig.clock.t += 2.0
    sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 1, True
    rig.feed("moving", rig.clock.t)
    rig.settle()
    assert rig.rows() == [] and rig.uia.calls == []
    assert rig.w.counters["dropped_withheld"] == 2


def test_typing_right_after_a_tab_switch_or_into_a_page_that_turns_out_blocked_is_dropped(rig):
    tl = _real_pages(rig)
    rig.fg = (HWND[BROWSER], BROWSER)
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/")
    rig.w._page(BROWSER, HWND[BROWSER])
    tl._title_first = 1.0                                        # (c) Ctrl+Tab: the new tab's title still settling
    for _ in range(6):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    rig.settle()
    assert rig.rows() == []
    tl._title_first = None                                       # typing on the known page, then it settles blocked
    for _ in range(6):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    tl._current = St(BROWSER, HWND[BROWSER], "my.bank.example", "/")
    rig.settle()
    assert rig.rows() == []


def test_a_link_clicked_on_a_known_page_is_kept_while_the_next_page_loads(rig):
    tl = _real_pages(rig)
    rig.under = rig.fg = (HWND[BROWSER], BROWSER)
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/")
    rig.uia.points[(150, 120)] = Element(role="Hyperlink", name="Invented story", pid=BROWSER,
                                         bounds=(100, 100, 300, 140), ancestors=[("Document", "")])
    rig.click()
    tl._current = St(BROWSER, HWND[BROWSER], settled=False)      # the next page is loading
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "click" and row["role"] == "Hyperlink"
    tl._current = St(BROWSER, HWND[BROWSER], "news.example", "/story")
    for _ in range(4):                                           # typing on a known page, then Enter loads the next
        rig.feed("char", rig.clock.t, 1, "user", dt=0.05)
    tl._current = St(BROWSER, HWND[BROWSER], settled=False)
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["click", "type"]


# -- V2: the drop window ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", [VAULT, OWN])
def test_a_drag_onto_a_blocked_or_own_window_keeps_no_end_element(rig, target):
    rig.uia.points[(150, 120)] = Element(role="ListItem", name="Invented file", pid=APP, bounds=(100, 100, 300, 140),
                                         ancestors=[("List", "files")])
    rig.uia.points[(800, 500)] = Element(role="Button", name="Unlock vault", pid=target, bounds=(780, 480, 840, 520))
    rig.w._window_at = lambda x, y: (HWND[APP], APP) if x < 500 else (HWND[target], target)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("up", t + 0.6, "left", 800, 500, "user", True, dt=0.6)
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "drag" and row["role"] == "ListItem"
    assert row["end_role"] is None and row["end_name_ciphertext"] is None and row["end_element_key"] is None
    assert ("point", 800, 500) not in rig.uia.calls and rig.w.counters["drag_end_withheld"] == 1


# -- V3: the app brought to the front ---------------------------------------------------------------------------------


def test_a_blocked_app_brought_to_the_front_is_never_named_on_the_row(rig):
    rig.uia.points[(150, 120)] = button()
    rig.click()
    rig.feed("fg", rig.clock.t + 0.3, HWND[VAULT], VAULT, dt=0.3)
    rig.settle()
    rows = rig.rows()
    assert [r["kind"] for r in rows] == ["click"]                 # no switch row for the blocked app
    row = rows[0]
    assert all("vaultkeeper" not in str(v) for v in row.values())
    assert json.loads(rig.store._dec(row["outcome_ciphertext"]))["window_changed"] == 1
    assert row["facts"] is None


# -- V4: sources ------------------------------------------------------------------------------------------------------


class Cursor:
    def __init__(self) -> None:
        self.x, self.y = 500, 400

    def __call__(self):
        return self.x, self.y


def _sink(cur):
    items: list = []
    s = TraceInput(items.append, metrics=Metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5, double_w=4,
                                                 double_h=4, drag_w=4, drag_h=4, read=False),
                   cursor=cur, clock=lambda: 0.0)
    s.blocked = False
    return s, items


def test_a_pointer_placed_by_software_never_starts_a_rest_the_user_seems_to_make():
    cur = Cursor()
    s, items = _sink(cur)
    rng = random.Random(3)
    t = 0.0

    def jitter(seconds):
        nonlocal t
        for _ in range(int(23 * seconds)):
            t += 1 / 23
            s.mouse(t, 0, 0, 0, rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1)), 0, 1)

    jitter(1.0)
    cur.x, cur.y = 900, 300                                       # Yuki's SetCursorPos: no Raw Input of its own
    jitter(1.0)
    assert items == [] and s.counts["jumps"] == 1 and s.jump_seq == s.anchor_seq
    assert round(s.travel) == 0
    for _ in range(20):                                           # the user moves the mouse for real
        t += 0.005
        cur.x += 3
        s.mouse(t, 0, 0, 0, 3, 0, 0, 1)
    assert items and items[0][0] == "moving" and s.jump_seq != s.anchor_seq


def test_a_rest_begun_by_a_jump_is_not_a_hover(rig):
    sink = FakeSink()
    sink.jump_seq = 1
    rig.w.sink = sink
    rig.uia.points[(150, 120)] = button(name="Settings")
    sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 1, True
    rig.feed("moving", rig.clock.t)
    rig.settle()
    assert rig.rows() == [] and rig.uia.calls == [] and rig.w.counters["hover_after_jump"] == 1


def test_an_app_yuki_opens_after_the_ask_bar_enter_is_yukis_and_a_real_takeover_stays_the_users(rig):
    rig.fg = (HWND[OWN], OWN)
    rig.feed("char", rig.clock.t, 5, "user")
    rig.feed("key", rig.clock.t + 0.1, "named", "Enter", "user", 0, dt=0.1)
    rig.acting = object()
    rig.feed("fg", rig.clock.t + 0.8, HWND[APP], APP, dt=0.8)
    rig.settle()
    assert [(r["kind"], r["source"], r["during_run"]) for r in rig.rows()] == [("switch", "yuki", 1)]
    rig.fg = (HWND[APP], APP)
    rig.uia.points[(150, 120)] = button()
    rig.click()                                                   # the user's own click in the app: a takeover
    rig.feed("fg", rig.clock.t + 0.3, HWND[BROWSER], BROWSER, dt=0.3)
    rig.settle()
    assert [(r["kind"], r["source"]) for r in rig.rows()][1:] == [("click", "user"), ("switch", "user")]


def test_warp_commands_take_their_source_and_run_from_the_enter_that_ran_them(rig):
    rig.fg = (HWND.get(WARP, 16), WARP)
    rig.uia.focus = Element(role="Edit", name="", pid=WARP, bounds=(0, 0, 900, 50))
    rig.acting = object()
    enter_wall = WALL0 + rig.clock.t
    rig.feed("key", rig.clock.t, "named", "Enter", "yuki", 0)
    rig.acting = None
    rig.settle()
    rig.feed("cmd", [{"command": "invented-build --fast", "at": enter_wall + 0.2, "exit_code": 0},
                     {"command": "invented-check", "at": enter_wall + 300, "exit_code": 0, "during_run": True},
                     {"command": "invented-list", "at": enter_wall + 600, "exit_code": 0}])
    rig.settle()
    cmds = [(r["source"], r["during_run"]) for r in rig.rows() if r["kind"] == "command"]
    assert cmds == [("yuki", 1), ("yuki", 1), ("user", 0)]


def test_the_controller_marks_commands_run_during_a_yuki_run(env):  # noqa: F811
    trace, privacy, gate, attached = env

    class Acting:
        started_at = time.time()

    state = {"acting": None}
    trace._acting = lambda: state["acting"]
    _consent(privacy, True)
    trace.beat()
    state["acting"] = Acting()
    trace.beat()
    during = time.time()
    time.sleep(0.01)
    state["acting"] = None
    trace.beat()
    time.sleep(1.1)
    after = time.time()
    assert trace.run_at(during) is True and trace.run_at(after + 5) is False


# -- V5: blind windows ------------------------------------------------------------------------------------------------


def test_clicks_in_a_blind_window_are_point_only_with_their_points(rig):
    whole = Element(role="Pane", name="Invented Project - Beat Maker", pid=APP, bounds=(0, 0, 1000, 800))
    rig.uia.points[(150, 120)] = whole
    rig.uia.points[(700, 600)] = whole
    rig.click(150, 120)
    rig.settle()
    rig.click(700, 600)
    rig.settle()
    rows = rig.rows()
    assert [(r["role"], r["resolved"], r["point"], r["element_key"], r["name_ciphertext"]) for r in rows] == [
        ("Pane", "unresolved", "150,120", None, None), ("Pane", "unresolved", "700,600", None, None)]
    sink = FakeSink()
    rig.w.sink = sink
    sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 1, True
    rig.cursor = (700, 600)
    rig.feed("moving", rig.clock.t)
    rig.settle()
    assert len(rig.rows()) == 2 and rig.w.counters["skip_hover_unresolved"] == 1   # no whole-window hover


# -- V6: folded repeats share one outcome cap -------------------------------------------------------------------------


def test_repeats_that_fold_do_not_renew_the_outcome_cap():
    folds: list[tuple[float, bool]] = []
    s = TraceInput(lambda item: None, metrics=Metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5, double_w=4,
                                                      double_h=4, drag_w=4, drag_h=4, read=False),
                   cursor=lambda: (0, 0), on_action=lambda t, fold: folds.append((t, fold)), clock=lambda: 0.0)
    s._action(1.0, ("key", "Down"))
    s._action(1.03, ("key", "Down"))
    s._action(1.06, None)                                         # Enter: never a fold
    s._action(1.07, None)
    s._action(1.1, ("wheel", False, False))
    s._action(1.2, ("wheel", False, False))
    s._action(3.0, ("wheel", False, False))                       # too late: a new action
    assert [f for _t, f in folds] == [False, True, False, False, False, True, False]
    h = trace_hooks.TraceHooks(lambda item: None, pid_of=lambda hwnd: (77,), own_pid=1, recent=None,
                               clock=lambda: 5.0)
    h.blocked, h._fg, h.gen, h._n = False, 900, 1, 0
    for i in range(150):
        h._on_event(None, trace_hooks.EVENT_OBJECT_SHOW, 900, -4, i, 0, 0)
    h.blocked = True                                              # (no timer in a test)
    h._open_outcome(fold=True)
    h.blocked = False
    for i in range(150):
        h._on_event(None, trace_hooks.EVENT_OBJECT_SHOW, 900, -4, i, 0, 0)
    assert h.gen == 1 and h.counts["outcome"] == OUTCOME_CAP and h.counts["outcome_over"] == 100
    h.blocked = True
    h._open_outcome(fold=False)
    assert h.gen == 2 and h._n == 0


def test_one_folded_row_keeps_at_most_the_cap_of_outcome_events(rig):
    for i in range(30):
        t = rig.clock.t
        rig.feed("key", t, "named", "Down", "user", 0)
        for j in range(250):
            rig.feed("out", t + 0.001, EVENT_OBJECT_SHOW, 9, -4, j, i)
        rig.clock.t += 0.03
    rig.settle()
    (row,) = rig.rows()
    assert row["n"] == 30
    assert json.loads(rig.store._dec(row["outcome_ciphertext"]))["shown"] == OUTCOME_CAP
    assert rig.w.counters["outcome_over_row"] == 30 * 250 - OUTCOME_CAP


# -- V7: the .flp reader is bounded in memory -------------------------------------------------------------------------


def test_an_oversized_name_or_a_big_file_costs_no_more_than_a_chunk(tmp_path):
    data = flp.build([flp.event(flp.EV_TITLE, "ab " * (2 * 2**20)), flp.event(flp.EV_TEMPO, 128000)])
    assert len(data) > 12 * 2**20
    tracemalloc.start()
    try:
        facts = flp.parse_bytes(data)
        _cur, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        path = tmp_path / "Invented Big.flp"
        path.write_bytes(data)
        del data
        tracemalloc.reset_peak()
        from_file = flp.read_flp(path)
        _cur, peak_file = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert facts.project.startswith("ab ab") and len(facts.project) == flp.MAX_TEXT and facts.tempo == 128.0
    assert from_file.as_dict() == facts.as_dict()
    assert peak_bytes < 256 * 1024 and peak_file < 2 * 2**20
    path.write_bytes(path.read_bytes()[:-5])                      # cut short: fails closed
    with pytest.raises(flp.FlpError):
        flp.read_flp(path)


# -- V8: a save chord re-checks only that app's files -----------------------------------------------------------------


def test_ctrl_s_in_one_app_never_claims_another_apps_file(tmp_path, rig):
    rig.rules = TraceRules.from_dict({"record_actions": True, "blocked_folders": ["Z:/invented-vault"]})
    rig.w._wall = time.time
    log = tmp_path / "invented-build.log"
    log.write_text("x")
    os.utime(log, (time.time() - 60, time.time() - 60))
    rig.w._known_files.append((str(log), "d1", "fieldnotes", time.time() - 60))
    rig.fg = (HWND[BROWSER], BROWSER)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0)
    log.write_text("xy")                                          # its own program appends meanwhile
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["keys"]
    rig.fg = (HWND[APP], APP)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=3.0)
    log.write_text("xyz")
    rig.settle()
    assert sorted((r["kind"], r["process"]) for r in rig.rows()) == [
        ("file_save", "fieldnotes"), ("keys", "browserx"), ("keys", "fieldnotes")]


def test_a_file_the_shell_opened_belongs_to_the_app_it_launched(rig):
    rig.w._wall = time.time
    rig.w._known_files.append(("Z:/invented/Groove.flp", "d2", "fieldnotes", time.time()))
    rig.feed("fg", rig.clock.t + 1.0, 17, LAUNCHED, dt=1.0)       # started after the file was opened
    assert rig.w._known_files[-1][2] == "beatmaker"
    rig.w._known_files.append(("Z:/invented/Notes.txt", "d3", "fieldnotes", time.time()))
    rig.feed("fg", rig.clock.t + 1.0, HWND[BROWSER], BROWSER, dt=1.0)   # running long before: not adopted
    assert rig.w._known_files[-1][2] == "fieldnotes"


# -- V9: the acceptance analyzer --------------------------------------------------------------------------------------


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_analyzer_normalises_logs_reads_chords_and_matches_fl_studio_exactly(tmp_path):
    mod = _load("trace_acceptance")
    db = tmp_path / "memory.db"
    s = Store.open(db)
    t0 = time.time() - 100
    rows = [{"at": t0 + i, "process": "flow.launcher", "kind": "keys", "source": "user", "chord": c}
            for i, c in enumerate(["A", "Ctrl+Alt+B", "1", "2"])]
    trace_store.write_batch(s, rows)
    s.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "memory-3.jsonl").write_text('{"type": "x", "note": "violet otter 4417"}\n', encoding="utf-8")
    assert mod.scan_for_marker(db, "violet-otter-4417", logs) == {"log:memory-3.jsonl": 1}
    report = mod.analyze(db, t0 - 1, t0 + 60, {}, steps=False, marker="ab-12")
    assert report["fl_studio_rows"] == 0
    assert report["typed_like_chords"] == 3 and report["altgr_like_chords"] == 1 and report["chords_spell_marker"]
    checks = {what: ok for what, ok, _v in mod.verdict(report, {})}
    assert checks["FL Studio maximised: rows present"] is False
    assert checks["no letters or digits recorded as chords without Ctrl, Alt or Win"] is False
    assert checks["the chords' keys, in order, never spell the marker"] is False
