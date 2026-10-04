"""appPerception (report 58): the screen-understanding foundation, on stub trees shaped like the 3-4 Oct reads.

No desktop, no COM calls to other processes, no network: the reader runs over a fake UI Automation tree
(``tests/_fake_uia.py``), the dispatcher over a stub desk built on it, the event hub over a fake source. Names are
invented.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from _fake_uia import Node, chat_window, feed_window, player_window, read
from yuki.actions import ActionResult
from yuki.perception import tree as T
from yuki.perception.model import WindowModel, carry_refs
from yuki.perception.outcome import describe, diff, verdict


def _by_name(tree: T.WindowTree, name: str, role: str | None = None) -> T.UIElement:
    return next(e for e in tree.elements if e.name == name and (role is None or e.role == role))


# ---------------------------------------------------------------------------
# (a) the reader: hidden state controls, what was left out, the budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("box", ["none", "pixel"])
def test_spotify_volume_slider_hidden_in_its_label_is_read_with_its_numbers(monkeypatch: pytest.MonkeyPatch,
                                                                            box: str) -> None:
    tree = read(player_window(volume=0.62, slider_box=box), monkeypatch=monkeypatch)
    slider = _by_name(tree, "Change volume", "Slider")
    label = next(e for e in tree.elements if e.role == "Text" and e.name == "Change volume")
    assert slider.hidden and slider.range == (0.62, 0.0, 1.0) and "range" in slider.patterns
    assert slider.bounds == label.bounds and slider.runtime_id is not None and slider.parent == label.id
    assert tree.hidden_controls == 2   # the volume and the progress inputs
    text = T.format_window_tree(tree)
    line = next(ln for ln in text.splitlines() if '"Change volume"' in ln and "Slider" in ln)
    assert "[hidden]" in line and 'value="0.62 (range 0-1)"' in line and "@(" not in line
    assert "[hidden] = a control the app keeps out of sight" in text.splitlines()[0]


def test_a_normal_visible_slider_is_not_marked_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = read(player_window(slider_box="full"), monkeypatch=monkeypatch)
    slider = _by_name(tree, "Change volume", "Slider")
    assert not slider.hidden and slider.range == (0.6, 0.0, 1.0)
    assert "@(1396,930)" in T.format_window_tree(tree)


def test_a_toggle_scrolled_out_of_view_is_not_taken_for_a_hidden_control(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Node("Window", "Settings", (0, 0, 800, 600))
    pane = Node("List", "Options", (0, 0, 800, 600), scroll=True)
    pane.add(Node("CheckBox", "Visible option", (10, 10, 300, 40), toggle=1),
             Node("CheckBox", "Far option", (10, 900, 300, 930), toggle=0, offscreen=True))
    root.add(pane)
    tree = read(root, monkeypatch=monkeypatch)
    names = {e.name for e in tree.elements}
    assert "Visible option" in names and "Far option" not in names
    entry = next(e for e in tree.left_out if e["parent"] == _by_name(tree, "Options").id)
    assert entry["below"] == 1 and entry["named"] == [["CheckBox", "Far option", "below"]]


def test_toggle_states_are_in_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    on = read(player_window(lyrics_on=True), monkeypatch=monkeypatch)
    off = read(player_window(lyrics_on=False), monkeypatch=monkeypatch)
    assert "on" in _by_name(on, "Lyrics").states and "off" in _by_name(off, "Lyrics").states
    assert '"Lyrics" @(1290,917) {toggle} [on]' in T.format_window_tree(on)


def test_an_auto_hidden_player_bar_is_said_to_be_kept_out_of_sight(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = read(player_window(bar_offscreen=True), monkeypatch=monkeypatch)
    assert not any(e.name == "Lyrics" for e in tree.elements)
    text = T.format_window_tree(tree)
    assert 'kept out of sight by the app: Button "Lyrics", Button "Queue"' in text


def test_a_long_chat_says_how_many_messages_are_above_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = read(chat_window(messages=60, visible_from=40, doc_scrolls=True), monkeypatch=monkeypatch)
    chat = _by_name(tree, "Messages in @Sam")
    entry = next(e for e in tree.left_out if e["parent"] == chat.id)
    assert entry["above"] == 40 and entry["below"] == 5
    text = T.format_window_tree(tree)
    assert "off screen: 40 above, 5 below" in text and "scroll it to bring them into view" in text
    # the newest visible message is in the read
    assert "invented message number 55" in text


def test_the_apps_own_numbering_says_what_lies_beyond_the_listed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = read(chat_window(messages=60, visible_from=40, positions=True), monkeypatch=monkeypatch)
    text = T.format_window_tree(tree)
    assert "items 41-55 of 60 are listed; 40 before them and 5 after them are out of view" in text


def test_a_read_stopped_by_its_cap_names_what_it_did_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = read(player_window(), monkeypatch=monkeypatch, max_elements=9)
    assert tree.truncated
    text = T.format_window_tree(tree)
    assert "not read, the read stopped at its limit" in text and "look_at_window with within=" in text
    assert any(entry["unread_count"] for entry in tree.left_out)


def _long_list_tree(items: int = 300, focused: int | None = None) -> T.WindowTree:
    elements = [T.UIElement(0, "Window", "Library", None, (500, 500), (0, 0, 1000, 1000), False, False, False, None,
                            0),
                T.UIElement(1, "List", "Songs", None, (500, 500), (0, 0, 1000, 1000), True, True, False, None, 1,
                            parent=0)]
    for i in range(items):
        elements.append(T.UIElement(2 + i, "ListItem", f"Invented song number {i + 1} by Example Band", None,
                                    (500, 10 + i), (0, i, 1000, i + 1), True, False, focused == i, None, 2,
                                    patterns=("invoke", "select"), parent=1))
    return T.WindowTree(hwnd=1, title="Library", process_name="App.exe", elements=elements)


def test_past_the_budget_the_middle_of_a_long_list_is_folded_with_both_ends_kept() -> None:
    tree = _long_list_tree(300, focused=150)
    text = T.format_window_tree(tree, budget=6000)
    assert len(text) <= 6500
    assert "number 1 by" in text and "number 300 by" in text      # both ends
    assert "number 151 by" in text                                 # the focused item is never folded
    assert "items folded here" in text and "look_at_window with within=1 lists them all" in text
    full = T.format_window_tree(tree, within=1)
    assert all(f"number {i} by" in full for i in (2, 77, 150, 299)) and "only [1] and what is below it" in full


def test_nothing_is_folded_while_the_read_fits() -> None:
    text = T.format_window_tree(_long_list_tree(20))
    assert "folded" not in text and text.count("ListItem") == 20


def test_recorded_size_reads_stay_under_the_budget() -> None:
    text = T.format_window_tree(_long_list_tree(1000))
    assert len(text) <= T.FORMAT_BUDGET_CHARS + 200


# ---------------------------------------------------------------------------
# (c) stable references
# ---------------------------------------------------------------------------


def _el(i: int, role: str, name: str, parent: int = -1, rid: tuple | None = None, aid: str = "",
        center: tuple = (0, 0)) -> dict[str, Any]:
    return {"id": i, "role": role, "name": name, "parent": parent, "runtime_id": list(rid) if rid else None,
            "automation_id": aid, "center": center}


def test_ids_follow_the_runtime_id_even_when_an_element_moves_and_new_ones_never_reuse_an_id() -> None:
    before = {0: _el(0, "Window", "W", rid=(1,)), 1: _el(1, "Button", "Send", 0, rid=(2,)),
              2: _el(2, "Group", "Panel", 0, rid=(3,)), 3: _el(3, "Text", "gone", 0, rid=(4,))}
    new = [_el(0, "Window", "W", rid=(1,)), _el(1, "Group", "Panel", 0, rid=(3,)),
           _el(2, "Button", "Send", 1, rid=(2,)),        # moved under the panel
           _el(3, "Text", "fresh", 0, rid=(9,))]
    mapping, top = carry_refs(before, new, ceiling=3)
    assert mapping == {0: 0, 1: 2, 2: 1, 3: 4} and top == 4


def test_two_look_alike_buttons_keep_their_own_ids_by_runtime_id() -> None:
    before = {0: _el(0, "Window", "W", rid=(1,)), 1: _el(1, "Button", "Play", 0, rid=(10,), center=(100, 100)),
              2: _el(2, "Button", "Play", 0, rid=(11,), center=(100, 200))}
    # the list re-sorted: the second Play is now first and both moved
    new = [_el(0, "Window", "W", rid=(1,)), _el(1, "Button", "Play", 0, rid=(11,), center=(100, 100)),
           _el(2, "Button", "Play", 0, rid=(10,), center=(100, 200))]
    mapping, _ = carry_refs(before, new, ceiling=2)
    assert mapping == {0: 0, 1: 2, 2: 1}


def test_a_control_the_app_drew_again_keeps_its_id_by_place() -> None:
    before = {0: _el(0, "Window", "W", rid=(1,)), 1: _el(1, "Button", "Lyrics", 0, rid=(5,))}
    new = [_el(0, "Window", "W", rid=(1,)), _el(1, "Button", "Lyrics", 0, rid=(6,))]   # re-mounted
    assert carry_refs(before, new, ceiling=1)[0] == {0: 0, 1: 1}


def test_the_automation_id_carries_an_element_whose_name_changed() -> None:
    before = {0: _el(0, "Window", "W"), 1: _el(1, "Button", "Play", 0, aid="play-pause")}
    new = [_el(0, "Window", "W"), _el(1, "Button", "Pause", 0, aid="play-pause")]
    assert carry_refs(before, new, ceiling=1)[0] == {0: 0, 1: 1}


def test_the_model_remembers_what_a_gone_id_was() -> None:
    model = WindowModel(5)
    model.carry([_el(0, "Window", "W", rid=(1,)), _el(1, "Button", "Lyrics", 0, rid=(2,))], read=1)
    model.carry([_el(0, "Window", "W", rid=(1,))], read=2)
    assert model.gone(1) == ("Button", "Lyrics", 1) and model.gone(0) is None and 2 in model.lineage


# ---------------------------------------------------------------------------
# (b) what an action changed
# ---------------------------------------------------------------------------


def _carried(model: WindowModel, tree: T.WindowTree, read_no: int) -> list[T.UIElement]:
    return model.carry(list(tree.elements), read_no)


def test_a_lyrics_click_is_described_as_the_toggle_turning_on_and_the_view_opening(
        monkeypatch: pytest.MonkeyPatch) -> None:
    window = player_window(lyrics_on=False)
    model = WindowModel(7001)
    before = _carried(model, read(window, monkeypatch=monkeypatch), 1)
    lyrics_node = next(n for n in window.walk() if n.name == "Lyrics")
    lyrics_node.toggle = 1
    shell = next(n for n in window.walk() if n.role == "Group" and n.invoke)
    view = Node("Group", "Lyrics view", (472, 227, 1212, 880))
    view.add(*[Node("Text", f"invented lyric line {i}", (480, 240 + 40 * i, 1200, 270 + 40 * i)) for i in range(5)])
    shell.add(view)
    after = _carried(model, read(window, monkeypatch=monkeypatch), 2)
    target = next(e.id for e in before if e.name == "Lyrics")
    change = diff(before, after, target=target)
    lines = describe(change, target_label='Button "Lyrics"')
    assert lines[0] == 'Button "Lyrics" turned on'
    assert any(line.startswith('Group "Lyrics view" appeared with 5 element(s) inside') for line in lines)
    assert verdict(change) == (True, 'the window changed: Button "Lyrics" turned on')


def test_the_volume_change_is_said_in_percent() -> None:
    base = dict(center=(0, 0), bounds=(0, 0, 10, 10), states=(), value=None, is_interactive=True)
    before = [{"id": 0, "role": "Window", "name": "W", "parent": -1, "depth": 0, **base},
              {"id": 1, "role": "Slider", "name": "Change volume", "parent": 0, "range": (0.62, 0.0, 1.0), **base}]
    after = [dict(before[0]), {**before[1], "range": (0.47, 0.0, 1.0)}]
    assert describe(diff(before, after, target=1), target_label='Slider "Change volume"') == [
        'Slider "Change volume" went from 62% to 47%']


def test_nothing_visible_changed_is_said_and_is_a_contradicted_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    window = player_window()
    model = WindowModel(7001)
    before = _carried(model, read(window, monkeypatch=monkeypatch), 1)
    after = _carried(model, read(window, monkeypatch=monkeypatch), 2)
    change = diff(before, after, target=before[3].id)
    assert describe(change) == ["nothing visible changed in the window"]
    assert verdict(change) == (False, "nothing visible changed in the window")
    assert verdict(change, opaque=True)[0] is None and verdict(None)[0] is None


def test_a_youtube_scroll_is_seen_as_the_page_content_moving(monkeypatch: pytest.MonkeyPatch) -> None:
    model = WindowModel(7001)
    before = _carried(model, read(feed_window(offset=0), monkeypatch=monkeypatch), 1)
    after = _carried(model, read(feed_window(offset=600), monkeypatch=monkeypatch), 2)
    change = diff(before, after)
    assert change.moved and change.moved[0][1] == -600
    line = next(ln for ln in describe(change) if "moved" in ln)
    assert line.startswith('the content of Document "(12) YouTube" moved up by about 600 px')
    assert "came into view" in line and "left it" in line


def test_what_was_already_changing_before_the_action_is_left_out() -> None:
    def tree(clock: str, lyrics: str) -> list[dict[str, Any]]:
        common = dict(bounds=(0, 0, 10, 10), center=(5, 5), value=None, is_interactive=False)
        return [{"id": 0, "role": "Window", "name": "W", "parent": -1, "depth": 0, "states": (), **common},
                {"id": 1, "role": "Text", "name": clock, "parent": 0, "states": (), **common},
                {"id": 2, "role": "Button", "name": "Lyrics", "parent": 0, "states": (lyrics,), **common}]
    reference, before, after = tree("1:13", "off"), tree("1:14", "off"), tree("1:15", "on")
    change = diff(before, after, target=2, reference=reference)
    assert change.ignored == 1 and describe(change, target_label='Button "Lyrics"') == ['Button "Lyrics" turned on']
    assert diff(before, after, target=2, busy={1}).ignored == 1


# ---------------------------------------------------------------------------
# The dispatcher: every read carries ids, gone ids, the outcome bracket
# ---------------------------------------------------------------------------


class _PlayerDesk:
    """A stub desk over the fake Spotify window: reads go through the real reader; a click on Lyrics flips it and
    opens the lyrics view; a scroll moves the feed; the clock ticks on every read when asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, window: Node | None = None) -> None:
        self.mp = monkeypatch
        self.window = window or player_window()
        self.reads = 0
        self.clicks: list[tuple[int, int]] = []
        self.tick = False
        self.clock = None
        self.parts: list = []

    def get_window_tree(self, hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0,
                        part: tuple | None = None) -> T.WindowTree:
        self.reads += 1
        self.parts.append(part)
        if self.tick and self.clock is not None:
            self.clock.name = f"1:{10 + self.reads:02d}"
        return read(self.window, hwnd=hwnd, monkeypatch=self.mp, max_elements=max_elements, part=part)

    def format_window_tree(self, tree: T.WindowTree, **kw: Any) -> str:
        return T.format_window_tree(tree, **kw)

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, expect_hwnd: int | None = None) -> Any:
        self.clicks.append((x, y))
        for node in self.window.walk():
            b = node.bounds
            if node.name in ("Mute", "Unmute") and b and b[0] <= x < b[2] and b[1] <= y < b[3]:
                node.name = "Unmute" if node.name == "Mute" else "Mute"
                break
            if node.toggle is not None and b and b[0] <= x < b[2] and b[1] <= y < b[3]:
                node.toggle = 1 - node.toggle
                if node.name == "Lyrics" and node.toggle:
                    shell = next(n for n in self.window.walk() if n.role == "Group" and n.invoke)
                    shell.add(Node("Group", "Lyrics view", (472, 227, 1212, 880)).add(
                        Node("Text", "invented lyric", (480, 240, 1200, 270))))
        return ActionResult(ok=True, summary=f"clicked at ({x},{y}); same page", details={
            "focus_changed": False, "foreground_changed": False, "page_changed": False,
            "foreground_title_changed": False})

    def scroll(self, x: int, y: int, *, dy: int = 0, dx: int = 0, expect_hwnd: int | None = None) -> Any:
        # the old single-point check: the renderer pane under the point never moves
        offset = getattr(self, "offset", 0) + (-dy * 60)
        self.offset = offset
        self.window = feed_window(offset=offset)
        return ActionResult(ok=False, summary="sent wheel notches, but nothing under the point moved: it did not "
                                              "scroll", details={"verified": False, "scrolls": [{"moved": False}]})

    def get_desktop_overview(self) -> dict[str, Any]:
        return {"foreground_hwnd": 7001, "windows": []}


