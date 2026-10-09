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

**Every group** (``all_groups=True``, PLAN.md D8): a browser with vertical tabs (Zen)
draws its Essentials, pinned and normal tabs in separate containers, so a pinned
Meet tab sits in a smaller group than the strip. Then every group's tabs are
returned, each :class:`Tab` with its group's ordinal (in the order the walk met the
groups) and its index inside that group. The default path (the largest group) is
unchanged for the brain's ``read_tabs`` and the voice's desk block.

**Why a strip is empty** (``diagnostics``): a dict of content-free facts the walk
fills (group sizes, nodes, whether it was cut short, Documents skipped near the
root, the depth cap) and a ``reason`` code when no tab was found: ``cut_short``,
``no_tab_items``, ``tabs_inside_documents`` (TabItems exist, but only inside a
Document near the root: the browser draws its tab strip as page content), or
``error``. Never a name.

**Identities** (:func:`rank_tabs`): a tab found again by what was learned about it
(its RuntimeId while the browser lives, digests of its name, its place in a group
other than the main strip), for the Meet-tab census (PLAN.md D8). Names are digested
by the caller's function and dropped at once.
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
    #: The ordinal of the tab's group (container) in the order the walk met the groups, and that group's size
    #: (``index`` counts inside the group). One group: 0 and the strip's length.
    group: int = 0
    group_size: int = 0


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


def walk_tabs(automation: Any, hwnd: int, deadline: float, *, all_groups: bool = False,
              diagnostics: dict[str, Any] | None = None) -> tuple[list[tuple[Tab, Any]], int, bool]:
    """``([(tab, element)], nodes visited, complete)`` for ``hwnd``'s tab strip (blocking, bounded).

    ``all_groups``: every group's tabs (group by group, in the order met), not only the largest group's.
    ``diagnostics``: filled with content-free facts about the walk (see the module docstring). A window that has gone
    raises (as it always did) when no ``diagnostics`` dict is given; with one, it reads as no tabs, ``reason=error``."""
    diag = diagnostics if diagnostics is not None else {}
    request = automation.CreateCacheRequest()
    for prop in (_P_CONTROL_TYPE, _P_NAME, _P_BOUNDING_RECT, _P_SELECTION_ITEM_IS_SELECTED, _P_RUNTIME_ID):
        request.AddProperty(prop)
    request.TreeScope = _TREE_SCOPE_ELEMENT
    condition = automation.ControlViewCondition
    try:
        root = automation.ElementFromHandle(int(hwnd))
    except Exception:  # noqa: BLE001 - a window that went away
        if diagnostics is None:
            raise   # the caller words it ("could not read the tabs of window X: ...")
        diag.update(groups=[], nodes=0, complete=False, reason="error")
        return [], 0, False
    queue: list[tuple[Any, int, int]] = [(root, 0, 0)]  # element, depth, parent key
    groups: dict[int, list[tuple[str, bool, tuple[int, int, int, int], Any]]] = {}
    nodes, key, complete = 0, 0, True
    documents = shallow_documents = depth_capped = max_depth = 0
    shallow: list[Any] = []
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
            max_depth = max(max_depth, depth + 1)
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
            if kind == _DOCUMENT:
                documents += 1
                if depth + 1 <= SHALLOW_DOCUMENT_DEPTH:
                    shallow_documents += 1
                    if len(shallow) < 3:
                        shallow.append(child)
                continue  # page content is never walked
            if depth + 1 >= MAX_DEPTH:
                depth_capped += 1
                continue
            queue.append((child, depth + 1, parent_key))
    diag.update(groups=[len(g) for g in groups.values()], nodes=nodes, complete=complete, documents_skipped=documents,
                shallow_documents=shallow_documents, depth_capped=depth_capped, max_depth=max_depth)
    if not groups:
        diag["reason"] = "cut_short" if not complete else "no_tab_items"
        diag["selected_group"] = None
        if shallow and time.monotonic() < deadline:
            inside = _tab_items_inside(automation, shallow, deadline)
            diag["tabitems_in_documents"] = inside
            if inside:
                diag["reason"] = "tabs_inside_documents"
        return [], nodes, complete
    diag["reason"] = None
    ordered = list(groups.values())
    if all_groups:
        chosen = list(enumerate(ordered))
    else:
        biggest = max(range(len(ordered)), key=lambda i: len(ordered[i]))
        chosen = [(biggest, ordered[biggest])]
    out: list[tuple[Tab, Any]] = []
    for ordinal, strip in chosen:
        for i, (n, sel, b, e) in enumerate(strip):
            out.append((Tab(index=i + 1, name=n, selected=sel, bounds=b, tab_id=_tab_id(e), group=ordinal,
                            group_size=len(strip)), e))
    diag["selected_group"] = next((t.group for t, _e in out if t.selected), None)
    diag["main_group"] = max(range(len(ordered)), key=lambda i: len(ordered[i]))
    return out, nodes, complete


