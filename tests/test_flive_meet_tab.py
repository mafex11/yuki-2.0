"""fLive: the Meet tab found by identity, never by title (PLAN.md D8, F5; map-foundations F5.5).

Offline: a fake UI Automation tree shaped like Zen's vertical tabs (Essentials/pinned and normal tabs in separate
groups), a stub site judge, a temp store stub with a real cipher. Nothing reads a real window or the real database.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.callevidence import CallEvidence, SiteTabs
from yuki.memory.crypto import FieldCipher
from yuki.perception import tabs as T

TAB_ITEM, BUTTON, GROUP, DOCUMENT, PANE, TOOLBAR = 50019, 50000, 50026, 50030, 50033, 50021


class El:
    def __init__(self, kind: int, name: str = "", rid: tuple[int, ...] = (), children: list | None = None,
                 selected: bool = False) -> None:
        self.kind, self.name, self.rid, self.selected = kind, name, rid, selected
        self.children = children or []

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def FindAllBuildCache(self, scope: int, cond: Any, _request: Any) -> "Arr":
        if scope == 2:
            return Arr(list(self.children))
        found = [e for e in self.walk() if cond is None or cond == ("type", e.kind)]
        return Arr(found)

    def GetCachedPropertyValue(self, prop: int) -> Any:
        return {30003: self.kind, 30005: self.name, 30001: (0, 0, 200, 30), 30000: list(self.rid) or None,
                30079: self.selected}.get(prop)


class Arr:
    def __init__(self, items: list) -> None:
        self.items, self.Length = items, len(items)

    def GetElement(self, i: int) -> Any:
        return self.items[i]


class Auto:
    ControlViewCondition = None

    def __init__(self, roots: dict[int, El]) -> None:
        self.roots = roots

    def CreateCacheRequest(self) -> Any:
        return SimpleNamespace(AddProperty=lambda _p: None, TreeScope=0)

    def CreatePropertyCondition(self, prop: int, value: Any) -> Any:
        return ("type", value)

    def ElementFromHandle(self, hwnd: int) -> El:
        if hwnd not in self.roots:
            raise OSError("gone")
        return self.roots[hwnd]


PINNED = ["Invented Mail", "Daily Sync - Meet", "Invented Calendar"]
NORMAL = [f"Invented page {i}" for i in range(1, 10)]


def zen(selected: str = "Daily Sync - Meet", base: int = 100) -> El:
    """Zen's window: a sidebar with the pinned group (3 tabs) and the normal group (9 tabs), and a page Document."""
    def tab(name: str, rid: int) -> El:
        return El(TAB_ITEM, name, (42, base, rid), [El(BUTTON, "Close Tab")], selected=name == selected)

    pinned = El(GROUP, "", (), [tab(n, i) for i, n in enumerate(PINNED)])
    normal = El(GROUP, "", (), [tab(n, 10 + i) for i, n in enumerate(NORMAL)])
    sidebar = El(TOOLBAR, "", (), [pinned, El(BUTTON, "New Tab"), normal])
    page = El(DOCUMENT, "page", (), [El(TAB_ITEM, "a page's own tab", (9,))])
    return El(PANE, "Zen", (), [sidebar, page])


def test_all_groups_finds_every_tab_the_default_still_the_strip() -> None:
    auto = Auto({1: zen()})
    found, _n, complete = T.walk_tabs(auto, 1, time.monotonic() + 5)
    assert complete and len(found) == 9 and all(t.group == 1 for t, _e in found)
    diag: dict = {}
    every, _n, _c = T.walk_tabs(auto, 1, time.monotonic() + 5, all_groups=True, diagnostics=diag)
    assert len(every) == 12
    assert [t.name for t, _e in every if t.group == 0] == PINNED
    sel = [t for t, _e in every if t.selected][0]
    assert sel.group == 0 and sel.index == 2 and sel.group_size == 3
    assert diag["groups"] == [3, 9] and diag["selected_group"] == 0 and diag["main_group"] == 1
    assert diag["reason"] is None and diag["documents_skipped"] == 1


