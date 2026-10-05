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
import threading
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
    #: V6 (3 Oct): the wheel went into a web page that takes no UI Automation scrolling, so its scrolling cannot be
    #: watched from here: expected-unverifiable, never "did not scroll" (live the page scrolled and Yuki said it had not).
    page: bool = False
    #: On such a page, an element inside it was watched the whole time and did not move (review F5: several of these in
    #: a row are how a continuous scroll learns the page has stopped, at its end or stuck).
    still: bool = False

    def facts(self) -> dict[str, Any]:
        return {"moved": self.moved, "how": self.how, "before": _r(self.before), "after": _r(self.after),
                "at_end": self.at_end, "shift_px": self.shift_px, "scroller": self.target, "note": self.note,
                "page": self.page, "still": self.still}


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
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
             page: bool = False, own: Callable[[bool], None] | None = None) -> Outcome:
    """The wheel at (x, y) with the cursor put back after; ``marker()`` is the top edge of what was under the point.

    ``page`` (V6): the point is inside a web page that takes no UI Automation scrolling. Its elements' positions are not
    a reliable read-back there (the page itself never moves, and a browser may report its content's new positions
    late): a marker that moved still verifies it, one that did not makes it unverifiable (``moved`` None), never "it
    did not scroll"; ``still`` says a marker was watched and did not move. ``own(True)`` / ``own(False)``: Yuki's own
    input begins (the cursor jump and the wheel) and ends (the cursor is back), so only that moment is taken for
    Yuki's input, never the watch around it (review F4)."""
    out = _wheel(x, y, send=send, marker=marker, cursor=cursor, move=move, watch_s=watch_s, back_after_s=back_after_s,
                 poll_s=poll_s, clock=clock, sleep=sleep, own=own)
    if page and out.moved is not True:
        out.still = out.moved is False
        out.moved, out.page = None, True
        out.note = ("a web page here does not report its scroll position, so whether it moved could not be checked "
                    "(expected for this kind of page, not a failure)")
    return out


def _wheel(x: int, y: int, *, send: Callable[[], None], marker: Callable[[], int | None],
           cursor: Callable[[], tuple[int, int] | None], move: Callable[[int, int], None],
           watch_s: float, back_after_s: float, poll_s: float,
           clock: Callable[[], float], sleep: Callable[[float], None],
           own: Callable[[bool], None] | None = None) -> Outcome:
    def mark(running: bool) -> None:
        if own is not None:
            try:
                own(running)
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass

    before = marker()
    home = cursor()
    back = False
    mark(True)
    try:
        move(x, y)
        send()
        started = clock()
        after = marker()
        while clock() - started < watch_s:
            if not back and (clock() - started >= back_after_s or (after is not None and after != before)):
                if home is not None:
                    move(*home)
                back = True
                mark(False)
            if after is not None and before is not None and after != before:
                break
            sleep(poll_s)
            after = marker()
    finally:
        if not back:
            if home is not None:
                move(*home)
            mark(False)
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


#: The UIA role of a web page (the platform's own control type name).
PAGE_ROLE = "Document"
#: How deep, and through how many children per level, a marker inside a page is looked for under the point (V6).
MARKER_DEPTH = 6
MARKER_CHILDREN = 40


def find_target(automation: Any, x: int, y: int, hwnd: int | None, vertical: bool,
                deadline: float, facts: dict[str, Any] | None = None) -> tuple[_UiaTarget | None, str, Any]:
    """The scrollable element for a scroll at (x, y) in ``hwnd``: ``(target, where, element at the point)``.

    The element under the point or its nearest scrollable ancestor (what the wheel
    would have scrolled, had it reached it); else the window's largest element that
    scrolls on this axis. ``where`` says which. ``facts`` (V6) gets ``page``: the point is inside a web page (a
    Document is the element there or one of its ancestors), and ``page_element``: that page.
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
        if facts is not None and "page_element" not in facts and _role(element) == PAGE_ROLE:
            facts["page"], facts["page_element"] = True, element
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


def _contains(element: Any, x: int, y: int) -> int | None:
    """The area of ``element``'s box when it holds the point (x, y), else ``None``."""
    try:
        r = element.CurrentBoundingRectangle
    except Exception:  # noqa: BLE001
        return None
    if r.left <= x < r.right and r.top <= y < r.bottom:
        return max(0, r.right - r.left) * max(0, r.bottom - r.top)
    return None


