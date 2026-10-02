"""Phase 2 "eyes" (2026-09-29): everything visible, Now, opaque windows, "this", raw-click staleness.

Offline only: synthetic rectangles, trees, boards, a local pipe and a temp SQLite file stand in for the
desktop; nothing reads or touches a real window (ARCHITECTURE.md rule 7). Only the tricky logic is here.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.extract.model import Extraction, Message, Node, Snapshot, number
from yuki.perception.tree import FocusSnapshot, UIElement, WindowTree, content_coverage, format_window_tree
from yuki.perception.visibility import visible_shares


def _el(i: int, role: str, name: str, bounds: tuple[int, int, int, int], depth: int, parent: int = -1,
        **kw: Any) -> UIElement:
    left, top, right, bottom = bounds
    return UIElement(id=i, role=role, name=name, value=kw.pop("value", None),
                     center=((left + right) // 2, (top + bottom) // 2), bounds=bounds,
                     is_interactive=kw.pop("interactive", False), is_scrollable=False, is_focused=False,
                     shortcut=None, depth=depth, patterns=kw.pop("patterns", ()), parent=parent, **kw)


# -- what is on screen -------------------------------------------------------------------------------


def test_visible_shares_measure_occlusion_by_windows_above_and_monitor_gaps() -> None:
    monitors = [(0, 0, 1920, 1080), (1920, 0, 4480, 1440)]
    top = (0, 0, 1920, 1080)            # a maximised window on the left monitor
    under = (100, 100, 900, 900)        # wholly under it
    right = (1920, 0, 3200, 1440)       # the right monitor's left half
    half_covered = (2560, 0, 3840, 720)  # half under `right`
    gap = (0, 1080, 1920, 1440)         # below the left monitor: on no monitor at all
    shares = visible_shares([top, under, right, half_covered, gap], monitors)
    assert shares[0] == 1.0
    assert shares[1] == 0.0
    assert shares[2] == 1.0
    assert 0.45 <= shares[3] <= 0.55
    assert shares[4] == 0.0


# -- the tree: opaque windows, unnamed controls, sets, near-duplicates ---------------------------------


def test_content_coverage_does_not_count_the_window_itself() -> None:
    viewport = (0, 0, 1280, 800)
    only_frame = [_el(0, "Window", "Warp", viewport, 0)]
    assert content_coverage(only_frame, viewport) == 0.0
    with_content = only_frame + [_el(1, "Text", "hello", (0, 0, 640, 800), 1, 0)]
    assert content_coverage(with_content, viewport) == pytest.approx(0.5, abs=0.05)


def _tree(elements: list[UIElement]) -> WindowTree:
    return WindowTree(hwnd=1, title="t", process_name="app.exe", elements=elements)


def test_unnamed_controls_are_labelled_from_structure_and_geometry() -> None:
    elements = [
        _el(0, "Window", "App", (0, 0, 1000, 800), 0),
        _el(1, "ToolBar", "", (0, 0, 1000, 40), 1, 0),
        _el(2, "Text", "Title", (10, 5, 200, 35), 2, 0),
        _el(3, "Button", "", (220, 5, 250, 35), 2, 0, interactive=True, patterns=("invoke",)),
        _el(4, "Button", "", (260, 5, 290, 35), 2, 0, interactive=True, patterns=("invoke",),
            description="Like"),
    ]
    text = format_window_tree(_tree(elements))
    assert '[3] Button (no name, right of "Title")' in text
    assert '[4] Button (no name, described "Like")' in text


def test_set_items_carry_their_place_and_colliding_names_are_written_in_full() -> None:
    long_a = "Make Up My Mind - Stellar Blade: BLOOD RAIN Official Trailer Mix and a long tail of words " + "A" * 20
    long_b = "Make Up My Mind - Stellar Blade: BLOOD RAIN Official Trailer Mix and a long tail of words " + "B" * 20
    elements = [
        _el(0, "Window", "YouTube", (0, 0, 1000, 800), 0),
        _el(1, "List", "Mixes", (0, 0, 500, 800), 1, 0),
        _el(2, "ListItem", long_a, (0, 0, 500, 100), 2, 1, interactive=True, patterns=("select",)),
        _el(3, "ListItem", long_b, (0, 100, 500, 200), 2, 1, interactive=True, patterns=("select",)),
        _el(4, "ListItem", "Other", (0, 200, 500, 300), 2, 1, interactive=True, patterns=("select",),
            position=(7, 40)),
    ]
    text = format_window_tree(_tree(elements))
    assert "#1/3" in text and "#2/3" in text
    assert "#7/40" in text  # the app's own numbering wins
    assert long_a in text and long_b in text  # printed alike when clipped: written out
    assert "#k/n" in text.splitlines()[0]  # explained once in the header


def test_focus_line_never_shows_a_password_and_says_why_it_is_unknown() -> None:
    assert "password" in FocusSnapshot(ok=True, role="Edit", name="Password", is_password=True).line()
    assert FocusSnapshot(ok=False, error="no answer within 0.05 s").line() == \
        "focus: unknown (no answer within 0.05 s)"
    assert FocusSnapshot(ok=True).line() == "focus: nothing has keyboard focus"
    line = FocusSnapshot(ok=True, role="Document", name="Doc", selection="the chosen words", window=5,
                         window_title="Notes").line()
    assert 'selected: "the chosen words"' in line and "[5]" in line


# -- raw x,y clicks: the staleness rule ---------------------------------------------------------------


class _ClickBackend:
    """Stands in for the desktop: a window at every point, clicks recorded, never sent anywhere."""

    def __init__(self, window: int = 0) -> None:
        self.window = window
        self.clicks: list[tuple[int, int]] = []

    def window_at(self, x: int, y: int) -> int:
        return self.window

    def click(self, x: int, y: int, **kw: Any) -> Any:
        self.clicks.append((x, y))
        return SimpleNamespace(ok=True, summary="clicked", details={}, elapsed_ms=1.0)

    def get_window_tree(self, hwnd: int, **kw: Any) -> Any:
        return _tree([_el(0, "Window", "App", (0, 0, 100, 100), 0),
                      _el(1, "Button", "OK", (10, 10, 50, 30), 1, 0, interactive=True, patterns=("invoke",))])

    def format_window_tree(self, tree: Any) -> str:
        return "tree"


def test_raw_click_on_a_never_read_window_is_refused_and_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent import tools
    from yuki.perception import windows

    monkeypatch.setattr(windows, "is_user_window", lambda hwnd: True)
    backend = _ClickBackend(window=4242)
    dispatcher = tools.Dispatcher(backend)
    out = dispatcher.dispatch("click", {"x": 20, "y": 20})
    assert not out.ok and "has not been read" in out.summary
    assert backend.clicks == []


def test_raw_click_after_a_read_goes_and_after_acting_on_the_window_is_refused(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent import tools

    backend = _ClickBackend(window=4242)
    dispatcher = tools.Dispatcher(backend)
    monkeypatch.setattr(tools, "_window_rect_now", lambda hwnd: [0, 0, 100, 100])
    monkeypatch.setattr(tools, "_window_title_now", lambda hwnd: "App")
    dispatcher._tree_outcome(4242, backend.get_window_tree(4242), time.perf_counter())
    dispatcher._read_facts[4242]["title"] = "App"
    assert dispatcher.dispatch("click", {"x": 20, "y": 20}).ok
    assert backend.clicks == [(20, 20)]
    dispatcher._mark_acted(4242, "scroll")
    out = dispatcher.dispatch("click", {"x": 20, "y": 20})
    assert not out.ok and "changed since read" in out.summary
    assert backend.clicks == [(20, 20)]
    # an element id from the same read is not a raw coordinate: its own rule applies (latest read)
    assert dispatcher.dispatch("click", {"element": 1}).ok


def test_raw_click_on_an_opaque_window_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent import tools
    from yuki.perception import tree

    backend = _ClickBackend(window=777)
    dispatcher = tools.Dispatcher(backend)
    monkeypatch.setattr(tree, "measured_opaque", lambda hwnd: hwnd == 777)
    out = dispatcher.dispatch("click", {"x": 5, "y": 5})
    assert not out.ok and "draws its own interface" in out.summary
    assert backend.clicks == []


# -- Now: the board, its text, the pipe -----------------------------------------------------------------


def _extraction(**kw: Any) -> Extraction:
    base = dict(kind="page", app="Chrome", title="Page", url="https://example.com/a", thread_scope="s")
    base.update(kw)
    return Extraction(**base)


def test_board_marks_a_read_stale_after_a_title_change_and_keeps_no_content_when_blocked() -> None:
    from yuki.memory.now import NowBoard, gist_of

    board = NowBoard()
    board.set_layout([{"hwnd": 1, "kind": "window", "title": "Page", "rect": [0, 0, 10, 10], "share": 1.0, "z": 0},
                      {"hwnd": 2, "kind": "window", "title": "Bank", "rect": [0, 0, 10, 10], "share": 0.5, "z": 1}],
                     foreground=1, at=time.time(), ms=1.0)
    gist = gist_of(_extraction(body="line one\n•••••\nline two"), front=True, is_masked=lambda s: set(s) <= {"•"})
    assert gist["text"] == "line one\nline two"
    board.put(1, {**gist, "app": "Chrome", "read_at": time.time() - 5})
    board.block(2, app="Bank", process="bank.exe", reason="blocked_host", at=time.time(), foreground=False)
    snap = board.snapshot()
    front = next(w for w in snap["windows"] if w["hwnd"] == 1)
    assert not front["stale"]
    board.note_title(1, "Another page", time.time())
    front = next(w for w in board.snapshot()["windows"] if w["hwnd"] == 1)
    assert front["stale"] and front["live_title"] == "Another page"
    blocked = next(w for w in board.snapshot()["windows"] if w["hwnd"] == 2)
    assert blocked["text"] == "" and blocked["blocked"] == "blocked_host"


def test_messages_become_the_gist_newest_last() -> None:
    from yuki.memory.now import gist_of

    messages = [Message(fingerprint=str(i), sender="Varun", is_me=i % 2 == 0, time_label="18:0" + str(i), at=None,
                        text=f"message {i}") for i in range(20)]
    gist = gist_of(_extraction(kind="conversation", messages=messages), front=False)
    assert len(gist["messages"]) == 12 and gist["messages"][-1]["text"] == "message 19"
    assert gist["messages"][-2]["sender"] == "the user"


def test_format_now_is_capped_and_says_opaque_blocked_and_age() -> None:
    from yuki.memory.now import format_now

    board = {
        "foreground": 1,
        "windows": [
            {"hwnd": 1, "app": "Chrome", "live_title": "Docs", "kind": "page", "read_at": 1.0, "age_s": 3.0,
             "text": "x" * 5000, "z": 0, "share": 1.0},
            {"hwnd": 2, "app": "Warp", "live_title": "tab", "read_at": 1.0, "age_s": 30.0, "opaque": True, "z": 1,
             "share": 0.9, "app_data": {"source": "Warp's own saved window state (read-only)", "saved_at": 0,
                                        "tabs": [{"n": 1, "title": "LLM OS agent harness revamp",
                                                  "folder": "C:\\x", "showing": False},
                                                 {"n": 2, "title": "tab", "folder": "", "showing": True}]}},
            {"hwnd": 3, "app": "Bank", "live_title": "", "blocked": "blocked_host", "z": 2, "share": 0.5},
        ],
        "popups": [],
    }
    text = format_now(board, foreground=1, front_chars=1500)
    assert len(text) < 2600
    # revamp A8: opaque is a measured fact (coverage, characters read), never "nothing can be read"
    assert "opaque: its UI Automation elements cover" in text and "no text of it was read" in text
    assert "nothing inside it can be read" not in text
    assert 'tab 1 "LLM OS agent harness revamp"' in text and "tab 2 (showing)" in text
    assert "not read (the user's privacy rules)" in text
    assert "read 3 s ago" in text


def test_now_pipe_round_trip_with_the_shared_key(tmp_path: Path) -> None:
    from yuki.memory.now import NowBoard, NowClient, NowServer

    class Cipher:
        def digest(self, *parts: str) -> str:
            return "ab" * 32

    board = NowBoard()
    board.set_layout([{"hwnd": 9, "kind": "window", "title": "W", "rect": [0, 0, 1, 1], "share": 1.0, "z": 0}],
                     foreground=9, at=time.time(), ms=0.5)
    asked: list[Any] = []
    server = NowServer(board, Cipher(), tmp_path / "memory.db", on_refresh=lambda h: asked.append(h) or True).start()
    try:
        client = NowClient(Cipher(), tmp_path / "memory.db")
        got = None
        for _ in range(20):
            got = client.now(wait_s=0.5)
            if got is not None:
                break
        assert got is not None and got["windows"][0]["hwnd"] == 9
        assert client.refresh(9, wait_s=0.5) and asked == [9]

        class Wrong:
            def digest(self, *parts: str) -> str:
                return "cd" * 32

        assert NowClient(Wrong(), tmp_path / "memory.db").now(wait_s=0.3) is None
        client.close()
    finally:
        server.stop()


# -- Warp's own saved state --------------------------------------------------------------------------------


def _warp_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE windows (id INTEGER, active_tab_index INTEGER, window_width REAL, window_height REAL,
            origin_x REAL, origin_y REAL, fullscreen_state INTEGER, vertical_tabs_panel_open INTEGER);
        CREATE TABLE tabs (id INTEGER, window_id INTEGER, custom_title TEXT);
        CREATE TABLE pane_nodes (id INTEGER, tab_id INTEGER, is_leaf INTEGER);
        CREATE TABLE pane_leaves (pane_node_id INTEGER, is_focused INTEGER, custom_vertical_tabs_title TEXT);
        CREATE TABLE terminal_panes (id INTEGER, cwd TEXT);
        CREATE TABLE commands (id INTEGER, command TEXT);
        INSERT INTO windows VALUES (2, 1, 1280, 1179, 1920, 261, 0, 1);
        INSERT INTO tabs VALUES (2, 2, NULL), (3, 2, NULL);
        INSERT INTO pane_nodes VALUES (2, 2, 1), (3, 3, 1);
        INSERT INTO pane_leaves VALUES (2, 1, '◐ LLM OS agent harness revamp'), (3, 1, NULL);
        INSERT INTO terminal_panes VALUES (2, 'C:\\Users\\me'), (3, 'C:\\Users\\me\\work');
        INSERT INTO commands VALUES (1, 'echo secret-never-read');
    """)
    conn.commit()
    conn.close()


