"""Select one tab of a window's own tab strip, and verify it is the one showing (docs/VOICE.md §5.6).

The strip is read as :func:`yuki.perception.tabs.walk_tabs` reads it. The tab is
selected through UI Automation, cursor and keyboard untouched: its
SelectionItem ``Select``, else its Invoke, else its LegacyIAccessible default
action. The effect is read back (a condition poll, bounded): the tab reports
itself selected. ``ok`` is True only then; the result names the tab, its place
and the window's title now. A tab list that changed since the caller saw it (the
name at that place is not the one it expected) is refused, never guessed at.

:func:`tab_action` is the brain's hand (the agent's ``tab`` tool): a tab found by its
identity (its UIA RuntimeId, :attr:`yuki.perception.tabs.Tab.tab_id`), selected or closed,
and the strip read again afterwards so what is said about it rests on that read.
"""

from __future__ import annotations

import time
from typing import Any

from yuki.actions import ActionResult

_PATTERN_INVOKE = 10000
_PATTERN_SELECTION_ITEM = 10010
_PATTERN_LEGACY = 10018
_P_SELECTION_ITEM_IS_SELECTED = 30079

#: How long the selection is watched for.
WATCH_S = 1.0
POLL_S = 0.03
BUDGET_S = 2.5


def _select(element: Any, module: Any) -> str:
    """Ask ``element`` to become the selected tab; the way that was accepted."""
    for pattern_id, iface, call, how in (
        (_PATTERN_SELECTION_ITEM, "IUIAutomationSelectionItemPattern", "Select", "SelectionItem.Select"),
        (_PATTERN_INVOKE, "IUIAutomationInvokePattern", "Invoke", "Invoke"),
        (_PATTERN_LEGACY, "IUIAutomationLegacyIAccessiblePattern", "DoDefaultAction", "default action"),
    ):
        try:
            raw = element.GetCurrentPattern(pattern_id)
            if not raw:
                continue
            getattr(raw.QueryInterface(getattr(module, iface)), call)()
            return how
        except Exception:  # noqa: BLE001 - try the next way
            continue
    return ""


def select_tab(hwnd: int, index: int, *, expect_name: str | None = None) -> ActionResult:
    """Make tab ``index`` (from 1, left to right) of ``hwnd``'s strip the one showing, verified."""
    from yuki.actions.input import _Pending
    from yuki.perception.tabs import walk_tabs
    from yuki.perception.tree import _uia_core
    from yuki.perception.windows import window_info

    started = time.perf_counter()
    details: dict[str, Any] = {"hwnd": int(hwnd), "index": int(index), "expect_name": expect_name}

    def finish(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    def job(automation: Any, shared: dict) -> None:
        found, nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + BUDGET_S * 0.4)
        shared["count"], shared["nodes"] = len(found), nodes
        if not 1 <= int(index) <= len(found):
            return
        tab, element = found[int(index) - 1]
        shared["tab"] = {"index": tab.index, "name": tab.name, "was_selected": tab.selected}
        if expect_name is not None and tab.name != expect_name:
            shared["changed"] = True
            return
        if tab.selected:
            shared["how"], shared["selected"] = "already showing", True
            return
        shared["how"] = _select(element, _uia_core())
        deadline = time.monotonic() + WATCH_S
        while time.monotonic() < deadline:
            try:
                if bool(element.GetCurrentPropertyValue(_P_SELECTION_ITEM_IS_SELECTED)):
                    shared["selected"] = True
                    return
            except Exception:  # noqa: BLE001
                break
            time.sleep(POLL_S)
        shared["selected"] = False

    pending = _Pending(job, "yuki-tab", BUDGET_S)
    shared = pending.wait()
    details.update({k: shared.get(k) for k in ("count", "nodes", "tab", "how", "selected", "error")})
    tab = shared.get("tab") or {}
    count = shared.get("count")
    if shared.get("error"):
        return finish(False, f"could not read the tabs of window {hwnd}: {shared['error']}")
    if pending.timed_out and not tab:
        return finish(False, f"the tabs of window {hwnd} did not answer within {BUDGET_S:g} s")
    if not count:
        return finish(False, f"window {hwnd} shows no tabs")
    if not tab:
        return finish(False, f"window {hwnd} has {count} tabs; there is no tab {index}")
    if shared.get("changed"):
        return finish(False, f"the tabs changed: tab {index} is now \"{tab['name']}\", not \"{expect_name}\"; "
                             f"nothing was selected")
    info = window_info(int(hwnd))
    title = info.title if info else ""
    details["title"] = title
    details["verified"] = bool(shared.get("selected"))
    where = f"tab {index} of {count}, \"{tab['name']}\""
    if shared.get("selected"):
        already = shared.get("how") == "already showing"
        return finish(True, f"{where} is {'already' if already else 'now'} the one showing "
                            f"(window title now \"{title}\")")
    how = shared.get("how") or "no way to select it was accepted"
    return finish(False, f"asked {where} to show ({how}), but it is not the selected tab after {WATCH_S:g} s")