def marker_in_page(automation: Any, page: Any, x: int, y: int, deadline: float) -> Any:
    """V6: an element inside the page, under the point and smaller than the page, to watch while the page scrolls (the
    page's own box never moves); ``None`` when there is none or no time. By position and size only."""
    whole = _contains(page, x, y)
    if whole is None:
        return None
    walker = automation.ControlViewWalker
    best, node = None, page
    for _depth in range(MARKER_DEPTH):
        if time.monotonic() >= deadline:
            break
        try:
            child = walker.GetFirstChildElement(node)
        except Exception:  # noqa: BLE001
            child = None
        found = None
        for _n in range(MARKER_CHILDREN):
            if child is None or time.monotonic() >= deadline:
                break
            area = _contains(child, x, y)
            if area is not None and 0 < area < whole:
                found = child
                break
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:  # noqa: BLE001
                child = None
        if found is None:
            break
        best = node = found
    return best


#: An element covering at least this share of its window is the window's own surface (a frame, a renderer pane).
WINDOW_SIZED = 0.9


def window_sized(element: Any, hwnd: int | None) -> bool:
    """Whether ``element`` covers at least :data:`WINDOW_SIZED` of window ``hwnd``'s rectangle. Rectangles only."""
    if not hwnd:
        return False
    try:
        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
        r = element.CurrentBoundingRectangle
    except Exception:  # noqa: BLE001
        return False
    window = max(0, right - left) * max(0, bottom - top)
    area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
    return window > 0 and area >= WINDOW_SIZED * window


def page_below(automation: Any, element: Any) -> Any:
    """The first page (a Document) at or below ``element``, or ``None``."""
    try:
        if _role(element) == PAGE_ROLE:
            return element
        condition = automation.CreatePropertyCondition(_P_CONTROL_TYPE, 50030)  # UIA_DocumentControlTypeId
        found = element.FindFirst(_TREE_SCOPE_DESCENDANTS, condition)
        return found if found else None
    except Exception:  # noqa: BLE001
        return None


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
        found: dict[str, Any] = {}
        target, where, at_point = find_target(automation, x, y, hwnd, vertical, deadline, facts=found)
        shared["find_ms"] = round((time.monotonic() - started) * 1000, 1)
        shared["where"] = where
        if target is not None:
            shared["stage"] = "pattern"
            shared["outcome"] = scroll_pattern_target(target, vertical=vertical, notches=notches, forward=forward)
            return
        shared["stage"] = "wheel"
        page = bool(found.get("page"))
        watched = at_point
        if page and at_point is not None and _role(at_point) == PAGE_ROLE:
            # V6: the point gave the page itself, whose box never moves: watch an element inside it instead
            inner = marker_in_page(automation, found.get("page_element") or at_point, x, y,
                                   started + budget_s * 0.5)
            shared["page_marker"] = inner is not None
            watched = inner
        elif not page and at_point is not None:
            # report 58: on Chromium/CEF the point answers with the renderer's whole pane (13:30 on 3 Oct: "nothing
            # under the point moved: it did not scroll" while the page scrolled). percFix (5 Oct, YouTube in Arc,
            # 2 of 4 scrolls "did not scroll" while the page moved): the browser's own content host there is not
            # window-sized (the sidebar is beside it) but never moves either. Any element that holds a page is a
            # surface, not content: watch inside the page below it; a window-sized one with no page, nothing
            below = page_below(automation, at_point)
            if below is not None:
                shared["surface"] = True
                page = True
                watched = marker_in_page(automation, below, x, y, started + budget_s * 0.5)
                shared["page_marker"] = watched is not None
            elif window_sized(at_point, hwnd or _root_of(x, y)):
                shared["surface"] = True
                watched = None
        shared["page"] = page

        def cursor() -> tuple[int, int] | None:
            try:
                return tuple(cursor_position())  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                return None

        span: dict[str, float] = {}

        def own(running: bool) -> None:
            # review F4: only the jump, the wheel and the jump back are Yuki's own input (the scroll action itself is
            # leased without a whole-call stamp, yuki.actions.input)
            from yuki.actions.leases import note_own_input

            if running:
                span["begin"] = time.monotonic()
                note_own_input(span["begin"], None)
            else:
                note_own_input(span.get("begin") or time.monotonic(), time.monotonic())

        shared["outcome"] = wheel_at(x, y, send=_wheel_sender(dy if vertical else 0, 0 if vertical else dx),
                                     marker=_marker(watched), cursor=cursor, move=_move, page=page, own=own)

    pending = _Pending(job, "yuki-scroll", budget_s)
    shared = pending.wait()
    facts = {"find_ms": shared.get("find_ms"), "where": shared.get("where"), "stage": shared.get("stage"),
             "error": shared.get("error")}
    if "page_marker" in shared:
        facts["page_marker"] = shared["page_marker"]
    outcome = shared.get("outcome")
    if outcome is None:
        stage = shared.get("stage")
        note = (f"UI Automation did not answer within {budget_s:g} s" if pending.timed_out
                else f"UI Automation failed: {shared.get('error') or 'no answer'}")
        if stage:
            note += f" (it had got as far as the {stage} step, so it may have scrolled)"
        outcome = Outcome(moved=None, how=stage or "none", note=note)
    return outcome, facts