def _dispatcher(desk: Any, *, verify: bool = True) -> Any:
    from yuki.agent.tools import Dispatcher

    d = Dispatcher(backend=desk)
    d.verify_outcomes = verify
    d.guard_raw_clicks = False
    return d


def _ids(text: str, name: str) -> int:
    line = next(ln for ln in text.splitlines() if f'"{name}"' in ln and ln.strip().startswith("["))
    return int(line.strip()[1:line.strip().index("]")])


def test_ids_hold_across_explicit_reads_and_a_vanished_element_is_reported_gone(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.tools import ToolError

    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk, verify=False)
    first = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    lyrics = _ids(first, "Lyrics")
    queue = _ids(first, "Queue")
    # the bar is rebuilt in another order between reads: ids still follow the elements
    bar = next(n for n in desk.window.walk() if n.name == "Now playing bar")
    bar.children.reverse()
    second = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    assert _ids(second, "Lyrics") == lyrics and _ids(second, "Queue") == queue
    assert d._element_target({"element": lyrics, "read": 1})[3].startswith(f'[{lyrics}] Button "Lyrics"')
    bar.children = [c for c in bar.children if c.name != "Queue"]
    d.dispatch("look_at_window", {"hwnd": 7001})
    with pytest.raises(ToolError, match=r'\(Button "Queue", seen in read #2\) is not in window 7001 now'):
        d._element_target({"element": queue})


