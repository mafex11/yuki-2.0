"""A window's own tab strip (a browser's tabs), read through UI Automation (docs/VOICE.md §5.6).

A tab is not a window: on 2026-09-25 "switch to the video tab" was done by the
window hand focusing Chrome, and Yuki said "Done, switched" with the same tab in
front. The voice's desktop block now lists the foreground window's tabs, and its
tab hand selects one of them (:func:`yuki.actions.tabs.select_tab`).

The strip is found structurally: the TabItem elements of the window outside any
Document (a page's own tabs are its content), grouped by their parent; the parent
holding the most is the strip, in tree order (left to right). Page content is
never walked (a Document's subtree is skipped), and the walk is bounded by a
node count and a time budget, one cached ``FindAllBuildCache`` per node. Nothing
is matched on names: a tab is whatever the window's UIA tree says is a TabItem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

_P_RUNTIME_ID = 30000
_P_CONTROL_TYPE = 30003
_P_NAME = 30005
_P_BOUNDING_RECT = 30001
_P_SELECTION_ITEM_IS_SELECTED = 30079
_TAB_ITEM = 50019
_DOCUMENT = 50030
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_ELEMENT = 1

#: Elements visited at most (Chrome's non-page tree is ~100-250).
MAX_NODES = 700
#: Depth walked at most.
MAX_DEPTH = 16


@dataclass
class Tab:
    """One tab of a strip (``index`` counts from 1, left to right).

    ``tab_id`` is the TabItem's UIA RuntimeId as ``"42.1234.4.567"``: the provider's own
    identity of that tab for as long as it lives, whatever its title or place (``""`` when
    the provider gives none). The brain's ``tab`` tool acts on a tab by it, never by its title.
    """

    index: int
    name: str
    selected: bool
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    tab_id: str = ""


def runtime_id_text(value: Any) -> str:
    """A cached RuntimeId (an array of ints) as ``"42.1234.4.567"``; ``""`` when there is none."""
    try:
        parts = [int(part) for part in value] if value is not None else []
    except Exception:  # noqa: BLE001 - a provider that gave something else
        return ""
    return ".".join(str(part) for part in parts)


@dataclass
class TabStrip:
    """A window's tabs, as read. ``tabs`` is empty when it has none (or none could be read)."""

    hwnd: int
    tabs: list[Tab] = field(default_factory=list)
    nodes: int = 0
    ms: float = 0.0
    complete: bool = True
    error: str | None = None

    @property
    def selected(self) -> Tab | None:
        return next((t for t in self.tabs if t.selected), None)


def _rect(value: Any) -> tuple[int, int, int, int]:
    try:
        return (int(value.left), int(value.top), int(value.right), int(value.bottom))
    except Exception:  # noqa: BLE001
        try:
            left, top, width, height = (int(v) for v in value)
            return (left, top, left + width, top + height)
        except Exception:  # noqa: BLE001
            return (0, 0, 0, 0)


def walk_tabs(automation: Any, hwnd: int, deadline: float) -> tuple[list[tuple[Tab, Any]], int, bool]:
    """``([(tab, element)], nodes visited, complete)`` for ``hwnd``'s tab strip (blocking, bounded)."""
    request = automation.CreateCacheRequest()
    for prop in (_P_CONTROL_TYPE, _P_NAME, _P_BOUNDING_RECT, _P_SELECTION_ITEM_IS_SELECTED, _P_RUNTIME_ID):
        request.AddProperty(prop)
    request.TreeScope = _TREE_SCOPE_ELEMENT
    condition = automation.ControlViewCondition
    root = automation.ElementFromHandle(int(hwnd))
    queue: list[tuple[Any, int, int]] = [(root, 0, 0)]  # element, depth, parent key
    groups: dict[int, list[tuple[str, bool, tuple[int, int, int, int], Any]]] = {}
    nodes, key, complete = 0, 0, True
    while queue:
        if time.monotonic() >= deadline or nodes >= MAX_NODES:
            complete = False
            break
        element, depth, _parent = queue.pop(0)
        key += 1
        parent_key = key
        try:
            children = element.FindAllBuildCache(_TREE_SCOPE_CHILDREN, condition, request)
            count = children.Length
        except Exception:  # noqa: BLE001 - a provider that went away mid-walk: skip it
            continue
        for i in range(count):
            child = children.GetElement(i)
            nodes += 1
            try:
                kind = int(child.GetCachedPropertyValue(_P_CONTROL_TYPE) or 0)
            except Exception:  # noqa: BLE001
                continue
            if kind == _TAB_ITEM:
                name = child.GetCachedPropertyValue(_P_NAME)
                selected = bool(child.GetCachedPropertyValue(_P_SELECTION_ITEM_IS_SELECTED))
                groups.setdefault(parent_key, []).append(
                    (" ".join(str(name or "").split()), selected,
                     _rect(child.GetCachedPropertyValue(_P_BOUNDING_RECT)), child))
                continue  # a tab's own children (its close button) are not tabs
            if kind == _DOCUMENT or depth + 1 >= MAX_DEPTH:
                continue  # page content is never walked
            queue.append((child, depth + 1, parent_key))
    if not groups:
        return [], nodes, complete
    strip = max(groups.values(), key=len)
    return [(Tab(index=i + 1, name=n, selected=s, bounds=b, tab_id=_tab_id(e)), e)
            for i, (n, s, b, e) in enumerate(strip)], nodes, complete


