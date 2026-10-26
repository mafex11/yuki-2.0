"""percFix (report 73, release 2 live check): the perception failures, on fakes shaped like the 5 Oct reads.

No desktop, no COM calls to other processes: the reader runs over the fake UI Automation tree (``tests/_fake_uia.py``),
the dispatcher over stub desks, the slider over fake sliders, the window close over stubbed window calls. Names are
invented.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from _fake_uia import Node, feed_window, player_window, read
from yuki.actions import ActionResult
from yuki.perception import tree as T
from yuki.perception.outcome import describe, diff, verdict


def _by_name(tree: T.WindowTree, name: str, role: str | None = None) -> T.UIElement:
    return next(e for e in tree.elements if e.name == name and (role is None or e.role == role))


def _node(window: Node, name: str, role: str) -> Node:
    return next(n for n in window.walk() if n.name == name and n.role == role)


# ---------------------------------------------------------------------------
# (1) Spotify's hidden volume slider is listed, hidden, with its numbers
# ---------------------------------------------------------------------------


def test_a_hidden_input_reported_with_its_unclipped_full_box_is_listed_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chromium reports an input clipped inside its label with its own full box: off-screen, overlapping the view
    window = player_window(volume=0.4)
    slider = _node(window, "Change volume", "Slider")
    slider.bounds, slider.offscreen = (1376, 918, 1416, 942), True
    tree = read(window, monkeypatch=monkeypatch)
    found = _by_name(tree, "Change volume", "Slider")
    assert found.hidden and found.range == (0.4, 0.0, 1.0)


def test_a_slider_whose_label_is_kept_off_screen_is_found_by_the_sweep_and_placed_under_the_bar(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # live 5 Oct: the label holding the volume input reported itself off-screen, so the walk left it out as "kept out
    # of sight" and never met the slider inside (0 of 8 reads listed it); the slider sweep finds it
    window = player_window(volume=0.4)
    _node(window, "Change volume", "Text").offscreen = True
    tree = read(window, monkeypatch=monkeypatch)
    slider = _by_name(tree, "Change volume", "Slider")
    bar = _by_name(tree, "Now playing bar")
    assert slider.hidden and slider.range == (0.4, 0.0, 1.0) and slider.runtime_id is not None
    assert slider.parent == bar.id
    # the list stays in tree order with ids matching positions and parents before children
    assert [e.id for e in tree.elements] == list(range(len(tree.elements)))
    assert all(e.parent < e.id for e in tree.elements if e.parent >= 0)
    assert slider.id > bar.id and all(tree.elements[i].depth > bar.depth for i in range(bar.id + 1, slider.id))
    line = next(ln for ln in T.format_window_tree(tree).splitlines() if '"Change volume"' in ln and "Slider" in ln)
    assert "[hidden]" in line and "0.4 (40%, range 0-1)" in line


def test_the_sweep_leaves_out_a_slider_of_a_page_the_read_does_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    window = player_window()
    pane = next(n for n in window.walk() if n.role == "Pane")
    covered = Node("Document", "Covered page", (105, 121, 1507, 951), offscreen=True)
    covered.add(Node("Group", "Covered player", (111, 890, 1501, 945), offscreen=True).add(
        Node("Slider", "Covered volume", (1374, 925, 1418, 935), range=(0.9, 0.0, 1.0), offscreen=True)))
    pane.add(covered)
    tree = read(window, monkeypatch=monkeypatch)
    assert not any(e.name == "Covered volume" for e in tree.elements)
    assert any(e.name == "Change volume" and e.role == "Slider" for e in tree.elements)


def test_the_sweep_does_not_search_again_a_window_with_no_slider(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Node("Window", "Notes", (0, 0, 800, 600))
    root.add(Node("Button", "Save", (10, 10, 80, 40), invoke=True))
    first = read(root, monkeypatch=monkeypatch)
    assert 7001 in T._no_sliders
    calls: list[int] = []
    from _fake_uia import Cached

    original = Cached.FindAllBuildCache

    def counted(self: Any, scope: int, condition: Any, request: Any) -> Any:
        calls.append(1)
        return original(self, scope, condition, request)

    monkeypatch.setattr(Cached, "FindAllBuildCache", counted)
    # a second read of the same window within the memo's time: no search (read() clears the memo, so walk directly)
    import time

    from _fake_uia import Automation

    automation = Automation({7001: root})
    T._collect_pass(automation, 7001, [], 400, time.monotonic() + 5.0)
    assert calls == [] and len(first.elements) == 2


# ---------------------------------------------------------------------------
# (1b) set_slider: read back on its change event, never "reached" without moving
# ---------------------------------------------------------------------------


class _LateSlider:
    """A slider whose new value arrives after a while (Chromium/CEF), with or without a change event."""

    def __init__(self, value: float = 0.4, step: float = 0.1, *, arrives_after: int = 3, takes: bool = True,
                 events: bool = True) -> None:
        self.value, self.step, self.arrives_after, self.takes, self.events = value, step, arrives_after, takes, events
        self.pending: float | None = None
        self.reads_since_set = 0
        self.watches: list[Any] = []

    def read(self) -> tuple[float, float, float, bool, float]:
        if self.pending is not None:
            self.reads_since_set += 1
            if self.reads_since_set > self.arrives_after:
                self.value, self.pending = self.pending, None
        return self.value, 0.0, 1.0, False, self.step

    def set(self, value: float) -> None:
        if self.takes:
            self.pending = value

    def focus(self) -> None:
        pass

    def watch(self) -> Any:
        slider = self

        class Watch:
            live = slider.events
            closed = False

            def wait(self, timeout_s: float) -> bool:
                if slider.pending is None:
                    return False
                slider.value, slider.pending = slider.pending, None   # the event comes with the new value
                return True

            def close(self) -> None:
                self.closed = True

        w = Watch()
        self.watches.append(w)
        return w


def test_set_slider_reads_back_on_its_change_event_and_says_the_real_new_level() -> None:
    from yuki.actions.slider import drive

    s = _LateSlider(arrives_after=50)
    ok, summary, details = drive(s, by=-10, sleep=lambda _s: None, hold_s=0.0)
    assert ok and details["verified"] is True and details["after"] == pytest.approx(0.3)
    assert details["read_back"] == "on its change event" and "40% -> 30%" in summary
    assert s.watches and s.watches[0].closed


def test_set_slider_without_events_waits_a_bounded_moment_for_the_value() -> None:
    from yuki.actions.slider import drive

    s = _LateSlider(arrives_after=3, events=False)
    ok, _summary, details = drive(s, by=-10, sleep=lambda _s: None, hold_s=0.0)
    assert ok and details["verified"] is True and details["after"] == pytest.approx(0.3)


def test_a_value_that_never_moved_is_never_reached_even_within_one_step_of_the_target() -> None:
    # live 5 Oct: 0.40 against a target of 0.30 with a step of 0.10 counted as reached: "40% -> 40%", verified
    from yuki.actions.slider import drive

    s = _LateSlider(takes=False, events=False)
    ok, summary, details = drive(s, by=-10, sleep=lambda _s: None, settle_s=0.0, hold_s=0.0)
    assert details["verified"] is False and not ok
    assert details["after"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# (2)/(8) toggles: the control's own state is the verdict
# ---------------------------------------------------------------------------


class _Desk:
    """A stub desk over a fake window: reads go through the real reader."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, window: Node) -> None:
        self.mp = monkeypatch
        self.window = window
        self.reads = 0
        self.clicks: list[tuple[int, int]] = []
        self.pointed: list[tuple[int, int]] = []
        self.on_click: Any = None
        self.on_point: Any = None

    def get_window_tree(self, hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0,
                        part: tuple | None = None) -> T.WindowTree:
        self.reads += 1
        return read(self.window, hwnd=hwnd, monkeypatch=self.mp, max_elements=max_elements, part=part)

    def format_window_tree(self, tree: T.WindowTree, **kw: Any) -> str:
        return T.format_window_tree(tree, **kw)

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, expect_hwnd: int | None = None) -> Any:
        self.clicks.append((x, y))
        if self.on_click is not None:
            self.on_click(x, y)
        return ActionResult(ok=True, summary=f"clicked at ({x},{y}); same page", details={
            "focus_changed": False, "foreground_changed": False, "page_changed": False,
            "foreground_title_changed": False})

    def get_desktop_overview(self) -> dict[str, Any]:
        return {"foreground_hwnd": 7001, "windows": []}


