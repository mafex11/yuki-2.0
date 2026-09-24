"""The memory watcher: what is on screen, recorded as text deltas.

Contract: ``docs/MEMORY.md`` -> "Watcher (capture)".

Two threads:

* the **hook thread** owns a hidden top-level window and a Win32 message loop.
  It registers ``SetWinEventHook`` (out of context) for
  ``EVENT_SYSTEM_FOREGROUND`` system-wide and, for the foreground window's
  process only, ``EVENT_OBJECT_NAMECHANGE`` and ``EVENT_OBJECT_FOCUS`` (re-hooked
  on every foreground change, so the rest of the desktop costs nothing).  It
  also receives ``WM_WTSSESSION_CHANGE`` (lock/unlock).  Events are coalesced:
  a burst becomes one capture once the foreground window has *settled* - no
  hooked event from it and the same title and rectangle for
  :attr:`WatcherSettings.settle_s`, capped at :attr:`WatcherSettings.settle_cap_s`
  after the first event.  Waiting is ``MsgWaitForMultipleObjectsEx`` with the
  next deadline as its timeout: it wakes on the next event or the deadline,
  never on a fixed sleep.  A slow adaptive **backstop** re-reads the foreground
  window every 45-90 s while the user is active (``GetLastInputInfo``), and
  never while they are idle.
* the **capture worker** does the reading (UIA through
  :mod:`yuki.perception`), privacy gating (:mod:`yuki.memory.privacy`), the
  delta against the thread's latest text and the store writes, one capture at a
  time, latest request wins.

Nothing here sends input, changes focus, launches anything or takes pixels:
the watcher only listens to window events and reads the foreground window's
UIA text.  Logs are content-free: apps, triggers, outcomes, sizes and timings.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psutil
import win32api
import win32con
import win32gui

from yuki.memory.privacy import PrivacyConfig, PrivacyRules
from yuki.perception.tree import (
    UIElement,
    get_window_tree,
    is_address,
    page_identity,
    page_text,
)
from yuki.perception.windows import is_user_window

# ---------------------------------------------------------------------------
# Store protocol (implemented by yuki.memory.store.Store)
# ---------------------------------------------------------------------------


class CaptureStore(Protocol):
    def upsert_thread(self, app: str, title: str, url: str | None, *, process: str | None = None) -> int: ...
    def latest_text(self, thread_id: int) -> str | None: ...
    def add_capture(
        self, thread_id: int, at: float, trigger: str, full_text: str, delta_text: str
    ) -> int | None: ...
    def add_health(
        self, at: float, app: str, trigger: str, outcome: str, reason: str, chars: int, ms: float
    ) -> None: ...


LogFn = Callable[..., Any]

# ---------------------------------------------------------------------------
# Tuning (plumbing budgets, not behaviour)
# ---------------------------------------------------------------------------


@dataclass
class WatcherSettings:
    #: The foreground window has settled when nothing moved for this long.
    settle_s: float = 0.4
    #: ...or this long after the first event of the burst, whichever is first.
    settle_cap_s: float = 2.0
    #: Backstop re-read interval while the user is active: starts at the
    #: minimum, grows x1.5 after each backstop that found nothing new.
    backstop_min_s: float = 45.0
    backstop_max_s: float = 90.0
    #: No backstop reads once the last keyboard/mouse input is older than this.
    idle_limit_s: float = 120.0
    #: Wall-clock budget for reading one window (page text, else tree).
    capture_budget_s: float = 0.6
    #: Floor for the tree read when the page probe used the budget up.
    tree_floor_s: float = 0.3
    #: Characters kept per snapshot; the rest is dropped and noted.
    max_chars: int = 20000
    #: Elements read from a window without a page.
    max_elements: int = 400
    #: Minimum gap per window between captures from one trigger kind, so a
    #: title that ticks every second or a user tabbing through fields cannot
    #: turn into a capture per second.  The capture is delayed, not dropped.
    min_gap_s: dict[str, float] = field(default_factory=lambda: {"title": 5.0, "focus": 10.0})
    #: UIA reads still running from earlier timed-out captures; above this the
    #: next capture is skipped instead of piling up threads on a hung provider.
    uia_backlog: int = 3
    #: Edit fields whose IsPassword is checked per tree capture.
    password_checks: int = 12
    #: Latency samples kept for the stats.
    latency_window: int = 2000


#: Class name of the watcher's hidden window.  The tray app stops the service
#: with ``PostMessage(FindWindow(WINDOW_CLASS, None), WM_CLOSE)``.
WINDOW_CLASS = "YukiMemoryWatcher"

TRIGGER_RANK = {"backstop": 0, "focus": 1, "title": 2, "resume": 3, "foreground": 3, "start": 3}

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32")
_wtsapi32 = ctypes.WinDLL("wtsapi32")

_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)
_user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    _WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.UnhookWinEvent.restype = wintypes.BOOL
_user32.MsgWaitForMultipleObjectsEx.argtypes = [
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
_user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
_user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.PeekMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = ctypes.c_ssize_t
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_kernel32.GetTickCount.restype = wintypes.DWORD
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = ctypes.c_long
_wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
_wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
_wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD),
]
_wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0
GA_ROOT = 2
QS_ALLINPUT = 0x04FF
MWMO_INPUTAVAILABLE = 0x0004
PM_REMOVE = 0x0001
WM_QUIT = 0x0012
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
WM_APP_STOP = win32con.WM_APP + 1
WM_APP_REFRESH = win32con.WM_APP + 2
WM_APP_PAUSE = win32con.WM_APP + 3
MONITOR_DEFAULTTONEAREST = 2
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_SESSION_INFO_EX = 25
_WTS_SESSIONSTATE_LOCK = 0
INFINITE = 0xFFFFFFFF


def _window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def user_idle_s() -> float:
    """Seconds since the last keyboard/mouse input in this session."""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def notification_state() -> int:
    """``SHQueryUserNotificationState`` (QUNS_*), 0 when unavailable."""
    state = ctypes.c_int(0)
    if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return 0
    return state.value


def is_fullscreen_front(hwnd: int) -> bool:
    """Whether ``hwnd`` is a full-screen window in front (a game, F11 video).

    Direct3D exclusive full screen is reported by the shell as such.  Otherwise
    the window must cover its whole monitor without a caption, and the shell
    must report a full-screen app (QUNS_BUSY / presentation mode) or the window
    must be a user window (the desktop covers its monitor too, and is not one).
    """
    state = notification_state()
    if state == QUNS_RUNNING_D3D_FULL_SCREEN:
        return True
    if not hwnd:
        return False
    style = _user32.GetWindowLongW(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF
    if style & win32con.WS_CAPTION == win32con.WS_CAPTION:
        return False
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    if not monitor or not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    left, top, right, bottom = _window_rect(hwnd)
    m = info.rcMonitor
    covers = left <= m.left and top <= m.top and right >= m.right and bottom >= m.bottom
    if not covers:
        return False
    return state in (QUNS_BUSY, QUNS_PRESENTATION_MODE) or is_user_window(hwnd)


def session_locked() -> bool:
    """Whether this session is locked right now (WTSSessionInfoEx flags)."""
    buf = ctypes.c_void_p()
    size = wintypes.DWORD(0)
    ok = _wtsapi32.WTSQuerySessionInformationW(
        None, _WTS_CURRENT_SESSION, _WTS_SESSION_INFO_EX, ctypes.byref(buf), ctypes.byref(size)
    )
    if not ok or not buf.value:
        return False
    try:
        # WTSINFOEXW { DWORD Level; union (8-aligned) { WTSINFOEX_LEVEL1_W {
        #   ULONG SessionId; WTS_CONNECTSTATE_CLASS SessionState; LONG SessionFlags; ... } } }
        if size.value < 20 or ctypes.c_uint32.from_address(buf.value).value != 1:
            return False
        return ctypes.c_int32.from_address(buf.value + 16).value == _WTS_SESSIONSTATE_LOCK
    finally:
        _wtsapi32.WTSFreeMemory(buf)


# ---------------------------------------------------------------------------
# Process facts (cached per pid)
# ---------------------------------------------------------------------------

_VENV_ROOT = Path(sys.prefix).resolve()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProcessFacts:
    pid: int
    process_name: str  # "chrome.exe"
    exe: str
    app_name: str  # FileDescription ("Google Chrome"), else the image name's stem
    is_own: bool  # a Yuki process: this one, or one run by Yuki's interpreter


def _file_description(exe: str) -> str:
    try:
        pairs = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")
        for lang, codepage in pairs or []:
            key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            value = win32api.GetFileVersionInfo(exe, key)
            if value and str(value).strip():
                return str(value).strip()
    except Exception:
        pass
    return ""


def _under(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


class _ProcessCache:
    def __init__(self) -> None:
        self._facts: dict[int, tuple[float, ProcessFacts]] = {}
        self._descriptions: dict[str, str] = {}

    def get(self, pid: int) -> ProcessFacts:
        cached = self._facts.get(pid)
        try:
            proc = psutil.Process(pid)
            created = proc.create_time()
        except (psutil.Error, OSError):
            return ProcessFacts(pid, "", "", "", False)
        if cached and cached[0] == created:
            return cached[1]
        name, exe, own = "", "", pid == os.getpid()
        try:
            name = proc.name()
            exe = proc.exe()
        except (psutil.Error, OSError):
            pass
        if not own:
            # Yuki's interpreter: the venv launcher starts the base python as a
            # child, so the parent chain is checked too (two levels).
            chain = [exe]
            try:
                parent = proc.parent()
                for _ in range(2):
                    if parent is None:
                        break
                    chain.append(parent.exe())
                    parent = parent.parent()
            except (psutil.Error, OSError):
                pass
            own = any(p and (_under(p, _VENV_ROOT) or _under(p, _PROJECT_ROOT)) for p in chain)
        if exe not in self._descriptions:
            self._descriptions[exe] = _file_description(exe) if exe else ""
        stem = name[:-4] if name.lower().endswith(".exe") else name
        facts = ProcessFacts(pid, name, exe, self._descriptions[exe] or stem, own)
        if len(self._facts) > 512:
            self._facts.clear()
        self._facts[pid] = (created, facts)
        return facts


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalise_text(text: str, rules: PrivacyRules, max_chars: int) -> tuple[str, bool]:
    """Trim lines, drop empty and masked lines and repeated neighbours; cap size."""
    lines: list[str] = []
    size = 0
    truncated = False
    previous = None
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or line == previous or rules.is_masked(line):
            continue
        if size + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        size += len(line) + 1
        previous = line
    return "\n".join(lines), truncated


def text_delta(previous: str | None, current: str) -> str:
    """The lines of ``current`` that ``previous`` did not have, in order.

    Multiset line diff: a line counts as old as many times as it occurred
    before, so a repeated message ("ok" twice) is still new the second time,
    while lines that only moved (a list re-sorted, a chat scrolled) are not.
    Runs of new lines are kept together; separate runs are split by a blank
    line.  For a chat this is exactly the messages that appeared since the
    last capture.
    """
    if not previous:
        return current
    remaining = Counter(previous.splitlines())
    groups: list[list[str]] = []
    run: list[str] = []
    for line in current.splitlines():
        if remaining[line] > 0:
            remaining[line] -= 1
            if run:
                groups.append(run)
                run = []
        else:
            run.append(line)
    if run:
        groups.append(run)
    return "\n\n".join("\n".join(group) for group in groups)


def _tree_text(elements: list[UIElement], hidden: set[int], rules: PrivacyRules) -> str:
    """Names and values of a window's elements, one element per line."""
    lines: list[str] = []
    for element in elements:
        name = (element.name or "").strip()
        value = (element.value or "").strip()
        if element.id in hidden or rules.is_masked(value):
            value = ""
        if element.role == "Hyperlink" and is_address(value):
            value = ""  # a link's address, not text on screen
        if name and value and value != name:
            lines.append(f"{name}: {value}")
        elif name or value:
            lines.append(name or value)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UIA helpers for the password gates (worker thread only)
