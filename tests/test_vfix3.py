"""vfix3 (4 Oct, report 53): Spotify's in-app volume, the judge's false holds, and "my Japanese playlist".

Live 4 Oct (02:22-02:26): set_slider failed 4 of 4 times on Spotify's volume ("no element at its point matches the
read": UI Automation's point lookup answers a Chromium/CEF window with its whole renderer pane), a coordinate click
landed on Mute, a guessed shortcut did nothing, and the Windows mixer was lowered instead of the player's own volume;
the judge held "Search in Your Library" as not asked for; the thinker asked which playlist "my Japanese playlist" was
while memory held the answer.

Offline only: fakes, stub desks and tree SHAPES copied from the live read (roles, boxes, patterns, the label that hides
the slider); every song, playlist and person name here is invented. No microphone, speaker, live desktop or real memory
(ARCHITECTURE.md rule 7).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yuki.voice.offline import ChatPlayerDesk

PLAYER = ChatPlayerDesk.PLAYER


@pytest.fixture(autouse=True)
def _no_live_now(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# (a) The slider hand on the live shape
# ---------------------------------------------------------------------------

#: The player bar of the 4 Oct read (element ids 28-52 of read #4): role, name, box, parent, description. The song and
#: playlist names are invented; the boxes, roles and nesting are as UI Automation gave them.
LIVE_BAR = [
    ("Group", "Now playing bar", (147, 948, 1537, 1003), None, ""),
    ("Group", "Now playing: Paper Moon by Ayane Kito", (153, 954, 565, 997), "Now playing bar", ""),
    ("Group", "Player controls", (567, 953, 1117, 998), "Now playing bar", ""),
    ("Button", "Previous", (793, 953, 818, 979), "Player controls", ""),
    ("Button", "Play", (829, 953, 855, 979), "Player controls", ""),
    ("Text", "1:05", (585, 984, 606, 998), "Player controls", ""),
    ("Text", "Change progress", (610, 985, 660, 1020), "Player controls", ""),
    ("Button", "Queue", (1338, 963, 1363, 988), "Now playing bar", ""),
    ("Button", "Connect to a device", (1362, 963, 1388, 988), "Now playing bar", ""),
    ("Button", "Mute", (1387, 963, 1412, 988), "Now playing bar", "Volume medium"),
    ("Text", "Change volume", (1410, 970, 1454, 1005), "Now playing bar", ""),
    ("Button", "Open Miniplayer", (1488, 963, 1513, 988), "Now playing bar", ""),
]
#: The element the thinker named, as the read gave it: a Text, no patterns.
CHANGE_VOLUME = {"id": 50, "role": "Text", "name": "Change volume", "center": [1432, 987],
                 "bounds": [1410, 970, 1454, 1005], "patterns": [], "is_interactive": False}
MUTE = {"id": 49, "role": "Button", "name": "Mute", "center": [1399, 975], "bounds": [1387, 963, 1412, 988],
        "patterns": ["invoke"]}


class FakeSlider:
    """A RangeValue element: ``takes`` (SetValue moves it), ``resets`` (its app puts the old value back a moment
    later), arrow keys after focus move it by ``step``."""

    def __init__(self, value: float, low: float = 0.0, high: float = 1.0, step: float = 0.1, *, takes: bool = True,
                 resets: bool = False) -> None:
        self.value, self.low, self.high, self.step = value, low, high, step
        self.takes, self.resets = takes, resets
        self.focused = False
        self.sets: list[float] = []
        self._pending_reset: float | None = None

    def read(self) -> tuple[float, float, float, bool, float]:
        if self._pending_reset is not None:   # the second read after a set: the app has put it back
            value, self._pending_reset = self.value, None
            self.value = self._reset_to
            return value, self.low, self.high, False, self.step
        return self.value, self.low, self.high, False, self.step

    def set(self, value: float) -> None:
        self.sets.append(value)
        if self.takes:
            if self.resets:
                self._reset_to, self._pending_reset = self.value, value
            self.value = max(self.low, min(self.high, value))

    def focus(self) -> None:
        self.focused = True

    def press(self, key: str, times: int) -> None:
        if self.focused:
            sign = 1 if key in ("right", "up") else -1
            self.value = round(max(self.low, min(self.high, self.value + sign * self.step * times)), 4)


class FakeTree:
    """A window's live tree as the slider hand searches it, from rows like :data:`LIVE_BAR` plus hidden sliders
    (``sliders``: name -> (FakeSlider, box or None, parent name)). ``hidden_until_hover``: the bar's rows and sliders
    exist only after a hover."""

    def __init__(self, rows: list[tuple], sliders: dict[str, tuple[FakeSlider, Any, str]], *,
                 hidden_until_hover: bool = False) -> None:
        from yuki.actions.slider import Node

        self.Node = Node
        self.rows = rows
        self.sliders = sliders
        self.hidden = hidden_until_hover
        self.hovers: list[tuple[int, int]] = []
        self.descriptions = {r[1]: r[4] for r in rows}
        self.names: dict[str, str] = {}   # what a row shows now, when it changed (a ticking clock)

    def hover(self, x: int, y: int) -> None:
        self.hovers.append((x, y))
        self.hidden = False

    def _parent_of(self, name: str) -> str | None:
        for r in self.rows:
            if r[1] == name:
                return r[3]
        return self.sliders[name][2] if name in self.sliders else None

    def _under(self, scope: str | None, name: str) -> bool:
        p = self._parent_of(name)
        while p is not None:
            if p == scope:
                return True
            p = self._parent_of(p)
        return False

    def ranges(self, scope: Any = None) -> list[Any]:
        if self.hidden:
            return []
        return [self.Node("Slider", n, box, n) for n, (_s, box, _p) in self.sliders.items()
                if scope is None or self._under(scope.ref, n)]

    def named(self, role: str, name: str) -> list[Any]:
        if self.hidden:
            return []
        return [self.Node(r[0], r[1], r[2], r[1]) for r in self.rows if r[0] == role and r[1] == name]

    def parent(self, node: Any) -> Any:
        p = self._parent_of(node.ref)
        row = next((r for r in self.rows if r[1] == p), None)
        return self.Node(row[0], row[1], row[2], row[1]) if row else None

    def slider(self, node: Any) -> FakeSlider:
        return self.sliders[node.ref][0]

    def shown(self, node: Any) -> list[tuple[str, Any]]:
        return [(f"{r[0]} \"{self.names.get(r[1], r[1])}\""
                 + (f": {self.descriptions[r[1]]}" if self.descriptions.get(r[1]) else ""), r[2])
                for r in self.rows if self._under(node.ref, r[1])]


def live_tree(volume: float = 0.5, **kw: Any) -> tuple[FakeTree, FakeSlider]:
    """The 4 Oct shape: both sliders hidden next to their labels (siblings, not children), named like them, with
    empty boxes (an offscreen web input)."""
    vol = FakeSlider(volume, **kw)
    progress = FakeSlider(65.0, 0.0, 219.0, 5.0)
    return FakeTree(LIVE_BAR, {"Change progress": (progress, None, "Player controls"),
                               "Change volume": (vol, None, "Now playing bar")}), vol


def test_a_the_live_shape_finds_spotifys_hidden_volume_slider_by_its_label_and_sets_it() -> None:
    from yuki.actions.slider import choose, run

    tree, vol = live_tree(0.5)
    node, how, _ = choose(tree, CHANGE_VOLUME)
    assert node is not None and node.name == "Change volume" and how == "named"   # never "Change progress"
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-15, press=vol.press, sleep=lambda _s: None, settle_s=0.0)
    assert ok and details["verified"] and details["via"] == "RangeValue" and vol.value == pytest.approx(0.35)
    assert "50% -> 35%" in summary and "read back" in summary and "the mouse was not used" in summary


def test_a_the_mute_button_next_to_it_reaches_the_volume_slider_by_place() -> None:
    from yuki.actions.slider import choose

    tree, _vol = live_tree()
    tree.sliders["Change volume"] = (tree.sliders["Change volume"][0], (1431, 986, 1432, 987), "Now playing bar")
    node, how, _ = choose(tree, MUTE)
    assert node is not None and node.name == "Change volume" and how == "near"


def test_a_two_sliders_it_cannot_tell_apart_are_named_back_never_guessed() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree()
    bar = {"role": "Group", "name": "Now playing bar", "center": [842, 975], "bounds": [147, 948, 1537, 1003]}
    ok, summary, details = run(tree, bar, to=20, press=vol.press, sleep=lambda _s: None)
    assert not ok and details["found"] == "ambiguous" and vol.sets == []
    assert "Change volume" in summary and "Change progress" in summary and "nothing was changed" in summary


def test_a_a_bar_hidden_until_the_pointer_is_over_it_is_shown_once_by_hover_no_click() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree(0.6)
    tree.hidden = True
    ok, summary, details = run(tree, CHANGE_VOLUME, to=30, hover=tree.hover, press=vol.press, sleep=lambda _s: None,
                               settle_s=0.0)
    assert ok and details["hovered"] and tree.hovers == [(1432, 987)] and vol.value == pytest.approx(0.3)
    assert "the pointer was moved over the bar to show it" in summary


def test_a_nothing_exposing_the_level_is_said_plainly_and_never_falls_back_to_the_mixer() -> None:
    from yuki.actions.slider import run

    tree = FakeTree(LIVE_BAR, {})
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-15, hover=tree.hover, sleep=lambda _s: None)
    assert not ok and not details["verified"] and details["hovered"] and len(tree.hovers) == 1
    assert "nothing in this window exposes a slider's level" in summary
    assert "Nothing was dragged or clicked" in summary and "never change the mixer instead" in summary


def test_a_a_value_the_app_puts_back_is_caught_and_the_arrow_keys_finish_it() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree(0.5, resets=True)
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-20, press=vol.press, sleep=lambda _s: None, settle_s=0.0)
    assert details.get("reverted") and details["via"] == "keys" and vol.focused
    assert ok and vol.value == pytest.approx(0.3) and "its own arrow keys" in summary


def test_a_a_little_on_a_stepped_slider_is_one_step_not_a_noop() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree(0.5)
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-5, press=vol.press, sleep=lambda _s: None, settle_s=0.0)
    assert ok and not details.get("noop") and vol.value == pytest.approx(0.4)


# -- a shortcut is tried with the level read back, and learned only from a verified success --------------------------


def test_a_a_shortcut_that_moves_the_level_is_verified_by_reading_it_back() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree(0.5)
    pressed: list[list[str]] = []

    def hotkey(keys: list[str]) -> None:          # the stand-in app's own shortcut: one step down
        pressed.append(keys)
        vol.value = round(vol.value - 0.1, 4)

    ok, summary, details = run(tree, CHANGE_VOLUME, by=-20, keys=["ctrl", "down"], hotkey=hotkey,
                               press=vol.press, sleep=lambda _s: None)
    assert ok and details["via"] == "shortcut" and details["verified"] and details["presses"] == 2
    assert vol.sets == [] and pressed == [["ctrl", "down"], ["ctrl", "down"]] and vol.value == pytest.approx(0.3)
    assert "pressed the app's own shortcut ctrl+down 2 times" in summary and "50% -> 30%" in summary


def test_a_the_guessed_shortcut_of_4_oct_did_nothing_and_that_is_said_then_the_value_does_it() -> None:
    from yuki.actions.slider import run

    tree, vol = live_tree(0.5)
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-15, keys=["ctrl", "shift", "down"], hotkey=lambda k: None,
                               press=vol.press, sleep=lambda _s: None, settle_s=0.0)
    assert details["shortcut"]["moved"] is False and details["via"] == "RangeValue"
    assert summary.startswith("pressed ctrl+shift+down: Slider \"Change volume\" did not change (50%)")
    assert ok and vol.value == pytest.approx(0.35)


def test_a_a_shortcut_that_goes_the_other_way_stops_at_once() -> None:
    from yuki.actions.slider import shortcut

    vol = FakeSlider(0.5)

    def up(keys: list[str]) -> None:
        vol.value = round(vol.value + 0.1, 4)

    ok, summary, details = shortcut(vol, ["ctrl", "up"], up, by=-20, sleep=lambda _s: None)
    assert not ok and details["wrong_way"] and details["presses"] == 1 and "the other way" in summary


def test_a_a_shortcut_with_no_value_to_read_is_checked_against_what_the_bar_shows() -> None:
    from yuki.actions.slider import run

    rows = [list(r) for r in LIVE_BAR]
    tree = FakeTree([tuple(r) for r in rows], {})

    def hotkey(keys: list[str]) -> None:
        tree.descriptions["Mute"] = "Volume low"

    ok, summary, details = run(tree, CHANGE_VOLUME, by=-20, keys=["ctrl", "down"], hotkey=hotkey,
                               sleep=lambda _s: None)
    assert ok and details["level_unread"] and details["verified"]
    assert 'Button "Mute": Volume medium -> Button "Mute": Volume low' in summary
    tree.descriptions["Mute"] = "Volume low"
    nothing = run(tree, CHANGE_VOLUME, by=-20, keys=["ctrl", "down"], hotkey=lambda k: None, sleep=lambda _s: None)
    assert not nothing[0] and "did nothing visible here" in nothing[1]


def test_a_review_f2_a_ticking_clock_on_the_bar_is_never_taken_for_the_shortcut_working() -> None:
    """Review F2: the label's parent is the whole player bar, which holds the playback clock. A shortcut that did
    nothing came back verified because the clock ticked. Only elements next to the label count, and any that change
    with no key pressed are left out."""
    from yuki.actions.slider import run

    tree = FakeTree(list(LIVE_BAR), {})
    tick = iter(range(65, 400))

    def ticking(node: Any) -> list[tuple[str, Any]]:     # every look, the clock has moved on a second
        tree.names["1:05"] = f"{(s := next(tick)) // 60}:{s % 60:02d}"
        return FakeTree.shown(tree, node)

    tree.shown = ticking   # type: ignore[method-assign]
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-20, keys=["ctrl", "shift", "down"], hotkey=lambda k: None,
                               sleep=lambda _s: None)
    assert not ok and not details["verified"] and "did nothing visible here" in summary

    # a compact bar with the clock right under the label: it changes by itself between the two looks before the
    # press, so it is left out of the comparison
    near_clock = [r for r in LIVE_BAR if r[1] != "1:05"] + [("Text", "1:05", (1420, 1006, 1441, 1020), "Now playing bar",
                                                            "")]
    tree2 = FakeTree(near_clock, {})
    tick2 = iter(range(65, 400))

    def ticking2(node: Any) -> list[tuple[str, Any]]:
        tree2.names["1:05"] = f"{(s := next(tick2)) // 60}:{s % 60:02d}"
        return FakeTree.shown(tree2, node)

    tree2.shown = ticking2   # type: ignore[method-assign]
    ok, summary, details = run(tree2, CHANGE_VOLUME, by=-20, keys=["ctrl", "shift", "down"], hotkey=lambda k: None,
                               sleep=lambda _s: None)
    assert not ok and details["ignored"] == 1 and "did nothing visible here" in summary

    def works(keys: list[str]) -> None:
        tree2.descriptions["Mute"] = "Volume low"

    ok, summary, details = run(tree2, CHANGE_VOLUME, by=-20, keys=["ctrl", "down"], hotkey=works,
                               sleep=lambda _s: None)
    assert ok and details["level_unread"] and 'Volume medium -> Button "Mute": Volume low' in summary
    assert "1:0" not in summary


def test_a_review_f2_an_unread_level_never_gets_a_percent_or_turned_down_finish_line() -> None:
    from yuki.agent.finish import noop_line

    line = noop_line("set_slider", {"level_unread": True, "verified": True, "moved": True}, process="Spotify.exe")
    assert line == "I pressed Spotify's own shortcut for it and the bar changed, but I can't read the new level."
    assert noop_line("set_slider", {"verified": True, "after_percent": 30}, process="Spotify.exe") is None


def test_a_review_f5_nothing_is_set_once_the_caller_has_stopped_waiting() -> None:
    """Review F5: the caller said "nothing was dragged or clicked" after its budget; the late job thread then set the
    value anyway. Once stopped, the hand finds but never acts."""
    from yuki.actions.slider import drive, run

    tree, vol = live_tree(0.5)
    acted: list[bool] = []
    ok, summary, details = run(tree, CHANGE_VOLUME, by=-20, press=vol.press, stopped=lambda: True,
                               acting=lambda: acted.append(True), sleep=lambda _s: None, settle_s=0.0)
    assert not ok and details["stopped"] and vol.sets == [] and vol.value == 0.5 and acted == []
    assert "ran out" in summary
    ok, _summary, details = drive(vol, by=-20, press=vol.press, stopped=lambda: True, sleep=lambda _s: None)
    assert not ok and details["stopped"] and vol.sets == []


# -- the real UI Automation adapter, on a fake COM tree: searched from the window, never by point --------------------


class _Cond:
    def __init__(self, test: Any) -> None:
        self.test = test


class _El:
    def __init__(self, ct: int, name: str, box: tuple[int, int, int, int], parent: "_El | None" = None, *,
                 rng: FakeSlider | None = None, desc: str = "") -> None:
        self.ct, self.name, self.box, self.parent, self.rng, self.desc = ct, name, box, parent, rng, desc
        self.children: list[_El] = []
        if parent is not None:
            parent.children.append(self)

    @property
    def CachedBoundingRectangle(self) -> Any:  # noqa: N802
        return SimpleNamespace(left=self.box[0], top=self.box[1], right=self.box[2], bottom=self.box[3])

    def GetCachedPropertyValue(self, prop: int) -> Any:  # noqa: N802
        return {30003: self.ct, 30005: self.name, 30020: 0, 30033: self.rng is not None, 30159: self.desc}.get(prop, "")

    def walk(self) -> list["_El"]:
        out = [self]
        for c in self.children:
            out += c.walk()
        return out

    def FindAllBuildCache(self, scope: int, cond: _Cond, _req: Any) -> Any:  # noqa: N802
        found = [e for e in self.walk() if cond.test(e)]
        return SimpleNamespace(Length=len(found), GetElement=lambda i: found[i])

    def GetCurrentPattern(self, _pid: int) -> Any:  # noqa: N802
        rng = self.rng
        pattern = SimpleNamespace(CurrentValue=0.0, CurrentMinimum=rng.low, CurrentMaximum=rng.high,
                                  CurrentIsReadOnly=False, CurrentSmallChange=rng.step)

        class _P:
            @property
            def CurrentValue(self) -> float:  # noqa: N802
                return rng.value

            CurrentMinimum, CurrentMaximum = pattern.CurrentMinimum, pattern.CurrentMaximum
            CurrentIsReadOnly, CurrentSmallChange = False, rng.step

            def SetValue(self, v: float) -> None:  # noqa: N802
                rng.set(v)

        return SimpleNamespace(QueryInterface=lambda _iface: _P())

    def SetFocus(self) -> None:  # noqa: N802
        self.rng.focus()


class _Automation:
    """Just enough of IUIAutomation for :class:`yuki.actions.slider._UiaTree`. ``ElementFromPoint`` answers with the
    renderer pane, as a CEF window did live (it is never asked)."""

    def __init__(self, root: _El) -> None:
        self.root = root
        self.ControlViewWalker = SimpleNamespace(GetParentElementBuildCache=lambda el, _r: el.parent)
        self.point_asked = 0

    def CreateCacheRequest(self) -> Any:  # noqa: N802
        return SimpleNamespace(AddProperty=lambda p: None, TreeScope=0)

    def ElementFromHandleBuildCache(self, hwnd: int, _req: Any) -> _El:  # noqa: N802
        return self.root

    def ElementFromPointBuildCache(self, *_a: Any) -> _El:  # noqa: N802
        self.point_asked += 1
        return self.root.children[0]

    def CreatePropertyCondition(self, prop: int, value: Any) -> _Cond:  # noqa: N802
        return _Cond(lambda e: e.GetCachedPropertyValue(prop) == value)

    def CreateAndCondition(self, a: _Cond, b: _Cond) -> _Cond:  # noqa: N802
        return _Cond(lambda e: a.test(e) and b.test(e))

    def CreateTrueCondition(self) -> _Cond:  # noqa: N802
        return _Cond(lambda e: True)


def test_a_the_uia_adapter_finds_the_hidden_input_from_the_window_root(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.perception.tree as tree_mod
    from yuki.actions.slider import _UiaTree, run

    monkeypatch.setattr(tree_mod, "_uia_core", lambda: SimpleNamespace(IUIAutomationRangeValuePattern=object))
    vol = FakeSlider(0.7)
    window = _El(50032, "Spotify Premium", (141, 179, 1543, 1009))
    pane = _El(50033, "", (141, 179, 1543, 1009), window)               # what ElementFromPoint answered live
    doc = _El(50030, "Spotify", (141, 179, 1543, 1009), pane)
    bar = _El(50026, "Now playing bar", (147, 948, 1537, 1003), doc)
    _El(50000, "Mute", (1387, 963, 1412, 988), bar, desc="Volume high")
    _El(50020, "Change volume", (1410, 970, 1454, 1005), bar)
    _El(50015, "Change volume", (0, 0, 0, 0), bar, rng=vol)             # the hidden range input, an empty box
    _El(50015, "Change progress", (0, 0, 0, 0), bar, rng=FakeSlider(65.0, 0.0, 219.0, 5.0))
    automation = _Automation(window)
    ok, summary, details = run(_UiaTree(automation, 1234), CHANGE_VOLUME, by=-20, press=vol.press,
                               sleep=lambda _s: None, settle_s=0.0)
    assert ok and details["found"] == "named" and vol.value == pytest.approx(0.5) and automation.point_asked == 0
    assert "70% -> 50%" in summary


# -- through the dispatcher, on the stub desk shaped like the live player ---------------------------------------------


def _ids(tree: dict[str, Any]) -> dict[str, int]:
    return {e["name"]: e["id"] for e in tree["elements"]}


def test_a_the_dispatcher_lowers_the_players_own_volume_and_never_touches_the_mixer() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk(volume=60.0, mixer_level=100.0)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    out = d.dispatch("set_slider", {"element": _ids(desk.get_window_tree(PLAYER))["Change volume"],
                                    "expect_hwnd": PLAYER, "by": -15})
    assert out.ok and desk.volume == pytest.approx(45.0) and "60% -> 45%" in out.summary
    assert desk.mixer.levels["Spotify.exe"] == 100.0
    assert not any(c[0] in ("app_volume", "run_powershell", "click") for c in desk.calls)


def test_a_the_dispatcher_hands_a_shortcut_to_the_slider_hand_with_read_back() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk(volume=60.0)
    desk.volume_keys = {("ctrl", "down"): -10.0}
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    out = d.dispatch("set_slider", {"element": _ids(desk.get_window_tree(PLAYER))["Change volume"],
                                    "expect_hwnd": PLAYER, "by": -20, "keys": ["ctrl", "down"]})
    assert out.ok and desk.volume == pytest.approx(40.0) and out.payload["details"]["via"] == "shortcut"
    assert [c for c in desk.calls if c[0] == "hotkey"] and "read back" in out.summary
    bad = d.dispatch("set_slider", {"element": _ids(desk.get_window_tree(PLAYER))["Change volume"],
                                    "expect_hwnd": PLAYER, "by": -20, "keys": "ctrl+down"})
    assert not bad.ok and "'keys' must be a list" in bad.summary


def test_a_the_full_size_lyrics_hide_the_bar_and_the_hand_shows_it_by_hover() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk(volume=50.0)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    ids = _ids(desk.get_window_tree(PLAYER))
    desk.toggles.update({"Lyrics": True, "Enter Full screen": True})
    desk.lyrics_full = True                                          # the bar went away after the read
    out = d.dispatch("set_slider", {"element": ids["Change volume"], "expect_hwnd": PLAYER, "to": 20})
    assert out.ok and desk.bar_revealed and desk.volume == pytest.approx(20.0)
    assert [c for c in desk.calls if c[0] == "hover"] and not [c for c in desk.calls if c[0] == "click"]


# ---------------------------------------------------------------------------
# (b) The judge: a step that only moves around is never held as "not asked for"; what lasts still is
# ---------------------------------------------------------------------------


class _Stream:
    """A streamed forced-tool answer in small chunks (the shape Bedrock streams)."""

    def __init__(self, data: dict[str, Any], tool: str) -> None:
        self.data, self.tool = data, tool

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Any:
        import json

        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=None))
        text = json.dumps(self.data)
        for i in range(0, len(text), 7):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 7]))

    def get_final_message(self) -> Any:
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=self.tool, input=self.data)], usage=None)


class _JudgeClient:
    def __init__(self, quick: dict[str, Any], review: dict[str, Any]) -> None:
        self.answers = {"verdict": quick, "review": review}
        self.calls: list[str] = []
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **params: Any) -> _Stream:
        tool = params["tool_choice"]["name"]
        self.calls.append(tool)
        return _Stream(self.answers[tool], tool)


def _authorizer(client: _JudgeClient, policy_path: Any) -> Any:
    from yuki.agent.judge import Authorizer, PolicyReader

    settings = SimpleNamespace(judge_model="judge-model", judge_timeout_s=3.0, estimate_cost=None)
    return Authorizer(lambda: client, settings, policy=PolicyReader(policy_path))


#: The 4 Oct requests, paraphrased (the shape kept: a combined play-and-volume ask, a "No." correction).
PLAYLIST_WORDS = [{"how": "heard", "text": "Yuki, could you put on my Japanese playlist and turn Spotify up a bit?"}]
VOLUME_WORDS = [{"how": "heard", "text": "No. Turn down the in app volume."}]
SPOTIFY = {"hwnd": PLAYER, "app": "Spotify.exe", "title": "Spotify Premium"}
GO_TO_CHAT = [{"how": "typed", "text": "go to the chat with sam"}]


def test_b_search_in_your_library_is_allowed_on_the_exact_live_answers(tmp_path: Any) -> None:
    """Live: the quick check asked, the review answered toward false, lasting false, asked false, with a question -
    and the click on the library's search was held; the request broke. The quick check now also answers lasting
    (review F4): false here, as its own why says (it held the step as not matching, not as lasting)."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Click \"Search in Your Library\"? You asked me to play your Japanese "
                                             "playlist.", "lasting": False, "sends": False, "scope": "call",
         "why": "Their words ask to play a playlist and raise volume. This button does not match either."},
        {"asked": "Play Japanese playlist and increase Spotify volume.", "toward": False, "lasting": False,
         "asked_effect": False, "question": "Click \"Search in Your Library\"? You asked me to play your Japanese "
                                            "playlist and turn up the volume.", "sends": False, "scope": "call"})
    auth = _authorizer(client, tmp_path / "none.json")
    v = auth.check("click", {"element": 23}, words=PLAYLIST_WORDS,
                   target={**SPOTIFY, "role": "Button", "element": "Button \"Search in Your Library\""}, signature="s")
    assert v.allowed and v.source == "review" and client.calls == ["verdict", "review"] and auth.denials == 0
    assert v.question == "" and not v.doubt