def test_look_at_window_within_lists_one_part_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk, verify=False)
    first = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    library = _ids(first, "Your Library")
    part = d.dispatch("look_at_window", {"hwnd": 7001, "within": library}).content[0]["text"]
    assert "Playlist 6" in part and "Lyrics" not in part and f"only [{library}]" in part


def test_a_click_result_says_what_changed_and_the_verdict_follows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    lyrics = _ids(text, "Lyrics")
    out = d.dispatch("click", {"element": lyrics, "expect_hwnd": 7001, "toggle_to": "on"})
    # review F3: a toggle keeps its live read-back (vfix2 N4) and is bracketed too: its side effects are said
    assert out.ok and out.payload["details"]["state_after"] == "on" and out.payload["details"]["verified"] is True
    said = out.content[0]["text"]
    assert "read back: now on" in said
    assert 'What changed: Button "Lyrics" turned on; Group "Lyrics view" appeared with 1 element(s) inside' in said
    assert out.payload["details"]["screen_change"]["verified"] is True
    mute = _ids(text, "Mute")
    out = d.dispatch("click", {"element": mute, "expect_hwnd": 7001})
    assert 'What changed: Button "Mute" is now called "Unmute".' in out.content[0]["text"]
    details = out.payload["details"]
    assert details["screen_change"]["verified"] is True and details["verified"] is True
    assert action_verdict("click", out)[1].startswith("the window changed: ")