def test_an_empty_strip_says_why_without_names() -> None:
    # tabs drawn inside a Document near the root (page content): never read as tabs, but the reason says so
    inside = El(PANE, "Zen", (), [El(DOCUMENT, "chrome", (), [El(GROUP, "", (), [El(TAB_ITEM, "secret tab", (1,))])])])
    diag: dict = {}
    found, _n, _c = T.walk_tabs(Auto({1: inside}), 1, time.monotonic() + 5, all_groups=True, diagnostics=diag)
    assert found == [] and diag["reason"] == "tabs_inside_documents" and diag["tabitems_in_documents"] == 1
    assert "secret" not in json.dumps(diag)
    diag2: dict = {}
    T.walk_tabs(Auto({1: El(PANE, "", (), [El(BUTTON, "x")])}), 1, time.monotonic() + 5, diagnostics=diag2)
    assert diag2["reason"] == "no_tab_items"
    diag3: dict = {}
    T.walk_tabs(Auto({1: zen()}), 1, time.monotonic() - 1, diagnostics=diag3)
    assert diag3["reason"] == "cut_short" and diag3["complete"] is False
    diag4: dict = {}
    T.walk_tabs(Auto({}), 1, time.monotonic() + 5, diagnostics=diag4)
    assert diag4["reason"] == "error"


def test_tab_action_selects_a_pinned_tab_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.actions.input as input_module
    import yuki.perception.tree as tree_module
    import yuki.perception.windows as windows_module
    from yuki.actions.tabs import tab_action

    root = zen(selected="Invented page 3")
    tabs = {t.rid: t for t in root.walk() if t.kind == TAB_ITEM}

    class Pattern:
        def __init__(self, el: El) -> None:
            self.el = el

        def QueryInterface(self, _i: Any) -> "Pattern":
            return self

        def Select(self) -> None:
            for t in tabs.values():
                t.selected = t is self.el

    def current(self: El, prop: int) -> Any:
        return self.GetCachedPropertyValue(prop)

    monkeypatch.setattr(El, "GetCurrentPropertyValue", current, raising=False)
    monkeypatch.setattr(El, "GetCurrentPattern", lambda self, pid: Pattern(self)
                        if pid == 10010 and self.kind == TAB_ITEM else None, raising=False)
    monkeypatch.setattr(input_module, "_new_automation", lambda: Auto({5: root}))
    monkeypatch.setattr(tree_module, "_uia_core", lambda: SimpleNamespace(
        IUIAutomationSelectionItemPattern=1, IUIAutomationInvokePattern=2, IUIAutomationLegacyIAccessiblePattern=3))
    monkeypatch.setattr(windows_module, "window_info", lambda _h: SimpleNamespace(title="t"))
    meet = T.runtime_id_text((42, 100, 1))
    result = tab_action(5, meet, "select")
    assert result.ok and result.details["verified"]
    assert tabs[(42, 100, 1)].selected
    assert any(r["group"] == 0 and r["id"] == meet and r["selected"] for r in result.details["tabs_after"])


# ---------------------------------------------------------------------------------------------------------------------
# Identities, the census
# ---------------------------------------------------------------------------------------------------------------------


def _digest(name: str) -> str:
    import hashlib

    return hashlib.sha256(("k|" + name).encode()).hexdigest()