def _tab_id(element: Any) -> str:
    """The TabItem's cached RuntimeId as text (``""`` when the provider gives none)."""
    try:
        return runtime_id_text(element.GetCachedPropertyValue(_P_RUNTIME_ID))
    except Exception:  # noqa: BLE001
        return ""


def _on_uia_thread(job: Any, budget_s: float, name: str) -> tuple[dict, bool]:
    """Run ``job(automation, shared)`` on its own COM thread; ``(shared, timed_out)``."""
    from yuki.actions.input import _Pending

    pending = _Pending(job, name, budget_s)
    shared = pending.wait()
    return shared, pending.timed_out


def read_tabs(hwnd: int, *, budget_s: float = 0.2) -> TabStrip:
    """``hwnd``'s tabs (read-only, at most ``budget_s``). Never raises."""
    started = time.perf_counter()
    strip = TabStrip(hwnd=int(hwnd))

    def job(automation: Any, shared: dict) -> None:
        found, nodes, complete = walk_tabs(automation, int(hwnd), time.monotonic() + budget_s * 0.9)
        shared["tabs"] = [t for t, _e in found]
        shared["nodes"], shared["complete"] = nodes, complete

    try:
        shared, timed_out = _on_uia_thread(job, budget_s, "yuki-tabs")
    except Exception as exc:  # noqa: BLE001
        strip.error = f"{type(exc).__name__}: {exc}"
        return strip
    strip.tabs = list(shared.get("tabs") or [])
    strip.nodes = int(shared.get("nodes") or 0)
    strip.complete = bool(shared.get("complete", False)) and not timed_out
    strip.error = shared.get("error") or ("timed out" if timed_out and not strip.tabs else None)
    strip.ms = round((time.perf_counter() - started) * 1000.0, 1)
    return strip


def tabs_line(strip: TabStrip, *, limit: int = 20, name_chars: int = 60) -> str:
    """The strip as one line for the voice's desktop block (``""`` without tabs)."""
    if not strip.tabs:
        return ""
    shown = strip.tabs[:limit]
    items = []
    for tab in shown:
        name = tab.name if len(tab.name) <= name_chars else tab.name[: name_chars - 1] + "…"
        items.append(f"{tab.index}{'*' if tab.selected else ''} \"{name}\"")
    more = f"; +{len(strip.tabs) - limit} more" if len(strip.tabs) > limit else ""
    return (f"tabs of [{strip.hwnd}] ({len(strip.tabs)}, * = the one showing): " + "; ".join(items) + more)



def format_tabs(strip: TabStrip) -> str:
    """The strip for the brain's ``read_tabs``: one line per tab with its id, place and full name.

    The id (``[42.1234.4.567]``) is what the ``tab`` tool takes: a tab is acted on by its
    identity, never by its title (a title is cut short in the strip, and many pages never
    say which site they are on).
    """
    if not strip.tabs:
        if strip.error:
            return f"could not read the tabs of window {strip.hwnd}: {strip.error}"
        return f"window {strip.hwnd} shows no tabs" + ("" if strip.complete else " (the read was cut short)")
    head = f"window {strip.hwnd}: {len(strip.tabs)} tabs, left to right (* = the one showing)"
    if not strip.complete:
        head += "; the read was cut short, there may be more"
    lines = [head]
    for tab in strip.tabs:
        ident = f"[{tab.tab_id}]" if tab.tab_id else "[no id: this window gives its tabs none]"
        lines.append(f"- {ident} {tab.index}{'*' if tab.selected else ''} \"{tab.name}\"")
    return "\n".join(lines)
