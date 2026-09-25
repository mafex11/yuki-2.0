"""Scrolling that acts on the scrollable element and says whether it moved (docs/VOICE.md §5.6).

Measured live (2026-09-25, 18:54-18:57): the voice's scroll hand and the brain's
``scroll`` sent wheel notches at a point and reported "scrolled" - the wheel went
where the cursor was (over Chrome's tab bar, or off the page), nothing moved, and
Yuki said "Done" twice. "Sent" is not "scrolled". So a scroll here:

1. **Acts on the element, not the cursor.** The element under the target point, or
   its nearest ancestor that scrolls on that axis (UIA ``ScrollPattern``), else the
   largest element of the window that scrolls on it. It is moved with
   ``SetScrollPercent`` (by a screenful per 10 notches, from its own view size) and
   the cursor is never touched.
2. **Falls back to the wheel at the element's point** when nothing there has a
   ``ScrollPattern``: the cursor is moved there, the wheel is sent, and the cursor
   is put back where the user had it.
3. **Verifies the effect.** With the pattern: the scroll percent before and after
   (a condition poll, bounded). With the wheel: whether the element under the
   point moved on screen (the viewport's content). The result says which, with
   the numbers; ``ok`` is False when it provably did not move (or was already at
   the end), and the facts say so.

The logic (:func:`scroll_pattern_target`, :func:`wheel_at`) works on a small
:class:`Target` interface so it is tested without a desktop; :func:`scroll_at`
is the real one over UI Automation, run on its own COM thread with a budget.
Plumbing only: nothing here looks at what an element is called.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

#: UIA ids used here.
_PATTERN_SCROLL = 10004
_P_IS_SCROLL_AVAILABLE = 30034
_P_V_SCROLLABLE = 30058
_P_H_SCROLLABLE = 30057
_P_CONTROL_TYPE = 30003
_P_NAME = 30005
_P_BOUNDING_RECT = 30001
_NO_SCROLL = -1.0            # UIA_ScrollPatternNoScroll
_AMOUNT = {"large_back": 0, "small_back": 1, "none": 2, "large_forward": 3, "small_forward": 4}
_TREE_SCOPE_DESCENDANTS = 4
_GA_ROOT = 2

#: Notches per screenful (the hands' and the brain's contract: "about a screenful is 10").
NOTCHES_PER_SCREEN = 10
#: How long the effect is watched for (a condition poll; it ends the moment it moves).
WATCH_S = 0.8
POLL_S = 0.03
#: With the wheel, the cursor is put back once the content has moved or after this long.
CURSOR_BACK_S = 0.15
#: The whole UIA job's budget.
BUDGET_S = 2.5
#: Levels climbed from the element at the point to a scrollable ancestor.
CLIMB = 40


class Target(Protocol):
    """One scrollable element (UIA ScrollPattern), as the logic needs it."""

    role: str
    name: str

    def percent(self, vertical: bool) -> float | None: ...
    def view(self, vertical: bool) -> float | None: ...
    def set_percent(self, vertical: bool, value: float) -> None: ...
    def step(self, vertical: bool, forward: bool, large: bool) -> None: ...


@dataclass
class Outcome:
    """What a scroll did, measured."""

    moved: bool | None
    how: str                                  # pattern | wheel | none
    before: float | None = None
    after: float | None = None
    at_end: bool = False
    shift_px: int | None = None
    note: str = ""
    target: dict[str, Any] = field(default_factory=dict)

    def facts(self) -> dict[str, Any]:
        return {"moved": self.moved, "how": self.how, "before": _r(self.before), "after": _r(self.after),
                "at_end": self.at_end, "shift_px": self.shift_px, "scroller": self.target, "note": self.note}


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def screen_percent(view: float | None) -> float | None:
    """How many scroll percent one screenful is, from the view size (percent of the content shown)."""
    if view is None or view <= 0 or view >= 100:
        return None
    return view / (100.0 - view) * 100.0


def scroll_pattern_target(target: Target, *, vertical: bool, notches: int, forward: bool,
                          watch_s: float = WATCH_S, poll_s: float = POLL_S,
                          clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """Scroll ``target`` by ``notches`` (forward = down / right) and watch its percent change."""
    facts = {"role": target.role, "name": target.name}
    before = target.percent(vertical)
    if before is not None and before >= 0:
        if (forward and before >= 99.99) or (not forward and before <= 0.01):
            return Outcome(moved=False, how="pattern", before=before, after=before, at_end=True, target=facts,
                           note="already at the " + (("bottom" if vertical else "right end") if forward
                                                     else ("top" if vertical else "left end")))
    screen = screen_percent(target.view(vertical))
    how = "pattern"
    if before is not None and before >= 0 and screen is not None:
        delta = screen * notches / NOTCHES_PER_SCREEN
        goal = min(100.0, before + delta) if forward else max(0.0, before - delta)
        try:
            target.set_percent(vertical, goal)
        except Exception:  # noqa: BLE001 - a provider without SetScrollPercent: step instead
            _steps(target, vertical, notches, forward)
            how = "pattern-steps"
    else:
        _steps(target, vertical, notches, forward)
        how = "pattern-steps"
    deadline = clock() + watch_s
    after = target.percent(vertical)
    while (after is None or before is None or abs(after - before) < 0.005) and clock() < deadline:
        sleep(poll_s)
        after = target.percent(vertical)
    if before is None or after is None or before < 0 or after < 0:
        return Outcome(moved=None, how=how, before=before, after=after, target=facts,
                       note="its scroll position cannot be read")
    return Outcome(moved=abs(after - before) >= 0.005, how=how, before=before, after=after, target=facts)


def _steps(target: Target, vertical: bool, notches: int, forward: bool) -> None:
    """Scroll by the pattern's own increments: a large one per screenful, small ones for the rest."""
    large, small = divmod(max(1, notches), NOTCHES_PER_SCREEN)
    for _ in range(large):
        target.step(vertical, forward, True)
    for _ in range(small * 2):  # a small increment is ~40 px, a wheel notch ~100 px
        target.step(vertical, forward, False)