def test_b_the_mute_click_on_the_volume_bar_is_still_held(tmp_path: Any) -> None:
    """Live: a coordinate click at the bar landed on Mute; holding it was right (lower is not silent)."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Mute Spotify? You asked me to decrease the volume.", "sends": False,
         "scope": "call", "why": "They asked to decrease volume, not mute it."},
        {"asked": "Decrease Spotify's in-app volume.", "toward": True, "lasting": True, "asked_effect": False,
         "question": "Mute Spotify? You asked me to decrease the volume.", "sends": False, "scope": "call"})
    auth = _authorizer(client, tmp_path / "none.json")
    v = auth.check("click", {"x": 1410, "y": 987}, words=VOLUME_WORDS,
                   target={**SPOTIFY, "role": "Button", "element": "Button \"Mute\""}, signature="m")
    assert not v.allowed and "Mute Spotify?" in v.question


def test_b_a_send_button_or_typed_text_waiting_stays_held_whatever_the_review_says_of_lasting(tmp_path: Any) -> None:
    from yuki.agent.judge import Verdict, combine_stages
    from yuki.agent.tools import COMMITTING_CLICK_TRAITS, PENDING_TEXT_CLICK_TRAITS

    quick_send = {"decision": "ask_user", "question": "Send Sam \"hey\"?", "sends": True, "scope": "call", "why": "x"}
    sloppy = {"asked": "go to the chat", "toward": True, "lasting": False, "asked_effect": False, "question": "",
              "sends": False, "scope": "call"}
    auth = _authorizer(_JudgeClient(quick_send, sloppy), tmp_path / "none.json")
    send = auth.check("click", {"element": 9}, words=GO_TO_CHAT,
                      target={"hwnd": 7, "app": "Discord.exe", "title": "@sam_99", "role": "Button",
                              "element": "Button \"Send\""}, signature="send")
    assert not send.allowed and send.doubt == "stages_disagree"           # the quick check said it sends
    quick = {"decision": "ask_user", "question": "Click it?", "sends": False, "scope": "call", "why": "x"}
    pending = _authorizer(_JudgeClient(quick, sloppy), tmp_path / "none.json")
    typed = pending.check("click", {"element": 9}, words=GO_TO_CHAT,
                          target={"hwnd": 7, "app": "Discord.exe", "title": "@sam_99", "role": "Button",
                                  "element": "Button \"Open\"", "typed_pending": True}, signature="p")
    assert not typed.allowed                                              # typed text waits: the click can send it
    first, review = Verdict(decision="ask_user", lasting=False), Verdict(decision="allow", lasting=False)
    assert combine_stages(first, review, COMMITTING_CLICK_TRAITS, "click", weak=True).allowed
    assert not combine_stages(first, review, COMMITTING_CLICK_TRAITS, "click", weak=False).allowed
    assert not combine_stages(first, review, PENDING_TEXT_CLICK_TRAITS, "click", weak=False).allowed
    assert not combine_stages(Verdict(decision="ask_user", sends=True, lasting=False), review,
                              COMMITTING_CLICK_TRAITS, "click", weak=True).allowed
    # review F4: one look reading a committing button as not lasting is not enough; the quick check's own answer counts
    assert not combine_stages(Verdict(decision="ask_user"), review, COMMITTING_CLICK_TRAITS, "click", weak=True).allowed
    assert not combine_stages(Verdict(decision="ask_user", lasting=True), review, COMMITTING_CLICK_TRAITS, "click",
                              weak=True).allowed


@pytest.mark.parametrize("app,title,button,words,quick_q", [
    ("explorer.exe", "Downloads", "Delete", "tidy my downloads view", "Delete these files? You asked me to tidy."),
    ("OUTLOOK.EXE", "Re: Friday plan - Message", "Send", "open my reply to dana", "Send the reply to Dana?"),
    ("chrome.exe", "Checkout", "Buy now", "show me the cart", "Buy it? You asked me to show the cart."),
])
def test_b_review_f4_a_delete_send_or_buy_button_the_review_calls_not_lasting_stays_held(
        tmp_path: Any, app: str, title: str, button: str, words: str, quick_q: str) -> None:
    """Review F4: the quick check holds a committing button as lasting; a review that mislabels it lasting false
    cannot make it an allow (the role stays firm unless both looks say nothing lasts)."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": quick_q, "lasting": True, "sends": False, "scope": "call", "why": "x"},
        {"asked": words, "toward": True, "lasting": False, "asked_effect": False, "question": "", "sends": False,
         "scope": "call"})
    auth = _authorizer(client, tmp_path / "none.json")
    v = auth.check("click", {"element": 5}, words=[{"how": "typed", "text": words}],
                   target={"hwnd": 11, "app": app, "title": title, "role": "Button",
                           "element": f"Button \"{button}\""}, signature=button)
    assert not v.allowed and v.doubt == "stages_disagree" and client.calls == ["verdict", "review"]


