"""The trace's side of the one Raw Input reader (report 78 §4.2; capC1 additions of 7 Oct 2026).

Raw Input registration is one per process, per usage page and usage: a second ``RegisterRawInputDevices`` in the
memory service would take presence's events (or presence would take these). So there is one reader,
:class:`yuki.memory.userpresence._RawReader`, and this is the *sink* it also feeds: :class:`TraceInput` gets the
fields it needs from each ``WM_INPUT`` as plain integers (never the ``RAWINPUT`` buffer), classifies at once, and puts
only small content-free tuples on the trace worker's queue. Presence's counts and idle signal are untouched.

On the reader's thread, per event, so it stays cheap:

* **first step: the drop.** While the hard game gate holds, the user paused memory or the trace is off,
  :attr:`TraceInput.blocked` is set and every event returns at the first line (nothing classified, nothing queued).
  Presence keeps its own registration and behaviour: unregistering Raw Input during a game would make presence deaf
  and read the user as away (report 90).
* **keys** become a class (:mod:`yuki.memory.trace_keys`): a named key or chord leaves as its text, a character as a
  count of one, and the virtual key is dropped with the class.
* **mouse buttons**: at button-down the pointer's screen point (``GetPhysicalCursorPos``) and a *lookup now* item, so
  the worker asks UI Automation what is under the pointer before the button is up or a menu closes. Button-up makes a
  click, a double click (Windows' double-click time and rectangle) or a drag (Windows' drag rectangle) with its end
  point. The wheel leaves as notches with its point.
* **moves** are never stored and never leave as a path. Each report adds its raw counts to the net movement since
  the pointer's current *rest anchor*; the pointer (in screen pixels, as Windows judges a hover) leaving Windows' own
  hover rectangle (``SPI_GETMOUSEHOVERWIDTH`` x ``SPI_GETMOUSEHOVERHEIGHT``, read from the system) around the anchor
  starts a new rest: the net raw movement of the leg just finished is added to the travel since the last action, and
  the worker is told once that the pointer is moving, so it can see a rest of ``SPI_GETMOUSEHOVERTIME`` and look up
  the element under it. A vibrating sensor's reports (about 23 a second of 0-1 px, measured on this PC) stay inside
  the rectangle: they cost a few additions and one cursor read each, add no travel and wake nothing.
* **source** of each action: Yuki's stamp in the extra-info field (:func:`yuki.core.inputtag.is_yuki_input`) is
  ``yuki``; injected input without it (``hDevice`` 0: Handy dictation, Deskflow, G Hub macros) is
  ``user_injected``; real hardware is ``user``.

Queue items (tuples, first field the kind): ``("down", t, button, x, y, source, travel, dbl)``,
``("up", t, button, x, y, source, drag)``, ``("wheel", t, notches, horizontal, x, y, source, travel)``,
``("key", t, kind, text, source, travel)``, ``("char", t, n, source)``, ``("moving", t)``.
"""

from __future__ import annotations

import ctypes
import math
import time
from collections.abc import Callable
from typing import Any

from yuki.core.inputtag import is_yuki_input
from yuki.memory.trace_keys import CHAR, CHORD, NAMED_KEY, KeyClassifier

USER, INJECTED, YUKI = "user", "user_injected", "yuki"

# RAWMOUSE.usButtonFlags.
_DOWN = {0x0001: "left", 0x0004: "right", 0x0010: "middle", 0x0040: "x1", 0x0100: "x2"}
_UP = {0x0002: "left", 0x0008: "right", 0x0020: "middle", 0x0080: "x1", 0x0200: "x2"}
_WHEEL, _HWHEEL = 0x0400, 0x0800
_MOVE_ABSOLUTE, _VIRTUAL_DESKTOP = 0x01, 0x02
_WHEEL_DELTA = 120


def source_of(extra: int | None, device: int | None) -> str:
    """``yuki`` (Yuki's stamp), ``user_injected`` (injected, no stamp: ``hDevice`` is 0) or ``user`` (hardware)."""
    if is_yuki_input(extra):
        return YUKI
    return USER if device else INJECTED