def test_identities_learned_and_the_census_finds_the_tab_again() -> None:
    auto = Auto({1: zen()})
    diag: dict = {}
    found, _n, _c = T.walk_tabs(auto, 1, time.monotonic() + 5, all_groups=True, diagnostics=diag)
    sel = [t for t, _e in found if t.selected][0]
    seen = T.sighting(sel, digest=_digest(sel.name), pid=777, main_group=diag["main_group"], groups=2, at=1.0)
    assert seen["position"] == {"group": 0, "index": 2, "groups": 2}
    ident = T.learn_identity(None, seen)
    ident = T.learn_identity(ident, {**seen, "at": 2.0})
    assert ident["names"][_digest("Daily Sync - Meet")]["n"] == 2 and len(ident["runtime_ids"]) == 1
    # same browser process: the RuntimeId wins
    result = T.census_job(auto, [(1, 777)], ident, _digest)
    assert result["decision"] == "one" and result["chosen"][0].match == "runtime_id"
    assert result["chosen"][0].group == 0 and result["chosen"][0].index == 2
    # Zen restarted (new pid, new RuntimeIds): found by the name digest
    auto2 = Auto({1: zen(base=555, selected="Invented page 1")})
    result2 = T.census_job(auto2, [(1, 888)], ident, _digest)
    assert result2["decision"] == "one" and result2["chosen"][0].match == "name"
    # the title changed and Zen restarted: the pinned place still finds it
    renamed = zen(base=600)
    for t in renamed.walk():
        if t.name == "Daily Sync - Meet":
            t.name = "Meet - another title"
    result3 = T.census_job(Auto({1: renamed}), [(1, 999)], ident, _digest)
    assert result3["decision"] == "one" and result3["chosen"][0].match == "position"
    # two equal Meet tabs: ask
    two = zen(base=700)
    two.children[0].children[2].children.append(El(TAB_ITEM, "Daily Sync - Meet", (42, 700, 99)))
    result4 = T.census_job(Auto({1: two}), [(1, 1000)], {"names": {_digest("Daily Sync - Meet"): {"n": 3}}}, _digest)
    assert result4["decision"] == "ask" and len(result4["chosen"]) == 2
    # none
    result5 = T.census_job(Auto({1: zen(base=800)}), [(1, 1)], {"names": {_digest("nope"): {"n": 1}}}, _digest)
    assert result5["decision"] == "none"
    assert "Daily Sync" not in json.dumps([vars(c) for c in result["candidates"]] + result["diagnostics"])


# ---------------------------------------------------------------------------------------------------------------------
# calling_service and the persisted site verdicts
# ---------------------------------------------------------------------------------------------------------------------


class Store:
    def __init__(self) -> None:
        self.cipher = FieldCipher(os.urandom(32))
        self.state: dict[str, str] = {}

    def nudge_state(self, name: str) -> str | None:
        return self.state.get(name)

    def set_nudge_state(self, name: str, value: str | None) -> None:
        self.state[name] = value  # type: ignore[assignment]


ENTRIES = ("meet.google.com", "zoom.us", "examplebank.com", "accounts.google.com")


def _sites(calls: list) -> Any:
    def judge(entries: tuple[str, ...]) -> dict:
        calls.append(entries)
        return {"meet.google.com": (True, "Google Meet"), "zoom.us": (True, "Zoom")}
    return judge


def _wait(pred: Any, s: float = 2.0) -> None:
    end = time.monotonic() + s
    while not pred() and time.monotonic() < end:
        time.sleep(0.01)


def test_calling_service_never_blocks_and_verdicts_survive_a_restart() -> None:
    store, calls = Store(), []
    ev = CallEvidence(sites_fn=_sites(calls), entries=ENTRIES, state=store)
    assert ev.calling_service("meet.google.com") is None          # not judged yet: judged off this thread
    _wait(lambda: ev.calling_service("meet.google.com", judge=False) is not None)
    assert ev.calling_service("meet.google.com") == "Google Meet"
    assert ev.calling_service("secure.examplebank.com") is None    # a bank: nothing
    assert ev.calling_service("unlisted.example") is None          # a site the user blocked: never counts
    assert len(calls) == 1
    kept = json.loads(store.state["call_evidence:sites"])
    assert kept["verdicts"]["meet.google.com"] == [True, "Google Meet"]
    # a second instance (a service restart) reuses them: no judgement
    ev2 = CallEvidence(sites_fn=_sites(calls), entries=ENTRIES, state=store)
    assert ev2.calling_service("meet.google.com") == "Google Meet" and len(calls) == 1
    # a changed shipped list is judged again
    ev3 = CallEvidence(sites_fn=_sites(calls), entries=ENTRIES + ("teams.live.com",), state=store)
    assert ev3.calling_service("meet.google.com") is None
    _wait(lambda: len(calls) == 2)
    assert len(calls) == 2