def wheel_at(x: int, y: int, *, send: Callable[[], None], marker: Callable[[], int | None],
             cursor: Callable[[], tuple[int, int] | None], move: Callable[[int, int], None],
             watch_s: float = WATCH_S, back_after_s: float = CURSOR_BACK_S, poll_s: float = POLL_S,
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """The wheel at (x, y) with the cursor put back after; ``marker()`` is the top edge of what was under the point."""
    before = marker()
    home = cursor()
    move(x, y)
    send()
    started = clock()
    back = False
    after = marker()
    while clock() - started < watch_s:
        if not back and (clock() - started >= back_after_s or (after is not None and after != before)):
            if home is not None:
                move(*home)
            back = True
        if after is not None and before is not None and after != before:
            break
        sleep(poll_s)
        after = marker()
    if not back and home is not None:
        move(*home)
    if before is None or after is None:
        return Outcome(moved=None, how="wheel", note="nothing under the point could be watched")
    return Outcome(moved=after != before, how="wheel", shift_px=abs(after - before))


# ---------------------------------------------------------------------------
# The real thing: UI Automation
# ---------------------------------------------------------------------------


class _UiaTarget:
    """A UIA element's ScrollPattern as a :class:`Target`."""

    def __init__(self, element: Any, pattern: Any, role: str, name: str) -> None:
        self.element, self.pattern, self.role, self.name = element, pattern, role, name

    def percent(self, vertical: bool) -> float | None:
        p = self.pattern
        value = p.CurrentVerticalScrollPercent if vertical else p.CurrentHorizontalScrollPercent
        return float(value) if value is not None else None

    def view(self, vertical: bool) -> float | None:
        p = self.pattern
        value = p.CurrentVerticalViewSize if vertical else p.CurrentHorizontalViewSize
        return float(value) if value is not None else None

    def set_percent(self, vertical: bool, value: float) -> None:
        if vertical:
            self.pattern.SetScrollPercent(_NO_SCROLL, float(value))
        else:
            self.pattern.SetScrollPercent(float(value), _NO_SCROLL)

    def step(self, vertical: bool, forward: bool, large: bool) -> None:
        amount = _AMOUNT[("large_" if large else "small_") + ("forward" if forward else "back")]
        if vertical:
            self.pattern.Scroll(_AMOUNT["none"], amount)
        else:
            self.pattern.Scroll(amount, _AMOUNT["none"])


def _module() -> Any:
    from yuki.perception.tree import _uia_core

    return _uia_core()


def _role(element: Any) -> str:
    try:
        from yuki.perception.tree import _role_name

        return _role_name(element.CurrentControlType)
    except Exception:  # noqa: BLE001
        return "element"


def _as_target(element: Any, vertical: bool) -> _UiaTarget | None:
    """``element``'s ScrollPattern when it scrolls on this axis, else ``None``."""
    try:
        raw = element.GetCurrentPattern(_PATTERN_SCROLL)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        pattern = raw.QueryInterface(_module().IUIAutomationScrollPattern)
        scrollable = pattern.CurrentVerticallyScrollable if vertical else pattern.CurrentHorizontallyScrollable
    except Exception:  # noqa: BLE001
        return None
    if not scrollable:
        return None
    name = element.CurrentName
    return _UiaTarget(element, pattern, _role(element), " ".join(str(name or "").split())[:80])


def _root_of(x: int, y: int) -> int:
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = ctypes.c_void_p
    child = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not child:
        return 0
    user32.GetAncestor.restype = ctypes.c_void_p
    return int(user32.GetAncestor(ctypes.c_void_p(child), _GA_ROOT) or 0)


def _area(element: Any) -> int:
    try:
        r = element.CurrentBoundingRectangle
        return max(0, r.right - r.left) * max(0, r.bottom - r.top)
    except Exception:  # noqa: BLE001
        return 0


def find_target(automation: Any, x: int, y: int, hwnd: int | None, vertical: bool,
                deadline: float) -> tuple[_UiaTarget | None, str, Any]:
    """The scrollable element for a scroll at (x, y) in ``hwnd``: ``(target, where, element at the point)``.

    The element under the point or its nearest scrollable ancestor (what the wheel
    would have scrolled, had it reached it); else the window's largest element that
    scrolls on this axis. ``where`` says which.
    """
    at_point = None
    top = int(hwnd) if hwnd else _root_of(x, y)
    if hwnd is None or _root_of(x, y) == int(hwnd):
        try:
            at_point = automation.ElementFromPoint(wintypes.POINT(int(x), int(y)))
        except Exception:  # noqa: BLE001
            at_point = None
    element, hops = at_point, 0
    walker = automation.ControlViewWalker if element else None
    while element and hops < CLIMB and time.monotonic() < deadline:
        target = _as_target(element, vertical)
        if target is not None:
            return target, "at the point", at_point
        try:
            if top and int(element.CurrentNativeWindowHandle or 0) == top:
                break  # the window itself: nothing above it is this window's
            element = walker.GetParentElement(element)
        except Exception:  # noqa: BLE001
            element = None
        hops += 1
    window = top
    if not window or time.monotonic() >= deadline:
        return None, "none", at_point
    try:
        root = automation.ElementFromHandle(window)
        condition = automation.CreateAndCondition(
            automation.CreatePropertyCondition(_P_IS_SCROLL_AVAILABLE, True),
            automation.CreatePropertyCondition(_P_V_SCROLLABLE if vertical else _P_H_SCROLLABLE, True))
        found = root.FindAll(_TREE_SCOPE_DESCENDANTS | 1, condition)
        elements = [found.GetElement(i) for i in range(min(found.Length, 50))]
    except Exception:  # noqa: BLE001
        return None, "none", at_point
    for element in sorted(elements, key=_area, reverse=True):
        target = _as_target(element, vertical)
        if target is not None:
            return target, "the window's largest scrollable area", at_point
    return None, "none", at_point


def _marker(element: Any) -> Callable[[], int | None]:
    """The top (or left) edge of ``element`` on screen, re-read each call."""
    def read() -> int | None:
        if element is None:
            return None
        try:
            r = element.CurrentBoundingRectangle
            return int(r.top) * 100000 + int(r.left)
        except Exception:  # noqa: BLE001
            return None
    return read


def _wheel_sender(dy: int, dx: int) -> Callable[[], None]:
    def send() -> None:
        from yuki.actions.input import _MOUSEEVENTF_HWHEEL, _MOUSEEVENTF_WHEEL, _WHEEL_DELTA, _user32

        if dy:
            _user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(dy) * _WHEEL_DELTA, 0)
        if dx:
            _user32.mouse_event(_MOUSEEVENTF_HWHEEL, 0, 0, int(dx) * _WHEEL_DELTA, 0)
    return send