class _PatternDesk(_Desk):
    """The Toggle pattern says the state changed at once; the window's tree shows it only later (or never)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, window: Node) -> None:
        super().__init__(monkeypatch, window)
        self.acted: list[str] = []

    def element_action(self, hwnd: int, element: dict[str, Any], verb: str, **kw: Any) -> Any:
        self.acted.append(verb)
        return ActionResult(ok=True, summary=f'Toggle on {element.get("name")} through UI Automation; its state changed',
                            details={"verified": True, "toggle_state": {"before": 0, "after": 1}})


def _dispatcher(desk: Any) -> Any:
    from yuki.agent.tools import Dispatcher

    d = Dispatcher(backend=desk)
    d.verify_outcomes = True
    d.guard_raw_clicks = False
    return d


def _id_of(text: str, name: str) -> int:
    line = next(ln for ln in text.splitlines() if f'"{name}"' in ln and ln.strip().startswith("["))
    return int(line.strip()[1:line.strip().index("]")])


def test_a_toggle_whose_tree_lags_is_verified_by_the_controls_own_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # live 5 Oct: the Lyrics toggle worked both ways, but the read after it still showed the old state: "contradicted"
    from yuki.agent.loop import action_verdict

    desk = _PatternDesk(monkeypatch, player_window(lyrics_on=False))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert desk.acted == ["click"] and desk.clicks == []
    details = out.payload["details"]
    assert details["verified"] is True and "read back from the control itself: now on" in out.content[0]["text"]
    assert action_verdict("click", out)[0] is True


def test_a_toggle_the_app_drew_again_under_another_name_is_found_by_its_place(monkeypatch: pytest.MonkeyPatch) -> None:
    # live 5 Oct (T3 Code): the panel toggle was drawn anew under a longer name; "not in the window now" overrode a
    # panel that had closed
    from yuki.agent.loop import action_verdict

    window = Node("Window", "Editor", (0, 0, 1600, 900))
    bar = Node("Group", "Top bar", (0, 0, 1600, 40))
    toggle = Node("Button", "Toggle panel", (1500, 5, 1530, 35), toggle=1)
    bar.add(toggle)
    panel = Node("Group", "Side panel", (1200, 40, 1600, 900))
    panel.add(*[Node("Button", f"File {i}", (1210, 60 + 40 * i, 1590, 90 + 40 * i), invoke=True) for i in range(6)])
    window.add(bar, panel)
    desk = _Desk(monkeypatch, window)

    def redraw(x: int, y: int) -> None:
        bar.children = [Node("Button", "Toggle panel (Ctrl+B)", (1500, 5, 1530, 35), toggle=0)]
        window.children = [bar]

    desk.on_click = redraw
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Toggle panel"), "expect_hwnd": 7001, "toggle_to": "off"})
    details = out.payload["details"]
    assert details["state_after"] == "off" and details["verified"] is True
    assert action_verdict("click", out)[0] is True


def test_a_toggle_still_reading_its_old_state_while_the_window_changed_is_unverified_not_contradicted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    window = player_window(lyrics_on=False)
    desk = _Desk(monkeypatch, window)

    def opens_view_only(x: int, y: int) -> None:
        shell = next(n for n in window.walk() if n.role == "Group" and n.invoke)
        shell.add(Node("Group", "Lyrics view", (472, 227, 1212, 880)).add(
            Node("Text", "invented line", (480, 240, 1200, 270))))

    desk.on_click = opens_view_only
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    details = out.payload["details"]
    assert "verified" not in details and details["toggle_unconfirmed"] is True
    assert "though the window did change" in out.content[0]["text"]
    assert action_verdict("click", out)[0] is None


def test_a_toggle_that_did_nothing_is_still_contradicted(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _Desk(monkeypatch, player_window(lyrics_on=False))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert out.payload["details"]["verified"] is False and action_verdict("click", out)[0] is False


def test_a_toggle_hidden_with_its_whole_bar_is_never_clicked_by_the_mouse_at_the_containers_place(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # a full-size view hides the player bar: its toggles are listed hidden (with their states), standing at the box
    # of the window-sized group that holds them; the mouse would land on whatever the view draws there
    desk = _Desk(monkeypatch, player_window(bar_offscreen=True))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert not out.ok and "kept out of sight" in out.summary and desk.clicks == []
    # through its Toggle pattern it is fine: no pointer needed
    desk = _PatternDesk(monkeypatch, player_window(bar_offscreen=True))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert out.ok and desk.acted == ["click"] and desk.clicks == []


def test_the_toggle_found_again_by_place_ignores_a_different_control_or_a_small_overlap() -> None:
    from yuki.agent.tools import _toggle_in

    was = {"id": 4, "role": "Button", "bounds": (100, 10, 130, 40), "patterns": ["toggle"], "runtime_id": [1, 2]}
    other_role = {"id": 9, "role": "CheckBox", "bounds": (100, 10, 130, 40), "patterns": ["toggle"]}
    beside = {"id": 10, "role": "Button", "bounds": (125, 10, 155, 40), "patterns": ["toggle"]}
    same = {"id": 11, "role": "Button", "bounds": (101, 10, 131, 40), "patterns": ["toggle"], "states": ["off"]}
    assert _toggle_in([other_role, beside], 4, was) is None
    assert _toggle_in([other_role, beside, same], 4, was) is same
    assert _toggle_in([{**same, "runtime_id": [1, 2], "id": 12}], 4, was)["id"] == 12


# ---------------------------------------------------------------------------
# (6) scroll: judged by the content (position, items in view replaced), not one point
# ---------------------------------------------------------------------------


def _item(i: int, name: str, parent: int, y: int, **kw: Any) -> dict[str, Any]:
    return {"id": i, "role": kw.pop("role", "ListItem"), "name": name, "parent": parent, "depth": kw.pop("depth", 2),
            "bounds": (0, y, 800, y + 40), "center": (400, y + 20), "states": (), "value": None,
            "is_interactive": True, "is_scrollable": False, **kw}


def _feed(first: int, *, position: float | None) -> list[dict[str, Any]]:
    root = {"id": 0, "role": "Window", "name": "Feed", "parent": -1, "depth": 0, "bounds": (0, 0, 800, 600),
            "center": (400, 300), "states": (), "value": None, "is_interactive": True, "is_scrollable": False}
    pane = {"id": 1, "role": "Document", "name": "Feed page", "parent": 0, "depth": 1, "bounds": (0, 0, 800, 600),
            "center": (400, 300), "states": (), "value": None, "is_interactive": True, "is_scrollable": True,
            "scroll": (-1.0, position) if position is not None else None}
    return [root, pane, *[_item(100 + n, f"Video {n}", 1, 40 * (n - first)) for n in range(first, first + 10)]]


def test_a_scroll_that_replaced_every_item_in_view_is_the_content_moving() -> None:
    # live 5 Oct on YouTube: "82 appeared, 31 went away", nothing left in both reads to measure a shift by
    change = diff(_feed(0, position=None), _feed(20, position=None), scroll=True)
    assert change.turnover and change.content_moved and not change.appeared and not change.gone
    assert verdict(change)[0] is True and verdict(change)[1].startswith("the window changed: its content moved")
    assert any("scrolled on: 10 element(s) came into view, 10 left it" in ln for ln in describe(change))
    # the same reads judged as a click are what appeared and went, not a scroll
    assert not diff(_feed(0, position=None), _feed(20, position=None)).turnover


def test_a_scroll_position_that_moved_is_said_in_percent() -> None:
    change = diff(_feed(0, position=10.0), _feed(20, position=35.0), scroll=True)
    assert change.scrolled and change.scrolled[0][1:] == ("v", 10.0, 35.0)
    line = next(ln for ln in describe(change) if "scrolled down" in ln)
    assert "from 10% to 35% of the way" in line and "came into view" in line


def test_a_scroll_the_point_called_failed_is_ok_when_the_items_in_view_were_replaced(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    class FeedDesk(_Desk):
        offset = 0

        def scroll(self, x: int, y: int, *, dy: int = 0, dx: int = 0, expect_hwnd: int | None = None) -> Any:
            self.offset += 3000   # more than a screenful: no item stays in view
            self.window = feed_window(offset=self.offset)
            from yuki.actions.scrolling import POINT_STILL

            return ActionResult(ok=False, summary=f"sent 10 wheel notches down at ({x},{y}){POINT_STILL}",
                                details={"verified": False, "scrolls": [{"moved": False}]})

    desk = FeedDesk(monkeypatch, feed_window(offset=0))
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("scroll", {"x": 960, "y": 600, "dy": -10, "expect_hwnd": 7001})
    assert out.ok and "did not scroll" not in out.summary and "the window's content did move" in out.summary
    assert action_verdict("scroll", out)[0] is True


# ---------------------------------------------------------------------------
# (7) a raw click on nothing is never verified
# ---------------------------------------------------------------------------


def test_a_raw_click_on_an_empty_margin_is_never_verified_by_churn(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    window = player_window()
    desk = _Desk(monkeypatch, window)
    library = next(n for n in window.walk() if n.name == "Your Library")

    def churn(x: int, y: int) -> None:
        library.children.pop()   # something elsewhere goes away on its own as the click lands

    desk.on_click = churn
    monkeypatch.setattr("yuki.perception.windows.cursor_position", lambda: (0, 0))
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 300, "y": 870, "expect_hwnd": 7001})   # in the library, below the playlists
    details = out.payload["details"]
    assert out.ok and "verified" not in details and details["point_on_nothing"] is True
    assert details["screen_change"]["undecided"] is True
    assert "Not taken as the click's effect" in out.content[0]["text"]
    assert action_verdict("click", out) == (None, details["screen_change"]["why"])


def test_a_raw_click_on_a_control_keeps_its_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    window = player_window()
    desk = _Desk(monkeypatch, window)
    mute = _node(window, "Mute", "Button")

    def rename(x: int, y: int) -> None:
        mute.name = "Unmute"

    desk.on_click = rename
    monkeypatch.setattr("yuki.perception.windows.cursor_position", lambda: (0, 0))
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 1360, "y": 915, "expect_hwnd": 7001})
    assert out.payload["details"]["verified"] is True and "point_on_nothing" not in out.payload["details"]
    assert action_verdict("click", out)[0] is True


def test_what_the_pointer_alone_does_is_in_the_read_before_the_click(monkeypatch: pytest.MonkeyPatch) -> None:
    # live 5 Oct: a video preview that ended when the pointer left it "verified" a click on an empty margin
    from yuki.agent.loop import action_verdict

    window = player_window()
    library = next(n for n in window.walk() if n.name == "Your Library")
    preview = Node("Group", "Preview playing", (153, 341, 461, 391))
    library.add(preview)

    class PointerDesk(_Desk):
        def point_at(self, x: int, y: int, *, expect_hwnd: int | None = None) -> Any:
            self.pointed.append((x, y))
            if preview in library.children:
                library.children.remove(preview)   # the pointer left the preview: it ends
            return ActionResult(ok=True, summary=f"pointer at ({x},{y})", details={})

    desk = PointerDesk(monkeypatch, window)

    def pointer_moves_with_the_click(x: int, y: int) -> None:
        if preview in library.children:   # a click moves the pointer too: without the move first, this is its "effect"
            library.children.remove(preview)

    desk.on_click = pointer_moves_with_the_click
    monkeypatch.setattr("yuki.perception.windows.cursor_position", lambda: (0, 0))
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 1360, "y": 915, "expect_hwnd": 7001})   # on Mute, which does nothing here
    assert desk.pointed == [(1360, 915)] and desk.clicks == [(1360, 915)]
    assert "nothing visible changed in the window" in out.content[0]["text"]
    assert action_verdict("click", out)[0] is False


# ---------------------------------------------------------------------------
# The tray's `now`: finished runs leave it
# ---------------------------------------------------------------------------


def _ev(type_: str, at: float, **kw: Any) -> dict[str, Any]:
    return {"type": type_, "at": at, **kw}


def _running(model: Any) -> list[Any]:
    return [i for i in model.items if i.kind == "assistant" and i.running]


def test_a_run_paused_by_a_takeover_and_resumed_by_typed_words_leaves_now_when_it_ends() -> None:
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply(_ev("user", 1.0, text="type two lines in the editor", job=31))
    m.apply(_ev("started", 1.1, job=31, lane="worker"))
    m.apply(_ev("tool_called", 1.2, job=31, name="type_text", label="Typing"))
    m.apply(_ev("said", 2.0, job=31, text="You took over, so I paused."))
    m.apply(_ev("answer", 9.0, job=31, text="go on"))
    m.apply(_ev("tool_finished", 9.5, job=31, name="type_text", ok=True, summary="typed"))
    m.apply(_ev("finished", 12.0, job=31, text="Done: both lines are in."))
    assert _running(m) == []


def test_a_question_answered_leaves_no_spinning_ask_step() -> None:
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply(_ev("user", 1.0, text="rename the file", job=35))
    m.apply(_ev("started", 1.1, job=35, lane="worker"))
    m.apply(_ev("tool_called", 1.2, job=35, name="ask_user", label="Asking you"))
    m.apply(_ev("asked", 1.3, job=35, question="Which name?"))
    m.apply(_ev("answer", 5.0, job=35, text="notes"))
    m.apply(_ev("finished", 7.0, job=35, text="Renamed it to notes."))
    assert _running(m) == []


def test_a_request_set_aside_on_its_question_carries_on_and_ends_below_the_answer() -> None:
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply(_ev("user", 1.0, text="book the room", job=40))
    m.apply(_ev("started", 1.1, job=40, lane="worker"))
    m.apply(_ev("asked", 1.3, job=40, question="Which day?"))
    # the answer came after the question was set aside: the runtime resumes it as job 41, then the answer is shown
    m.apply(_ev("resumed", 60.0, from_job=40, to_job=41))
    m.apply(_ev("answer", 60.0, job=40, text="Friday"))
    m.apply(_ev("started", 60.1, job=41, lane="worker"))
    m.apply(_ev("finished", 65.0, job=41, text="Booked for Friday."))
    assert _running(m) == []
    assert m.items[-1].text == "Booked for Friday."


# ---------------------------------------------------------------------------
# The takeover watch: a fast real keystroke counts
# ---------------------------------------------------------------------------


def _watch(state: dict[str, Any]) -> Any:
    from yuki.actions.leases import TakeoverWatch

    return TakeoverWatch(age=lambda: state["t"] - state["last"], keys=lambda: (frozenset(), frozenset()),
                         cursor=lambda: (500, 500), span=lambda: state["span"], ours=lambda kind, where: False,
                         now=lambda: state["t"], pressed=lambda: state["pressed"])


def test_a_50_ms_key_tap_the_polls_never_saw_pauses_the_run() -> None:
    state = {"t": 1000.0, "last": 900.0, "span": (0.0, 0.0), "pressed": 0.0}
    w = _watch(state)
    assert w.poll() is None
    for _ in range(3):
        state["t"] += 0.1
        assert w.poll() is None
    state["pressed"] = state["t"] + 0.03   # down and up between two looks; the polled key states saw nothing
    state["last"] = state["t"] + 0.08
    state["t"] += 0.1
    assert w.poll() == "keys"


def test_a_key_yuki_itself_typed_is_not_a_takeover() -> None:
    state = {"t": 1000.0, "last": 900.0, "span": (0.0, 0.0), "pressed": 0.0}
    w = _watch(state)
    w.poll()
    state["t"] += 0.1
    w.poll()
    state["span"] = (state["t"] + 0.01, state["t"] + 0.05)   # Yuki's own typing
    state["pressed"] = state["t"] + 0.02
    state["last"] = state["t"] + 0.05
    state["t"] += 0.1
    assert w.poll() is None and w.ignored.get("own")


def test_a_key_pressed_before_the_watch_began_is_the_baseline() -> None:
    state = {"t": 1000.0, "last": 900.0, "span": (0.0, 0.0), "pressed": 950.0}
    w = _watch(state)
    assert w.poll() is None
    state["t"] += 0.1
    assert w.poll() is None


def test_the_keyboard_hook_stamps_the_key_down_time() -> None:
    from yuki.actions import leases

    before = leases.key_down_at()
    leases.note_key_down(at=before + 5.0)
    assert leases.key_down_at() == before + 5.0
    leases.note_key_down(at=before)


# ---------------------------------------------------------------------------
# Window close: a slow app is not "failed"; its own dialog is "asking"
# ---------------------------------------------------------------------------


class _Win:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, closes_after: float | None = None, cloaks_after: float | None
                 = None, dialog_after: float | None = None, other_front: bool = False) -> None:
        import time

        from yuki.actions import launch as L

        self.t0 = time.monotonic()
        self.closes_after, self.cloaks_after, self.dialog_after = closes_after, cloaks_after, dialog_after
        self.other_front = other_front
        self.posted: list[int] = []
        g = L.win32gui
        monkeypatch.setattr(g, "IsWindow", lambda h: not self._past(self.closes_after))
        monkeypatch.setattr(g, "IsWindowVisible", lambda h: h != 77 and not self._past(self.closes_after) or h == 88)
        monkeypatch.setattr(g, "IsWindowEnabled", lambda h: True)
        monkeypatch.setattr(g, "IsIconic", lambda h: False)
        monkeypatch.setattr(g, "PostMessage", lambda h, *a: self.posted.append(h))
        monkeypatch.setattr(g, "GetWindow", lambda h, kind: 501 if h == 88 else 0)
        monkeypatch.setattr(L, "_is_zoomed", lambda h: False)
        monkeypatch.setattr(L, "window_info", lambda h: SimpleNamespace(process_name="Browser.exe", title="Start"))
        monkeypatch.setattr(L, "_is_cloaked", lambda h: self._past(self.cloaks_after))
        monkeypatch.setattr(L._user32, "GetForegroundWindow", lambda: self._front())
        monkeypatch.setattr("yuki.perception.windows.window_state", lambda h: "normal")
        monkeypatch.setattr(L, "_CLOSE_WAIT_S", 1.5)
        monkeypatch.setattr(L, "_ASKING_HOLD_S", 0.2)
        monkeypatch.setattr(L, "_POLL_S", 0.02)

    def _past(self, after: float | None) -> bool:
        import time

        return after is not None and time.monotonic() - self.t0 >= after

    def _front(self) -> int:
        if self._past(self.dialog_after):
            return 88   # a dialog owned by the window
        return 99 if self.other_front else 501


def test_a_window_that_closes_after_a_few_seconds_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.actions.launch import window_action

    _Win(monkeypatch, closes_after=0.6, other_front=True)   # another window of the app came forward meanwhile
    out = window_action(501, "close")
    assert out.ok and "is closed" in out.summary


def test_a_window_cloaked_on_its_way_out_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.actions.launch import window_action

    _Win(monkeypatch, cloaks_after=0.2)
    assert window_action(501, "close").ok


def test_a_window_whose_own_dialog_asks_something_says_so_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.actions.launch import window_action

    _Win(monkeypatch, dialog_after=0.1)
    out = window_action(501, "close")
    assert not out.ok and out.details["asking"] is True and "asking something first" in out.summary
    assert out.details["close_waited_s"] < 1.0


# ---------------------------------------------------------------------------
# Arc's slow open: an address dropped at a cold start is handed again once
# ---------------------------------------------------------------------------


def _launch_world(monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> dict[str, Any]:
    from yuki.actions import launch as L
    from yuki.perception.tree import PageSeen, PageWait

    state: dict[str, Any] = {"starts": 0, "running": set()}
    chosen = {"name": "Browser", "appid": "Example.Browser"}
    monkeypatch.setattr(L, "get_start_apps", lambda refresh=False: [chosen])
    monkeypatch.setattr(L, "_match_apps", lambda query, apps: (chosen, []))
    monkeypatch.setattr(L, "_plan_start_with_args", lambda c, args: (
        {"via": "Start-Process", "arguments": " ".join(args), "command": "start browser", "exe": "browser.exe",
         "source": "test"}, []))

    class Identity:
        def running_pids(self) -> set[int]:
            return set(state["running"])

        def add_launched(self, pid: int) -> None:
            pass

        def describe(self) -> dict[str, Any]:
            return {"names": ["browser.exe"]}

        def owns_window(self, hwnd: int) -> bool:
            return True

    monkeypatch.setattr(L, "_identity_for", lambda c, plan: Identity())
    monkeypatch.setattr(L, "_top_level_windows", lambda pids: [])
    monkeypatch.setattr(L, "_window_snapshot", lambda: ({}, set(), {}))
    monkeypatch.setattr(L._user32, "GetForegroundWindow", lambda: 0)

    def start(command: str, timeout_s: float = 10.0) -> Any:
        state["starts"] += 1
        state["running"] = {4242}
        return ActionResult(ok=True, summary="started", details={"stdout": "4242", "stderr": ""})

    monkeypatch.setattr(L, "run_powershell", start)
    waits = list(pages)

    # r30: x8WebArc (12/16 Oct) passes _await_page the pages already showing (``unchanged``) and _page_opened its
    # focus floor (``focus_floor_s``, ``already``)
    def await_page(identity: Any, targets: list[str], before: Any, timeout_s: float, unchanged: Any = None) -> Any:
        page = waits.pop(0)
        if page == "none":
            return PageWait(seen=[], windows=[700], polls=3, answered=True)
        return PageWait(matched=PageSeen(hwnd=700, title="Video site", url=targets[0]), seen=[], windows=[700],
                        polls=1, answered=True)

    monkeypatch.setattr(L, "_await_page", await_page)
    monkeypatch.setattr(L, "_page_opened", lambda name, wait, before, details, deadline, targets, identity=None, **_k: (
        details.update(page_verified=True, hwnd=700, foreground=True) or f"{name} shows {targets[0]}"))
    return state


def test_an_address_a_cold_start_dropped_is_handed_again_once_and_the_page_is_verified(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.actions.launch import launch_app

    state = _launch_world(monkeypatch, ["none", "page"])
    out = launch_app("Browser", args=["https://www.example.com"], timeout_s=0.5, content_timeout_s=0.5)
    assert state["starts"] == 2 and out.ok and out.details["verified"] is True
    assert out.details["handed_again"] == {"launched": True} and "handed again" in out.summary


def test_a_running_browser_is_never_handed_the_address_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.actions import launch as L

    state = _launch_world(monkeypatch, ["none"])
    state["running"] = {1111}
    monkeypatch.setattr(L, "_wait_for_new_window", lambda *a, **k: (None, "", []))
    monkeypatch.setattr(L, "_no_window_outcome", lambda *a, **k: "no page could be read")
    monkeypatch.setattr(L.psutil, "Process", lambda pid: SimpleNamespace(is_running=lambda: False))
    out = L.launch_app("Browser", args=["https://www.example.com"], timeout_s=0.5, content_timeout_s=0.5)
    assert state["starts"] == 1 and "handed_again" not in out.details
    assert out.details["verified"] is None   # handed an address, no page read: not verified by the window alone


def test_the_launch_verdict_with_an_address_and_no_page_read_is_unverified() -> None:
    from yuki.actions.launch import _launch_verdict

    own = {"hwnd": 5, "foreground": True, "identity": {"names": ["browser.exe"]}}
    assert _launch_verdict(True, {**own, "page_targets": ["https://www.example.com"]}) is None


# ---------------------------------------------------------------------------
# Review fixes (report 83, "Review fixes")
# ---------------------------------------------------------------------------


def _feed_under_a_bar(first: int, *, extra: list[dict[str, Any]] | None = None,
                      drop: int | None = None) -> list[dict[str, Any]]:
    """A page whose Document scrolls: a search bar that stays put at the top, video tiles below it."""
    root, pane, *items = _feed(first, position=None)
    tiles = [{**t, "bounds": (0, t["bounds"][1] + 100, 800, t["bounds"][3] + 100),
              "center": (400, t["center"][1] + 100)} for t in items if t["id"] != drop]
    bar = _item(50, "Search", 1, 20, role="Edit")
    bar["bounds"], bar["center"] = (200, 20, 600, 60), (400, 40)
    return [root, pane, bar, *tiles, *(extra or [])]


def test_f1_a_scroll_that_did_nothing_while_one_item_swapped_on_its_own_is_not_the_content_moving() -> None:
    # review F1: the page is at its end, the scroll does nothing, a hover label goes and a badge comes: 9 of 10 items
    # stayed in place, so nothing was replaced by scrolling
    before = _feed(0, position=None)
    after = [e for e in before if e["id"] != 105] + [_item(300, "New badge", 1, 480)]
    for point in (None, (400, 300), (400, 220)):
        change = diff(before, after, scroll=True, point=point)
        assert not change.turnover and not change.content_moved, point
        assert not verdict(change)[1].startswith("the window changed: its content moved"), point


def test_f1_a_screenful_scroll_under_a_bar_that_stays_is_judged_by_what_was_under_the_point() -> None:
    # the live YouTube case with its fixed search bar: every tile replaced, the bar left in both reads
    before, after = _feed_under_a_bar(0), _feed_under_a_bar(20)
    change = diff(before, after, scroll=True, point=(400, 300))   # over the tiles
    assert change.turnover and change.content_moved
    # without the point, an item left in place says nothing was replaced
    assert not diff(before, after, scroll=True).turnover
    # with the point on the bar that stayed, the content under it did not move
    assert not diff(before, after, scroll=True, point=(400, 40)).turnover
    # a point outside the scrolling container is not a scroll of it
    assert not diff(before, after, scroll=True, point=(900, 300)).turnover


def test_f1_one_tile_under_the_point_swapped_while_the_rest_stayed_is_not_a_scroll() -> None:
    # tile 105 (y 300-340) went and another came at its place, but the tiles around it stayed where they were
    before = _feed_under_a_bar(0)
    after = _feed_under_a_bar(0, drop=105, extra=[_item(301, "Ad", 1, 300)])
    change = diff(before, after, scroll=True, point=(400, 320))
    assert not change.turnover and not change.content_moved


def _covered_settings_page() -> Node:
    window = Node("Window", "Settings - Browser", (0, 0, 1200, 800))
    pane = Node("Pane", "", (0, 80, 1200, 800), offscreen=True)
    page = Node("Document", "Settings", (0, 80, 1200, 800), value="https://example.com/settings", offscreen=True)
    page.add(Node("CheckBox", "Send reports", (20, 100, 200, 130), toggle=0, offscreen=True),
             Node("Button", "Dark mode", (20, 140, 200, 170), toggle=1, offscreen=True).add(
                 Node("Text", "Dark mode label", (25, 145, 190, 165), offscreen=True)),
             Node("Slider", "Text size", (20, 180, 300, 210), range=(12.0, 8.0, 32.0), offscreen=True),
             Node("Button", "Save", (20, 220, 100, 250), invoke=True, offscreen=True))
    pane.add(page)
    window.add(pane)
    return window


def test_f2_a_covered_window_read_by_geometry_lists_its_switches_and_sliders_as_shown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # review F2: a window other windows cover reports everything off-screen; its state controls are not "hidden"
    tree = read(_covered_settings_page(), monkeypatch=monkeypatch)
    for name in ("Send reports", "Dark mode", "Text size", "Save"):
        assert not _by_name(tree, name).hidden, name
    assert any(e.name == "Dark mode label" for e in tree.elements)
    assert _by_name(tree, "Text size").range == (12.0, 8.0, 32.0)


def test_f2_the_small_box_rule_still_holds_where_the_off_screen_flag_is_not_trusted() -> None:
    def getter(box: tuple[int, int, int, int] | None, offscreen: bool = True) -> Any:
        values = {T._P_IS_RANGE_VALUE_AVAILABLE: True, T._P_IS_TOGGLE_AVAILABLE: False,
                  T._P_IS_OFFSCREEN: offscreen, T._P_BOUNDING_RECT: box}
        return lambda prop: values.get(prop)

    area = (0, 0, 1000, 800)
    full = (20, 180, 280, 30)    # the raw property: left, top, width, height
    tiny = (100, 100, 1, 1)
    assert T._hidden_in_place(getter(full), area) is True                       # trusted: any size counts
    assert T._hidden_in_place(getter(full), area, trusted=False) is False       # covered window: not hidden
    assert T._hidden_in_place(getter(tiny), area, trusted=False) is True        # a 1 px input is hidden anywhere
    assert T._hidden_in_place(getter(None), area, trusted=False) is True        # no box at all
    assert T._hidden_in_place(getter(full, False), area, trusted=False) is False


class _Hub:
    """A fake event hub: a generation counter the test moves; nothing waits for real."""

    def __init__(self) -> None:
        self.gen = 0
        self.waits: list[tuple[int, float]] = []

    def watch(self, hwnd: int) -> None:
        return None

    def generation(self, hwnd: int) -> int:
        return self.gen

    def wait_change(self, hwnd: int, since: int, timeout_s: float) -> bool:
        self.waits.append((since, timeout_s))
        return self.gen > since

    def wait_quiet(self, hwnd: int, *, quiet_s: float, cap_s: float) -> tuple[bool, float]:
        return True, 0.0

    def busy_since(self, hwnd: int, since: float) -> list[Any]:
        return []


def _hub_dispatcher(hub: _Hub, reads: list[Any], *, bump_on_read: bool = False) -> Any:
    from yuki.agent.tools import Dispatcher

    d = Dispatcher(backend=SimpleNamespace())
    d.verify_outcomes = True
    d.events = hub
    d.use_events = True
    taken: list[Any] = []

    def observe(hwnd: int) -> tuple[Any, bool]:
        taken.append(reads[len(taken)])
        if bump_on_read:
            hub.gen += 1   # the window's event lands while this read runs
        return taken[-1], False

    d._observe = observe
    return d


def test_f3_an_event_that_landed_during_the_read_after_is_not_waited_for_again() -> None:
    import time

    hub = _Hub()
    d = _hub_dispatcher(hub, ["on"])
    read_generation = hub.gen      # taken before the after-read
    hub.gen += 1                   # the toggle's event arrives while that read runs: it still shows "off"
    after, _partial, extra = d._read_again_until(7001, "off", False, lambda r: r == "on",
                                                 time.monotonic() + 3.0, read_generation)
    assert after == "on" and extra == 1 and hub.waits[0][0] == read_generation


def test_f3_each_re_read_takes_its_generation_before_it_reads() -> None:
    import time

    hub = _Hub()
    hub.gen = 1
    d = _hub_dispatcher(hub, ["off", "on"], bump_on_read=True)
    after, _partial, extra = d._read_again_until(7001, "off", False, lambda r: r == "on",
                                                 time.monotonic() + 3.0, 0)
    # the first re-read still showed "off" while an event landed during it: read again at once, not a later event
    assert after == "on" and extra == 2 and [w[0] for w in hub.waits] == [0, 1]


def test_f4_a_toggle_its_own_pattern_confirmed_does_not_wait_long_for_a_window_event(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent import tools as tools_mod

    firsts: list[float] = []

    def recording(d: Any) -> None:
        original = d._settled_after

        def recorded(hwnd: int, generation: Any, *, first_s: float = tools_mod.OUTCOME_FIRST_EVENT_S) -> None:
            firsts.append(first_s)
            return original(hwnd, generation, first_s=first_s)

        d._settled_after = recorded

    desk = _PatternDesk(monkeypatch, player_window(lyrics_on=False))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    recording(d)
    out = d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert out.payload["details"]["verified"] is True
    assert firsts == [tools_mod.OUTCOME_FIRST_EVENT_S]
    # a mouse-clicked toggle (no state of its own yet) still gets the long first wait
    desk = _Desk(monkeypatch, player_window(lyrics_on=False))
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    firsts.clear()
    recording(d)
    d.dispatch("click", {"element": _id_of(text, "Lyrics"), "expect_hwnd": 7001, "toggle_to": "on"})
    assert firsts == [tools_mod.OUTCOME_TARGET_WAIT_S]


def _guarded(desk: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dispatcher with the production raw-click guard on, its window facts stubbed (no real window is asked)."""
    from yuki.agent import tools as tools_mod

    d = _dispatcher(desk)
    d.guard_raw_clicks = True
    d._pointed_window = lambda x, y: 7001
    monkeypatch.setattr(tools_mod, "_window_title_now", lambda h: None)
    monkeypatch.setattr(tools_mod, "_window_rect_now", lambda h: None)
    monkeypatch.setattr("yuki.perception.tree.measured_opaque", lambda h: False)
    monkeypatch.setattr("yuki.perception.windows.cursor_position", lambda: (0, 0))
    return d