# ---------------------------------------------------------------------------

_CUIAUTOMATION8_CLSID = "{e22ad333-b25f-460c-83d0-0581107395c9}"
_CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"


class _PasswordProbe:
    """A UIA client of the worker's own, bounded by UIA's transaction timeouts."""

    def __init__(self) -> None:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        self._module = comtypes.client.GetModule("UIAutomationCore.dll")
        automation = None
        try:
            automation = comtypes.client.CreateObject(
                _CUIAUTOMATION8_CLSID, interface=self._module.IUIAutomation2
            )
            automation.ConnectionTimeout = 800
            automation.TransactionTimeout = 800
        except Exception:
            automation = comtypes.client.CreateObject(
                _CUIAUTOMATION_CLSID, interface=self._module.IUIAutomation
            )
        self._uia = automation

    def focused_is_password(self) -> bool | None:
        try:
            element = self._uia.GetFocusedElement()
            return bool(element.CurrentIsPassword) if element else False
        except Exception:
            return None

    def password_ids(self, elements: list[UIElement], limit: int) -> set[int]:
        """Ids of Edit elements with a value that UIA says are password fields.

        Each is hit-tested at its centre; when the element found there is not
        the same Edit (an overlay, a moved field) its value is dropped too.
        """
        hidden: set[int] = set()
        checked = 0
        for element in elements:
            if element.role != "Edit" or not element.value:
                continue
            if checked >= limit:
                hidden.add(element.id)
                continue
            checked += 1
            try:
                point = self._module.tagPOINT(*element.center)
                hit = self._uia.ElementFromPoint(point)
                rect = hit.CurrentBoundingRectangle
                same = (rect.left, rect.top, rect.right, rect.bottom) == tuple(element.bounds)
                if not same or bool(hit.CurrentIsPassword):
                    hidden.add(element.id)
            except Exception:
                hidden.add(element.id)
        return hidden


