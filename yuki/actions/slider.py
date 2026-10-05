"""A real slider hand (report 47, N2): set a slider by UI Automation's RangeValue, else by its own keys, read back.

Live 3 Oct the user said Spotify's own volume slider was low. For about five minutes Yuki hovered, clicked coordinates
on a player bar that was hidden, scrolled the lyrics instead of the volume, ran two PowerShell ``mouse_event`` drag
scripts and tried to click Mute. There was no way to set a slider: the patterns covered Invoke, Toggle, Select,
Value.SetValue and Scroll, not RangeValue.

:func:`set_slider` takes an element of the brain's latest read (the slider itself, or the control that holds it: a web
player often shows a labelled bar and keeps the real ``<input type=range>`` hidden inside it) and:

1. finds the slider in the live window (:func:`choose`): the window's RangeValue elements, the one named like the read's
   element first (a web slider takes the name of the label that hides it: Spotify's read shows only ``Text "Change
   volume"``, its hidden range input is named "Change volume" too), else the one at the read element's box, else the
   nearest under the read element or its parent; two it cannot tell apart are named back, never guessed between;
2. when none is there (a player bar hidden until the pointer is over it), moves the pointer over the read's point once
   (``hover``: no click) and looks again;
3. with ``keys`` (an app's own shortcut for that level, learned from a run that worked: practice memory, never a list
   in code) presses them, reading the level back after each press, and says whether the shortcut moved it, which way
   and how far; with no value to read, compares what the bar shows before and after (a mute button's "Volume medium"
   becoming "Volume low");
4. sets the new value with ``RangeValue.SetValue``, reads it back and reads it again a moment later (an app that puts
   its control back is caught);
5. when the value cannot be set that way (read-only, it did not move, or it went back), gives the slider keyboard
   focus (UI Automation ``SetFocus``) and presses its own arrow keys toward the target, reading the value back after
   each few presses, and stops at the target or as soon as presses stop moving it;
6. when nothing exposes a value, does nothing and says so: no drag, no click along the bar, no script, and never the
   Windows mixer instead (that is another control: ``app_volume``).

Live 4 Oct every try failed with "no element at its point matches the read": in a Chromium or CEF window (Spotify)
UI Automation's ``ElementFromPoint`` answers with the renderer's whole pane, never the label under the point, so the
hand never got past finding the element. The window's own tree is searched instead.

A target already reached is a no-op, reported as such. Which element is the slider is fixed UI Automation facts
(patterns, names, boxes); nothing here knows which program drew it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from yuki.actions import ActionResult

_PATTERN_RANGE_VALUE = 10003
_P_IS_RANGE_VALUE_AVAILABLE = 30033
_UIA_BOUNDING_RECT = 30001
_UIA_CONTROL_TYPE = 30003
_UIA_NAME = 30005
_UIA_HELP_TEXT = 30013
_UIA_NATIVE_WINDOW_HANDLE = 30020
_UIA_VALUE_VALUE = 30045
_UIA_RANGE_VALUE_VALUE = 30047
_UIA_FULL_DESCRIPTION = 30159
_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_SUBTREE = 7
#: Levels above the read's element searched for a RangeValue element when nothing is named like it or at its box.
SEARCH_UP = 2
#: The whole job's budget (s: the keys path, up to MAX_PRESSES with a read-back after each batch and the input gate's
#: wait before each, takes longer than one SetValue), and how long a set value is watched for its read-back.
BUDGET_S = 9.0
SETTLE_S = 0.4
#: percFix: how long a set value's own change event is waited for before the read-back stands (s), and after each
#: batch of keys.
VALUE_WAIT_S = 2.0
KEY_WAIT_S = 0.6
POLL_S = 0.05
#: Keys: presses per batch, the most presses in all, and how close (share of the range) counts as reached.
KEYS_PER_BATCH = 5
MAX_PRESSES = 60
TOLERANCE_SHARE = 0.02
#: A RangeValue element whose box centre is this close (px) to the read element's box is at it.
NEAR_PX = 60
#: Two candidates whose distances to the read's box differ by less than this (px) cannot be told apart by place.
TIE_PX = 2.0
#: How long a bar is given to appear after the pointer moved over it, and how long a set value is watched again (s).
REVEAL_S = 0.4
HOLD_S = 0.5
#: A learned shortcut: presses at most, and the wait for its effect before each read-back (s).
MAX_SHORTCUT_PRESSES = 12
SHORTCUT_WAIT_S = 0.15
#: What a bar shows, compared before and after a shortcut when nothing exposes a value: lines at most.
SHOWN_LINES = 60

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
          sleep: Callable[[float], None] = time.sleep, settle_s: float = SETTLE_S,
          stopped: Callable[[], bool] | None = None, hold_s: float = HOLD_S) -> tuple[bool, str, dict[str, Any]]:
    """Move one live slider to its target and read it back: ``(ok, summary, details)``. RangeValue.SetValue first,
    read again after ``hold_s`` (an app that puts its control back is caught: ``reverted``); when that cannot move it,
    focus and the arrow keys (``press(key, times)``) with a read-back after each batch. ``stopped()`` true (the caller
    stopped waiting: its result has gone back) ends the keys before the next batch."""
    details: dict[str, Any] = {"verified": False}
    value, low, high, read_only, small = slider.read()
    details.update(before=round(value, 4), minimum=low, maximum=high, before_percent=round(percent(value, low, high), 1))
    if high <= low:
        return False, f"{name} reports an empty range ({low:g}-{high:g}); nothing was changed", details
    want = target_value(value, low, high, to=to, by=by)
    if by and small and 0 < abs(want - value) < abs(small):
        # "a little" on a slider that moves in steps: one step that way (a smaller value would snap back)
        want = max(low, min(high, value + math.copysign(abs(small), want - value)))
    tolerance = max(abs(small) if small else 0.0, (high - low) * TOLERANCE_SHARE)
    details.update(target=round(want, 4), target_percent=round(percent(want, low, high), 1))
    if abs(want - value) <= tolerance / 2:
        details.update(verified=True, noop=True, after=round(value, 4), after_percent=details["before_percent"])
        end = " (its end)" if want in (low, high) else ""
        return True, (f"{name} is already at {details['before_percent']:g}% of its range{end}: nothing to move, "
                      f"nothing was changed"), details

    def there(level: float) -> bool:
        """At the target, and moved toward it (percFix: live 5 Oct a step of 0.1 was also the tolerance, so a value
        that had not moved yet, 0.40 against a target of 0.30, counted as reached and was reported verified)."""
        return abs(level - want) <= tolerance and (level - value) * (want - value) > 0

    if stopped is not None and stopped():
        # review F5: the caller's answer ("nothing was dragged or clicked") has gone back; nothing is set after it
        details.update(stopped=True, after=round(value, 4), after_percent=details["before_percent"])
        return False, f"{name} was not changed: the time for it ran out first", details
    watcher = getattr(slider, "watch", None)
    watch = None
    try:
        watch = watcher() if callable(watcher) else None
    except Exception:  # noqa: BLE001 - read without events then
        watch = None
    live = watch is not None and getattr(watch, "live", False)
    try:
        return _drive_moves(slider, details, value=value, want=want, low=low, high=high, small=small,
                            read_only=read_only, tolerance=tolerance, there=there, watch=watch if live else None,
                            press=press, name=name, sleep=sleep, settle_s=settle_s, stopped=stopped, hold_s=hold_s)
    finally:
        if watch is not None:
            try:
                watch.close()
            except Exception:  # noqa: BLE001
                pass


def _drive_moves(slider: Slider, details: dict[str, Any], *, value: float, want: float, low: float, high: float,
                 small: float, read_only: bool, tolerance: float, there: Callable[[float], bool], watch: Any,
                 press: Callable[[str, int], Any] | None, name: str, sleep: Callable[[float], None],
                 settle_s: float, stopped: Callable[[], bool] | None, hold_s: float) -> tuple[bool, str, dict[str, Any]]:
    """:func:`drive` once the target is known: the value, else the keys, then the verdict. ``watch``: the slider's
    own change events (percFix), ``None`` when it gives none (then the read-back waits ``settle_s``, polled)."""
    live = watch is not None
    after = value
    if not read_only:
        try:
            slider.set(want)
            details["via"] = "RangeValue"
            # percFix: read back when the slider says its value changed (its own event, bounded), not after a fixed
            # moment: Chromium and CEF deliver the new value a little later than SetValue returns
            deadline = time.monotonic() + (VALUE_WAIT_S if live else settle_s)
            after = slider.read()[0]
            while not there(after):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if live:
                    if not watch.wait(remaining):
                        break
                else:
                    sleep(POLL_S)
                after = slider.read()[0]
            if live:
                details["read_back"] = "on its change event" if there(after) else "no change event in time"
            if there(after) and hold_s > 0:
                # a controlled web input can take the value and be put back by its app a moment later
                if live:
                    if watch.wait(hold_s):
                        again = slider.read()[0]
                    else:
                        again = after
                else:
                    sleep(hold_s)
                    again = slider.read()[0]
                if not there(again):
                    details["reverted"] = True
                    after = again
        except Exception as exc:  # noqa: BLE001 - the keys then
            details["set_error"] = f"{type(exc).__name__}: {exc}"[:160]
    if not there(after) and press is not None:
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
            if stopped is not None and stopped():
                details["stopped"] = True   # review F7: no key after the caller has given its answer
                break
            need = max(1, min(KEYS_PER_BATCH, math.ceil(abs(want - after) / max(step, 1e-9))))
            before_batch = after
            try:
                press(key, need)
            except Exception as exc:  # noqa: BLE001 - the user took the keyboard, or the window left the front
                details["keys_error"] = f"{type(exc).__name__}: {exc}"[:160]
                break
            presses += need
            if live:
                watch.wait(KEY_WAIT_S)   # percFix: the level a key set arrives on its own event, a moment later
            else:
                sleep(POLL_S)
            after = slider.read()[0]
            if abs(after - before_batch) < 1e-9:
                details["keys_stuck"] = True
                break
            if (key == "right" and after > want) or (key == "left" and after < want):
                break   # one press past it: close enough within a step
        details["presses"] = presses
    details.update(after=round(after, 4), after_percent=round(percent(after, low, high), 1))
    # percFix: never "reached" without having moved toward the target (a key step as wide as the asked move made a
    # level that had not moved count as reached)
    reached = abs(after - want) <= max(tolerance, abs(small) if small else 0.0) and (after - value) * (want - value) > 0
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
                   f"{' (it went back a moment after being set)' if details.get('reverted') else ''}"
                   f"{' and its arrow keys did not move it either' if details.get('via') == 'keys' else ''}. "
                   f"Nothing was dragged or clicked"), details


# ---------------------------------------------------------------------------
# The live slider (UI Automation)
# ---------------------------------------------------------------------------


class _LiveSlider:
    """A RangeValue element, read and set on the UI Automation thread that found it."""

    def __init__(self, element: Any, pattern: Any, automation: Any = None) -> None:
        self.element = element
        self.pattern = pattern
        self.automation = automation

    def watch(self) -> Any:
        """percFix: the slider's own value-changed events (:class:`yuki.perception.events.ElementWatch`), made before
        it is set so the change cannot be missed; ``None`` without an automation object."""
        if self.automation is None:
            return None
        from yuki.perception.events import ElementWatch

        return ElementWatch(self.automation, self.element, (_UIA_RANGE_VALUE_VALUE,))

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


def _distance(rect: tuple[int, int, int, int] | None, want: Any) -> float:
    """How far a candidate's box centre is from the read's box (0 inside it)."""
    if rect is None or not want or len(want) != 4:
        return math.inf
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    dx = max(int(want[0]) - cx, 0.0, cx - int(want[2]))
    dy = max(int(want[1]) - cy, 0.0, cy - int(want[3]))
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# Finding the slider in the live window
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """One live element as the slider hand sees it: its role, name and box, and the provider's own handle."""

    role: str
    name: str
    bounds: tuple[int, int, int, int] | None
    ref: Any = None


