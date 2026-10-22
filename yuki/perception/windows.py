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

The overview also names the Start menu apps that are running with no window on
screen (:func:`background_apps`), so "running in the background" can be told
from "not running": a browser whose window was closed keeps running, and an
overview of visible windows alone cannot say so.  That join goes through the
Start menu, read once in the background at import (:func:`warm_start_menu_index`).

morning21 (lane B): each monitor carries its own identity, read from Windows'
display configuration when the displays change (its EDID model name, device path,
Plug and Play id and connector), its role by place (``main``, ``second``, ...:
:func:`monitor_roles`; the words to say it by, never its index), and its screen
power as the display driver last read it (:func:`note_display_power`, never
polled).  The overview names the notification area's icons too
(:func:`tray_icons`: UI Automation's names of the tray buttons and the shell's
record of the icons of running programs; read in the background at most every
:data:`TRAY_CACHE_S`, never on the overview's own time, no screenshot).
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterable

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
    #: ``IsZoomed``: maximized (it fills its monitor's work area, title bar and taskbar still shown).
    is_maximized: bool = False
    #: Full screen: it covers its whole monitor with no caption (F11, a video, a game). Not the same as maximized.
    is_fullscreen: bool = False


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
        label: morning21 B2: the monitor's own model name from its EDID, as Windows reads it
            (``DisplayConfigGetDeviceInfo`` target name: ``VG271U``); ``""`` when it gives none.
        device_path: the monitor's device path (``\\\\?\\DISPLAY#ACR06D7#...``), its identity across
            reconnects and renumbering (``\\\\.\\DISPLAYn`` and ``index`` are not); ``""`` unread.
        pnp: its Plug and Play id from that path (``ACR06D7``: maker and product code).
        connector: how it is connected, in a word (``HDMI``, ``DisplayPort``, ``internal``); ``""`` unknown.
        role: what the user calls it by place (:func:`monitor_roles`): ``main`` (the primary), then
            ``second``, ``third``, ... left to right; never a number said to the user.
        power: its screen's power as last read over DDC/CI by the display driver
            (:func:`note_display_power`, at most :data:`POWER_FRESH_S` old): ``on``, ``off``, or ``""`` not
            known (never polled).
        handle: its ``HMONITOR`` now (valid until the next display change; the display driver's way in).
        target: mon1: the GPU target id of its port (``QueryDisplayConfig``; the "UID" of its Plug and Play
            instance), ``0`` unread: memory's monitor record keeps it, so a monitor Windows dropped can be told
            from an empty port.
    """

    index: int
    name: str
    bounds: tuple[int, int, int, int]
    work_area: tuple[int, int, int, int]
    is_primary: bool
    dpi_scale: float
    dpi: int
    label: str = ""
    device_path: str = ""
    pnp: str = ""
    connector: str = ""
    role: str = ""
    power: str = ""
    handle: int = 0
    target: int = 0


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
    #: Start menu apps that are running but have no window on screen (closed to
    #: the background or the tray) - see :func:`background_apps`.  Empty until
    #: the Start menu index has been read (a second or so after import).
    background_apps: list["BackgroundApp"] = field(default_factory=list)
    #: morning21 B7: the notification area's icons by name (:func:`tray_icons`, the
    #: last read, at most :data:`TRAY_CACHE_S` old; never read on this call).
    tray: list["TrayIcon"] = field(default_factory=list)
    #: mon1: the user's monitors memory's monitor record knows that Windows does not list now (switched fully off,
    #: unplugged), as :func:`away_monitors` says them: ``{"label", "pnp", "connector", "removed_at", "called"}``.
    monitors_away: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class TrayIcon:
    """One icon of the taskbar's notification area (morning21 B7), by name only.

    Attributes:
        name: what the icon says it is (its tooltip, as UI Automation or the
            shell's own record of it names it).
        image: its program's file name when the shell's record names it
            (``Twinkle Tray.exe``), else ``""``.
        shown: on the taskbar itself (False: among the hidden icons).
        monitor_app: its program is a monitor app by Yuki's app data
            (``yuki/connect/sites/*.toml`` ``[app] controls = "monitors"``).
    """

    name: str
    image: str = ""
    shown: bool = True
    monitor_app: bool = False


@dataclass
class BackgroundApp:
    """A Start menu app that is running with no window on screen.

    Attributes:
        name: its Start menu display name (what ``launch_app`` takes).
        appid: its Start menu AppID (an AppUserModelID or a program path).
        pids: its running processes that were seen (at most a few).
        process_name: image name of the first of them.
        hidden_hwnd: a main-style window it keeps hidden (or cloaks itself),
            when it has one; ``None`` when it runs with no such window at all.
    """

    name: str
    appid: str
    pids: list[int] = field(default_factory=list)
    process_name: str = ""
    hidden_hwnd: int | None = None


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
        is_maximized=_is_zoomed(hwnd),
        is_fullscreen=_covers_monitor_without_caption(hwnd, (left, top, right, bottom)),
    )


def _is_zoomed(hwnd: int) -> bool:
    try:
        return bool(_user32.IsZoomed(hwnd))
    except Exception:
        return False


#: The states :func:`window_state` reads (voice v3.14b).
WINDOW_STATES = ("minimized", "fullscreen", "maximized", "normal")


def window_state(hwnd: int) -> str | None:
    """How a top-level window is shown, measured from Windows: ``minimized`` (iconic), ``fullscreen`` (it covers its
    whole monitor, not only the work area, with no caption), ``maximized`` (zoomed), ``normal``; ``None`` when it
    cannot be read (gone). Cheap (a few Win32 calls): read before and after a key, so an F11 is verified in code."""
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return "minimized"
        rect = win32gui.GetWindowRect(hwnd)
        if _covers_monitor_without_caption(hwnd, rect):
            return "fullscreen"
        return "maximized" if _is_zoomed(hwnd) else "normal"
    except Exception:
        return None


def _covers_monitor_without_caption(hwnd: int, rect: tuple[int, int, int, int]) -> bool:
    """Full screen, measured: the window covers its whole monitor (not only the work area) and has no caption."""
    try:
        import win32con

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF
        if style & win32con.WS_CAPTION == win32con.WS_CAPTION:
            return False
        import win32api

        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        m_left, m_top, m_right, m_bottom = win32api.GetMonitorInfo(monitor)["Monitor"]
        left, top, right, bottom = rect
        return left <= m_left and top <= m_top and right >= m_right and bottom >= m_bottom
    except Exception:
        return False


#: Windows seen on screen as user windows (``{hwnd: last seen, monotonic}``), newest last, at most :data:`_SEEN_CAP`.
#: A hidden window that is in here was on screen before (an app closed to the tray): showing it again brings back what
#: the user had. One never seen (a tray-icon host with a caption, appOpen 5 Oct: the Riot Client's
#: "BackgroundModeTrayIconClass") is not shown by a launch. Fed in this process by :func:`list_windows` and the overview
#: (:func:`_enumerate_desktop`); across processes and restarts by the seen ledger (:data:`SEEN_FILE`).
_seen_on_screen: dict[int, float] = {}
_SEEN_CAP = 4096
_seen_lock = threading.Lock()
#: appOpen second review (V1): the seen ledger - ``{hwnd: [pid, process start]}``, content-free - written by the memory
#: service's timeline for every window that comes to the front (it follows the foreground all the time; a launch only
#: looks now and then), read by :func:`was_on_screen` in any process. A Yuki restart, or a window the user closed to
#: the tray while Yuki never listed windows, still counts as seen. Keyed with the process's start so a reused handle
#: never counts. ``None``: ``%LOCALAPPDATA%\Yuki\seen_windows.json`` (tests point it elsewhere).
SEEN_FILE: str | None = None
SEEN_LEDGER_CAP = 512
_ledger: dict[int, tuple[int, float]] = {}
_ledger_read: tuple[str, float] | None = None
#: appOpen final check (V1-ledger): what this process last wrote to the ledger for a window -
#: ``{hwnd: (pid, monotonic when written or confirmed, write count then)}`` - so a window that comes to the front again
#: is written again when its process changed (a reused handle), when it has drifted into the older half of the ledger
#: (newest last: a long-lived tray app is never the one cut at the cap), or :data:`SEEN_REFRESH_S` after it was last
#: confirmed (another writer, a restart). Never the in-process "seen" set: that one says nothing about the file.
_persisted: dict[int, tuple[int, float, int]] = {}
#: Windows written so far by this process (each one moved to the newest end counts once).
_ledger_writes = 0
#: Windows whose last write failed (the file held open by a reader): written with the next one.
_seen_pending: set[int] = set()
SEEN_REFRESH_S = 600.0
#: Tries of the atomic replace when a reader holds the file open, and the pause between them.
_REPLACE_TRIES = 3
_REPLACE_PAUSE_S = 0.02


def _seen_path() -> str:
    if SEEN_FILE:
        return SEEN_FILE
    if os.environ.get("YUKI_SEEN_FILE"):   # the test run's guard (tests/conftest.py): never the user's own file
        return os.environ["YUKI_SEEN_FILE"]
    from yuki.core.paths import data_dir

    return str(data_dir() / "seen_windows.json")


def _process_start(pid: int) -> float:
    try:
        return round(float(psutil.Process(int(pid)).create_time()), 2)
    except Exception:
        return 0.0


def _load_ledger() -> dict[int, tuple[int, float]]:
    """The ledger as last written (re-read only when the file changed); ``{}`` when there is none."""
    global _ledger, _ledger_read
    path = _seen_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _ledger_read == (path, mtime):
        return _ledger
    try:
        import json

        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        _ledger = {int(k): (int(v[0]), float(v[1])) for k, v in (raw or {}).items()}
    except Exception:
        _ledger = {}
    _ledger_read = (path, mtime)
    return _ledger


def _owner_pid(hwnd: int) -> int:
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1] or 0)
    except Exception:
        return 0


def note_on_screen(hwnds: Iterable[int], *, persist: bool = False) -> None:
    """Remember these windows as seen on screen (cheap: a dict update). ``persist`` (the timeline, one writer): also
    keep them in the seen ledger - written when a window is new to this process's writes, its process changed, it is
    no longer among the newer half of the ledger, its last write is :data:`SEEN_REFRESH_S` old, or an earlier write
    failed (appOpen final check V1-ledger)."""
    now = time.monotonic()
    handles = [int(h) for h in hwnds]
    due: list[int] = []
    with _seen_lock:
        for hwnd in handles:
            _seen_on_screen.pop(hwnd, None)
            _seen_on_screen[hwnd] = now
        while len(_seen_on_screen) > _SEEN_CAP:
            _seen_on_screen.pop(next(iter(_seen_on_screen)))
        if persist:
            for hwnd in handles:
                kept = _persisted.get(hwnd)
                if (kept is None or hwnd in _seen_pending or now - kept[1] >= SEEN_REFRESH_S
                        or _ledger_writes - kept[2] >= SEEN_LEDGER_CAP // 2 or kept[0] != _owner_pid(hwnd)):
                    due.append(hwnd)
            if due:
                due += [h for h in _seen_pending if h not in due]
    if due:
        try:
            _persist_seen(due)
        except Exception:  # noqa: BLE001 - a ledger that cannot be written costs a re-launch, never the caller
            pass


def _persist_seen(hwnds: list[int]) -> None:
    import json

    global _ledger, _ledger_read, _ledger_writes
    with _seen_lock:
        ledger = dict(_load_ledger())
        order = list(ledger)
        newer = set(order[max(0, len(order) - SEEN_LEDGER_CAP // 2):])
        confirmed: dict[int, int] = {}
        moved = 0
        for hwnd in hwnds:
            pid = _owner_pid(hwnd)
            if not pid:
                _seen_pending.discard(hwnd)
                continue
            entry = (pid, _process_start(pid))
            confirmed[hwnd] = pid
            if ledger.get(hwnd) == entry and hwnd in newer and hwnd not in _seen_pending:
                continue   # already there, among the newer half: nothing to write
            ledger.pop(hwnd, None)   # a re-seen window moves to the newest end; a changed process overwrites it
            ledger[hwnd] = entry
            moved += 1
        now = time.monotonic()
        if moved:
            if len(ledger) > SEEN_LEDGER_CAP:
                # windows that no longer exist go first; then the oldest
                for hwnd in [h for h in ledger if h not in confirmed]:
                    if len(ledger) <= SEEN_LEDGER_CAP:
                        break
                    try:
                        alive = bool(win32gui.IsWindow(hwnd))
                    except Exception:
                        alive = False
                    if not alive:
                        ledger.pop(hwnd, None)
                while len(ledger) > SEEN_LEDGER_CAP:
                    ledger.pop(next(iter(ledger)))
            path = _seen_path()
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({str(k): list(v) for k, v in ledger.items()}, fh)
                for attempt in range(_REPLACE_TRIES):
                    try:
                        os.replace(tmp, path)
                        break
                    except PermissionError:
                        # a reader has the file open for a moment (Windows refuses the replace meanwhile)
                        if attempt == _REPLACE_TRIES - 1:
                            raise
                        time.sleep(_REPLACE_PAUSE_S)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                _seen_pending.update(confirmed)   # written with the next window that comes to the front
                return
            _ledger = ledger
            try:
                _ledger_read = (path, os.path.getmtime(path))
            except OSError:
                _ledger_read = None
            _ledger_writes += moved
        for hwnd, pid in confirmed.items():
            _persisted.pop(hwnd, None)
            _persisted[hwnd] = (pid, now, _ledger_writes)
        _seen_pending.difference_update(confirmed)
        while len(_persisted) > _SEEN_CAP:
            _persisted.pop(next(iter(_persisted)))


def was_on_screen(hwnd: int) -> bool:
    """Whether this window was ever seen on screen: by this process (:func:`list_windows`, the overview), or by the
    timeline (the seen ledger, same window of the same process) - appOpen."""
    hwnd = int(hwnd)
    with _seen_lock:
        if hwnd in _seen_on_screen:
            return True
        entry = _load_ledger().get(hwnd)
    if entry is None:
        return False
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return False
    return bool(pid) and entry[0] == int(pid) and abs(entry[1] - _process_start(pid)) < 1.0


def list_windows() -> list[WindowInfo]:
    """All user windows in Z-order (topmost first)."""
    foreground = _user32.GetForegroundWindow()
    handles: list[int] = []

    def _collect(hwnd: int, _: object) -> bool:
        if is_user_window(hwnd):
            handles.append(hwnd)
        return True

    win32gui.EnumWindows(_collect, None)
    note_on_screen(handles)

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


# ---------------------------------------------------------------------------
# Main-style windows, process identity, and apps running with no window
# ---------------------------------------------------------------------------
#: ``DWM_CLOAKED_APP``: the app cloaked the window itself (it is hiding it).
#: The other cloak reasons - ``DWM_CLOAKED_SHELL`` (another virtual desktop, a
#: suspended UWP frame) and ``DWM_CLOAKED_INHERITED`` - are the shell's doing.
_DWM_CLOAKED_APP = 0x1


def cloak_state(hwnd: int) -> int:
    """The raw ``DWMWA_CLOAKED`` flags of a window (0: not cloaked, or unknown)."""
    value = ctypes.c_int(0)
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.c_uint(_DWMWA_CLOAKED),
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    return int(value.value) if result == 0 else 0


def could_be_main_window(hwnd: int) -> bool:
    """Whether a window that is not on screen is one a user could switch to.

    Window-style facts only: top level and unowned, not a tool or no-activate
    window, titled, with a caption *and* a system menu (the frame a user can
    move, switch to and close).  Helper windows an app keeps hidden (tray-icon
    hosts, message sinks, IME windows) usually lack that frame or a title.
    Cheapest checks first: it runs on every hidden top-level window of the
    desktop for each overview.
    """
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        if (style & win32con.WS_CAPTION) != win32con.WS_CAPTION or not (
            style & win32con.WS_SYSMENU
        ):
            return False
        if style & win32con.WS_CHILD:
            return False
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex_style & (win32con.WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
            return False
        if owner_of(hwnd):
            return False
        return win32gui.GetWindowText(hwnd).strip() != ""
    except Exception:
        return False


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_APPMODEL_BUFFER = 512

_proc_api = ctypes.WinDLL("kernel32")  # private: its argtypes leak nowhere
_proc_api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_proc_api.OpenProcess.restype = ctypes.c_void_p
_proc_api.CloseHandle.argtypes = [ctypes.c_void_p]
_proc_api.CloseHandle.restype = ctypes.c_int
for _fn in ("GetApplicationUserModelId", "GetPackageFamilyName"):
    getattr(_proc_api, _fn).argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
    ]
    getattr(_proc_api, _fn).restype = ctypes.c_long
_proc_api.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_wchar_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_proc_api.QueryFullProcessImageNameW.restype = ctypes.c_int


def process_package_ids(pid: int) -> tuple[str, str]:
    """``(AppUserModelID, package family name)`` of a packaged process.

    Both ``""`` for a process without package identity or one that cannot be
    opened.  Read from the process itself (``GetApplicationUserModelId``,
    ``GetPackageFamilyName``): the identity Windows gave it, whatever its image.
    """
    handle = _proc_api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return "", ""
    try:
        values = []
        for fn in (_proc_api.GetApplicationUserModelId, _proc_api.GetPackageFamilyName):
            length = ctypes.c_uint32(_APPMODEL_BUFFER)
            buffer = ctypes.create_unicode_buffer(_APPMODEL_BUFFER)
            values.append(buffer.value if fn(handle, ctypes.byref(length), buffer) == 0 else "")
        return values[0], values[1]
    finally:
        _proc_api.CloseHandle(handle)


#: pid -> ``(AppUserModelID lowercased, image path normcased)``, both ``""``
#: when unreadable.  A process's identity and image never change while it
#: lives, so each pid is asked once; pids that have exited are dropped on the
#: next :func:`background_apps` call.
_pid_facts: dict[int, tuple[str, str]] = {}
_pid_facts_lock = threading.Lock()


def _read_pid_facts(pid: int) -> tuple[str, str]:
    handle = _proc_api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return "", ""
    try:
        length = ctypes.c_uint32(_APPMODEL_BUFFER)
        buffer = ctypes.create_unicode_buffer(_APPMODEL_BUFFER)
        aumid = (
            buffer.value
            if _proc_api.GetApplicationUserModelId(handle, ctypes.byref(length), buffer) == 0
            else ""
        )
        size = ctypes.c_uint32(1024)
        path = ctypes.create_unicode_buffer(size.value)
        exe = (
            path.value
            if _proc_api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
            else ""
        )
    finally:
        _proc_api.CloseHandle(handle)
    return aumid.lower(), os.path.normcase(exe) if exe else ""


def _facts_for(pid: int) -> tuple[str, str]:
    with _pid_facts_lock:
        known = _pid_facts.get(pid)
    if known is not None:
        return known
    facts = _read_pid_facts(pid)
    with _pid_facts_lock:
        _pid_facts[pid] = facts
    return facts


@dataclass(frozen=True)
class StartEntry:
    """One Start menu entry: display name, AppID, and the program it starts."""

    name: str
    appid: str
    #: ``PKEY_Link_TargetParsingPath``: the program file a desktop entry's
    #: shortcut points at (normcased); ``""`` for packaged apps.
    target: str = ""


#: ``PKEY_Link_TargetParsingPath``.
_PKEY_LINK_TARGET = ("{B9B4B3FC-2B51-4A42-B5D8-324146AFCF25}", 2)


def _is_within(path: str, folder: str) -> bool:
    """Whether normcased ``path`` lies inside normcased ``folder``."""
    folder = folder.rstrip("\\/")
    return bool(folder) and (path == folder or path.startswith(folder + os.sep))


class _StartIndex:
    """The Start menu, indexed for "which entry does this process belong to".

    A process belongs to an entry when its AppUserModelID is the entry's AppID
    (packaged apps), when its image is the program the entry's shortcut starts,
    or when its image is installed under that program's folder (a launcher stub
    that runs the real program from a versioned subfolder).  A program started
    by several differently named entries (a shell, an installer) names none of
    them, and neither does a folder shared by several: the entry would be a
    guess.  Folders inside the Windows directory and the bare per-machine /
    per-user install roots never count as an app's folder - they hold
    everybody's programs.
    """

    def __init__(self, entries: list[StartEntry]) -> None:
        self.entries = entries
        self.by_appid = {entry.appid.lower(): entry for entry in entries}
        by_target: dict[str, dict[str, StartEntry]] = {}
        for entry in entries:
            if entry.target:
                by_target.setdefault(entry.target, {})[entry.name] = entry
        self.by_target = {
            target: next(iter(named.values()))
            for target, named in by_target.items()
            if len(named) == 1
        }
        roots = {
            os.path.normcase(os.path.abspath(path)).rstrip("\\/")
            for path in (
                os.environ.get("ProgramFiles") or "",
                os.environ.get("ProgramFiles(x86)") or "",
                os.environ.get("ProgramW6432") or "",
                os.environ.get("ProgramData") or "",
                os.environ.get("LOCALAPPDATA") or "",
                os.environ.get("APPDATA") or "",
                os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs"),
                os.environ.get("USERPROFILE") or "",
            )
            if path
        }
        windows_dir = os.path.normcase(os.environ.get("SystemRoot") or r"C:\Windows")
        by_dir: dict[str, dict[str, StartEntry]] = {}
        for target, entry in self.by_target.items():
            folder = os.path.dirname(target)
            if not folder or folder in roots or _is_within(folder, windows_dir):
                continue
            by_dir.setdefault(folder, {})[entry.name] = entry
        #: Longest folder first, so the most specific one wins.
        self.dirs = sorted(
            (
                (folder, next(iter(named.values())))
                for folder, named in by_dir.items()
                if len(named) == 1
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def entry_for(self, aumid: str, exe: str) -> StartEntry | None:
        """The entry a process with this (lowercased) AUMID and (normcased) image is."""
        if aumid:
            return self.by_appid.get(aumid)
        if not exe:
            return None
        entry = self.by_target.get(exe)
        if entry is not None:
            return entry
        for folder, entry in self.dirs:
            if _is_within(exe, folder):
                return entry
        return None


_start_index: _StartIndex | None = None
_start_index_lock = threading.Lock()
_start_index_thread: threading.Thread | None = None


def _read_start_menu() -> list[StartEntry]:
    """Every ``shell:AppsFolder`` item - the list the Start menu shows.

    Runs on its own thread with its own COM apartment (see
    :func:`warm_start_menu_index`).  Measured on this desktop: 293 entries in
    about 1.2 s, which is why it is read once, in the background, and cached.
    """
    import pythoncom
    import pywintypes
    from win32com.shell import shell, shellcon

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        folder = shell.SHCreateItemFromParsingName("shell:AppsFolder", None, shell.IID_IShellItem)
        items = folder.BindToHandler(None, shell.BHID_EnumItems, shell.IID_IEnumShellItems)
        key = (pywintypes.IID(_PKEY_LINK_TARGET[0]), _PKEY_LINK_TARGET[1])
        entries: list[StartEntry] = []
        while True:
            batch = items.Next(64)
            if not batch:
                break
            for item in batch:
                try:
                    name = item.GetDisplayName(shellcon.SIGDN_NORMALDISPLAY)
                    appid = item.GetDisplayName(shellcon.SIGDN_PARENTRELATIVEPARSING)
                except Exception:
                    continue
                target = ""
                try:
                    target = item.QueryInterface(shell.IID_IShellItem2).GetString(key) or ""
                except Exception:
                    pass
                if name and appid:
                    entries.append(
                        StartEntry(
                            name=str(name),
                            appid=str(appid),
                            target=os.path.normcase(str(target)) if target else "",
                        )
                    )
        return entries
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def warm_start_menu_index(*, refresh: bool = False) -> None:
    """Start reading the Start menu in the background (once; ``refresh`` re-reads).

    Returns at once.  Until the read has finished, :func:`start_menu_index` is
    ``None`` and :func:`background_apps` reports nothing - it never guesses
    without it.  Started at import, so it is normally ready before the first
    request is typed.
    """
    global _start_index_thread

    def build() -> None:
        global _start_index
        try:
            index = _StartIndex(_read_start_menu())
        except Exception:
            return  # no Start menu to read: background apps stay unreported
        with _start_index_lock:
            _start_index = index

    with _start_index_lock:
        running = _start_index_thread is not None and _start_index_thread.is_alive()
        if running or (_start_index is not None and not refresh):
            return
        _start_index_thread = threading.Thread(
            target=build, name="yuki-start-menu-index", daemon=True
        )
        _start_index_thread.start()


def start_menu_index(*, wait_s: float = 0.0) -> _StartIndex | None:
    """The Start menu index if it has been read, else ``None`` (and a read is started).

    ``wait_s``: wait up to this long for a read in progress (a caller that is
    about to act on the answer, not the per-turn overview).
    """
    with _start_index_lock:
        index = _start_index
        thread = _start_index_thread
    if index is None:
        warm_start_menu_index()
        if wait_s > 0:
            with _start_index_lock:
                thread = _start_index_thread
            if thread is not None:
                thread.join(wait_s)
            with _start_index_lock:
                index = _start_index
    return index


#: The console host's window class.  A hidden one belongs to a console program
#: that was started without a window of its own (a script, a build step, this
#: program's own shell) - OS plumbing, never an app the user closed.
_CONSOLE_WINDOW_CLASS = "ConsoleWindowClass"

#: At most this many background apps are reported (the line says how many more).
MAX_BACKGROUND_APPS = 10


def hidden_main_window(hwnd: int, visible: bool | None = None) -> bool:
    """A top-level window that is not on screen but has a main window's frame.

    Hidden (``IsWindowVisible`` false), or visible but cloaked by its own app
    (``DWM_CLOAKED_APP``: the app is hiding it), with the frame of
    :func:`could_be_main_window`; never the console host's window.
    """
    try:
        if visible is None:
            visible = bool(win32gui.IsWindowVisible(hwnd))
        if visible and not cloak_state(hwnd) & _DWM_CLOAKED_APP:
            return False
        if not could_be_main_window(hwnd):
            return False
        return win32gui.GetClassName(hwnd) != _CONSOLE_WINDOW_CLASS
    except Exception:
        return False


def background_apps(
    windows: list[WindowInfo],
    hidden: list[tuple[int, int]],
    *,
    others: set[int] | frozenset[int] = frozenset(),
) -> list[BackgroundApp]:
    """Start menu apps that are running with no window on screen.

    OS facts only, joined through the Start menu (see :class:`_StartIndex`):

    * a process that keeps a main-style window hidden (:func:`hidden_main_window`)
      - an app closed to the tray or the background;
    * a packaged process (it has an AppUserModelID) whose AUMID is a Start
      menu entry - a packaged app can keep running like that with no window;

    never a process whose image lies in the Windows directory (the shell, or
    ``dllhost.exe`` serving a package's shell extension: the OS, not an app);

    and no process of the same entry has a window in ``windows`` (or on another
    virtual desktop, ``others``).  Processes that belong to no Start menu entry
    - services, helpers, drivers' tray hosts - are left out, whatever windows
    they keep.  Sorted by name; at most :data:`MAX_BACKGROUND_APPS` + 1 are
    returned (the extra one tells the formatter there are more).

    Args:
        windows: the user windows on screen right now.
        hidden: ``(hwnd, pid)`` of the hidden main-style windows found in the
            same enumeration.
        others: pids owning a main-style window cloaked by the shell (on
            another virtual desktop): such an app has a window, just elsewhere.
    """
    index = start_menu_index()
    if index is None:
        return []
    pids = psutil.pids()
    live = set(pids)
    with _pid_facts_lock:
        for gone in [pid for pid in _pid_facts if pid not in live]:
            del _pid_facts[gone]

    def entry_of(pid: int) -> StartEntry | None:
        aumid, exe = _facts_for(pid)
        return index.entry_for(aumid, exe)

    shown: set[str] = set()
    for pid in {w.pid for w in windows} | set(others):
        entry = entry_of(pid)
        if entry is not None:
            shown.add(entry.appid)

    found: dict[str, BackgroundApp] = {}

    def add(entry: StartEntry, pid: int, hwnd: int | None) -> None:
        if entry.appid in shown:
            return
        app = found.get(entry.appid)
        if app is None:
            app = found[entry.appid] = BackgroundApp(name=entry.name, appid=entry.appid)
        if pid not in app.pids and len(app.pids) < 4:
            app.pids.append(pid)
        if hwnd and app.hidden_hwnd is None:
            app.hidden_hwnd = hwnd

    # A process whose image is a Windows program is part of the OS, never an
    # app running in the background: the shell (a Start entry can be a
    # shortcut that runs explorer.exe on a folder), or a host a package
    # borrowed (dllhost.exe serving its shell extension, a background-task
    # host).
    windows_dir = os.path.normcase(os.environ.get("SystemRoot") or r"C:\Windows")

    def os_program(pid: int) -> bool:
        exe = _facts_for(pid)[1]
        return bool(exe) and _is_within(exe, windows_dir)

    for hwnd, pid in hidden:
        entry = entry_of(pid)
        if entry is not None and not os_program(pid):
            add(entry, pid, hwnd)
    for pid in pids:
        aumid, _ = _facts_for(pid)
        if not aumid or (entry := index.by_appid.get(aumid)) is None or os_program(pid):
            continue
        add(entry, pid, None)
    apps = sorted(found.values(), key=lambda app: app.name.lower())[: MAX_BACKGROUND_APPS + 1]
    for app in apps:
        app.process_name = _process_name(app.pids[0]) if app.pids else ""
    return apps


# ---------------------------------------------------------------------------
# The notification area (morning21 B7): the tray's icons by name, so "the
# brightness app in my taskbar" can be matched to an app and a tray app's
# missing window is known to be by design.  Names only: UI Automation's names
# of the notification area's buttons, and the shell's own record of the icons
# of programs running now.  No screenshot, no pixel, nothing clicked.
# ---------------------------------------------------------------------------

#: A tray read counts for this long (s); an overview older than that starts a new read in the background.
TRAY_CACHE_S = 120.0
#: One tray read's UI Automation part stops by this much (s).
TRAY_READ_S = 0.6
#: At most this many icons are said.
MAX_TRAY_ICONS = 14
#: An icon's name is said up to this many characters (its tooltip's first line).
TRAY_NAME_CHARS = 40
#: UI Automation ids (``UIA_*PropertyId``, ``UIA_ButtonControlTypeId``, ``TreeScope_Descendants``).
_UIA_P_NAME, _UIA_P_AUTOMATION_ID, _UIA_P_CONTROL_TYPE = 30005, 30011, 30003
_UIA_BUTTON = 50000
_UIA_DESCENDANTS = 4
_UIA_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
#: The AutomationId Windows 11's taskbar gives a notification icon's button (shell plumbing, never an app's name).
_NOTIFY_ICON_ID = "NotifyItemIcon"
#: The window classes that hold notification icons: Windows 11's taskbar and its hidden-icons flyout.
_TRAY_XAML_HOSTS = (("Shell_TrayWnd", True), ("TopLevelWindowForOverflowXamlIsland", False))
#: Windows 10's notification area toolbars: (class path from a top-level window, shown on the taskbar).
_TRAY_TOOLBARS = ((("Shell_TrayWnd", "TrayNotifyWnd", "SysPager", "ToolbarWindow32"), True),
                  (("NotifyIconOverflowWindow", "ToolbarWindow32"), False))
#: The shell's record of every notification icon (Windows 11): its program, first tooltip, and whether it is pinned
#: to the taskbar.
_NOTIFY_SETTINGS_KEY = r"Control Panel\NotifyIconSettings"

_tray_lock = threading.Lock()
#: ``(monotonic time read, icons)``: the last read.
_tray_cache: tuple[float, list[TrayIcon]] | None = None
_tray_thread: threading.Thread | None = None


def _tray_name(text: object) -> str:
    """An icon's name as said: its tooltip's first line, spaced once, cut to :data:`TRAY_NAME_CHARS`."""
    first = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    first = " ".join(first.split())
    return first if len(first) <= TRAY_NAME_CHARS else first[: TRAY_NAME_CHARS - 1].rstrip() + "…"


def _squash_name(text: object) -> str:
    """Letters and digits only, lower case: two names compared without spaces, case or punctuation."""
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _names_alike(a: str, b: str) -> bool:
    """Two names that are one name: equal once squashed, or one inside the other when that one is 4 characters or
    more ("Twinkle Tray" in "Twinkle Tray - 2 monitors"). Spelling only, no list of apps."""
    x, y = _squash_name(a), _squash_name(b)
    if not x or not y:
        return False
    return x == y or (len(x) >= 4 and x in y) or (len(y) >= 4 and y in x)


def _child_of_class(parent: int, cls: str) -> int:
    try:
        return int(win32gui.FindWindowEx(parent, 0, cls, None) or 0)
    except Exception:
        return 0


def _tray_uia(deadline: float) -> list[tuple[str, bool]]:
    """``[(name, shown on the taskbar)]`` of the notification icons UI Automation names: Windows 11's taskbar and
    hidden-icons flyout buttons whose AutomationId marks a notification icon, and every button of Windows 10's
    notification area toolbars. One ``FindAll`` per host, names and ids only. ``[]`` when none can be read."""
    import comtypes
    import comtypes.client

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:
        pass   # already initialised for this thread
    out: list[tuple[str, bool]] = []
    try:
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        automation = comtypes.client.CreateObject(_UIA_CLSID, interface=module.IUIAutomation)
        request = automation.CreateCacheRequest()
        request.AddProperty(_UIA_P_NAME)
        request.AddProperty(_UIA_P_AUTOMATION_ID)
        buttons = automation.CreatePropertyCondition(_UIA_P_CONTROL_TYPE, _UIA_BUTTON)

        def read(hwnd: int, shown: bool, only_icons: bool) -> None:
            if not hwnd or time.monotonic() > deadline:
                return
            try:
                found = automation.ElementFromHandle(hwnd).FindAllBuildCache(_UIA_DESCENDANTS, buttons, request)
            except Exception:
                return
            for i in range(int(found.Length or 0)):
                try:
                    element = found.GetElement(i)
                    ident = str(element.GetCachedPropertyValue(_UIA_P_AUTOMATION_ID) or "")
                    name = _tray_name(element.GetCachedPropertyValue(_UIA_P_NAME))
                except Exception:
                    continue
                if name and (not only_icons or ident == _NOTIFY_ICON_ID):
                    out.append((name, shown))

        for cls, shown in _TRAY_XAML_HOSTS:
            read(int(win32gui.FindWindow(cls, None) or 0), shown, True)
        for path, shown in _TRAY_TOOLBARS:
            hwnd = int(win32gui.FindWindow(path[0], None) or 0)
            for cls in path[1:]:
                hwnd = _child_of_class(hwnd, cls) if hwnd else 0
            read(hwnd, shown, False)
    except Exception:
        pass   # what was read so far
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    return out


def _known_folder_path(path: str) -> str:
    """A path the shell writes with a known folder's id in front (``{6D809377-...}\\App\\app.exe``) as a plain path;
    any other path as it is. ``""`` when the folder can't be named."""
    path = str(path or "").strip()
    if not path.startswith("{") or "}" not in path:
        return path
    guid_text, _, rest = path.partition("}")
    try:
        from comtypes import GUID

        folder_id = GUID(guid_text + "}")
        out = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) != 0:
            return ""
        try:
            folder = str(out.value or "")
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return ""
    return os.path.join(folder, rest.lstrip("\\/")) if folder else ""


def _tray_registry() -> list[tuple[str, str, bool]]:
    """``[(tooltip, program path normcased, pinned to the taskbar)]`` from the shell's record of notification icons
    (Windows 11, ``HKCU\\Control Panel\\NotifyIconSettings``). Read-only; ``[]`` where there is no such record."""
    import winreg

    out: list[tuple[str, str, bool]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _NOTIFY_SETTINGS_KEY)
    except OSError:
        return out
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as item:
                    values: dict[str, object] = {}
                    for name in ("ExecutablePath", "InitialTooltip", "IsPromoted"):
                        try:
                            values[name] = winreg.QueryValueEx(item, name)[0]
                        except OSError:
                            values[name] = None
            except OSError:
                continue
            exe = _known_folder_path(str(values.get("ExecutablePath") or ""))
            if exe:
                out.append((_tray_name(values.get("InitialTooltip")), os.path.normcase(exe),
                            bool(values.get("IsPromoted"))))
    return out


def _monitor_apps() -> tuple[frozenset[str], tuple[str, ...]]:
    """``(program file names casefolded, display names)`` of the monitor apps Yuki's app data describes
    (``yuki/connect/sites/*.toml`` whose ``[app] controls = "monitors"``). Data only."""
    try:
        from yuki.connect.templates import site, sites
    except Exception:
        return frozenset(), ()
    images: set[str] = set()
    names: list[str] = []
    for key in sites():
        app = site(key).get("app") or {}
        if not isinstance(app, dict) or str(app.get("controls") or "") != "monitors":
            continue
        images |= {str(i).casefold() for i in app.get("images") or [] if str(i).strip()}
        if str(app.get("name") or "").strip():
            names.append(str(app["name"]))
    return frozenset(images), tuple(names)


def _read_tray() -> list[TrayIcon]:
    """One tray read: the names UI Automation gives the notification icons, joined with the shell's record of the
    icons of programs running now (their program file, and the icons among the hidden ones), in that order, each
    name once; at most :data:`MAX_TRAY_ICONS`."""
    uia = _tray_uia(time.monotonic() + TRAY_READ_S)
    try:
        record = _tray_registry()
    except Exception:
        record = []
    running: set[str] = set()
    if record:
        for pid in psutil.pids():
            exe = _facts_for(pid)[1]
            if exe:
                running.add(exe)
    live = [(tip, exe, pinned) for tip, exe, pinned in record if exe in running]
    images, app_names = _monitor_apps()
    icons: list[TrayIcon] = []
    said: set[str] = set()

    def add(name: str, image: str, shown: bool) -> None:
        key = _squash_name(name)
        if not key or key in said or len(icons) >= MAX_TRAY_ICONS:
            return
        said.add(key)
        mine = (bool(image) and image.casefold() in images) or any(_names_alike(name, n) for n in app_names)
        icons.append(TrayIcon(name=name, image=image, shown=shown, monitor_app=mine))

    for name, shown in uia:
        exe = next((e for tip, e, _p in live if tip and _names_alike(tip, name)), "")
        add(name, os.path.basename(exe), shown)
    for tip, exe, pinned in live:
        stem = os.path.splitext(os.path.basename(exe))[0]
        if any(_names_alike(tip or stem, i.name) for i in icons):
            continue
        add(tip or stem, os.path.basename(exe), pinned and not uia)
    return icons


def warm_tray() -> threading.Thread | None:
    """Start a tray read in the background (returns at once) unless one is running, or a game is in front (nothing
    is read then; the last read stands). The read's thread, or ``None``."""
    global _tray_thread
    with _tray_lock:
        if _tray_thread is not None and _tray_thread.is_alive():
            return _tray_thread
    try:
        from yuki.memory.gamegate import game_in_front

        if game_in_front():
            return None
    except Exception:
        pass   # no game check: the read is a few names, read off the game's own process

    def run() -> None:
        global _tray_cache
        try:
            icons = _read_tray()
        except Exception:
            icons = []
        with _tray_lock:
            _tray_cache = (time.monotonic(), icons)

    with _tray_lock:
        if _tray_thread is not None and _tray_thread.is_alive():
            return _tray_thread
        _tray_thread = threading.Thread(target=run, name="yuki-tray-read", daemon=True)
        _tray_thread.start()
        return _tray_thread


def tray_icons(*, wait_s: float = 0.0, max_age_s: float = TRAY_CACHE_S, refresh: bool = True) -> list[TrayIcon]:
    """The notification area's icons (:class:`TrayIcon`) from the last read when it is at most ``max_age_s`` old;
    else (``refresh``) a new read is started in the background and waited for up to ``wait_s`` (``0``: the last
    read, or ``[]``, at once). ``refresh=False``: the last read whatever its age, nothing started (a desk read
    around an act). Names in RAM only. Never raises."""
    with _tray_lock:
        hit = _tray_cache
    if refresh and (hit is None or time.monotonic() - hit[0] > max_age_s):
        thread = warm_tray()
        if thread is not None and wait_s > 0:
            thread.join(wait_s)
        with _tray_lock:
            hit = _tray_cache
    return list(hit[1]) if hit is not None else []


def format_tray(icons: Iterable[TrayIcon | dict], limit: int = MAX_TRAY_ICONS) -> str:
    """The tray in words (``""`` with none): ``Twinkle Tray (monitor app), Steam; hidden icons: OneDrive``. Takes
    :class:`TrayIcon` records or their dict form (an overview's payload)."""

    def get(icon: object, key: str, default: object) -> object:
        return icon.get(key, default) if isinstance(icon, dict) else getattr(icon, key, default)

    listed = [i for i in list(icons or [])[: max(0, int(limit))] if str(get(i, "name", "") or "").strip()]

    def word(icon: object) -> str:
        return str(get(icon, "name", "")) + (" (monitor app)" if get(icon, "monitor_app", False) else "")

    shown = [word(i) for i in listed if get(i, "shown", True)]
    hidden = [word(i) for i in listed if not get(i, "shown", True)]
    parts = [", ".join(shown)] if shown else []
    if hidden:
        parts.append("hidden icons: " + ", ".join(hidden))
    return "; ".join(parts)


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


# ---------------------------------------------------------------------------
# Monitor identity (morning21 B2): the EDID name, device path and connector of
# each display, from Windows' display configuration (QueryDisplayConfig +
# DisplayConfigGetDeviceInfo).  Read-only, no DDC/CI, about a millisecond; read
# again only when the set of displays changes (the enumeration's own stamp:
# what WM_DISPLAYCHANGE would say, with no window to receive it).
# ---------------------------------------------------------------------------

#: ``QDC_ONLY_ACTIVE_PATHS``: the displays Windows is showing now.
_QDC_ONLY_ACTIVE_PATHS = 0x2
#: ``DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME`` / ``..._GET_TARGET_NAME``.
_DCI_SOURCE_NAME = 1
_DCI_TARGET_NAME = 2
_ERROR_INSUFFICIENT_BUFFER = 122


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PathSourceInfo(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _Rational(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class _PathTargetInfo(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshRate", _Rational), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class _PathInfo(ctypes.Structure):
    """``DISPLAYCONFIG_PATH_INFO`` (72 bytes)."""

    _fields_ = [("sourceInfo", _PathSourceInfo), ("targetInfo", _PathTargetInfo), ("flags", ctypes.c_uint32)]


class _ModeInfo(ctypes.Structure):
    """``DISPLAYCONFIG_MODE_INFO`` (64 bytes; its union is never read here)."""

    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", _LUID),
                ("mode", ctypes.c_uint64 * 6)]


class _DeviceInfoHeader(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID),
                ("id", ctypes.c_uint32)]


class _SourceDeviceName(ctypes.Structure):
    """``DISPLAYCONFIG_SOURCE_DEVICE_NAME``: the source's GDI name (``\\\\.\\DISPLAY1``)."""

    _fields_ = [("header", _DeviceInfoHeader), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class _TargetDeviceName(ctypes.Structure):
    """``DISPLAYCONFIG_TARGET_DEVICE_NAME`` (420 bytes): the monitor's EDID name and device path."""

    _fields_ = [("header", _DeviceInfoHeader), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


_display_api = ctypes.WinDLL("user32")  # private: its argtypes leak nowhere
_display_api.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                                     ctypes.POINTER(ctypes.c_uint32)]
_display_api.GetDisplayConfigBufferSizes.restype = ctypes.c_long
_display_api.QueryDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                            ctypes.POINTER(_PathInfo), ctypes.POINTER(ctypes.c_uint32),
                                            ctypes.POINTER(_ModeInfo), ctypes.c_void_p]
_display_api.QueryDisplayConfig.restype = ctypes.c_long
_display_api.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
_display_api.DisplayConfigGetDeviceInfo.restype = ctypes.c_long

#: ``DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY`` values as a word (Windows' own enum, a fixed machine table).
_CONNECTOR_WORDS = {0: "VGA", 4: "DVI", 5: "HDMI", 6: "internal", 10: "DisplayPort", 11: "internal", 12: "UDI",
                    13: "internal", 15: "wireless", 16: "USB", 17: "virtual", 18: "USB-C", 0x80000000: "internal"}
#: How the monitors other than the primary are called by place, left to right (the words a person uses for them).
_ROLE_WORDS = ("second", "third", "fourth", "fifth", "sixth")


@dataclass(frozen=True)
class _Target:
    """One monitor as Windows' display configuration names it (``target_id``: its port's GPU target id)."""

    label: str
    device_path: str
    pnp: str
    connector: str
    target_id: int = 0


def _pnp_of(device_path: str) -> str:
    """The Plug and Play id in a monitor device path (``\\\\?\\DISPLAY#ACR06D7#5&...#{guid}`` -> ``ACR06D7``): the
    segment after ``DISPLAY#``, a fixed machine format."""
    parts = str(device_path or "").split("#")
    return parts[1].upper() if len(parts) > 2 and parts[0].upper().endswith("DISPLAY") else ""


#: ``GUID_DEVINTERFACE_MONITOR``: the interface class a monitor's device path ends with.
_MONITOR_INTERFACE = "{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"


def instance_of(device_path: str) -> str:
    """mon1: the Plug and Play instance part of a monitor device path (``\\\\?\\DISPLAY#AOP069C#5&19123132&0&UID155906
    #{guid}`` -> ``5&19123132&0&UID155906``): the port it is on (it changes when the cable moves), and the key a monitor
    app such as Twinkle Tray lists it by. A fixed machine format; ``""`` for anything else."""
    parts = str(device_path or "").split("#")
    return parts[2] if len(parts) > 3 and parts[0].upper().endswith("DISPLAY") else ""


def device_path_of(pnp: str, instance: str) -> str:
    """mon1: the device path a monitor of this Plug and Play id on this instance has while attached (the inverse of
    :func:`_pnp_of` / :func:`instance_of`). ``""`` without both."""
    if not pnp or not instance:
        return ""
    return f"\\\\?\\DISPLAY#{pnp}#{instance}#{_MONITOR_INTERFACE}"


def _read_display_targets() -> dict[str, list[_Target]]:
    """``{GDI device name (\\\\.\\DISPLAYn): [the monitors it shows]}`` for the active display paths (one, or more
    for a duplicated display). ``{}`` when Windows would not say. Read-only."""
    for _ in range(3):
        n_paths, n_modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
        if _display_api.GetDisplayConfigBufferSizes(_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths),
                                                    ctypes.byref(n_modes)) != 0:
            return {}
        paths = (_PathInfo * max(1, n_paths.value))()
        modes = (_ModeInfo * max(1, n_modes.value))()
        got = _display_api.QueryDisplayConfig(_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                              ctypes.byref(n_modes), modes, None)
        if got == _ERROR_INSUFFICIENT_BUFFER:
            continue   # the configuration changed between the two calls
        if got != 0:
            return {}
        break
    else:
        return {}
    out: dict[str, list[_Target]] = {}
    for i in range(n_paths.value):
        path = paths[i]
        source = _SourceDeviceName()
        source.header.type = _DCI_SOURCE_NAME
        source.header.size = ctypes.sizeof(_SourceDeviceName)
        source.header.adapterId = path.sourceInfo.adapterId
        source.header.id = path.sourceInfo.id
        if _display_api.DisplayConfigGetDeviceInfo(ctypes.byref(source)) != 0:
            continue
        target = _TargetDeviceName()
        target.header.type = _DCI_TARGET_NAME
        target.header.size = ctypes.sizeof(_TargetDeviceName)
        target.header.adapterId = path.targetInfo.adapterId
        target.header.id = path.targetInfo.id
        if _display_api.DisplayConfigGetDeviceInfo(ctypes.byref(target)) != 0:
            continue
        device_path = str(target.monitorDevicePath or "")
        out.setdefault(str(source.viewGdiDeviceName or "").upper(), []).append(_Target(
            label=" ".join(str(target.monitorFriendlyDeviceName or "").split()),
            device_path=device_path,
            pnp=_pnp_of(device_path),
            connector=_CONNECTOR_WORDS.get(int(target.outputTechnology), ""),
            target_id=int(path.targetInfo.id),
        ))
    return out


#: ``QDC_ALL_PATHS``: every path the adapters could show, active or not (an empty or dark port too).
_QDC_ALL_PATHS = 0x1
#: ``DISPLAYCONFIG_PATH_ACTIVE``.
_PATH_ACTIVE = 0x1


@dataclass(frozen=True)
class DisplayPort:
    """mon1: one GPU port (``target_id``) as Windows' display configuration holds it now: ``available`` a monitor
    answers on it (attached, lit or in standby), ``active`` Windows shows a desktop on it, the monitor's
    ``device_path`` and ``label`` when one is named there, its ``connector`` word."""

    target_id: int
    available: bool
    active: bool
    device_path: str = ""
    label: str = ""
    connector: str = ""


def display_ports() -> dict[int, DisplayPort]:
    """mon1: ``{GPU target id: DisplayPort}`` from ``QueryDisplayConfig(QDC_ALL_PATHS)``: every port, so a monitor that
    left Windows (its port kept, ``available`` False) is told from one Windows keeps but shows nothing on
    (``available`` True, ``active`` False). Read-only (no SetDisplayConfig), about a millisecond, never cached (a
    driver run reads it once). ``{}`` when Windows would not say. Never raises."""
    try:
        for _ in range(3):
            n_paths, n_modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
            if _display_api.GetDisplayConfigBufferSizes(_QDC_ALL_PATHS, ctypes.byref(n_paths),
                                                        ctypes.byref(n_modes)) != 0:
                return {}
            paths = (_PathInfo * max(1, n_paths.value))()
            modes = (_ModeInfo * max(1, n_modes.value))()
            got = _display_api.QueryDisplayConfig(_QDC_ALL_PATHS, ctypes.byref(n_paths), paths,
                                                  ctypes.byref(n_modes), modes, None)
            if got == _ERROR_INSUFFICIENT_BUFFER:
                continue
            if got != 0:
                return {}
            break
        else:
            return {}
        out: dict[int, DisplayPort] = {}
        for i in range(n_paths.value):
            path = paths[i]
            tid = int(path.targetInfo.id)
            seen = out.get(tid)
            available = bool(path.targetInfo.targetAvailable) or bool(seen and seen.available)
            active = bool(path.flags & _PATH_ACTIVE) or bool(seen and seen.active)
            device_path, label = (seen.device_path, seen.label) if seen is not None else ("", "")
            if not device_path and path.targetInfo.targetAvailable:
                target = _TargetDeviceName()
                target.header.type = _DCI_TARGET_NAME
                target.header.size = ctypes.sizeof(_TargetDeviceName)
                target.header.adapterId = path.targetInfo.adapterId
                target.header.id = path.targetInfo.id
                if _display_api.DisplayConfigGetDeviceInfo(ctypes.byref(target)) == 0:
                    device_path = str(target.monitorDevicePath or "")
                    label = " ".join(str(target.monitorFriendlyDeviceName or "").split())
            out[tid] = DisplayPort(target_id=tid, available=available, active=active, device_path=device_path,
                                   label=label,
                                   connector=_CONNECTOR_WORDS.get(int(path.targetInfo.outputTechnology), ""))
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# mon1: the monitors Windows keeps a device for, attached now or not (SetupAPI,
# non-present devices included) with their EDID from the registry. Read-only,
# no admin, no setup; read at the memory service's start (names and ports for
# memory's monitor record: never a sighting) and for one monitor's EDID when it
# is first seen attached.
# ---------------------------------------------------------------------------

_GUID_DEVCLASS_MONITOR = "{4d36e96e-e325-11ce-bfc1-08002be10318}"
#: ``DEVPKEY_Device_LastArrivalDate`` / ``..._LastRemovalDate`` / ``DEVPKEY_Device_IsPresent`` (devpkey.h).
_DEVPKEY_ARRIVAL = ("{83da6326-97a6-4088-9453-a1923f573b29}", 102)
_DEVPKEY_REMOVAL = ("{83da6326-97a6-4088-9453-a1923f573b29}", 103)
_DEVPKEY_PRESENT = ("{540b947e-8b40-45bc-a8a2-6a0b894cbda2}", 5)
_DEVPROP_TYPE_FILETIME = 0x10
_DEVPROP_TYPE_BOOLEAN = 0x11
#: FILETIME's epoch (1601) to the Unix one, in seconds.
_FILETIME_EPOCH_S = 11_644_473_600
_EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


class _SpDevInfoData(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t)]


def _guid(text: str) -> _GUID:
    import uuid

    u = uuid.UUID(text)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


_setupapi_lib: object = None


def _setupapi() -> object:
    """setupapi with its signatures (private), or ``None`` where it can't be loaded."""
    global _setupapi_lib
    if _setupapi_lib is None:
        try:
            lib = ctypes.WinDLL("setupapi")
            lib.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND,
                                                 wintypes.DWORD]
            lib.SetupDiGetClassDevsW.restype = ctypes.c_void_p
            lib.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SpDevInfoData)]
            lib.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
            lib.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SpDevInfoData),
                                                        wintypes.LPWSTR, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD)]
            lib.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
            lib.SetupDiGetDevicePropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SpDevInfoData),
                                                      ctypes.POINTER(_DevPropKey), ctypes.POINTER(wintypes.ULONG),
                                                      ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
            lib.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
            lib.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
            lib.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
            _setupapi_lib = lib
        except Exception:
            _setupapi_lib = False
    return _setupapi_lib or None


def _dev_prop(lib: object, info: object, data: _SpDevInfoData, key: tuple[str, int]) -> tuple[int, bytes] | None:
    """One device property's ``(type, raw bytes)``, or ``None``."""
    pk = _DevPropKey()
    pk.fmtid, pk.pid = _guid(key[0]), key[1]
    kind, need = wintypes.ULONG(0), wintypes.DWORD(0)
    buf = (ctypes.c_ubyte * 64)()
    if not lib.SetupDiGetDevicePropertyW(info, ctypes.byref(data), ctypes.byref(pk), ctypes.byref(kind), buf,
                                         ctypes.sizeof(buf), ctypes.byref(need), 0):
        return None
    return int(kind.value), bytes(buf[: min(int(need.value), ctypes.sizeof(buf))])


def _filetime_s(raw: tuple[int, bytes] | None) -> float | None:
    if raw is None or raw[0] != _DEVPROP_TYPE_FILETIME or len(raw[1]) < 8:
        return None
    ticks = int.from_bytes(raw[1][:8], "little")
    return ticks / 10_000_000 - _FILETIME_EPOCH_S if ticks else None


def edid_facts(edid: bytes) -> tuple[str, str, int, int]:
    """mon1: ``(name, serial, width, height)`` from an EDID block (VESA E-EDID: the 0xFC name and 0xFF serial
    descriptors, else the numeric serial at bytes 12-15; the first detailed timing is the native mode). A fixed
    machine format; ``("", "", 0, 0)`` for a block that isn't one."""
    data = bytes(edid or b"")
    if len(data) < 128 or data[:8] != _EDID_HEADER:
        return "", "", 0, 0
    name = text_serial = ""
    width = height = 0
    for off in (54, 72, 90, 108):
        d = data[off:off + 18]
        if d[0] or d[1]:
            if not width:
                width = d[2] | ((d[4] & 0xF0) << 4)
                height = d[5] | ((d[7] & 0xF0) << 4)
            continue
        text = d[5:18].split(b"\n")[0].decode("ascii", "ignore").strip()
        if d[3] == 0xFC and not name:
            name = text
        elif d[3] == 0xFF and not text_serial:
            text_serial = text
    number = int.from_bytes(data[12:16], "little")
    return " ".join(name.split()), text_serial or (str(number) if number else ""), int(width), int(height)


_edid_cache: dict[str, tuple[str, str, int, int]] = {}
_edid_lock = threading.Lock()


def monitor_edid(pnp: str, instance: str) -> tuple[str, str, int, int]:
    """mon1: ``(name, serial, width, height)`` of the monitor Windows keeps as ``DISPLAY\\<pnp>\\<instance>``, from its
    EDID in the registry (readable without admin, for a device not attached now too), cached per device. ``("", "",
    0, 0)`` when none. Never raises."""
    if not pnp or not instance:
        return "", "", 0, 0
    key = f"DISPLAY\\{pnp}\\{instance}".upper()
    with _edid_lock:
        hit = _edid_cache.get(key)
    if hit is not None:
        return hit
    got = ("", "", 0, 0)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            f"SYSTEM\\CurrentControlSet\\Enum\\{key}\\Device Parameters") as reg:
            value, _kind = winreg.QueryValueEx(reg, "EDID")
        got = edid_facts(bytes(value or b""))
    except Exception:
        got = ("", "", 0, 0)
    with _edid_lock:
        _edid_cache[key] = got
    return got


@dataclass(frozen=True)
class MonitorDevice:
    """mon1: a monitor Windows keeps a device for: ``pnp`` (``AOP069C``), ``instance`` (the port's instance key,
    ``5&19123132&0&UID155906``), the EDID ``label``, ``serial``, native ``width`` x ``height``, whether it is
    ``present`` now (``None`` unread), and when it last arrived and was last removed (epoch seconds, ``None``
    unread)."""

    pnp: str
    instance: str
    label: str = ""
    serial: str = ""
    width: int = 0
    height: int = 0
    present: bool | None = None
    arrived_at: float | None = None
    removed_at: float | None = None


def monitor_devices() -> list[MonitorDevice]:
    """mon1: every monitor device Windows keeps (SetupAPI, not-present ones included: a monitor switched fully off or
    unplugged keeps its device, its EDID and its last arrival and removal times), each with its EDID facts. Read-only,
    no admin; tens of milliseconds, so read rarely (the memory service's start). ``[]`` when it can't be read. Never
    raises."""
    lib = _setupapi()
    if lib is None:
        return []
    out: list[MonitorDevice] = []
    info = None
    try:
        cls_guid = _guid(_GUID_DEVCLASS_MONITOR)
        info = lib.SetupDiGetClassDevsW(ctypes.byref(cls_guid), None, None, 0)   # 0: present and not present
        if not info or info == ctypes.c_void_p(-1).value:
            return []
        index = 0
        while index < 256:
            data = _SpDevInfoData()
            data.cbSize = ctypes.sizeof(_SpDevInfoData)
            if not lib.SetupDiEnumDeviceInfo(info, index, ctypes.byref(data)):
                break
            index += 1
            buf = ctypes.create_unicode_buffer(512)
            if not lib.SetupDiGetDeviceInstanceIdW(info, ctypes.byref(data), buf, 512, None):
                continue
            parts = str(buf.value or "").split("\\")
            if len(parts) != 3 or parts[0].upper() != "DISPLAY":
                continue
            pnp, instance = parts[1].upper(), parts[2]
            present_raw = _dev_prop(lib, info, data, _DEVPKEY_PRESENT)
            present = (bool(present_raw[1][0]) if present_raw and present_raw[0] == _DEVPROP_TYPE_BOOLEAN
                       and present_raw[1] else None)
            label, serial, width, height = monitor_edid(pnp, instance)
            out.append(MonitorDevice(pnp=pnp, instance=instance, label=label, serial=serial, width=width,
                                     height=height, present=present,
                                     arrived_at=_filetime_s(_dev_prop(lib, info, data, _DEVPKEY_ARRIVAL)),
                                     removed_at=_filetime_s(_dev_prop(lib, info, data, _DEVPKEY_REMOVAL))))
    except Exception:
        return out
    finally:
        if info and info != ctypes.c_void_p(-1).value:
            try:
                lib.SetupDiDestroyDeviceInfoList(info)
            except Exception:
                pass
    return out


_targets_lock = threading.Lock()
#: ``(stamp, targets)``: the last read, kept until the displays' own stamp changes.
_targets_cache: tuple[tuple, dict[str, list[_Target]]] | None = None


def _display_targets(stamp: tuple) -> dict[str, list[_Target]]:
    """:func:`_read_display_targets`, read again only when ``stamp`` (every display's GDI name and rectangle) is not
    the last read's. Never raises (``{}``)."""
    global _targets_cache
    with _targets_lock:
        hit = _targets_cache
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        got = _read_display_targets()
    except Exception:
        got = {}
    with _targets_lock:
        _targets_cache = (stamp, got)
    return got


def monitor_roles(monitors: Iterable[MonitorInfo]) -> dict[int, str]:
    """``{index: role}``: ``main`` for the primary display, then ``second``, ``third``, ... left to right (top to
    bottom when two share a left edge): the place a person names a monitor by. Pure geometry."""
    listed = list(monitors)
    out: dict[int, str] = {}
    rest = sorted((m for m in listed if not m.is_primary), key=lambda m: (m.bounds[0], m.bounds[1]))
    for m in listed:
        if m.is_primary:
            out[m.index] = "main"
    if not out and listed:
        first = min(listed, key=lambda m: (m.bounds[0], m.bounds[1]))
        out[first.index] = "main"
        rest = [m for m in rest if m.index != first.index]
    for i, m in enumerate(rest):
        out[m.index] = _ROLE_WORDS[i] if i < len(_ROLE_WORDS) else f"monitor {i + 2}"
    return out


#: A screen power state read over DDC/CI counts for this long (s): the user may change it with the monitor's own
#: buttons or another app at any time, and nothing here polls the monitor.
POWER_FRESH_S = 120.0
_power_lock = threading.Lock()
#: ``{device path: (VCP 0xD6 value, wall time read)}``: written by the display driver's own reads
#: (:func:`note_display_power`), never by polling.
_power: dict[str, tuple[int, float]] = {}
#: ``{device path: (label, pnp)}``: every monitor this process saw attached, so a monitor Windows dropped (switched
#: fully off) can still be named.
_seen_monitors: dict[str, tuple[str, str]] = {}
#: mon1: ``{device path: wall time}`` when this process's last read no longer listed a monitor it listed before (RAM;
#: memory's monitor record keeps the lasting one).
_gone_at: dict[str, float] = {}
#: mon1: the device paths the last :func:`list_monitors` listed.
_attached_last: set[str] = set()


def note_display_power(device_path: str, value: int | None) -> None:
    """Keep a monitor's screen power as the display driver just read it (VCP 0xD6: 1 on, 2-5 standby or off;
    ``None`` forgets it). RAM only."""
    key = str(device_path or "")
    if not key:
        return
    with _power_lock:
        if value is None:
            _power.pop(key, None)
        else:
            _power[key] = (int(value), time.time())


def display_power(device_path: str, *, fresh_s: float = POWER_FRESH_S) -> str:
    """``on`` / ``off`` from the last DDC/CI read of this monitor's power, when it is at most ``fresh_s`` old; ``""``
    otherwise (not known: never a guess)."""
    with _power_lock:
        hit = _power.get(str(device_path or ""))
    if hit is None or time.time() - hit[1] > fresh_s:
        return ""
    return "on" if hit[0] == 1 else "off" if 2 <= hit[0] <= 5 else ""


def seen_monitors() -> dict[str, tuple[str, str]]:
    """``{device path: (label, pnp)}`` of every monitor this process saw attached, and (mon1) every one memory's
    monitor record knows as the user's (``yuki.memory.monitors``: so a fresh process names a monitor that was
    switched fully off before it started), by the device path it had on its last port. A copy; never raises."""
    with _power_lock:
        out = dict(_seen_monitors)
    try:
        from yuki.memory import monitors as record

        for row in record.known():
            path = device_path_of(row.pnp, row.instance)
            if path and path not in out and (row.label or row.pnp):
                out[path] = (row.label, row.pnp)
    except Exception:
        pass
    return out


def gone_at(device_path: str) -> float | None:
    """mon1: when this process's reads stopped listing the monitor (wall time), or ``None`` (listed now, or never
    seen leave)."""
    with _power_lock:
        return _gone_at.get(str(device_path or ""))


def away_monitors(listed: Iterable[MonitorInfo] | None = None) -> list[dict]:
    """mon1: the user's monitors memory's monitor record knows that Windows does not list now (``listed``: a
    :func:`list_monitors` already read), as the desk says them: ``{"label", "pnp", "connector", "removed_at",
    "called"}`` (``called``: the user's own words for it, learned; ``removed_at``: the record's, else when this
    process saw it leave). ``[]`` without memory. Never raises."""
    try:
        from yuki.memory import monitors as record

        attached = list(listed) if listed is not None else list_monitors()
        out = []
        for row in record.away([m.device_path for m in attached if m.device_path],
                               [(m.pnp, m.label) for m in attached]):
            seen = gone_at(device_path_of(row.pnp, row.instance))
            removed = max(x for x in (row.removed_at, seen, 0.0) if x is not None) or None
            out.append({"label": row.label, "pnp": row.pnp, "connector": row.connector, "removed_at": removed,
                        "called": list(record.words(row, heard=False))})
        return out
    except Exception:
        return []


def list_monitors() -> list[MonitorInfo]:
    """Every display attached right now, in the order Windows enumerates them.

    morning21 B2: each carries its monitor's EDID name, device path, Plug and Play id and connector (Windows' display
    configuration, read again only when the displays change), its role by place (:func:`monitor_roles`), its screen
    power as last read by the display driver (:func:`display_power`), and its ``HMONITOR``.

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
                handle=int(handle),
            )
        )
    try:
        targets = _display_targets(tuple((m.name, m.bounds) for m in monitors))
        roles = monitor_roles(monitors) if len(monitors) > 1 else {m.index: "main" for m in monitors}
        attached: set[str] = set()
        for m in monitors:
            m.role = roles.get(m.index, "")
            shown = targets.get(m.name.upper()) or []
            if not shown:
                continue
            first = shown[0]
            m.label, m.device_path, m.pnp, m.connector = first.label, first.device_path, first.pnp, first.connector
            m.target = first.target_id
            m.power = display_power(m.device_path)
            attached.add(m.device_path)
            with _power_lock:
                _seen_monitors[m.device_path] = (m.label, m.pnp)
        now = time.time()
        with _power_lock:
            for gone in [k for k in _power if k not in attached]:
                del _power[gone]   # a monitor Windows dropped: its last read says nothing now
            # mon1: when a monitor this process listed stops being listed (RAM; the record keeps the lasting one)
            for left in _attached_last - attached:
                _gone_at[left] = now
            for back in attached:
                _gone_at.pop(back, None)
            _attached_last.clear()
            _attached_last.update(attached)
    except Exception:
        pass   # identity is extra: the displays themselves are still reported
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


def _enumerate_desktop() -> tuple[list[int], list[tuple[int, int]], set[int]]:
    """One ``EnumWindows`` pass: user windows, hidden main windows, windows elsewhere.

    Returns:
        ``(user_handles, hidden, elsewhere_pids)``: the handles
        :func:`is_user_window` accepts, in Z-order; ``(hwnd, pid)`` of every
        :func:`hidden_main_window`; and the pids owning a main-style window that
        the shell has cloaked (one on another virtual desktop).
    """
    user: list[int] = []
    hidden: list[tuple[int, int]] = []
    elsewhere: set[int] = set()

    def _collect(hwnd: int, _: object) -> bool:
        try:
            visible = bool(win32gui.IsWindowVisible(hwnd))
            if visible and is_user_window(hwnd):
                user.append(hwnd)
            elif hidden_main_window(hwnd, visible):
                hidden.append((hwnd, int(win32process.GetWindowThreadProcessId(hwnd)[1])))
            elif visible and cloak_state(hwnd) and could_be_main_window(hwnd):
                elsewhere.add(int(win32process.GetWindowThreadProcessId(hwnd)[1]))
        except Exception:
            pass  # the window died mid-enumeration
        return True

    win32gui.EnumWindows(_collect, None)
    note_on_screen(user)   # appOpen second review (V1): the overview sees what is on screen too
    return user, hidden, elsewhere


def get_desktop_overview() -> DesktopOverview:
    """Snapshot every user window, every monitor, the foreground window, the cursor,
    and the Start menu apps running with no window on screen."""
    foreground = int(_user32.GetForegroundWindow() or 0)
    handles, hidden, elsewhere = _enumerate_desktop()
    windows: list[WindowInfo] = []
    for hwnd in handles:
        try:
            windows.append(_window_info(hwnd, foreground))
        except Exception:
            continue  # the window died between enumeration and inspection
    try:
        background = background_apps(windows, hidden, others=elsewhere)
    except Exception:  # an overview without them is still an overview
        background = []
    try:
        tray = tray_icons()   # morning21 B7: the last read (a stale one starts a new read in the background)
    except Exception:
        tray = []
    virtual_bounds = virtual_screen_bounds()
    left, top, right, bottom = virtual_bounds
    try:
        locale = locale_facts(foreground or None)
    except Exception:  # an overview without locale is still an overview
        locale = {}
    keyboard = locale.get("keyboard") or {}
    monitors = list_monitors()
    return DesktopOverview(
        windows=windows,
        foreground_hwnd=foreground or None,
        cursor=cursor_position(),
        screen_size=(right - left, bottom - top),
        captured_at=time.time(),
        monitors=monitors,
        monitors_away=away_monitors(monitors),
        virtual_bounds=virtual_bounds,
        foreground_owner_hwnd=(owner_of(foreground) or None) if foreground else None,
        foreground_title=win32gui.GetWindowText(foreground) if foreground else "",
        ui_language=str(locale.get("ui_language") or ""),
        user_locale=str(locale.get("user_locale") or ""),
        keyboard_layout=str(keyboard.get("summary") or ""),
        background_apps=background,
        tray=tray,
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

    Last, one line naming the Start menu apps that are running with no window
    on screen (``running with no open window: Arc, WhatsApp``), so "running in
    the background" can be told from "not running"; left out when there are
    none (or the Start menu has not been read yet).
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
            # morning21 B3: its role and its own model name, the words a person uses for it (never the index)
            named = ", ".join(x for x in (getattr(monitor, "role", ""), getattr(monitor, "label", "")) if x)
            parts = [
                f"monitor {monitor.index}"
                f"{' PRIMARY' if monitor.is_primary else ''}"
                f"{f' ({named})' if named else ''}:"
                f" @{left},{top} {right - left}x{bottom - top}"
            ]
            if getattr(monitor, "power", "") == "off":
                parts.append("screen off")
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
        if not w.is_minimized and getattr(w, "is_fullscreen", False):
            flags += " FULL SCREEN"
        elif not w.is_minimized and getattr(w, "is_maximized", False):
            flags += " MAXIMIZED (not full screen)"
        if w.hwnd in blockers:
            dialog, title = blockers[w.hwnd]
            flags += f' BLOCKED by [{dialog}] "{title}"'
        elif not w.is_enabled:
            flags += " DISABLED"
        if w.hwnd in dialogs:
            flags += f" DIALOG for [{dialogs[w.hwnd]}]"
        lines.append(f'[{w.hwnd}] {w.process_name or "unknown"} "{w.title}" {where}{flags}')
    background = list(getattr(o, "background_apps", None) or [])
    if background:
        names = [app.name for app in background[:MAX_BACKGROUND_APPS]]
        more = len(background) > len(names)
        lines.append(
            "running with no open window: " + ", ".join(names) + (" and more" if more else "")
        )
    tray = format_tray(getattr(o, "tray", None) or [])
    if tray:
        lines.append("in the tray: " + tray)
    return "\n".join(lines)


# Read the Start menu in the background now, so the first overview can already
# say which apps run with no window (the read takes about a second).
warm_start_menu_index()
