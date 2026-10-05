"""The user's presence for the memory service: seconds since their last real input, a vibrating mouse left out.

Why (presence, 5 Oct 2026; report 73 release 6): the user's mouse sensor vibrates by itself and reports 20-30 moves a
second of 0-1 px with nobody at the PC. The memory service judged presence from ``GetLastInputInfo`` and the cursor
(:class:`yuki.memory.watcher._PresenceSampler`), whose rule "3 idle-timer resets within 1.5 s is a person" always held
at 20-30 resets a second: :func:`yuki.memory.watcher.user_idle_s` stayed near 0 for a whole minute with nobody there.
So the timeline never wrote "away", the jobs that wait for the user to step away (the portrait, the weekly review's
catch-up, call transcription on the idle GPU) never ran, and cards timed on stepping away or coming back were wrong.

What counts as the user (the same rule as the tray's input gate and takeover, :mod:`yuki.actions.userinput`):

* a key going down, a mouse button going down, the wheel turning;
* the mouse travelling the user's takeover distance (``ui.json`` ``takeover_move_px``, 40 by default) within
  :data:`MOVE_WINDOW_S`, read from the mouse's own reports, never the cursor: a relative mouse's counts summed over the
  window (a twitching sensor's 0-1 count back-and-forth never sums that far: measured 5 Oct, at most 9 counts over a
  minute of it; a hand gets there at once), an absolute device's (pen, remote desktop) coordinates in screen pixels.
  Yuki's own cursor jumps (``SetCursorPos``, from the tray's process) send no report, so they never count, and a game
  that holds the cursor still (turning in Valorant) still does;
* unlocking the session (:meth:`Presence.note_unlock`: the password goes to the secure desktop, which Raw Input does not
  see).

Never the user: an event carrying Yuki's input stamp (:data:`yuki.core.inputtag.YUKI_INPUT_TAG`). Untagged injected
input (dictation, Deskflow, macro tools) is the user's.

How: :class:`_RawReader` registers for the mouse and keyboard through Raw Input (``RIDEV_INPUTSINK`` on a message-only
window on its own thread). Windows *posts* each event after the fact, so unlike a low-level hook it can never hold up
or lag the mouse. It runs from the first question to the end of the process; nothing polls. Once the mouse showed the
user is there it rests the mouse for :data:`MOUSE_REST_S` (one timer), so a 1000 Hz gaming mouse costs a few reports
every two seconds, not a thousand a second. Measured 5 Oct: ~12 us per report; with the vibrating mouse alone (24
reports a second) the reader thread used ~0.2% of one core, and the reader added under 0.5 MB to a process that
already has a window thread (as the memory service does). Counts and times only:
never which key, never where the cursor was. The tray's reader (:mod:`yuki.actions.userinput`) is the same idea; it
is not reused here because importing it pulls in the whole ``yuki.actions`` package (pyautogui and the hands, ~14 MB
and ~0.1 s in a process that never acts), and it reads the cursor on every move, which this does not need. Raw Input
registration is one per process: never run both readers in one process (the memory service never runs the tray's).

Semantics kept for the callers: :meth:`Presence.idle_s` is seconds since the user's last real input, never less than
Windows' own idle time (``GetLastInputInfo``: no input of any kind for that long means no real input either). If Raw
Input is unavailable (no desktop), ``fallback`` answers (the old sampler) and the reason is in :meth:`Presence.facts`;
if the reader only finishes starting after :data:`START_WAIT_S`, it takes over then (``late_start`` in the facts).

The one thing Raw Input cannot hear (review F1, 5 Oct): input to a window of a higher-integrity process (an elevated
terminal, Task Manager, a tool or game run as administrator) while this process is not elevated: Windows' UIPI keeps
it from the reader, though ``GetLastInputInfo`` (session-wide) still sees it. So when Windows saw input newer than the
reader's last and the window in front is such a process (:class:`_FrontIntegrity`: its token's integrity level above
this process's, or it cannot be asked), Windows' idle time answers, and that input stays credited after the window
leaves the front (``answering: idle_timer`` and ``unheard_front`` in the facts). The cost: with the vibrating mouse and
an elevated window left in front, the user reads as present, the old behaviour, for as long as it stays in front.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from collections import deque
from typing import Any, Callable

from yuki.core.inputtag import is_yuki_input

#: The kinds of real input.
KEYS, CLICK, WHEEL, MOUSE = "keys", "click", "wheel", "mouse"
KINDS = (KEYS, CLICK, WHEEL, MOUSE)
#: The window the mouse's travel is measured over (s), as the tray's takeover watch.
MOVE_WINDOW_S = 0.3
#: The takeover distance when the user's setting cannot be read (px), as ``yuki.actions.leases.TAKEOVER_MOVE_PX``.
DEFAULT_MOVE_PX = 40
#: How often the user's takeover distance is read again from their settings (s): it can change while the service runs.
SETTINGS_EVERY_S = 60.0
#: How long starting the reader may take (s).
START_WAIT_S = 1.0
#: After the mouse showed the user is there (a click, the wheel, a far move), the reader stops listening to the mouse
#: for this long (s), then listens again; the keyboard is always heard. A 1000 Hz gaming mouse would otherwise wake the
#: reader a thousand times a second (~12 us each, measured) for facts that cannot change the answer: presence is
#: judged in minutes, so the last input reading up to this much older costs nothing.
MOUSE_REST_S = 2.0

# Raw Input numbers (Windows' fixed values).
_WM_INPUT = 0x00FF
_WM_QUIT = 0x0012
_RID_INPUT = 0x10000003
_RIM_TYPEMOUSE, _RIM_TYPEKEYBOARD = 0, 1
_RIDEV_INPUTSINK, _RIDEV_REMOVE = 0x00000100, 0x00000001
_HWND_MESSAGE = -3
_WM_KEYDOWN, _WM_SYSKEYDOWN = 0x0100, 0x0104
_MOUSE_MOVE_ABSOLUTE, _MOUSE_VIRTUAL_DESKTOP = 0x01, 0x02
#: RI_MOUSE_{LEFT,RIGHT,MIDDLE}_BUTTON_DOWN, RI_MOUSE_BUTTON_4_DOWN, RI_MOUSE_BUTTON_5_DOWN.
_BUTTON_DOWNS = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100
#: RI_MOUSE_WHEEL, RI_MOUSE_HWHEEL.
_WHEELS = 0x0400 | 0x0800
#: GetSystemMetrics: the primary screen, the virtual desktop (px).
_SM_CXSCREEN, _SM_CYSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 0, 1, 78, 79
_WM_TIMER = 0x0113
_REST_TIMER = 1


class _FrontIntegrity:
    """Whether the window in front belongs to a process at a higher integrity level than this one.

    Windows' UIPI gives a process none of a higher-integrity window's input through Raw Input: while the user types in
    an elevated window (an admin terminal, Task Manager, a game or tool run as administrator) the reader hears nothing,
    though ``GetLastInputInfo`` (session-wide) still sees it. A process that cannot be asked (access denied, a
    protected process) counts as higher; no window in front, or one of this process's own, does not. The answer is
    kept per foreground window, so asking costs two calls while the same window stays in front.
    """

    _TOKEN_QUERY = 0x0008
    _QUERY_LIMITED = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
    _TOKEN_INTEGRITY_LEVEL = 25

    def __init__(self) -> None:
        self._api: Any = None
        self._own: int | None = None
        self._own_pid = 0
        self._key: tuple[int, int] | None = None
        self._answer = False

    def _setup(self) -> Any:
        from ctypes import wintypes as W

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        user32.GetForegroundWindow.restype = W.HWND
        user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        user32.GetWindowThreadProcessId.restype = W.DWORD
        kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        kernel32.OpenProcess.restype = W.HANDLE
        kernel32.GetCurrentProcess.restype = W.HANDLE
        kernel32.CloseHandle.argtypes = [W.HANDLE]
        advapi32.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
        advapi32.OpenProcessToken.restype = W.BOOL
        advapi32.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD,
                                                 ctypes.POINTER(W.DWORD)]
        advapi32.GetTokenInformation.restype = W.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, W.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(W.DWORD)
        self._own_pid = int(kernel32.GetCurrentProcessId())
        return W, user32, kernel32, advapi32

    def _level(self, process: Any) -> int | None:
        """The integrity level (the label's RID: 0x2000 medium, 0x3000 high, 0x4000 system) of a process handle."""
        W, _user32, kernel32, advapi32 = self._api
        token = W.HANDLE()
        if not advapi32.OpenProcessToken(process, self._TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            buf = (ctypes.c_byte * 128)()
            size = W.DWORD(0)
            if not advapi32.GetTokenInformation(token, self._TOKEN_INTEGRITY_LEVEL, buf, ctypes.sizeof(buf),
                                                ctypes.byref(size)):
                return None
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]   # TOKEN_MANDATORY_LABEL.Label.Sid
            count = advapi32.GetSidSubAuthorityCount(sid)[0]
            if not count:
                return None
            return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
        finally:
            kernel32.CloseHandle(token)

    def own_level(self) -> int | None:
        """This process's integrity level."""
        if self._api is None:
            self._api = self._setup()
        if self._own is None:
            self._own = self._level(self._api[2].GetCurrentProcess())
        return self._own

    def pid_higher(self, pid: int) -> bool:
        """Whether process ``pid`` runs above this process's integrity level (or cannot be asked)."""
        own = self.own_level()
        W, _user32, kernel32, _advapi32 = self._api
        if pid == self._own_pid:
            return False
        process = kernel32.OpenProcess(self._QUERY_LIMITED, False, int(pid))
        if not process:
            return True                           # access denied: a protected or higher process
        try:
            level = self._level(process)
        finally:
            kernel32.CloseHandle(process)
        if level is None or own is None:
            return True
        return level > own

    def __call__(self) -> bool:
        if self._api is None:
            self._api = self._setup()
        W, user32, _kernel32, _advapi32 = self._api
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return False
        pid = W.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        key = (hwnd, int(pid.value))
        if key != self._key:
            self._answer = bool(pid.value) and self.pid_higher(int(pid.value))
            self._key = key
        return self._answer


def _settings_move_px() -> int:
    """The user's takeover distance (px), from ``ui.json`` (kept within its range there)."""
    try:
        from yuki.ui import ui_settings

        return int(ui_settings.load().takeover_move_px)
    except Exception:  # noqa: BLE001 - no settings: the default
        return DEFAULT_MOVE_PX


class PresenceInput:
    """The user's real input, as times per kind (``time.monotonic()``), fed by the reader (or a test).

    Args:
        move_px: The travel that counts as the user moving the mouse.
        window_s: The window that travel is measured over (s).
        now: The clock.

    Attributes:
        counts: Events by kind, counts only (``keys``, ``click``, ``wheel``, ``moves``: mouse reports, ``mouse``: moves
            that went far, ``own``: Yuki's own, left out).
        max_travel: The farthest the mouse got within the window without counting: what the jitter looked like.
    """

    def __init__(self, *, move_px: int = DEFAULT_MOVE_PX, window_s: float = MOVE_WINDOW_S,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.move_px = int(move_px)
        self.window_s = float(window_s)
        self.now = now
        self.counts: dict[str, int] = {}
        self.max_travel = 0.0
        self._last: dict[str, float] = {kind: 0.0 for kind in KINDS}
        self._rel: deque[tuple[float, int, int]] = deque()     # relative reports in the window and their sum
        self._sum_x = 0
        self._sum_y = 0
        self._abs: deque[tuple[float, float, float]] = deque()  # absolute places in the window

    # -- facts -----------------------------------------------------------------------------------------------------

    def last(self, kind: str) -> float:
        """When the user's input of this kind last came (monotonic; 0.0: never)."""
        return self._last.get(kind, 0.0)

    def latest(self) -> float:
        """When the user's latest real input came, any kind (0.0: never)."""
        return max(self._last.values())

    # -- intake ----------------------------------------------------------------------------------------------------

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
        """A key went down (autorepeat included: a held key is a hand on the keyboard)."""
        return self._stamp(KEYS, at, extra)

    def button_down(self, at: float | None = None, extra: int | None = 0) -> bool:
        """A mouse button went down."""
        return self._stamp(CLICK, at, extra)

    def wheel(self, at: float | None = None, extra: int | None = 0) -> bool:
        """The wheel turned (or tilted)."""
        return self._stamp(WHEEL, at, extra)

    def _far(self, t: float, travel: float) -> bool:
        if travel >= self.move_px:
            self._last[MOUSE] = t
            self._count(MOUSE)
            return True
        if travel > self.max_travel:
            self.max_travel = travel
        return False

    def relative(self, dx: int, dy: int, at: float | None = None, extra: int | None = 0) -> bool:
        """A relative mouse report of ``(dx, dy)`` counts. Returns whether their net sum within the window reached the
        takeover distance (the user moving the mouse); the sum then starts again."""
        t = self.now() if at is None else float(at)
        self._count("moves")
        if is_yuki_input(extra):
            self._count("own")
            return False
        rel = self._rel
        while rel and t - rel[0][0] > self.window_s:
            _t, ox, oy = rel.popleft()
            self._sum_x -= ox
            self._sum_y -= oy
        rel.append((t, int(dx), int(dy)))
        self._sum_x += int(dx)
        self._sum_y += int(dy)
        if self._far(t, math.hypot(self._sum_x, self._sum_y)):
            rel.clear()
            self._sum_x = self._sum_y = 0
            return True
        return False

    def absolute(self, x: float, y: float, at: float | None = None, extra: int | None = 0) -> bool:
        """An absolute device (pen, remote desktop) report at ``(x, y)`` screen px. Returns whether it is the takeover
        distance from where the device was at the start of the window."""
        t = self.now() if at is None else float(at)
        self._count("moves")
        if is_yuki_input(extra):
            self._count("own")
            return False
        places = self._abs
        while places and t - places[0][0] > self.window_s:
            places.popleft()
        places.append((t, float(x), float(y)))
        _t, x0, y0 = places[0]
        if self._far(t, math.hypot(float(x) - x0, float(y) - y0)):
            places.clear()
            return True
        return False


class _RawReader:
    """The Raw Input window and its message loop, on its own daemon thread, for the life of the process."""

    def __init__(self, monitor: PresenceInput) -> None:
        self.monitor = monitor
        self.running = False
        self.error = ""
        self.thread_id = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, wait_s: float = START_WAIT_S) -> bool:
        """Starts listening; returns whether it is."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._guarded, name="yuki-presence-input", daemon=True)
            self._thread.start()
        self._ready.wait(wait_s)
        return self.running

    def stop(self) -> None:
        """Stops listening (a probe or a test; the service never stops it)."""
        if self.thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self.thread_id, _WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(2.0)

    def _guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - not on Windows, or no desktop: the fallback answers
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
            self._ready.set()

    def _run(self) -> None:
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
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.SetTimer.argtypes = [W.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [W.HWND, ctypes.c_size_t]
        user32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
        kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
        kernel32.GetModuleHandleW.restype = W.HMODULE

        monitor = self.monitor
        buf = RAWINPUT()
        size = ctypes.c_uint(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        device_size = ctypes.sizeof(RAWINPUTDEVICE)
        clock = time.monotonic
        resting = [False]
        hwnd_box = [None]

        def rest_mouse() -> None:
            """The mouse just showed the user is there: stop hearing it for :data:`MOUSE_REST_S`."""
            if resting[0]:
                return
            off = (RAWINPUTDEVICE * 1)(RAWINPUTDEVICE(0x01, 0x02, _RIDEV_REMOVE, None))
            if user32.RegisterRawInputDevices(off, 1, device_size):
                resting[0] = True
                monitor._count("mouse_rests")
                if not user32.SetTimer(hwnd_box[0], _REST_TIMER, int(MOUSE_REST_S * 1000), None):
                    wake_mouse()

        def wake_mouse() -> None:
            user32.KillTimer(hwnd_box[0], _REST_TIMER)
            on = (RAWINPUTDEVICE * 1)(RAWINPUTDEVICE(0x01, 0x02, _RIDEV_INPUTSINK, hwnd_box[0]))
            if user32.RegisterRawInputDevices(on, 1, device_size):
                resting[0] = False
            else:                       # try again after another rest rather than go deaf to the mouse
                user32.SetTimer(hwnd_box[0], _REST_TIMER, int(MOUSE_REST_S * 1000), None)

        def on_input(l_param: int) -> None:
            size.value = ctypes.sizeof(buf)
            got = user32.GetRawInputData(W.HANDLE(l_param), _RID_INPUT, ctypes.byref(buf), ctypes.byref(size),
                                         header_size)
            if got in (0, 0xFFFFFFFF):
                return
            t = clock()
            kind = buf.header.dwType
            if kind == _RIM_TYPEMOUSE:
                if resting[0]:
                    return                                  # queued before the rest began
                m = buf.data.mouse
                extra = int(m.ulExtraInformation)
                flags = int(m.usButtonFlags)
                real = False
                if flags & _BUTTON_DOWNS:
                    real = monitor.button_down(t, extra) or real
                if flags & _WHEELS:
                    real = monitor.wheel(t, extra) or real
                if m.usFlags & _MOUSE_MOVE_ABSOLUTE:      # 0..65535 over the primary screen or the virtual desktop
                    virtual = m.usFlags & _MOUSE_VIRTUAL_DESKTOP
                    w = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN if virtual else _SM_CXSCREEN) or 1
                    h = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN if virtual else _SM_CYSCREEN) or 1
                    real = monitor.absolute(m.lLastX * w / 65535.0, m.lLastY * h / 65535.0, t, extra) or real
                elif m.lLastX or m.lLastY:
                    real = monitor.relative(int(m.lLastX), int(m.lLastY), t, extra) or real
                if real:
                    rest_mouse()
            elif kind == _RIM_TYPEKEYBOARD:
                k = buf.data.keyboard
                if int(k.Message) in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    monitor.key_down(t, int(k.ExtraInformation))

        def wndproc(hwnd: int, msg: int, w_param: int, l_param: int) -> int:
            try:
                if msg == _WM_INPUT:
                    on_input(l_param)
                elif msg == _WM_TIMER and w_param == _REST_TIMER:
                    wake_mouse()
                    return 0
            except Exception:  # noqa: BLE001 - a broken read never takes the reader down
                pass
            return user32.DefWindowProcW(hwnd, msg, w_param, l_param)

        proc = WNDPROC(wndproc)    # held for the life of the window
        instance = kernel32.GetModuleHandleW(None)
        name = f"YukiPresenceInput{id(self):x}"
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
        hwnd_box[0] = hwnd
        devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, _RIDEV_INPUTSINK, hwnd),   # mouse
                                       RAWINPUTDEVICE(0x01, 0x06, _RIDEV_INPUTSINK, hwnd))   # keyboard
        registered = False
        try:
            if not user32.RegisterRawInputDevices(devices, 2, device_size):
                raise OSError(f"RegisterRawInputDevices failed ({ctypes.get_last_error()})")
            registered = True
            self.thread_id = int(kernel32.GetCurrentThreadId())
            self.running = True
            self._ready.set()
            msg = W.MSG()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.running = False
            self.thread_id = 0
            user32.KillTimer(hwnd, _REST_TIMER)
            if registered:
                for usage in (0x02, 0x06):     # one at a time: the mouse may be resting (already removed)
                    remove = (RAWINPUTDEVICE * 1)(RAWINPUTDEVICE(0x01, usage, _RIDEV_REMOVE, None))
                    user32.RegisterRawInputDevices(remove, 1, device_size)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(name, instance)


class Presence:
    """Seconds since the user's last real input (:class:`PresenceInput`, fed by :class:`_RawReader`).

    Args:
        raw_idle: Windows' idle time, any input (s): the floor, and the start's baseline.
        fallback: Answers when Raw Input is unavailable (the old sampler).
        monitor: The input facts (default: a new :class:`PresenceInput`).
        reader: Starts the facts' feed: ``start() -> bool``, ``error``, ``running`` (default: :class:`_RawReader`).
        move_px: The user's takeover distance, read on start and every :data:`SETTINGS_EVERY_S`.
        clock: The facts' clock (``time.monotonic``).
        front_unheard: Whether the window in front is one Raw Input cannot hear (a higher-integrity process: UIPI);
            asked only when Windows saw input newer than the reader's last (default: :class:`_FrontIntegrity`).
        fallback_stop: Stops the fallback's own work once the reader answers (the old sampler's 10 Hz thread).

    Attributes:
        source: ``"raw_input"`` once listening, ``"fallback"`` when it could not, ``""`` before the first question.
        error: Why Raw Input is unavailable.
        unheard: Whether the last answer came from Windows' idle time because the window in front could not be heard.
    """

    def __init__(self, *, raw_idle: Callable[[], float], fallback: Callable[[], float],
                 monitor: PresenceInput | None = None, reader: Any = None,
                 move_px: Callable[[], int] = _settings_move_px, clock: Callable[[], float] = time.monotonic,
                 front_unheard: Callable[[], bool] | None = None,
                 fallback_stop: Callable[[], None] | None = None) -> None:
        self._raw_idle = raw_idle
        self._fallback = fallback
        self._fallback_stop = fallback_stop
        self.monitor = monitor or PresenceInput(now=clock)
        self._reader = reader
        self._move_px = move_px
        self._clock = clock
        self._front_unheard = front_unheard if front_unheard is not None else _FrontIntegrity()
        self._lock = threading.Lock()
        self.source = ""
        self.error = ""
        self._baseline = 0.0       # the start, less Windows' idle time then: no real input known before it
        self._unlock_at = 0.0
        self._settings_at = 0.0
        self._unheard_at = 0.0     # the latest input Windows saw while an unheard window was in front
        self.unheard = False
        self._unheard_switches = 0
        self._late_start = False

    def _raw(self) -> float:
        try:
            return float(self._raw_idle())
        except Exception:  # noqa: BLE001
            return 0.0

    def _ensure(self) -> bool:
        """Whether the reader listens, starting it on the first question."""
        if self.source == "raw_input":
            if self._reader.running:
                return True
            with self._lock:   # the reader stopped (its thread failed): the fallback from now on
                if self.source == "raw_input":
                    self.source, self.error = "fallback", self._reader.error or "the input reader stopped"
            return False
        if self.source == "fallback":
            reader = self._reader
            if reader is None or not getattr(reader, "running", False):
                return False
            with self._lock:   # the reader finished starting after start() gave up waiting: it answers from now on
                if self.source == "fallback" and reader.running:
                    self._baseline = self._clock() - self._raw()
                    self.source, self.error, self._late_start = "raw_input", "", True
                    self._stop_fallback()
            return self.source == "raw_input"
        with self._lock:
            if self.source == "":
                try:
                    self.monitor.move_px = int(self._move_px())
                    self._settings_at = self._clock()
                    if self._reader is None:
                        self._reader = _RawReader(self.monitor)
                    raw = self._raw()
                    if self._reader.start():
                        self._baseline = self._clock() - raw
                        self.source = "raw_input"
                    else:
                        self.source, self.error = "fallback", self._reader.error or "the input reader did not start"
                except Exception as exc:  # noqa: BLE001 - no desktop: the fallback
                    self.source, self.error = "fallback", f"{type(exc).__name__}: {exc}"
        return self.source == "raw_input"

    def last_input_at(self) -> float | None:
        """When the user's last real input came (the clock's time), ``None`` when Raw Input is unavailable."""
        if not self._ensure():
            return None
        return max(self.monitor.latest(), self._baseline, self._unlock_at, self._unheard_at)

    def _stop_fallback(self) -> None:
        if self._fallback_stop is not None:
            try:
                self._fallback_stop()
            except Exception:  # noqa: BLE001
                pass

    def _unheard_front(self) -> bool:
        try:
            return bool(self._front_unheard())
        except Exception:  # noqa: BLE001 - cannot tell: the reader's answer
            return False

    def idle_s(self) -> float:
        """Seconds since the user's last real input, never less than Windows' idle time.

        While a window Raw Input cannot hear is in front (a higher-integrity process, see :class:`_FrontIntegrity`),
        Windows' idle time answers instead, and that input stays credited after the window leaves the front."""
        last = self.last_input_at()
        if last is None:
            return float(self._fallback())
        now = self._clock()
        if now - self._settings_at >= SETTINGS_EVERY_S:
            self._settings_at = now
            try:
                self.monitor.move_px = int(self._move_px())
            except Exception:  # noqa: BLE001 - keep the last one
                pass
        raw = self._raw()
        heard = now - last
        if raw < heard:                         # Windows saw input the reader did not: jitter, or an unheard window
            if self._unheard_front():
                self._unheard_at = max(self._unheard_at, now - raw)
                if not self.unheard:
                    self.unheard = True
                    self._unheard_switches += 1
                return raw
            self.unheard = False
        elif self.unheard and (self.monitor.latest() > self._unheard_at or not self._unheard_front()):
            self.unheard = False                # the reader hears the user again, or the window left the front
        return max(raw, heard)

    def note_unlock(self) -> None:
        """The session was unlocked: the user is at the PC."""
        self._unlock_at = self._clock()

    def facts(self) -> dict[str, Any]:
        """Counts and numbers only, for a log line."""
        out: dict[str, Any] = {"source": self.source or "not_started"}
        if self.error:
            out["error"] = self.error
        if self.source == "raw_input":
            out["counts"] = dict(self.monitor.counts)
            out["max_travel"] = round(self.monitor.max_travel, 1)
            out["move_px"] = self.monitor.move_px
            out["answering"] = "idle_timer" if self.unheard else "raw_input"
            if self._unheard_switches:
                out["unheard_front"] = self._unheard_switches
            if self._late_start:
                out["late_start"] = True
        return out

    def answering(self) -> str:
        """What answers right now: ``raw_input``, ``idle_timer`` (an unheard window in front), ``fallback``."""
        if self.source == "raw_input":
            return "idle_timer" if self.unheard else "raw_input"
        return self.source or "not_started"


__all__ = ["DEFAULT_MOVE_PX", "KINDS", "MOVE_WINDOW_S", "Presence", "PresenceInput", "SETTINGS_EVERY_S"]