class Tree(Protocol):
    """One window's live tree as the slider hand needs it (UI Automation; tests give a fake shaped like a real
    read)."""

    def ranges(self, scope: Node | None = None) -> list[Node]: ...   # RangeValue elements under scope (None: window)
    def named(self, role: str, name: str) -> list[Node]: ...        # the window's elements with this role and name
    def parent(self, node: Node) -> Node | None: ...
    def slider(self, node: Node) -> Slider: ...
    # what the elements under it show, one (line, box or None) each, in tree order
    def shown(self, node: Node) -> list[tuple[str, tuple[int, int, int, int] | None]]: ...


def _words(text: Any) -> str:
    return " ".join(str(text or "").split())


def name_fits(candidate: str, read: str) -> bool:
    """A RangeValue element named as the read's element: the same name, or that name then a value ("Change volume
    50"): a web slider takes the name of the label that hides it, and some engines add its value. Names as the app
    wrote them, case folded."""
    c, r = _words(candidate).casefold(), _words(read).casefold()
    return bool(r) and (c == r or c.startswith(r + " "))


def _label(node: Node) -> str:
    return f"{node.role or 'Slider'} \"{_words(node.name)[:60]}\""


def _point(element: dict[str, Any]) -> tuple[int, int] | None:
    try:
        center = element.get("center") or ()
        return int(center[0]), int(center[1])
    except (TypeError, ValueError, IndexError):
        return None