#: The end of a wheel scroll's summary when the one element watched under the point did not move. The dispatcher
#: (``yuki.agent.tools``) drops it when its before/after reads of the window show the content did move (percFix).
POINT_STILL = ", but nothing under the point moved: it did not scroll"


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
        where = " on the web page" if outcome.page else ""
        sent = f"sent {notches} wheel notch(es) {direction}{where} at ({x},{y}) (nothing there takes UI Automation " \
               f"scrolling; the cursor was put back)"
        if outcome.moved:
            return True, f"{sent}; the content moved"
        if outcome.moved is False:
            return False, f"{sent}{POINT_STILL}"
        return True, f"{sent}; {outcome.note or 'whether it moved is not known'}"
    return False, outcome.note or "nothing scrollable was found"


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


# ---------------------------------------------------------------------------
# V7: keep scrolling until they say stop
# ---------------------------------------------------------------------------
#
# Live 3 Oct "keep scrolling until I say stop" could not be done: the thinker scrolled once, tried page down, and gave
# up. A continuous scroll is one object here, shared by the talker's keep_scrolling hand and the thinker's scroll with
# until_stopped: it scrolls the window in steps on its own thread, pauses while the user talks (their words may be
# "stop"), and stops when the talker calls stop, when the Stop hotkey or the Stop button cancels, when the user really
# takes over (a key or a click: :class:`yuki.actions.leases.RealInput`, watched on its own thread during steps too),
# when Yuki goes on to act on the desktop, when its window leaves the front, when the page ends or stops moving, after
# two failed steps, or at its limits. It waits while the user moves the mouse. Its report says where it stopped.
# Measured facts only; no words are read here.

#: Paces: (seconds between steps, wheel notches per step; a screenful is about 10).
PACES: dict[str, tuple[float, int]] = {"slow": (2.6, 3), "normal": (1.7, 4), "fast": (1.0, 6)}
#: A continuous scroll ends by itself after this many steps or this long.
MAX_STEPS = 150
MAX_S = 300.0
#: Two failed steps in a row end it (the window went away, or nothing scrolls there).
MAX_FAILURES = 2
#: Steps in a row on a web page whose watched element did not move: the page has stopped, at its end or stuck (review
#: F5: a page without UI Automation scrolling never reports its end, so it ran on to the 5-minute limit).
MAX_STILL = 3
#: How often the loop looks at its stop and pause signals, and the watch thread at the user's input (s).
TICK_S = 0.1
#: After the user moved the mouse, steps wait until the cursor has been still this long (review F4: a step jumps the
#: cursor to the scroll point and back, never under a moving hand).
MOUSE_STILL_S = 1.0
#: Why it stopped, when it stopped by itself (the rest are a stop someone asked for: voice, stop, replaced, ended,
#: acting).
SELF_STOPS = ("end", "still", "window", "limit", "failed", "user_input", "error")