def scroll_at(x: int, y: int, *, dy: int = 0, dx: int = 0, hwnd: int | None = None,
              budget_s: float = BUDGET_S) -> tuple[Outcome, dict[str, Any]]:
    """Scroll at (x, y) (in ``hwnd``) by ``dy`` notches up (+) / down (-) or ``dx`` right (+): ``(outcome, facts)``.

    Blocking (at most ``budget_s``); UIA runs on its own COM thread. One axis per call
    (vertical when ``dy`` is given).
    """
    from yuki.actions.input import _Pending, _move
    from yuki.perception.windows import cursor_position

    vertical = bool(dy)
    notches = abs(int(dy if vertical else dx))
    forward = (dy < 0) if vertical else (dx > 0)

    def job(automation: Any, shared: dict) -> None:
        started = time.monotonic()
        deadline = started + budget_s * 0.4
        target, where, at_point = find_target(automation, x, y, hwnd, vertical, deadline)
        shared["find_ms"] = round((time.monotonic() - started) * 1000, 1)
        shared["where"] = where
        if target is not None:
            shared["stage"] = "pattern"
            shared["outcome"] = scroll_pattern_target(target, vertical=vertical, notches=notches, forward=forward)
            return
        shared["stage"] = "wheel"

        def cursor() -> tuple[int, int] | None:
            try:
                return tuple(cursor_position())  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                return None

        shared["outcome"] = wheel_at(x, y, send=_wheel_sender(dy if vertical else 0, 0 if vertical else dx),
                                     marker=_marker(at_point), cursor=cursor, move=_move)

    pending = _Pending(job, "yuki-scroll", budget_s)
    shared = pending.wait()
    facts = {"find_ms": shared.get("find_ms"), "where": shared.get("where"), "stage": shared.get("stage"),
             "error": shared.get("error")}
    outcome = shared.get("outcome")
    if outcome is None:
        stage = shared.get("stage")
        note = (f"UI Automation did not answer within {budget_s:g} s" if pending.timed_out
                else f"UI Automation failed: {shared.get('error') or 'no answer'}")
        if stage:
            note += f" (it had got as far as the {stage} step, so it may have scrolled)"
        outcome = Outcome(moved=None, how=stage or "none", note=note)
    return outcome, facts


