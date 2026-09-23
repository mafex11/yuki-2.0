"""Desktop overview: what windows exist right now, on which monitors.

Pure Win32 (``EnumWindows`` + DWM cloak state + ``EnumDisplayMonitors`` +
``psutil`` for process names).  No UI Automation is involved, which keeps a full
overview in the low milliseconds so the agent can be handed a fresh one every
turn.

Every coordinate in here - window rectangles, element centres, the cursor - is in
**virtual-desktop** pixels: one coordinate space spanning every monitor, with the
primary monitor's top-left at (0, 0) and other monitors at whatever offset the
user arranged them, which can be negative.  ``GetWindowRect`` and ``GetCursorPos``
already speak it, and :mod:`yuki.actions.input` validates against it, so nothing
needs converting - but the space is only trustworthy in a per-monitor DPI aware
process, which :mod:`yuki` arranges at import.  Without that, Windows lies to the
process about rectangles on any display whose scaling differs from the primary's.
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
from yuki.perception.system import locale_facts

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


class _MonitorInfoEx(ctypes.Structure):
    """``MONITORINFOEXW``: one display's rectangles, flags and device name."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


#: ``MONITORINFOF_PRIMARY``.
_MONITORINFOF_PRIMARY = 1

#: ``MDT_EFFECTIVE_DPI``: the DPI the user's scaling setting asks for, which is
#: the one window coordinates are laid out against.
_MDT_EFFECTIVE_DPI = 0

#: A display at 100% scaling reports 96 dots per inch; every scale factor here is
#: a monitor's DPI divided by this.
_DEFAULT_DPI = 96

_MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR if hasattr(wintypes, "HMONITOR") else wintypes.HANDLE,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND
_owner_api = ctypes.WinDLL("user32")  # private: its argtypes leak nowhere
_owner_api.IsWindowEnabled.argtypes = [wintypes.HWND]
_owner_api.IsWindowEnabled.restype = wintypes.BOOL
_owner_api.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
_owner_api.GetWindow.restype = wintypes.HWND

#: ``GetWindow(hwnd, GW_OWNER)``: the window that owns a popup or dialog.
_GW_OWNER = 4

#: ``WS_EX_NOACTIVATE``: a window the user can never switch to (popup hosts,
#: tooltips) - never a dialog waiting for an answer.
_WS_EX_NOACTIVATE = 0x08000000

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
    #: ``IsWindowEnabled``. False while a modal dialog it owns holds it (clicks
    #: and keys sent to it are then ignored), or when the app disabled it.
    is_enabled: bool = True
    #: ``GetWindow(GW_OWNER)``: the window that owns this one (a dialog's
    #: parent window), or None for an unowned top-level window.
    owner_hwnd: int | None = None


@dataclass
class MonitorInfo:
    """One display, in virtual-desktop coordinates.

    Attributes:
        index: position in :func:`list_monitors`, which is the order Windows
            enumerates displays in.  Stable for as long as the arrangement is.
        name: the display device name Windows uses (``\\.\\DISPLAY1``).
        bounds: the whole monitor: ``(left, top, right, bottom)``.
        work_area: ``bounds`` minus the taskbar and any other docked appbar -
            where a maximised window actually ends up.
        is_primary: the monitor whose top-left corner is the origin (0, 0).
        dpi_scale: display scaling as a factor, 1.0 at 100% and 1.5 at 150%.
            Informational only: this process is per-monitor DPI aware, so every
            coordinate here (bounds, window rectangles, element centres, the
            cursor) is already in physical pixels and needs no scaling.  It tells
            a reader why text on that display looks bigger for the same pixels.
        dpi: the raw effective DPI ``dpi_scale`` was derived from.
    """

    index: int
    name: str
    bounds: tuple[int, int, int, int]
    work_area: tuple[int, int, int, int]
    is_primary: bool
    dpi_scale: float
    dpi: int


