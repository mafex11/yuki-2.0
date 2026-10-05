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
  window every 45-90 s while the user is active (:func:`user_idle_s`: their
  real keys, clicks, wheel and mouse travel, :mod:`yuki.memory.userpresence`),
  and never while they are idle.
* the **capture worker** does the reading - a structured UIA read turned into
  messages (chats, mail) or clean main text by :func:`yuki.memory.extract.extract`
  - privacy gating (:mod:`yuki.memory.privacy`: the page URL, masked lines and
  messages), and the store writes, one capture at a time, latest request wins.
  A thread is ``(app, Extraction.thread_scope)``: a conversation's messages are
  stored against the thread's fingerprints (only unseen ones are stored,
  labelled new or history, see :meth:`yuki.memory.store.Store.add_conversation`);
  any other kind keeps a line delta of its main text, keyed by URL.

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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import psutil
import win32api
import win32con
import win32gui

from yuki.memory import secret_filter
from yuki.memory.extract import Extraction, extract
from yuki.memory.extract.uia import content_pid
from yuki.memory.privacy import PrivacyConfig, PrivacyRules
from yuki.memory.userpresence import Presence
from yuki.perception import visibility as _visibility
from yuki.perception.windows import is_user_window

# ---------------------------------------------------------------------------
# Store protocol (implemented by yuki.memory.store.Store)
# ---------------------------------------------------------------------------


class CaptureStore(Protocol):
    def upsert_thread(
        self, app: str, title: str, url: str | None, *, process: str | None = None,
        scope: str | None = None, kind: str | None = None,
    ) -> int: ...
    def latest_text(self, thread_id: int) -> str | None: ...
    def add_capture(
        self, thread_id: int, at: float, trigger: str, full_text: str, delta_text: str,
        *, kind: str | None = None, profile: str | None = None, by_yuki: bool = False,
        yuki_request: str | None = None,
    ) -> int | None: ...
    def add_conversation(self, thread_id: int, at: float, trigger: str, messages: list, **kwargs: Any) -> Any: ...
    def add_me_names(self, app: str | None, names: list[str]) -> int: ...
    def me_names(self) -> list[str]: ...
    def add_health(
        self, at: float, app: str, trigger: str, outcome: str, reason: str, chars: int, ms: float,
        **extra: Any,
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
    #: Wall-clock budget for the structured read of one window (yuki.memory.extract).
    capture_budget_s: float = 1.3
    #: Characters of main text kept per page/document snapshot; the rest is dropped and noted.
    max_chars: int = 20000
    #: Conversation labelling reference points (see Store.add_conversation):
    #: on a thread never seen before, a message stamped within this long of the
    #: capture counts as new; on a known thread, a time label may read this
    #: much earlier than the last look (labels show minutes only).
    first_visit_new_s: float = 300.0
    label_tolerance_s: float = 90.0
    #: Minimum gap per window between captures from one trigger kind, so a
    #: title that ticks every second or a user tabbing through fields cannot
    #: turn into a capture per second.  The capture is delayed, not dropped.
    min_gap_s: dict[str, float] = field(default_factory=lambda: {"title": 5.0, "focus": 10.0, "page": 2.0})
    #: A read of a window that had just come to the front (or changed page)
    #: that found nothing, or less than this many characters, or no answer,
    #: is retried once, after the window settles: on its next content event,
    #: or when the process behind a UWP frame appears, or at the latest
    #: ``retry_wait_s`` later. Settings pages and lazily built trees (CEF)
    #: gave an empty first read, and nothing followed (seen live 2026-09-24).
    retry_below_chars: int = 24
    retry_wait_s: float = 5.0
    #: Captures whose triggering event came this soon after Yuki stopped
    #: acting are still Yuki's doing (the page it opened finishing its load).
    acting_tail_s: float = 3.0
    #: UIA reads still running from earlier timed-out captures; above this the
    #: next capture is skipped instead of piling up threads on a hung provider.
    uia_backlog: int = 3
    #: Latency samples kept for the stats.
    latency_window: int = 2000

    # -- everything that can be seen, not only the window in front (2026-09-29) --
    #: Read the other visible windows, popups, menus and notification toasts too
    #: (on change only; docs/MEMORY.md "Watcher (capture)").
    visible: bool = True
    #: A window counts as on screen from this share of it not covered by windows above.
    visible_min_share: float = 0.03
    #: After a window event, the layout (Z-order, rectangles, visible shares) is measured again this much later.
    layout_debounce_s: float = 0.3
    #: A background window is read once its change events have been quiet this long, or this long after the first.
    bg_settle_s: float = 1.0
    bg_settle_cap_s: float = 6.0
    #: Gap between two reads of one background window: this after a read that found something new; each read
    #: that found nothing new doubles it, up to the maximum (the window keeps changing without saying anything).
    bg_min_gap_s: float = 15.0
    bg_max_gap_s: float = 300.0
    #: Budget of background reads (popups apart): at most this many, and this much reading time, per minute.
    #: Measured 2026-09-29: a background read costs 16-30 ms of this process's CPU on an opaque window, 90-200 ms
    #: on a browser page, so the ceiling is ~1% of one core while every visible window keeps changing; a desktop
    #: whose background windows are still costs a read per window per few minutes (the gap doubles).
    bg_reads_per_min: int = 4
    bg_read_ms_per_min: float = 1500.0
    #: One background read's own budget (the one in front keeps capture_budget_s).
    bg_budget_s: float = 1.0
    #: Processes whose visible background windows get a name-change hook; one sending more than
    #: bg_noisy_per_min events in a minute is unhooked for bg_mute_s (its windows then wait for the layout and
    #: the backstop). Measured 2026-09-29: Task Manager alone sent ~900 a minute, costing ~0.3% of one core.
    bg_max_processes: int = 8
    bg_noisy_per_min: int = 300
    bg_mute_s: float = 600.0
    #: A window measured opaque (content coverage under 5%) is not read again for its title changes or content
    #: events for this long (a Warp window's spinner changed its title every second: 509 empty reads in 3 h);
    #: its title and the app's own data are followed without a UI Automation read.
    opaque_reprobe_s: float = 600.0
    #: Popups (menus, toasts, flyouts, dialogs that appeared): read this long after they appeared, if still
    #: there; at most this many a minute; with this budget each.
    popup_settle_s: float = 0.5
    popups_per_min: int = 6
    popup_budget_s: float = 0.8
    #: Content changes inside the window in front that change neither its title nor its focus (a chat message
    #: arriving): a read at most this often, doubling while reads find nothing new, up to the maximum.
    fg_content_gap_s: float = 30.0
    fg_content_max_s: float = 180.0
    #: The app's own data about an opaque window (Warp's saved tabs) is read at most this often per window.
    app_data_every_s: float = 5.0


#: Class name of the watcher's hidden window.  The tray app stops the service
#: with ``PostMessage(FindWindow(WINDOW_CLASS, None), WM_CLOSE)``.
WINDOW_CLASS = "YukiMemoryWatcher"

TRIGGER_RANK = {"backstop": 0, "content": 0, "focus": 1, "title": 2, "page": 2, "retry": 2, "refresh": 2,
                "resume": 3, "foreground": 3, "start": 3}
#: Triggers a window measured opaque is not read for (its title and app data are followed instead).
OPAQUE_SKIP_TRIGGERS = frozenset({"backstop", "content", "focus", "title"})
#: Triggers a window whose last read was privacy-skipped (a blocked site) is not read again for, while its
#: title is the same ("title" and "page" always read: it may have left the site).
BLOCKED_SKIP_TRIGGERS = frozenset({"backstop", "content", "focus", "refresh"})
#: Privacy reasons of a page on a site the privacy file names (its sign-in, payment and meeting sites, whole or
#: by path): the page's host alone - never its title, path or content - goes to call detection, in this process
#: only, so a call on a meeting site that is never read can still be noticed (2026-10-05: a Google Meet call in
#: the Zen browser). Call detection decides from the privacy file's own lists which hosts are meeting sites and
#: never sends a visited host anywhere (yuki.memory.callevidence).
SITE_ONLY_REASONS = frozenset({"blocked_host", "blocked_path"})
#: Popups whose own element is one of these UIA control types (a fixed machine vocabulary) are shown in Now
#: but never stored: a tooltip or a menu is what is on screen, not something that happened.
NOW_ONLY_POPUPS = frozenset({"ToolTip", "Menu", "MenuBar"})
#: Why a background window is read, strongest last (a merge keeps the strongest).
BG_RANK = {"content": 0, "layout": 1, "title": 2, "shown": 3, "refresh": 4}
#: Triggers that mean the window in front just changed: an empty read after
#: one of them is retried once (``WatcherSettings.retry_wait_s``).
FRESH_TRIGGERS = frozenset({"foreground", "start", "resume", "page"})

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
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_CLOAKED = 0x8017
EVENT_OBJECT_UNCLOAKED = 0x8018
#: Events that change what is on screen (a window shown, hidden, cloaked, minimised, moved).
SURFACE_EVENTS = frozenset({EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND,
                            EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED})
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
#: From the timeline (same process, another thread): the page in front changed
#: address without a title change (a reel, a feed); wParam = the window.
WM_APP_PAGE = win32con.WM_APP + 4
#: From the capture worker: the last read of this window (wParam) was empty
#: right after it changed; retry it once it settles.
WM_APP_RETRY = win32con.WM_APP + 5
#: From the Now pipe (another thread): read this window (wParam; 0 = the one in front) again now.
WM_APP_NOW = win32con.WM_APP + 6
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
    """The process whose content ``hwnd`` shows: for the OS's UWP frame
    (ApplicationFrameHost.exe) the hosted app's (Windows Settings is
    SystemSettings.exe), so privacy gates, the app name and the per-process
    focus hooks all see the app rather than its host."""
    pid = content_pid(hwnd)
    if pid:
        return pid
    raw = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(raw))
    return int(raw.value)


def raw_idle_s() -> float:
    """Seconds since the last input Windows saw in this session, drift included."""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


