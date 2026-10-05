"""Whether the user is really using the PC: their key presses, mouse button presses, wheel turns and big mouse moves.

hotInput (5 Oct, report 73 release 6): the user's mouse sensor vibrates by itself and sends 20-30 tiny moves a second
(measured read-only 5 Oct ~16:00 over 60 s: 11-37 moves a second, median 23; 75% of them 0 px, the rest 1 px; the
farthest the cursor got within 0.3 s was 5 px; 0 keys, 0 clicks, 0 wheel). ``GetLastInputInfo`` counts every one of
those moves, so it never got older than ~0.3 s, and every guess built on it went wrong: the input gate held every
keystroke, scroll and raw click for its full 12 s, and the takeover watch never armed. ``GetLastInputInfo`` cannot tell
a key from a twitch; this module can.

:class:`UserInputMonitor` reads the input stream through Raw Input (``RegisterRawInputDevices`` with
``RIDEV_INPUTSINK`` on a message-only window on its own thread): Windows *posts* each event to that window after the
fact, so unlike a ``WH_MOUSE_LL`` hook it can never hold up or lag the mouse, even while Python is busy. It keeps,
counts only (never which key, never where the cursor was):

* when a key last went down (``keys``), a mouse button (``click``), the wheel (``wheel``);
* when the cursor last travelled at least the user's takeover distance (``ui.json`` ``takeover_move_px``, 40 px by
  default) within :data:`MOVE_WINDOW_S` (``mouse``). A twitching sensor never gets that far; a hand does at once.

Yuki's own input is left out: every event Yuki injects carries :data:`yuki.core.inputtag.YUKI_INPUT_TAG` in its extra
info, which Raw Input hands back. Injected input without the tag (Handy's dictation, Deskflow, macro tools) is the
user's. Yuki also moves the cursor with ``SetCursorPos``, which sends no event: the travel measure starts again during
and right after one of Yuki's own input moments (:func:`yuki.actions.leases.own_input_now`).

It runs only while something needs it (:func:`acquire` / :func:`release`, counted): the agent holds it while a request
runs on the real desktop, so the input gate and the takeover watch read the same facts. Nothing runs while Yuki is idle.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from collections import deque
from typing import Any, Callable

from yuki.core.inputtag import is_yuki_input

#: The kinds of real input, and what each is.
KEYS, CLICK, WHEEL, MOUSE = "keys", "click", "wheel", "mouse"
KINDS = (KEYS, CLICK, WHEEL, MOUSE)
#: The window the cursor's travel is measured over (s): the takeover watch's own (``leases.TAKEOVER_WINDOW_S``).
MOVE_WINDOW_S = 0.3
#: How long :func:`acquire` waits for the reader to be listening (s).
START_WAIT_S = 1.0

# Raw Input numbers (Windows' fixed values).
_WM_INPUT = 0x00FF
_WM_QUIT = 0x0012
_RID_INPUT = 0x10000003
_RIM_TYPEMOUSE, _RIM_TYPEKEYBOARD = 0, 1
_RIDEV_INPUTSINK, _RIDEV_REMOVE = 0x00000100, 0x00000001
_HWND_MESSAGE = -3
_WM_KEYDOWN, _WM_SYSKEYDOWN = 0x0100, 0x0104
#: RI_MOUSE_{LEFT,RIGHT,MIDDLE}_BUTTON_DOWN, RI_MOUSE_BUTTON_4_DOWN, RI_MOUSE_BUTTON_5_DOWN.
_BUTTON_DOWNS = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100
#: RI_MOUSE_WHEEL, RI_MOUSE_HWHEEL.
_WHEELS = 0x0400 | 0x0800


class UserInputMonitor:
    """The user's real input, as times per kind (``time.monotonic()``). Fed by :meth:`key_down`, :meth:`button_down`,
    :meth:`wheel` and :meth:`move` (from the Raw Input thread, or a test); read by :meth:`last` / :meth:`latest`.

    Args:
        move_px: The travel that counts as the user moving the mouse (``None``: the user's setting, read on start).
        window_s: The window that travel is measured over (s).
        own_now: Whether one of Yuki's own input moments runs or just ended (``leases.own_input_now``).
        own_span: Yuki's latest input span (``leases.own_input_span``): a new one starts the travel measure again.
        now: The clock.

    Attributes:
        running: Whether the reader is listening (Raw Input registered), or a test feeds it.
        started_at: When it started listening (monotonic; 0.0: not running). Facts older than that are unknown.
        counts: Events by kind, counts only (``keys``, ``click``, ``wheel``, ``moves``, ``mouse``: moves that went
            far, ``own``: Yuki's own, left out).
        max_travel: The farthest the cursor got within the window without counting (px): what the jitter looked like.
    """

    def __init__(self, *, move_px: int | None = None, window_s: float = MOVE_WINDOW_S,
                 own_now: Callable[[float], bool] | None = None,
                 own_span: Callable[[], tuple[float, float | None]] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.move_px = move_px
        self.window_s = float(window_s)
        self._own_now = own_now
        self._own_span = own_span
        self.now = now
        self.running = False
        self.started_at = 0.0
        self.error = ""
        self.counts: dict[str, int] = {}
        self.max_travel = 0.0
        self._last: dict[str, float] = {kind: 0.0 for kind in KINDS}
        self._trail: deque[tuple[float, int, int]] = deque()
        self._span_seen = 0.0
        self._users = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._gen = 0
        self._ready = threading.Event()
        #: Raw Input registration is one per process: the stretch whose reader holds it, and a lock so an old reader's
        #: removal never lands after a new reader registered (a release straight followed by an acquire).
        self._registered_gen = 0
        self._reg_lock = threading.Lock()

    # -- facts ---------------------------------------------------------------------------------------------------------

    def last(self, kind: str) -> float:
        """When the user's input of this kind last came (monotonic; 0.0: not since the monitor started)."""
        return self._last.get(kind, 0.0)

    def latest(self, kinds: tuple[str, ...] = KINDS) -> tuple[float, str | None]:
        """``(when, kind)`` of the user's latest real input among ``kinds`` (``(0.0, None)``: none seen)."""
        best, which = 0.0, None
        for kind in kinds:
            at = self._last.get(kind, 0.0)
            if at > best:
                best, which = at, kind
        return best, which

    def facts(self) -> dict[str, Any]:
        """Counts and numbers only, for a log line."""
        return {"running": self.running, "counts": dict(self.counts), "max_travel": round(self.max_travel, 1),
                "move_px": self._move_px()}

    # -- intake (the reader's thread) ----------------------------------------------------------------------------------

    def _count(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def _stamp(self, kind: str, at: float | None, extra: int | None) -> bool:
        if is_yuki_input(extra):
            self._count("own")
            return False
        self._last[kind] = self.now() if at is None else float(at)
        self._count(kind)
        return True

    def key_down(self, at: float | None = None, extra: int | None = 0) -> bool:
        """A key went down (autorepeat included: a held key is the user's hand on the keyboard)."""
        return self._stamp(KEYS, at, extra)

    def button_down(self, at: float | None = None, extra: int | None = 0) -> bool:
        """A mouse button went down."""
        return self._stamp(CLICK, at, extra)

    def wheel(self, at: float | None = None, extra: int | None = 0) -> bool:
        """The wheel turned (or tilted)."""
        return self._stamp(WHEEL, at, extra)

    def _move_px(self) -> int:
        if self.move_px is None:
            from yuki.actions.leases import takeover_move_px

            self.move_px = takeover_move_px()
        return int(self.move_px)

    def _own(self, t: float) -> bool:
        """Yuki's own input moment runs or just ended, or a new one began since the last move (its ``SetCursorPos``
        jumps send no event, so the cursor's places from before are no measure of the user's hand)."""
        from yuki.actions import leases

        span = (self._own_span or leases.own_input_span)()
        start = float(span[0] or 0.0)
        fresh = start > self._span_seen
        self._span_seen = max(self._span_seen, start)
        own_now = self._own_now or (lambda at: leases.own_input_now(at, span=lambda: span))
        return fresh or bool(own_now(t))

    def move(self, where: tuple[int, int] | None, at: float | None = None, extra: int | None = 0) -> bool:
        """The mouse moved and the cursor is now at ``where`` (screen px; ``None``: unknown). Returns whether the
        cursor has now travelled the takeover distance within the window (the user moving the mouse)."""
        t = self.now() if at is None else float(at)
        self._count("moves")
        if is_yuki_input(extra):
            self._count("own")
            self._trail.clear()
            return False
        if self._own(t):
            self._trail.clear()
            return False
        if where is None:
            return False
        x, y = int(where[0]), int(where[1])
        trail = self._trail
        while trail and t - trail[0][0] > self.window_s:
            trail.popleft()
        travel = max((math.hypot(x - px, y - py) for _t, px, py in trail), default=0.0)
        trail.append((t, x, y))
        if travel >= self._move_px():
            self._last[MOUSE] = t
            self._count(MOUSE)
            return True
        if travel > self.max_travel:
            self.max_travel = travel
        return False

    # -- the reader ----------------------------------------------------------------------------------------------------

    def acquire(self) -> bool:
        """One more user of the monitor; the first starts the reader. Returns whether it is listening."""
        with self._lock:
            self._users += 1
            if self._users == 1 and not self.running:
                self._start()
        if not self.running:
            self._ready.wait(START_WAIT_S)
        return self.running

    def release(self) -> None:
        """One user fewer; the last stops the reader."""
        with self._lock:
            if self._users <= 0:
                return
            self._users -= 1
            if self._users == 0:
                self._stop()

    def _start(self) -> None:
        self._gen += 1
        self._ready.clear()
        self.error = ""
        self.move_px = None         # the user's setting, read again for this stretch
        self._trail.clear()
        self.counts = {}            # counts per listening stretch (the facts' times are kept: they are only times)
        self.max_travel = 0.0
        self._thread = threading.Thread(target=self._run, args=(self._gen,), name="yuki-user-input", daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        self._gen += 1              # the reader of the stretch that ends sees it is no longer current and leaves
        thread_id, self._thread_id = self._thread_id, 0
        self.running = False
        self.started_at = 0.0
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def _current(self, gen: int) -> bool:
        return gen == self._gen and self._users > 0

    def _run(self, gen: int) -> None:
        try:
            _RawReader(self, gen).run()
        except Exception as exc:  # noqa: BLE001 - not on Windows, or no desktop: the callers fall back
            if gen == self._gen:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if gen == self._gen:
                self.running = False
                # only the current stretch's reader wakes acquire(): an old reader leaving after a new stretch began
                # would wake it before the new reader listens (a false "unavailable")
                self._ready.set()


class _RawReader:
    """The Raw Input window and its message loop (one per monitor start)."""

    def __init__(self, monitor: UserInputMonitor, gen: int) -> None:
        self.monitor, self.gen = monitor, gen

    def run(self) -> None:
        from ctypes import wintypes as W

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, ctypes.c_uint, W.WPARAM, W.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                        ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                        ("lpszClassName", W.LPCWSTR)]

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort), ("dwFlags", W.DWORD),
                        ("hwndTarget", W.HWND)]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD), ("hDevice", W.HANDLE), ("wParam", W.WPARAM)]

        class RAWMOUSE(ctypes.Structure):
            _fields_ = [("usFlags", ctypes.c_ushort), ("_pad", ctypes.c_ushort), ("usButtonFlags", ctypes.c_ushort),
                        ("usButtonData", ctypes.c_ushort), ("ulRawButtons", ctypes.c_ulong), ("lLastX", ctypes.c_long),
                        ("lLastY", ctypes.c_long), ("ulExtraInformation", ctypes.c_ulong)]

        class RAWKEYBOARD(ctypes.Structure):
            _fields_ = [("MakeCode", ctypes.c_ushort), ("Flags", ctypes.c_ushort), ("Reserved", ctypes.c_ushort),
                        ("VKey", ctypes.c_ushort), ("Message", ctypes.c_uint), ("ExtraInformation", ctypes.c_ulong)]

        class _Data(ctypes.Union):
            _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]

        class RAWINPUT(ctypes.Structure):
            _fields_ = [("header", RAWINPUTHEADER), ("data", _Data)]

        user32.DefWindowProcW.argtypes = [W.HWND, ctypes.c_uint, W.WPARAM, W.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.GetRawInputData.argtypes = [W.HANDLE, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                                           ctypes.c_uint]
        user32.GetRawInputData.restype = ctypes.c_uint
        user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        user32.RegisterRawInputDevices.restype = W.BOOL
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = W.ATOM
        user32.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
        user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, ctypes.c_void_p]
        user32.CreateWindowExW.restype = W.HWND
        user32.DestroyWindow.argtypes = [W.HWND]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
        user32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
        kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
        kernel32.GetModuleHandleW.restype = W.HMODULE

        monitor = self.monitor
        buf = RAWINPUT()
        size = ctypes.c_uint(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        point = W.POINT()

        def on_input(l_param: int) -> None:
            size.value = ctypes.sizeof(buf)
            got = user32.GetRawInputData(W.HANDLE(l_param), _RID_INPUT, ctypes.byref(buf), ctypes.byref(size),
                                         header_size)
            if got in (0, 0xFFFFFFFF):
                return
            t = time.monotonic()
            kind = buf.header.dwType
            if kind == _RIM_TYPEMOUSE:
                m = buf.data.mouse
                extra = int(m.ulExtraInformation)
                flags = int(m.usButtonFlags)
                if flags & _BUTTON_DOWNS:
                    monitor.button_down(t, extra)
                if flags & _WHEELS:
                    monitor.wheel(t, extra)
                if m.lLastX or m.lLastY or (m.usFlags & 0x01):     # a move (MOUSE_MOVE_ABSOLUTE included)
                    where = (point.x, point.y) if user32.GetCursorPos(ctypes.byref(point)) else None
                    monitor.move(where, t, extra)
            elif kind == _RIM_TYPEKEYBOARD:
                k = buf.data.keyboard
                if int(k.Message) in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    monitor.key_down(t, int(k.ExtraInformation))

        def wndproc(hwnd: int, msg: int, w_param: int, l_param: int) -> int:
            if msg == _WM_INPUT:
                try:
                    on_input(l_param)
                except Exception:  # noqa: BLE001 - a broken read never takes the reader down
                    pass
            return user32.DefWindowProcW(hwnd, msg, w_param, l_param)

        proc = WNDPROC(wndproc)    # held for the life of the window
        instance = kernel32.GetModuleHandleW(None)
        name = f"YukiUserInput{id(self):x}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = instance
        wc.lpszClassName = name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(f"RegisterClassW failed ({ctypes.get_last_error()})")
        hwnd = user32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, W.HWND(_HWND_MESSAGE), None, instance, None)
        if not hwnd:
            user32.UnregisterClassW(name, instance)
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, _RIDEV_INPUTSINK, hwnd),   # mouse
                                       RAWINPUTDEVICE(0x01, 0x06, _RIDEV_INPUTSINK, hwnd))   # keyboard
        try:
            with monitor._reg_lock:
                if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
                    raise OSError(f"RegisterRawInputDevices failed ({ctypes.get_last_error()})")
                monitor._registered_gen = self.gen
            with monitor._lock:
                if not monitor._current(self.gen):
                    return          # stopped while it was starting
                monitor._thread_id = int(kernel32.GetCurrentThreadId())
                monitor.started_at = time.monotonic()
                monitor.running = True
            monitor._ready.set()
            msg = W.MSG()
            while monitor._current(self.gen):
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            with monitor._reg_lock:
                if monitor._registered_gen == self.gen:     # a newer reader's registration is never taken away
                    remove = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, _RIDEV_REMOVE, None),
                                                  RAWINPUTDEVICE(0x01, 0x06, _RIDEV_REMOVE, None))
                    user32.RegisterRawInputDevices(remove, 2, ctypes.sizeof(RAWINPUTDEVICE))
                    monitor._registered_gen = 0
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(name, instance)


#: The process's one monitor (Raw Input registration is per process).
_SHARED = UserInputMonitor()


def shared() -> UserInputMonitor:
    """The process's monitor (running only while someone holds it)."""
    return _SHARED


def running() -> UserInputMonitor | None:
    """The process's monitor when it is listening, else ``None`` (the callers then fall back to polling keys)."""
    return _SHARED if _SHARED.running else None


def acquire() -> bool:
    return _SHARED.acquire()


def release() -> None:
    _SHARED.release()


__all__ = ["CLICK", "KEYS", "KINDS", "MOUSE", "MOVE_WINDOW_S", "UserInputMonitor", "WHEEL", "acquire", "release",
           "running", "shared"]