def test_b_review_f3_a_review_that_calls_mute_reversible_but_asks_about_it_is_an_ask(tmp_path: Any) -> None:
    """Review F3: the review answers toward true, lasting false (mute read as reversible) and writes a question: its
    own doubt (V5), so an ask, even when the quick check also said nothing lasts."""
    from yuki.agent.judge import review_decision

    review = {"asked": "lower Spotify's own volume", "toward": True, "lasting": False, "asked_effect": False,
              "question": "Mute Spotify? You asked me to lower it.", "sends": False, "scope": "call"}
    assert review_decision(review) == "ask_user"
    assert review_decision({**review, "question": ""}) == "allow"
    assert review_decision({**review, "question": None}, partial=True) is None       # its question not written yet
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Mute Spotify?", "lasting": False, "sends": False, "scope": "call",
         "why": "lower is not silent"}, review)
    auth = _authorizer(client, tmp_path / "none.json")
    v = auth.check("click", {"x": 1399, "y": 975}, words=VOLUME_WORDS,
                   target={**SPOTIFY, "role": "Button", "element": "Button \"Mute\""}, signature="m2")
    assert not v.allowed and "Mute Spotify?" in v.question


def test_b_the_live_quick_answer_without_its_lasting_answer_keeps_the_button_firm(tmp_path: Any) -> None:
    """Fail closed (review F4): a quick check that did not answer lasting (the 4 Oct schema) leaves a held button
    click held, whatever the review says."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Click it?", "sends": False, "scope": "call", "why": "x"},
        {"asked": "play a playlist", "toward": False, "lasting": False, "asked_effect": False, "question": "Click?",
         "sends": False, "scope": "call"})
    auth = _authorizer(client, tmp_path / "none.json")
    v = auth.check("click", {"element": 23}, words=PLAYLIST_WORDS,
                   target={**SPOTIFY, "role": "Button", "element": "Button \"Search in Your Library\""}, signature="s")
    assert not v.allowed


def test_b_review_f1_the_keys_a_slider_step_presses_are_judged_like_a_hotkey(tmp_path: Any) -> None:
    """Review F1: set_slider's keys went out through the hotkey hand with no judge (set_slider's own traits carry
    no destroys or reaches). With keys it now carries the hotkey tool's traits: judged, alt+f4 held."""
    from yuki.agent.judge import POLICY_DEFAULTS, firm_lasting, gate_reasons
    from yuki.agent.tools import TOOL_TRAITS, call_traits

    levels = dict(POLICY_DEFAULTS)
    with_keys = call_traits("set_slider", {"element": 50, "by": -10, "keys": ["alt", "f4"]})
    assert with_keys is TOOL_TRAITS["hotkey"]
    assert gate_reasons(with_keys, levels) == gate_reasons(call_traits("hotkey", {"keys": ["alt", "f4"]}), levels)
    assert gate_reasons(call_traits("set_slider", {"element": 50, "by": -10}), levels) == []   # no keys: no gate
    assert not firm_lasting("set_slider", with_keys)          # keys by their tool alone: the review's lasting stands
    held = _JudgeClient(
        {"decision": "ask_user", "question": "Close Spotify?", "lasting": True, "sends": False, "scope": "call",
         "why": "alt+f4 closes the window"},
        {"asked": "turn spotify's own volume down", "toward": False, "lasting": True, "asked_effect": False,
         "question": "Close Spotify? You asked me to turn it down.", "sends": False, "scope": "call"})
    v = _authorizer(held, tmp_path / "none.json").check(
        "set_slider", {"element": 50, "by": -10, "keys": ["alt", "f4"]}, words=VOLUME_WORDS,
        target={**SPOTIFY, "role": "Text", "element": "Text \"Change volume\""}, signature="k1")
    assert not v.allowed and held.calls == ["verdict", "review"]
    ok = _JudgeClient({"decision": "allow", "question": "", "lasting": False, "sends": False, "scope": "call",
                       "why": "the player's volume shortcut"}, {})
    v = _authorizer(ok, tmp_path / "none.json").check(
        "set_slider", {"element": 50, "by": -10, "keys": ["ctrl", "shift", "down"]}, words=VOLUME_WORDS,
        target={**SPOTIFY, "role": "Text", "element": "Text \"Change volume\""}, signature="k2")
    assert v.allowed and ok.calls == ["verdict"]