def test_a_click_that_changes_nothing_is_not_verified_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
    assert "What changed: nothing visible changed in the window." in out.content[0]["text"]
    assert action_verdict("click", out) == (False, "nothing visible changed in the window")


def test_a_wheel_scroll_the_point_called_failed_is_a_scroll_when_the_page_moved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _PlayerDesk(monkeypatch, window=feed_window(offset=0))
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("scroll", {"x": 960, "y": 600, "dy": -10, "expect_hwnd": 7001})
    assert out.ok and "but the window's content did move" in out.summary
    assert "moved up by about 600 px" in out.content[0]["text"]
    assert action_verdict("scroll", out)[0] is True


def test_the_ticking_clock_is_not_reported_as_the_actions_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    window = player_window()
    clock = Node("Text", "1:10", (549, 926, 570, 940))
    next(n for n in window.walk() if n.name == "Player controls").add(clock)
    desk = _PlayerDesk(monkeypatch, window=window)
    desk.clock, desk.tick = clock, True
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
    # before the click a fresh read showed the clock changing since the thinker's read: left out of the account
    assert "nothing visible changed in the window" in out.content[0]["text"]
    assert out.payload["details"]["screen_change"]["counts"]["ignored"] == 1


def test_without_outcome_checks_a_stub_desk_keeps_its_old_results(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk, verify=False)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    reads = desk.reads
    out = d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
    assert "What changed" not in out.content[0]["text"] and desk.reads == reads


