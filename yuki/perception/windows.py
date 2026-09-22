"""Desktop overview: what windows exist right now.

Pure Win32 (``EnumWindows`` + DWM cloak state + ``psutil`` for process names).
No UI Automation is involved, which keeps a full overview in the low
milliseconds so the agent can be handed a fresh one every turn.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil
import win32con
import win32gui
import win32process

import yuki  # noqa: F401  (imported for the DPI-awareness side effect)

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi

_DWMWA_CLOAKED = 14

#: ``GetAncestor(hwnd, GA_ROOT)``: the top-level window a control belongs to.
_GA_ROOT = 2

class _GUIThreadInfo(ctypes.Structure):
    """``GUITHREADINFO``: what one GUI thread thinks it is doing right now."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND

#: Window classes registered by Windows itself: the explorer.exe shell surfaces
#: (desktop, taskbars, task view), the alt-tab / notification hosts, and the XAML
#: framework's own windowed-popup host.  A window of one of these classes belongs
#: to the OS, not to an app the user opened, so it is dropped from the overview.
#: Window-class plumbing, not app matching - nothing here names an application.
#:
#: ``Xaml_WindowedPopupClass`` is the class Microsoft's XAML framework registers
#: for the light-dismiss surfaces it has to give their own HWND (flyouts, menus,
#: combo drop-downs, tooltips).  The framework creates and titles them
#: ("PopupHost"), keeps one alive per popup site whether or not anything is shown,
#: and marks them ``WS_EX_NOACTIVATE`` so the user can never switch to one: a
#: single Windows 11 Notepad contributed fourteen of the nineteen lines in one
#: real snapshot.  Dialogs are untouched by this - a "Save as" dialog is a
#: ``#32770`` and an in-window WinUI dialog ("Save changes?") lives in its
#: parent's UIA tree rather than in a popup host.
_SHELL_WINDOW_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "TopLevelWindowForOverflowXamlIsland",
        "Shell_InputSwitchTopLevelWindow",
        "ForegroundStaging",
        "MultitaskingViewFrame",
        "XamlExplorerHostIslandWindow",
        "Xaml_WindowedPopupClass",
    }
)


@dataclass
class WindowInfo:
    """One top-level window as the model sees it."""

    hwnd: int
    title: str
    process_name: str
    pid: int
    is_foreground: bool
    is_minimized: bool
    bounds: tuple[int, int, int, int]  # left, top, right, bottom (screen px)


@dataclass
class DesktopOverview:
    """Everything cheap that can be known about the desktop at one instant."""

    windows: list[WindowInfo] = field(default_factory=list)
    foreground_hwnd: int | None = None
    cursor: tuple[int, int] = (0, 0)
    screen_size: tuple[int, int] = (0, 0)
    captured_at: float = 0.0


def is_cloaked(hwnd: int) -> bool:
    """True when DWM reports the window as cloaked (hidden but "visible").

    Suspended UWP apps, windows parked on another virtual desktop and a pile of
    helper windows pass ``IsWindowVisible`` while being cloaked; reporting them
    would fill the overview with things the user cannot see.
    """
    value = ctypes.c_int(0)
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.c_uint(_DWMWA_CLOAKED),
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    if result != 0:  # S_OK == 0; if the call fails, assume not cloaked
        return False
    return value.value != 0


def is_user_window(hwnd: int) -> bool:
    """Filter shared by the overview and the launch watcher.

    Answers one question - could the user see this window and point at it? - and
    answers it only from OS facts.  A user window is visible and not DWM-cloaked,
    is not a tool window, is not one of the OS's own window classes, carries a
    title, and occupies a rectangle that overlaps the desktop.
    Minimised windows keep the off-screen rectangle Windows parks them at, so the
    geometry rules are skipped for them and they are still reported (flagged as
    minimised).

    Nothing here looks at the process or reads the title: every rule is a window
    style, a cloak state, a rectangle, or a system window class.
    """
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return False
    title = win32gui.GetWindowText(hwnd)
    if title.strip() == "":
        return False
    if win32gui.GetClassName(hwnd) in _SHELL_WINDOW_CLASSES:
        return False
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & win32con.WS_EX_TOOLWINDOW:
        return False
    if is_cloaked(hwnd):
        return False
    if not win32gui.IsIconic(hwnd):
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left <= 1 or bottom - top <= 1:
            return False
        # Parked off the side of every monitor: it exists, but not on screen.
        v_left, v_top, v_right, v_bottom = virtual_screen_bounds()
        if right <= v_left or left >= v_right or bottom <= v_top or top >= v_bottom:
            return False
    return True


def focused_control_hwnd(hwnd: int) -> int:
    """The control ``hwnd``'s own GUI thread reports keyboard focus on, or 0.

    ``GetGUIThreadInfo`` asks the window's thread what *it* thinks is focused,
    which is the only signal that a freshly created or freshly activated window
    has finished wiring up its input: until the thread names a focused control,
    ``SendInput`` reports success and the characters go nowhere.
    """
    try:
        thread_id, _ = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return 0  # the window died between the caller's check and this call
    if not thread_id:
        return 0
    info = _GUIThreadInfo()
    info.cbSize = ctypes.sizeof(_GUIThreadInfo)
    if not _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return 0
    return int(info.hwndFocus or 0)