def test_b_settings_that_ask_first_keep_toward_as_a_condition(tmp_path: Any) -> None:
    from yuki.ui import policy as ui_policy

    path = tmp_path / "autonomy.json"
    ui_policy.save({"click_type": "ask"}, path)
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Click it?", "sends": False, "scope": "call", "why": "x"},
        {"asked": "play a playlist", "toward": False, "lasting": False, "asked_effect": False, "question": "Click?",
         "sends": False, "scope": "call"})
    auth = _authorizer(client, path)
    v = auth.check("click", {"element": 23}, words=PLAYLIST_WORDS,
                   target={**SPOTIFY, "role": "Button", "element": "Button \"Search in Your Library\""}, signature="s")
    assert not v.allowed


def test_b_the_prompt_carries_the_4_oct_cases_and_the_rule_that_holds_are_for_what_lasts() -> None:
    from yuki.agent.judge import JUDGE_PROMPT, REVIEW_TOOL

    assert "holds are for effects that last" in JUDGE_PROMPT
    assert "\"Search in Your Library\"" in JUDGE_PROMPT and "click Text \"Change volume\"" in JUDGE_PROMPT
    assert "Mute Spotify? You asked me to lower it." in JUDGE_PROMPT
    assert "a search inside the app" in REVIEW_TOOL["input_schema"]["properties"]["toward"]["description"]