@dataclass
class DesktopOverview:
    """Everything cheap that can be known about the desktop at one instant.

    ``screen_size`` is the size of the **virtual desktop** - the box around every
    monitor - not of the primary display.  It used to be the primary display's
    size, which on this desktop meant reporting 1920x1080 while windows sat at
    x=2480: every coordinate the model reasoned about was measured against a
    picture that was missing two thirds of the screen space it was allowed to
    click in.  ``virtual_bounds`` gives the same box with its origin, which is
    what matters when a monitor is placed left of or above the primary one and the
    coordinates there are negative.
    """

    windows: list[WindowInfo] = field(default_factory=list)
    foreground_hwnd: int | None = None
    cursor: tuple[int, int] = (0, 0)
    screen_size: tuple[int, int] = (0, 0)
    captured_at: float = 0.0
    monitors: list[MonitorInfo] = field(default_factory=list)
    virtual_bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    #: Owner of the foreground window (``GW_OWNER``), which may be a listed
    #: window even when the foreground one itself is not listed.
    foreground_owner_hwnd: int | None = None
    #: Title of the foreground window, for when it is not one of ``windows``.
    foreground_title: str = ""
    #: Locale facts (see :func:`yuki.perception.system.locale_facts`): Windows
    #: display language, regional-format culture, and the keyboard layout the
    #: foreground window's thread is typing with.  "" when unknown.
    ui_language: str = ""
    user_locale: str = ""
    keyboard_layout: str = ""


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

    One exception to the title and tool-window rules: a window whose owner is
    disabled is kept anyway, because that is the OS shape of a modal dialog
    holding its owner (an untitled or tool-styled "Save changes?" box is still
    the thing the user has to answer before the owner takes input again).

    Nothing here looks at the process or reads the title's content: every rule
    is a window style, a cloak state, an enabled state, a rectangle, or a system
    window class.
    """
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.GetClassName(hwnd) in _SHELL_WINDOW_CLASSES:
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
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    titled = win32gui.GetWindowText(hwnd).strip() != ""
    if titled and not ex_style & win32con.WS_EX_TOOLWINDOW:
        return True
    return _holds_owner_disabled(hwnd, ex_style)


def owner_of(hwnd: int) -> int:
    """``GetWindow(hwnd, GW_OWNER)``: the window owning ``hwnd``, or 0."""
    return int(_owner_api.GetWindow(wintypes.HWND(hwnd), _GW_OWNER) or 0)


def is_enabled(hwnd: int) -> bool:
    """``IsWindowEnabled``: False while the window refuses mouse and keyboard input."""
    return bool(_owner_api.IsWindowEnabled(wintypes.HWND(hwnd)))


def _holds_owner_disabled(hwnd: int, ex_style: int) -> bool:
    """True when ``hwnd`` can be activated and its owner is disabled.

    Windows' own dialog manager (``DialogBox``, ``MessageBox``, the common
    dialogs) disables the owner for as long as a modal dialog is up, so this is
    an OS fact, not a guess about what the window is.  A window that can never
    be activated (``WS_EX_NOACTIVATE``) cannot be that dialog.
    """
    if ex_style & _WS_EX_NOACTIVATE:
        return False
    owner = owner_of(hwnd)
    return bool(owner) and not is_enabled(owner)


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
        is_enabled=is_enabled(hwnd),
        owner_hwnd=owner_of(hwnd) or None,
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


def _monitor_dpi(handle: int) -> int:
    """Effective DPI of one monitor, or 96 when Windows will not say.

    ``GetDpiForMonitor`` lives in shcore and only answers usefully in a
    per-monitor DPI aware process; in one that is not, it reports the primary
    monitor's DPI for every display, which is exactly the lie that makes
    coordinates on a differently-scaled monitor wrong.
    """
    dpi_x = ctypes.c_uint(0)
    dpi_y = ctypes.c_uint(0)
    try:
        result = ctypes.windll.shcore.GetDpiForMonitor(
            ctypes.c_void_p(handle),
            ctypes.c_int(_MDT_EFFECTIVE_DPI),
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        )
    except Exception:  # pragma: no cover - shcore missing on very old Windows
        return _DEFAULT_DPI
    if result != 0 or not dpi_x.value:
        return _DEFAULT_DPI
    return int(dpi_x.value)


def list_monitors() -> list[MonitorInfo]:
    """Every display attached right now, in the order Windows enumerates them.

    Returns:
        One :class:`MonitorInfo` per monitor.  Never empty in practice; a session
        with no display at all (an RDP session that has dropped) returns ``[]``
        rather than raising, because an overview is still useful without it.
    """
    handles: list[int] = []

    def _collect(handle: int, _hdc: int, _rect: object, _data: int) -> int:
        handles.append(int(handle))
        return 1

    try:
        _user32.EnumDisplayMonitors(None, None, _MONITOR_ENUM_PROC(_collect), 0)
    except Exception:
        return []

    monitors: list[MonitorInfo] = []
    for index, handle in enumerate(handles):
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if not _user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
            continue
        dpi = _monitor_dpi(handle)
        monitors.append(
            MonitorInfo(
                index=index,
                name=str(info.szDevice),
                bounds=(
                    int(info.rcMonitor.left),
                    int(info.rcMonitor.top),
                    int(info.rcMonitor.right),
                    int(info.rcMonitor.bottom),
                ),
                work_area=(
                    int(info.rcWork.left),
                    int(info.rcWork.top),
                    int(info.rcWork.right),
                    int(info.rcWork.bottom),
                ),
                is_primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                dpi_scale=round(dpi / _DEFAULT_DPI, 4),
                dpi=dpi,
            )
        )
    return monitors


def monitor_at(x: int, y: int, monitors: list[MonitorInfo] | None = None) -> MonitorInfo | None:
    """The monitor a point falls on, or ``None`` when it falls off every one.

    Args:
        x, y: a point in virtual-desktop coordinates.
        monitors: a list already read by :func:`list_monitors`, to avoid asking
            Windows again; omitted means read it now.
    """
    for monitor in monitors if monitors is not None else list_monitors():
        left, top, right, bottom = monitor.bounds
        if left <= x < right and top <= y < bottom:
            return monitor
    return None


def screen_size() -> tuple[int, int]:
    """Size of the whole virtual desktop in physical pixels.

    Deliberately *not* the primary monitor's size, which is what
    ``SM_CXSCREEN``/``SM_CYSCREEN`` report and what this used to return: a caller
    asking "how big is the screen" is asking where it is allowed to point, and on
    a multi-monitor desktop the primary display's size is a smaller box than the
    truth.  :func:`virtual_screen_bounds` adds the origin.
    """
    left, top, right, bottom = virtual_screen_bounds()
    return (right - left, bottom - top)


def primary_screen_size() -> tuple[int, int]:
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
    """Snapshot every user window, every monitor, the foreground window, the cursor."""
    windows = list_windows()
    foreground = int(_user32.GetForegroundWindow() or 0)
    virtual_bounds = virtual_screen_bounds()
    left, top, right, bottom = virtual_bounds
    try:
        locale = locale_facts(foreground or None)
    except Exception:  # an overview without locale is still an overview
        locale = {}
    keyboard = locale.get("keyboard") or {}
    return DesktopOverview(
        windows=windows,
        foreground_hwnd=foreground or None,
        cursor=cursor_position(),
        screen_size=(right - left, bottom - top),
        captured_at=time.time(),
        monitors=list_monitors(),
        virtual_bounds=virtual_bounds,
        foreground_owner_hwnd=(owner_of(foreground) or None) if foreground else None,
        foreground_title=win32gui.GetWindowText(foreground) if foreground else "",
        ui_language=str(locale.get("ui_language") or ""),
        user_locale=str(locale.get("user_locale") or ""),
        keyboard_layout=str(keyboard.get("summary") or ""),
    )


def _blockers(o: DesktopOverview) -> dict[int, tuple[int, str]]:
    """Disabled listed window -> ``(hwnd, title)`` of the window it is held by.

    OS facts only: the window is disabled, and a listed window it owns is
    enabled and not minimised, or is itself disabled while owning a listed
    window (a dialog that opened a dialog), or the foreground window is owned by
    it.
    """
    listed = {w.hwnd: w for w in o.windows}
    owned: dict[int, list[WindowInfo]] = {}
    for w in o.windows:
        if w.owner_hwnd:
            owned.setdefault(w.owner_hwnd, []).append(w)
    blockers: dict[int, tuple[int, str]] = {}
    for w in o.windows:
        if w.is_enabled:
            continue
        children = owned.get(w.hwnd, [])
        pick = next((c for c in children if c.is_enabled and not c.is_minimized), None)
        if pick is None:
            pick = next((c for c in children if not c.is_enabled and c.hwnd in owned), None)
        if pick is not None:
            blockers[w.hwnd] = (pick.hwnd, pick.title)
        elif o.foreground_hwnd and o.foreground_owner_hwnd == w.hwnd:
            fg = listed.get(o.foreground_hwnd)
            blockers[w.hwnd] = (o.foreground_hwnd, fg.title if fg else o.foreground_title)
    return blockers


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

    The monitor layout is printed whenever there is more than one display,
    because without it every window rectangle outside the primary monitor looks
    like a mistake.  One line per monitor: which one it is, the rectangle it
    occupies in the same coordinates as the windows below it, its work area when
    that differs (a docked taskbar), and its scaling when it is not 100%.

    Input-blocking facts ride on the window lines: a disabled window with an
    owned window up (or owning the foreground window) reads
    ``BLOCKED by [hwnd] "title"`` and that window reads ``DIALOG for [owner]``;
    a disabled window with no such window in sight reads ``DISABLED``.  The
    header ends with the locale: Windows display language, regional formats,
    and the keyboard layout of the foreground window's thread.
    """
    width, height = o.screen_size
    v_left, v_top = o.virtual_bounds[0], o.virtual_bounds[1]
    span = "all screens" if len(o.monitors) > 1 else "one screen"
    lines = [
        f"screen space {width}x{height} @{v_left},{v_top} ({span}) | "
        f"cursor ({o.cursor[0]},{o.cursor[1]})"
    ]
    if o.foreground_hwnd and not any(w.is_foreground for w in o.windows):
        lines[0] += f" | foreground hwnd {o.foreground_hwnd} (not a user window)"
    locale = [
        part
        for part in (
            f"ui {o.ui_language}" if o.ui_language else "",
            f"formats {o.user_locale}" if o.user_locale else "",
            f"keyboard {o.keyboard_layout}" if o.keyboard_layout else "",
        )
        if part
    ]
    if locale:
        lines[0] += " | " + ", ".join(locale)
    if len(o.monitors) > 1:
        cursor_monitor = monitor_at(o.cursor[0], o.cursor[1], o.monitors)
        for monitor in o.monitors:
            left, top, right, bottom = monitor.bounds
            parts = [
                f"monitor {monitor.index}"
                f"{' PRIMARY' if monitor.is_primary else ''}:"
                f" @{left},{top} {right - left}x{bottom - top}"
            ]
            if monitor.work_area != monitor.bounds:
                w_left, w_top, w_right, w_bottom = monitor.work_area
                parts.append(
                    f"work @{w_left},{w_top} {w_right - w_left}x{w_bottom - w_top}"
                )
            if monitor.dpi_scale != 1.0:
                parts.append(f"{monitor.dpi_scale:g}x scaling")
            if cursor_monitor is not None and cursor_monitor.index == monitor.index:
                parts.append("cursor here")
            lines.append(" | ".join(parts))
    if not o.windows:
        lines.append("(no user windows)")
    blockers = _blockers(o)
    dialogs = {dialog: owner for owner, (dialog, _) in blockers.items()}
    for w in o.windows:
        left, top, right, bottom = w.bounds
        where = (
            "minimized"
            if w.is_minimized
            else f"@{left},{top} {right - left}x{bottom - top}"
        )
        flags = " FOREGROUND" if w.is_foreground else ""
        if w.hwnd in blockers:
            dialog, title = blockers[w.hwnd]
            flags += f' BLOCKED by [{dialog}] "{title}"'
        elif not w.is_enabled:
            flags += " DISABLED"
        if w.hwnd in dialogs:
            flags += f" DIALOG for [{dialogs[w.hwnd]}]"
        lines.append(f'[{w.hwnd}] {w.process_name or "unknown"} "{w.title}" {where}{flags}')
    return "\n".join(lines)