# ---------------------------------------------------------------------------
# The brain's tab hand: a tab by its identity (UIA RuntimeId), never its title
# ---------------------------------------------------------------------------

_P_CONTROL_TYPE = 30003
#: Control type of a Button: a TabItem's own close control is its Button child.
_BUTTON = 50000
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_ELEMENT = 1
#: How long a closed tab is watched for leaving the strip (strip re-reads, a condition poll).
CLOSE_WATCH_S = 1.5
#: Bound on one tab action: find, act, watch, re-read the strip.
ACTION_BUDGET_S = 4.0
TAB_ACTIONS = ("select", "close")


def _close_control(automation: Any, element: Any) -> Any:
    """The TabItem's own close control: its Button child at the tab's trailing edge. ``None`` if unsure.

    One Button child is that control. A tab playing sound has a second one, the browser's sound indicator
    (live 2026-09-27 14:18 and 14:19 both closes of a playing YouTube tab were refused as "no single close
    control"): the close control is the one that sits last along the tab, the right-most (measured pixels, never
    its localized name). With several buttons and no positions to tell them apart, nothing is chosen.
    """
    return pick_close_control(_button_children(automation, element))


def _button_children(automation: Any, element: Any) -> list[tuple[Any, tuple[int, int, int, int] | None]]:
    """``(element, (left, top, right, bottom) or None)`` of a TabItem's Button children, in tree order."""
    from yuki.perception.tabs import _P_BOUNDING_RECT, _rect

    request = automation.CreateCacheRequest()
    request.AddProperty(_P_CONTROL_TYPE)
    request.AddProperty(_P_BOUNDING_RECT)
    request.TreeScope = _TREE_SCOPE_ELEMENT
    try:
        children = element.FindAllBuildCache(_TREE_SCOPE_CHILDREN, automation.ControlViewCondition, request)
        items = [children.GetElement(i) for i in range(children.Length)]
        out: list[tuple[Any, tuple[int, int, int, int] | None]] = []
        for c in items:
            if int(c.GetCachedPropertyValue(_P_CONTROL_TYPE) or 0) != _BUTTON:
                continue
            try:
                rect = _rect(c.GetCachedPropertyValue(_P_BOUNDING_RECT))
            except Exception:  # noqa: BLE001 - no position: still a candidate when it is the only one
                rect = None
            out.append((c, rect if rect and rect[2] > rect[0] and rect[3] > rect[1] else None))
        return out
    except Exception:  # noqa: BLE001 - a tab that went away mid-read
        return []


def pick_close_control(buttons: list[tuple[Any, Any]]) -> Any:
    """The close control among a tab's buttons: the only one, else the right-most by its on-screen box.

    ``buttons`` are ``(element, (left, top, right, bottom) or None)``. ``None`` when there are none, or several
    without boxes to tell them apart.
    """
    if len(buttons) == 1:
        return buttons[0][0]
    if len(buttons) < 2 or any(r is None for _b, r in buttons):
        return None
    return max(buttons, key=lambda br: br[1][2])[0]


def _invoke(element: Any, module: Any) -> bool:
    try:
        raw = element.GetCurrentPattern(_PATTERN_INVOKE)
        if not raw:
            return False
        raw.QueryInterface(module.IUIAutomationInvokePattern).Invoke()
        return True
    except Exception:  # noqa: BLE001
        return False


