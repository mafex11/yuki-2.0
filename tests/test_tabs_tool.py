"""The brain's tab tools (read_tabs, tab): tabs by their UIA identity, never by title.

Offline: a fake UI Automation tree stands in for a browser's tab strip; nothing reads
or touches a real window (ARCHITECTURE.md rule 7). The 25 Sept failures this covers:
"No GitHub tab left open" judged by tab title text, and closes reported done without
reading the strip again (docs/research/coach-and-answers-review.md 3.2 C).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yuki.perception.tabs import Tab, TabStrip, format_tabs, runtime_id_text, walk_tabs

TAB_ITEM, BUTTON, PANE, DOCUMENT = 50019, 50000, 50033, 50030


class FakePattern:
    def __init__(self, on_call: Any) -> None:
        self.on_call = on_call

    def QueryInterface(self, _iface: Any) -> "FakePattern":
        return self

    def Select(self) -> None:
        self.on_call()

    def Invoke(self) -> None:
        self.on_call()

    def DoDefaultAction(self) -> None:
        self.on_call()


class FakeElement:
    def __init__(self, kind: int, name: str = "", rid: tuple[int, ...] = (), children: list | None = None) -> None:
        self.kind, self.name, self.rid = kind, name, rid
        self.children: list[FakeElement] = children or []
        self.parent: FakeElement | None = None
        self.strip: FakeStrip | None = None
        for c in self.children:
            c.parent = self

    # UIA surface ------------------------------------------------------------
    def FindAllBuildCache(self, _scope: int, _cond: Any, _request: Any) -> "FakeArray":
        return FakeArray(list(self.children))

    def GetCachedPropertyValue(self, prop: int) -> Any:
        return {30003: self.kind, 30005: self.name, 30001: (0, 0, 100, 30), 30000: self.rid,
                30079: self.strip is not None and self.strip.selected is self}.get(prop)

    def GetCurrentPropertyValue(self, prop: int) -> Any:
        return self.GetCachedPropertyValue(prop)

    def GetCurrentPattern(self, pattern: int) -> Any:
        if self.kind == TAB_ITEM and pattern == 10010 and self.strip is not None:
            return FakePattern(lambda: self.strip.select(self))
        if self.kind == BUTTON and pattern == 10000 and self.parent is not None and self.parent.strip is not None:
            return FakePattern(lambda: self.parent.strip.close(self.parent))
        return None


class FakeArray:
    def __init__(self, items: list) -> None:
        self.items = items
        self.Length = len(items)

    def GetElement(self, i: int) -> Any:
        return self.items[i]


class FakeStrip:
    """A browser window: a pane holding TabItems (each with one close Button) and a page Document."""

    def __init__(self, tabs: list[tuple[str, tuple[int, ...]]], selected: int = 0) -> None:
        self.tabs = [FakeElement(TAB_ITEM, name, rid, [FakeElement(BUTTON, "Close")]) for name, rid in tabs]
        for t in self.tabs:
            t.strip = self
        self.selected = self.tabs[selected]
        self.pane = FakeElement(PANE, "", (), list(self.tabs))
        # a page's own tabs are content: never read as the window's tabs
        page = FakeElement(DOCUMENT, "page", (), [FakeElement(TAB_ITEM, "page tab", (9,)),
                                                   FakeElement(TAB_ITEM, "page tab 2", (10,)),
                                                   FakeElement(TAB_ITEM, "page tab 3", (11,)),
                                                   FakeElement(TAB_ITEM, "page tab 4", (12,))])
        self.root = FakeElement(PANE, "window", (), [self.pane, page])
        self.closed: list[str] = []

    def select(self, tab: FakeElement) -> None:
        self.selected = tab

    def close(self, tab: FakeElement) -> None:
        self.tabs.remove(tab)
        self.pane.children.remove(tab)
        self.closed.append(tab.name)
        if self.selected is tab and self.tabs:
            self.selected = self.tabs[0]


class FakeAutomation:
    ControlViewCondition = object()

    def __init__(self, strip: FakeStrip) -> None:
        self.strip = strip

    def CreateCacheRequest(self) -> Any:
        return SimpleNamespace(AddProperty=lambda _p: None, TreeScope=0)

    def ElementFromHandle(self, _hwnd: int) -> FakeElement:
        return self.strip.root


RELAY = ("youtube relay: retry the relay list twice by mafex11 · Pull Request #99", (42, 7, 4, 101))
VIDEO = ("ready or noT - YouTube", (42, 7, 4, 102))
DOCS = ("Settings | ElevenLabs", (42, 7, 4, 103))


@pytest.fixture()
def browser(monkeypatch: pytest.MonkeyPatch) -> FakeStrip:
    """The fake strip, served to yuki.actions.tabs through the real _Pending thread (fake automation)."""
    import yuki.actions.input as input_module
    import yuki.perception.tree as tree_module
    import yuki.perception.windows as windows_module

    strip = FakeStrip([RELAY, VIDEO, DOCS], selected=1)
    monkeypatch.setattr(input_module, "_new_automation", lambda: FakeAutomation(strip))
    monkeypatch.setattr(tree_module, "_uia_core", lambda: SimpleNamespace(
        IUIAutomationSelectionItemPattern=1, IUIAutomationInvokePattern=2, IUIAutomationLegacyIAccessiblePattern=3))
    monkeypatch.setattr(windows_module, "window_info", lambda _hwnd: SimpleNamespace(title=strip.selected.name))
    return strip


def test_walk_reads_each_tab_identity_and_skips_page_content(browser: FakeStrip) -> None:
    found, _nodes, complete = walk_tabs(FakeAutomation(browser), 4242, deadline=float("inf"))
    assert complete
    assert [(t.index, t.name, t.selected, t.tab_id) for t, _e in found] == [
        (1, RELAY[0], False, "42.7.4.101"), (2, VIDEO[0], True, "42.7.4.102"), (3, DOCS[0], False, "42.7.4.103")]


def test_tab_stays_backward_compatible() -> None:
    """The voice's stub builds Tab without an id: it still works, with no id."""
    tab = Tab(index=1, name="x", selected=True, bounds=(0, 0, 1, 1))
    assert tab.tab_id == ""
    assert runtime_id_text(None) == "" and runtime_id_text((42, -3, 7)) == "42.-3.7"


