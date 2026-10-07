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
  the worker asks UI Automation what is under the pointer before the button is up or a menu closes, and the top-level
  window under the press (capC1c: ``WindowFromPhysicalPoint`` + ``GetAncestor``, no UI Automation), so the worker can
  tell whether what is under the press changed before its lookup answered. Button-up makes a click, a double click
  (Windows' double-click time and rectangle) or a drag with its end point: a drag only when the cursor at the release
  is outside Windows' drag rectangle (``SM_CXDRAG`` x ``SM_CYDRAG``, scaled for the monitor's DPI as Windows does)
  around the cursor at the press (capC1c: cursor pixels, never raw device counts; a resting sensor's 0-1 px jitter
  never makes one). Unlike ``DragDetect``, a cursor that left the rectangle and came back before the release makes a
  click; how far it went leaves with the release (``came_back``). The wheel leaves as notches with its point.
* **moves** are never stored and never leave as a path. Each report adds its raw counts to the net movement since
  the pointer's current *rest anchor*; the pointer (in screen pixels, as Windows judges a hover) leaving Windows' own
  hover rectangle (``SPI_GETMOUSEHOVERWIDTH`` x ``SPI_GETMOUSEHOVERHEIGHT``, read from the system) around the anchor
  starts a new rest: the net raw movement of the leg just finished is added to the travel since the last action, and
  the worker is told once that the pointer is moving, so it can see a rest of ``SPI_GETMOUSEHOVERTIME`` and look up
  the element under it. A vibrating sensor's reports (about 23 a second of 0-1 px, measured on this PC) stay inside
  the rectangle: they cost a few additions and one cursor read each, add no travel and wake nothing. A pointer that
  left the rectangle much further than the device's own counts could move it (:data:`JUMP_GAIN`) was placed by
  software (Yuki's ``SetCursorPos``): the rest starts again there silently and never becomes a hover.
* **source** of each action: Yuki's stamp in the extra-info field (:func:`yuki.core.inputtag.is_yuki_input`) is
  ``yuki``; injected input without it (``hDevice`` 0: Handy dictation, Deskflow, G Hub macros) is
  ``user_injected``; real hardware is ``user``.

Queue items (tuples, first field the kind): ``("down", t, button, x, y, source, travel, dbl, root, root_pid)``,
``("up", t, button, x, y, source, drag[, came_back])``, ``("wheel", t, notches, horizontal, x, y, source, travel)``,
``("key", t, kind, text, source, travel)``, ``("char", t, n, source)``, ``("moving", t)``.
"""

from __future__ import annotations

import ctypes
import math
import time
from collections.abc import Callable
from typing import Any

from yuki.core.inputtag import is_yuki_input
from yuki.memory.trace_keys import CHAR, CHORD, FOLD_S, NAMED_KEY, RUN_KEYS, KeyClassifier

USER, INJECTED, YUKI = "user", "user_injected", "yuki"

# RAWMOUSE.usButtonFlags.
_DOWN = {0x0001: "left", 0x0004: "right", 0x0010: "middle", 0x0040: "x1", 0x0100: "x2"}
_UP = {0x0002: "left", 0x0008: "right", 0x0020: "middle", 0x0080: "x1", 0x0200: "x2"}
_WHEEL, _HWHEEL = 0x0400, 0x0800
_MOVE_ABSOLUTE, _VIRTUAL_DESKTOP = 0x01, 0x02
_WHEEL_DELTA = 120
#: Pixels a raw count can move the pointer at most, well above Windows' own pointer gain (the speed slider's 3.5x
#: times Enhance pointer precision's curve): a pointer that moved further than this times the device's net counts
#: since the rest anchor was placed by software (second verify V4: Yuki's ``SetCursorPos`` read by the next jitter
#: report must not start a rest the user seems to make).
JUMP_GAIN = 32


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
                 double_w: int = 0, double_h: int = 0, drag_w: int = 0, drag_h: int = 0, read: bool = True,
                 dpi_at: Callable[[int, int], int] | None = None,
                 for_dpi: Callable[[int, int], int] | None = None) -> None:
        self.hover_w, self.hover_h, self.hover_s = hover_w, hover_h, hover_s
        self.double_s, self.double_w, self.double_h = double_s, double_w, double_h
        self.drag_w, self.drag_h = drag_w, drag_h
        #: capC1c: ``(x, y) -> dpi`` of the monitor under a physical point, and ``(metric, dpi) -> value``
        #: (``GetSystemMetricsForDpi``): the drag rectangle as Windows scales it for that monitor. Without them the
        #: 96-DPI values above are scaled by ``dpi_at``'s answer, or used as they are.
        self._dpi_at = dpi_at
        self._for_dpi = for_dpi
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
        try:
            self._dpi_at, self._for_dpi = _monitor_dpi(), _metric_for_dpi()
        except (AttributeError, OSError):      # before Windows 10 1607: the system's own values stand
            self._dpi_at = self._for_dpi = None

    def drag_size(self, x: int, y: int) -> tuple[int, int]:
        """capC1c: Windows' drag rectangle (``SM_CXDRAG`` x ``SM_CYDRAG``) for the monitor under a physical point,
        scaled for its DPI as Windows does (``GetSystemMetricsForDpi``). The pointer is read in physical pixels
        (``GetPhysicalCursorPos``), so on a monitor scaled to 150% the rectangle is 150% of its 96-DPI size."""
        w, h = self.drag_w, self.drag_h
        if self._dpi_at is None:
            return w, h
        try:
            dpi = int(self._dpi_at(int(x), int(y)) or 96)
            if self._for_dpi is not None:
                return (int(self._for_dpi(self._SM_CXDRAG, dpi)) or w, int(self._for_dpi(self._SM_CYDRAG, dpi)) or h)
            return max(1, (w * dpi + 48) // 96), max(1, (h * dpi + 48) // 96)
        except Exception:  # noqa: BLE001 - the system's own values stand
            return w, h

    def is_drag(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """capC1c: whether a press at ``(x0, y0)`` released at ``(x1, y1)`` (both the cursor in physical pixels, never
        the device's raw counts) is a drag: the release is outside Windows' drag rectangle centred on the press (the
        rectangle of the press's monitor). Only the release counts, not where the cursor went while held (that is
        ``DragDetect``'s rule, deliberately not followed: see :class:`TraceInput`). A sensor's 0-1 px jitter never
        leaves it."""
        w, h = self.drag_size(x0, y0)
        return self.outside(x0, y0, x1, y1, w, h)

    @staticmethod
    def outside(x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> bool:
        """Whether ``(x1, y1)`` is outside a ``w`` x ``h`` rectangle centred on ``(x0, y0)``."""
        return abs(x1 - x0) * 2 > max(w, 1) or abs(y1 - y0) * 2 > max(h, 1)

    def facts(self) -> dict[str, Any]:
        return {"hover_w": self.hover_w, "hover_h": self.hover_h, "hover_ms": round(self.hover_s * 1000),
                "double_ms": round(self.double_s * 1000), "double_w": self.double_w, "double_h": self.double_h,
                "drag_w": self.drag_w, "drag_h": self.drag_h}


def _monitor_dpi() -> Callable[[int, int], int]:
    """``(x, y) -> dpi`` of the monitor under a physical point (``MonitorFromPoint`` + ``GetDpiForMonitor``, effective
    DPI). The memory service is per-monitor DPI aware (``yuki.__init__``), so the point and the answer are physical."""
    from ctypes import wintypes as W

    u = ctypes.WinDLL("user32", use_last_error=True)
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
    u.MonitorFromPoint.argtypes = [W.POINT, W.DWORD]
    u.MonitorFromPoint.restype = W.HMONITOR
    shcore.GetDpiForMonitor.argtypes = [W.HMONITOR, ctypes.c_int, ctypes.POINTER(W.UINT), ctypes.POINTER(W.UINT)]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
    nearest = 2                                 # MONITOR_DEFAULTTONEAREST

    def dpi(x: int, y: int) -> int:
        mon = u.MonitorFromPoint(W.POINT(int(x), int(y)), nearest)
        dx, dy = W.UINT(0), W.UINT(0)
        if not mon or shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) != 0:
            return 96
        return int(dx.value) or 96

    return dpi


def _metric_for_dpi() -> Callable[[int, int], int]:
    u = ctypes.WinDLL("user32", use_last_error=True)
    fn = u.GetSystemMetricsForDpi                 # AttributeError before Windows 10 1607
    fn.argtypes = [ctypes.c_int, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return lambda metric, dpi: int(fn(int(metric), int(dpi)))


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
        on_action: called with the time when an action starts (button-down, chord, named key, wheel) and whether
            it is a repeat that folds into the previous action (the same run key or chord, or a wheel notch the same
            way, within :data:`yuki.memory.trace_keys.FOLD_S`): opens the outcome hooks around it, a fold without a
            fresh event cap (second verify V6). Must return at once.
        clock: ``time.monotonic``.
    """

    def __init__(self, put: Callable[[tuple], None], *, metrics: Metrics | None = None,
                 cursor: Callable[[], tuple[int, int]] | None = None, classifier: KeyClassifier | None = None,
                 on_action: Callable[[float, bool], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 screen: Callable[[bool], tuple[int, int]] | None = None,
                 root_at: Callable[[int, int], int] | None = None,
                 pid_of: Callable[[int], int] | None = None) -> None:
        self._put = put
        #: capC1c: ``(x, y) -> hwnd``, the top-level window under a physical point (``WindowFromPhysicalPoint`` +
        #: ``GetAncestor(GA_ROOT)``, a few microseconds, no UI Automation): taken at button-down so the worker can
        #: tell whether what is under the press changed before its lookup answered. None: not taken (0).
        self._root_at = root_at
        #: capC1c review: ``(hwnd) -> pid`` (``GetWindowThreadProcessId``, microseconds, sends no message): that
        #: window's process, taken with it, so a press on a menu or dialog that is gone before the worker reaches it
        #: still belongs to its app (a stale, point-only row) instead of disappearing. None: not taken (0).
        self._pid_of = pid_of
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
        self._anchored = False
        self._sx = self._sy = 0              # net raw counts since the anchor
        self._abs_last: tuple[float, float] | None = None
        self.anchor_at = 0.0                 # when the current rest began (monotonic)
        self.anchor_seq = 0                  # bumped on every new rest
        self.jump_seq = -1                   # the anchor_seq of a rest begun by a software jump (no hover)
        #: The worker was told the pointer moves and has not yet judged the rest; cleared by the worker.
        self.moving = False
        self.travel = 0.0                    # raw counts travelled since the last action
        self.move_source = USER              # who moved the pointer last (user | user_injected)
        # buttons
        #: button -> (t, x, y) of the press (cursor px), Windows' drag rectangle there (w, h) and whether the cursor
        #: has left it while the button was held (``DragDetect``'s rule)
        self._down: dict[str, tuple[float, int, int, int, int, bool]] = {}
        self._last_click: tuple[str, float, int, int] | None = None
        self._fold: tuple[Any, float] | None = None    # the last action's fold key and time (V6)
        #: Content-free counters (read by the trace's stats).
        self.counts: dict[str, int] = {"moves": 0, "buttons": 0, "wheel": 0, "keys": 0, "dropped": 0, "rests": 0,
                                       "jumps": 0}

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

    def _action(self, t: float, key: Any = None) -> None:
        last = self._fold
        fold = key is not None and last is not None and last[0] == key and t - last[1] <= FOLD_S
        self._fold = (key, t)
        if self._on_action is not None:
            try:
                self._on_action(t, fold)
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

    def _jump(self, t: float, x: int, y: int) -> None:
        """The pointer is far from the rest anchor but this device barely moved (second verify V4): something placed
        it (Yuki's ``SetCursorPos``, its stamped moves, an app warping the pointer). The rest starts again where it
        is, silently: no travel, no *moving*, and a rest begun by a jump never becomes a hover (:attr:`jump_seq`)."""
        self._sx = self._sy = 0
        self._ax, self._ay = x, y
        self.anchor_at = t
        self.anchor_seq += 1
        self.jump_seq = self.anchor_seq
        self.counts["jumps"] += 1

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
            if not self._anchored:                     # the first report since listening: the rest starts here
                self._ax, self._ay, self.anchor_at, self._anchored = x, y, t, True
                self._sx = self._sy = 0
            elif abs(x - self._ax) * 2 > m.hover_w or abs(y - self._ay) * 2 > m.hover_h:
                moved = abs(x - self._ax) + abs(y - self._ay)
                if moved > JUMP_GAIN * (abs(self._sx) + abs(self._sy)) + m.hover_w + m.hover_h:
                    self._jump(t, x, y)                # placed by software, not this device's move
                else:
                    self._new_anchor(t, x, y)
            if self._down:
                # capC1c review: how far the cursor went from the press while the button is held, per axis (a few
                # compares per report). Never decides the kind; a release back inside the rectangle after leaving it
                # is told to the worker as that distance, so a live hour can see such clicks
                for held, (t0, x0, y0, w, h, fx, fy) in list(self._down.items()):
                    ex, ey = abs(x - x0), abs(y - y0)
                    if ex > fx or ey > fy:
                        self._down[held] = (t0, x0, y0, w, h, max(fx, ex), max(fy, ey))
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
                w, h = m.drag_size(x, y)                # the monitor of the press, read once per press
                self._down[button] = (t, x, y, max(w, 1), max(h, 1), 0, 0)
                # a press ends the rest: the pointer must move away before it can hover again
                self._ax, self._ay, self.anchor_at = x, y, t
                self.anchor_seq += 1
                self._action(t)
                root = root_pid = 0
                if self._root_at is not None:
                    try:
                        root = int(self._root_at(x, y) or 0)
                    except Exception:  # noqa: BLE001 - the worker then judges staleness without it
                        root = 0
                if root and self._pid_of is not None:
                    try:
                        root_pid = int(self._pid_of(root) or 0)
                    except Exception:  # noqa: BLE001 - the worker reads it later, if the window is still there
                        root_pid = 0
                self._put(("down", t, button, x, y, source, self._take_travel(), dbl, root, root_pid))
        for flag, button in _UP.items():
            if button_flags & flag:
                x, y = self._cursor()
                held = self._down.pop(button, None)
                # capC1c: the cursor at the release against the cursor at the press (never the device's raw counts,
                # which pointer speed and acceleration rescale), in Windows' drag rectangle for that monitor's DPI.
                # This is not DragDetect's rule (which also fires when the cursor left the rectangle and came back):
                # the first live hour had plain clicks with a few pixels of wobble recorded as drags. The trade-off:
                # a drag released where it began (a knob turned and let go at its start) is a click; ``came_back``
                # (px, the larger axis, 0 when the cursor never left the rectangle) tells the worker of it
                came_back = 0
                if held is None:
                    drag = False
                else:
                    _t0, x0, y0, w, h, fx, fy = held
                    drag = self.metrics.outside(x0, y0, x, y, w, h)
                    if not drag and (fx * 2 > w or fy * 2 > h):
                        came_back = int(max(fx, fy))
                self._last_click = None if drag else (button, t, x, y)
                # ``came_back`` only when there is one: the item keeps its 7 fields otherwise
                self._put(("up", t, button, x, y, source, drag) + ((came_back,) if came_back else ()))
        if button_flags & (_WHEEL | _HWHEEL):
            self.counts["wheel"] += 1
            delta = ctypes.c_short(button_data & 0xFFFF).value
            notches = int(delta / _WHEEL_DELTA) or (1 if delta > 0 else -1)
            x, y = self._cursor()
            self._action(t, ("wheel", bool(button_flags & _HWHEEL), delta > 0))
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
            # a chord or run key repeated folds into one row: the same outcome window (V6); Enter, Tab... do not
            self._action(t, ("key", cls.key) if kind == CHORD or cls.key in RUN_KEYS else None)
            self._put(("key", t, kind, cls.key, source_of(extra, device), self._take_travel()))

    def reset(self) -> None:
        """Forget held buttons and the rest (the trace was blocked and opens again)."""
        self._down.clear()
        self._last_click = None
        self._fold = None
        self._sx = self._sy = 0
        self.travel = 0.0
        self._abs_last = None
        self._anchored = False
        self.moving = False


_SCREEN: Callable[[bool], tuple[int, int]] | None = None


def _default_screen() -> Callable[[bool], tuple[int, int]]:
    global _SCREEN
    if _SCREEN is None:
        _SCREEN = _screen_size()
    return _SCREEN


__all__ = ["INJECTED", "Metrics", "TraceInput", "USER", "YUKI", "source_of"]