def choose(tree: Tree, element: dict[str, Any]) -> tuple[Node | None, str, list[Node]]:
    """The slider the read's element means, in the live window: ``(node, how, sliders)``. ``how``: ``named`` (named
    like the read's element), ``near`` (at its box), ``under`` (under it or a parent of it), ``ambiguous`` (two it
    cannot tell apart: ``node`` is ``None`` and ``sliders`` are the ones that fit), ``unrelated`` (the window has
    sliders, none of them it), ``none`` (no slider in the window). ``sliders``: every RangeValue element seen, unless
    ambiguous. Element facts only (patterns, names, boxes)."""
    every = list(tree.ranges(None))
    if not every:
        return None, "none", []
    want = element.get("bounds")
    name = _words(element.get("name"))

    def far(n: Node) -> float:
        return _distance(n.bounds, want)

    pool, how = [n for n in every if name_fits(n.name, name)], "named"
    if not pool:
        pool, how = [n for n in every if far(n) <= NEAR_PX], "near"
    if not pool:
        how = "under"
        role = _words(element.get("role"))
        holders = sorted(tree.named(role, name), key=far) if role and name else []
        scope = holders[0] if holders else None
        for _ in range(SEARCH_UP + 1):
            if scope is None:
                break
            pool = list(tree.ranges(scope))
            if pool:
                break
            scope = tree.parent(scope)
    if not pool:
        return None, "unrelated", every
    pool.sort(key=far)
    if len(pool) > 1:
        a, b = far(pool[0]), far(pool[1])
        if a == b or (math.isfinite(a) and abs(a - b) < TIE_PX):
            return None, "ambiguous", pool
    return pool[0], how, every