def describe(outcome: Outcome, *, direction: str, notches: int, x: int, y: int) -> tuple[bool, str]:
    """``(ok, summary)`` for an :class:`Outcome`: ok only when it did not provably fail."""
    who = outcome.target
    name = f"{who.get('role', 'element')} \"{who.get('name')}\"" if who.get("name") else who.get("role", "the content")
    if outcome.how.startswith("pattern"):
        if outcome.at_end:
            return False, f"{name} is {outcome.note} ({_pct(outcome.before)}): nothing to scroll {direction}"
        if outcome.moved:
            return True, (f"scrolled {name} {direction}: {_pct(outcome.before)} -> {_pct(outcome.after)} of the way "
                          f"(UI Automation; the cursor was not moved)")
        if outcome.moved is False:
            return False, f"asked {name} to scroll {direction}, but it did not move (still {_pct(outcome.before)})"
        return True, f"asked {name} to scroll {direction}; {outcome.note or 'whether it moved is not known'}"
    if outcome.how == "wheel":
        sent = f"sent {notches} wheel notch(es) {direction} at ({x},{y}) (nothing there takes UI Automation scrolling; " \
               f"the cursor was put back)"
        if outcome.moved:
            return True, f"{sent}; the content moved"
        if outcome.moved is False:
            return False, f"{sent}, but nothing under the point moved: it did not scroll"
        return True, f"{sent}; {outcome.note or 'whether it moved is not known'}"
    return False, outcome.note or "nothing scrollable was found"


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"