def test_a_hidden_slider_is_not_clicked_but_pointed_to_the_slider_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _PlayerDesk(monkeypatch)
    d = _dispatcher(desk, verify=False)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    slider = next(int(ln.strip()[1:ln.strip().index("]")]) for ln in text.splitlines()
                  if "Slider" in ln and "Change volume" in ln)
    out = d.dispatch("click", {"element": slider, "expect_hwnd": 7001})
    assert not out.ok and "set_slider" in out.summary and desk.clicks == []


class _TruncatingDesk(_PlayerDesk):
    """The second read of the settle loop is cut short by the clock (3 Oct: "these 189 elements ...")."""

    def get_window_tree(self, hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0) -> T.WindowTree:
        tree = super().get_window_tree(hwnd, max_elements=max_elements, timeout_s=timeout_s)
        if self.reads >= 2:
            tree.elements = tree.elements[:5]
            tree.truncated = True
        return tree


def test_a_reread_cut_short_never_replaces_a_whole_read_in_the_view_after_actions(
        monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _TruncatingDesk(monkeypatch)
    d = _dispatcher(desk, verify=False)
    view = d.window_view(7001, settle=True, quiet_s=0.05, cap_s=0.4)
    assert "cut short" in view.reason and "Lyrics" in view.text and view.reads == 2


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self) -> None:
        self.sinks: dict[int, Any] = {}
        self.unsubscribed: list[int] = []

    def subscribe(self, hwnd: int, sink: Any) -> Any:
        self.sinks[hwnd] = sink
        return hwnd

    def unsubscribe(self, token: Any) -> None:
        self.unsubscribed.append(token)
        self.sinks.pop(token, None)