# ---------------------------------------------------------------------------
# A learned shortcut, read back
# ---------------------------------------------------------------------------


def shortcut(slider: Slider, keys: list[str], hotkey: Callable[[list[str]], Any], *, to: float | None = None,
             by: float | None = None, name: str = "the slider", sleep: Callable[[float], None] = time.sleep,
             wait_s: float = SHORTCUT_WAIT_S, stopped: Callable[[], bool] | None = None
             ) -> tuple[bool, str, dict[str, Any]]:
    """An app's own shortcut for a level (``keys``, learned from a run that worked), pressed toward the target with
    the level read back after each press: ``(ok, summary, details)`` with ``moved`` (it changed the level at all),
    ``wrong_way`` (it moved it the other way: stopped at once) and ``verified`` (it reached the target, as near as its
    steps get). Nothing about which app or which keys is known here."""
    combo = "+".join(keys)
    value, low, high, _read_only, small = slider.read()
    details: dict[str, Any] = {"via": "shortcut", "keys": list(keys), "before": round(value, 4), "minimum": low,
                               "maximum": high, "before_percent": round(percent(value, low, high), 1),
                               "verified": False, "moved": False}
    if high <= low:
        return False, f"{name} reports an empty range ({low:g}-{high:g}); {combo} was not pressed", details
    want = target_value(value, low, high, to=to, by=by)
    tolerance = max(abs(small) if small else 0.0, (high - low) * TOLERANCE_SHARE)
    details.update(target=round(want, 4), target_percent=round(percent(want, low, high), 1))
    if abs(want - value) <= tolerance / 2:
        details.update(verified=True, noop=True, after=round(value, 4), after_percent=details["before_percent"])
        return True, (f"{name} is already at {details['before_percent']:g}% of its range: nothing to move, nothing "
                      f"was pressed"), details
    toward = 1.0 if want > value else -1.0
    after, presses, step = value, 0, 0.0
    while presses < MAX_SHORTCUT_PRESSES:
        if stopped is not None and stopped():
            details["stopped"] = True
            break
        before_press = after
        try:
            hotkey(list(keys))
        except Exception as exc:  # noqa: BLE001 - the user took the keyboard, or the window left the front
            details["keys_error"] = f"{type(exc).__name__}: {exc}"[:160]
            break
        presses += 1
        sleep(wait_s)
        after = slider.read()[0]
        moved = after - before_press
        if abs(moved) < 1e-9:
            details["keys_stuck"] = True
            break
        if moved * toward < 0:
            details["wrong_way"] = True
            break
        step = abs(moved)
        if abs(after - want) <= max(tolerance, step * 0.75) or (after - want) * toward > 0:
            break
    reached = not details.get("wrong_way") and abs(after - want) <= max(tolerance, step * 0.75)
    details.update(presses=presses, after=round(after, 4), after_percent=round(percent(after, low, high), 1),
                   moved=abs(after - value) > 1e-9, verified=bool(reached))
    moved_text = f"{details['before_percent']:g}% -> {details['after_percent']:g}% of its range"
    times = f"{presses} time{'' if presses == 1 else 's'}"
    if reached:
        return True, (f"pressed the app's own shortcut {combo} {times}: {name} {moved_text}, read back (the mouse was "
                      f"not used)"), details
    if details.get("wrong_way"):
        return False, f"pressed {combo}: it moved {name} the other way ({moved_text})", details
    if not details["moved"]:
        return False, (f"pressed {combo}: {name} did not change ({details['before_percent']:g}%), so that shortcut "
                       f"does not move it here"), details
    return False, f"pressed {combo} {times}: {name} {moved_text}, short of {details['target_percent']:g}%", details