def test_format_lists_ids_full_names_and_the_one_showing() -> None:
    strip = TabStrip(hwnd=4242, tabs=[Tab(1, RELAY[0], False, tab_id="42.7.4.101"),
                                      Tab(2, VIDEO[0], True, tab_id="42.7.4.102")])
    text = format_tabs(strip)
    assert "window 4242: 2 tabs" in text
    assert f'- [42.7.4.101] 1 "{RELAY[0]}"' in text          # the full title, not the strip's cut
    assert f'- [42.7.4.102] 2* "{VIDEO[0]}"' in text
    assert "no tabs" in format_tabs(TabStrip(hwnd=1))


def test_select_by_id_whatever_its_place_now(browser: FakeStrip) -> None:
    from yuki.actions.tabs import tab_action

    browser.tabs.insert(0, browser.tabs.pop())      # the strip was reordered since it was read
    browser.pane.children[:] = list(browser.tabs)
    result = tab_action(4242, "[42.7.4.101]", "select")
    assert result.ok, result.summary
    assert browser.selected.name == RELAY[0]
    assert result.details["verified"] is True
    assert [t["id"] for t in result.details["tabs_after"]] == ["42.7.4.103", "42.7.4.101", "42.7.4.102"]


def test_close_by_id_and_the_strip_is_read_again(browser: FakeStrip) -> None:
    from yuki.actions.tabs import tab_action

    result = tab_action(4242, "42.7.4.101", "close")
    assert result.ok, result.summary
    assert browser.closed == [RELAY[0]]
    assert [t["id"] for t in result.details["tabs_after"]] == ["42.7.4.102", "42.7.4.103"]
    assert "2 tabs" in result.summary


