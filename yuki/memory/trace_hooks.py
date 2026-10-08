"""The trace's hook thread (report 78 §4.1): window events, the clipboard and the Recent folder, events only.

One thread with a message-only window and a ``MsgWaitForMultipleObjectsEx`` loop (no polling). Its callbacks do
nothing but put small tuples on the trace worker's queue; it never calls UI Automation.

* **Foreground** (system-wide): ``("fg", t, hwnd, pid)``; the per-process hooks follow the window in front.
* **Low-rate events, always on, for the process in front only**: ``INVOKED``, ``SELECTION`` / ``SELECTIONADD``,
  ``SYSTEM_MENUSTART`` / ``MENUEND`` and the popup pair, ``DIALOGSTART``, ``FOCUS``, and ``NAMECHANGE`` of top-level
  windows only (a title: anything else from a streaming app is dropped at the callback's first line):
  ``("ev", t, event, hwnd, id_object, id_child, tick)`` (``tick``: the event's own time, ``dwmsEventTime``).
* **Outcome events, only around an action**: ``STATECHANGE``, ``VALUECHANGE``, ``SHOW``, ``HIDE``, ``REORDER``
  for the process in front, registered when an action starts (:meth:`TraceHooks.action`) and removed
  :data:`OUTCOME_S` after the last action. Never ``LOCATIONCHANGE``. At most :data:`OUTCOME_CAP` events per action
  are queued (``("out", t, event, hwnd, id_object, id_child, tick, gen)``); the rest are only counted. In a
  streaming app (a chat being written, a progress bar) nothing outside those windows ever reaches Yuki.
* **Clipboard** (``AddClipboardFormatListener``): ``("clip", t, seq, owner_pid, formats, private)``: the sequence
  number, the owner's process, the format *names* (``GetUpdatedClipboardFormats``: the clipboard is never opened,
  never read), and whether the copy carries a private mark (``ExcludeClipboardContentFromMonitorProcessing``,
  ``CanIncludeInClipboardHistory``, ``Clipboard Viewer Ignore``: password managers set these).
* **Files**: a change notification on the shell's Recent folder: ``("recent", t)`` (the worker reads which links
  are new; a ``DIALOGSTART`` among the low-rate events and the foreground's return tell it a file came from that
  app's open/save dialog).

While blocked (game gate, pause, consent off) the per-process and outcome hooks are removed and nothing is queued.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes as W
from pathlib import Path

#: Outcome hooks stay this long after the last action (s).
OUTCOME_S = 1.5
#: Outcome events queued per action at most; the rest are counted.
OUTCOME_CAP = 200

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MENUSTART, EVENT_SYSTEM_MENUEND = 0x0004, 0x0005
EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND = 0x0006, 0x0007
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER = 0x8002, 0x8003, 0x8004
EVENT_OBJECT_FOCUS, EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD = 0x8005, 0x8006, 0x8007
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_VALUECHANGE = 0x800E
EVENT_OBJECT_INVOKED = 0x8013
#: (low, high) ranges of the low-rate set and of the outcome set.
LOW_RATE = ((EVENT_SYSTEM_MENUSTART, EVENT_SYSTEM_MENUPOPUPEND), (EVENT_SYSTEM_DIALOGSTART, EVENT_SYSTEM_DIALOGSTART),
            (EVENT_OBJECT_FOCUS, EVENT_OBJECT_SELECTIONADD), (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE),
            (EVENT_OBJECT_INVOKED, EVENT_OBJECT_INVOKED))
OUTCOME = ((EVENT_OBJECT_SHOW, EVENT_OBJECT_REORDER), (EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_STATECHANGE),
           (EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_VALUECHANGE))
#: Clipboard formats whose presence marks a copy as private (Windows' documented names; password managers set them).
PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                   "Clipboard Viewer Ignore")
#: Standard clipboard format names (Windows' fixed ids).
STANDARD_FORMATS = {1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 7: "CF_OEMTEXT", 8: "CF_DIB",
                    13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE", 15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5"}
#: Format names kept per copy.
MAX_FORMATS = 8

_WINEVENT_OUTOFCONTEXT, _WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
_OBJID_WINDOW, _CHILDID_SELF = 0, 0
_GA_ROOT = 2
_HWND_MESSAGE = -3
_WM_TIMER, _WM_QUIT, _WM_CLIPBOARDUPDATE = 0x0113, 0x0012, 0x031D
_WM_APP = 0x8000
WM_TRACE_ACTION = _WM_APP + 21
WM_TRACE_BLOCK = _WM_APP + 22
WM_TRACE_STOP = _WM_APP + 23
_OUTCOME_TIMER = 7
_QS_ALLINPUT, _MWMO_INPUTAVAILABLE, _PM_REMOVE = 0x04FF, 0x0004, 0x0001
_WAIT_OBJECT_0, _INFINITE = 0, 0xFFFFFFFF
_FILE_NOTIFY_CHANGE_FILE_NAME, _FILE_NOTIFY_CHANGE_LAST_WRITE = 0x1, 0x10
_INVALID_HANDLE = ctypes.c_void_p(-1).value

_U = ctypes.WinDLL("user32", use_last_error=True)
_K = ctypes.WinDLL("kernel32", use_last_error=True)
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND, W.LONG, W.LONG, W.DWORD, W.DWORD)
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, W.HWND, ctypes.c_uint, W.WPARAM, W.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


_U.SetWinEventHook.argtypes = [W.DWORD, W.DWORD, W.HMODULE, _WINEVENTPROC, W.DWORD, W.DWORD, W.DWORD]
_U.SetWinEventHook.restype = W.HANDLE
_U.UnhookWinEvent.argtypes = [W.HANDLE]
_U.UnhookWinEvent.restype = W.BOOL
_U.DefWindowProcW.argtypes = [W.HWND, ctypes.c_uint, W.WPARAM, W.LPARAM]
_U.DefWindowProcW.restype = _LRESULT
_U.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_U.RegisterClassW.restype = W.ATOM
_U.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
_U.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, ctypes.c_void_p]
_U.CreateWindowExW.restype = W.HWND
_U.DestroyWindow.argtypes = [W.HWND]
_U.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
_U.PostMessageW.restype = W.BOOL
_U.SetTimer.argtypes = [W.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
_U.SetTimer.restype = ctypes.c_size_t
_U.KillTimer.argtypes = [W.HWND, ctypes.c_size_t]
_U.MsgWaitForMultipleObjectsEx.argtypes = [W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.DWORD]
_U.MsgWaitForMultipleObjectsEx.restype = W.DWORD
_U.PeekMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
_U.PeekMessageW.restype = W.BOOL
_U.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
_U.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
_U.DispatchMessageW.restype = _LRESULT
_U.GetForegroundWindow.restype = W.HWND
_U.GetAncestor.argtypes = [W.HWND, W.UINT]
_U.GetAncestor.restype = W.HWND
_U.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
_U.GetWindowThreadProcessId.restype = W.DWORD
_U.WindowFromPhysicalPoint.argtypes = [W.POINT]
_U.WindowFromPhysicalPoint.restype = W.HWND
_U.AddClipboardFormatListener.argtypes = [W.HWND]
_U.AddClipboardFormatListener.restype = W.BOOL
_U.RemoveClipboardFormatListener.argtypes = [W.HWND]
_U.GetClipboardSequenceNumber.restype = W.DWORD
_U.GetClipboardOwner.restype = W.HWND
_U.GetUpdatedClipboardFormats.argtypes = [ctypes.POINTER(W.UINT), W.UINT, ctypes.POINTER(W.UINT)]
_U.GetUpdatedClipboardFormats.restype = W.BOOL
_U.GetClipboardFormatNameW.argtypes = [W.UINT, W.LPWSTR, ctypes.c_int]
_U.GetClipboardFormatNameW.restype = ctypes.c_int
_U.RegisterClipboardFormatW.argtypes = [W.LPCWSTR]
_U.RegisterClipboardFormatW.restype = W.UINT
_K.GetModuleHandleW.argtypes = [W.LPCWSTR]
_K.GetModuleHandleW.restype = W.HMODULE
_K.FindFirstChangeNotificationW.argtypes = [W.LPCWSTR, W.BOOL, W.DWORD]
_K.FindFirstChangeNotificationW.restype = W.HANDLE
_K.FindNextChangeNotification.argtypes = [W.HANDLE]
_K.FindNextChangeNotification.restype = W.BOOL
_K.FindCloseChangeNotification.argtypes = [W.HANDLE]


def recent_folder() -> Path:
    """The shell's Recent folder (``%APPDATA%\\Microsoft\\Windows\\Recent``)."""
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Recent"