def _next_to(bounds: Any, near: Any) -> bool:
    """An element's box at or next to the read element's box (within :data:`NEAR_PX` of its edges). Boxes only."""
    if not bounds or len(bounds) != 4 or not near or len(near) != 4:
        return False
    left, top, right, bottom = (int(v) for v in near)
    dx = max(left - int(bounds[2]), 0, int(bounds[0]) - right)
    dy = max(top - int(bounds[3]), 0, int(bounds[1]) - bottom)
    return math.hypot(dx, dy) <= NEAR_PX


def shortcut_shown(tree: Tree, scope: Node, keys: list[str], hotkey: Callable[[list[str]], Any], *,
                   near: Any = None, sleep: Callable[[float], None] = time.sleep, wait_s: float = REVEAL_S
                   ) -> tuple[bool, str, dict[str, Any]]:
    """A learned shortcut where nothing exposes the level: pressed once, and what the elements next to the bar show
    (names, values, descriptions: a mute button's "Volume medium") compared before and after. A change is the
    evidence it acted; how far it moved cannot be read (``level_unread``).

    Review F2: only volume evidence counts. The scope (the label's parent) is a whole player bar with a ticking
    playback clock, so (1) with ``near`` (the read element's box) only elements at or next to it are compared, and
    (2) the elements are looked at twice before the press, ``wait_s`` apart: any that changed with no key pressed (a
    clock, a progress) are left out of the comparison."""
    combo = "+".join(keys)
    details: dict[str, Any] = {"via": "shortcut", "keys": list(keys), "level_unread": True, "verified": False,
                               "moved": False}

    def look() -> dict[tuple[Any, int], str]:
        out: dict[tuple[Any, int], str] = {}
        count: dict[Any, int] = {}
        for line, box in tree.shown(scope):
            if near is not None and not _next_to(box, near):
                continue
            where = tuple(box) if box else None
            count[where] = count.get(where, 0) + 1
            out[(where, count[where])] = line
        return out

    first = look()
    sleep(wait_s)
    before = look()
    live = {k for k in set(first) | set(before) if first.get(k) != before.get(k)}   # changed by itself
    try:
        hotkey(list(keys))
    except Exception as exc:  # noqa: BLE001
        details["keys_error"] = f"{type(exc).__name__}: {exc}"[:160]
        return False, f"{combo} could not be pressed ({details['keys_error']}); nothing changed", details
    sleep(wait_s)
    after = look()
    if live:
        details["ignored"] = len(live)
    changed = [k for k in set(before) | set(after) if k not in live and before.get(k) != after.get(k)]
    gone = [before[k] for k in changed if k in before]
    new = [after[k] for k in changed if k in after]
    if not gone and not new:
        return False, (f"pressed {combo} once: nothing around the bar changed, and nothing there exposes its level, so "
                       f"that shortcut did nothing visible here"), details
    details.update(moved=True, verified=True, shown_before=gone[:2], shown_after=new[:2])
    change = f"{gone[0]} -> {new[0]}" if gone and new else f"now {new[0]}" if new else f"no longer {gone[0]}"
    return True, (f"pressed {combo} once: what the bar shows changed ({change}); nothing there exposes the level "
                  f"itself, so how far it moved is not known"), details