class Metrics:
    """Windows' own pointer settings, read from the system (never invented constants): the hover rectangle and time,
    the double-click time and rectangle, the drag rectangle. Read once at start; :meth:`refresh` reads them again."""

    _SPI_GETMOUSEHOVERWIDTH, _SPI_GETMOUSEHOVERHEIGHT, _SPI_GETMOUSEHOVERTIME = 0x0062, 0x0064, 0x0066
    _SM_CXDOUBLECLK, _SM_CYDOUBLECLK, _SM_CXDRAG, _SM_CYDRAG = 36, 37, 68, 69

    def __init__(self, *, hover_w: int = 0, hover_h: int = 0, hover_s: float = 0.0, double_s: float = 0.0,
                 double_w: int = 0, double_h: int = 0, drag_w: int = 0, drag_h: int = 0, read: bool = True) -> None:
        self.hover_w, self.hover_h, self.hover_s = hover_w, hover_h, hover_s
        self.double_s, self.double_w, self.double_h = double_s, double_w, double_h
        self.drag_w, self.drag_h = drag_w, drag_h
        if read:
            self.refresh()

    def refresh(self) -> None:
        from ctypes import wintypes as W

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SystemParametersInfoW.argtypes = [W.UINT, W.UINT, ctypes.c_void_p, W.UINT]
        u.SystemParametersInfoW.restype = W.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetDoubleClickTime.restype = W.UINT

        def spi(action: int) -> int:
            value = W.UINT(0)
            if not u.SystemParametersInfoW(action, 0, ctypes.byref(value), 0):
                raise OSError(ctypes.get_last_error(), "SystemParametersInfoW failed")
            return int(value.value)

        self.hover_w = spi(self._SPI_GETMOUSEHOVERWIDTH)
        self.hover_h = spi(self._SPI_GETMOUSEHOVERHEIGHT)
        self.hover_s = spi(self._SPI_GETMOUSEHOVERTIME) / 1000.0
        self.double_s = int(u.GetDoubleClickTime()) / 1000.0
        self.double_w = int(u.GetSystemMetrics(self._SM_CXDOUBLECLK))
        self.double_h = int(u.GetSystemMetrics(self._SM_CYDOUBLECLK))
        self.drag_w = int(u.GetSystemMetrics(self._SM_CXDRAG))
        self.drag_h = int(u.GetSystemMetrics(self._SM_CYDRAG))

    def facts(self) -> dict[str, Any]:
        return {"hover_w": self.hover_w, "hover_h": self.hover_h, "hover_ms": round(self.hover_s * 1000),
                "double_ms": round(self.double_s * 1000), "double_w": self.double_w, "double_h": self.double_h,
                "drag_w": self.drag_w, "drag_h": self.drag_h}


def _physical_cursor() -> Callable[[], tuple[int, int]]:
    from ctypes import wintypes as W

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
    u.GetPhysicalCursorPos.restype = W.BOOL
    pt = W.POINT()
    ref = ctypes.byref(pt)

    def read() -> tuple[int, int]:
        if u.GetPhysicalCursorPos(ref):
            return int(pt.x), int(pt.y)
        return 0, 0

    return read


def _screen_size() -> Callable[[bool], tuple[int, int]]:
    u = ctypes.WinDLL("user32", use_last_error=True)

    def size(virtual: bool) -> tuple[int, int]:
        return (int(u.GetSystemMetrics(78 if virtual else 0)) or 1, int(u.GetSystemMetrics(79 if virtual else 1)) or 1)

    return size