#: A Document this close to the window root is counted in the diagnostics (a browser that draws its own tab strip as
#: page content would put it there).
SHALLOW_DOCUMENT_DEPTH = 2
#: The empty-strip diagnostic looks this many child levels into such a Document, and at most this many elements in
#: all: a tab strip drawn as page content sits at its top, and the page itself is never walked (review r1 L6).
INSIDE_DEPTH = 2
INSIDE_MAX_NODES = 120


def _tab_items_inside(automation: Any, documents: list[Any], deadline: float) -> int:
    """How many TabItems sit in the top :data:`INSIDE_DEPTH` child levels of the given Documents (control types only,
    no names; one ``TreeScope_Children`` request per element, at most :data:`INSIDE_MAX_NODES` elements, within the
    deadline); 0 when that cannot be read. Never a whole page's subtree."""
    total = nodes = 0
    try:
        request = automation.CreateCacheRequest()
        request.AddProperty(_P_CONTROL_TYPE)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        condition = automation.ControlViewCondition
    except Exception:  # noqa: BLE001
        return 0
    level = list(documents)
    for _depth in range(INSIDE_DEPTH):
        below: list[Any] = []
        for element in level:
            if time.monotonic() >= deadline or nodes >= INSIDE_MAX_NODES:
                return total
            try:
                children = element.FindAllBuildCache(_TREE_SCOPE_CHILDREN, condition, request)
                count = int(children.Length) if children else 0
            except Exception:  # noqa: BLE001
                continue
            for i in range(count):
                if nodes >= INSIDE_MAX_NODES:
                    break
                nodes += 1
                child = children.GetElement(i)
                try:
                    kind = int(child.GetCachedPropertyValue(_P_CONTROL_TYPE) or 0)
                except Exception:  # noqa: BLE001
                    continue
                if kind == _TAB_ITEM:
                    total += 1
                elif kind != _DOCUMENT:
                    below.append(child)
        level = below
    return total


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


# ---------------------------------------------------------------------------
# Identities and the census (PLAN.md D8): a site's tab found again without its title
# ---------------------------------------------------------------------------

#: Identities kept per site: RuntimeIds (one per browser process lives), name digests, group places.
MAX_RUNTIME_IDS = 4
MAX_NAME_DIGESTS = 8
MAX_POSITIONS = 4
#: The identity kinds, strongest first.
MATCH_ORDER = ("runtime_id", "name", "position")


def sighting(tab: Tab, *, digest: str, pid: int, main_group: int | None, groups: int, at: float) -> dict[str, Any]:
    """One sighting of a site's selected tab as identities (content-free but for the caller's keyed name digest):
    its RuntimeId with the browser's pid, its name digest, and its place when it sits outside the main strip (a
    pinned or Essentials group, whose places are stable; a normal tab's place moves)."""
    out: dict[str, Any] = {"at": float(at), "digest": digest or "", "pid": int(pid or 0), "tab_id": tab.tab_id}
    if main_group is not None and tab.group != main_group:
        out["position"] = {"group": tab.group, "index": tab.index, "groups": int(groups)}
    return out