# ---------------------------------------------------------------------------
# The whole hand on one window's tree
# ---------------------------------------------------------------------------


def run(tree: Tree, element: dict[str, Any], *, to: float | None = None, by: float | None = None,
        press: Callable[[str, int], Any] | None = None, hotkey: Callable[[list[str]], Any] | None = None,
        keys: Any = None, hover: Callable[[int, int], Any] | None = None, stopped: Callable[[], bool] | None = None,
        acting: Callable[[], None] | None = None, sleep: Callable[[float], None] = time.sleep,
        settle_s: float = SETTLE_S, hold_s: float = HOLD_S, reveal_s: float = REVEAL_S,
        wait_s: float = SHORTCUT_WAIT_S) -> tuple[bool, str, dict[str, Any]]:
    """The slider hand on one window's live tree (see the module docstring): find the slider, show a hidden bar once,
    a learned shortcut read back, the value, the arrow keys. ``(ok, summary, details)``; ``acting()`` is called just
    before the first thing that can change the level."""
    name = f"{element.get('role') or 'element'} \"{_words(element.get('name'))[:60]}\""
    keys = [_words(k) for k in (keys or ()) if _words(k)]
    node, how, seen = choose(tree, element)
    details: dict[str, Any] = {"found": how}

    def out_of_time() -> bool:
        # review F5: a slow search (a whole Chromium tree, twice around a hover) can outlast the caller's wait; once
        # its answer has gone back, nothing acts
        if stopped is not None and stopped():
            details.update(stopped=True, verified=False)
            return True
        return False

    if node is None and how in ("none", "unrelated") and hover is not None:
        point = _point(element)
        if point is not None:
            try:
                hover(*point)
                details["hovered"] = True
                sleep(reveal_s)
                node, how, seen = choose(tree, element)
                details["found"] = how
            except Exception as exc:  # noqa: BLE001 - reported with the rest
                details["hover_error"] = f"{type(exc).__name__}: {exc}"[:160]
    shown = ", ".join(_label(n) for n in seen[:4])
    if how == "ambiguous":
        details.update(verified=False, sliders=[_label(n) for n in seen[:4]])
        return False, (f"{name}: more than one slider fits it ({shown}) and nothing tells them apart; nothing was "
                       f"changed. Name the one you mean (its element in the read)"), details
    revealed = " (even after moving the pointer over the bar to show it)" if details.get("hovered") else ""
    if node is None:
        if keys and hotkey is not None:
            holders = sorted(tree.named(_words(element.get("role")), _words(element.get("name"))),
                             key=lambda n: _distance(n.bounds, element.get("bounds")))
            scope = (tree.parent(holders[0]) or holders[0]) if holders else None
            if scope is not None:
                if out_of_time():
                    return False, f"{name}: the time for it ran out before {'+'.join(keys)} was pressed", details
                if acting is not None:
                    acting()
                ok, summary, sd = shortcut_shown(tree, scope, keys, hotkey, near=element.get("bounds"), sleep=sleep,
                                                 wait_s=max(reveal_s, wait_s))
                return ok, summary, {**details, **sd}
        where = (f"the sliders this window has are {shown}, none of them at {name}" if seen else
                 "nothing in this window exposes a slider's level to UI Automation")
        details.update(verified=False, missing=where)
        return False, (f"{name}: {where}{revealed}. Nothing was dragged or clicked. The Windows mixer is another "
                       f"control: when they asked for this app's own level, say plainly it cannot be reached, never "
                       f"change the mixer instead"), details
    slider = tree.slider(node)
    label = _label(node) if _words(node.name) else name
    details["slider"] = label
    if out_of_time():
        return False, f"{label} was found but not changed: the time for it ran out first", details
    if acting is not None:
        acting()
    note = ""
    if keys and hotkey is not None:
        ok, summary, sd = shortcut(slider, keys, hotkey, to=to, by=by, name=label, sleep=sleep, wait_s=wait_s,
                                   stopped=stopped)
        if sd.get("verified") or sd.get("stopped") or sd.get("keys_error") or sd.get("minimum") == sd.get("maximum"):
            return ok, summary, {**details, **sd}
        # the shortcut did nothing, went the other way or fell short: its value next, to the same target
        details["shortcut"] = {k: sd.get(k) for k in ("keys", "presses", "moved", "wrong_way", "before_percent",
                                                      "after_percent")}
        note = summary + ". "
        to, by = sd.get("target_percent", to), None
    ok, summary, dd = drive(slider, to=to, by=by, press=press, name=label, sleep=sleep, settle_s=settle_s,
                            stopped=stopped, hold_s=hold_s)
    if details.get("hovered"):
        summary += " (the pointer was moved over the bar to show it)"
    return ok, note + summary, {**details, **dd}