class _PresenceSampler:
    """Tells a person's input from a drifting mouse, from the idle timer and the cursor.

    presence (5 Oct): only the fallback now, when Raw Input is unavailable
    (:mod:`yuki.memory.userpresence`). Its :attr:`BURST` rule cannot tell a
    vibrating sensor (20-30 resets a second, 0-1 px each) from typing, so with
    such a mouse it never lets the user go idle.

    Some mice report a one-count move every few seconds with nobody touching
    them, which resets the system idle timer forever. Measured on this PC
    (2026-09-24, G402): with hands off, a reset every ~4 s, each moving the
    cursor 0-1 px, ~9 px per minute in one direction. A person moves the cursor
    tens of pixels, and typing, clicking, scrolling or turning in a game (cursor
    captured, so it does not move) produce resets several times a second.

    So a reset counts as the user's only when the cursor moved at least
    :attr:`MOVE_PX` over the last :attr:`WINDOW_S`, or :attr:`BURST` resets
    arrived within :attr:`WINDOW_S`. Polls ``GetLastInputInfo`` and
    ``GetCursorPos`` at 10 Hz on a daemon thread: no hooks, nothing about keys
    or buttons is read, nothing is stored.
    """

    PERIOD_S = 0.1
    WINDOW_S = 1.5
    MOVE_PX = 4
    BURST = 3

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._real_tick: int | None = None
        self._thread: threading.Thread | None = None
        self._failed = False

    def idle_s(self) -> float:
        raw = raw_idle_s()
        if self._failed:
            return raw
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="yuki-presence", daemon=True)
                    self._thread.start()
        with self._lock:
            real = self._real_tick
        if real is None:
            return raw
        return max(raw, ((_kernel32.GetTickCount() - real) & 0xFFFFFFFF) / 1000.0)

    def _run(self) -> None:
        try:
            info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
            pt = wintypes.POINT()
            positions: deque[tuple[float, int, int]] = deque()
            resets: deque[float] = deque()
            last_tick: int | None = None
            while True:
                now = time.monotonic()
                have_pos = bool(_user32.GetCursorPos(ctypes.byref(pt)))
                if have_pos:
                    positions.append((now, pt.x, pt.y))
                while positions and now - positions[0][0] > self.WINDOW_S:
                    positions.popleft()
                if _user32.GetLastInputInfo(ctypes.byref(info)):
                    tick = int(info.dwTime)
                    if last_tick is None:
                        with self._lock:
                            self._real_tick = tick
                    elif tick != last_tick:
                        resets.append(now)
                        while resets and now - resets[0] > self.WINDOW_S:
                            resets.popleft()
                        moved = 0
                        if have_pos and positions:
                            moved = max(abs(pt.x - x) + abs(pt.y - y) for _, x, y in positions)
                        if moved >= self.MOVE_PX or len(resets) >= self.BURST:
                            with self._lock:
                                self._real_tick = tick
                    last_tick = tick
                time.sleep(self.PERIOD_S)
        except Exception:
            self._failed = True


_presence = Presence(raw_idle=raw_idle_s, fallback=_PresenceSampler().idle_s)


def user_idle_s() -> float:
    """Seconds since the user's last real input: a key, a mouse button, the wheel, the mouse travelling the takeover
    distance, an unlock; a vibrating or drifting mouse's reports and Yuki's own input left out.

    See :mod:`yuki.memory.userpresence`; falls back to :class:`_PresenceSampler` if Raw Input is unavailable.
    """
    return _presence.idle_s()


def presence_facts() -> dict[str, Any]:
    """Where :func:`user_idle_s` comes from and its counts (numbers only), for a log line."""
    return _presence.facts()


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
from yuki.core import appnames as _appnames   # noqa: E402 - the one app-name resolver
#: The frozen build's program folder (Yuki.exe, yuki-cli.exe); the project itself in dev.
from yuki.core.paths import app_dir as _app_dir   # noqa: E402 - stdlib-only helper

_APP_ROOT = _app_dir().resolve()


@dataclass(frozen=True)
class ProcessFacts:
    pid: int
    process_name: str  # "chrome.exe"
    exe: str
    app_name: str  # the app's own name (yuki.core.appnames: "Google Chrome", "WhatsApp" for WhatsApp.Root.exe)
    is_own: bool  # a Yuki process: this one, or one run by Yuki's interpreter
    #: Runs above Yuki (as administrator, like Task Manager): Windows keeps its UI out of reach, so its windows are
    #: reported ``elevated`` instead of read empty (report 78 C0; :func:`yuki.core.elevation.runs_above_us`).
    elevated: bool = False