def _rows(found: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [{"index": t.index, "group": t.group, "id": t.tab_id, "name": t.name, "selected": t.selected}
            for t, _e in found]


def tab_action(hwnd: int, tab_id: str, action: str) -> ActionResult:
    """Select or close the tab of ``hwnd``'s strip whose identity is ``tab_id``, then re-read the strip.

    ``tab_id`` is a :attr:`yuki.perception.tabs.Tab.tab_id` from a read of the strip (the
    TabItem's UIA RuntimeId), from any of the strip's groups (a pinned tab's too): the tab is
    found by it, whatever its title or place now, and
    one that is no longer there is reported, never replaced by another. ``select`` asks it
    to show (as :func:`select_tab`) and watches it report itself selected; ``close`` invokes
    its own close control (its single Button child; with none or several nothing is done)
    and watches it leave the strip. Either way the strip is read again at the end and
    ``details["tabs_after"]`` lists what is there now, so what is said about it rests on
    that read. ``ok`` only when the effect was seen. Cursor and keyboard are untouched.
    """
    from yuki.actions.input import _Pending
    from yuki.perception.tabs import walk_tabs
    from yuki.perception.tree import _uia_core
    from yuki.perception.windows import window_info

    started = time.perf_counter()
    tab_id = str(tab_id or "").strip().strip("[]").strip()
    details: dict[str, Any] = {"hwnd": int(hwnd), "tab_id": tab_id, "action": action}

    def finish(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    if action not in TAB_ACTIONS:
        return finish(False, f"unknown tab action {action!r}: use one of {', '.join(TAB_ACTIONS)}")
    if not tab_id:
        return finish(False, "no tab id given: read the tabs first and pass one of their ids")

    def job(automation: Any, shared: dict) -> None:
        # a tab id from any group of the strip (PLAN.md D8: a pinned or Essentials tab sits in a smaller group)
        found, nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + ACTION_BUDGET_S * 0.25,
                                            all_groups=True)
        shared["count"], shared["nodes"] = len(found), nodes
        match = next(((t, e) for t, e in found if t.tab_id and t.tab_id == tab_id), None)
        if match is None:
            shared["after"] = _rows(found)
            return
        tab, element = match
        shared["tab"] = {"index": tab.index, "id": tab.tab_id, "name": tab.name, "was_selected": tab.selected}
        module = _uia_core()
        if action == "select":
            if tab.selected:
                shared["how"], shared["seen"] = "already showing", True
            else:
                shared["how"] = _select(element, module)
                deadline = time.monotonic() + WATCH_S
                while shared["how"] and time.monotonic() < deadline:
                    try:
                        if bool(element.GetCurrentPropertyValue(_P_SELECTION_ITEM_IS_SELECTED)):
                            shared["seen"] = True
                            break
                    except Exception:  # noqa: BLE001
                        break
                    time.sleep(POLL_S)
        else:
            control = _close_control(automation, element)
            if control is None:
                shared["no_control"] = True
            elif _invoke(control, module):
                shared["how"] = "its close control"
                deadline = time.monotonic() + CLOSE_WATCH_S
                while time.monotonic() < deadline:
                    now, _n, _c = walk_tabs(automation, int(hwnd), time.monotonic() + 0.3, all_groups=True)
                    if not any(t.tab_id == tab_id for t, _e in now):
                        shared["seen"] = True
                        break
                    time.sleep(POLL_S)
        after, _n, complete = walk_tabs(automation, int(hwnd), time.monotonic() + ACTION_BUDGET_S * 0.2,
                                        all_groups=True)
        shared["after"], shared["after_complete"] = _rows(after), complete

    pending = _Pending(job, "yuki-tab-action", ACTION_BUDGET_S)
    shared = pending.wait()
    tab = shared.get("tab") or {}
    after = list(shared.get("after") or [])
    details.update({"count": shared.get("count"), "nodes": shared.get("nodes"), "tab": tab or None,
                    "how": shared.get("how"), "verified": bool(shared.get("seen")), "tabs_after": after,
                    "tabs_after_complete": bool(shared.get("after_complete", True))})
    if shared.get("error"):
        return finish(False, f"could not read the tabs of window {hwnd}: {shared['error']}")
    if pending.timed_out and not tab:
        return finish(False, f"the tabs of window {hwnd} did not answer within {ACTION_BUDGET_S:g} s")
    if not shared.get("count"):
        return finish(False, f"window {hwnd} shows no tabs")
    if not tab:
        return finish(False, f"window {hwnd} has no tab with id {tab_id} now (closed, or the strip changed); "
                             f"nothing was done; its {len(after)} tabs now are listed below")
    where = f"tab \"{tab['name']}\" [{tab_id}]"
    if action == "select":
        if not shared.get("seen"):
            how = shared.get("how") or "no way to select it was accepted"
            return finish(False, f"asked {where} to show ({how}), but it is not the selected tab after {WATCH_S:g} s")
        info = window_info(int(hwnd))
        details["title"] = info.title if info else ""
        already = shared.get("how") == "already showing"
        return finish(True, f"{where} is {'already' if already else 'now'} the one showing "
                            f"(window title now \"{details['title']}\")")
    if shared.get("no_control"):
        return finish(False, f"{where} has no single close control of its own; nothing was closed")
    if not shared.get("how"):
        return finish(False, f"{where}: its close control did not accept the request; nothing was closed")
    if not shared.get("seen"):
        return finish(False, f"asked {where} to close, but it is still in the strip after {CLOSE_WATCH_S:g} s")
    return finish(True, f"closed {where}; the strip, read again, has {len(after)} tabs")