# ---------------------------------------------------------------------------
# The live tree (UI Automation)
# ---------------------------------------------------------------------------


def _control_type(role: str) -> int | None:
    """The UI Automation control type of a read's role name (``yuki.perception.tree``'s own table, inverted)."""
    try:
        from yuki.perception.tree import _ROLE_NAMES
    except Exception:  # noqa: BLE001
        return None
    for control_type, role_name in _ROLE_NAMES.items():
        if role_name == role:
            return int(control_type)
    return None


class _UiaTree:
    """One window's live tree through UI Automation, used on the thread that made ``automation``. Searched from the
    window's root (``FindAll``), never by point: a Chromium or CEF window answers ``ElementFromPoint`` with its whole
    renderer pane."""

    def __init__(self, automation: Any, hwnd: int) -> None:
        self.automation = automation
        request = automation.CreateCacheRequest()
        for prop in (_UIA_CONTROL_TYPE, _UIA_NAME, _UIA_NATIVE_WINDOW_HANDLE, _UIA_BOUNDING_RECT, _UIA_VALUE_VALUE,
                     _UIA_HELP_TEXT):
            request.AddProperty(prop)
        try:
            request.AddProperty(_UIA_FULL_DESCRIPTION)   # Windows 10 1703 and later
        except Exception:  # noqa: BLE001
            pass
        request.TreeScope = _TREE_SCOPE_ELEMENT
        self.request = request
        self.walker = automation.ControlViewWalker
        self.root = automation.ElementFromHandleBuildCache(int(hwnd), request)

    def _node(self, element: Any) -> Node:
        from yuki.actions.patterns import _facts

        facts = _facts(element)
        box = tuple(int(v) for v in facts["bounds"])
        return Node(facts["role"], facts["name"], box if any(box) else None, element)   # type: ignore[arg-type]

    def _find(self, scope: Any, condition: Any) -> list[Node]:
        try:
            array = scope.FindAllBuildCache(_TREE_SCOPE_SUBTREE, condition, self.request)
            return [self._node(array.GetElement(i)) for i in range(array.Length if array else 0)]
        except Exception:  # noqa: BLE001 - a provider that fails reads as nothing found
            return []

    def ranges(self, scope: Node | None = None) -> list[Node]:
        condition = self.automation.CreatePropertyCondition(_P_IS_RANGE_VALUE_AVAILABLE, True)
        return self._find(scope.ref if scope is not None else self.root, condition)

    def named(self, role: str, name: str) -> list[Node]:
        control_type = _control_type(role)
        if control_type is None or not name:
            return []
        a = self.automation
        condition = a.CreateAndCondition(a.CreatePropertyCondition(_UIA_CONTROL_TYPE, control_type),
                                         a.CreatePropertyCondition(_UIA_NAME, name))
        return self._find(self.root, condition)

    def parent(self, node: Node) -> Node | None:
        try:
            element = self.walker.GetParentElementBuildCache(node.ref, self.request)
        except Exception:  # noqa: BLE001
            return None
        return self._node(element) if element else None

    def slider(self, node: Node) -> Slider:
        from yuki.perception.tree import _uia_core

        module = _uia_core()
        pattern = node.ref.GetCurrentPattern(_PATTERN_RANGE_VALUE).QueryInterface(
            module.IUIAutomationRangeValuePattern)
        return _LiveSlider(node.ref, pattern, self.automation)

    def shown(self, node: Node) -> list[tuple[str, tuple[int, int, int, int] | None]]:
        out: list[tuple[str, tuple[int, int, int, int] | None]] = []
        for n in self._find(node.ref, self.automation.CreateTrueCondition())[:SHOWN_LINES]:
            bits = []
            for prop in (_UIA_VALUE_VALUE, _UIA_HELP_TEXT, _UIA_FULL_DESCRIPTION):
                try:
                    value = n.ref.GetCachedPropertyValue(prop)
                except Exception:  # noqa: BLE001 - a property not cached reads as absent
                    value = None
                if isinstance(value, str) and value.strip():
                    bits.append(_words(value)[:80])
            out.append((_label(n) + (f": {'; '.join(dict.fromkeys(bits))}" if bits else ""), n.bounds))
        return out