def _under(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


class _ProcessCache:
    def __init__(self) -> None:
        self._facts: dict[int, tuple[float, ProcessFacts]] = {}

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
            own = any(p and (_under(p, _VENV_ROOT) or _under(p, _PROJECT_ROOT) or _under(p, _APP_ROOT))
                      for p in chain)
        try:
            from yuki.core.elevation import runs_above_us

            elevated = not own and runs_above_us(pid)
        except Exception:  # noqa: BLE001 - unknown: read it as before
            elevated = False
        facts = ProcessFacts(pid, name, exe, _appnames.name_for_exe(exe, pid=pid, image=name), own, elevated)
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


def mask_message_text(text: str, rules: PrivacyRules) -> str:
    """A message's text without its masked lines (a hidden secret); "" when nothing is left."""
    return "\n".join(line for line in (text or "").splitlines() if line.strip() and not rules.is_masked(line))


def _stats(extraction: Extraction) -> dict[str, Any]:
    """The extraction's diagnostics, content-free keys only."""
    keep = ("nodes", "read_ms", "total_ms", "source", "truncated")
    return {k: extraction.stats[k] for k in keep if k in extraction.stats}


# ---------------------------------------------------------------------------
# UIA helper for the password gate (worker thread only)
# ---------------------------------------------------------------------------

_CUIAUTOMATION8_CLSID = "{e22ad333-b25f-460c-83d0-0581107395c9}"
_CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"


_UIA_IS_PASSWORD = 30019  # UIA_IsPasswordPropertyId
_TREE_SCOPE_ELEMENT = 1
_ELEMENT_MODE_NONE = 0  # cached properties only, no live element reference

#: Longest the capture waits for the password-focus answer.
PASSWORD_PROBE_S = 0.1
#: UIA's own timeouts for the probe's client (ms): a hung provider frees the
#: probe thread within about this long, whatever the capture already decided.
_PROBE_UIA_TIMEOUT_MS = 300


class _PasswordProbe:
    """Whether the focused element is a password field, answered in ~100 ms.

    One daemon thread with its own UIA client and COM apartment asks
    ``GetFocusedElementBuildCache`` with a cache request for ``IsPassword``
    only (one cross-process call; element mode None, so no live element and
    no second round trip for the property).  :meth:`focused_is_password`
    waits at most ``timeout_s`` for it: ``True`` / ``False``, or ``None`` =
    unknown (the focused app did not answer in time, or a previous question
    is still stuck in it - then no new one is queued).  Unknown is not a skip:
    the structured read never reads a password field's value either way.
    Measured 2026-09-24: one capture spent ~4.5 s in the old
    ``GetFocusedElement`` + ``CurrentIsPassword`` gate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._busy = False
        self._generation = 0
        self._answer: tuple[int, bool | None] = (0, None)
        self._answered = threading.Condition(self._lock)
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._main, name="yuki-password-probe", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        if self._error:
            raise RuntimeError(self._error)

    def _main(self) -> None:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        try:
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            try:
                uia = comtypes.client.CreateObject(_CUIAUTOMATION8_CLSID, interface=module.IUIAutomation2)
                uia.ConnectionTimeout = _PROBE_UIA_TIMEOUT_MS
                uia.TransactionTimeout = _PROBE_UIA_TIMEOUT_MS
            except Exception:
                uia = comtypes.client.CreateObject(_CUIAUTOMATION_CLSID, interface=module.IUIAutomation)
            request = uia.CreateCacheRequest()
            request.AddProperty(_UIA_IS_PASSWORD)
            request.TreeScope = _TREE_SCOPE_ELEMENT
            request.AutomationElementMode = _ELEMENT_MODE_NONE
        except Exception as exc:  # noqa: BLE001 - reported by the constructor
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        try:
            # The client's first call sets up its connection (25-150 ms
            # measured); pay that here, not inside the first capture's budget.
            uia.GetFocusedElementBuildCache(request)
        except Exception:
            pass
        self._ready.set()
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                generation = self._generation
            try:
                element = uia.GetFocusedElementBuildCache(request)
                value = element.GetCachedPropertyValue(_UIA_IS_PASSWORD) if element else False
                answer: bool | None = value is True or (isinstance(value, int) and value != 0)
            except Exception:
                answer = None
            with self._lock:
                self._busy = False
                self._answer = (generation, answer)
                self._answered.notify_all()

    def focused_is_password(self, timeout_s: float = PASSWORD_PROBE_S) -> bool | None:
        deadline = time.monotonic() + timeout_s
        with self._lock:
            if self._busy:
                return None  # the last question is still stuck in some provider
            self._generation += 1
            generation = self._generation
            self._busy = True
            self._wake.set()
            while self._answer[0] != generation:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._answered.wait(left)
            return self._answer[1]


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
    #: Yuki was acting for the user when this capture was triggered
    #: (:class:`yuki.memory.acting.Acting`), else None.
    acting: Any = None


@dataclass
class _Pending:
    hwnd: int
    trigger: str
    first: float
    last: float
    signature: tuple
    not_before: float = 0.0


@dataclass
class _BgPending:
    """A visible window (not the one in front) or a popup waiting to be read (hook thread)."""

    hwnd: int
    reason: str      # BG_RANK key, or "popup"
    first: float
    last: float
    popup: bool = False


@dataclass
class _BgJob:
    hwnd: int
    reason: str
    popup: bool = False
    #: Only the app's own data for an opaque window (no UI Automation read).
    app_data_only: bool = False


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
    #: The extraction: profile ("slack", "generic_page"), kind ("conversation",
    #: "page", ...), characters of UI chrome it removed, its diagnostics.
    profile: str = ""
    kind: str = ""
    dropped_chars: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    #: Conversations: messages on screen, and how many were stored as new / history.
    messages: int = 0
    new_messages: int = 0
    history_messages: int = 0
    source: str = ""  # uia|tree|page_text (the extraction's read)
    ms: float = 0.0
    #: Pre-read gates (password-focus probe, full-screen check...), the read, the store writes.
    gates_ms: float = 0.0
    #: The password-focus probe alone, and its answer: "no" | "yes" | "unknown"
    #: (no answer within PASSWORD_PROBE_S: the capture goes on, the read never
    #: reads password values) | "off" (disabled or no probe).
    password_ms: float = 0.0
    password_check: str = "off"
    read_ms: float = 0.0
    store_ms: float = 0.0
    #: Secrets hidden before storing (:mod:`yuki.memory.secret_filter`): counts by kind, and its cost.
    secrets: dict[str, int] = field(default_factory=dict)
    secret_ms: float = 0.0
    #: Visible, not in front (2026-09-29): the share of the window no other covers, the share of it UI
    #: Automation shows content in (``coverage``; opaque under 5%), and elements left out as covered.
    visible_share: float | None = None
    coverage: float | None = None
    opaque: bool = False
    covered_nodes: int = 0


def _hide_secrets(ex: Extraction, result: CaptureResult) -> Extraction:
    """The extraction with every secret in its messages, body, title, address and scope replaced by a
    typed placeholder (``[password hidden]``), before anything is diffed, stored or sent to a model.

    A message is read with the message before it as context ("pass :" then the password alone). The
    fingerprints stay those of the text on screen, so a message re-read later is still recognised.
    """
    t0 = time.perf_counter()
    counts: Counter = Counter()

    def hide(text: str | None, context: str = "") -> str | None:
        if not text:
            return text
        r = secret_filter.redact(text, context=context)
        counts.update(r.counts)
        return r.text

    changes: dict[str, Any] = {}
    if ex.messages:
        messages, previous = [], ""
        for m in ex.messages:
            text = hide(m.text, previous)
            previous = m.text or ""
            messages.append(m if text == m.text else replace(m, text=text))
        changes["messages"] = messages
    for name in ("body", "title", "url", "thread_scope"):
        value = getattr(ex, name, None)
        if isinstance(value, str) and value:
            hidden = hide(value)
            if hidden != value:
                changes[name] = hidden
    result.secret_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    if not counts:
        return ex
    result.secrets = dict(counts)
    return replace(ex, **changes)


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
        user_names: the user's configured names (``Settings.user_names``);
            merged with the names learned on screen and kept in the store.
    """

    def __init__(
        self,
        store_factory: Callable[[], CaptureStore],
        *,
        privacy: PrivacyConfig | None = None,
        log: LogFn | None = None,
        settings: WatcherSettings | None = None,
        on_close: Callable[[], None] | None = None,
        user_names: list[str] | None = None,
        now_board: Any = None,
    ) -> None:
        self._store_factory = store_factory
        self.privacy = privacy or PrivacyConfig()
        self._log_fn = log
        self.settings = settings or WatcherSettings()
        self._on_close = on_close
        self._processes = _ProcessCache()
        #: The live picture of the screen (:class:`yuki.memory.now.NowBoard`), fed by every read; ``None``: off.
        self.now = now_board
        # -- visible windows, popups (hook thread, except where noted) --
        self._visible_hooks: list[int] = []
        self._layout: Any = None
        self._layout_due: float | None = None
        #: Visible background windows -> their title at the last layout (what is tracked).
        self._surf_titles: dict[int, str] = {}
        self._surf_pids: dict[int, int] = {}
        self._bg_pending: dict[int, _BgPending] = {}
        #: Per window: when it was last read (monotonic) and the gap before the next read.
        self._bg_last: dict[int, float] = {}
        self._bg_gap: dict[int, float] = {}
        #: Per-process name-change hooks of visible background windows, event counts this minute, mutes.
        self._bg_hooks: dict[int, list[int]] = {}
        self._bg_counts: Counter[int] = Counter()
        self._bg_minute = 0.0
        self._muted: dict[int, float] = {}
        #: Top-level windows that appeared (show / uncloak) and when, for popups.
        self._appeared: dict[int, float] = {}
        #: Popups already read (hwnd -> title at the time), so one is read once while it stays up.
        self._popups_read: dict[int, str] = {}
        #: Windows measured opaque (hwnd -> monotonic time), written by both workers, and the coverage measured.
        self._opaque: dict[int, float] = {}
        self._opaque_coverage: dict[int, float] = {}
        self._app_data_at: dict[int, float] = {}
        self._titles_cache: dict[str, tuple[float, list[str]]] = {}
        self._store_ref: Any = None
        #: Windows whose last read was privacy-skipped after reading (a blocked site): (title, why).
        self._bg_blocked: dict[int, tuple[str, str]] = {}
        #: ... and of those on a site the privacy file names (:data:`SITE_ONLY_REASONS`): (app key, exe, host, why),
        #: in memory only, so a skip without a read still tells call detection which site the window shows.
        self._blocked_site: dict[int, tuple[str, str, str, str]] = {}
        #: The window in front: its content events since its last read (first, last) and the adaptive gap.
        self._fg_content: tuple[float, float] | None = None
        self._fg_content_gap: dict[int, float] = {}
        # background worker
        self._bg_cond = threading.Condition()
        self._bg_queue: deque[_BgJob] = deque()
        self._bg_worker: threading.Thread | None = None
        self._bg_busy: _BgJob | None = None
        #: ``(monotonic time, ms, popup)`` of the background reads of the last minute (both threads, locked).
        self._bg_spent: deque[tuple[float, float, bool]] = deque()
        self._bg_lock = threading.Lock()
        self._bg_stats: Counter[str] = Counter()
        self._layout_ms: deque[float] = deque(maxlen=200)
        self._bg_next_due: float | None = None
        self._config_names = [n.strip() for n in (user_names or []) if n and n.strip()]
        #: Configured + learned names; loaded from the store by the worker.
        self._names: list[str] | None = None

        # hook-thread state
        self._hwnd = 0
        self._hook_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._callback = _WINEVENTPROC(self._on_winevent)
        self._fg_hook = None
        self._pid_hooks: list[int] = []
        self._hooked_pid: tuple[int, ...] = ()
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
        self._probe: _PasswordProbe | None = None
        #: What Yuki is acting on now (yuki.memory.acting.Acting), set by the
        #: service from any thread; and the last one with when it ended.
        self._acting: Any = None
        self._acting_last: tuple[Any, float] = (None, 0.0)
        #: ``(screen)``: called on the worker thread after each extraction of a window that passed the privacy
        #: gates, with ``{"process", "exe", "hwnd", "at", "host", "title", "controls"}`` (the visible control
        #: names), for call detection (:class:`yuki.memory.callevidence.CallEvidence`). Must not block. A page on
        #: a site the privacy file names (:data:`SITE_ONLY_REASONS`) is handed as ``{"process", "exe", "hwnd",
        #: "at", "host", "blocked"}`` only (no title, no controls), also when a skip did not read it again.
        self.on_screen: Callable[[dict[str, Any]], None] | None = None
        #: A retry waiting for the window to settle (hook thread):
        #: ``(hwnd, deadline, content pid when armed)``.
        self._retry: tuple[int, float, int] | None = None

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
        if self.settings.visible:
            self._bg_worker = threading.Thread(target=self._bg_main, name="yuki-memory-visible", daemon=True)
            self._bg_worker.start()
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
        with self._bg_cond:
            self._bg_queue.clear()
            self._bg_cond.notify_all()
        if self._worker is not None:
            self._worker.join(timeout_s)
        if self._bg_worker is not None:
            self._bg_worker.join(timeout_s)

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

    def set_acting(self, acting: Any) -> None:
        """Yuki started (an :class:`~yuki.memory.acting.Acting`) or stopped (``None``)
        acting on the desktop for the user; safe from any thread.

        Captures triggered meanwhile (and within ``acting_tail_s`` after) are
        stored ``by_yuki`` with the request's text.
        """
        previous = self._acting
        if acting is None and previous is not None:
            self._acting_last = (previous, time.monotonic())
        self._acting = acting
        if (acting is None) != (previous is None):
            self._log("watcher_state", acting=acting is not None)

    def acting_now(self) -> Any:
        """The request Yuki is acting on, or the one it finished within ``acting_tail_s``."""
        acting = self._acting
        if acting is not None:
            return acting
        last, ended = self._acting_last
        if last is not None and time.monotonic() - ended <= self.settings.acting_tail_s:
            return last
        return None

    def request_capture(self, hwnd: int, trigger: str = "page") -> None:
        """Ask for a capture of ``hwnd`` if it is still in front (another thread of
        the service, e.g. the timeline seeing the page's address change);
        rate-limited per window by ``min_gap_s[trigger]``."""
        if self._hwnd and trigger == "page":
            _user32.PostMessageW(self._hwnd, WM_APP_PAGE, int(hwnd), 0)

    def request_refresh(self, hwnd: int | None = None) -> bool:
        """Read ``hwnd`` (``None``: the window in front) again soon - the Now pipe's ``refresh``; any thread.

        Rate-limited like every trigger (a window read in the last 2 s is not read again); returns whether
        the request was posted to the hook thread."""
        if not self._hwnd:
            return False
        return bool(_user32.PostMessageW(self._hwnd, WM_APP_NOW, int(hwnd or 0), 0))

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
            "acting": self._acting is not None,
            "backstop_interval_s": self._backstop_interval,
            "visible": self._visible_stats(),
        }

    def _visible_stats(self) -> dict[str, Any]:
        """Content-free counters of the visible-window reads (2026-09-29)."""
        with self._bg_lock:
            counts = dict(self._bg_stats)
            spent = list(self._bg_spent)
        layouts = sorted(self._layout_ms)
        minute = time.monotonic() - 60.0
        return {
            "counts": counts,
            "reads_last_min": sum(1 for at, _ms, popup in spent if at >= minute and not popup),
            "popups_last_min": sum(1 for at, _ms, popup in spent if at >= minute and popup),
            "tracked": len(self._surf_titles),
            "hooked_processes": len(self._bg_hooks),
            "muted_processes": len(self._muted),
            "opaque_windows": len(self._opaque),
            "layout_ms_p50": layouts[len(layouts) // 2] if layouts else None,
            "layout_ms_max": layouts[-1] if layouts else None,
            "now": self.now.sizes() if self.now is not None else None,
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
            if self.settings.visible:
                # What is on screen changes: windows shown, hidden, cloaked, minimised, moved (system-wide,
                # out of context; measured 2026-09-29 at ~10-25 events a minute on this desktop).
                flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
                for low, high in ((EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE),
                                  (EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED),
                                  (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
                                  (EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MOVESIZEEND)):
                    handle = _user32.SetWinEventHook(low, high, None, self._callback, 0, 0, flags)
                    if handle:
                        self._visible_hooks.append(handle)
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
        self._unhook_bg()
        for handle in self._visible_hooks:
            _user32.UnhookWinEvent(handle)
        self._visible_hooks = []
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
                    _presence.note_unlock()     # the user is back: their password went to the secure desktop
                    self._set_locked(False)
                return 0
            if msg == WM_APP_PAUSE:
                self._set_paused(bool(wparam))
                return 0
            if msg == WM_APP_REFRESH:
                self._on_foreground(int(_user32.GetForegroundWindow() or 0), "resume")
                return 0
            if msg == WM_APP_PAGE:
                target = int(wparam or 0)
                if target and target == self._fg and not (self._locked or self._fullscreen or self._paused):
                    self._events["page"] += 1
                    now = time.monotonic()
                    self._activity_at = now
                    self._arm("page", now)
                return 0
            if msg == WM_APP_RETRY:
                target = int(wparam or 0)
                if target and target == self._fg and not (self._locked or self._fullscreen or self._paused):
                    self._events["retry_armed"] += 1
                    self._retry = (target, time.monotonic() + self.settings.retry_wait_s, _window_pid(target))
                return 0
            if msg == WM_APP_NOW:
                self._on_refresh_request(int(wparam or 0), time.monotonic())
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
            self._stop_visible()
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
            self._stop_visible()
            self._log("paused", via="flag")
            self._health_now(0, "paused", "user", "pause")
        else:
            self._log("watcher_state", paused=False)
            if self._hwnd:
                self._on_foreground(int(_user32.GetForegroundWindow() or 0), "resume")

    def _hook_pid(self, hwnd: int) -> None:
        """Focus/name-change hooks for the processes behind ``hwnd``: the
        window's own and, for a UWP frame, the hosted app's (the frame raises
        the title changes, the app its focus changes)."""
        pid = _window_pid(hwnd)
        raw = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(raw))
        pids = tuple(p for p in dict.fromkeys((pid, int(raw.value))) if p and p != os.getpid())
        if pids == self._hooked_pid:
            return
        self._unhook_pid()
        if not pids:
            return
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        for hooked in pids:
            for event in (EVENT_OBJECT_FOCUS, EVENT_OBJECT_NAMECHANGE):
                handle = _user32.SetWinEventHook(event, event, None, self._callback, hooked, 0, flags)
                if handle:
                    self._pid_hooks.append(handle)
        self._hooked_pid = pids

    def _unhook_pid(self) -> None:
        for handle in self._pid_hooks:
            _user32.UnhookWinEvent(handle)
        self._pid_hooks = []
        self._hooked_pid = ()

    def _on_winevent(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:
        try:
            hwnd = int(hwnd or 0)
            if event == EVENT_SYSTEM_FOREGROUND:
                self._events["foreground"] += 1
                self._on_foreground(hwnd or int(_user32.GetForegroundWindow() or 0), "foreground")
                return
            if event in SURFACE_EVENTS:
                self._on_surface_event(event, hwnd, id_object, id_child)
                return
            fg = self._fg
            if not fg or not hwnd or self._locked or self._fullscreen or self._paused:
                return
            root = hwnd if hwnd == fg else int(_user32.GetAncestor(hwnd, GA_ROOT) or 0)
            if root != fg:
                if event == EVENT_OBJECT_NAMECHANGE and root:
                    self._on_bg_name(root, hwnd == root and id_object == OBJID_WINDOW and id_child == CHILDID_SELF)
                return
            now = time.monotonic()
            self._activity_at = now
            if self._retry is not None and self._retry[0] == fg:
                # The window that read empty is doing something: read it again
                # once this burst settles.
                self._retry = None
                self._events["retry"] += 1
                self._arm("retry", now)
            if event == EVENT_OBJECT_NAMECHANGE:
                if hwnd == fg and id_object == OBJID_WINDOW and id_child == CHILDID_SELF:
                    self._events["title"] += 1
                    if self.now is not None:
                        self.now.note_title(fg, _window_title(fg), time.time())
                    self._arm("title", now)
                else:
                    self._events["content_name"] += 1  # delays the settle; a slow "content" read (below)
                    if self.settings.visible:
                        first = self._fg_content[0] if self._fg_content else now
                        self._fg_content = (first, now)
                        if self.now is not None:
                            self.now.note_change(fg, time.time())
            elif event == EVENT_OBJECT_FOCUS:
                self._events["focus"] += 1
                self._arm("focus", now)
        except Exception as exc:  # never let an exception escape a ctypes callback
            self._log_error("winevent", exc)

    # -- everything visible (hook thread) -----------------------------------------------

    def _on_surface_event(self, event: int, hwnd: int, id_object: int, id_child: int) -> None:
        """A top-level window was shown, hidden, (un)cloaked, (un)minimised or moved: measure the layout again."""
        if not hwnd or id_object != OBJID_WINDOW or id_child != CHILDID_SELF:
            return
        if self._locked or self._paused or self._fullscreen or not self.settings.visible:
            return
        if event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_UNCLOAKED):
            if int(_user32.GetAncestor(hwnd, GA_ROOT) or 0) != hwnd:
                return  # a child window: its top-level window's own events say what matters
            self._events["surface_shown"] += 1
            if len(self._appeared) > 256:
                self._appeared.clear()
            self._appeared[hwnd] = time.monotonic()
        else:
            self._events["surface_changed"] += 1
        self._layout_soon(time.monotonic())

    def _layout_soon(self, now: float) -> None:
        if not self.settings.visible:
            return
        due = now + self.settings.layout_debounce_s
        if self._layout_due is None or due < self._layout_due:
            self._layout_due = due

    def _on_bg_name(self, root: int, is_title: bool) -> None:
        """A name changed in a window that is not in front: its title, or something inside it."""
        if root not in self._surf_titles:
            return  # not a window on screen that is being followed
        now = time.monotonic()
        pid = self._surf_pids.get(root, 0)
        if now - self._bg_minute >= 60.0:
            self._bg_minute, self._bg_counts = now, Counter()
        self._bg_counts[pid] += 1
        if self._bg_counts[pid] > self.settings.bg_noisy_per_min and pid in self._bg_hooks:
            # Measured: a process that renames things all the time (Task Manager's counters) costs more
            # in events than its windows are worth; its windows wait for the layout and the backstop.
            self._unhook_bg_pid(pid)
            self._muted[pid] = now + self.settings.bg_mute_s
            self._log("visible_hook", op="muted", events_per_min=self._bg_counts[pid])
            with self._bg_lock:
                self._bg_stats["muted"] += 1
            return
        if is_title:
            title = _window_title(root)
            if title == self._surf_titles.get(root):
                return
            self._surf_titles[root] = title
            self._events["bg_title"] += 1
            if self.now is not None:
                self.now.note_title(root, title, time.time())
            self._bg_mark(root, "title", now)
        else:
            self._events["bg_content"] += 1
            if self.now is not None:
                self.now.note_change(root, time.time())
            self._bg_mark(root, "content", now)

    def _bg_mark(self, hwnd: int, reason: str, now: float, *, popup: bool = False) -> None:
        if not self.settings.visible or self._locked or self._paused or self._fullscreen:
            return
        pending = self._bg_pending.get(hwnd)
        if pending is not None:
            pending.last = now
            if BG_RANK.get(reason, 0) > BG_RANK.get(pending.reason, 0):
                pending.reason = reason
            return
        self._bg_pending[hwnd] = _BgPending(hwnd=hwnd, reason=reason, first=now, last=now, popup=popup)

    def _on_refresh_request(self, hwnd: int, now: float) -> None:
        """The Now pipe asked for a fresh read (hook thread)."""
        if self._locked or self._paused or self._fullscreen:
            return
        target = hwnd or self._fg
        if not target:
            return
        with self._results_lock:
            last = self._last_capture.get(target)
        if last is not None and now - last < 2.0:
            return  # read moments ago: that read is the fresh one
        self._events["refresh"] += 1
        if target == self._fg:
            self._arm("refresh", now)
        elif target in self._surf_titles:
            self._bg_mark(target, "refresh", now)
            pending = self._bg_pending.get(target)
            if pending is not None:
                pending.first = pending.last = now - self.settings.bg_settle_cap_s  # due at once
            self._bg_last.pop(target, None)

    def _stop_visible(self) -> None:
        """Locked, paused or a full-screen app in front: nothing else is read, and no process is hooked for it."""
        self._bg_pending.clear()
        self._appeared.clear()
        self._unhook_bg()
        with self._bg_cond:
            self._bg_queue.clear()

    def _unhook_bg_pid(self, pid: int) -> None:
        for handle in self._bg_hooks.pop(pid, []):
            _user32.UnhookWinEvent(handle)

    def _unhook_bg(self) -> None:
        for pid in list(self._bg_hooks):
            self._unhook_bg_pid(pid)

    def _refresh_layout(self, now: float) -> None:
        """Measure what is on screen, tell the board, and schedule reads of what appeared or changed."""
        self._layout_due = None
        if self._locked or self._paused or self._fullscreen or not self.settings.visible:
            return
        layout = _visibility.layout(foreground=self._fg or None)
        self._layout = layout
        self._layout_ms.append(round(layout.elapsed_ms, 2))
        with self._bg_lock:
            self._bg_stats["layouts"] += 1
        share = self.settings.visible_min_share
        visible = [s for s in layout.surfaces if s.visible_share >= share]
        if self.now is not None:
            self.now.set_layout(
                [{"hwnd": s.hwnd, "kind": s.kind, "title": s.title, "rect": list(s.rect), "share": s.visible_share,
                  "z": s.z, "pid": s.pid} for s in visible],
                foreground=self._fg, at=time.time(), ms=layout.elapsed_ms,
            )
        titles: dict[int, str] = {}
        pids: dict[int, int] = {}
        for s in visible:
            if s.hwnd == self._fg or s.pid == os.getpid() or self._processes.get(s.pid).is_own:
                continue  # Yuki's own windows (the tray's cards and pills) are never read
            if s.kind == "window":
                titles[s.hwnd], pids[s.hwnd] = s.title, s.pid
                before = self._surf_titles.get(s.hwnd)
                if before is None:
                    self._bg_mark(s.hwnd, "shown", now)
                elif before != s.title:
                    if self.now is not None:
                        self.now.note_title(s.hwnd, s.title, time.time())
                    self._bg_mark(s.hwnd, "title", now)
            elif s.kind == "popup":
                appeared = self._appeared.get(s.hwnd)
                if self._popups_read.get(s.hwnd) == s.title and appeared is None:
                    continue  # read already, still the same popup
                if appeared is not None or s.hwnd not in self._popups_read:
                    self._appeared.pop(s.hwnd, None)
                    self._popups_read[s.hwnd] = s.title
                    self._bg_mark(s.hwnd, "popup", appeared or now, popup=True)
        on_screen = {s.hwnd for s in visible}
        for hwnd in [h for h in self._bg_pending if h not in on_screen]:
            del self._bg_pending[hwnd]
        for hwnd in [h for h in self._popups_read if h not in on_screen]:
            del self._popups_read[hwnd]
        self._surf_titles, self._surf_pids = titles, pids
        self._update_bg_hooks(visible, now)

    def _update_bg_hooks(self, visible: list[Any], now: float) -> None:
        """Name-change hooks for the processes of the visible background windows (largest share first)."""
        for pid, until in list(self._muted.items()):
            if now >= until:
                del self._muted[pid]
        fg_pids = set(self._hooked_pid)
        wanted: list[int] = []
        for s in sorted((s for s in visible if s.hwnd in self._surf_titles), key=lambda s: -s.visible_share):
            if s.pid in fg_pids or s.pid in self._muted or s.pid in wanted:
                continue  # the one in front has its own hooks (they route here too)
            wanted.append(s.pid)
            if len(wanted) >= self.settings.bg_max_processes:
                break
        for pid in [p for p in self._bg_hooks if p not in wanted]:
            self._unhook_bg_pid(pid)
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        for pid in wanted:
            if pid in self._bg_hooks:
                continue
            handle = _user32.SetWinEventHook(EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, None,
                                             self._callback, pid, 0, flags)
            self._bg_hooks[pid] = [handle] if handle else []

    def _bg_spent_last_min(self, now: float, *, popup: bool) -> tuple[int, float]:
        with self._bg_lock:
            while self._bg_spent and now - self._bg_spent[0][0] > 60.0:
                self._bg_spent.popleft()
            reads = [(at, ms) for at, ms, p in self._bg_spent if p == popup]
        return len(reads), sum(ms for _at, ms in reads)

    def _bg_schedule(self, now: float) -> float | None:
        """Hand due background reads to the background worker, within budget; returns the next deadline."""
        if not self._bg_pending:
            return None
        if self._locked or self._paused or self._fullscreen:
            self._bg_pending.clear()
            return None
        s = self.settings
        idle = user_idle_s() > s.idle_limit_s
        next_due: float | None = None
        with self._bg_cond:
            queued = {job.hwnd for job in self._bg_queue}
            busy = self._bg_busy.hwnd if self._bg_busy is not None else None
            # reads handed over but not done yet count against the budget as well
            waiting = {True: 0, False: 0}
            for job in [*self._bg_queue, *([self._bg_busy] if self._bg_busy is not None else [])]:
                if not job.app_data_only:
                    waiting[job.popup] += 1
        for hwnd, pending in list(self._bg_pending.items()):
            if hwnd in queued or hwnd == busy:
                continue
            if hwnd == self._fg and not pending.popup:
                del self._bg_pending[hwnd]  # it came to the front: read by the capture of the window in front
                continue
            if pending.popup:
                due = pending.first + s.popup_settle_s
            else:
                with self._results_lock:
                    read_any = self._last_capture.get(hwnd)  # also a read while it was in front
                if pending.reason == "shown" and read_any is not None and now - read_any < s.bg_max_gap_s:
                    del self._bg_pending[hwnd]  # it was just in front and read then: that read is on the board
                    continue
                quiet = pending.last + s.bg_settle_s
                due = min(max(quiet, pending.first), pending.first + s.bg_settle_cap_s)
                last = max((t for t in (self._bg_last.get(hwnd), read_any) if t is not None), default=None)
                if last is not None:
                    due = max(due, last + self._bg_gap.get(hwnd, s.bg_min_gap_s))
            if due > now:
                next_due = due if next_due is None else min(next_due, due)
                continue
            if idle and not pending.popup:
                continue  # nobody is looking; read it when the user is back (it stays pending)
            opaque_at = self._opaque.get(hwnd)
            app_data_only = (not pending.popup and pending.reason != "refresh" and opaque_at is not None
                             and time.monotonic() - opaque_at < s.opaque_reprobe_s)
            if not app_data_only:
                reads, ms = self._bg_spent_last_min(now, popup=pending.popup)
                reads += waiting[pending.popup]
                limit = s.popups_per_min if pending.popup else s.bg_reads_per_min
                if reads >= limit or (not pending.popup and ms >= s.bg_read_ms_per_min):
                    with self._bg_lock:
                        self._bg_stats["over_budget"] += 1
                    retry = now + 5.0
                    next_due = retry if next_due is None else min(next_due, retry)
                    continue
            del self._bg_pending[hwnd]
            job = _BgJob(hwnd=hwnd, reason=pending.reason, popup=pending.popup, app_data_only=app_data_only)
            if not app_data_only:
                waiting[pending.popup] += 1
            with self._bg_cond:
                self._bg_queue.append(job)
                self._bg_cond.notify()
        return next_due

    def _on_foreground(self, hwnd: int, trigger: str) -> None:
        if hwnd:
            hwnd = int(_user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
        self._fg = hwnd
        self._retry = None
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
            self._stop_visible()
            self._pending = None
            return
        self._hook_pid(hwnd)
        self._fg_content = None
        self._bg_pending.pop(hwnd, None)  # in front now: the capture of the window in front reads it
        self._surf_titles.pop(hwnd, None)
        self._arm(trigger, time.monotonic(), force=True)
        self._layout_soon(time.monotonic())

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
        if self._layout_due is not None:
            deadlines.append(self._layout_due)
        if self._bg_next_due is not None:
            deadlines.append(self._bg_next_due)
        content_due = self._fg_content_due()
        if content_due is not None:
            deadlines.append(content_due)
        if self._retry is not None:
            # a condition poll (the hosted app's window appearing), bounded by the retry's deadline
            deadlines.append(min(self._retry[1], now + 0.25))
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
        retry = self._retry
        if retry is not None:
            hwnd, deadline, pid = retry
            if hwnd != self._fg:
                self._retry = None
            else:
                moved = _window_pid(hwnd) != pid
                if moved or now >= deadline:
                    # The frame's app appeared (its own focus/name events need its
                    # own hooks), or nothing happened within the bound: read again.
                    self._retry = None
                    self._events["retry"] += 1
                    self._hook_pid(hwnd)
                    self._arm("retry", now)
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
        if self._layout_due is not None and now >= self._layout_due:
            self._refresh_layout(now)
        content_due = self._fg_content_due()
        if content_due is not None and now >= content_due:
            self._fg_content = None
            if self._pending is None and self._busy is None:
                self._arm("content", now)
        self._bg_next_due = self._bg_schedule(now)

    def _fg_content_due(self) -> float | None:
        """When the content events of the window in front call for a read (a chat message arriving), or None."""
        span = self._fg_content
        if span is None or not self._fg:
            return None
        first, last = span
        s = self.settings
        due = min(max(last + s.bg_settle_s, first), first + s.bg_settle_cap_s)
        with self._results_lock:
            read = self._last_capture.get(self._fg)
        if read is not None:
            due = max(due, read + self._fg_content_gap.get(self._fg, s.fg_content_gap_s))
        return due

    def _backstop(self, now: float) -> None:
        with self._results_lock:
            outcome = self._last_backstop_captured
            self._last_backstop_captured = None
        if outcome is True:
            self._backstop_interval = self.settings.backstop_min_s
        elif outcome is False:
            self._backstop_interval = min(self._backstop_interval * 1.5, self.settings.backstop_max_s)
        self._next_backstop = now + self._backstop_interval
        self._layout_soon(now)  # a maximise, a restore or a snap raises no event of its own that is hooked
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
            self._log("watcher_state", idle=idle, presence=presence_facts())
        if idle:
            return
        self._submit(self._fg, "backstop", now)

    def _submit(self, hwnd: int, trigger: str, now: float) -> None:
        self._pending = None
        if hwnd == self._fg:
            self._fg_content = None  # this read covers the content events so far
        self._next_backstop = max(self._next_backstop, now + self._backstop_interval)
        request = _Request(hwnd=hwnd, trigger=trigger, title=_window_title(hwnd), requested_at=now,
                           acting=self.acting_now())
        with self._cond:
            self._request = request  # latest wins
            self._cond.notify()

    # -- worker thread ---------------------------------------------------

    def _worker_main(self) -> None:
        store: CaptureStore | None = None
        try:
            store = self._store_factory()
            self._store_ref = store
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

    # -- background worker: the other visible windows and popups ---------------------------

    def _bg_main(self) -> None:
        store: CaptureStore | None = None
        try:
            store = self._store_factory()
        except BaseException as exc:
            self._log_error("store_open_visible", exc)
        while True:
            with self._bg_cond:
                while not self._bg_queue and not self._stopping:
                    self._bg_cond.wait()
                if self._stopping:
                    return
                job = self._bg_queue.popleft()
                self._bg_busy = job
            started = time.monotonic()
            try:
                result = self._capture_visible(store, job)
            except Exception as exc:
                self._log_error("capture_visible", exc)
                result = CaptureResult(trigger="popup" if job.popup else f"visible:{job.reason}", outcome="failed",
                                       reason="unexpected")
            finally:
                with self._bg_cond:
                    self._bg_busy = None
            self._record_visible(store, job, result, started)

    def _capture_visible(self, store: CaptureStore | None, job: _BgJob) -> CaptureResult:
        """Read one window that is on screen but not in front (or a popup that appeared), through the same
        structured read, privacy gates, secret filter and store as the window in front."""
        started = time.perf_counter()
        result = CaptureResult(trigger="popup" if job.popup else f"visible:{job.reason}")
        self._flush_paused_health(store)

        def done(outcome: str, reason: str = "") -> CaptureResult:
            result.outcome, result.reason = outcome, reason
            result.ms = (time.perf_counter() - started) * 1000.0
            return result

        hwnd = job.hwnd
        if store is None:
            return done("failed", "store_unavailable")
        if self._paused or self._locked or self._fullscreen:
            return done("skipped", "paused" if self._paused else "locked" if self._locked else "fullscreen")
        if not _user32.IsWindow(hwnd):
            return done("skipped", "window_gone")
        if int(_user32.GetForegroundWindow() or 0) == hwnd:
            return done("skipped", "in_front")  # the capture of the window in front reads it
        layout = _visibility.layout()
        surface = layout.by_hwnd(hwnd)
        if surface is None:
            return done("skipped", "not_on_screen")
        result.visible_share = surface.visible_share
        if not job.popup and surface.visible_share < self.settings.visible_min_share:
            return done("skipped", "covered")
        facts = self._processes.get(_window_pid(hwnd))
        result.app = facts.app_name
        rules = self.privacy.rules()
        if facts.is_own or facts.pid == os.getpid():
            return done("skipped", "own_window")
        title = _window_title(hwnd)
        reason = rules.check_app(facts.process_name, facts.app_name) or rules.check_window(facts.process_name, title)
        if reason:
            if self.now is not None and not job.popup:
                self.now.block(hwnd, app=facts.app_name, process=facts.process_name, reason=reason, at=time.time(),
                               foreground=False)
            return done("skipped", reason)
        if facts.elevated:
            return done("skipped", "elevated")   # as for the window in front: Windows keeps its UI from us
        if job.app_data_only:
            result.opaque = True
            self._follow_opaque(hwnd, facts, title, foreground=False)
            return done("skipped", "opaque")
        blocked_as = self._bg_blocked.get(hwnd)
        if blocked_as is not None and blocked_as[0] == title and job.reason != "refresh":
            # its last read was a blocked site or a sign-in page, and it shows the same title: not read again
            self._tell_site_again(hwnd)   # still on that site: its host only, for call detection
            return done("skipped", f"{blocked_as[1]}_before")
        if _uia_threads_alive() > self.settings.uia_backlog:
            return done("skipped", "uia_backlog")
        t0 = time.perf_counter()
        result.gates_ms = (t0 - started) * 1000.0
        hidden = None if job.popup else (lambda bounds: layout.covered(hwnd, bounds))
        extraction = extract(
            hwnd, now=time.time(), user_names=self.user_names(store), app=facts.app_name,
            timeout_s=self.settings.popup_budget_s if job.popup else self.settings.bg_budget_s, hidden=hidden,
        )
        result.read_ms = (time.perf_counter() - t0) * 1000.0
        result.covered_nodes = int(extraction.stats.get("covered_nodes") or 0)
        if job.popup and self._names_blocked(extraction, rules):
            # A popup hosted by another process (a notification toast shown by the shell) that names an app the
            # user blocked: nothing of it is kept (their own list, matched exactly, fails safe).
            return done("skipped", "blocked_app_named")
        if extraction.profile != "error":
            blocked = rules.check_url(extraction.url or "")
            if blocked:   # a blocked site on screen: for a site the privacy file names, its host for call detection
                self._tell_site(hwnd, facts, extraction.url, blocked)
        opaque = False if job.popup else self._measure_opaque(hwnd, extraction, result)
        if opaque:
            self._follow_opaque(hwnd, facts, title, foreground=False)
        if job.popup and str(extraction.stats.get("root_role") or "") in NOW_ONLY_POPUPS:
            # a tooltip or a menu: part of what is on screen now, not something to remember
            rules_now = self.privacy.rules()
            if not (rules_now.check_url(extraction.url or "") or extraction.stats.get("password_fields")):
                self._now_put({"hwnd": hwnd, "foreground": False, "read_ms": result.read_ms, "popup": True,
                               "process": facts.process_name}, _hide_secrets(extraction, result), rules_now,
                              result.trigger, time.time(), by_yuki=False)
            result.profile, result.kind = extraction.profile, extraction.kind
            return done("skipped", "now_only")
        outcome, reason = self.ingest(
            store, extraction, result.trigger, process=facts.process_name, result=result, acting=self.acting_now(),
            now_meta={"hwnd": hwnd, "foreground": False, "read_ms": result.read_ms, "popup": job.popup,
                      "visible_share": surface.visible_share, "opaque": opaque, "process": facts.process_name},
        )
        self._note_blocked(hwnd, title, outcome, reason)
        return done(outcome, reason)

    @staticmethod
    def _names_blocked(extraction: Extraction, rules: PrivacyRules) -> bool:
        """Whether a line or control of the read is exactly the name of an app the privacy rules block."""
        blocked = {n.removesuffix(".exe") for n in (*rules.blocked_processes, *rules.user_blocked_apps) if n}
        if not blocked:
            return False
        pieces = [*(extraction.controls or []), *str(extraction.body or "").splitlines(),
                  *(m.sender or "" for m in extraction.messages), extraction.title or ""]
        return any(" ".join(p.split()).casefold() in blocked for p in pieces if p and p.strip())

    def _record_visible(self, store: CaptureStore | None, job: _BgJob, result: CaptureResult, started: float) -> None:
        """Counters, the adaptive per-window gap, the budget, the log and the health row of one background read."""
        read = result.outcome != "skipped" or result.reason == "opaque" or result.read_ms > 0
        now = time.monotonic()
        with self._bg_lock:
            self._bg_stats[f"{'popup' if job.popup else 'window'}:{result.outcome}"] += 1
            if result.outcome != "skipped" or result.read_ms > 0:
                self._bg_spent.append((now, result.read_ms, job.popup))
        if not job.popup and read:
            gap = self._bg_gap.get(job.hwnd, self.settings.bg_min_gap_s)
            self._bg_gap[job.hwnd] = (self.settings.bg_min_gap_s if result.outcome == "captured"
                                      else min(gap * 2.0, self.settings.bg_max_gap_s))
            self._bg_last[job.hwnd] = now
            if len(self._bg_last) > 256:
                self._bg_last.clear()
                self._bg_gap.clear()
        with self._results_lock:
            if result.outcome != "skipped" or result.read_ms > 0:
                self._latency.append(result.ms)
                self._last_capture[job.hwnd] = now
            if result.outcome == "captured":
                self._captured_chars += result.delta_chars
            self._outcomes[result.outcome] += 1
            self._triggers[result.trigger] += 1
        self._log("capture", **result.__dict__)
        if store is not None and result.outcome != "skipped":
            try:
                store.add_health(
                    time.time(), result.app, result.trigger, result.outcome, result.reason,
                    result.chars, round(result.ms, 1),
                    profile=result.profile or None, kind=result.kind or None,
                    dropped_chars=result.dropped_chars if result.profile else None,
                    messages=result.messages if result.kind in ("conversation", "email") else None,
                    new_messages=result.new_messages if result.kind in ("conversation", "email") else None,
                    stats=result.stats or None,
                )
            except Exception as exc:
                self._log_error("add_health", exc)

    def _record(self, store: CaptureStore | None, request: _Request, result: CaptureResult) -> None:
        with self._results_lock:
            if result.outcome != "skipped":
                self._latency.append(result.ms)  # reads only: skips cost microseconds
            if result.outcome == "captured":
                self._captured_chars += result.delta_chars
            self._outcomes[result.outcome] += 1
            self._triggers[request.trigger] += 1
            if result.outcome != "skipped" or result.read_ms > 0 or result.reason == "opaque":
                # a read that happened counts for the gaps, even when its result was then skipped (a blocked site),
                # and so does following an opaque window's title (its spinner must not cost a pass a second)
                self._last_capture[request.hwnd] = time.monotonic()
                if len(self._last_capture) > 256:
                    self._last_capture.clear()
            if request.trigger == "backstop":
                self._last_backstop_captured = result.outcome == "captured"
            if request.trigger == "content":
                gap = self._fg_content_gap.get(request.hwnd, self.settings.fg_content_gap_s)
                self._fg_content_gap[request.hwnd] = (
                    self.settings.fg_content_gap_s if result.outcome == "captured"
                    else min(gap * 2.0, self.settings.fg_content_max_s))
                if len(self._fg_content_gap) > 256:
                    self._fg_content_gap.clear()
        self._log("capture", **result.__dict__)
        if self._hwnd and (
            (request.trigger in FRESH_TRIGGERS or request.trigger == "title") and result.reason == "window_busy"
            or request.trigger in FRESH_TRIGGERS and (
                result.outcome == "empty"
                or (result.outcome in ("captured", "unchanged", "no_new_text", "deduplicated")
                    and result.chars < self.settings.retry_below_chars))
        ):
            # Once per change: the retry's own result never asks again.
            _user32.PostMessageW(self._hwnd, WM_APP_RETRY, int(request.hwnd), 0)
        if store is not None:
            try:
                store.add_health(
                    time.time(), result.app, result.trigger, result.outcome, result.reason,
                    result.chars, round(result.ms, 1),
                    profile=result.profile or None, kind=result.kind or None,
                    dropped_chars=result.dropped_chars if result.profile else None,
                    messages=result.messages if result.kind in ("conversation", "email") else None,
                    new_messages=result.new_messages if result.kind in ("conversation", "email") else None,
                    stats=result.stats or None,
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
            try:
                window_class = win32gui.GetClassName(hwnd)
            except win32gui.error:
                window_class = "?"
            # The class name is OS metadata ("Shell_TrayWnd"), not content.
            return done("skipped", f"not_user_window:{window_class}")
        if rules.skip_own_windows and facts.is_own:
            return done("skipped", "own_window")
        window_title = _window_title(hwnd)
        reason = rules.check_app(facts.process_name, facts.app_name) or rules.check_window(
            facts.process_name, window_title
        )
        if reason:
            if self.now is not None:
                self.now.block(hwnd, app=facts.app_name, process=facts.process_name, reason=reason, at=time.time(),
                               foreground=True)
            return done("skipped", reason)
        if rules.pause_when_fullscreen and is_fullscreen_front(hwnd):
            if self._hwnd:
                _user32.PostMessageW(self._hwnd, WM_APP_REFRESH, 0, 0)
            return done("skipped", "fullscreen")
        if facts.elevated:
            # runs as administrator (Task Manager): Windows keeps its UI from us, so a read only comes back empty
            return done("skipped", "elevated")
        blocked_as = self._bg_blocked.get(hwnd)
        if (request.trigger in BLOCKED_SKIP_TRIGGERS and blocked_as is not None and blocked_as[0] == window_title):
            # Its last read was a blocked site or a sign-in page (known only after reading), and it still shows
            # the same title: a meeting page in front was read again on every focus change (0.5-2 s each).
            result.app = facts.app_name
            self._tell_site_again(hwnd)   # still on that site: its host only, for call detection
            return done("skipped", f"{blocked_as[1]}_before")
        opaque_at = self._opaque.get(hwnd)
        if (request.trigger in OPAQUE_SKIP_TRIGGERS and opaque_at is not None
                and time.monotonic() - opaque_at < self.settings.opaque_reprobe_s):
            # Measured opaque minutes ago (its tree is its frame): its title is followed, and the app's own
            # data when a reader for it exists, without reading it again.
            result.app = facts.app_name
            result.opaque = True
            self._follow_opaque(hwnd, facts, window_title, foreground=True)
            return done("skipped", "opaque")
        if _uia_threads_alive() > self.settings.uia_backlog:
            return done("skipped", "uia_backlog")
        if rules.skip_when_password_focused and self._probe is not None:
            p0 = time.perf_counter()
            focused_password = self._probe.focused_is_password()
            result.password_ms = (time.perf_counter() - p0) * 1000.0
            result.password_check = {True: "yes", False: "no"}.get(focused_password, "unknown")  # type: ignore[arg-type]
            if focused_password:
                return done("skipped", "password_focused")

        # The structured read drops the value of every IsPassword element
        # itself; what it returns is messages (chats, mail) or main text.
        t0 = time.perf_counter()
        result.gates_ms = (t0 - started) * 1000.0
        extraction = extract(
            hwnd, now=time.time(), user_names=self.user_names(store), app=facts.app_name,
            timeout_s=self.settings.capture_budget_s,
        )
        result.read_ms = (time.perf_counter() - t0) * 1000.0
        self._tell_screen(hwnd, facts, extraction, rules)
        opaque = self._measure_opaque(hwnd, extraction, result)
        if opaque:
            self._follow_opaque(hwnd, facts, window_title, foreground=True)
        acting = request.acting or self._acting
        outcome, reason = self.ingest(store, extraction, request.trigger, process=facts.process_name, result=result,
                                      acting=acting,
                                      now_meta={"hwnd": hwnd, "foreground": True, "read_ms": result.read_ms,
                                                "visible_share": 1.0, "opaque": opaque, "process": facts.process_name})
        self._note_blocked(hwnd, window_title, outcome, reason)
        return done(outcome, reason)

    def _note_blocked(self, hwnd: int, title: str, outcome: str, reason: str) -> None:
        """Remember a read the privacy gates skipped after reading (a blocked site, a sign-in page), by title."""
        if outcome == "skipped" and reason and reason != "window_busy":
            if len(self._bg_blocked) > 256:
                self._bg_blocked.clear()
            self._bg_blocked[hwnd] = (title, reason)
        else:
            self._bg_blocked.pop(hwnd, None)
        if not (outcome == "skipped" and reason in SITE_ONLY_REASONS):
            self._blocked_site.pop(hwnd, None)

    # -- opaque windows and the app's own data --------------------------------------------

    #: Hours of the timeline the recent titles of an opaque app's windows come from, how many, how often.
    RECENT_TITLES_H = 12.0
    RECENT_TITLES_MAX = 10
    RECENT_TITLES_EVERY_S = 60.0

    def _recent_titles(self, process: str) -> list[str]:
        """The distinct titles an app's windows had in front lately, most used first (memory's timeline,
        privacy-filtered where it was recorded): for an app that shows no items, its tabs are its titles."""
        store = getattr(self, "_store_ref", None)
        read = getattr(store, "timeline_of", None)
        if not callable(read) or not process:
            return []
        cached = self._titles_cache.get(process)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.RECENT_TITLES_EVERY_S:
            return cached[1]
        try:
            from yuki.memory.store import app_key
            from yuki.memory.warp import clean_title

            key = app_key(process) or ""
            weight: dict[str, float] = {}
            for row in read(key, time.time() - self.RECENT_TITLES_H * 3600.0):
                if (getattr(row, "process", "") or "") != key or not getattr(row, "title", ""):
                    continue
                title = clean_title(str(row.title))
                if title:
                    weight[title] = weight.get(title, 0.0) + float((row.active_s or 0) + (row.passive_s or 0) + 1)
            titles = [t for t, _w in sorted(weight.items(), key=lambda kv: -kv[1])][: self.RECENT_TITLES_MAX]
        except Exception as exc:  # noqa: BLE001 - a hint only
            self._log_error("recent_titles", exc)
            titles = []
        if len(self._titles_cache) > 32:
            self._titles_cache.clear()
        self._titles_cache[process] = (now, titles)
        return titles

    def _measure_opaque(self, hwnd: int, extraction: Extraction, result: CaptureResult) -> bool:
        """Whether this read says the window is opaque (its content leaves cover under 5% of it), remembered.

        Only a read that answered in time counts (a window that did not answer is busy, not opaque).
        """
        from yuki.core.opacity import is_opaque   # the one threshold (revamp A8)

        coverage = extraction.stats.get("coverage")
        result.coverage = coverage
        truncated = bool(extraction.stats.get("truncated"))
        opaque = extraction.profile != "error" and not truncated and is_opaque(coverage)
        result.opaque = opaque
        if opaque:
            if len(self._opaque) > 256:
                self._opaque.clear()
                self._opaque_coverage.clear()
            self._opaque[hwnd] = time.monotonic()
            self._opaque_coverage[hwnd] = float(coverage)
        else:
            self._opaque.pop(hwnd, None)
            self._opaque_coverage.pop(hwnd, None)
        return opaque

    def _follow_opaque(self, hwnd: int, facts: ProcessFacts, title: str, *, foreground: bool) -> None:
        """For a window UI Automation cannot see into: the board gets its title and, when a reader for the app
        exists (:data:`yuki.memory.now.APP_DATA_READERS`), what the app's own local data says about it."""
        if self.now is None:
            return
        self.now.note_title(hwnd, title, time.time())
        if self.now.entry(hwnd) is None:
            self.now.put(hwnd, {"app": facts.app_name, "process": facts.process_name, "title": title, "kind": "",
                                "text": "", "messages": [], "controls": [], "read_at": time.time(),
                                "trigger": "opaque", "foreground": foreground, "opaque": True,
                                "coverage": self._opaque_coverage.get(hwnd)})
        now = time.monotonic()
        if now - self._app_data_at.get(hwnd, 0.0) < self.settings.app_data_every_s:
            return
        self._app_data_at[hwnd] = now
        from yuki.memory.now import app_data_for

        rect = _visibility.frame_bounds(hwnd) or _window_rect(hwnd)
        try:
            from yuki.perception.windows import list_monitors, monitor_at

            center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            monitor = monitor_at(center[0], center[1], list_monitors())
            scales = (1.0, monitor.dpi_scale) if monitor is not None and monitor.dpi_scale != 1.0 else (1.0,)
        except Exception:
            scales = (1.0,)
        t0 = time.perf_counter()
        data = app_data_for(facts.process_name, rect, title, privacy=self.privacy, scales=scales)
        recent = self._recent_titles(facts.process_name)
        if recent:
            data = {**(data or {"source": "memory"}), "recent_titles": recent}
        self.now.set_app_data(hwnd, data)
        with self._bg_lock:
            self._bg_stats["app_data_reads"] += 1
        self._log("app_data", app=facts.app_name, found=data is not None,
                  tabs=len(data.get("tabs") or []) if data else 0, ms=round((time.perf_counter() - t0) * 1000.0, 1))

    def _tell_screen(self, hwnd: int, facts: ProcessFacts, extraction: Extraction, rules: PrivacyRules) -> None:
        """Hand what the window offers (its control names) to ``on_screen``; never raises, never blocks."""
        hook = self.on_screen
        if hook is None or extraction.profile == "error":
            return
        try:
            blocked = rules.check_url(extraction.url or "")
            if blocked:
                # a blocked site: nothing of it leaves the watcher but, for a site the privacy file names, its host
                self._tell_site(hwnd, facts, extraction.url, blocked)
                return
            if extraction.stats.get("password_fields"):
                return   # a sign-in page: nothing of it leaves the watcher
            from yuki.memory.store import app_key, url_host

            hook({"process": app_key(facts.process_name) or "", "exe": facts.exe, "hwnd": int(hwnd),
                  "at": time.time(), "host": url_host(extraction.url), "title": extraction.title or "",
                  "controls": list(extraction.controls or [])})
        except Exception as exc:  # noqa: BLE001 - call detection never breaks capture
            self._log_error("on_screen", exc)

    def _tell_site(self, hwnd: int, facts: ProcessFacts, url: str | None, reason: str) -> None:
        """A read page the privacy rules skipped: when the reason is a site the privacy file names
        (:data:`SITE_ONLY_REASONS`), hand ``on_screen`` its app, host and time - nothing else - and remember them
        for the skips that do not read the window again (:meth:`_tell_site_again`). Never raises, never blocks."""
        if self.on_screen is None or reason not in SITE_ONLY_REASONS:
            return
        try:
            from yuki.memory.store import app_key, url_host

            host = url_host(url)
            if not host:
                return
            if len(self._blocked_site) > 256:
                self._blocked_site.clear()
            self._blocked_site[hwnd] = (app_key(facts.process_name) or "", facts.exe, host, reason)
            self._tell_site_again(hwnd)
        except Exception as exc:  # noqa: BLE001 - call detection never breaks capture
            self._log_error("on_screen", exc)

    def _tell_site_again(self, hwnd: int) -> None:
        """The window still shows the site its last read was skipped for (same title, not read again): tell
        ``on_screen`` it was seen now (app, host, time only). Never raises."""
        hook = self.on_screen
        site = self._blocked_site.get(hwnd)
        if hook is None or site is None:
            return
        key, exe, host, reason = site
        try:
            hook({"process": key, "exe": exe, "hwnd": int(hwnd), "at": time.time(), "host": host, "blocked": reason})
        except Exception as exc:  # noqa: BLE001 - call detection never breaks capture
            self._log_error("on_screen", exc)

    # -- extraction -> store (worker thread; also usable offline) -----------

    def user_names(self, store: CaptureStore | None) -> list[str]:
        """The configured names plus the names learned on screen (cached)."""
        if self._names is None:
            learned: list[str] = []
            if store is not None:
                try:
                    learned = store.me_names()
                except Exception as exc:
                    self._log_error("me_names", exc)
            self._names = list(dict.fromkeys([*self._config_names, *learned]))
        return self._names

    def ingest(
        self,
        store: CaptureStore,
        extraction: Extraction,
        trigger: str,
        *,
        process: str | None = None,
        at: float | None = None,
        result: CaptureResult | None = None,
        acting: Any = None,
        now_meta: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Privacy-gate and store one extraction; returns ``(outcome, reason)``.

        Conversations and mail (``kind`` conversation/email) are stored message
        by message against the thread's fingerprints; everything else keeps a
        line delta of its main text.  The thread is ``(app, thread_scope)``.
        ``acting``: Yuki was acting for the user (:class:`yuki.memory.acting.Acting`):
        the capture is stored ``by_yuki`` with the request's text.
        ``now_meta``: the window it came from (``hwnd``, ``foreground``, ``visible_share``, ``read_ms``,
        ``opaque``, ``process``): the Now board (:mod:`yuki.memory.now`) gets the same gated, redacted text.
        """
        rules = self.privacy.rules()
        result = result if result is not None else CaptureResult(trigger=trigger)
        result.app = result.app or extraction.app
        result.profile, result.kind = extraction.profile, extraction.kind
        result.dropped_chars = int(extraction.dropped_chars or 0)
        result.stats = _stats(extraction)
        result.source = str(extraction.stats.get("source", ""))
        result.truncated = bool(extraction.stats.get("truncated"))
        at = time.time() if at is None else at
        if extraction.profile == "error":
            return "failed", "extract_error"
        block = rules.check_url(extraction.url or "")
        if block:
            self._now_block(now_meta, extraction, block, at)
            return "skipped", block
        if rules.skip_when_password_present and extraction.stats.get("password_fields"):
            # a password field on screen (UIA IsPassword): a sign-in page or credentials dialog, whatever its
            # address says - measured on the window itself instead of guessed from words in the URL path
            self._now_block(now_meta, extraction, "password_field", at)
            return "skipped", "password_field"
        self._learn_names(store, extraction)
        # Secrets typed into chats, pages and documents become typed placeholders here,
        # before the delta, the store and every model call (docs/MEMORY.md, "Secret filter").
        extraction = _hide_secrets(extraction, result)
        self._now_put(now_meta, extraction, rules, trigger, at, by_yuki=acting is not None)

        s0 = time.perf_counter()
        yuki = {"by_yuki": True, "yuki_request": getattr(acting, "request", "") or ""} if acting is not None else {}
        try:
            if extraction.kind in ("conversation", "email"):
                return self._ingest_messages(store, extraction, trigger, process, at, rules, result, yuki)
            return self._ingest_text(store, extraction, trigger, process, at, rules, result, yuki)
        except Exception as exc:
            self._log_error("store", exc)
            return "failed", "store_error"
        finally:
            result.store_ms = (time.perf_counter() - s0) * 1000.0

    def _now_block(self, meta: dict[str, Any] | None, extraction: Extraction, reason: str, at: float) -> None:
        if self.now is None or not meta or not meta.get("hwnd"):
            return
        self.now.block(int(meta["hwnd"]), app=extraction.app, process=str(meta.get("process") or ""), reason=reason,
                       at=at, foreground=bool(meta.get("foreground")))

    def _now_put(self, meta: dict[str, Any] | None, extraction: Extraction, rules: PrivacyRules, trigger: str,
                 at: float, *, by_yuki: bool) -> None:
        """The board's copy of a read that passed every gate, secrets already hidden (never raises)."""
        if self.now is None or not meta or not meta.get("hwnd"):
            return
        try:
            from yuki.memory.now import gist_of

            front = bool(meta.get("foreground"))
            entry = gist_of(extraction, front=front, is_masked=rules.is_masked)
            entry.update(app=extraction.app, process=str(meta.get("process") or ""), read_at=at,
                         read_ms=round(float(meta.get("read_ms") or 0.0), 1), trigger=trigger, foreground=front,
                         visible_share=meta.get("visible_share"), coverage=extraction.stats.get("coverage"),
                         opaque=bool(meta.get("opaque")), by_yuki=by_yuki)
            if meta.get("popup"):
                entry["hwnd"] = int(meta["hwnd"])
                entry["at"] = at
                self.now.add_popup(entry)
            else:
                self.now.put(int(meta["hwnd"]), entry)
        except Exception as exc:  # noqa: BLE001 - the board never breaks capture
            self._log_error("now_board", exc)

    def _learn_names(self, store: CaptureStore, extraction: Extraction) -> None:
        known = {n.casefold() for n in self.user_names(store)}
        learned = [n for n in extraction.me_names if n and n.strip() and n.strip().casefold() not in known]
        if not learned:
            return
        try:
            if store.add_me_names(extraction.app or None, learned):
                self._log("me_names_learned", app=extraction.app, count=len(learned))
        except Exception as exc:
            self._log_error("add_me_names", exc)
        self._names = None  # reload on next use

    def _ingest_messages(
        self, store: CaptureStore, ex: Extraction, trigger: str, process: str | None, at: float,
        rules: PrivacyRules, result: CaptureResult, yuki: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        messages = []
        for message in ex.messages:
            text = mask_message_text(message.text, rules)
            if text:
                messages.append(message if text == message.text else replace(message, text=text))
        result.messages = len(messages)
        result.chars = sum(len(m.text) for m in messages)
        if not messages:
            return "empty", ("masked" if ex.messages else "no_complete_message")
        thread_id = store.upsert_thread(
            ex.app, ex.title, ex.url, process=process or None, scope=ex.thread_scope or None, kind=ex.kind,
        )
        written = store.add_conversation(
            thread_id, at, trigger, messages, kind=ex.kind, profile=ex.profile,
            first_visit_new_s=self.settings.first_visit_new_s, label_tolerance_s=self.settings.label_tolerance_s,
            **(yuki or {}),
        )
        result.new_messages, result.history_messages = written.new, written.history
        if written.capture_id is None:
            return "unchanged", ""
        result.delta_chars = int(getattr(written, "chars", 0) or 0)
        return "captured", ""

    def _ingest_text(
        self, store: CaptureStore, ex: Extraction, trigger: str, process: str | None, at: float,
        rules: PrivacyRules, result: CaptureResult, yuki: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        text, clipped = normalise_text(ex.body, rules, self.settings.max_chars)
        result.truncated = result.truncated or clipped
        result.chars = len(text)
        if not text:
            if ex.profile == "none" and result.truncated:
                # The window did not answer within the budget (a page loading):
                # the load's own events, or the backstop, bring the next attempt.
                return "skipped", "window_busy"
            return "empty", f"no_text:{ex.kind}"
        thread_id = store.upsert_thread(
            ex.app, ex.title, ex.url, process=process or None, scope=ex.thread_scope or None, kind=ex.kind,
        )
        previous = store.latest_text(thread_id)
        if previous is not None and _digest(previous) == _digest(text):
            return "unchanged", "truncated" if result.truncated else ""
        delta = text_delta(previous, text)
        # An empty delta (lines only went away or moved) writes no capture
        # row, but the store still makes this text the thread's latest.
        capture_id = store.add_capture(thread_id, at, trigger, text, delta, kind=ex.kind, profile=ex.profile,
                                       **(yuki or {}))
        if not delta:
            return "no_new_text", "truncated" if result.truncated else ""
        result.delta_chars = len(delta)
        outcome = "captured" if capture_id is not None else "deduplicated"
        return outcome, "truncated" if result.truncated else ""

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
