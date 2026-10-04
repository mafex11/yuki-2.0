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
import math
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


def own_input_now(now: float | None = None, *, span: Callable[[], tuple[float, float | None]] | None = None) -> bool:
    """Whether one of Yuki's own input moments is running, or ended less than :data:`OWN_INPUT_SLACK_S` ago (a cursor
    that moved then was moved by Yuki)."""
    start, end = (span or own_input_span)()
    if not start:
        return False
    t = time.monotonic() if now is None else now
    return end is None or t <= end + OWN_INPUT_SLACK_S


def leased(fn: Callable[..., Any], *, stamp: bool = True) -> Callable[..., Any]:
    """Wrap a global-input action: it holds the input lease (and its ``expect_hwnd`` window's lease) while it runs, and
    its span is recorded as Yuki's own input. A lease that cannot be had in time is an ``ok=False`` result.

    ``stamp`` False: the action records its own input moments itself (:func:`note_own_input` around each send). A
    scroll spends most of its time finding its target and watching the page, sending nothing; stamped whole, every
    input of the user's in that time was taken for Yuki's (review F4: a click to take over a running "keep
    scrolling" was missed)."""
    import functools

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        hwnd = kwargs.get("expect_hwnd")
        try:
            with input_lease(int(hwnd) if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd else None):
                if not stamp:
                    return fn(*args, **kwargs)
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


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_age_s() -> float | None:
    """Seconds since the keyboard or mouse was last used, any source (``GetLastInputInfo``), ``None`` unknown."""
    try:
        # (the structure is made once: a class made per call cost ~0.15 ms, and the takeover watch asks 10 times a second)
        info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
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


#: Virtual-key codes polled for "a key or a mouse button is held down now": the mouse buttons (1, 2, 4, 5, 6) and every
#: keyboard key (8..254) - Windows' own fixed numbering.
_REAL_INPUT_VKS: tuple[int, ...] = (1, 2, 4, 5, 6, *range(8, 255))


def key_or_button_down() -> bool | None:
    """Whether a keyboard key or a mouse button is held down right now, or went down since the last look
    (``GetAsyncKeyState``'s high bit, and its low bit "pressed since the last call"; polled only while Yuki waits to act
    or a continuous scroll runs: no input hook), ``None`` when Windows cannot say. A moving mouse presses nothing.

    The low bit catches a quick click between two looks that a move hid (review F4: a move and a click inside one
    tick read as the mouse moving). It is Windows' best effort (another program's call can take it first), so it only
    adds to the high bit; the first look of a watch is a baseline (:class:`RealInput`), since it may be old."""
    try:
        get = ctypes.windll.user32.GetAsyncKeyState
        # every key is asked each look (no early stop), so no key's "pressed since" bit is left over for the next one
        return bool(sum(1 for vk in _REAL_INPUT_VKS if get(vk) & 0x8001))
    except Exception:  # noqa: BLE001 - not on Windows
        return None


def cursor_at() -> tuple[int, int] | None:
    """Where the mouse cursor is (``GetCursorPos``), ``None`` when Windows cannot say."""
    try:
        from ctypes import wintypes

        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception:  # noqa: BLE001
        return None