def test_the_microphone_path_also_reuses_the_kept_verdicts() -> None:
    store, calls = Store(), []
    CallEvidence(sites_fn=_sites(calls), entries=ENTRIES, state=store)._judge_entries()
    ev = CallEvidence(sites_fn=_sites(calls), entries=ENTRIES, state=store)
    ev.seen({"process": "zen", "exe": "C:/zen/zen.exe", "hwnd": 1, "at": time.time(), "host": "meet.google.com",
             "blocked": "blocked_host"})
    ev.judge(["zen"])
    assert len(calls) == 1
    assert ev.state("zen", time.time() - 5)[0] == "fresh"


# ---------------------------------------------------------------------------------------------------------------------
# SiteTabs: the watcher hook
# ---------------------------------------------------------------------------------------------------------------------


class Board:
    def __init__(self) -> None:
        self.tabs: dict[int, dict] = {}

    def set_calling_tab(self, hwnd: int, info: dict) -> None:
        self.tabs[hwnd] = info


def test_site_tabs_learn_the_meet_tab_on_a_sighting_and_never_keep_its_name() -> None:
    store, calls, logs, board = Store(), [], [], Board()
    ev = CallEvidence(sites_fn=_sites(calls), entries=ENTRIES, state=store)
    ev._judge_entries()
    reads = []

    def reader(hwnd: int, *, digest_fn: Any) -> dict:
        reads.append(hwnd)
        auto = Auto({hwnd: zen()})
        diag: dict = {}
        found, _n, _c = T.walk_tabs(auto, hwnd, time.monotonic() + 1, all_groups=True, diagnostics=diag)
        sel = [t for t, _e in found if t.selected][0]
        return {"sighting": T.sighting(sel, digest=digest_fn(sel.name), pid=777, main_group=diag["main_group"],
                                       groups=len(diag["groups"]), at=time.time()), "tabs": len(found), **diag}

    tabs = SiteTabs(ev, store=store, board=board, log=lambda t, **f: logs.append((t, f)), reader=reader)
    sighting = {"process": "zen", "exe": "C:/zen/zen.exe", "hwnd": 31, "at": time.time(), "host": "meet.google.com",
                "blocked": "blocked_host"}
    tabs.seen(sighting)
    tabs.seen({**sighting, "at": sighting["at"] + 5})        # within 10 min: not read again
    tabs.seen({**sighting, "host": "secure.examplebank.com"})  # a bank: nothing
    tabs.seen({"process": "zen", "hwnd": 31, "host": "meet.google.com"})   # not a blocked sighting: nothing
    assert reads == [31]
    ident = json.loads(store.state["site_tabs:Google Meet"])
    assert ident["process"] == "zen" and len(ident["names"]) == 1 and ident["positions"][0]["group"] == 0
    assert tabs.known(31) is True
    info = board.tabs[31]
    assert info["service"] == "Google Meet" and info["known"] and info["group"] == 0 and info["index"] == 2
    assert len(info["name_digest"]) == 64
    strip = [f for t, f in logs if t == "tab_strip"]
    assert strip and strip[0]["groups"] == [3, 9] and strip[0]["tab_known"] is True and strip[0]["pinned"] is True
    blob = json.dumps(logs) + json.dumps(store.state) + json.dumps(board.tabs)
    for name in PINNED + NORMAL:
        assert name not in blob