class _PointerDesk(_Desk):
    """A desk that can bring the pointer to a point (``on_point`` runs as it arrives)."""

    def point_at(self, x: int, y: int, *, expect_hwnd: int | None = None) -> Any:
        self.pointed.append((x, y))
        if self.on_point is not None:
            self.on_point(x, y)
        return ActionResult(ok=True, summary=f"pointer at ({x},{y})", details={})


def test_f5_with_the_guard_on_a_click_on_an_empty_margin_is_refused_before_anything_moves(
        monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _PointerDesk(monkeypatch, player_window())
    d = _guarded(desk, monkeypatch)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 300, "y": 870, "expect_hwnd": 7001})   # in the library, below the playlists
    assert not out.ok and "nothing you can click is at (300,870)" in out.summary
    assert desk.clicks == [] and desk.pointed == []


def test_f5_with_the_guard_on_a_control_that_leaves_as_the_pointer_comes_makes_the_click_unverified(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # the latest read had a card at the point (the guard lets the click through); the pointer arriving closes it, so
    # the read just before the click has nothing there: what changes after is not taken as the click's effect
    from yuki.agent.loop import action_verdict

    window = player_window()
    library = next(n for n in window.walk() if n.name == "Your Library")
    card = Node("Button", "Show card", (153, 850, 461, 880), invoke=True)
    library.add(card)
    desk = _PointerDesk(monkeypatch, window)

    def closes(x: int, y: int) -> None:
        if card in library.children:
            library.children.remove(card)

    def churn(x: int, y: int) -> None:
        library.children.pop(0)   # something else in the window goes on its own as the click lands

    desk.on_point, desk.on_click = closes, churn
    d = _guarded(desk, monkeypatch)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 300, "y": 865, "expect_hwnd": 7001})
    details = out.payload["details"]
    assert desk.pointed == [(300, 865)] and desk.clicks == [(300, 865)]
    assert out.ok and details["point_on_nothing"] is True and "verified" not in details
    assert action_verdict("click", out)[0] is None