def test_the_event_hub_counts_waits_quietly_and_lets_idle_windows_go() -> None:
    from yuki.perception.events import EventHub

    source = _FakeSource()
    hub = EventHub(lambda: source, idle_s=0.3)
    try:
        assert hub.watch(11, wait_s=1.0) and hub.generation(11) == 0
        source.sinks[11]("property", 30086, (42, 7))
        source.sinks[11]("structure", 0, None)
        assert hub.generation(11) == 2 and hub.busy_since(11, 0.0) == {(42, 7)}
        assert hub.wait_change(11, 1, 0.5) and not hub.wait_change(11, 2, 0.05)
        quiet, waited = hub.wait_quiet(11, quiet_s=0.05, cap_s=1.0)
        assert quiet and waited < 0.5

        def ticking() -> None:
            for _ in range(20):
                source.sinks.get(11, lambda *a: None)("property", 30005, None)
                time.sleep(0.02)

        thread = threading.Thread(target=ticking)
        thread.start()
        quiet, waited = hub.wait_quiet(11, quiet_s=0.1, cap_s=0.2)
        thread.join()
        assert not quiet and waited >= 0.19
        deadline = time.monotonic() + 3.0
        while 11 not in source.unsubscribed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert source.unsubscribed == [11] and hub.generation(11) is None
    finally:
        hub.close()


def test_an_event_source_that_fails_leaves_reads_to_stand_in() -> None:
    from yuki.perception.events import EventHub

    def broken() -> Any:
        raise OSError("no UI Automation here")

    hub = EventHub(broken)
    try:
        assert not hub.watch(3, wait_s=0.5)
        assert hub.generation(3) is None
    finally:
        hub.close()