def test_a_tab_no_longer_there_is_reported_never_swapped(browser: FakeStrip) -> None:
    from yuki.actions.tabs import tab_action

    result = tab_action(4242, "42.7.4.999", "close")
    assert not result.ok
    assert browser.closed == [] and "no tab with id" in result.summary
    assert len(result.details["tabs_after"]) == 3


def test_close_refused_without_a_single_close_control(browser: FakeStrip) -> None:
    from yuki.actions.tabs import tab_action

    browser.tabs[0].children = []                   # no close control of its own: nothing is guessed
    result = tab_action(4242, "42.7.4.101", "close")
    assert not result.ok and browser.closed == []
    assert "no single close control" in result.summary


class StubBackend:
    """Only what the two tools need."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def read_tabs(self, hwnd: int, *, budget_s: float = 0.2) -> TabStrip:
        self.calls.append(("read_tabs", hwnd, budget_s))
        return TabStrip(hwnd=hwnd, tabs=[Tab(1, RELAY[0], True, tab_id="42.7.4.101"),
                                         Tab(2, VIDEO[0], False, tab_id="42.7.4.102")])

    def tab(self, hwnd: int, tab_id: str, action: str) -> Any:
        from yuki.actions import ActionResult

        self.calls.append(("tab", hwnd, tab_id, action))
        return ActionResult(ok=True, summary=f"closed tab \"{RELAY[0]}\" [{tab_id}]; the strip, read again, has 1 tabs",
                            details={"hwnd": hwnd, "tabs_after": [
                                {"index": 1, "id": "42.7.4.102", "name": VIDEO[0], "selected": True}]})


def test_dispatcher_read_tabs_and_tab() -> None:
    from yuki.agent.tools import Dispatcher

    backend = StubBackend()
    d = Dispatcher(backend)
    listed = d.dispatch("read_tabs", {"hwnd": 4242})
    assert listed.ok and listed.summary == "2 tabs"
    assert "[42.7.4.101]" in listed.content[0]["text"]
    closed = d.dispatch("tab", {"hwnd": 4242, "tab": "[42.7.4.101]", "action": "close"})
    assert closed.ok
    assert backend.calls[-1] == ("tab", 4242, "42.7.4.101", "close")
    text = closed.content[0]["text"]
    assert "read again after it" in text and f'- [42.7.4.102] 1* "{VIDEO[0]}"' in text
    assert closed.payload["details"]["hwnd"] == 4242    # the view after the action reads that window
    bad = d.dispatch("tab", {"hwnd": 4242, "tab": "42.7.4.101", "action": "open"})
    assert not bad.ok


def test_registry_places_the_tools() -> None:
    from yuki.agent.tools import ACTION_TOOL_NAMES, AUTO_VIEW_TOOLS, FINISHES_FIELD, PERCEPTION_TOOLS, TOOLS_BY_NAME

    assert "read_tabs" in PERCEPTION_TOOLS and "read_tabs" not in ACTION_TOOL_NAMES   # a look
    assert "tab" in ACTION_TOOL_NAMES and "tab" in AUTO_VIEW_TOOLS                    # an action on a window
    assert FINISHES_FIELD in TOOLS_BY_NAME["tab"]["input_schema"]["properties"]
    assert TOOLS_BY_NAME["read_tabs"]["label"] and TOOLS_BY_NAME["tab"]["label"]


def test_look_only_backend_refuses_the_tab_action() -> None:
    """The front desk's backend blocks every action by its tool name: `tab` included."""
    from yuki.ui.runtime import LOOK_ONLY_TOOLS, HandsBlockedBackend

    blocked = HandsBlockedBackend(StubBackend())
    result = blocked.tab(4242, "42.7.4.101", "close")
    assert not result.ok and blocked.attempted == ["tab"]
    assert "tab" not in LOOK_ONLY_TOOLS and "read_tabs" in LOOK_ONLY_TOOLS
