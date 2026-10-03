"""Resource leases and yielding to the user (revamp D, S7 "quieter hands"; 07 §6 G, 11 §12 #9).

One mouse and one keyboard, several hands: the brain's actions, the voice's own hands, a helper that only reads. Two
kinds of lease keep them from colliding without making everything wait for everything:

* the **input lease** (one holder at a time, re-entrant per thread): taken by every action that sends global mouse or
  keyboard input (click by coordinates, typing, keys, wheel scrolling, hover), together with the target window's lease;
* a **window lease** per hwnd: taken by an action that works on one window through UI Automation (an Invoke, a
  Toggle, a SelectionItem.Select, a Value.SetValue, a ScrollPattern scroll, a tab select). It needs no input lease, so
  a pattern action on one window proceeds while a typing job holds the input lease for another.

**Yielding to the user**: before global input, :class:`InputGate` asks Windows when the keyboard or mouse was last used
(``GetLastInputInfo``: a time, not an input hook) and leaves out Yuki's own synthetic input (the time of its last
``SendInput`` is kept here, :func:`note_own_input`). Used within ``recent_s``: it says one line through ``notify`` and
waits until ``quiet_s`` pass with no input from the user, at most ``max_s``; past that nothing is sent and the caller is
told. Numbers, measured on the clock, never on what was typed.
"""

from __future__ import annotations

import ctypes
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

#: The global input lease (re-entrant: an action that clicks to focus a field and then types holds it throughout).
_INPUT = threading.RLock()
#: One lease per window, made on first use.
_WINDOWS: dict[int, threading.RLock] = {}
#: How many hands hold or wait for each window's lease (taken under the guard before the lock is tried, given back
#: after it is released): a window lease is forgotten only when nobody holds or waits for it.
_WINDOW_USERS: dict[int, int] = {}
_WINDOWS_GUARD = threading.Lock()
#: Past this many window leases the idle ones are forgotten (at most :data:`WINDOWS_FORGET` at a time).
WINDOWS_KEEP = 256
WINDOWS_FORGET = 128
#: ``(start, end)`` (``time.monotonic()``) of Yuki's latest input action; ``end`` is ``None`` while it runs. Input
#: Windows stamped inside that span (plus :data:`OWN_INPUT_SLACK_S`) is Yuki's own.
_own_span: tuple[float, float | None] = (0.0, 0.0)

#: How long an action waits for a lease before it gives up (s).
INPUT_LEASE_S = 10.0
WINDOW_LEASE_S = 5.0
#: Input this soon after Yuki's own last synthetic input is taken for Yuki's (s): Windows stamps it when it lands.
OWN_INPUT_SLACK_S = 0.35


class LeaseBusy(RuntimeError):
    """A lease could not be had in time (another hand holds it)."""


def note_own_input(start: float | None = None, end: float | None = None) -> None:
    """Record the span of one of Yuki's own input actions (``end`` ``None``: still running; both ``None``: an instant
    send now)."""
    global _own_span
    now = time.monotonic()
    _own_span = (now if start is None else float(start), (now if start is None else None) if end is None else float(end))


def own_input_span() -> tuple[float, float | None]:
    return _own_span


def own_input_at() -> float:
    """When Yuki's latest input action ended (or now, while one runs)."""
    start, end = _own_span
    return time.monotonic() if end is None and start else float(end or 0.0)