def accepts_input(hwnd: int) -> bool:
    """True when keystrokes sent right now would land in ``hwnd``.

    Two OS facts have to agree: the window is the foreground window (so synthetic
    input is delivered to its thread at all), and that thread reports keyboard
    focus on a control belonging to this top-level window (so there is something
    ready to receive it).  In the gap right after a launch or an activation the
    first is true while the second is not, and characters typed in that gap are
    lost silently - on this desktop Windows 11 Notepad turned "hello world" into
    "ld" while ``SendInput`` reported every event accepted.
    """
    hwnd = int(hwnd)
    if int(_user32.GetForegroundWindow()) != hwnd:
        return False
    focus = focused_control_hwnd(hwnd)
    if not focus:
        return False
    # The focused control must belong to this window, not to another top-level
    # window the same thread owns (a popup, a second document).
    return int(_user32.GetAncestor(wintypes.HWND(focus), _GA_ROOT) or 0) == hwnd


def _process_name(pid: int) -> str:
    """Process image name for a pid, or an empty string when unreadable."""
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _window_info(hwnd: int, foreground: int) -> WindowInfo:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        process_name=_process_name(pid),
        pid=pid,
        is_foreground=hwnd == foreground,
        is_minimized=bool(win32gui.IsIconic(hwnd)),
        bounds=(left, top, right, bottom),
    )


def list_windows() -> list[WindowInfo]:
    """All user windows in Z-order (topmost first)."""
    foreground = _user32.GetForegroundWindow()
    handles: list[int] = []

    def _collect(hwnd: int, _: object) -> bool:
        if is_user_window(hwnd):
            handles.append(hwnd)
        return True

    win32gui.EnumWindows(_collect, None)

    windows: list[WindowInfo] = []
    for hwnd in handles:
        try:
            windows.append(_window_info(hwnd, foreground))
        except Exception:
            continue  # the window died between enumeration and inspection
    return windows


def window_info(hwnd: int) -> WindowInfo | None:
    """Look up a single window, or ``None`` when it no longer exists."""
    if not win32gui.IsWindow(hwnd):
        return None
    try:
        return _window_info(hwnd, _user32.GetForegroundWindow())
    except Exception:
        return None


def screen_size() -> tuple[int, int]:
    """Primary monitor size in physical pixels."""
    return (
        _user32.GetSystemMetrics(win32con.SM_CXSCREEN),
        _user32.GetSystemMetrics(win32con.SM_CYSCREEN),
    )


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Bounding rectangle of all monitors: (left, top, right, bottom).

    Coordinates on secondary monitors are often negative or far beyond the
    primary screen, so this - not ``screen_size()`` - is what input
    coordinates get validated against.
    """
    left = _user32.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    top = _user32.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    width = _user32.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    height = _user32.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return (left, top, left + width, top + height)


def cursor_position() -> tuple[int, int]:
    """Current cursor position in screen pixels."""
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y)


def get_desktop_overview() -> DesktopOverview:
    """Snapshot every user window, the foreground window and the cursor."""
    windows = list_windows()
    foreground = _user32.GetForegroundWindow()
    return DesktopOverview(
        windows=windows,
        foreground_hwnd=foreground or None,
        cursor=cursor_position(),
        screen_size=screen_size(),
        captured_at=time.time(),
    )


def format_overview(o: DesktopOverview) -> str:
    """Compact text rendering of an overview, one line per window.

    The model is handed this every turn, so it is kept to facts it cannot get
    anywhere else and nothing is said twice.  The window count and the foreground
    handle are not in the header: the lines are the count, and the foreground one
    is flagged (the header only mentions it when the foreground window is *not*
    one of the listed ones -- a shell surface, say -- because that is information
    the list cannot carry).  A minimised window's rectangle is the off-screen
    parking spot Windows gives it, which is worse than useless to act on, so it
    is left out rather than printed as if it meant something.

    Handles are printed in decimal because the model passes them straight back
    as JSON integers, and geometry as origin plus size because that is what a
    reader wants to know about a window.
    """
    width, height = o.screen_size
    lines = [f"screen {width}x{height} | cursor ({o.cursor[0]},{o.cursor[1]})"]
    if o.foreground_hwnd and not any(w.is_foreground for w in o.windows):
        lines[0] += f" | foreground hwnd {o.foreground_hwnd} (not a user window)"
    if not o.windows:
        lines.append("(no user windows)")
    for w in o.windows:
        left, top, right, bottom = w.bounds
        where = (
            "minimized"
            if w.is_minimized
            else f"@{left},{top} {right - left}x{bottom - top}"
        )
        flags = " FOREGROUND" if w.is_foreground else ""
        lines.append(f'[{w.hwnd}] {w.process_name or "unknown"} "{w.title}" {where}{flags}')
    return "\n".join(lines)
