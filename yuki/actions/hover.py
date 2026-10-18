"""Hover: put the pointer on an element and report what appeared (a verified pointer move).

Some controls only show themselves while the pointer is over something: YouTube's volume
slider appears on hover over the volume button (clicking that button mutes instead). The
brain had no way to do that (the memory builder's note, 2026-09-26). This moves the pointer
onto the point, in the window it belongs to only while that window is in front, then watches
the UI Automation elements around the point for up to :data:`WATCH_S` and reports what is
new there (role, name, where), so the next read of the window can act on it. Nothing is
clicked or typed. The pointer is left where it is, because moving it away would hide what
the hover showed.
"""

from __future__ import annotations

import time
from typing import Any

from yuki.actions import ActionResult

#: How long the elements near the point are watched for something new (a condition poll).
WATCH_S = 1.0
#: The box around the point that is read (px each side), and the most elements read in it.
RADIUS = 260
MAX_NODES = 400
POLL_S = 0.1


def _near(automation: Any, x: int, y: int, deadline: float) -> dict[tuple[str, str], tuple[int, int]]:
    """``{(role, name): centre}`` of the named elements within :data:`RADIUS` of (x, y), bounded."""
    from yuki.actions.input import _cache_request, _element_facts

    request = _cache_request(automation)
    try:
        point = automation.ElementFromPoint(_point(x, y))
    except Exception:  # noqa: BLE001
        return {}
    # Walk up a few levels so a sibling that appears next to the pointed element is inside the read.
    walker = automation.ControlViewWalker
    root = point
    for _ in range(3):
        try:
            parent = walker.GetParentElement(root)
        except Exception:  # noqa: BLE001
            break
        if not parent:
            break
        root = parent
    out: dict[tuple[str, str], tuple[int, int]] = {}
    try:
        found = root.FindAllBuildCache(4, automation.ControlViewCondition, request)  # TreeScope_Descendants
    except Exception:  # noqa: BLE001
        return out
    for i in range(min(found.Length, MAX_NODES)):
        if time.monotonic() > deadline:
            break
        facts = _element_facts(found.GetElement(i))
        left, top, right, bottom = facts["bounds"]
        cx, cy = (left + right) // 2, (top + bottom) // 2
        if right > left and abs(cx - x) <= RADIUS and abs(cy - y) <= RADIUS and (facts["name"] or facts["value"]):
            out[(facts["role"], facts["name"] or str(facts["value"])[:60])] = (cx, cy)
    return out


def _point(x: int, y: int) -> Any:
    from ctypes import wintypes

    return wintypes.POINT(int(x), int(y))


def hover(x: int, y: int, *, expect_hwnd: int | None = None, label: str = "") -> ActionResult:
    """Move the pointer onto (x, y) (in ``expect_hwnd`` while it is in front) and report what appeared.

    ``ok`` when the pointer is there; ``details["appeared"]`` lists the new elements near it
    (``verified`` when at least one appeared, since that is what a hover is for).
    """
    from yuki.actions.input import _foreground_mismatch, _in_screen_bounds, _move, _Pending

    started = time.perf_counter()
    details: dict[str, Any] = {"x": int(x), "y": int(y), "verified": False, "appeared": []}

    def done(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1))

    if not _in_screen_bounds(int(x), int(y)):
        return done(False, f"({x},{y}) is off every screen; the pointer was not moved")
    mismatch = _foreground_mismatch(expect_hwnd)
    if mismatch:
        return done(False, f"{mismatch}; the pointer was not moved")

    def job(automation: Any, shared: dict) -> None:
        shared["before"] = _near(automation, int(x), int(y), time.monotonic() + 0.6)
        why: list[str] = []
        if not _move(int(x), int(y), why=why):
            # D17 (fGate review r2 L2): a game in front, or a game's window at the point: the pointer was not moved
            shared["refused"] = why[0] if why else "the pointer move was refused"
            return
        shared["moved"] = True
        deadline = time.monotonic() + WATCH_S
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            now = _near(automation, int(x), int(y), time.monotonic() + 0.4)
            new = {k: v for k, v in now.items() if k not in shared["before"]}
            if new:
                shared["appeared"] = new
                break

    pending = _Pending(job, "yuki-hover", 0.6 + WATCH_S + 0.8)
    shared = pending.wait()
    if shared.get("refused"):
        from yuki.actions.gate import kind_of

        details["refused"] = kind_of(shared["refused"])   # "game" only when a game refused it
        return done(False, f"{shared['refused']}; the pointer was not moved")
    if not shared.get("moved"):
        return done(False, f"could not move the pointer ({shared.get('error') or 'timed out'})")
    appeared = shared.get("appeared") or {}
    details["appeared"] = [{"role": r, "name": n, "center": c} for (r, n), c in list(appeared.items())[:8]]
    details["verified"] = bool(appeared)
    where = f"on {label} " if label else ""
    if appeared:
        listing = "; ".join(f'{r} "{n[:50]}" at ({c[0]},{c[1]})' for (r, n), c in list(appeared.items())[:8])
        return done(True, f"the pointer is {where}at ({x},{y}); appeared near it: {listing}. Read the window again to "
                          f"act on it (the pointer stays, so it stays shown)")
    return done(True, f"the pointer is {where}at ({x},{y}); nothing new appeared near it within {WATCH_S:g} s")


__all__ = ["hover"]


# Revamp D (S7): the pointer move holds the input lease like every global-input action (yuki.actions.leases).
from yuki.actions.leases import leased as _leased  # noqa: E402

hover = _leased(hover)