def learn_identity(stored: dict[str, Any] | None, seen: dict[str, Any]) -> dict[str, Any]:
    """``stored`` (a site's identities, as kept in ``nudge_state['site_tabs:<service>']``) with one more sighting:
    counts and last-seen times, each list capped (the least recently seen dropped first)."""
    ident = {"runtime_ids": list((stored or {}).get("runtime_ids") or []),
             "names": dict((stored or {}).get("names") or {}),
             "positions": list((stored or {}).get("positions") or [])}
    at = float(seen.get("at") or time.time())
    if seen.get("tab_id"):
        ident["runtime_ids"] = [r for r in ident["runtime_ids"]
                                if not (r.get("id") == seen["tab_id"] and r.get("pid") == seen.get("pid"))]
        ident["runtime_ids"].append({"id": seen["tab_id"], "pid": int(seen.get("pid") or 0), "at": at})
        ident["runtime_ids"] = sorted(ident["runtime_ids"], key=lambda r: r.get("at") or 0)[-MAX_RUNTIME_IDS:]
    if seen.get("digest"):
        entry = dict(ident["names"].get(seen["digest"]) or {"n": 0})
        entry["n"] = int(entry.get("n") or 0) + 1
        entry["at"] = at
        ident["names"][seen["digest"]] = entry
        if len(ident["names"]) > MAX_NAME_DIGESTS:
            oldest = sorted(ident["names"], key=lambda k: ident["names"][k].get("at") or 0)
            for d in oldest[: len(ident["names"]) - MAX_NAME_DIGESTS]:
                ident["names"].pop(d, None)
    pos = seen.get("position")
    if pos:
        found = next((p for p in ident["positions"] if (p.get("group"), p.get("index"), p.get("groups"))
                      == (pos.get("group"), pos.get("index"), pos.get("groups"))), None)
        if found is None:
            found = {**pos, "n": 0}
            ident["positions"].append(found)
        found["n"] = int(found.get("n") or 0) + 1
        found["at"] = at
        ident["positions"] = sorted(ident["positions"], key=lambda p: p.get("at") or 0)[-MAX_POSITIONS:]
    return ident


@dataclass
class Candidate:
    """A tab the census found that matches a learned identity: where it is and by which identity."""

    hwnd: int
    tab_id: str
    group: int
    index: int
    match: str
    score: int
    selected: bool = False

    def rank(self) -> tuple[int, int]:
        return (MATCH_ORDER.index(self.match), -self.score)


def rank_tabs(found: list[tuple[int, int, Tab, str]], identity: dict[str, Any] | None) -> list[Candidate]:
    """The tabs that match ``identity``, in identity order (RuntimeId of the same browser process, then name digest
    by how often it was seen, then a place outside the main strip), best first. ``found``: ``(hwnd, browser pid,
    tab, its name digest)`` per tab read (the name itself never reaches here)."""
    ident = identity or {}
    ids = {(r.get("id"), int(r.get("pid") or 0)) for r in ident.get("runtime_ids") or [] if r.get("id")}
    names = {d: int((v or {}).get("n") or 0) for d, v in (ident.get("names") or {}).items()}
    places = {(p.get("group"), p.get("index"), p.get("groups")): int(p.get("n") or 0)
              for p in ident.get("positions") or []}
    groups_per_window: dict[int, int] = {}
    for hwnd, _pid, tab, _d in found:
        groups_per_window[hwnd] = max(groups_per_window.get(hwnd, 0), tab.group + 1)
    out = []
    for hwnd, pid, tab, digest in found:
        place = (tab.group, tab.index, groups_per_window.get(hwnd, 0))
        if tab.tab_id and (tab.tab_id, int(pid or 0)) in ids:
            out.append(Candidate(hwnd, tab.tab_id, tab.group, tab.index, "runtime_id", 1, tab.selected))
        elif digest and digest in names:
            out.append(Candidate(hwnd, tab.tab_id, tab.group, tab.index, "name", names[digest], tab.selected))
        elif place in places:
            out.append(Candidate(hwnd, tab.tab_id, tab.group, tab.index, "position", places[place], tab.selected))
    return sorted(out, key=lambda c: c.rank())


