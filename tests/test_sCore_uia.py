"""sCore: rung B's step runner moved to ``yuki.skills.uia_route`` and the by-name pattern calls inside containers
(PLAN.md B.2 "Recorded-path dependencies", D4 containers, M5).

A fake UI Automation tree only (``tests/_fake_uia.py``, invented names): no desktop, no COM, nothing pressed for real.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._fake_uia import Automation, Cached, Node
from yuki.connect.spotify_desktop import UiaTree
from yuki.skills import uia_route as U

HWND = 7301


class FakeAutomation(Automation):
    def CreateTrueCondition(self) -> tuple:
        return ("true",)


class Pattern:
    def __init__(self, node: Node, log: list) -> None:
        self.node, self.log = node, log

    def QueryInterface(self, _iface: Any) -> "Pattern":
        return self

    def Invoke(self) -> None:
        self.log.append(("invoke", self.node.name))

    def Select(self) -> None:
        self.log.append(("select", self.node.name))

    def SetValue(self, value: Any) -> None:
        self.log.append(("set", self.node.name, value))


def _with_patterns(log: list) -> None:
    def get_pattern(self: Cached, pid: int) -> Any:
        return Pattern(self.node, log) if (self.node.invoke or self.node.range is not None) else None

    Cached.GetCurrentPattern = get_pattern       # the fake gains UIA's pattern call for these tests


@pytest.fixture(autouse=True)
def _clean_patterns():
    yield
    if hasattr(Cached, "GetCurrentPattern"):
        del Cached.GetCurrentPattern


class Module:
    IUIAutomationInvokePattern = object()
    IUIAutomationSelectionItemPattern = object()
    IUIAutomationValuePattern = object()
    IUIAutomationRangeValuePattern = object()


def _row(name: str, playing: bool = False, offscreen: bool = False) -> Node:
    group = Node("Group", name, offscreen=offscreen).add(
        Node("Button", f"{name} Playlist • Invented Owner", invoke=True, offscreen=offscreen),
        Node("Button", ("Pause " if playing else "Play ") + name, invoke=True, offscreen=offscreen))
    return Node("DataItem", "", invoke=True, offscreen=offscreen).add(Node("DataItem", "", offscreen=offscreen).add(group))


def player(rows: list[tuple[str, bool]], *, offscreen: bool = False) -> Node:
    window = Node("Window", "Invented Player", (0, 0, 1600, 900), offscreen=offscreen)
    home = Node("Group", "Home", offscreen=offscreen)
    for i in range(8):                                     # 8 more Buttons named "Play" (one "Pause") outside the grid
        home.add(Node("Button", "Pause" if i == 3 else "Play", invoke=True, offscreen=offscreen))
    home.add(Node("Button", "Pause Rainy Tapes", invoke=True, offscreen=offscreen))   # a card, outside the grid
    grid = Node("DataGrid", "Demo lists", offscreen=offscreen)
    for name, playing in rows:
        grid.add(_row(name, playing, offscreen=offscreen))
    bar = Node("Group", "Demo bar", offscreen=offscreen).add(
        Node("Slider", "Demo volume", range=(0.4, 0.0, 1.0), offscreen=offscreen),
        Node("Button", "Play", invoke=True, offscreen=offscreen))
    window.add(home, grid, bar)
    return window


GRID = [["DataGrid", "Demo lists"]]


def test_a_site_is_its_host_without_www() -> None:
    # r30: the yuki.voice.skills shim this rode on was deleted (iVoice, 18 Oct); the check on the moved module stays
    assert U.site("www.Example.org") == "example.org"


def test_a_label_matches_only_inside_its_container() -> None:
    tree = UiaTree(FakeAutomation({HWND: player([("Rainy Tapes", False), ("Sunday Static", True)])}), HWND)
    got = U.search(tree, role="Button", name="Play Rainy Tapes", container=GRID)
    assert got.status == "found" and got.n == 1
    assert U.search(tree, role="Button", name="Pause Rainy Tapes", container=GRID).status == "missing"   # card's
    assert U.search(tree, role="Button", name="Play", container=[["Group", "Demo bar"]]).status == "found"
    assert U.search(tree, role="Button", name="Play").status == "ambiguous"     # from the root: never guessed
    assert U.search(tree, role="Button", name="x", container=[["DataGrid", "Nothing"]]).status == "no_container"


def test_two_rows_with_one_name_are_ambiguous_never_a_guess() -> None:
    tree = UiaTree(FakeAutomation({HWND: player([("Twin Name", False), ("Twin Name", False)])}), HWND)
    got = U.search(tree, role="Button", name="Play Twin Name", container=GRID)
    assert got.status == "ambiguous" and got.n == 2


def test_offscreen_elements_are_still_searched() -> None:
    tree = UiaTree(FakeAutomation({HWND: player([("Rainy Tapes", False)], offscreen=True)}), HWND)
    assert U.search(tree, role="Button", name="Play Rainy Tapes", container=GRID).status == "found"


def test_press_calls_the_pattern_on_the_one_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "refuse_input", lambda *a, **k: None)
    log: list = []
    _with_patterns(log)
    auto = FakeAutomation({HWND: player([("Rainy Tapes", False), ("Sunday Static", True)])})
    got = U.press(HWND, role="Button", name="Play Rainy Tapes", container=GRID, automation_fn=lambda: auto,
                  module=Module)
    assert got.outcome == "pressed" and log == [("invoke", "Play Rainy Tapes")]
    got = U.press(HWND, role="Slider", name="Demo volume", container=[["Group", "Demo bar"]], pattern="range",
                  value=0.25, automation_fn=lambda: auto, module=Module)
    assert got.outcome == "pressed" and log[-1] == ("set", "Demo volume", 0.25)
    amb = U.press(HWND, role="Button", name="Play", automation_fn=lambda: auto, module=Module)
    assert amb.outcome == "ambiguous" and len(log) == 2                       # nothing pressed
    assert U.press(HWND, role="Button", name="Play Rainy Tapes", pattern="drag").outcome == "error"


def test_route_steps_fill_slots_by_code_and_refuse_a_missing_value() -> None:
    steps = U.route_steps([{"do": "keys", "keys": ["ctrl", "k"]}, {"do": "type", "text": "{person.name}"},
                           {"do": "keys", "keys": ["enter"]}], {"person": {"name": "Invented Friend"}})
    assert steps[1] == {"do": "type", "text": "Invented Friend"}
    with pytest.raises(U.SkillRefused):
        U.route_steps([{"do": "type", "text": "{text}"}], {})


def test_run_route_never_sends_what_the_user_did_not_say() -> None:
    class Hands:
        _dispatcher = None
        backend = None
        memory = None

    steps = [{"do": "type", "text": "on my way"}, {"do": "keys", "keys": ["enter"]}]
    with pytest.raises(U.SkillRefused):
        U.run_route(Hands(), steps, words=["message Invented Friend"], apps=["discord.exe"], key="message",
                    sends={"to": "Invented Friend", "what": "on my way"})