def set_slider(hwnd: int, element: dict[str, Any], *, to: float | None = None, by: float | None = None,
               press: Callable[..., Any] | None = None, hover: Callable[..., Any] | None = None,
               hotkey: Callable[..., Any] | None = None, keys: Any = None) -> ActionResult:
    """The real slider hand (see the module docstring). ``press(key, times, stopped=...)`` sends keys to the slider's
    window; ``hover(x, y, stopped=...)`` moves the pointer over a point of it (no click), to show a bar hidden until
    then; ``hotkey(keys, stopped=...)`` presses an app's own shortcut there (``keys``: one learned from a run that
    worked). All three are the caller's guarded actions: ``stopped()`` turns true once this has returned, after which
    they must not act (one may have waited for the user to stop typing); ``None``: not used."""
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
    from yuki.actions.input import _Pending

    stop = threading.Event()

    def guarded(fn: Callable[..., Any] | None) -> Callable[..., Any] | None:
        # review F7: the caller's action is told when the result has already gone back (it may wait for the user to
        # stop typing first), so nothing is pressed or moved after that
        if fn is None:
            return None

        def call(*args: Any) -> Any:
            if stop.is_set():
                raise TimeoutError("out of time")
            return fn(*args, stopped=stop.is_set)

        return call

    def job(automation: Any, shared: dict) -> None:
        tree = _UiaTree(automation, int(hwnd))
        shared["done"] = run(tree, element, to=to, by=by, press=guarded(press), hotkey=guarded(hotkey), keys=keys,
                             hover=guarded(hover), stopped=stop.is_set,
                             acting=lambda: shared.__setitem__("acting", True))

    pending = _Pending(job, "yuki-slider", BUDGET_S)
    shared = pending.wait()
    stop.set()
    if shared.get("done"):
        ok, summary, details = shared["done"]
        return result(ok, summary, {**details, "via_uia": True})
    if shared.get("acting"):
        return result(False, f"setting {name} did not finish ({shared.get('error') or 'out of time'}); the value may "
                             f"have moved: read it again before anything else", {"error": shared.get("error")})
    why = shared.get("error") or "UI Automation did not answer in time"
    return result(False, f"{name}: {why}. Nothing was dragged or clicked", {"missing": why})


__all__ = ["Node", "Slider", "Tree", "choose", "drive", "name_fits", "percent", "run", "set_slider", "shortcut",
           "shortcut_shown", "target_value"]