# ---------------------------------------------------------------------------
# Second review fixes (report 83, "Second review")
# ---------------------------------------------------------------------------


def _with_background_tab(window: Node) -> Node:
    """The live Zen case (V1): a background tab's page under the browser's content pane, reported off-screen, its
    checkboxes and a toggle with boxes inside the view (Chromium keeps a hidden tab's layout)."""
    pane = next(n for n in window.walk() if n.role == "Pane")
    tab = Node("Document", "Background tab", (105, 121, 1507, 951), value="https://example.com/settings",
               offscreen=True)
    tab.add(Node("Group", "Settings list", (300, 300, 900, 600), offscreen=True).add(
        *[Node("CheckBox", f"Option {i}", (310, 310 + 40 * i, 600, 340 + 40 * i), toggle=0, offscreen=True)
          for i in range(5)],
        Node("Button", "Background switch", (700, 310, 760, 340), toggle=1, offscreen=True)))
    pane.add(tab)
    return window


def test_v1_a_background_tabs_checkboxes_are_not_listed_as_hidden_controls_of_the_front_page(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # the fake now refuses to climb from a cached-only element, as real UI Automation does ("Invalid pointer")
    window = _with_background_tab(player_window(volume=0.4))
    slider = _node(window, "Change volume", "Slider")
    slider.bounds, slider.offscreen = (1376, 918, 1416, 942), True   # the front page's own hidden-in-place input
    tree = read(window, monkeypatch=monkeypatch)
    assert not any(e.name.startswith("Option ") or e.name == "Background switch" for e in tree.elements)
    found = _by_name(tree, "Change volume", "Slider")
    assert found.hidden and found.range == (0.4, 0.0, 1.0)


def test_v1_the_fake_refuses_to_climb_from_a_cached_only_element() -> None:
    from _fake_uia import Automation, Request

    root = player_window()
    automation = Automation({1: root})
    request = Request()
    cached = automation.build(root.children[0], request)   # AutomationElementMode NONE
    with pytest.raises(OSError):
        automation.ControlViewWalker.GetParentElementBuildCache(cached, request)


def test_v1_when_the_pages_under_a_batch_cannot_be_asked_a_hidden_control_is_left_out(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from _fake_uia import Cached

    def refuses(self: Any, scope: int, condition: Any, request: Any) -> Any:
        raise OSError(-2147467259, "E_FAIL")

    monkeypatch.setattr(Cached, "FindAllBuildCache", refuses)
    tree = read(_with_background_tab(player_window()), monkeypatch=monkeypatch)
    assert not any(e.name.startswith("Option ") or e.name == "Background switch" for e in tree.elements)


def test_v3_a_window_whose_sliders_are_all_listed_is_not_swept_again_until_its_pages_change(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from _fake_uia import Automation, Cached

    window = player_window(slider_box="full")
    first = read(window, monkeypatch=monkeypatch)
    assert any(e.name == "Change volume" and e.role == "Slider" and not e.hidden for e in first.elements)
    assert 7001 in T._no_sliders   # swept once, nothing to add
    calls: list[int] = []
    original = Cached.FindAllBuildCache

    def counted(self: Any, scope: int, condition: Any, request: Any) -> Any:
        if condition == ("prop", T._P_IS_RANGE_VALUE_AVAILABLE, True):   # the sweep's search (not the V1 lookup)
            calls.append(1)
        return original(self, scope, condition, request)

    monkeypatch.setattr(Cached, "FindAllBuildCache", counted)
    T._collect_pass(Automation({7001: window}), 7001, [], 400, time.monotonic() + 5.0)
    assert calls == []
    # a navigation: the page Document is new, so the sweep runs again
    doc = next(n for n in window.walk() if n.role == "Document")
    doc.rid = (42, 999_999)
    T._collect_pass(Automation({7001: window}), 7001, [], 400, time.monotonic() + 5.0)
    assert calls


def _point_feed(first: int, *, position: float, carousel: float | None = None,
                fade: int | None = None) -> list[dict[str, Any]]:
    """A page whose Document scrolls (tiles at x 0-800), with a horizontal carousel scroller at the right (x 820-1200)
    or a fading one-slide carousel under the point."""
    root, pane, *items = _feed(first, position=position)
    root["bounds"] = (0, 0, 1200, 600)
    out = [root, pane, *items]
    if carousel is not None:
        out.append({"id": 60, "role": "List", "name": "Carousel", "parent": 0, "depth": 1,
                    "bounds": (820, 0, 1200, 200), "center": (1010, 100), "states": (), "value": None,
                    "is_interactive": True, "is_scrollable": True, "scroll": (carousel, -1.0)})
    if fade is not None:
        out.append({"id": 70, "role": "Group", "name": "Hero", "parent": 0, "depth": 1, "bounds": (820, 300, 1200,
                    560), "center": (1010, 430), "states": (), "value": None, "is_interactive": False,
                    "is_scrollable": True})
        out.append({**_item(700 + fade, f"Slide {fade}", 70, 400), "bounds": (830, 400, 1190, 440),
                    "center": (1010, 420)})
    return out


def test_v4_a_carousel_elsewhere_scrolling_itself_is_not_the_scroll_moving_the_content() -> None:
    before = _point_feed(0, position=100.0, carousel=10.0)
    after = _point_feed(0, position=100.0, carousel=40.0)
    change = diff(before, after, scroll=True, point=(400, 300), axes="v")
    assert not change.content_moved and change.scroll_aside and change.scroll_aside[0][4] == "elsewhere"
    assert verdict(change)[0] is None
    assert any("away from where the scroll was made" in ln for ln in describe(change))
    # the same carousel scrolled by a horizontal scroll made over it is the scroll
    over = diff(before, after, scroll=True, point=(1000, 100), axes="h")
    assert over.content_moved and over.scrolled


def test_v4_one_fading_slide_swapped_under_the_point_is_not_a_scroll() -> None:
    before = _point_feed(0, position=100.0, fade=1)
    after = _point_feed(0, position=100.0, fade=2)
    change = diff(before, after, scroll=True, point=(1000, 420), axes="v")
    assert not change.turnover and not change.content_moved


def test_v4_a_feed_that_loaded_more_at_its_end_did_not_move_the_view() -> None:
    before = _feed(0, position=100.0)
    after = _feed(0, position=85.0)   # the same items in place; the page grew below the view
    change = diff(before, after, scroll=True, point=(400, 300), axes="v")
    assert not change.content_moved and change.scroll_aside[0][4] == "grew"
    assert any("its content grew or shrank, the view did not move" in ln for ln in describe(change))
    assert not any("scrolled up" in ln for ln in describe(change))
    # a real scroll still moves: the items shift and the position goes on
    real = diff(_feed(0, position=10.0), _feed(3, position=20.0), scroll=True, point=(400, 300), axes="v")
    assert real.content_moved


class _TimedSlider:
    """A slider over real time (V2/V8): SetValue applies at once (snapped to ``snap`` when given) and raises a change
    event; the app puts the old value back ``revert_after`` s later (another event)."""

    def __init__(self, value: float = 0.5, *, snap: float | None = None, revert_after: float | None = None,
                 events: bool = True) -> None:
        import threading

        self.value, self.snap, self.revert_after, self.events = value, snap, revert_after, events
        self.event = threading.Event()
        self.timers: list[Any] = []

    def read(self) -> tuple[float, float, float, bool, float]:
        return self.value, 0.0, 1.0, False, 0.01

    def set(self, value: float) -> None:
        import threading

        old = self.value
        self.value = round(round(value / self.snap) * self.snap, 4) if self.snap else value
        self.event.set()
        if self.revert_after is not None:
            def back() -> None:
                self.value = old
                self.event.set()
            timer = threading.Timer(self.revert_after, back)
            timer.start()
            self.timers.append(timer)

    def focus(self) -> None:
        pass

    def watch(self) -> Any:
        slider = self

        class Watch:
            live = slider.events

            def wait(self, timeout_s: float) -> bool:
                got = slider.event.wait(max(0.0, timeout_s))
                slider.event.clear()
                return got

            def close(self) -> None:
                pass

        return Watch()


def test_v2_a_value_the_app_puts_back_during_the_hold_is_caught_with_events() -> None:
    from yuki.actions.slider import drive

    s = _TimedSlider(revert_after=0.15)
    ok, _summary, details = drive(s, by=-30, hold_s=0.4)
    for timer in s.timers:
        timer.join()
    assert not ok and details["verified"] is False and details.get("reverted") is True
    assert details["after"] == pytest.approx(0.5)


def test_v2_a_value_that_holds_is_verified_after_the_whole_hold() -> None:
    import time

    from yuki.actions.slider import drive

    s = _TimedSlider()
    started = time.monotonic()
    ok, _summary, details = drive(s, by=-30, hold_s=0.3)
    assert ok and details["verified"] is True and time.monotonic() - started >= 0.29


@pytest.mark.parametrize("events", [True, False])
def test_v8_a_slider_that_snaps_off_target_stops_waiting_once_it_holds_its_level(events: bool) -> None:
    import time

    from yuki.actions.slider import drive

    s = _TimedSlider(snap=0.1, events=events)
    started = time.monotonic()
    ok, _summary, details = drive(s, to=33, hold_s=0.0)
    elapsed = time.monotonic() - started
    assert not ok and details["after"] == pytest.approx(0.3) and details.get("settled_off_target") is True
    assert elapsed < 0.8, elapsed


class _HoverHub:
    def __init__(self, *, busy: bool) -> None:
        import time

        self.now = time.monotonic()
        self.last_at = self.now - (0.05 if busy else 30.0)
        self.firsts: list[float] = []
        self.caps: list[float] = []

    def clock(self) -> float:
        return self.now

    def watch(self, hwnd: int) -> None:
        return None

    def generation(self, hwnd: int) -> int:
        return 5

    def activity(self, hwnd: int) -> Any:
        return SimpleNamespace(last_at=self.last_at)

    def wait_change(self, hwnd: int, since: int, timeout_s: float) -> bool:
        self.firsts.append(timeout_s)
        return True

    def wait_quiet(self, hwnd: int, *, quiet_s: float, cap_s: float) -> tuple[bool, float]:
        self.caps.append(cap_s)
        return False, cap_s


@pytest.mark.parametrize("busy", [False, True])
def test_v5_the_hover_before_a_raw_click_waits_briefly(monkeypatch: pytest.MonkeyPatch, busy: bool) -> None:
    from yuki.agent import tools as tools_mod
    from yuki.agent.tools import Dispatcher

    pointed: list[tuple[int, int]] = []
    backend = SimpleNamespace(point_at=lambda x, y, expect_hwnd=None: pointed.append((x, y)) or {"ok": True})
    d = Dispatcher(backend=backend)
    hub = _HoverHub(busy=busy)
    d.events, d.use_events = hub, True
    d._before_input = lambda name: None
    monkeypatch.setattr("yuki.perception.windows.cursor_position", lambda: (0, 0))
    d._hover_first(7001, 300, 400, 7001)
    assert pointed == [(300, 400)]
    assert hub.firsts == [tools_mod.HOVER_FIRST_EVENT_S] and tools_mod.HOVER_FIRST_EVENT_S <= 0.2
    assert hub.caps == [tools_mod.HOVER_BUSY_CAP_S if busy else tools_mod.HOVER_SETTLE_S]
    assert tools_mod.HOVER_BUSY_CAP_S < tools_mod.HOVER_SETTLE_S


def test_v6_a_click_that_opened_a_menu_is_verified_though_the_hover_replaced_the_control(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    window = player_window()
    library = next(n for n in window.walk() if n.name == "Your Library")
    card = Node("Button", "Show card", (153, 850, 461, 880), invoke=True)
    library.add(card)
    desk = _PointerDesk(monkeypatch, window)

    def preview(x: int, y: int) -> None:
        if card in library.children:   # the hover swaps the card for a preview that is not clickable
            library.children.remove(card)
            library.add(Node("Group", "Preview", (153, 850, 461, 880)))

    def opens(x: int, y: int) -> None:
        library.add(Node("Menu", "Card menu", (300, 600, 500, 845)).add(
            Node("MenuItem", "Save", (305, 605, 495, 630), invoke=True)))

    desk.on_point, desk.on_click = preview, opens
    d = _guarded(desk, monkeypatch)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("click", {"x": 300, "y": 865, "expect_hwnd": 7001})
    details = out.payload["details"]
    assert out.ok and "point_on_nothing" not in details and details["verified"] is True
    assert action_verdict("click", out)[0] is True


def test_v7_a_key_the_hook_saw_that_the_pc_never_took_as_input_is_not_a_takeover() -> None:
    # Deskflow's hook swallowed it (typed onto another screen): GetLastInputInfo never moved
    state = {"t": 1000.0, "last": 900.0, "span": (0.0, 0.0), "pressed": 0.0}
    w = _watch(state)
    assert w.poll() is None
    state["t"] += 1.0
    assert w.poll() is None
    state["pressed"] = state["t"] + 0.03
    for _ in range(5):
        state["t"] += 0.1
        assert w.poll() is None
    assert w.ignored.get("unseen") == 1


def test_v7_a_hook_key_whose_last_input_shows_a_moment_later_still_counts() -> None:
    state = {"t": 1000.0, "last": 900.0, "span": (0.0, 0.0), "pressed": 0.0}
    w = _watch(state)
    w.poll()
    state["t"] += 1.0
    w.poll()
    state["pressed"] = state["t"] + 0.03
    state["t"] += 0.1
    assert w.poll() is None          # the last-input time has not caught up yet
    state["last"] = state["pressed"] + 0.001
    state["t"] += 0.1
    assert w.poll() == "keys"


def test_v7_yukis_own_keys_carry_the_mark_the_hook_leaves_out() -> None:
    from yuki.actions import input as I
    from yuki.ui.focus import INJECT_MARK

    assert I.INJECT_MARK == INJECT_MARK
    event = I._key_event(0x41, 0, up=False)
    assert event.union.ki.dwExtraInfo == INJECT_MARK


def test_v7_the_hook_stamps_the_events_own_time() -> None:
    import ctypes
    import time

    from yuki.ui.hotkey import _event_moment

    tick = int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF
    at = _event_moment((tick - 200) & 0xFFFFFFFF)
    assert time.monotonic() - at == pytest.approx(0.2, abs=0.05)
    assert abs(time.monotonic() - _event_moment((tick - 60_000) & 0xFFFFFFFF)) < 0.05   # far off: now
