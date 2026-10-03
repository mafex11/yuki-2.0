"""A real slider hand (report 47, N2): set a slider by UI Automation's RangeValue, else by its own keys, read back.

Live 3 Oct the user said Spotify's own volume slider was low. For about five minutes Yuki hovered, clicked coordinates
on a player bar that was hidden, scrolled the lyrics instead of the volume, ran two PowerShell ``mouse_event`` drag
scripts and tried to click Mute. There was no way to set a slider: the patterns covered Invoke, Toggle, Select,
Value.SetValue and Scroll, not RangeValue.

:func:`set_slider` takes an element of the brain's latest read (the slider itself, or the control that holds it: a web
player often shows a labelled bar and keeps the real ``<input type=range>`` hidden inside it) and:

1. finds the live element again at the read's point, and the RangeValue element there - the element itself, else the
   RangeValue element inside it or beside it whose box is nearest the read's (a page may hold several sliders: the
   playback position and the volume; the nearest to the bar the brain named wins), reads its minimum, maximum and
   value, sets the new value with ``RangeValue.SetValue`` and reads it back;
2. when the value cannot be set that way (read-only, or it did not move), gives the slider keyboard focus (UI Automation
   ``SetFocus``) and presses its own arrow keys toward the target, reading the value back after each few presses, and
   stops at the target or as soon as presses stop moving it;
3. when nothing there exposes a value, does nothing and says so: no drag, no click along the bar, no script.

A target already reached is a no-op, reported as such. An app's own keyboard path (a player's volume shortcut) is not
known here: practice memory learns it from a run that worked, never a list in code.

Which element is the slider is fixed UI Automation facts (patterns, boxes); nothing here knows which program drew it.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Protocol

from yuki.actions import ActionResult

_PATTERN_RANGE_VALUE = 10003
_P_IS_RANGE_VALUE_AVAILABLE = 30033
_UIA_BOUNDING_RECT = 30001
_UIA_CONTROL_TYPE = 30003
_UIA_NAME = 30005
_UIA_NATIVE_WINDOW_HANDLE = 30020
_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_SUBTREE = 7
#: Ancestors climbed to match the read's element, and levels above it searched for a RangeValue element.
MATCH_LEVELS = 4
SEARCH_UP = 2
#: The whole job's budget (s: the keys path, up to MAX_PRESSES with a read-back after each batch and the input gate's
#: wait before each, takes longer than one SetValue), and how long a set value is watched for its read-back.
BUDGET_S = 8.0
SETTLE_S = 0.4
POLL_S = 0.05
#: Keys: presses per batch, the most presses in all, and how close (share of the range) counts as reached.
KEYS_PER_BATCH = 5
MAX_PRESSES = 60
TOLERANCE_SHARE = 0.02

class Slider(Protocol):
    """One live slider: its range, value and the two ways to move it (tests give a fake)."""

    def read(self) -> tuple[float, float, float, bool, float]: ...   # value, minimum, maximum, read-only, small step
    def set(self, value: float) -> None: ...
    def focus(self) -> None: ...


def percent(value: float, low: float, high: float) -> float:
    """``value`` as a percent of the range (0 when the range is empty)."""
    return 0.0 if high <= low else max(0.0, min(100.0, (value - low) * 100.0 / (high - low)))


def target_value(value: float, low: float, high: float, *, to: float | None, by: float | None) -> float:
    """The value to reach: ``to`` percent of the range, else the value moved by ``by`` percent points; clamped."""
    span = high - low
    if to is not None:
        want = low + span * float(to) / 100.0
    else:
        want = value + span * float(by or 0.0) / 100.0
    return max(low, min(high, want))


def drive(slider: Slider, *, to: float | None = None, by: float | None = None,
          press: Callable[[str, int], Any] | None = None, name: str = "the slider",
          sleep: Callable[[float], None] = time.sleep, settle_s: float = SETTLE_S) -> tuple[bool, str, dict[str, Any]]:
    """Move one live slider to its target and read it back: ``(ok, summary, details)``. RangeValue.SetValue first;
    when that cannot move it, focus and the arrow keys (``press(key, times)``) with a read-back after each batch."""
    details: dict[str, Any] = {"verified": False}
    value, low, high, read_only, small = slider.read()
    details.update(before=round(value, 4), minimum=low, maximum=high, before_percent=round(percent(value, low, high), 1))
    if high <= low:
        return False, f"{name} reports an empty range ({low:g}-{high:g}); nothing was changed", details
    want = target_value(value, low, high, to=to, by=by)
    tolerance = max(abs(small) if small else 0.0, (high - low) * TOLERANCE_SHARE)
    details.update(target=round(want, 4), target_percent=round(percent(want, low, high), 1))
    if abs(want - value) <= tolerance / 2:
        details.update(verified=True, noop=True, after=round(value, 4), after_percent=details["before_percent"])
        end = " (its end)" if want in (low, high) else ""
        return True, (f"{name} is already at {details['before_percent']:g}% of its range{end}: nothing to move, "
                      f"nothing was changed"), details
    after = value
    if not read_only:
        try:
            slider.set(want)
            details["via"] = "RangeValue"
            deadline = time.monotonic() + settle_s
            while True:
                after = slider.read()[0]
                if abs(after - want) <= tolerance or time.monotonic() >= deadline:
                    break
                sleep(POLL_S)
        except Exception as exc:  # noqa: BLE001 - the keys then
            details["set_error"] = f"{type(exc).__name__}: {exc}"[:160]
    if abs(after - want) > tolerance and press is not None:
        details["via"] = "keys"
        try:
            slider.focus()
        except Exception as exc:  # noqa: BLE001
            details["focus_error"] = f"{type(exc).__name__}: {exc}"[:160]
        key = "right" if want > after else "left"
        step = abs(small) if small else (high - low) / 100.0
        close = max(step / 2.0, (high - low) * 0.005)   # within half a key step of the target: as near as keys get
        presses = 0
        while presses < MAX_PRESSES and abs(after - want) > close:
            need = max(1, min(KEYS_PER_BATCH, math.ceil(abs(want - after) / max(step, 1e-9))))
            before_batch = after
            try:
                press(key, need)
            except Exception as exc:  # noqa: BLE001 - the user took the keyboard, or the window left the front
                details["keys_error"] = f"{type(exc).__name__}: {exc}"[:160]
                break
            presses += need
            sleep(POLL_S)
            after = slider.read()[0]
            if abs(after - before_batch) < 1e-9:
                details["keys_stuck"] = True
                break
            if (key == "right" and after > want) or (key == "left" and after < want):
                break   # one press past it: close enough within a step
        details["presses"] = presses
    details.update(after=round(after, 4), after_percent=round(percent(after, low, high), 1))
    reached = abs(after - want) <= max(tolerance, abs(small) if small else 0.0)
    details["verified"] = bool(reached)
    how = {"RangeValue": "its UI Automation value", "keys": "its own arrow keys"}.get(details.get("via"), "nothing")
    moved = f"{details['before_percent']:g}% -> {details['after_percent']:g}% of its range"
    if reached:
        return True, f"set {name} by {how}: {moved}, read back (the mouse was not used)", details
    if abs(after - value) > 1e-9:
        return False, (f"moved {name} by {how} but not to the target ({details['target_percent']:g}%): {moved}, "
                       f"read back"), details
    return False, (f"{name} did not move ({moved}): its value is "
                   f"{'read-only' if read_only else 'not taking the new level'}"
                   f"{' and its arrow keys did not move it either' if details.get('via') == 'keys' else ''}. "
                   f"Nothing was dragged or clicked"), details


# ---------------------------------------------------------------------------
# The live slider (UI Automation)
# ---------------------------------------------------------------------------


class _LiveSlider:
    """A RangeValue element, read and set on the UI Automation thread that found it."""

    def __init__(self, element: Any, pattern: Any) -> None:
        self.element = element
        self.pattern = pattern

    def read(self) -> tuple[float, float, float, bool, float]:
        p = self.pattern
        try:
            small = float(p.CurrentSmallChange)
        except Exception:  # noqa: BLE001
            small = 0.0
        return (float(p.CurrentValue), float(p.CurrentMinimum), float(p.CurrentMaximum), bool(p.CurrentIsReadOnly),
                small)

    def set(self, value: float) -> None:
        self.pattern.SetValue(float(value))

    def focus(self) -> None:
        self.element.SetFocus()


def _rect(element: Any) -> tuple[int, int, int, int] | None:
    try:
        r = element.CurrentBoundingRectangle
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:  # noqa: BLE001
        return None


def _distance(rect: tuple[int, int, int, int] | None, want: Any) -> float:
    """How far a candidate's box centre is from the read's box (0 inside it)."""
    if rect is None or not want or len(want) != 4:
        return math.inf
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    dx = max(int(want[0]) - cx, 0.0, cx - int(want[2]))
    dy = max(int(want[1]) - cy, 0.0, cy - int(want[3]))
    return math.hypot(dx, dy)


def set_slider(hwnd: int, element: dict[str, Any], *, to: float | None = None, by: float | None = None,
               press: Callable[[str, int], Any] | None = None) -> ActionResult:
    """The real slider hand (see the module docstring). ``press(key, times)`` sends keys to the slider's window (the
    caller's guarded key action); ``None``: RangeValue only."""
    started = time.perf_counter()
    name = f"{element.get('role') or 'element'} \"{str(element.get('name') or '')[:60]}\""
    base: dict[str, Any] = {"hwnd": int(hwnd), "element": {"role": element.get("role"),
                                                            "name": str(element.get("name") or "")[:80]},
                            "verified": False}

    def result(ok: bool, summary: str, details: dict[str, Any]) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details={**base, **details},
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    if to is None and by is None:
        return result(False, "give to (0-100 percent of its range) or by (percent points)", {})
    center = element.get("center") or ()
    try:
        x, y = int(center[0]), int(center[1])
    except (TypeError, ValueError, IndexError):
        return result(False, f"{name} has no position in the read", {})
    from yuki.actions.input import _Pending
    from yuki.actions.patterns import _facts, _same

    def job(automation: Any, shared: dict) -> None:
        from ctypes import wintypes

        from yuki.perception.tree import _uia_core

        module = _uia_core()
        request = automation.CreateCacheRequest()
        for prop in (_UIA_CONTROL_TYPE, _UIA_NAME, _UIA_NATIVE_WINDOW_HANDLE, _UIA_BOUNDING_RECT):
            request.AddProperty(prop)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        current = automation.ElementFromPointBuildCache(wintypes.POINT(x, y), request)
        walker = automation.ControlViewWalker
        found = None
        for _ in range(MATCH_LEVELS + 1):
            if not current:
                break
            if _same(_facts(current), element):
                found = current
                break
            current = walker.GetParentElementBuildCache(current, request)
        if found is None:
            shared["missing"] = "no element at its point matches the read (it moved, is hidden, or something covers it)"
            return
        condition = automation.CreatePropertyCondition(_P_IS_RANGE_VALUE_AVAILABLE, True)
        candidates: list[Any] = []
        scope = found
        for _ in range(SEARCH_UP + 1):
            if not scope:
                break
            try:
                array = scope.FindAll(_TREE_SCOPE_SUBTREE, condition)
                for i in range(array.Length if array else 0):
                    candidates.append(array.GetElement(i))
            except Exception:  # noqa: BLE001
                pass
            if candidates:
                break
            scope = walker.GetParentElement(scope)
        if not candidates:
            shared["missing"] = "no slider value under it (UI Automation shows no RangeValue there)"
            return
        best = min(candidates, key=lambda c: _distance(_rect(c), element.get("bounds")))
        pattern = best.GetCurrentPattern(_PATTERN_RANGE_VALUE).QueryInterface(module.IUIAutomationRangeValuePattern)
        shared["acting"] = True
        ok, summary, details = drive(_LiveSlider(best, pattern), to=to, by=by, press=press, name=name)
        shared["done"] = (ok, summary, details)

    pending = _Pending(job, "yuki-slider", BUDGET_S)
    shared = pending.wait()
    if shared.get("done"):
        ok, summary, details = shared["done"]
        return result(ok, summary, {**details, "via_uia": True})
    if shared.get("acting"):
        return result(False, f"setting {name} did not finish ({shared.get('error') or 'out of time'}); the value may "
                             f"have moved: read it again before anything else", {"error": shared.get("error")})
    why = shared.get("missing") or shared.get("error") or "UI Automation did not answer in time"
    return result(False, f"{name}: {why}. Nothing was dragged or clicked. Use the app's own keys for it if a playbook "
                         f"names them, or ask", {"missing": why})


__all__ = ["Slider", "drive", "percent", "set_slider", "target_value"]