def clipboard_formats() -> tuple[tuple[str, ...], bool]:
    """The clipboard's format names now and whether it carries a private mark, without opening it."""
    count = W.UINT(0)
    buf = (W.UINT * 64)()
    names: list[str] = []
    ids: set[int] = set()
    if _U.GetUpdatedClipboardFormats(buf, 64, ctypes.byref(count)):
        name_buf = ctypes.create_unicode_buffer(64)
        for i in range(min(int(count.value), 64)):
            fmt = int(buf[i])
            ids.add(fmt)
            if len(names) >= MAX_FORMATS:
                continue
            if fmt in STANDARD_FORMATS:
                names.append(STANDARD_FORMATS[fmt])
            elif fmt >= 0xC000 and _U.GetClipboardFormatNameW(fmt, name_buf, 64) > 0:
                names.append(name_buf.value[:40])
    private = any(_U.RegisterClipboardFormatW(name) in ids for name in PRIVATE_FORMATS)
    return tuple(names), private


class TraceHooks:
    """The hook thread (see the module docstring).

    Args:
        put: puts one item on the worker's queue.
        pid_of: ``(hwnd) -> tuple of pids`` behind a window (the app's and, for a UWP frame, the hosted app's).
        own_pid: this process (never hooked).
        recent: the Recent folder (None: no file notifications).
        clock: ``time.monotonic``.
    """

    def __init__(self, put: Callable[[tuple], None], *, pid_of: Callable[[int], tuple[int, ...]] | None = None,
                 own_pid: int | None = None, recent: Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._put = put
        self._pid_of = pid_of or _pids_behind
        self._own = int(own_pid or os.getpid())
        self._recent = recent
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.error = ""
        self.hwnd = 0
        self._callback = _WINEVENTPROC(self._on_event)
        self._fg_hook = None
        self._low: list[int] = []
        self._out: list[int] = []
        self._pids: tuple[int, ...] = ()
        self._fg = 0
        self.blocked = True
        self._last_action = 0.0
        self.gen = 0
        self._n = 0
        self.counts: dict[str, int] = {"fg": 0, "low": 0, "low_dropped": 0, "outcome": 0, "outcome_over": 0,
                                       "clip": 0, "recent": 0, "outcome_opens": 0}

    # -- control (any thread) ----------------------------------------------------------------------------------------

    def start(self, timeout_s: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._main, name="yuki-trace-hooks", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)
        return bool(self.hwnd)

    def stop(self, timeout_s: float = 3.0) -> None:
        if self.hwnd:
            _U.PostMessageW(self.hwnd, WM_TRACE_STOP, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout_s)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def action(self, t: float, fold: bool = False) -> None:
        """An action started (button-down, chord, named key, wheel): open the outcome hooks (cheap; any thread).

        ``fold``: a repeat that folds into the previous action's row (a held arrow, the next wheel notch): the window
        is kept open but the event cap is not renewed, so one row never gets more than :data:`OUTCOME_CAP` events
        however many repeats it folds (second verify V6)."""
        self._last_action = t
        if self.hwnd:
            _U.PostMessageW(self.hwnd, WM_TRACE_ACTION, int(bool(fold)), 0)

    def set_blocked(self, blocked: bool) -> None:
        if self.hwnd:
            _U.PostMessageW(self.hwnd, WM_TRACE_BLOCK, int(bool(blocked)), 0)
        else:
            self.blocked = bool(blocked)

    # -- hooks (this thread) -----------------------------------------------------------------------------------------

    def _hook_set(self, ranges: tuple[tuple[int, int], ...]) -> list[int]:
        handles = []
        flags = _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS
        for pid in self._pids:
            for low, high in ranges:
                h = _U.SetWinEventHook(low, high, None, self._callback, pid, 0, flags)
                if h:
                    handles.append(h)
        return handles

    @staticmethod
    def _unhook(handles: list[int]) -> None:
        for h in handles:
            _U.UnhookWinEvent(h)
        handles.clear()

    def _follow(self, hwnd: int) -> None:
        pids = tuple(p for p in self._pid_of(hwnd) if p and p != self._own) if hwnd else ()
        if pids == self._pids and (self._low or not pids):
            return
        self._unhook(self._low)
        had_out = bool(self._out)
        self._unhook(self._out)
        self._pids = pids
        if self.blocked or not pids:
            return
        self._low = self._hook_set(LOW_RATE)
        if had_out:
            self._out = self._hook_set(OUTCOME)

    def _open_outcome(self, fold: bool = False) -> None:
        if not fold:
            self.gen += 1
            self._n = 0
        if self.blocked:
            return
        if not self._out and self._pids:
            self._out = self._hook_set(OUTCOME)
            self.counts["outcome_opens"] += 1
        _U.SetTimer(self.hwnd, _OUTCOME_TIMER, int(OUTCOME_S * 1000) + 20, None)

    def _close_outcome_if_quiet(self) -> None:
        left = self._last_action + OUTCOME_S - self._clock()
        if left > 0.01 and not self.blocked:
            _U.SetTimer(self.hwnd, _OUTCOME_TIMER, int(left * 1000) + 20, None)
            return
        _U.KillTimer(self.hwnd, _OUTCOME_TIMER)
        self._unhook(self._out)

    def _set_blocked(self, blocked: bool) -> None:
        if blocked == self.blocked:
            return
        self.blocked = blocked
        if blocked:
            self._unhook(self._low)
            self._unhook(self._out)
            self._pids = ()
        else:
            self._follow(self._fg)

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:   # noqa: ANN001
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                hwnd = int(hwnd or 0)
                root = int(_U.GetAncestor(hwnd, _GA_ROOT) or hwnd) if hwnd else 0
                self._fg = root
                self.counts["fg"] += 1
                self._follow(root)
                if not self.blocked:
                    self._put(("fg", self._clock(), root, self._pids[0] if self._pids else 0))
                return
            if self.blocked:
                return
            if event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER, EVENT_OBJECT_STATECHANGE,
                         EVENT_OBJECT_VALUECHANGE):
                if self._n < OUTCOME_CAP:
                    self._n += 1
                    self.counts["outcome"] += 1
                    # capQ2a: the event's own time (``dwmsEventTime``, Windows' tick count) rides along, before the
                    # generation (still the last field): the worker can tell a change Windows dates before a
                    # lookup's answer even when it reached this thread after it
                    self._put(("out", self._clock(), int(event), int(hwnd or 0), int(id_object), int(id_child),
                               int(ms or 0) & 0xFFFFFFFF, self.gen))
                else:
                    self.counts["outcome_over"] += 1
                return
            if event == EVENT_OBJECT_NAMECHANGE and not (id_object == _OBJID_WINDOW and id_child == _CHILDID_SELF
                                                         and int(hwnd or 0) == self._fg):
                self.counts["low_dropped"] += 1      # only the window's own title: a streaming app costs nothing more
                return
            self.counts["low"] += 1
            self._put(("ev", self._clock(), int(event), int(hwnd or 0), int(id_object), int(id_child),
                       int(ms or 0) & 0xFFFFFFFF))
        except Exception:  # noqa: BLE001 - never let an exception escape a ctypes callback
            pass

    def _on_clipboard(self) -> None:
        if self.blocked:
            return
        try:
            seq = int(_U.GetClipboardSequenceNumber())
            owner = int(_U.GetClipboardOwner() or 0)
            pid = W.DWORD(0)
            if owner:
                _U.GetWindowThreadProcessId(owner, ctypes.byref(pid))
            formats, private = clipboard_formats()
            self.counts["clip"] += 1
            self._put(("clip", self._clock(), seq, int(pid.value), formats, private))
        except Exception:  # noqa: BLE001
            pass

    # -- the thread --------------------------------------------------------------------------------------------------

    def _wndproc(self, hwnd: int, msg: int, w_param: int, l_param: int) -> int:
        try:
            if msg == WM_TRACE_ACTION:
                self._open_outcome(bool(w_param))
                return 0
            if msg == _WM_TIMER and w_param == _OUTCOME_TIMER:
                self._close_outcome_if_quiet()
                return 0
            if msg == WM_TRACE_BLOCK:
                self._set_blocked(bool(w_param))
                return 0
            if msg == _WM_CLIPBOARDUPDATE:
                self._on_clipboard()
                return 0
            if msg == WM_TRACE_STOP:
                _U.PostMessageW(hwnd, _WM_QUIT, 0, 0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return _U.DefWindowProcW(hwnd, msg, w_param, l_param)

    def _main(self) -> None:
        proc = _WNDPROC(self._wndproc)
        self._proc = proc
        instance = _K.GetModuleHandleW(None)
        name = f"YukiTraceHooks{id(self):x}"
        wc = _WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = instance
        wc.lpszClassName = name
        change = None
        try:
            if not _U.RegisterClassW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
            hwnd = _U.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, W.HWND(_HWND_MESSAGE), None, instance, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
            self.hwnd = int(hwnd)
            self._fg_hook = _U.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._callback,
                                               0, 0, _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
            _U.AddClipboardFormatListener(self.hwnd)
            if self._recent is not None and Path(self._recent).is_dir():
                handle = _K.FindFirstChangeNotificationW(str(self._recent), False,
                                                         _FILE_NOTIFY_CHANGE_FILE_NAME | _FILE_NOTIFY_CHANGE_LAST_WRITE)
                if handle and handle != _INVALID_HANDLE:
                    change = handle
            fg = int(_U.GetForegroundWindow() or 0)
            self._fg = int(_U.GetAncestor(fg, _GA_ROOT) or fg) if fg else 0
        except Exception as exc:  # noqa: BLE001
            self.error = type(exc).__name__
            self._ready.set()
            return
        self._ready.set()
        msg = W.MSG()
        handles = (W.HANDLE * 1)(change) if change else None
        try:
            while True:
                result = _U.MsgWaitForMultipleObjectsEx(1 if change else 0, handles, _INFINITE, _QS_ALLINPUT,
                                                        _MWMO_INPUTAVAILABLE)
                if change and result == _WAIT_OBJECT_0:
                    _K.FindNextChangeNotification(change)
                    if not self.blocked:
                        self.counts["recent"] += 1
                        self._put(("recent", self._clock()))
                    continue
                quit_seen = False
                while _U.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                    if msg.message == _WM_QUIT:
                        quit_seen = True
                        break
                    _U.TranslateMessage(ctypes.byref(msg))
                    _U.DispatchMessageW(ctypes.byref(msg))
                if quit_seen:
                    break
        finally:
            self._unhook(self._low)
            self._unhook(self._out)
            if self._fg_hook:
                _U.UnhookWinEvent(self._fg_hook)
            try:
                _U.RemoveClipboardFormatListener(self.hwnd)
            except Exception:  # noqa: BLE001
                pass
            if change:
                _K.FindCloseChangeNotification(change)
            _U.KillTimer(self.hwnd, _OUTCOME_TIMER)
            _U.DestroyWindow(self.hwnd)
            _U.UnregisterClassW(name, instance)
            self.hwnd = 0


def _pids_behind(hwnd: int) -> tuple[int, ...]:
    """The process behind a window and, for a UWP frame, the hosted app's (the watcher's rule)."""
    pids: list[int] = []
    try:
        from yuki.memory.watcher import _window_pid

        pids.append(int(_window_pid(hwnd)))
    except Exception:  # noqa: BLE001
        pass
    raw = W.DWORD(0)
    _U.GetWindowThreadProcessId(hwnd, ctypes.byref(raw))
    pids.append(int(raw.value))
    return tuple(dict.fromkeys(p for p in pids if p))


def window_title(hwnd: int) -> str:
    """A top-level window's title (``GetWindowTextW``)."""
    try:
        import win32gui

        return win32gui.GetWindowText(hwnd) or ""
    except Exception:  # noqa: BLE001
        return ""


def root_at(x: int, y: int) -> int:
    """The top-level window under a screen point (physical pixels, as the pointer was read)."""
    try:
        h = int(_U.WindowFromPhysicalPoint(W.POINT(int(x), int(y))) or 0)
        return int(_U.GetAncestor(h, _GA_ROOT) or h) if h else 0
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["LOW_RATE", "OUTCOME", "OUTCOME_CAP", "OUTCOME_S", "PRIVATE_FORMATS", "TraceHooks", "clipboard_formats",
           "recent_folder", "root_at", "window_title"]

