"""UI Automation patterns before the global mouse and keyboard (revamp D, S7 "quieter hands"; 11 §12 #4, #9).

``tabs.py`` already selects a browser tab through its SelectionItem pattern, with the cursor and keyboard untouched.
This generalises that: an element from the brain's latest read of a window that supports a pattern is acted on through
the pattern - **Invoke** (a button, a link), **Toggle** (a checkbox, a switch), **SelectionItem.Select** (a tab, a
list item), **Value.SetValue** (replacing a field's contents) and **Scroll** (a scrollable pane) - so the user's mouse
does not move, nothing steals focus, and no global input lease is needed (only the window's own,
:func:`yuki.actions.leases.window_lease`).

The live element is found again at the read's point (``ElementFromPoint``, then up to a few ancestors) and must match
the read by role and name and sit inside the same window; anything else returns ``None`` and the caller falls back to
the mouse or keys (never a guess). A pattern call that raised before taking effect falls back too; one that ran is
reported with what it measured (a selection or toggle state read back, a value read back, a scroll position, a window
title or new window after an Invoke), never retried by the mouse (a second activation could send twice).

Which pattern a verb uses follows the element's own pattern list from the read (fixed UIA facts), in a fixed order;
nothing here knows which program drew the window.
"""

from __future__ import annotations

import time
from typing import Any

from yuki.actions import ActionResult

_PATTERN_INVOKE = 10000
_PATTERN_VALUE = 10002
_PATTERN_SCROLL = 10004
_PATTERN_SELECTION_ITEM = 10010
_PATTERN_TOGGLE = 10015
_UIA_BOUNDING_RECT = 30001
_UIA_CONTROL_TYPE = 30003
_UIA_NAME = 30005
_UIA_NATIVE_WINDOW_HANDLE = 30020
_TREE_SCOPE_ELEMENT = 1
_SCROLL_SMALL_DECREMENT, _SCROLL_NO_AMOUNT, _SCROLL_SMALL_INCREMENT = 1, 2, 4
#: Small scroll steps per wheel notch (a wheel notch scrolls about three lines).
STEPS_PER_NOTCH = 3
#: Ancestors climbed from the element at the point to the one the read named, and for a scrollable container.
MATCH_LEVELS = 4
SCROLL_LEVELS = 8
#: Bounds of the whole pattern action, and how long its effect is watched (s).
BUDGET_S = 2.0
WATCH_S = 0.6
POLL_S = 0.05
#: How far (px) the live element's rectangle may sit from the read's before it is not the same element.
BOUNDS_SLACK = 24
#: The job does not start acting this close to the end of its budget (s), so an answer that comes after the caller
#: gave up (and may have clicked with the mouse instead) never acts a second time.
ACT_MARGIN_S = 0.3
_UIA_RUNTIME_ID = 30000
_UIA_TOGGLE_STATE = 30086
_TREE_SCOPE_SUBTREE = 7


def by_runtime_id(automation: Any, element: dict[str, Any], start: Any, root: int, request: Any) -> Any:
    """The live element with the read element's RuntimeId (report 58), searched below ``start`` (the element at the
    read's point: on Chromium the renderer's pane, an ancestor of every web control) when it lies in the same window,
    else below the window's own root; ``None`` when the read has no RuntimeId or nothing has it now. The match is
    checked by role too (an id the provider gave to a new element after the old one went is not that element)."""
    import array

    rid = element.get("runtime_id")
    if not rid:
        return None
    try:
        condition = automation.CreatePropertyCondition(_UIA_RUNTIME_ID, array.array("i", [int(p) for p in rid]))
    except Exception:  # noqa: BLE001
        return None
    starts = []
    if start:
        try:
            native = int(_facts(start)["native"] or 0)
            if not native or _root(native) == root:
                starts.append(start)
        except Exception:  # noqa: BLE001
            pass
    try:
        starts.append(automation.ElementFromHandleBuildCache(int(root), request))
    except Exception:  # noqa: BLE001
        pass
    for base in starts:
        try:
            found = base.FindFirstBuildCache(_TREE_SCOPE_SUBTREE, condition, request)
        except Exception:  # noqa: BLE001
            continue
        if found and _facts(found)["role"] == str(element.get("role") or ""):
            return found
    return None