def test_a_a_verified_shortcut_is_learned_as_that_apps_way_and_a_dead_one_is_not(tmp_path: Any) -> None:
    """A shortcut is learned from a verified success (practice procedure), never from code; one that did nothing
    here (the value did the work) is not kept."""
    import time as _time

    import numpy as np

    from tests.test_vfix2 import Brain, _agent, _memory, _response, _run, _use
    from yuki.memory import practice as P
    from yuki.memory.api import MemoryClient
    from yuki.memory.store import Store

    class Words:
        model_name = "stub-words"

        def embed(self, texts: list[str], wait_s: float | None = None) -> Any:
            out = np.zeros((len(texts), 64), dtype=np.float32)
            for i, text in enumerate(texts):
                for w in str(text).casefold().split():
                    out[i, sum(map(ord, w)) % 64] += 1.0
            return out

    for keys_work, folder in ((True, "works"), (False, "dead")):
        client = MemoryClient(Store.open(tmp_path / folder / "memory.db"))
        client._embedder = Words()
        desk = ChatPlayerDesk(volume=60.0)
        if keys_work:
            desk.volume_keys = {("ctrl", "down"): -10.0}
        volume = _ids(desk.get_window_tree(PLAYER))["Change volume"]
        brain = Brain([_response(_use("look_at_window", hwnd=PLAYER)),
                       _response(_use("set_slider", element=volume, expect_hwnd=PLAYER, by=-20, keys=["ctrl", "down"])),
                       _response(_use("done", message="Turned Spotify's own volume down.", about=[], states=[]))])
        try:
            _run(_agent(brain, tmp_path / folder, desk, memory=_memory(client)), "lower the volume inside spotify")
            assert desk.volume == pytest.approx(40.0) and desk.mixer.levels["Spotify.exe"] == 100.0
            deadline = _time.time() + 5
            while _time.time() < deadline and not P.live(client.store, ["procedure"]):
                _time.sleep(0.05)
            ways = P.live(client.store, ["procedure"])
            assert len(ways) == 1 and ways[0].origin == "slider"
            text = ways[0].text()
            assert ("with keys ctrl+down (its own shortcut, verified" in text) is keys_work
            assert "the mixer is another control" in text
        finally:
            client.store.close()