@dataclass
class StepResult:
    """One step of a continuous scroll, from the scroll's own report."""

    ok: bool
    moved: bool | None = None
    at_end: bool = False
    percent: float | None = None
    report: str = ""
    #: A web page's watched element did not move (review F5).
    still: bool = False
    #: The step was refused because its window is no longer in front (review F6).
    gone: bool = False


def step_result(result: Any) -> StepResult:
    """A :class:`StepResult` from a scroll action's result (``yuki.actions.input.scroll``'s details)."""
    details = getattr(result, "details", None) or {}
    axes = [a for a in details.get("scrolls") or [] if isinstance(a, dict)]
    first = axes[0] if axes else {}
    after = first.get("after")
    return StepResult(ok=bool(getattr(result, "ok", False)), moved=details.get("verified"),
                      at_end=bool(first.get("at_end")),
                      percent=float(after) if isinstance(after, (int, float)) and after >= 0 else None,
                      report=str(getattr(result, "summary", "") or "")[:300],
                      still=bool(first.get("still")), gone=details.get("refused") == "foreground")


class ContinuousScroll:
    """A window's page scrolled on in steps until something stops it (V7).

    Args:
        step: One step (blocking; returns a :class:`StepResult`).
        where: The window as people say it (``Arc "YouTube"``), for its state line and report.
        direction: ``down`` / ``up``; ``pace``: a key of :data:`PACES`.
        hold: While it says True the scroll waits (the user is talking: their words may be "stop").
        took_over: Says True once the user really used the keyboard or mouse buttons (they took over). Polled on a
            watch thread of its own every tick, during steps too (review F4), never on two threads at once.
        moving: While it says True the scroll waits (the user is moving the mouse; :class:`InputWatch`).
        on_stop: Called with :meth:`report` when it stopped by itself (:data:`SELF_STOPS`), off its own thread.
        steps_done: Steps already taken before it started (the caller's first step).
    """

    def __init__(self, step: Callable[[], StepResult], *, where: str, direction: str = "down", pace: str = "normal",
                 hold: Callable[[], bool] | None = None, took_over: Callable[[], bool] | None = None,
                 moving: Callable[[], bool] | None = None,
                 on_stop: Callable[[dict[str, Any]], None] | None = None, max_steps: int = MAX_STEPS,
                 max_s: float = MAX_S, steps_done: int = 0, started_by: str = "voice",
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> None:
        self.step_fn, self.where = step, where
        self.direction = direction if direction in ("down", "up") else "down"
        self.pace = pace if pace in PACES else "normal"
        self.hold, self.took_over, self.moving, self.on_stop = hold, took_over, moving, on_stop
        self.max_steps, self.max_s, self.started_by = int(max_steps), float(max_s), started_by
        self.sleep, self.now = sleep, now
        self.steps = int(steps_done)
        self.failures = 0
        self.stills = 0
        self.percent: float | None = None
        self.last = ""
        self.why: str | None = None
        self.started_at = now()
        self.stopped_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watcher: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.why is None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="yuki-keep-scrolling", daemon=True)
        self._thread.start()
        if self.took_over is not None:
            self._watcher = threading.Thread(target=self._watch, name="yuki-keep-scrolling-watch", daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        """The user's take-over, looked at every tick for as long as it runs, steps included (review F4: looked at only
        between steps, a click during the 1-2 s of a step was gone by the next look)."""
        while not self._stop.is_set():
            try:
                if self.took_over is not None and self.took_over():
                    self._end("user_input")
                    return
            except Exception:  # noqa: BLE001 - a broken read never stops the scroll by itself
                pass
            self._stop.wait(TICK_S)

    def stop(self, why: str = "stop", wait_s: float = 2.0) -> dict[str, Any]:
        """Stop it (``why``: voice / stop / replaced / ended); returns the report."""
        if self.why is None:
            self.why = why
            self.stopped_at = self.now()
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait_s)
        return self.report()

    def _end(self, why: str) -> None:
        if self.why is None:
            self.why = why
            self.stopped_at = self.now()
        self._stop.set()

    def _wait(self, seconds: float) -> bool:
        """Wait between steps, looking at the stop every tick; False once it must stop (the take-over is the watch
        thread's)."""
        until = self.now() + seconds
        while not self._stop.is_set():
            if self.now() >= until:
                return True
            self.sleep(TICK_S)
        return False

    def _held(self) -> bool:
        """The user is talking, or moving the mouse: the next step waits."""
        for signal in (self.hold, self.moving):
            try:
                if signal is not None and signal():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _run(self) -> None:
        pace_s = PACES[self.pace][0]
        try:
            if not self._wait(pace_s):
                return
            while not self._stop.is_set():
                if self.steps >= self.max_steps or self.now() - self.started_at >= self.max_s:
                    self._end("limit")
                    break
                if self._held():
                    if not self._wait(TICK_S):
                        break
                    continue
                result = self.step_fn()
                if self._stop.is_set():
                    break
                self.steps += 1
                self.last = result.report
                if result.gone:
                    self._end("window")   # its window left the front: never on into another window (review F6)
                    break
                if result.still:
                    self.stills += 1
                    if self.stills >= MAX_STILL:
                        self._end("still")
                        break
                else:
                    self.stills = 0
                if not result.ok:
                    self.failures += 1
                    if self.failures >= MAX_FAILURES:
                        self._end("failed")
                        break
                else:
                    self.failures = 0
                    if result.percent is not None:
                        self.percent = result.percent
                if result.at_end:
                    self._end("end")
                    break
                if not self._wait(pace_s):
                    break
        except Exception as exc:  # noqa: BLE001 - a broken step ends it, reported
            self.last = f"{type(exc).__name__}: {exc}"[:200]
            self._end("error")
        finally:
            if self.why is None:
                self._end("stop")
            if self.why in SELF_STOPS and self.on_stop is not None:
                try:
                    self.on_stop(self.report())
                except Exception:  # noqa: BLE001 - the report is a courtesy
                    pass
            _forget(self)

    def report(self) -> dict[str, Any]:
        """Where it stopped (or is): steps, seconds, how far down when the page says, and why it stopped."""
        end = self.stopped_at if self.stopped_at is not None else self.now()
        return {"where": self.where, "direction": self.direction, "pace": self.pace, "steps": self.steps,
                "seconds": round(end - self.started_at, 1),
                "percent": None if self.percent is None else round(self.percent), "why": self.why,
                "running": self.why is None, "last": self.last, "started_by": self.started_by}

    def line(self) -> str:
        """The state line for the talker while it runs."""
        r = self.report()
        far = f", about {r['percent']}% of the way" if r["percent"] is not None else ""
        return f"{r['where']}, {r['direction']}, {r['pace']} pace, {r['steps']} steps in {r['seconds']:g} s{far}"