def test_warp_saved_tabs_match_the_live_window_by_its_rectangle(tmp_path: Path) -> None:
    from yuki.memory.warp import read_session, window_data, window_for

    db = tmp_path / "warp.sqlite"
    _warp_db(db)
    windows = read_session(db)
    assert len(windows) == 1 and [t.title for t in windows[0].tabs] == ["LLM OS agent harness revamp", ""]
    assert window_for((1920, 261, 3200, 1440), windows) is windows[0]
    assert window_for((0, 0, 1280, 1179), windows) is None
    data = window_data((1921, 262, 3200, 1440), "◑ Yuki voice pipeline", db_path=db)
    assert data is not None
    tabs = data["tabs"]
    assert tabs[0]["title"] == "LLM OS agent harness revamp" and not tabs[0]["showing"]
    assert tabs[1]["showing"] and tabs[1]["title"] == "Yuki voice pipeline"  # the live title names the tab showing


# -- extraction: covered parts left out, coverage measured -------------------------------------------------


def test_prune_leaves_out_what_other_windows_cover_and_coverage_ignores_the_root() -> None:
    from yuki.memory.extract import prune_snapshot, snapshot_coverage

    root = Node(role="Window", name="W", bounds=(0, 0, 1000, 1000), children=[
        Node(role="Text", name="left", bounds=(0, 0, 500, 1000)),
        Node(role="Text", name="right", bounds=(500, 0, 1000, 1000)),
    ])
    number(root)
    snap = Snapshot(hwnd=1, title="W", process_name="w.exe", root=root, viewport=(0, 0, 1000, 1000))
    assert snapshot_coverage(snap) == pytest.approx(1.0)
    removed = prune_snapshot(snap, lambda bounds: bounds[0] >= 500)
    assert removed == 1 and [c.name for c in snap.root.children] == ["left"]
    frame_only = Snapshot(hwnd=1, title="W", process_name="w.exe", root=Node(role="Window", bounds=(0, 0, 10, 10)),
                          viewport=(0, 0, 10, 10))
    assert snapshot_coverage(frame_only) == 0.0