def test_a_the_mixer_after_a_failed_in_app_slider_is_never_learned_as_the_way_instead() -> None:
    """Live 4 Oct practice memory kept 'instead: app_volume' (the mixer: no window, no control) after the player's
    own slider failed, and the next requests were steered to the mixer."""
    from yuki.agent.frame import session_lessons, session_pitfalls

    trace = [
        {"kind": "action", "tool": "set_slider", "app": "Spotify.exe", "target": 'Text "Change volume", window "x"',
         "ok": False, "verdict": None, "why": "it failed"},
        {"kind": "action", "tool": "app_volume", "app": "Spotify.exe", "target": "", "ok": True, "verdict": True,
         "why": "its report says it was seen"},
    ]
    pits = session_pitfalls(trace, outcome="cancelled")
    assert pits["Spotify.exe"]["instead"] == "" and "set_slider" in pits["Spotify.exe"]["text"]
    assert not any("what worked after that: app_volume" in x for x in session_lessons(trace, outcome="final")
                   .get("Spotify.exe", []))
    trace.append({"kind": "action", "tool": "set_slider", "app": "Spotify.exe",
                  "target": 'Text "Change volume", window "x"', "ok": True, "verdict": True})
    assert session_pitfalls(trace, outcome="cancelled")["Spotify.exe"]["instead"].startswith("set_slider on Text")