def census_decision(candidates: list[Candidate]) -> tuple[str, list[Candidate]]:
    """``("one", [best])``; ``("ask", [the tied ones])`` when the best ones match by the same identity equally; or
    ``("none", [])`` (PLAN.md D8: two equal, ask; none, say no Meet tab is open)."""
    if not candidates:
        return "none", []
    best = candidates[0]
    tied = [c for c in candidates if c.rank() == best.rank()]
    return ("ask", tied) if len(tied) > 1 else ("one", [best])


def census_job(automation: Any, windows: list[tuple[int, int]], identity: dict[str, Any] | None,
               digest_fn: Any, *, budget_per_window: float = 0.2) -> dict[str, Any]:
    """Read every window's tabs (all groups, at most ``budget_per_window`` each), digest each name with
    ``digest_fn(name)`` and drop it, and rank them against ``identity``. Runs on the caller's UIA thread. Content-free
    result: ``{"candidates", "decision", "chosen", "windows", "tabs", "diagnostics" (per window), "ms"}``."""
    started = time.perf_counter()
    found: list[tuple[int, int, Tab, str]] = []
    diagnostics = []
    for hwnd, pid in windows:
        diag: dict[str, Any] = {}
        tabs, _nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + budget_per_window,
                                            all_groups=True, diagnostics=diag)
        diagnostics.append(diag)
        for tab, _element in tabs:
            try:
                digest = digest_fn(tab.name) if tab.name else ""
            except Exception:  # noqa: BLE001
                digest = ""
            found.append((int(hwnd), int(pid or 0), Tab(index=tab.index, name="", selected=tab.selected,
                                                        tab_id=tab.tab_id, group=tab.group,
                                                        group_size=tab.group_size), digest))
    candidates = rank_tabs(found, identity)
    decision, chosen = census_decision(candidates)
    return {"candidates": candidates, "decision": decision, "chosen": chosen, "windows": len(windows),
            "tabs": len(found), "diagnostics": diagnostics, "ms": round((time.perf_counter() - started) * 1000.0, 1)}


def read_census(windows: list[tuple[int, int]], identity: dict[str, Any] | None, digest_fn: Any, *,
                budget_per_window: float = 0.2) -> dict[str, Any]:
    """:func:`census_job` on its own UIA thread (the tray's side), bounded by the windows' budgets. Never raises."""
    budget = max(0.2, budget_per_window * max(len(windows), 1) + 0.1)
    empty: dict[str, Any] = {"candidates": [], "decision": "none", "chosen": [], "windows": len(windows), "tabs": 0,
                             "diagnostics": []}

    def job(automation: Any, shared: dict) -> None:
        shared["result"] = census_job(automation, windows, identity, digest_fn, budget_per_window=budget_per_window)

    try:
        shared, timed_out = _on_uia_thread(job, budget, "yuki-tab-census")
    except Exception as exc:  # noqa: BLE001
        return {**empty, "error": type(exc).__name__}
    return shared.get("result") or {**empty, "error": "timeout" if timed_out else "error"}


__all__ = ["Candidate", "MATCH_ORDER", "Tab", "TabStrip", "census_decision", "census_job", "format_tabs",
           "learn_identity", "rank_tabs", "read_census", "read_tabs", "runtime_id_text", "sighting", "tabs_line",
           "walk_tabs"]