# -- the watcher's background schedule ----------------------------------------------------------------------


def _watcher() -> Any:
    from yuki.memory.watcher import Watcher, WatcherSettings

    return Watcher(lambda: None, settings=WatcherSettings(bg_reads_per_min=2), log=None)


def test_background_reads_keep_to_their_budget_and_skip_a_window_just_read_in_front(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.memory import watcher as w

    monkeypatch.setattr(w, "user_idle_s", lambda: 0.0)
    watcher = _watcher()
    now = time.monotonic()
    watcher._last_capture[10] = now - 5.0          # read while it was in front 5 s ago
    for hwnd in (10, 11, 12, 13):
        watcher._bg_mark(hwnd, "shown", now - 10.0)
    with watcher._bg_lock:
        watcher._bg_spent.append((now - 1.0, 100.0, False))  # one read already this minute
    watcher._bg_schedule(now)
    queued = [job.hwnd for job in watcher._bg_queue]
    assert 10 not in queued and 10 not in watcher._bg_pending  # its fresh read is on the board
    assert len(queued) == 1                                     # budget of 2 a minute, one already spent
    assert len(watcher._bg_pending) == 2                        # the rest wait, not dropped


def test_an_opaque_window_is_followed_by_its_app_data_not_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.memory import watcher as w

    monkeypatch.setattr(w, "user_idle_s", lambda: 0.0)
    watcher = _watcher()
    now = time.monotonic()
    watcher._opaque[20] = now
    watcher._bg_mark(20, "title", now - 10.0)
    watcher._bg_schedule(now)
    assert [(j.hwnd, j.app_data_only) for j in watcher._bg_queue] == [(20, True)]