def test_site_tabs_census_logs_content_free() -> None:
    store, logs = Store(), []
    ev = CallEvidence(sites_fn=_sites([]), entries=ENTRIES, state=store)
    tabs = SiteTabs(ev, store=store, log=lambda t, **f: logs.append((t, f)),
                    census_fn=lambda windows, ident, *, digest_fn: T.census_job(Auto({41: zen()}), windows, ident,
                                                                                digest_fn))
    store.state["site_tabs:Google Meet"] = json.dumps(
        {"process": "zen", "names": {tabs.digest("zen", "Daily Sync - Meet"): {"n": 2}}})
    result = tabs.census("Google Meet", windows=[(41, 5)])
    assert result["decision"] == "one" and result["chosen"][0].match == "name"
    log = [f for t, f in logs if t == "tab_census"][0]
    assert log["decision"] == "one" and log["match"] == "name" and log["windows"] == 1 and log["tabs"] == 12
    assert "Daily" not in json.dumps(logs)


def test_an_empty_strip_is_logged_with_its_reason() -> None:
    store, logs, board = Store(), [], Board()
    ev = CallEvidence(sites_fn=_sites([]), entries=ENTRIES, state=store)
    ev._judge_entries()
    tabs = SiteTabs(ev, store=store, board=board, log=lambda t, **f: logs.append((t, f)),
                    reader=lambda hwnd, *, digest_fn: {"sighting": None, "tabs": 0, "groups": [], "nodes": 4,
                                                       "complete": True, "reason": "no_tab_items"})
    tabs.seen({"process": "zen", "hwnd": 3, "at": 1.0, "host": "meet.google.com", "blocked": "blocked_host"})
    log = [f for t, f in logs if t == "tab_strip"][0]
    assert log["reason"] == "no_tab_items" and log["tab_known"] is False
    assert board.tabs[3]["known"] is False and "site_tabs:Google Meet" not in store.state


def test_a_failed_list_judgement_is_not_retried_on_every_sighting() -> None:
    calls = []

    def failing(entries: tuple[str, ...]) -> dict:
        calls.append(entries)
        raise RuntimeError("no network")

    ev = CallEvidence(sites_fn=failing, entries=ENTRIES, state=Store())
    assert ev.calling_service("meet.google.com") is None
    _wait(lambda: len(calls) == 1)
    _wait(lambda: not (ev._entries_thread and ev._entries_thread.is_alive()))
    for _ in range(20):
        assert ev.calling_service("meet.google.com") is None
    time.sleep(0.1)
    assert len(calls) == 1


def test_the_empty_strip_diagnostic_never_walks_a_whole_page() -> None:
    """Review r1 L6: only the top two child levels of a Document near the root are looked at, one children request
    per element (never a whole-subtree request into the page)."""
    scopes: list[int] = []

    class Rec(El):
        def FindAllBuildCache(self, scope: int, cond: Any, request: Any) -> "Arr":
            scopes.append(scope)
            return super().FindAllBuildCache(scope, cond, request)

    deep = Rec(DOCUMENT, "page", (), [Rec(GROUP, "", (), [Rec(GROUP, "", (), [Rec(TAB_ITEM, "deep tab", (3,))])])])
    diag: dict = {}
    T.walk_tabs(Auto({1: Rec(PANE, "Zen", (), [deep])}), 1, time.monotonic() + 5, all_groups=True, diagnostics=diag)
    assert diag["reason"] == "no_tab_items" and diag["tabitems_in_documents"] == 0
    assert set(scopes) == {2}
    top = Rec(DOCUMENT, "page", (), [Rec(GROUP, "", (), [Rec(TAB_ITEM, "top tab", (4,))])])
    diag2: dict = {}
    T.walk_tabs(Auto({1: Rec(PANE, "Zen", (), [top])}), 1, time.monotonic() + 5, diagnostics=diag2)
    assert diag2["reason"] == "tabs_inside_documents" and diag2["tabitems_in_documents"] == 1