def _uia_threads_alive() -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith("yuki-uia-"))


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


@dataclass
class _Request:
    hwnd: int
    trigger: str
    title: str = ""
    requested_at: float = 0.0


@dataclass
class _Pending:
    hwnd: int
    trigger: str
    first: float
    last: float
    signature: tuple
    not_before: float = 0.0


@dataclass
class _Read:
    title: str = ""
    url: str | None = None
    text: str = ""
    truncated: bool = False
    block: str = ""  # privacy reason: nothing of this window is kept
    failure: str = ""
    busy: bool = False  # the window did not answer within the budget
    note: str = ""  # why there is no text


@dataclass
class CaptureResult:
    """What one capture attempt did (content-free)."""

    trigger: str
    app: str = ""
    outcome: str = "skipped"  # captured|deduplicated|unchanged|no_new_text|empty|skipped|failed
    reason: str = ""
    chars: int = 0
    delta_chars: int = 0
    truncated: bool = False
    source: str = ""  # page|tree
    ms: float = 0.0
    page_ms: float = 0.0
    tree_ms: float = 0.0
    store_ms: float = 0.0


class Watcher:
    """Event-driven capture of the foreground window into a memory store.

    Args:
        store_factory: called once on the capture worker thread for the store
            to write to (``lambda: store``).  The watcher never closes it.
        privacy: privacy configuration; the user's file by default.
        log: ``log(type, **fields)`` for content-free diagnostics.
        settings: timing and size budgets.
        on_close: called (from the hook thread) when the hidden window gets
            ``WM_CLOSE`` / end-of-session, so the service can shut down.
    """

    def __init__(
        self,
        store_factory: Callable[[], CaptureStore],
        *,
        privacy: PrivacyConfig | None = None,
        log: LogFn | None = None,
        settings: WatcherSettings | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._store_factory = store_factory
        self.privacy = privacy or PrivacyConfig()
        self._log_fn = log
        self.settings = settings or WatcherSettings()
        self._on_close = on_close
        self._processes = _ProcessCache()

        # hook-thread state
        self._hwnd = 0
        self._hook_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._callback = _WINEVENTPROC(self._on_winevent)
        self._fg_hook = None
        self._pid_hooks: list[int] = []
        self._hooked_pid = 0
        self._fg = 0
        self._locked = False
        self._fullscreen = False
        #: User pause (the ``paused`` flag file): nothing is captured.
        self._paused = False
        self._pending: _Pending | None = None
        self._activity_at = 0.0
        self._backstop_interval = self.settings.backstop_min_s
        self._next_backstop = 0.0
        self._idle = False

        # worker state
        self._cond = threading.Condition()
        self._request: _Request | None = None
        self._stopping = False
        self._busy: _Request | None = None
        self._worker: threading.Thread | None = None
        self._results_lock = threading.Lock()
        self._last_capture: dict[int, float] = {}
        self._last_backstop_captured: bool | None = None
        self._had_page: dict[int, bool] = {}
        self._probe: _PasswordProbe | None = None

        # stats
        self._started_at = 0.0
        self._events: Counter[str] = Counter()
        self._outcomes: Counter[str] = Counter()
        self._triggers: Counter[str] = Counter()
        self._latency: deque[float] = deque(maxlen=self.settings.latency_window)
        self._captured_chars = 0
        self._errors = 0
        #: Pause rows noted on the hook thread, written by the worker's store.
        self._paused_health: deque[tuple[float, str, str, str, str]] = deque(maxlen=64)

    # -- public ----------------------------------------------------------

    def start(self, timeout_s: float = 10.0) -> None:
        """Start both threads; returns once the hooks are registered."""
        self._started_at = time.monotonic()
        self._worker = threading.Thread(target=self._worker_main, name="yuki-memory-capture", daemon=True)
        self._worker.start()
        self._hook_thread = threading.Thread(target=self._hook_main, name="yuki-memory-hooks", daemon=True)
        self._hook_thread.start()
        if not self._ready.wait(timeout_s):
            raise RuntimeError("watcher hook thread did not start")
        if self._start_error is not None:
            raise RuntimeError(f"watcher failed to start: {self._start_error}")

    def stop(self, timeout_s: float = 5.0) -> None:
        """Unhook, stop the message loop and let the in-flight capture finish."""
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_APP_STOP, 0, 0)
        if self._hook_thread is not None:
            self._hook_thread.join(timeout_s)
        with self._cond:
            self._stopping = True
            self._request = None
            self._cond.notify_all()
        if self._worker is not None:
            self._worker.join(timeout_s)

    def set_paused(self, paused: bool) -> None:
        """Pause or resume capturing (the user's ``paused`` flag); safe from any thread.

        While paused nothing is read or stored: the per-process hooks are
        dropped, pending and backstop captures are skipped. One ``paused``
        event is logged per pause (plus a content-free ``paused``/``user``
        health row). Hooks are owned by the hook thread, so a running watcher
        applies the change there.
        """
        paused = bool(paused)
        if self._hwnd and self._hook_thread is not None and self._hook_thread.is_alive():
            _user32.PostMessageW(self._hwnd, WM_APP_PAUSE, int(paused), 0)
        else:
            self._set_paused(paused)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def alive(self) -> bool:
        return bool(self._hook_thread and self._hook_thread.is_alive())

    def stats(self) -> dict[str, Any]:
        """Counters and latency percentiles since start (content-free)."""
        with self._results_lock:
            latencies = sorted(self._latency)
            outcomes = dict(self._outcomes)
            triggers = dict(self._triggers)
            captured_chars = self._captured_chars
        events: dict[str, int] = {}
        for _ in range(3):  # the hook thread may add a key mid-copy
            try:
                events = dict(self._events)
                break
            except RuntimeError:
                continue

        def pct(p: float) -> float | None:
            if not latencies:
                return None
            return round(latencies[min(len(latencies) - 1, int(p * len(latencies)))], 1)

        minutes = max((time.monotonic() - self._started_at) / 60.0, 1e-9)
        attempts = sum(outcomes.values())
        return {
            "uptime_s": round(minutes * 60.0, 1),
            "events": events,
            "triggers": triggers,
            "outcomes": outcomes,
            "attempts": attempts,
            "captures_per_min": round(outcomes.get("captured", 0) / minutes, 2),
            "attempts_per_min": round(attempts / minutes, 2),
            "captured_chars": captured_chars,
            "latency_ms": {"p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": pct(1.0), "n": len(latencies)},
            "errors": self._errors,
            "locked": self._locked,
            "fullscreen": self._fullscreen,
            "paused": self._paused,
            "idle": self._idle,
            "backstop_interval_s": self._backstop_interval,
        }

    # -- logging ---------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:
            pass

    def _log_error(self, where: str, exc: BaseException) -> None:
        self._errors += 1
        self._log(
            "error",
            where=where,
            error=f"{type(exc).__name__}: {exc}",
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )

    # -- hook thread -----------------------------------------------------

    def _hook_main(self) -> None:
        try:
            self._create_window()
            _wtsapi32.WTSRegisterSessionNotification(self._hwnd, NOTIFY_FOR_THIS_SESSION)
            self._locked = session_locked()
            self._fg_hook = _user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                None,
                self._callback,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            if not self._fg_hook:
                raise OSError(ctypes.get_last_error(), "SetWinEventHook(EVENT_SYSTEM_FOREGROUND) failed")
            now = time.monotonic()
            self._next_backstop = now + self._backstop_interval
            self._log("watcher_start", locked=self._locked, settings=self.settings.__dict__)
            self._on_foreground(int(_user32.GetForegroundWindow() or 0), "start")
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            self._cleanup_hooks()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while True:
                timeout = self._next_timeout_ms(time.monotonic())
                _user32.MsgWaitForMultipleObjectsEx(0, None, timeout, QS_ALLINPUT, MWMO_INPUTAVAILABLE)
                quit_seen = False
                while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        quit_seen = True
                        break
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                if quit_seen:
                    break
                try:
                    self._tick(time.monotonic())
                except Exception as exc:
                    self._log_error("tick", exc)
        finally:
            self._cleanup_hooks()
            self._log("watcher_stop", stats=self.stats())

    def _create_window(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = WINDOW_CLASS
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error as exc:
            if exc.winerror != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise
        # A hidden top-level window (not message-only) so WM_CLOSE sent to the
        # process's windows reaches it, and WTS notifications can target it.
        self._hwnd = win32gui.CreateWindowEx(
            0, WINDOW_CLASS, "Yuki memory", 0, 0, 0, 0, 0, 0, 0, hinst, None
        )

    def _cleanup_hooks(self) -> None:
        self._unhook_pid()
        if self._fg_hook:
            _user32.UnhookWinEvent(self._fg_hook)
            self._fg_hook = None
        if self._hwnd:
            try:
                _wtsapi32.WTSUnRegisterSessionNotification(self._hwnd)
            except Exception:
                pass
            if win32gui.IsWindow(self._hwnd):
                try:
                    win32gui.DestroyWindow(self._hwnd)
                except win32gui.error:
                    pass
            self._hwnd = 0

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK:
                    self._set_locked(True)
                elif wparam == WTS_SESSION_UNLOCK:
                    self._set_locked(False)
                return 0
            if msg == WM_APP_PAUSE:
                self._set_paused(bool(wparam))
                return 0
            if msg == WM_APP_REFRESH:
                self._on_foreground(int(_user32.GetForegroundWindow() or 0), "resume")
                return 0
            if msg == WM_APP_STOP:
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_CLOSE:
                self._log("watcher_close_requested", via="WM_CLOSE")
                if self._on_close:
                    self._on_close()
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_QUERYENDSESSION:
                return 1
            if msg == win32con.WM_ENDSESSION:
                if wparam:
                    self._log("watcher_close_requested", via="WM_ENDSESSION")
                    if self._on_close:
                        self._on_close()
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self._log_error("wndproc", exc)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _set_locked(self, locked: bool) -> None:
        if locked == self._locked:
            return
        self._locked = locked
        self._log("watcher_state", locked=locked)
        if locked:
            self._pending = None
            self._unhook_pid()
            self._health_now(0, "paused", "locked", "session")
        else:
            self._on_foreground(int(_user32.GetForegroundWindow() or 0), "resume")

    def _set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            self._pending = None
            self._unhook_pid()
            self._log("paused", via="flag")
            self._health_now(0, "paused", "user", "pause")
        else:
            self._log("watcher_state", paused=False)
            if self._hwnd:
                self._on_foreground(int(_user32.GetForegroundWindow() or 0), "resume")

    def _hook_pid(self, pid: int) -> None:
        if pid == self._hooked_pid:
            return
        self._unhook_pid()
        if not pid or pid == os.getpid():
            return
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        for event in (EVENT_OBJECT_FOCUS, EVENT_OBJECT_NAMECHANGE):
            handle = _user32.SetWinEventHook(event, event, None, self._callback, pid, 0, flags)
            if handle:
                self._pid_hooks.append(handle)
        self._hooked_pid = pid

    def _unhook_pid(self) -> None:
        for handle in self._pid_hooks:
            _user32.UnhookWinEvent(handle)
        self._pid_hooks = []
        self._hooked_pid = 0

    def _on_winevent(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:
        try:
            hwnd = int(hwnd or 0)
            if event == EVENT_SYSTEM_FOREGROUND:
                self._events["foreground"] += 1
                self._on_foreground(hwnd or int(_user32.GetForegroundWindow() or 0), "foreground")
                return
            fg = self._fg
            if not fg or not hwnd or self._locked or self._fullscreen or self._paused:
                return
            if hwnd != fg and int(_user32.GetAncestor(hwnd, GA_ROOT) or 0) != fg:
                return
            now = time.monotonic()
            self._activity_at = now
            if event == EVENT_OBJECT_NAMECHANGE:
                if hwnd == fg and id_object == OBJID_WINDOW and id_child == CHILDID_SELF:
                    self._events["title"] += 1
                    self._arm("title", now)
                else:
                    self._events["content_name"] += 1  # only delays the settle
            elif event == EVENT_OBJECT_FOCUS:
                self._events["focus"] += 1
                self._arm("focus", now)
        except Exception as exc:  # never let an exception escape a ctypes callback
            self._log_error("winevent", exc)

    def _on_foreground(self, hwnd: int, trigger: str) -> None:
        if hwnd:
            hwnd = int(_user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
        self._fg = hwnd
        if self._locked or self._paused or not hwnd:
            self._unhook_pid()
            self._pending = None
            return
        rules = self.privacy.rules()
        fullscreen = rules.pause_when_fullscreen and is_fullscreen_front(hwnd)
        if fullscreen != self._fullscreen:
            self._fullscreen = fullscreen
            self._log("watcher_state", fullscreen=fullscreen)
            if fullscreen:
                self._health_now(hwnd, "paused", "fullscreen", trigger)
        if fullscreen:
            # Nothing from a full-screen app is read; drop its per-process hooks
            # so a game costs this process nothing but the foreground hook.
            self._unhook_pid()
            self._pending = None
            return
        self._hook_pid(_window_pid(hwnd))
        self._arm(trigger, time.monotonic(), force=True)

    def _signature(self, hwnd: int) -> tuple:
        return (_window_title(hwnd), _window_rect(hwnd))

    def _arm(self, trigger: str, now: float, *, force: bool = False) -> None:
        if self._locked or self._fullscreen or self._paused:
            return
        fg = self._fg
        if not fg:
            return
        busy = self._busy
        if not force and busy is not None and busy.hwnd == fg:
            # Events while this very window is being read: a focus event is
            # most likely the read itself; a title change is real.
            if trigger == "focus" or _window_title(fg) == busy.title:
                self._events["during_capture"] += 1
                return
        pending = self._pending
        if pending is not None and pending.hwnd == fg:
            if TRIGGER_RANK.get(trigger, 0) > TRIGGER_RANK.get(pending.trigger, 0):
                pending.trigger = trigger
                pending.not_before = 0.0
            pending.last = now
            return
        self._pending = _Pending(
            hwnd=fg, trigger=trigger, first=now, last=now, signature=self._signature(fg)
        )

    def _not_before(self, pending: _Pending) -> float:
        gap = self.settings.min_gap_s.get(pending.trigger, 0.0)
        if gap <= 0:
            return 0.0
        with self._results_lock:
            last = self._last_capture.get(pending.hwnd)
        return (last + gap) if last is not None else 0.0

    def _next_timeout_ms(self, now: float) -> int:
        deadlines = [self._next_backstop]
        pending = self._pending
        if pending is not None:
            if pending.not_before:
                deadlines.append(pending.not_before)
            else:
                quiet_since = max(pending.last, self._activity_at)
                deadlines.append(min(quiet_since + self.settings.settle_s, pending.first + self.settings.settle_cap_s))
        wait = min(deadlines) - now
        return max(int(wait * 1000) + 1, 0)

    def _tick(self, now: float) -> None:
        pending = self._pending
        if pending is not None and pending.hwnd != self._fg:
            self._pending = pending = None
        if pending is not None:
            if pending.not_before:
                if now >= pending.not_before:
                    self._submit(pending.hwnd, pending.trigger, now)
            else:
                cap_at = pending.first + self.settings.settle_cap_s
                quiet_since = max(pending.last, self._activity_at)
                if now >= cap_at or now - quiet_since >= self.settings.settle_s:
                    signature = self._signature(pending.hwnd)
                    if signature != pending.signature and now < cap_at:
                        pending.signature = signature
                        pending.last = now
                    else:
                        not_before = self._not_before(pending)
                        if now >= not_before:
                            self._submit(pending.hwnd, pending.trigger, now)
                        else:
                            pending.not_before = not_before
        if now >= self._next_backstop:
            self._backstop(now)

    def _backstop(self, now: float) -> None:
        with self._results_lock:
            outcome = self._last_backstop_captured
            self._last_backstop_captured = None
        if outcome is True:
            self._backstop_interval = self.settings.backstop_min_s
        elif outcome is False:
            self._backstop_interval = min(self._backstop_interval * 1.5, self.settings.backstop_max_s)
        self._next_backstop = now + self._backstop_interval
        if self._fullscreen and self._fg:
            # Leaving full screen without a foreground change (F11, a video)
            # has no event of its own: re-check it here.
            if not is_fullscreen_front(self._fg):
                self._on_foreground(self._fg, "resume")
            return
        if self._locked or self._paused or self._pending is not None or self._busy is not None or not self._fg:
            return
        idle = user_idle_s() > self.settings.idle_limit_s
        if idle != self._idle:
            self._idle = idle
            self._log("watcher_state", idle=idle)
        if idle:
            return
        self._submit(self._fg, "backstop", now)

    def _submit(self, hwnd: int, trigger: str, now: float) -> None:
        self._pending = None
        self._next_backstop = max(self._next_backstop, now + self._backstop_interval)
        request = _Request(hwnd=hwnd, trigger=trigger, title=_window_title(hwnd), requested_at=now)
        with self._cond:
            self._request = request  # latest wins
            self._cond.notify()

    # -- worker thread ---------------------------------------------------

    def _worker_main(self) -> None:
        store: CaptureStore | None = None
        try:
            store = self._store_factory()
        except BaseException as exc:
            self._log_error("store_open", exc)
        try:
            self._probe = _PasswordProbe()
        except Exception as exc:
            self._log_error("uia_probe", exc)
        while True:
            with self._cond:
                while self._request is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    break
                request, self._request = self._request, None
                self._busy = request
            try:
                result = self._capture(store, request)
            except Exception as exc:
                self._log_error("capture", exc)
                result = CaptureResult(trigger=request.trigger, outcome="failed", reason="unexpected")
            finally:
                self._busy = None
            self._record(store, request, result)
        # The store belongs to whoever made the factory (the service shares it
        # with the journal worker); it is not closed here.

    def _record(self, store: CaptureStore | None, request: _Request, result: CaptureResult) -> None:
        with self._results_lock:
            if result.outcome != "skipped":
                self._latency.append(result.ms)  # reads only: skips cost microseconds
            if result.outcome == "captured":
                self._captured_chars += result.delta_chars
            self._outcomes[result.outcome] += 1
            self._triggers[request.trigger] += 1
            if result.outcome != "skipped":
                self._last_capture[request.hwnd] = time.monotonic()
                if len(self._last_capture) > 256:
                    self._last_capture.clear()
            if request.trigger == "backstop":
                self._last_backstop_captured = result.outcome == "captured"
        self._log("capture", **result.__dict__)
        if store is not None:
            try:
                store.add_health(
                    time.time(), result.app, result.trigger, result.outcome, result.reason,
                    result.chars, round(result.ms, 1),
                )
            except Exception as exc:
                self._log_error("add_health", exc)

    def _health_now(self, hwnd: int, outcome: str, reason: str, trigger: str) -> None:
        """A health row noted on the hook thread; the worker writes it (its store)."""
        app = self._processes.get(_window_pid(hwnd)).app_name if hwnd else ""
        with self._results_lock:
            self._outcomes[outcome] += 1
        self._log("capture", trigger=trigger, app=app, outcome=outcome, reason=reason, chars=0, ms=0.0)
        self._paused_health.append((time.time(), app, trigger, outcome, reason))

    def _capture(self, store: CaptureStore | None, request: _Request) -> CaptureResult:
        started = time.perf_counter()
        result = CaptureResult(trigger=request.trigger)
        self._flush_paused_health(store)

        def done(outcome: str, reason: str = "") -> CaptureResult:
            result.outcome, result.reason = outcome, reason
            result.ms = (time.perf_counter() - started) * 1000.0
            return result

        hwnd = request.hwnd
        if store is None:
            return done("failed", "store_unavailable")
        if self._paused:
            return done("skipped", "paused")
        if not _user32.IsWindow(hwnd):
            return done("skipped", "window_gone")
        if int(_user32.GetForegroundWindow() or 0) != hwnd:
            return done("skipped", "not_foreground")
        rules = self.privacy.rules()
        if self._locked and rules.pause_when_locked:
            return done("skipped", "locked")
        facts = self._processes.get(_window_pid(hwnd))
        result.app = facts.app_name
        if not is_user_window(hwnd):
            return done("skipped", "not_user_window")
        if rules.skip_own_windows and facts.is_own:
            return done("skipped", "own_window")
        window_title = _window_title(hwnd)
        reason = rules.check_app(facts.process_name, facts.app_name) or rules.check_window(
            facts.process_name, window_title
        )
        if reason:
            return done("skipped", reason)
        if rules.pause_when_fullscreen and is_fullscreen_front(hwnd):
            if self._hwnd:
                _user32.PostMessageW(self._hwnd, WM_APP_REFRESH, 0, 0)
            return done("skipped", "fullscreen")
        if _uia_threads_alive() > self.settings.uia_backlog:
            return done("skipped", "uia_backlog")
        if (
            rules.skip_when_password_focused
            and self._probe is not None
            and self._probe.focused_is_password()
        ):
            return done("skipped", "password_focused")

        read = self._read_window(hwnd, window_title, rules, result)
        if read.block:
            return done("skipped", read.block)
        if read.failure:
            return done("failed", read.failure)
        if read.busy:
            return done("skipped", "window_busy")
        title, url = read.title, read.url

        text, clipped = normalise_text(read.text, rules, self.settings.max_chars)
        result.truncated = read.truncated or clipped
        result.chars = len(text)
        if not text:
            return done("empty", read.note or "no_text")

        s0 = time.perf_counter()
        try:
            thread_id = store.upsert_thread(facts.app_name, title, url, process=facts.process_name or None)
            previous = store.latest_text(thread_id)
            if previous is not None and _digest(previous) == _digest(text):
                result.store_ms = (time.perf_counter() - s0) * 1000.0
                return done("unchanged", "truncated" if result.truncated else "")
            delta = text_delta(previous, text)
            # An empty delta (lines only went away or moved) writes no capture
            # row, but the store still makes this text the thread's latest.
            capture_id = store.add_capture(thread_id, time.time(), request.trigger, text, delta)
            if not delta:
                result.store_ms = (time.perf_counter() - s0) * 1000.0
                return done("no_new_text", "truncated" if result.truncated else "")
        except Exception as exc:
            self._log_error("store", exc)
            result.store_ms = (time.perf_counter() - s0) * 1000.0
            return done("failed", "store_error")
        result.store_ms = (time.perf_counter() - s0) * 1000.0
        result.delta_chars = len(delta)
        outcome = "captured" if capture_id is not None else "deduplicated"
        return done(outcome, "truncated" if result.truncated else "")

    def _read_window(
        self, hwnd: int, window_title: str, rules: PrivacyRules, result: CaptureResult
    ) -> _Read:
        """Read the foreground window within the capture budget.

        A window that showed a page last time (or has not been seen yet) is
        asked for its page text first: the page probe is a page-only pass and
        the page's Text pattern hands over the whole page in one call.
        Otherwise the visible tree is read, and if it holds a page after all,
        that page's text is read too.  A page's address is checked against the
        privacy rules before any of its text is kept.
        """
        deadline = time.perf_counter() + self.settings.capture_budget_s
        read = _Read(title=window_title)

        def from_page() -> bool:
            t0 = time.perf_counter()
            timeout_s = max(deadline - t0, 0.2)
            page = page_text(hwnd, max_chars=self.settings.max_chars, timeout_s=timeout_s)
            result.page_ms += (time.perf_counter() - t0) * 1000.0
            if not page.found and page.elapsed_ms >= timeout_s * 1000.0:
                # The window did not answer within the budget (a page loading):
                # its frame alone is not worth a capture.  The load's own
                # events, or the backstop, bring the next attempt.
                read.busy = True
                return True
            if not page.found:
                return False
            read.block = rules.check_url(page.url) or ""
            if read.block:
                return True
            read.title, read.url = page.title or window_title, page.url
            read.text, read.truncated = page.text, page.truncated
            if not page.text:
                read.note = "read_error" if page.error else "page_empty"
            result.source = "page"
            return True

        page_first = self._had_page.get(hwnd, True)
        if page_first and from_page():
            self._remember_page(hwnd, True)
            return read
        t0 = time.perf_counter()
        try:
            tree = get_window_tree(
                hwnd,
                max_elements=self.settings.max_elements,
                timeout_s=max(deadline - t0, self.settings.tree_floor_s),
            )
        except ValueError:
            read.failure = "window_gone"
            return read
        except RuntimeError:
            read.failure = "read_error"
            return read
        finally:
            result.tree_ms = (time.perf_counter() - t0) * 1000.0
        identity = page_identity(tree.elements)
        self._remember_page(hwnd, identity is not None)
        if identity is not None:
            read.block = rules.check_url(identity.url) or ""
            if read.block:
                return read
            if not page_first and from_page():
                return read  # this window has a page after all
            read.title, read.url = identity.title or window_title, identity.url
        hidden: set[int] = set()
        if rules.drop_password_values and self._probe is not None:
            hidden = self._probe.password_ids(tree.elements, self.settings.password_checks)
        read.text = _tree_text(tree.elements, hidden, rules)
        read.truncated = tree.truncated
        if not tree.elements and tree.status == "busy":
            read.busy = True
            return read
        if not tree.elements and tree.status != "ok":
            read.note = f"tree_{tree.status}"
        result.source = "tree"
        return read

    def _remember_page(self, hwnd: int, has_page: bool) -> None:
        if len(self._had_page) > 256:
            self._had_page.clear()
        self._had_page[hwnd] = has_page

    def _flush_paused_health(self, store: CaptureStore | None) -> None:
        if store is None:
            return
        while self._paused_health:
            at, app, trigger, outcome, reason = self._paused_health.popleft()
            try:
                store.add_health(at, app, trigger, outcome, reason, 0, 0.0)
            except Exception as exc:
                self._log_error("add_health", exc)
                return


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
