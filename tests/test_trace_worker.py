"""capC1 (report 96): the trace worker, driven item by item with fakes on a temp database.

Covers: identity at button-down (point, event path, renderer descent), staleness, the outcome window, batching, the
hard game gate (zero rows, zero lookups), the privacy gates (password fails closed, blocked app, blocked site,
private window, leave-out list, Yuki's own windows), source labels and Yuki runs, typing counts, hovers from rests,
drags, the clipboard-to-paste join, Warp command rows, switches, navigation, files and .flp facts, content-free logs.
Every app, name and file is invented; nothing touches the desktop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from yuki.memory import flp, trace_store
from yuki.memory.privacy import PrivacyRules
from yuki.memory.store import Store
from yuki.memory.trace import FLUSH_S, OUTCOME_S, TraceWorker
from yuki.memory.trace_hooks import (EVENT_OBJECT_FOCUS, EVENT_OBJECT_INVOKED, EVENT_OBJECT_SHOW,
                                     EVENT_OBJECT_STATECHANGE)
from yuki.memory.trace_privacy import TraceRules
from yuki.memory.trace_uia import Element

WALL0 = 1_780_000_000.0
APP, BROWSER, VAULT, OWN = 101, 102, 103, 104
PROCS = {APP: ("fieldnotes", "fieldnotes.exe", "Field Notes", False, 5.0),
         BROWSER: ("browserx", "browserx.exe", "Browser X", False, 6.0),
         VAULT: ("vaultkeeper", "vaultkeeper.exe", "Vault Keeper", False, 7.0),
         OWN: ("yuki", "pythonw.exe", "Yuki", True, 8.0)}
HWND = {APP: 11, BROWSER: 12, VAULT: 13, OWN: 14}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def button(name="Send", **kw) -> Element:
    return Element(role="Button", name=name, aid="send-btn", rid=(1, 2), bounds=(100, 100, 200, 140), pid=APP,
                   parent_role="ToolBar", ancestors=[("ToolBar", ""), ("Pane", ""), ("Window", "")], **kw)


class FakeUia:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple] = []
        self.points: dict[tuple[int, int], Element] = {}
        self.events: dict[tuple, Element] = {}
        self.focus: Element | None = Element(role="Edit", name="Message", pid=APP, bounds=(0, 0, 900, 50))
        self.state: dict = {}
        self.slow_s = 0.0

    def at_point(self, x, y, *, budget_s, index=False):
        self.calls.append(("point", x, y))
        self.clock.t += self.slow_s
        return self.points.get((x, y))

    def from_event(self, hwnd, oid, cid, *, budget_s, index=False):
        self.calls.append(("event", hwnd, oid, cid))
        return self.events.get((hwnd, oid, cid))

    def focused(self, *, budget_s):
        self.calls.append(("focus",))
        return self.focus

    def descend(self, pane, x, y, *, budget_s):
        self.calls.append(("descend", x, y))
        return None

    def reread(self, el, value_length=False):
        self.calls.append(("reread", value_length))
        return dict(self.state)


class Rig:
    def __init__(self, tmp: Path, **rules) -> None:
        self.store = Store.open(tmp / "memory.db")
        self.clock = Clock()
        self.uia = FakeUia(self.clock)
        self.gate = False
        self.acting = None
        self.pages: dict[int, tuple] = {}
        self.titles: dict[int, str] = {}
        self.fg = (HWND[APP], APP)
        self.under = (HWND[APP], APP)
        self.clip = (40, ("CF_UNICODETEXT",), False)
        self.cursor = (150, 120)
        self.log: list[tuple] = []
        self.rules = TraceRules.from_dict({"record_actions": True, **rules})
        self.privacy = PrivacyRules.from_dict({
            "apps": {"blocked_processes": ["vaultkeeper.exe"]},
            "private_windows": [{"process": "browserx.exe", "title_endswith": " - Private"}],
            "web": {"blocked_host_suffixes": ["bank.example"]},
        })
        self.recent: Path | None = None
        self.links: dict[str, str] = {}
        self.sink = None
        self.w = TraceWorker(
            self.store, privacy_rules=lambda: self.privacy, trace_rules=lambda: self.rules, gate=lambda: self.gate,
            acting=lambda: self.acting, page=lambda pid, hwnd=0: self.pages.get(pid, (None, None, None)),
            process=lambda pid: PROCS[pid], window_at=lambda x, y: self.under, foreground=lambda: self.fg,
            title=lambda h: self.titles.get(h, "Notes"), rect=lambda h: (0, 0, 1000, 800), uia=self.uia,
            sink=None, cursor=lambda: self.cursor, clipboard=lambda: self.clip, recent=None,
            resolve=lambda p: self.links.get(os.path.basename(p)), log=lambda t, **f: self.log.append((t, f)),
            clock=self.clock, wall=lambda: WALL0 + self.clock.t, hover=(4, 4, 0.4))

    def feed(self, *item, dt: float = 0.0) -> None:
        self.clock.t += dt
        self.w.handle(item)

    def settle(self, seconds: float = 5.0) -> None:
        for _ in range(int(seconds / 0.1)):
            self.clock.t += 0.1
            self.w.tick()
        self.w.flush()

    def click(self, x=150, y=120, source="user", button_name="left", dt=0.05) -> None:
        t = self.clock.t
        self.feed("down", t, button_name, x, y, source, 0, False)
        self.clock.t += dt
        self.feed("up", self.clock.t, button_name, x, y, source, False)

    def rows(self) -> list[dict]:
        return [dict(r) for r in self.store._query("SELECT * FROM ui_actions ORDER BY at, id")]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.store.close()


def test_a_click_on_a_button_is_one_row_with_its_element_taken_at_button_down(rig):
    rig.uia.points[(150, 120)] = button()
    rig.pages[APP] = ("page-a", None, None)
    rig.feed("down", rig.clock.t, "left", 150, 120, "user", 42, False)
    assert rig.uia.calls == [("point", 150, 120)]          # asked at button-down, before the release
    rig.clock.t += 0.08
    rig.feed("up", rig.clock.t, "left", 150, 120, "user", False)
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "click" and row["role"] == "Button" and row["resolved"] == "point"
    assert rig.store._dec(row["name_ciphertext"]) == "Send" and row["source"] == "user"
    assert row["process"] == "fieldnotes" and row["page_key"] == "page-a" and row["travel_px"] == 42
    assert row["point"] is None and row["outcome"] == "nothing"
    el = rig.store._query("SELECT * FROM ui_elements")[0]
    assert el["n"] == 1 and rig.store._dec(el["name_ciphertext"]) == "Send"


def test_the_event_path_names_the_element_when_the_app_raises_it(rig):
    pane = Element(role="Pane", name="", bounds=(0, 0, 400, 300), pid=APP)
    rig.uia.points[(150, 120)] = pane
    rig.under = (HWND[APP], APP)
    rig.w._rect = lambda h: (0, 0, 1000, 800)                 # the pane is not the whole window: no descent
    rig.uia.events[(55, -4, 7)] = button(name="Archive")
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("ev", t + 0.06, EVENT_OBJECT_INVOKED, 55, -4, 7, dt=0.06)
    rig.feed("up", t + 0.07, "left", 150, 120, "user", False, dt=0.01)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] == "event" and rig.store._dec(row["name_ciphertext"]) == "Archive"


def test_a_structure_change_before_the_answer_marks_the_row_stale(rig):
    rig.uia.points[(150, 120)] = button()
    rig.uia.slow_s = 0.2                                      # the lookup answered 200 ms after button-down
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("out", t + 0.05, EVENT_OBJECT_SHOW, 9, 0, 0, 1)   # a menu appeared meanwhile
    rig.feed("up", t + 0.25, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


def test_the_outcome_window_closes_after_its_time(rig):
    rig.uia.points[(150, 120)] = button()
    t = rig.clock.t
    rig.click()
    rig.feed("out", t + 0.3, EVENT_OBJECT_STATECHANGE, 9, -4, 1, 1, dt=0.25)
    rig.feed("out", t + OUTCOME_S + 0.6, EVENT_OBJECT_STATECHANGE, 9, -4, 2, 1, dt=OUTCOME_S + 0.3)
    rig.settle()
    row = rig.rows()[0]
    assert row["outcome"] == "changed"
    summary = json.loads(rig.store._dec(row["outcome_ciphertext"]))
    assert summary == {"state": 1}                            # the late event was outside the window


def test_writes_are_batched_at_most_every_two_seconds(rig):
    rig.uia.points[(150, 120)] = button()
    writes = []
    real = trace_store.write_batch
    trace_store_write = lambda *a, **k: writes.append(len(a[1])) or real(*a, **k)   # noqa: E731
    import yuki.memory.trace as trace_mod
    trace_mod.trace_store.write_batch = trace_store_write
    try:
        for _ in range(5):
            rig.click()
            for _ in range(5):
                rig.clock.t += 0.1
                rig.w.tick()
        for _ in range(30):
            rig.clock.t += 0.1
            rig.w.tick()
    finally:
        trace_mod.trace_store.write_batch = real
    assert sum(writes) == 5 and len(writes) <= 3
    assert FLUSH_S == 2.0


def test_nothing_at_all_while_the_game_gate_holds(rig):
    rig.uia.points[(150, 120)] = button()
    rig.gate = True
    rig.click()
    rig.feed("char", rig.clock.t, 1, "user")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0)
    rig.feed("clip", rig.clock.t, 41, APP, ("CF_TEXT",), False)
    rig.feed("cmd", [{"command": "git status", "at": WALL0, "exit_code": 0}])
    rig.settle()
    assert rig.rows() == [] and rig.uia.calls == []           # zero rows, zero lookups
    assert rig.w.counters["dropped_game"] >= 4


def test_a_gate_that_starts_holding_mid_action_drops_it_and_looks_nothing_up(rig):
    rig.uia.points[(150, 120)] = button()
    rig.uia.events[(55, -4, 7)] = button(name="Archive")
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("ev", t + 0.02, EVENT_OBJECT_INVOKED, 55, -4, 7, dt=0.02)   # queued before the gate held
    rig.gate = True
    rig.feed("up", rig.clock.t + 0.05, "left", 150, 120, "user", False)
    rig.settle()
    assert rig.rows() == [] and rig.uia.calls == [("point", 150, 120)]   # no event lookup, no re-read after


def test_password_fields_record_nothing_not_even_a_count(rig):
    rig.uia.points[(150, 120)] = Element(role="Edit", name="Password", password=True, bounds=(0, 0, 900, 900),
                                         pid=APP)
    rig.click()
    for _ in range(8):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.1)
    rig.settle()
    assert rig.rows() == []


def test_typing_fails_closed_when_the_field_cannot_be_checked(rig):
    rig.uia.focus = None                                      # the app did not answer within budget
    for _ in range(5):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["dropped_password_unknown"] == 1


def test_typing_is_a_count_per_field_and_enter_after_it_can_prove_a_submit(rig):
    rig.uia.focus = Element(role="Edit", name="Message", pid=APP, bounds=(0, 0, 900, 50), ancestors=[("Group", "")])
    for _ in range(12):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.12)
    rig.uia.state = {"length": 0}
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.2)
    rig.settle()
    typed, enter = rig.rows()
    assert typed["kind"] == "type" and typed["typed"] == 12 and typed["role"] == "Edit"
    assert typed["name_ciphertext"] is None                   # an edit field is a content control: digest only
    assert typed["container_digest"] and typed["resolved"] == "focus"
    assert enter["chord"] == "Enter" and json.loads(enter["facts"])["field_submitted"] == {"typed": 12}


@pytest.mark.parametrize("case", ["blocked_app", "blocked_host", "private_window", "left_out", "own_window"])
def test_blocked_places_produce_nothing(tmp_path, case):
    r = Rig(tmp_path, **({"leave_out": ["Field Notes"]} if case == "left_out" else {}))
    try:
        pid = {"blocked_app": VAULT, "blocked_host": BROWSER, "private_window": BROWSER, "left_out": APP,
               "own_window": OWN}[case]
        r.under = r.fg = (HWND[pid], pid)
        if case == "blocked_host":
            r.pages[BROWSER] = ("page-b", "https://my.bank.example/accounts", None)
        if case == "private_window":
            r.titles[HWND[BROWSER]] = "Something - Private"
        r.uia.points[(150, 120)] = button()
        r.click()
        for _ in range(3):
            r.feed("char", r.clock.t, 1, "user", dt=0.1)
        r.feed("key", r.clock.t, "chord", "Ctrl+V", "user", 0, dt=0.1)
        r.settle()
        assert r.rows() == []
        assert ("point", 150, 120) not in r.uia.calls          # no lookup in a place that is never recorded
    finally:
        r.store.close()


def test_source_labels_yuki_tag_injected_and_a_takeover_during_a_run(rig):
    rig.uia.points[(150, 120)] = button()
    rig.click(source="yuki")
    rig.click(source="user_injected", dt=0.05)
    rig.acting = object()                                     # a Yuki run is in progress
    rig.clock.t += 1.0
    rig.click(source="user")
    rig.settle()
    rows = rig.rows()
    assert [(r["source"], r["during_run"]) for r in rows] == [("yuki", 0), ("user_injected", 0), ("user", 1)]


def test_an_event_without_input_is_yukis_during_a_run_and_ignored_otherwise(rig):
    rig.uia.events[(5, -4, 3)] = button(name="Play")
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_INVOKED, 5, -4, 3)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["event_only_ignored"] == 1
    rig.acting = object()
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_INVOKED, 5, -4, 3)
    rig.settle()
    (row,) = rig.rows()
    assert (row["kind"], row["source"], row["via"], row["during_run"]) == ("invoke", "yuki", "event_only", 1)


def test_a_double_click_is_one_row(rig):
    rig.uia.points[(150, 120)] = button()
    rig.click()
    rig.clock.t += 0.1
    rig.feed("down", rig.clock.t, "left", 150, 120, "user", 0, True)
    rig.feed("up", rig.clock.t + 0.05, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "dclick" and row["n"] == 2


def test_a_drag_keeps_its_start_and_end_elements(rig):
    rig.uia.points[(150, 120)] = Element(role="ListItem", name="Invented file", pid=APP, bounds=(100, 100, 300, 140),
                                         ancestors=[("List", "files")])
    rig.uia.points[(600, 500)] = Element(role="TreeItem", name="Archive folder", pid=APP, bounds=(550, 480, 700, 520),
                                         ancestors=[("Tree", "nav")])
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("up", t + 0.6, "left", 600, 500, "user", True, dt=0.6)
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "drag" and row["role"] == "ListItem" and row["end_role"] == "TreeItem"
    assert row["end_element_key"] and row["end_name_ciphertext"] is None   # content controls: digests only
    assert row["travel_px"] >= 800


class FakeSink:
    def __init__(self) -> None:
        self.anchor_at = 0.0
        self.anchor_seq = 0
        self.moving = False
        self.travel = 0.0
        self.move_source = "user"


def test_a_rest_becomes_one_hover_row_with_its_dwell_and_drift_is_coalesced(rig):
    sink = FakeSink()
    rig.w.sink = sink
    rig.uia.points[(150, 120)] = button(name="Settings")
    sink.anchor_at, sink.anchor_seq, sink.moving, sink.travel = rig.clock.t, 1, True, 380.0
    rig.feed("moving", rig.clock.t)
    rig.clock.t += 0.2
    rig.w.tick()
    assert rig.uia.calls == []                                # not yet Windows' hover time
    rig.clock.t += 0.3
    rig.w.tick()
    assert rig.uia.calls == [("point", 150, 120)] and sink.moving is False
    rig.clock.t += 2.0                                        # resting on it for 2 s more
    sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 2, True   # a slow drift crossed the rectangle
    rig.feed("moving", rig.clock.t)
    rig.cursor = (152, 121)
    rig.clock.t += 0.5
    rig.w.tick()
    rig.settle()
    (row,) = rig.rows()
    assert row["kind"] == "hover" and rig.store._dec(row["name_ciphertext"]) == "Settings"
    assert 2300 <= row["dwell_ms"] <= 2700 and row["travel_px"] == 380
    assert rig.w.counters["hover_coalesced"] == 1 and len(rig.uia.calls) == 1


def test_a_click_during_the_rest_is_not_a_hover(rig):
    sink = FakeSink()
    rig.w.sink = sink
    rig.uia.points[(150, 120)] = button()
    sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 1, True
    rig.feed("moving", rig.clock.t)
    rig.clock.t += 0.1
    rig.click()
    sink.anchor_at, sink.anchor_seq = rig.clock.t, 2          # the press reset the rest
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["click"]


def test_a_copy_and_its_paste_join_on_the_clipboard_sequence(rig):
    rig.feed("key", rig.clock.t, "chord", "Ctrl+C", "user", 0)
    rig.feed("clip", rig.clock.t + 0.05, 77, APP, ("CF_UNICODETEXT", "HTML Format"), False, dt=0.05)
    rig.under = rig.fg = (HWND[BROWSER], BROWSER)
    rig.clip = (77, ("CF_UNICODETEXT", "HTML Format"), False)
    rig.feed("key", rig.clock.t + 3, "chord", "Ctrl+V", "user", 0, dt=3)
    rig.settle()
    kinds = [(r["kind"], r["process"], r["clip_seq"]) for r in rig.rows()]
    assert ("copy", "fieldnotes", 77) in kinds and ("paste", "browserx", 77) in kinds
    joins = trace_store.paste_joins(rig.store, since=0)
    assert len(joins) == 1 and joins[0]["from"] == "fieldnotes" and joins[0]["to"] == "browserx"
    copy = next(r for r in rig.rows() if r["kind"] == "copy")
    assert json.loads(copy["clip_formats"]) == ["CF_UNICODETEXT", "HTML Format"]


def test_a_private_copy_or_a_copy_without_the_user_is_not_recorded(rig):
    rig.feed("clip", rig.clock.t, 80, APP, ("CF_TEXT",), False)                 # no input: an app's own write
    rig.feed("key", rig.clock.t, "chord", "Ctrl+C", "user", 0, dt=5)
    rig.feed("clip", rig.clock.t, 81, VAULT, ("CF_TEXT",), True, dt=0.05)       # a password manager's mark
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["keys"]


def test_warp_history_rows_become_command_actions(rig):
    rig.feed("cmd", [{"command": "pytest -q tests/test_invented.py", "at": WALL0 + 900, "exit_code": 0,
                      "seconds": 3.5},
                     {"command": "make build", "at": WALL0 + 950, "exit_code": 2},
                     {"command": "agent ran this", "at": WALL0 + 960, "exit_code": 0, "agent": True}])
    rig.settle()
    rows = rig.rows()
    assert [r["kind"] for r in rows] == ["command", "command"]
    assert rig.store._dec(rows[0]["command_ciphertext"]) == "pytest -q tests/test_invented.py"
    assert json.loads(rows[0]["facts"]) == {"command_ok": True} and rows[1]["facts"] is None
    assert rows[0]["process"] == "warp" and rows[0]["source"] == "user" and rows[1]["exit_code"] == 2


def test_alt_tab_then_a_new_window_is_a_switch_with_a_verified_window(rig):
    rig.feed("key", rig.clock.t, "chord", "Alt+Tab", "user", 0)
    rig.feed("fg", rig.clock.t + 0.3, HWND[BROWSER], BROWSER, dt=0.3)
    rig.settle()
    switch = next(r for r in rig.rows() if r["kind"] == "switch")
    assert switch["process"] == "browserx" and json.loads(switch["facts"]) == {"window_verified": "browserx"}


def test_a_click_that_reaches_a_new_page_records_the_navigation(rig):
    rig.under = rig.fg = (HWND[BROWSER], BROWSER)
    rig.uia.points[(150, 120)] = Element(role="Hyperlink", name="Invented article", pid=BROWSER,
                                         bounds=(100, 100, 300, 140), ancestors=[("Document", "")])
    rig.pages[BROWSER] = ("page-1", "https://news.example/", None)
    rig.click()
    rig.pages[BROWSER] = ("page-2", "https://news.example/story", None)
    rig.settle()
    click, nav = rig.rows()
    assert json.loads(click["facts"])["page_reached"] == "page-2" and nav["kind"] == "nav"
    assert nav["page_key"] == "page-2"


def test_a_toggle_click_records_its_new_state(rig):
    rig.uia.points[(150, 120)] = Element(role="CheckBox", name="Dark mode", toggle=0, pid=APP,
                                         bounds=(100, 100, 300, 140))
    rig.uia.state = {"toggle": "on"}
    t = rig.clock.t
    rig.click()
    rig.feed("out", t + 0.1, EVENT_OBJECT_STATECHANGE, 9, -4, 1, 1, dt=0.05)
    rig.settle()
    assert json.loads(rig.rows()[0]["facts"])["toggle_state"] == "on"


def test_an_unresolved_click_in_a_blind_window_keeps_only_its_point(rig):
    rig.click(x=320, y=240)                                   # nothing answers at that point (a GPU-drawn window)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] == "unresolved" and row["point"] == "320,240" and row["outcome"] == "unverifiable"
    assert row["role"] is None and row["element_key"] is None


def test_files_from_the_recent_folder_and_an_flp_project(tmp_path, rig):
    recent = tmp_path / "Recent"
    recent.mkdir()
    project = tmp_path / "Invented Beat.flp"
    project.write_bytes(flp.build([flp.event(flp.EV_TITLE, "Invented Beat"), flp.event(flp.EV_TEMPO, 128000),
                                   flp.event(flp.EV_NEW_CHANNEL, 0), flp.event(flp.EV_CHANNEL_TYPE, 2),
                                   flp.event(flp.EV_PLUGIN_INTERNAL, "Synth One"),
                                   flp.event(flp.EV_PLUGIN_NAME, "Lead")]))
    os.utime(project, (time.time() - 3600, time.time() - 3600))        # written an hour ago: an open
    vault = tmp_path / "vault"
    vault.mkdir()
    temp_file = vault / "invented-temp.txt"
    rig.rules = TraceRules.from_dict({"record_actions": True, "blocked_folders": [str(vault)]})
    rig.w._recent = recent
    rig.w._recent_seen = time.time() - 5
    rig.links = {"Invented Beat.flp.lnk": str(project), "invented-temp.txt.lnk": str(temp_file)}
    temp_file.write_text("x")
    try:
        (recent / "Invented Beat.flp.lnk").write_bytes(b"L")
        (recent / "invented-temp.txt.lnk").write_bytes(b"L")
        rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0)
        rig.feed("recent", rig.clock.t + 1, dt=1)
        rig.settle()
    finally:
        temp_file.unlink()
    assert TraceRules.from_dict({}).folder_blocked(os.path.join(os.environ.get("TEMP", "C:\\"), "x.txt"))
    rows = [r for r in rig.rows() if r["kind"].startswith("file_")]
    assert len(rows) == 1 and rows[0]["kind"] == "file_open" and rows[0]["file_ext"] == ".flp"
    assert rig.store._dec(rows[0]["file_ciphertext"]) == str(project)
    assert rig.w.counters["dropped_blocked_folder"] == 1                # a blocked folder: never noted
    f = rig.store._query("SELECT * FROM ui_files")[0]
    assert f["action_id"] == rows[0]["id"] and f["outcome"] == "ok"
    data = json.loads(rig.store._dec(f["data_ciphertext"]))
    assert data["project"] == "Invented Beat" and data["tempo"] == 128.0 and data["generators"] == ["Synth One"]


def test_errors_are_logged_content_free(rig):
    rig.w._on_key = lambda *a: (_ for _ in ()).throw(ValueError("secret text that must not be logged"))
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0)
    (kind, fields), = [e for e in rig.log if e[0] == "error"]
    assert fields["error"] == "ValueError" and "secret" not in json.dumps(fields)
    assert set(fields) == {"where", "error", "line"}


def test_focus_events_invalidate_the_cached_field(rig):
    rig.feed("char", rig.clock.t, 1, "user")
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_FOCUS, 1, -4, 0, dt=3)
    rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.settle()
    assert [c for c in rig.uia.calls if c[0] == "focus"] == [("focus",), ("focus",)]


def test_each_row_keeps_the_time_since_the_previous_action_and_the_travel(rig):
    rig.uia.points[(150, 120)] = button()
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("up", t + 0.05, "left", 150, 120, "user", False, dt=0.05)
    rig.feed("down", t + 1.25, "left", 150, 120, "user", 310, False, dt=1.2)
    rig.feed("up", t + 1.3, "left", 150, 120, "user", False, dt=0.05)
    rig.settle()
    first, second = rig.rows()
    assert first["gap_ms"] is None and second["gap_ms"] == 1250 and second["travel_px"] == 310
    assert second["at"] - first["at"] == pytest.approx(1.25, abs=0.01)