class RealInput:
    """When the user last really used the PC: a key or a mouse button (V8, 3 Oct). ``GetLastInputInfo`` also counts
    every mouse move (and input another machine sends through a shared mouse), so live Yuki said twice "you're using the
    keyboard or mouse" while the user touched nothing. Polled while Yuki waits to act (no hook): an input that came
    while the cursor stayed where it was (a key, a click, the wheel) or while a key or button was down (or went down
    since the last look) counts; one that came with the cursor moving and nothing pressed is the mouse moving, and does
    not. The first look is a baseline (what was pressed before it does not count). Measured facts only."""

    def __init__(self, *, age: Callable[[], float | None] = user_input_age_s,
                 held: Callable[[], bool | None] = key_or_button_down,
                 cursor: Callable[[], tuple[int, int] | None] = cursor_at,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.age, self.held, self.cursor, self.now = age, held, cursor, now
        self.last_real: float | None = None   # when real input was last seen (monotonic), None: not yet
        self._last_input: float | None = None
        self._cursor: tuple[int, int] | None = None
        #: The cursor was seen at two different places on two looks in a row (the mouse moved while watched).
        self.cursor_moved = False
        self.polls = 0

    @property
    def cursor_seen(self) -> tuple[int, int] | None:
        """Where the cursor was on the latest look (``None``: not looked yet, or unknown)."""
        return self._cursor

    def poll(self) -> float | None:
        """Look once; returns :attr:`last_real`."""
        t = self.now()
        seconds = self.age()
        last = None if seconds is None or seconds == float("inf") else t - seconds
        where = self.cursor()
        down = self.held()
        if down and self.polls:
            self.last_real = t
        elif last is not None and self._last_input is not None and last > self._last_input + 0.005:
            # new input since the last look: real unless the cursor moved with it
            if where is not None and self._cursor is not None and where == self._cursor:
                self.last_real = max(self.last_real or 0.0, last)
        if where is not None and self._cursor is not None and where != self._cursor:
            self.cursor_moved = True
        if last is not None:
            self._last_input = last if self._last_input is None else max(self._last_input, last)
        self._cursor = where
        self.polls += 1
        return self.last_real


#: The line said when Yuki has waited this long for the user to stop typing or clicking (V8: before, it was said at
#: the start of every wait, mouse moves included; now the wait is silent unless it goes on).
INPUT_WAIT_LINE = "You're typing or clicking, so I'll wait a moment before I do it."


class InputGate:
    """Wait for the user to stop typing or clicking before global input; silent unless the wait goes on (V8).

    Args:
        recent_s: Input this recent (any kind, ``GetLastInputInfo``) makes the gate look closer.
        quiet_s: How long they must be still (no key, no click) before Yuki's input goes in.
        max_s: The longest wait; then the action is not sent.
        age: Seconds since the user's last input (:func:`user_input_age_s`; tests pass a stub).
        probe_s: How long the gate watches recent input to tell a key or click from the mouse moving. With no real
            input seen in that time and the cursor seen moving, the input that made it look was the mouse: Yuki goes
            ahead. With the cursor still the whole time, that input was a key, a click or the wheel, and Yuki waits
            until ``quiet_s`` after it (review F3: it went ahead 0.25 s after a keystroke, inside a typing pause).
        notify_after_s: The one notice is said only when the wait has lasted this long (~3 s).
        held, cursor: The key-or-button and cursor reads (:class:`RealInput`; tests pass stubs).
    """

    def __init__(self, *, recent_s: float = 1.0, quiet_s: float = 0.8, max_s: float = 12.0,
                 age: Callable[[], float | None] = user_input_age_s, poll_s: float = 0.05,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
                 probe_s: float = 0.25, notify_after_s: float = 3.0,
                 held: Callable[[], bool | None] = key_or_button_down,
                 cursor: Callable[[], tuple[int, int] | None] = cursor_at) -> None:
        self.recent_s, self.quiet_s, self.max_s = float(recent_s), float(quiet_s), float(max_s)
        self.age, self.poll_s, self.sleep, self.now = age, float(poll_s), sleep, now
        self.probe_s, self.notify_after_s = float(probe_s), float(notify_after_s)
        self.held, self.cursor = held, cursor

    def wait(self, *, notify: Callable[[str], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """``{"waited": bool, "ok": bool, "ms", "why"?, "real"?}``: ``ok`` False means the user kept typing or clicking
        past ``max_s`` (or the request was cancelled): send nothing. ``real``: whether a key or click was seen."""
        first = self.age()
        if first is None or first >= self.recent_s:
            return {"waited": False, "ok": True, "ms": 0.0}
        started = self.now()
        trigger = started - float(first)   # the input that made the gate look: its kind is not known yet
        watch = RealInput(age=self.age, held=self.held, cursor=self.cursor, now=self.now)
        notified = False
        while True:
            if cancelled is not None and cancelled():
                return {"waited": True, "ok": False, "ms": round((self.now() - started) * 1000), "why": "cancelled",
                        "real": watch.last_real is not None}
            last_real = watch.poll()
            t = self.now()
            ms = round((t - started) * 1000)
            if last_real is None and t - started >= self.probe_s:
                if watch.cursor_moved:
                    return {"waited": ms > 0, "ok": True, "ms": ms, "real": False}   # only the mouse moved: go ahead
                # the cursor stayed still while watched (or cannot be read): the input that made the gate look was a
                # key, a click or the wheel, never told apart from the mouse moving, so it counts
                last_real = watch.last_real = trigger
            if last_real is not None and t - last_real >= self.quiet_s:
                return {"waited": True, "ok": True, "ms": ms, "real": True}
            if t - started >= self.max_s:
                return {"waited": True, "ok": False, "ms": ms, "real": last_real is not None,
                        "why": f"the user kept typing or clicking for {self.max_s:g} s"}
            if notify is not None and not notified and t - started >= self.notify_after_s:
                notified = True
                try:
                    notify(INPUT_WAIT_LINE)
                except Exception:  # noqa: BLE001
                    pass
            self.sleep(self.poll_s)


# ---------------------------------------------------------------------------
# Taking over: the user's real input while a request acts (appTakeover, 4 Oct)
# ---------------------------------------------------------------------------
#
# The user, 4 Oct: "when it's working and I start typing with my keyboard or I suddenly move my mouse then it should
# think that I have taken over ... and it should stop temporarily ... not a very small vibration, a big movement,
# because my mouse sensor is bad and sometimes vibrates on its own." While a request acts, :class:`TakeoverThread`
# looks a few times a second (``GetLastInputInfo``, ``GetCursorPos``, and ``GetAsyncKeyState`` only when some input
# came since the last look: no hook, nothing at all while no request acts) for a key or a mouse button going down, or
# the cursor travelling far. Yuki's own input moments are left out (its own span, and any span that began since the
# last look: its keys and its cursor jumps are never the user's). Numbers on the clock and in pixels only.

#: How far (screen pixels) the cursor must travel within :data:`TAKEOVER_WINDOW_S` to count as the user moving the
#: mouse, by default. A sensor that twitches by itself moves the cursor a few pixels back and forth (it never gets far
#: from where it was); a hand reaching for something covers tens to hundreds of pixels in a fraction of a second. The
#: user's setting (``ui.json`` ``takeover_move_px``, Settings) overrides it, within the bounds below.
TAKEOVER_MOVE_PX = 40
TAKEOVER_MOVE_MIN_PX = 12
TAKEOVER_MOVE_MAX_PX = 400
#: The window the travel is measured over (s), and how often the watch looks (s).
TAKEOVER_WINDOW_S = 0.3
TAKEOVER_POLL_S = 0.1
#: Input already going on when the watch began (typing in a chat, keys held in a game as the request starts) is the
#: baseline, not a takeover: keys, clicks and movement count only once the user has been still this long (s) since the
#: watch began, or their last input was Yuki's own. The input gate's own "stopped typing" gap (``InputGate.quiet_s``),
#: measured on the clock (review F3).
TAKEOVER_QUIET_S = 0.8
#: The mouse buttons among :data:`_REAL_INPUT_VKS` (a button going down is a click; any other key is typing).
_MOUSE_VKS = frozenset({1, 2, 4, 5, 6})


def key_states(vks: tuple[int, ...] = _REAL_INPUT_VKS) -> tuple[frozenset[int], frozenset[int]] | None:
    """``(down, tapped)``: the keys and mouse buttons held down now (``GetAsyncKeyState``'s high bit), and those pressed
    and let go since the last look (its low bit, the key no longer down); ``None`` when Windows cannot say. Every key
    is asked each look, so no "pressed since" bit is left over for the next one."""
    try:
        get = ctypes.windll.user32.GetAsyncKeyState
        down: set[int] = set()
        tapped: set[int] = set()
        for vk in vks:
            state = get(vk)
            if state & 0x8000:
                down.add(vk)
            elif state & 1:
                tapped.add(vk)
        return frozenset(down), frozenset(tapped)
    except Exception:  # noqa: BLE001 - not on Windows
        return None


#: process id -> whether it is Yuki's (this process, or a program in Yuki's own folder: the window, the memory service).
_YUKI_PIDS: dict[int, bool] = {}


def _pid_is_yuki(pid: int) -> bool:
    if pid <= 0:
        return False
    known = _YUKI_PIDS.get(pid)
    if known is not None:
        return known
    import os

    mine = pid == os.getpid()
    if not mine:
        try:
            import sys
            from pathlib import Path

            import psutil

            from yuki.core.paths import app_dir

            exe = Path(psutil.Process(pid).exe()).resolve()
            mine = any(exe.is_relative_to(root) for root in (app_dir().resolve(), Path(sys.prefix).resolve()))
        except Exception:  # noqa: BLE001 - gone, or not ours to read
            mine = False
    if len(_YUKI_PIDS) > 512:
        _YUKI_PIDS.clear()
    _YUKI_PIDS[pid] = mine
    return mine


def _window_pid(hwnd: int) -> int:
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def yuki_has_it(kind: str, where: tuple[int, int] | None) -> bool:
    """Whether the user's input went to Yuki itself, not the desktop: keys typed into a Yuki window in front (the ask
    bar, the window's chat: words for Yuki), a click or the cursor on one of Yuki's windows (the island's buttons).
    Measured on the window, never on what was typed."""
    try:
        user32 = ctypes.windll.user32
        if kind == "keys":
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            hwnd = int(user32.GetForegroundWindow() or 0)
        else:
            if where is None:
                return False
            from ctypes import wintypes

            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hit = user32.WindowFromPoint(wintypes.POINT(int(where[0]), int(where[1])))
            hwnd = int(user32.GetAncestor(hit, 2) or 0) if hit else 0      # GA_ROOT
        return bool(hwnd) and _pid_is_yuki(_window_pid(hwnd))
    except Exception:  # noqa: BLE001
        return False


def takeover_move_px(value: Any = None) -> int:
    """The travel that counts as the user moving the mouse (px): ``value`` (or the user's ``ui.json`` setting), kept
    within :data:`TAKEOVER_MOVE_MIN_PX`..:data:`TAKEOVER_MOVE_MAX_PX`; :data:`TAKEOVER_MOVE_PX` when unset or unreadable."""
    if value is None:
        try:
            from yuki.ui import ui_settings

            value = getattr(ui_settings.load(), "takeover_move_px", None)
        except Exception:  # noqa: BLE001 - no settings file: the default
            value = None
    try:
        px = int(value)
    except (TypeError, ValueError):
        return TAKEOVER_MOVE_PX
    return max(TAKEOVER_MOVE_MIN_PX, min(TAKEOVER_MOVE_MAX_PX, px))


class TakeoverWatch:
    """Whether the user took over the keyboard or mouse since the watch began: one :meth:`poll` per look.

    * **keys** / **click**: a key or a mouse button went down since the last look (or was pressed and let go between
      two looks), not held from before the watch began;
    * **mouse**: the cursor travelled at least ``move_px`` from where it was within the last ``window_s`` seconds, and
      it is still that far on the next look (a twitching sensor's back-and-forth, or a one-look jump that comes straight
      back, never counts);
    * left out: every look while one of Yuki's own input moments runs or just ended (:data:`OWN_INPUT_SLACK_S`), or
      when one began since the previous look: its keys are consumed and not counted, and the cursor's places from
      before are forgotten (Yuki moves the cursor itself; review vfix2 for scroll); input that went to Yuki itself
      (:func:`yuki_has_it`);
    * not counted yet: input that was already going on when the watch began (the user typing elsewhere, keys held in a
      game): nothing counts until the user has been still for ``quiet_s`` since, or their last input was Yuki's own
      (review F3: the next keystroke of typing that began before the request paused it).

    ``GetAsyncKeyState`` is asked only when ``GetLastInputInfo`` shows new input since the last look (or around one of
    Yuki's own moments): an idle desk costs two calls a look. Every source is a parameter (tests pass stubs).

    Attributes:
        polls: Looks so far.
        max_travel: The farthest the cursor travelled in one window without counting (px): what jitter looked like.
        ignored: Why input was not counted, by reason (``own``, ``yuki``, ``busy``: going on since before the watch).
        armed: Whether the user's input counts yet (they were still for ``quiet_s`` since the watch began).
    """

    def __init__(self, *, move_px: int = TAKEOVER_MOVE_PX, window_s: float = TAKEOVER_WINDOW_S,
                 quiet_s: float = TAKEOVER_QUIET_S,
                 age: Callable[[], float | None] = last_input_age_s,
                 keys: Callable[[], tuple[frozenset[int], frozenset[int]] | None] = key_states,
                 cursor: Callable[[], tuple[int, int] | None] = cursor_at,
                 span: Callable[[], tuple[float, float | None]] = own_input_span,
                 ours: Callable[[str, tuple[int, int] | None], bool] = yuki_has_it,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.move_px, self.window_s, self.quiet_s = float(move_px), float(window_s), float(quiet_s)
        self.age, self.keys, self.cursor, self.span, self.ours, self.now = age, keys, cursor, span, ours, now
        self.armed = False
        self.polls = 0
        self.max_travel = 0.0
        self.ignored: dict[str, int] = {}
        self._down: frozenset[int] = frozenset()
        self._last_input: float | None = None
        self._prev_t: float | None = None
        self._samples: list[tuple[float, int, int]] = []
        self._far: tuple[int, int] | None = None   # where the cursor was when a far travel was first seen

    def _own(self, t: float) -> bool:
        """One of Yuki's own input moments runs, ended a moment ago, or began since the previous look."""
        start, end = self.span()
        if not start:
            return False
        if end is None or t <= end + OWN_INPUT_SLACK_S:
            return True
        return self._prev_t is not None and start >= self._prev_t - 0.05

    def _was_own(self, last: float, t: float) -> bool:
        """The last input (any source) came inside one of Yuki's own input moments (or the slack after it)."""
        start, end = self.span()
        return bool(start) and start - 0.05 <= last <= (t if end is None else end) + OWN_INPUT_SLACK_S

    def _skip(self, why: str) -> None:
        self.ignored[why] = self.ignored.get(why, 0) + 1

    def poll(self) -> str | None:
        """Look once: ``"keys"``, ``"click"`` or ``"mouse"`` when the user took over now, else ``None``."""
        t = self.now()
        own = self._own(t)
        seconds = self.age()
        last = None if seconds is None or seconds == float("inf") else t - float(seconds)
        came = last is not None and (self._last_input is None or last > self._last_input + 0.005)
        first = self.polls == 0
        if not self.armed and not own and (last is None or t - last >= self.quiet_s or self._was_own(last, t)):
            self.armed = True   # still since the watch began (or the last input was Yuki's): from here, input counts
        found: str | None = None
        if first or came or own:
            states = self.keys()
            if states is not None:
                down, tapped = states
                if own:
                    # consumed, never counted, and never the baseline: a key Yuki holds now is let go by Yuki
                    if (down - self._down) | (tapped - down):
                        self._skip("own")
                else:
                    fresh = (down - self._down) | (tapped - down)
                    self._down = down
                    if fresh and not first:
                        found = "click" if fresh & _MOUSE_VKS else "keys"
        if last is not None:
            self._last_input = last if self._last_input is None else max(self._last_input, last)
        where = self.cursor()
        if own:
            self._samples.clear()   # the cursor's places before Yuki moved it are no measure of the user's hand
            self._far = None
        elif where is not None:
            if self._far is not None:
                if math.dist(where, self._far) >= self.move_px:
                    found = found or "mouse"
                else:
                    self._samples.clear()   # it came straight back: that far place was a glitch, measured from here on
                self._far = None
            if found is None:
                self._samples = [s for s in self._samples if t - s[0] <= self.window_s]
                travel = max((math.dist(where, (x, y)) for _t, x, y in self._samples), default=0.0)
                if travel >= self.move_px:
                    far = max(self._samples, key=lambda s: math.dist(where, (s[1], s[2])))
                    self._far = (far[1], far[2])
                else:
                    self.max_travel = max(self.max_travel, travel)
            self._samples.append((t, int(where[0]), int(where[1])))
        self._prev_t = t
        self.polls += 1
        if found is not None and not self.armed:
            # going on since before the watch began (typing elsewhere, a game's keys): the baseline, not a takeover
            self._skip("busy")
            self._far = None
            return None
        if found is not None and self.ours(found, where):
            self._skip("yuki")
            self._samples.clear()
            self._far = None
            return None
        return found


class TakeoverThread:
    """Runs a :class:`TakeoverWatch` on its own thread while a request acts; calls ``on_takeover(kind)`` the first time
    the user takes over, then ends (unless ``on_takeover`` returns ``False``: not taken now, e.g. Yuki is asking
    something and the hand is on its card; then it watches on). :meth:`stop` ends it at once (the agent stops it when
    the request ends or is paused). ``grace_s``: how long it waits before its first look (after a resume, the hand that
    pressed Resume is still moving away)."""

    def __init__(self, on_takeover: Callable[[str], None], *, move_px: int | None = None,
                 poll_s: float = TAKEOVER_POLL_S, grace_s: float = 0.0,
                 watch: Callable[[], TakeoverWatch] | None = None) -> None:
        self.on_takeover = on_takeover
        self.move_px = takeover_move_px(move_px)
        self.poll_s, self.grace_s = float(poll_s), float(grace_s)
        self._make = watch or (lambda: TakeoverWatch(move_px=self.move_px))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watch: TakeoverWatch | None = None
        self.fired: str | None = None
        self.refused = 0

    def start(self) -> "TakeoverThread":
        self._thread = threading.Thread(target=self._run, name="yuki-takeover-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        if self.grace_s > 0 and self._stop.wait(self.grace_s):
            return
        try:
            self.watch = watch = self._make()   # made here: its first look (the baseline) is after the grace
        except Exception:  # noqa: BLE001
            return
        while not self._stop.is_set():
            try:
                kind = watch.poll()
            except Exception:  # noqa: BLE001 - a broken read never pauses anything
                kind = None
            if kind is not None and not self._stop.is_set():
                try:
                    taken = self.on_takeover(kind)
                except Exception:  # noqa: BLE001
                    taken = None
                if taken is not False:
                    self.fired = kind
                    return
                self.refused += 1
            self._stop.wait(self.poll_s)


__all__ = ["INPUT_LEASE_S", "InputGate", "LeaseBusy", "TAKEOVER_MOVE_PX", "TakeoverThread", "TakeoverWatch",
           "WINDOW_LEASE_S", "input_held_elsewhere", "input_lease", "key_states", "last_input_age_s", "leased",
           "note_own_input", "own_input_at", "own_input_now", "own_input_span", "takeover_move_px", "user_input_age_s",
           "window_lease", "yuki_has_it"]