def stopped_words(report: dict[str, Any] | None) -> str:
    """The report in plain words (for a tool result or the action log): how far it went and why it stopped."""
    if not report:
        return "nothing was scrolling"
    far = f", about {report['percent']}% of the way {report['direction']}" if report.get("percent") is not None else ""
    why = {"voice": "stopped as they asked", "stop": "stopped", "replaced": "replaced by a new scroll",
           "ended": "stopped when the conversation ended", "end": "reached the end of the page",
           "still": f"stopped: the page showed no movement for {MAX_STILL} steps (its end, or it would not scroll)",
           "window": "stopped: that window was no longer in front",
           "acting": "stopped when Yuki went on to act on the desktop",
           "limit": "stopped at its limit", "failed": "stopped: the page would not scroll any more",
           "user_input": "stopped: they took over the keyboard or mouse", "error": "stopped on an error"}
    return (f"scrolled {report.get('where')} {report.get('direction')} for {report.get('steps')} steps in "
            f"{report.get('seconds')} s{far}; {why.get(str(report.get('why')), str(report.get('why')))}")


_LOCK = threading.Lock()
_RUNNING: ContinuousScroll | None = None
_VOICE_HOLD: Callable[[], bool] | None = None


def _forget(scroller: ContinuousScroll) -> None:
    global _RUNNING
    with _LOCK:
        if _RUNNING is scroller:
            _RUNNING = None