def test_a_yukis_own_voice_note_is_not_the_users_words_in_a_lesson() -> None:
    """Live 4 Oct the voice's note naming the app ("Where they said to do it ... Spotify") was kept as "they said
    while it ran" and made an untroubled run a pitfall."""
    from pathlib import Path

    import yuki.voice.bridge as bridge
    from yuki.agent.frame import VOICE_NOTE_LABEL, session_lessons, session_pitfalls

    assert VOICE_NOTE_LABEL in Path(bridge.__file__).read_text(encoding="utf-8")   # the bridge's own label
    trace = [{"kind": "action", "tool": "set_slider", "app": "Spotify.exe", "target": 'Text "Change volume"',
              "ok": True, "verdict": True}]
    note = f"{VOICE_NOTE_LABEL} [Where they said to do it, in their own words: Spotify.]"
    assert session_lessons(trace, outcome="final", steers=[note]) == {}
    assert session_pitfalls(trace, outcome="final", steers=[note]) == {}
    assert session_lessons(trace, outcome="final", steers=[note, "the one inside spotify"])


def test_a_a_slider_finish_line_says_what_moved_not_done() -> None:
    from yuki.agent.finish import finish_line

    assert finish_line("set_slider", {"by": -15}, process="Spotify.exe") == "Turned it down in Spotify."
    assert finish_line("set_slider", {"to": 40}, process="Spotify.exe") == "Set it to 40% in Spotify."
    assert finish_line("set_slider", {"by": 10}) == "Turned it up."