#: The verb -> the patterns that carry it, in order of preference (the read's pattern labels, yuki.perception.tree).
VERB_PATTERNS: dict[str, tuple[str, ...]] = {
    "click": ("invoke", "toggle", "select"),
    "set_value": ("value",),
    "scroll": ("scroll",),
}


def usable(element: dict[str, Any] | None, verb: str) -> str | None:
    """The pattern a read element offers for ``verb`` (from the read's own pattern list), or ``None``."""
    if not isinstance(element, dict):
        return None
    patterns = set(element.get("patterns") or ())
    if verb == "scroll":
        return "scroll" if element.get("is_scrollable") or "scroll" in patterns else None
    for label in VERB_PATTERNS.get(verb, ()):
        if label in patterns:
            return label
    return None


def _role(control_type: Any) -> str:
    try:
        from yuki.perception.tree import _role_name

        return _role_name(control_type)
    except Exception:  # noqa: BLE001
        return f"ControlType{control_type}"


def _facts(element: Any) -> dict[str, Any]:
    get = element.GetCachedPropertyValue
    try:
        rect = element.CachedBoundingRectangle
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:  # noqa: BLE001
        bounds = (0, 0, 0, 0)
    name = get(_UIA_NAME)
    try:
        native = int(get(_UIA_NATIVE_WINDOW_HANDLE) or 0)
    except Exception:  # noqa: BLE001
        native = 0
    return {"role": _role(get(_UIA_CONTROL_TYPE)), "name": name.strip() if isinstance(name, str) else "",
            "bounds": bounds, "native": native}


def _same(live: dict[str, Any], read: dict[str, Any]) -> bool:
    """The live element is the one the read named: same role and name, a rectangle near the read's."""
    if live["role"] != str(read.get("role") or "") or live["name"] != str(read.get("name") or "").strip():
        return False
    want = read.get("bounds")
    if not want or len(want) != 4:
        return True
    return all(abs(int(a) - int(b)) <= BOUNDS_SLACK for a, b in zip(live["bounds"], want))


def _root(hwnd: int) -> int:
    try:
        import win32gui

        return int(win32gui.GetAncestor(int(hwnd), 2) or hwnd)
    except Exception:  # noqa: BLE001
        return int(hwnd)


def _title(hwnd: int) -> str | None:
    try:
        import win32gui

        return win32gui.GetWindowText(int(hwnd)) if win32gui.IsWindow(int(hwnd)) else None
    except Exception:  # noqa: BLE001
        return None


def _watch(automation: Any, element: Any, property_ids: tuple[int, ...]) -> Any:
    """The element's own property-changed events while a pattern acts (percFix), ``None`` when it gives none."""
    try:
        from yuki.perception.events import ElementWatch

        watch = ElementWatch(automation, element, property_ids)
    except Exception:  # noqa: BLE001
        return None
    return watch if watch.live else None


def _budget_left(stop_at: float) -> float:
    """How long the job may still wait for an effect: up to :data:`WATCH_S`, never past its budget."""
    return max(0.0, min(WATCH_S, stop_at + ACT_MARGIN_S - 0.1 - time.monotonic()))