def leased(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a global-input action: it holds the input lease (and its ``expect_hwnd`` window's lease) while it runs, and
    its span is recorded as Yuki's own input. A lease that cannot be had in time is an ``ok=False`` result."""
    import functools

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        hwnd = kwargs.get("expect_hwnd")
        try:
            with input_lease(int(hwnd) if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd else None):
                begin = time.monotonic()
                note_own_input(begin, None)
                try:
                    return fn(*args, **kwargs)
                finally:
                    note_own_input(begin, time.monotonic())
        except LeaseBusy as exc:
            from yuki.actions import ActionResult

            return ActionResult(ok=False, summary=f"Not sent: {exc}; nothing was typed or clicked",
                                details={"lease": "busy"})

    run.__wrapped_by_lease__ = True   # type: ignore[attr-defined]
    return run


@contextmanager
def input_lease(hwnd: int | None = None, *, timeout_s: float = INPUT_LEASE_S) -> Iterator[None]:
    """Hold the global input lease (and ``hwnd``'s window lease when given) for the duration of the block."""
    if not _INPUT.acquire(timeout=timeout_s):
        raise LeaseBusy("another hand is using the mouse and keyboard")
    try:
        if hwnd:
            with window_lease(int(hwnd), timeout_s=timeout_s):
                yield
        else:
            yield
    finally:
        _INPUT.release()


@contextmanager
def window_lease(hwnd: int, *, timeout_s: float = WINDOW_LEASE_S) -> Iterator[None]:
    """Hold one window's lease (an action that works on that window through UI Automation)."""
    hwnd = int(hwnd)
    with _WINDOWS_GUARD:
        lock = _WINDOWS.setdefault(hwnd, threading.RLock())
        _WINDOW_USERS[hwnd] = _WINDOW_USERS.get(hwnd, 0) + 1
        if len(_WINDOWS) > WINDOWS_KEEP:
            _forget_idle_windows(keep=hwnd)
    try:
        if not lock.acquire(timeout=timeout_s):
            raise LeaseBusy(f"another hand is acting on window {hwnd}")
        try:
            yield
        finally:
            lock.release()
    finally:
        with _WINDOWS_GUARD:
            left = _WINDOW_USERS.get(hwnd, 1) - 1
            if left > 0:
                _WINDOW_USERS[hwnd] = left
            else:
                _WINDOW_USERS.pop(hwnd, None)


def _forget_idle_windows(*, keep: int) -> int:
    """Windows come and go: forget up to :data:`WINDOWS_FORGET` window leases nobody holds or waits for (called under
    the guard). Each candidate is tried without blocking and released at once, so no lock is ever left held; only
    those stale keys are popped. Returns how many were forgotten."""
    stale: list[int] = []
    for key, lock in list(_WINDOWS.items()):
        if len(stale) >= WINDOWS_FORGET:
            break
        if key == keep or _WINDOW_USERS.get(key, 0) > 0:
            continue   # held, or about to be tried, by some hand (an RLock would let its own thread in again)
        if lock.acquire(blocking=False):
            lock.release()
            stale.append(key)
    for key in stale:
        _WINDOWS.pop(key, None)
    return len(stale)


def input_held_elsewhere() -> bool:
    """Whether another thread holds the input lease right now (a pattern action does not need it)."""
    got = _INPUT.acquire(blocking=False)
    if got:
        _INPUT.release()
    return not got


# ---------------------------------------------------------------------------
# Yielding to the user
# ---------------------------------------------------------------------------


def last_input_age_s() -> float | None:
    """Seconds since the keyboard or mouse was last used, any source (``GetLastInputInfo``), ``None`` unknown."""
    try:
        class _Info(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _Info(ctypes.sizeof(_Info), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        idle_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0
    except Exception:  # noqa: BLE001 - not on Windows, or no desktop
        return None


def user_input_age_s(*, age: Callable[[], float | None] = last_input_age_s,
                     span: Callable[[], tuple[float, float | None]] = own_input_span,
                     now: Callable[[], float] = time.monotonic) -> float | None:
    """Seconds since the *user* last used the keyboard or mouse. When the last input is Yuki's own (stamped inside
    the span of Yuki's latest input action, plus :data:`OWN_INPUT_SLACK_S`) the user has not touched anything since,
    and their own last input is older: that is not recent (``inf``). ``None`` when Windows cannot say."""
    seconds = age()
    if seconds is None:
        return None
    t = now()
    last = t - seconds
    start, end = span()
    if start and start - 0.05 <= last <= (t if end is None else end) + OWN_INPUT_SLACK_S:
        return float("inf")
    return seconds


class InputGate:
    """Wait for the user to stop typing or moving the mouse before global input (one notice per wait).

    Args:
        recent_s: The user's input this recent means they are using the PC now.
        quiet_s: How long they must be still before Yuki's input goes in.
        max_s: The longest wait; then the action is not sent.
        age: Seconds since the user's last input (:func:`user_input_age_s`; tests pass a stub).
    """

    def __init__(self, *, recent_s: float = 1.0, quiet_s: float = 0.8, max_s: float = 12.0,
                 age: Callable[[], float | None] = user_input_age_s, poll_s: float = 0.1,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> None:
        self.recent_s, self.quiet_s, self.max_s = float(recent_s), float(quiet_s), float(max_s)
        self.age, self.poll_s, self.sleep, self.now = age, float(poll_s), sleep, now

    def wait(self, *, notify: Callable[[str], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """``{"waited": bool, "ok": bool, "ms", "why"?}``: ``ok`` False means the user kept going past ``max_s``
        (or the request was cancelled): send nothing."""
        first = self.age()
        if first is None or first >= self.recent_s:
            return {"waited": False, "ok": True, "ms": 0.0}
        started = self.now()
        if notify is not None:
            try:
                notify("You're using the keyboard or mouse, so I'll wait a moment before I type or click.")
            except Exception:  # noqa: BLE001
                pass
        while True:
            if cancelled is not None and cancelled():
                return {"waited": True, "ok": False, "ms": round((self.now() - started) * 1000), "why": "cancelled"}
            seconds = self.age()
            if seconds is None or seconds >= self.quiet_s:
                return {"waited": True, "ok": True, "ms": round((self.now() - started) * 1000)}
            if self.now() - started >= self.max_s:
                return {"waited": True, "ok": False, "ms": round((self.now() - started) * 1000),
                        "why": f"the user kept using the keyboard or mouse for {self.max_s:g} s"}
            self.sleep(self.poll_s)


__all__ = ["INPUT_LEASE_S", "InputGate", "LeaseBusy", "WINDOW_LEASE_S", "input_held_elsewhere", "input_lease",
           "last_input_age_s", "leased", "note_own_input", "own_input_at", "own_input_span", "user_input_age_s",
           "window_lease"]