def test_with_events_the_read_before_an_action_is_skipped_while_nothing_happened(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.perception.events import EventHub

    source = _FakeSource()
    hub = EventHub(lambda: source)
    try:
        desk = _PlayerDesk(monkeypatch)
        d = _dispatcher(desk)
        d.events = hub
        hub.watch(7001, wait_s=1.0)
        text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
        reads = desk.reads
        d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
        assert desk.reads == reads + 1        # only the read after: the window's events said nothing changed
        source.sinks[7001]("structure", 0, None)
        reads = desk.reads
        d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
        assert desk.reads == reads + 2        # an event since the last read: read again before acting
    finally:
        hub.close()


def test_the_com_event_handlers_forward_to_their_sink_through_the_interface() -> None:
    from yuki.perception.events import handler_classes

    module = T._uia_core()
    structure_cls, property_cls, automation_cls = handler_classes(module)
    got: list[tuple] = []
    handler = structure_cls(lambda *a: got.append(a))
    pointer = handler.QueryInterface(module.IUIAutomationStructureChangedEventHandler)
    pointer.HandleStructureChangedEvent(None, 1, [42, 7])
    auto = automation_cls(lambda *a: got.append(a)).QueryInterface(module.IUIAutomationEventHandler)
    auto.HandleAutomationEvent(None, 20003)
    assert got == [("structure", 1, None), ("event", 20003, None)]


# ---------------------------------------------------------------------------
# Finding an element again on Chromium, the scroll surface, memory's snapshot
# ---------------------------------------------------------------------------


class _Live:
    def __init__(self, role: str, name: str, rid: tuple, children: list["_Live"] | None = None,
                 native: int = 0) -> None:
        self.role, self.name, self.rid, self.children, self.native = role, name, rid, children or [], native

    def GetCachedPropertyValue(self, pid: int) -> Any:
        return {30003: {"Pane": 50033, "Button": 50000}[self.role], 30005: self.name, 30020: self.native}.get(pid)

    @property
    def CachedBoundingRectangle(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(left=0, top=0, right=10, bottom=10)

    def FindFirstBuildCache(self, scope: int, condition: Any, request: Any) -> Any:
        want = tuple(condition[2])
        stack = list(self.children)
        while stack:
            node = stack.pop()
            if node.rid == want:
                return node
            stack.extend(node.children)
        return None


def test_a_web_control_is_found_again_by_its_runtime_id_below_the_renderer_pane() -> None:
    from yuki.actions.patterns import by_runtime_id

    button = _Live("Button", "Lyrics", (42, 9))
    pane = _Live("Pane", "", (42, 1), [_Live("Pane", "", (42, 2), [button])])

    class _Automation:
        def CreatePropertyCondition(self, pid: int, value: Any) -> tuple:
            return ("prop", pid, value)

        def ElementFromHandleBuildCache(self, hwnd: int, request: Any) -> Any:
            return pane

    element = {"role": "Button", "name": "Lyrics", "runtime_id": [42, 9]}
    assert by_runtime_id(_Automation(), element, pane, 0, None) is button
    assert by_runtime_id(_Automation(), {**element, "role": "Slider"}, pane, 0, None) is None   # role must match
    assert by_runtime_id(_Automation(), {**element, "runtime_id": None}, pane, 0, None) is None


def test_memory_snapshot_from_the_same_read_keeps_hidden_controls_out_of_text(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.memory.extract.uia import snapshot_from_tree

    snap = snapshot_from_tree(read(player_window(), monkeypatch=monkeypatch))
    hidden = [n for n in snap.root.walk() if n.sr_only]
    assert {n.name for n in hidden} == {"Change volume", "Change progress"}
    assert all(n.runtime_id for n in hidden)


# ---------------------------------------------------------------------------
# (e) cost on the recorded sizes
# ---------------------------------------------------------------------------


def test_format_carry_and_diff_of_a_thousand_element_read_cost_little() -> None:
    tree = _long_list_tree(1000)
    model = WindowModel(1)
    started = time.perf_counter()
    first = model.carry(list(tree.elements), 1)
    second = model.carry(list(tree.elements), 2)
    change = diff(first, second)
    T.format_window_tree(tree)
    elapsed = time.perf_counter() - started
    assert not change.any and elapsed < 1.0


def test_a_read_stopped_by_its_cap_does_not_make_the_rest_gone() -> None:
    model = WindowModel(5)
    model.carry([_el(0, "Window", "W", rid=(1,)), _el(1, "Button", "Far", 0, rid=(2,))], read=1)
    model.carry([_el(0, "Window", "W", rid=(1,))], read=2, partial=True)
    assert model.gone(1) is None and 1 in model.latest


def test_the_view_after_actions_reuses_the_after_read_while_the_events_show_no_change(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.perception.events import EventHub

    source = _FakeSource()
    hub = EventHub(lambda: source)
    try:
        desk = _PlayerDesk(monkeypatch)
        d = _dispatcher(desk)
        d.events = hub
        hub.watch(7001, wait_s=1.0)
        text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
        d.dispatch("click", {"element": _ids(text, "Mute"), "expect_hwnd": 7001})
        reads = desk.reads
        view = d.window_view(7001, settle=True, changes=True, near=_ids(text, "Mute"))
        assert desk.reads == reads and view.reads == 0 and view.settled
        assert '"Unmute"' in view.text   # the change since the thinker's read, from the reused read
        source.sinks[7001]("property", 30005, None)
        reads = desk.reads
        d.window_view(7001, settle=False)
        assert desk.reads == reads + 1    # an event since: read again
    finally:
        hub.close()


class _CutShortDesk(_PlayerDesk):
    def get_window_tree(self, hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0) -> T.WindowTree:
        tree = super().get_window_tree(hwnd, max_elements=max_elements, timeout_s=timeout_s)
        if self.reads >= 2:
            tree.elements = tree.elements[:4]
            tree.truncated = True
        return tree


def test_an_after_read_cut_short_leaves_the_actions_own_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = _CutShortDesk(monkeypatch)
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
    assert "What changed" not in out.content[0]["text"]
    assert out.payload["details"].get("screen_change") is None or out.payload["details"]["screen_change"][
        "verified"] is None


class _ChatDesk(_PlayerDesk):
    """A Discord-like DM: Enter after typing sends the message, which shows at the bottom as the list scrolls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rids: dict = {}
        self.count = 60
        super().__init__(monkeypatch, window=chat_window(messages=60, visible_from=40, rids=self.rids))

    def type_text(self, text: str, *, press_enter: bool = False, expect_hwnd: int | None = None, **kw: Any) -> Any:
        if press_enter:
            self.count += 1
            self.window = chat_window(messages=self.count, visible_from=self.count - 20, rids=self.rids)
        return ActionResult(ok=True, summary=f"typed {len(text)} chars", details={"readback": "unreadable"})


def test_enter_after_typing_is_judged_by_the_chat_moving_to_show_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _ChatDesk(monkeypatch)
    d = _dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": 7001})
    out = d.dispatch("type_text", {"text": "invented reply", "press_enter": True, "expect_hwnd": 7001})
    assert 'the content of List "Messages in @Sam" moved up by about 40 px' in out.content[0]["text"]
    assert action_verdict("type_text", out)[0] is True
    # typing without Enter keeps the field's own read-back, no extra reads
    reads = desk.reads
    d.dispatch("type_text", {"text": "draft", "expect_hwnd": 7001})
    assert desk.reads == reads


def test_left_out_notes_follow_the_carried_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    window = chat_window(messages=60, visible_from=40, doc_scrolls=True)
    desk = _PlayerDesk(monkeypatch, window=window)
    d = _dispatcher(desk, verify=False)
    first = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    chat = _ids(first, "Messages in @Sam")
    # something new above the chat shifts every position in the next read
    doc = next(n for n in window.walk() if n.role == "Document")
    doc.children.insert(0, Node("Button", "Invented banner", (500, 150, 900, 190), invoke=True))
    second = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    assert _ids(second, "Messages in @Sam") == chat
    lines = second.splitlines()
    at = next(i for i, ln in enumerate(lines) if '"Messages in @Sam"' in ln)
    assert "off screen: 40 above" in lines[at + 1]


class _DialogDesk(_PlayerDesk):
    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, expect_hwnd: int | None = None) -> Any:
        return ActionResult(ok=True, summary="clicked; a new window of the same process appeared",
                            details={"new_windows": [{"hwnd": 99, "title": "Invented dialog"}], "focus_changed": False})


def test_an_effect_outside_the_window_is_never_called_nothing_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.loop import action_verdict

    desk = _DialogDesk(monkeypatch)
    d = _dispatcher(desk)
    text = d.dispatch("look_at_window", {"hwnd": 7001}).content[0]["text"]
    out = d.dispatch("click", {"element": _ids(text, "Home"), "expect_hwnd": 7001})
    assert "nothing changed inside this window itself" in out.content[0]["text"]
    assert action_verdict("click", out)[0] is True


def test_an_empty_content_window_is_asked_once_as_an_accessibility_client_would(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class _Oleacc:
        def AccessibleObjectFromWindow(self, hwnd: Any, objid: Any, iid: Any, out: Any) -> int:
            calls.append(int(hwnd.value))
            return 0

    monkeypatch.setattr(T, "ctypes_oleacc", lambda: _Oleacc())
    monkeypatch.setattr(T, "_window_rect", lambda h: {901: (0, 0, 1000, 800), 902: (0, 0, 20, 20)}.get(h))
    monkeypatch.setattr(T, "_woken", {})
    view = (0, 0, 1000, 800)
    assert T._wake_accessibility([901, 902], view) == 1      # the small child is not a content window
    assert T._wake_accessibility([901, 902], view) == 0      # asked once per ten minutes
    assert calls == [901]


def test_the_renderer_pane_under_a_scroll_point_is_no_marker_and_the_page_below_it_is_found(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import win32gui

    from yuki.actions import scrolling

    monkeypatch.setattr(win32gui, "GetWindowRect", lambda h: (0, 0, 1000, 1000))
    page = SimpleNamespace(CurrentControlType=50030)

    class _Pane:
        CurrentBoundingRectangle = SimpleNamespace(left=0, top=0, right=1000, bottom=980)
        CurrentControlType = 50033

        def FindFirst(self, scope: int, condition: Any) -> Any:
            return page if condition == ("prop", 30003, 50030) else None

    class _Automation:
        def CreatePropertyCondition(self, pid: int, value: Any) -> tuple:
            return ("prop", pid, value)

    pane = _Pane()
    assert scrolling.window_sized(pane, 5)
    small = SimpleNamespace(CurrentBoundingRectangle=SimpleNamespace(left=10, top=10, right=200, bottom=60))
    assert not scrolling.window_sized(small, 5)
    assert scrolling.page_below(_Automation(), pane) is page


def test_a_new_page_and_a_retitled_page_are_said_apart() -> None:
    def read_of(title: str, url: str) -> list[dict[str, Any]]:
        common = dict(bounds=(0, 0, 10, 10), center=(5, 5), states=(), is_interactive=False)
        return [{"id": 0, "role": "Window", "name": "Arc", "parent": -1, "depth": 0, "value": None, **common},
                {"id": 1, "role": "Document", "name": title, "parent": 0, "depth": 1, "value": url, **common}]
    moved = diff(read_of("Home", "https://www.youtube.com/"), read_of("Clip", "https://www.youtube.com/watch?v=x"))
    assert describe(moved)[0] == "the page is now https://www.youtube.com/watch?v=x (was https://www.youtube.com/)"
    retitled = diff(read_of("(1) Home", "https://www.youtube.com/"), read_of("(2) Home", "https://www.youtube.com/"))
    assert retitled.page is None and describe(retitled)[0] == 'the window\'s title is now "(2) Home"'