def test_a_the_texts_never_offer_the_mixer_as_a_stand_in_for_the_in_app_volume() -> None:
    from yuki.agent.consult import CONSULT_PROMPT
    from yuki.agent.slider_tool import SLIDER_TOOL
    from yuki.agent.tools import TOOLS_BY_NAME

    assert "never a stand-in" in SLIDER_TOOL["description"] and "keys" in SLIDER_TOOL["input_schema"]["properties"]
    assert "never a stand-in for it" in TOOLS_BY_NAME["app_volume"]["description"]
    assert "which thing of theirs their words meant" in TOOLS_BY_NAME["remember_how"]["description"]
    assert "the Windows mixer (app_volume) is not an app's own in-app volume" in CONSULT_PROMPT
    # the talker too: live 4 Oct its own hand reached for the mixer first; a player's in-app volume is the thinker's
    from yuki.voice.hands import HAND_TOOLS
    from yuki.voice.mouth import HANDS_PROMPT

    assert "an \"in-app\" volume) is the thinker's" in HANDS_PROMPT
    hand = next(t for t in HAND_TOOLS if t["name"] == "app_volume")
    assert "Never for a player's in-app volume" in hand["description"]


# ---------------------------------------------------------------------------
# (c) "my Japanese playlist": their own item, matched by name script or what memory says it holds
# ---------------------------------------------------------------------------


def test_c_the_voice_layer_resolves_a_thing_of_theirs_by_quality_and_acts_naming_it() -> None:
    from yuki.agent.prompt import VOICE_LAYER

    assert "a name written in Japanese is a Japanese playlist" in VOICE_LAYER
    assert "never a public search or a mix the app made" in VOICE_LAYER
    assert "what memory says it holds" in VOICE_LAYER and "best guess first" in VOICE_LAYER
    assert "never ask because none is literally named that" in VOICE_LAYER and "remember_how" in VOICE_LAYER
    assert "opening it is not playing" in VOICE_LAYER


def _how_texts(client: Any) -> list[str]:
    return [k.text for k in client.store.knowhow_current(None)]


@pytest.mark.parametrize("plays", [True, False])
def test_c_review_f6_the_meaning_is_learned_only_after_the_play_worked(tmp_path: Any, plays: bool) -> None:
    """Review F6: remember_how sent with the play click ran first, in model order, and was saved before anything was
    seen to work. Now it runs after the response's actions and is held until the request ends well: saved after a
    play that worked, dropped when the play failed."""
    import time as _time

    from tests.test_vfix2 import Brain, _agent, _memory, _response, _run, _texts, _use
    from yuki.memory.api import MemoryClient
    from yuki.memory.store import Store
    from yuki.voice.offline import StubResult

    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    desk = ChatPlayerDesk(volume=50.0)
    if not plays:
        desk.click = lambda x, y, **kw: StubResult(ok=False, summary="the click did not land")   # type: ignore
    play = _ids(desk.get_window_tree(PLAYER))["Play J-Pop Nights"]
    meaning = "\"my Japanese playlist\" means J-Pop Nights"
    brain = Brain([
        _response(_use("look_at_window", hwnd=PLAYER)),
        # the know-how first in model order, the play after it: it must still wait for the play
        _response(_use("remember_how", text=meaning, app="Spotify.exe"),
                  _use("click", element=play, expect_hwnd=PLAYER)),
        _response(_use("done", message="Playing J-Pop Nights." if plays else "It didn't play.", about=[], states=[])),
    ])
    try:
        _run(_agent(brain, tmp_path, desk, memory=_memory(client)), "play my japanese playlist")
        assert (desk.playing == "J-Pop Nights") is plays
        told = _texts(brain.calls[2])
        assert ("Held: it is kept once this request ends" in told) is plays
        deadline = _time.time() + 3
        while plays and _time.time() < deadline and not _how_texts(client):
            _time.sleep(0.05)
        _time.sleep(0.1)
        assert (_how_texts(client) == [meaning]) is plays and (_how_texts(client) == []) is (not plays)
    finally:
        client.store.close()


def test_c_review_f6_a_held_meaning_is_dropped_when_they_correct_it_or_the_request_does_not_end_well(
        tmp_path: Any) -> None:
    import time as _time

    from tests.test_vfix2 import Brain, _agent

    saved: list[dict] = []
    agent = _agent(Brain([]), tmp_path, ChatPlayerDesk())
    agent.memory = SimpleNamespace(installed=True, run_tool=lambda name, inp, request=None: saved.append(inp) or
                                   SimpleNamespace(ok=True, summary="saved"))
    step = {"kind": "action", "tool": "click", "ok": True, "verdict": True, "call": 2}
    cases = [("final", [step], [], True),
             ("cancelled", [step], [], False),                                           # did not end with its answer
             ("final", [{**step, "verdict": False}], [], False),                          # its effect contradicted
             ("final", [step], ["no, the other one"], False),                             # they corrected it
             ("final", [step], ["(From Yuki's voice, about the request you are on) [Spotify]"], True),   # Yuki's note
             ("final", [], [], False)]                                                    # nothing ran after it
    for outcome, trace, later, kept in cases:
        saved.clear()
        agent._trace, agent._steer_texts, agent._model_calls = list(trace), [], 2
        agent._held_how = []
        agent._hold_how({"text": "\"my gym playlist\" means Leg Day", "app": "Spotify.exe"})
        agent._steer_texts.extend(later)
        agent._keep_held_how(outcome=outcome)
        _time.sleep(0.15)
        assert bool(saved) is kept, (outcome, trace, later)


def test_b_a_streamed_review_allow_waits_for_sends_on_a_step_that_moves_around() -> None:
    from yuki.agent.judge import review_decision

    assert review_decision({"toward": False, "lasting": False}, partial=True) is None          # sends not yet
    assert review_decision({"toward": False, "lasting": False, "sends": False}, partial=True) == "allow"
    assert review_decision({"toward": True, "lasting": False, "sends": True, "question": "",
                            "asked_effect": True}) == "allow"
    assert review_decision({"toward": True, "lasting": False, "sends": True, "question": "",
                            "asked_effect": False}) == "ask_user"                               # a send lasts