def set_voice_hold(fn: Callable[[], bool] | None) -> None:
    """The live voice conversation's "the user is talking" signal (``None`` when no conversation runs)."""
    global _VOICE_HOLD
    _VOICE_HOLD = fn


def voice_hold() -> bool:
    """Whether a continuous scroll should wait now because the user is talking."""
    fn = _VOICE_HOLD
    try:
        return bool(fn()) if fn is not None else False
    except Exception:  # noqa: BLE001
        return False


def claim_continuous(scroller: ContinuousScroll) -> dict[str, Any] | None:
    """Make ``scroller`` the continuous scroll before its first step runs, so a stop said during that step stops it
    (review F8: "stop" during the first step found nothing running, then the scroll started). A continuous scroll
    already running is stopped first (its report is returned)."""
    global _RUNNING
    with _LOCK:
        old, _RUNNING = _RUNNING, scroller
    return old.stop("replaced") if old is not None and old is not scroller and old.running else None


def start_continuous(scroller: ContinuousScroll) -> dict[str, Any] | None:
    """Start ``scroller`` (claimed first unless :func:`claim_continuous` already did); a continuous scroll already
    running is stopped first (its report is returned). One stopped while it was claimed is never started."""
    with _LOCK:
        claimed = _RUNNING is scroller
    report = None if claimed else claim_continuous(scroller)
    if not scroller.running:
        _forget(scroller)   # a stop came during its first step
        return report
    scroller.start()
    return report


def drop_continuous(scroller: ContinuousScroll) -> None:
    """A claimed scroller that will not go on (its first step failed or the page was at its end): forgotten, quietly."""
    if scroller.running:
        scroller.stop("not_started", wait_s=0.0)
    _forget(scroller)


def stop_continuous(why: str = "stop", wait_s: float = 2.0) -> dict[str, Any] | None:
    """Stop the continuous scroll (``None`` when none runs); returns its report. ``wait_s``: how long to wait for a
    step in progress to finish (0 on the GUI thread or the voice loop: the stop is set at once either way)."""
    with _LOCK:
        running = _RUNNING
    return running.stop(why, wait_s=wait_s) if running is not None and running.running else None


def continuous_running() -> ContinuousScroll | None:
    with _LOCK:
        running = _RUNNING
    return running if running is not None and running.running else None


def continuous_state() -> str | None:
    """The talker's state line while a continuous scroll runs, else ``None``."""
    running = continuous_running()
    return running.line() if running is not None else None


class InputWatch:
    """The user's input while a continuous scroll runs (review F4), looked at on the scroll's watch thread every tick.

    Called, it says whether they took over: a key or a mouse button went down (or was pressed since the last look),
    or other input came with the cursor still (:class:`yuki.actions.leases.RealInput`; Yuki's own wheel moment is left
    out). :meth:`moving`: they moved the cursor within :data:`MOUSE_STILL_S` (Yuki's own cursor jump left out), so the
    next step waits and never jumps the cursor under their hand. Measured facts only; no hook.

    Args:
        real: The input reader (tests pass one on stubs).
        own_now: Whether one of Yuki's own input moments is running or just ended
            (:func:`yuki.actions.leases.own_input_now`).
    """

    def __init__(self, real: Any = None, *, own_now: Callable[[float], bool] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        from yuki.actions.leases import RealInput, own_input_now

        self.real = real if real is not None else RealInput()
        self.own_now = own_now or own_input_now
        self.now = now
        self.moved_at: float | None = None
        self.real.poll()   # the baseline: input before now does not count
        self.real.last_real = None
        self.since = self.now()

    def __call__(self) -> bool:
        before = self.real.cursor_seen
        last = self.real.poll()
        after = self.real.cursor_seen
        t = self.now()
        if before is not None and after is not None and after != before and not self.own_now(t):
            self.moved_at = t
        return last is not None and last > self.since

    def moving(self) -> bool:
        return self.moved_at is not None and self.now() - self.moved_at < MOUSE_STILL_S


def input_watch() -> InputWatch:
    """A "they took over" signal from now on (:class:`InputWatch`; its ``moving`` is the mouse-moving hold)."""
    return InputWatch()