def element_action(hwnd: int, element: dict[str, Any], verb: str, *, value: str | None = None, dy: int = 0,
                   dx: int = 0) -> ActionResult | None:
    """Act on a read element through its pattern; ``None`` when that cannot be done (the caller falls back).

    Args:
        hwnd: The window the read came from.
        element: The read's element (role, name, center, bounds, patterns, is_scrollable).
        verb: ``click`` | ``set_value`` | ``scroll``.
        value: ``set_value``: the field's new contents.
        dy, dx: ``scroll``: wheel notches (positive dy is up).
    """
    label = usable(element, verb)
    if label is None:
        return None
    from yuki.actions.input import _Pending, _process_top_windows, _window_pid, _windows_appeared
    from yuki.actions.leases import LeaseBusy, window_lease

    started = time.perf_counter()
    center = element.get("center") or ()
    try:
        x, y = int(center[0]), int(center[1])
    except (TypeError, ValueError, IndexError):
        return None
    details: dict[str, Any] = {"via": "uia_pattern", "pattern": label, "hwnd": int(hwnd),
                               "element": {"role": element.get("role"), "name": str(element.get("name") or "")[:80]}}
    root = _root(hwnd)
    title_before = _title(root)
    pid = _window_pid(root) if verb == "click" else 0
    windows_before = _process_top_windows(pid) if pid else {}
    #: Past this the job never starts acting (the caller has given up waiting for it at BUDGET_S).
    stop_at = time.monotonic() + BUDGET_S - ACT_MARGIN_S

    def job(automation: Any, shared: dict) -> None:
        from ctypes import wintypes

        from yuki.perception.tree import _uia_core

        module = _uia_core()
        request = automation.CreateCacheRequest()
        for prop in (_UIA_CONTROL_TYPE, _UIA_NAME, _UIA_NATIVE_WINDOW_HANDLE, _UIA_BOUNDING_RECT):
            request.AddProperty(prop)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        at_point = current = automation.ElementFromPointBuildCache(wintypes.POINT(x, y), request)
        walker = automation.ControlViewWalker
        found = None
        for _ in range(MATCH_LEVELS + 1):
            if not current:
                break
            facts = _facts(current)
            if _same(facts, element):
                found = current
                break
            current = walker.GetParentElementBuildCache(current, request)
        if found is None:
            # report 58: on Chromium/CEF the point answers with the renderer's whole pane, an ancestor of every web
            # control, so the climb above never meets the control; its RuntimeId finds it below that pane
            found = by_runtime_id(automation, element, at_point, root, request)
        if found is None:
            shared["missing"] = "no element at its point matches the read (it moved, or something covers it)"
            return
        if time.monotonic() >= stop_at:
            # found too late: the caller has stopped waiting and may use the mouse; never act twice
            shared["missing"] = "finding it took too long"
            return
        try:
            import win32gui

            owner = int(win32gui.GetAncestor(int(_facts(found)["native"] or 0), 2) or 0) if _facts(found)["native"] \
                else 0
        except Exception:  # noqa: BLE001
            owner = 0
        if owner and owner != root:
            shared["missing"] = "the element at its point belongs to another window"
            return
        target = found
        if verb == "scroll":
            target = None
            current = found
            for _ in range(SCROLL_LEVELS + 1):
                if not current:
                    break
                raw = current.GetCurrentPattern(_PATTERN_SCROLL)
                if raw:
                    target = current
                    break
                current = walker.GetParentElementBuildCache(current, request)
            if target is None:
                shared["missing"] = "no scrollable container around the element"
                return
        try:
            if label == "invoke":
                pattern = target.GetCurrentPattern(_PATTERN_INVOKE).QueryInterface(module.IUIAutomationInvokePattern)
                shared["acting"] = True
                pattern.Invoke()
                shared["done"] = "Invoke"
            elif label == "toggle":
                pattern = target.GetCurrentPattern(_PATTERN_TOGGLE).QueryInterface(module.IUIAutomationTogglePattern)
                before = int(pattern.CurrentToggleState)
                watch = _watch(automation, target, (_UIA_TOGGLE_STATE,))
                try:
                    shared["acting"] = True
                    pattern.Toggle()
                    shared["done"] = "Toggle"
                    after = int(pattern.CurrentToggleState)
                    # percFix: Chromium and CEF report the new state a moment after Toggle returns (live 5 Oct:
                    # "its state did not change" for a Lyrics toggle that had worked); wait for its own event,
                    # within the budget, never a timed poll
                    if after == before and watch is not None and watch.wait(_budget_left(stop_at)):
                        after = int(pattern.CurrentToggleState)
                finally:
                    if watch is not None:
                        watch.close()
                shared["toggle"] = (before, after)
            elif label == "select":
                pattern = target.GetCurrentPattern(_PATTERN_SELECTION_ITEM).QueryInterface(
                    module.IUIAutomationSelectionItemPattern)
                shared["acting"] = True
                pattern.Select()
                shared["done"] = "SelectionItem.Select"
                deadline = time.monotonic() + WATCH_S
                selected = bool(pattern.CurrentIsSelected)
                while not selected and time.monotonic() < deadline:
                    time.sleep(POLL_S)
                    selected = bool(pattern.CurrentIsSelected)
                shared["selected"] = selected
            elif label == "value":
                pattern = target.GetCurrentPattern(_PATTERN_VALUE).QueryInterface(module.IUIAutomationValuePattern)
                if bool(pattern.CurrentIsReadOnly):
                    shared["missing"] = "the field's value is read-only"
                    return
                shared["acting"] = True
                pattern.SetValue(str(value or ""))
                shared["done"] = "Value.SetValue"
                shared["readback"] = str(pattern.CurrentValue or "")
            elif label == "scroll":
                pattern = target.GetCurrentPattern(_PATTERN_SCROLL).QueryInterface(module.IUIAutomationScrollPattern)
                before = (float(pattern.CurrentVerticalScrollPercent), float(pattern.CurrentHorizontalScrollPercent))
                shared["acting"] = True
                for _ in range(abs(int(dy)) * STEPS_PER_NOTCH):
                    pattern.Scroll(_SCROLL_NO_AMOUNT,
                                   _SCROLL_SMALL_DECREMENT if dy > 0 else _SCROLL_SMALL_INCREMENT)
                for _ in range(abs(int(dx)) * STEPS_PER_NOTCH):
                    pattern.Scroll(_SCROLL_SMALL_INCREMENT if dx > 0 else _SCROLL_SMALL_DECREMENT,
                                   _SCROLL_NO_AMOUNT)
                shared["done"] = "Scroll"
                after = (float(pattern.CurrentVerticalScrollPercent), float(pattern.CurrentHorizontalScrollPercent))
                shared["scroll"] = (before, after)
        except Exception as exc:  # noqa: BLE001
            shared["error"] = f"{type(exc).__name__}: {exc}"

    try:
        with window_lease(int(root)):
            pending = _Pending(job, "yuki-uia-pattern", BUDGET_S)
            shared = pending.wait()
    except LeaseBusy:
        return None
    if shared.get("missing") or (shared.get("error") and not shared.get("acting")) or not shared.get("done"):
        if shared.get("acting") and not shared.get("done"):
            # it may have taken effect before failing: never act again by the mouse
            details["error"] = shared.get("error") or "the pattern call did not finish"
            return ActionResult(ok=False, summary=f"{label} through UI Automation did not finish "
                                                  f"({details['error']}); not repeated by the mouse",
                                details=details, elapsed_ms=(time.perf_counter() - started) * 1000)
        return None
    how = shared["done"]
    details["how"] = how
    name = f"{element.get('role') or 'element'} \"{str(element.get('name') or '')[:60]}\""
    summary = f"{how} on {name} through UI Automation (the mouse and keyboard were not used)"
    if label == "toggle":
        before, after = shared.get("toggle", (None, None))
        changed = before is not None and after is not None and before != after
        if changed:
            details["verified"] = True
            summary += "; its state changed"
        else:
            # percFix: not "did not change": an app can show the new state later than the pattern call returns (or
            # draw the control again); the read of the window after it decides
            summary += "; its state did not read as changed right away"
    elif label == "select":
        details["verified"] = bool(shared.get("selected"))
        summary += "; it reads selected" if details["verified"] else "; it does not read selected"
    elif label == "value":
        held = shared.get("readback") or ""
        details["verified"] = " ".join(held.split()) == " ".join(str(value or "").split())
        details["readback"] = "arrived" if details["verified"] else "missing"
        summary += "; the field reads back the text" if details["verified"] else "; the field does not hold the text"
    elif label == "scroll":
        before, after = shared.get("scroll", ((0.0, 0.0), (0.0, 0.0)))
        moved = before != after
        details["verified"] = moved
        details["scroll_percent"] = {"before": before, "after": after}
        summary += "; it moved" if moved else "; its position did not change (at the end, or it ignores Scroll)"
    else:
        # An Invoke shows only through what it caused: the window's title, a new window of its app. What did not
        # change is not listed (focus never moves with a pattern), so an unseen effect reads "could not tell".
        deadline = time.monotonic() + WATCH_S
        title_after = _title(root)
        appeared: list = []
        while time.monotonic() < deadline:
            title_after = _title(root)
            appeared = _windows_appeared(pid, windows_before) if pid else []
            if title_after != title_before or appeared:
                break
            time.sleep(POLL_S)
        if title_after is not None and title_after != title_before:
            details["foreground_title_changed"] = True
            summary += f"; the window's title is now \"{title_after[:80]}\""
        if appeared:
            details["new_windows"] = appeared
            summary += f"; {len(appeared)} new window(s) appeared"
    return ActionResult(ok=True, summary=summary, details=details, elapsed_ms=(time.perf_counter() - started) * 1000)


__all__ = ["VERB_PATTERNS", "element_action", "usable"]