class TraceInput:
    """The sink the Raw Input reader feeds (see the module docstring). Thread: the reader's, except the attributes
    the worker reads (:attr:`anchor_at`, :attr:`anchor_seq`, :attr:`moving`) and sets (:attr:`blocked`,
    :attr:`moving`).

    Args:
        put: puts one item on the worker's queue (``queue.SimpleQueue.put``).
        metrics: Windows' pointer settings (:class:`Metrics`).
        cursor: ``() -> (x, y)``, the pointer in physical screen pixels (default ``GetPhysicalCursorPos``).
        classifier: the key classifier (default :class:`yuki.memory.trace_keys.KeyClassifier`).
        on_action: called with the time when an action starts (button-down, chord, named key, wheel): opens the
            outcome hooks around it. Must return at once.
        clock: ``time.monotonic``.
    """

    def __init__(self, put: Callable[[tuple], None], *, metrics: Metrics | None = None,
                 cursor: Callable[[], tuple[int, int]] | None = None, classifier: KeyClassifier | None = None,
                 on_action: Callable[[float], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 screen: Callable[[bool], tuple[int, int]] | None = None) -> None:
        self._put = put
        self.metrics = metrics if metrics is not None else Metrics()
        self._cursor = cursor if cursor is not None else _physical_cursor()
        self._screen = screen
        self.keys = classifier if classifier is not None else KeyClassifier()
        self._on_action = on_action
        self._clock = clock
        #: Set by the trace (game gate, pause, consent off): every event is dropped at the first line.
        self.blocked = True
        # pointer rest
        self._ax = self._ay = 0              # the anchor, in screen px
        self._sx = self._sy = 0              # net raw counts since the anchor
        self._abs_last: tuple[float, float] | None = None
        self.anchor_at = 0.0                 # when the current rest began (monotonic)
        self.anchor_seq = 0                  # bumped on every new rest
        #: The worker was told the pointer moves and has not yet judged the rest; cleared by the worker.
        self.moving = False
        self.travel = 0.0                    # raw counts travelled since the last action
        self.move_source = USER              # who moved the pointer last (user | user_injected)
        # buttons
        self._down: dict[str, tuple[float, int, int, int]] = {}   # button -> (t, x, y, max excursion px)
        self._last_click: tuple[str, float, int, int] | None = None
        #: Content-free counters (read by the trace's stats).
        self.counts: dict[str, int] = {"moves": 0, "buttons": 0, "wheel": 0, "keys": 0, "dropped": 0, "rests": 0}

    @property
    def wants_mouse(self) -> bool:
        """The reader keeps the mouse registered (no presence rest) while this is listening."""
        return not self.blocked

    # -- helpers -----------------------------------------------------------------------------------------------------

    def _take_travel(self) -> int:
        travel = self.travel + math.hypot(self._sx, self._sy)
        self.travel = 0.0
        self._sx = self._sy = 0
        return int(round(travel))

    def _action(self, t: float) -> None:
        if self._on_action is not None:
            try:
                self._on_action(t)
            except Exception:  # noqa: BLE001 - the hooks never break the reader
                pass

    def _new_anchor(self, t: float, x: int, y: int) -> None:
        self.travel += math.hypot(self._sx, self._sy)
        self._sx = self._sy = 0
        self._ax, self._ay = x, y
        self.anchor_at = t
        self.anchor_seq += 1
        self.counts["rests"] += 1
        if not self.moving:
            self.moving = True
            self._put(("moving", t))

    # -- the reader's calls ------------------------------------------------------------------------------------------

    def mouse(self, t: float, us_flags: int, button_flags: int, button_data: int, dx: int, dy: int, extra: int,
              device: int) -> None:
        """One Raw Input mouse report, as integers."""
        if self.blocked:
            return
        if (dx or dy) and not is_yuki_input(extra):     # Yuki's own stamped moves never make a rest or travel
            self.counts["moves"] += 1
            self.move_source = USER if device else INJECTED
            if us_flags & _MOVE_ABSOLUTE:              # pen, remote desktop: 0..65535 over a screen
                w, h = (self._screen or _default_screen())(bool(us_flags & _VIRTUAL_DESKTOP))
                px, py = dx * w / 65535.0, dy * h / 65535.0
                last, self._abs_last = self._abs_last, (px, py)
                if last is not None:
                    self._sx += int(round(px - last[0]))
                    self._sy += int(round(py - last[1]))
            else:
                self._sx += dx
                self._sy += dy
            x, y = self._cursor()
            m = self.metrics
            if abs(x - self._ax) * 2 > m.hover_w or abs(y - self._ay) * 2 > m.hover_h:
                self._new_anchor(t, x, y)
            for button, (t0, x0, y0, far) in list(self._down.items()):
                excursion = max(abs(x - x0), abs(y - y0))
                if excursion > far:
                    self._down[button] = (t0, x0, y0, excursion)
        if not button_flags:
            return
        source = source_of(extra, device)
        for flag, button in _DOWN.items():
            if button_flags & flag:
                self.counts["buttons"] += 1
                x, y = self._cursor()
                last = self._last_click
                m = self.metrics
                dbl = bool(last and last[0] == button and t - last[1] <= m.double_s
                           and abs(x - last[2]) * 2 <= m.double_w and abs(y - last[3]) * 2 <= m.double_h)
                self._down[button] = (t, x, y, 0)
                # a press ends the rest: the pointer must move away before it can hover again
                self._ax, self._ay, self.anchor_at = x, y, t
                self.anchor_seq += 1
                self._action(t)
                self._put(("down", t, button, x, y, source, self._take_travel(), dbl))
        for flag, button in _UP.items():
            if button_flags & flag:
                x, y = self._cursor()
                t0, x0, y0, far = self._down.pop(button, (t, x, y, 0))
                m = self.metrics
                far = max(far, abs(x - x0), abs(y - y0))
                drag = far * 2 > max(m.drag_w, m.drag_h, 1)
                self._last_click = None if drag else (button, t, x, y)
                self._put(("up", t, button, x, y, source, drag))
        if button_flags & (_WHEEL | _HWHEEL):
            self.counts["wheel"] += 1
            delta = ctypes.c_short(button_data & 0xFFFF).value
            notches = int(delta / _WHEEL_DELTA) or (1 if delta > 0 else -1)
            x, y = self._cursor()
            self._action(t)
            self._put(("wheel", t, notches, bool(button_flags & _HWHEEL), x, y, source, self._take_travel()))

    def key(self, t: float, vk: int, scan: int, flags: int, message: int, extra: int, device: int) -> None:
        """One Raw Input keyboard report, as integers: classified at once; only the class leaves."""
        if self.blocked:
            return
        cls = self.keys.classify(vk, scan, flags, message)
        kind = cls.kind
        if kind == CHAR:
            self.counts["keys"] += 1
            self._put(("char", t, cls.chars, source_of(extra, device)))
        elif kind in (CHORD, NAMED_KEY):
            self.counts["keys"] += 1
            self._action(t)
            self._put(("key", t, kind, cls.key, source_of(extra, device), self._take_travel()))

    def reset(self) -> None:
        """Forget held buttons and the rest (the trace was blocked and opens again)."""
        self._down.clear()
        self._last_click = None
        self._sx = self._sy = 0
        self.travel = 0.0
        self._abs_last = None
        self.moving = False


_SCREEN: Callable[[bool], tuple[int, int]] | None = None


def _default_screen() -> Callable[[bool], tuple[int, int]]:
    global _SCREEN
    if _SCREEN is None:
        _SCREEN = _screen_size()
    return _SCREEN


__all__ = ["INJECTED", "Metrics", "TraceInput", "USER", "YUKI", "source_of"]
