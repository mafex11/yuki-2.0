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

from yuki.memory.extract import Extraction, extract
from yuki.memory.extract.uia import content_pid
from yuki.memory.privacy import PrivacyConfig, PrivacyRules
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
        *, kind: str | None = None, profile: str | None = None,
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
    min_gap_s: dict[str, float] = field(default_factory=lambda: {"title": 5.0, "focus": 10.0})
    #: UIA reads still running from earlier timed-out captures; above this the
    #: next capture is skipped instead of piling up threads on a hung provider.
    uia_backlog: int = 3
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


@dataclass
class _Pending:
    hwnd: int
    trigger: str
    first: float
    last: float
    signature: tuple
    not_before: float = 0.0


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
    ) -> None:
        self._store_factory = store_factory
        self.privacy = privacy or PrivacyConfig()
        self._log_fn = log
        self.settings = settings or WatcherSettings()
        self._on_close = on_close
        self._processes = _ProcessCache()
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
        self._hook_pid(hwnd)
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
            return done("skipped", reason)
        if rules.pause_when_fullscreen and is_fullscreen_front(hwnd):
            if self._hwnd:
                _user32.PostMessageW(self._hwnd, WM_APP_REFRESH, 0, 0)
            return done("skipped", "fullscreen")
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
        outcome, reason = self.ingest(store, extraction, request.trigger, process=facts.process_name, result=result)
        return done(outcome, reason)

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
    ) -> tuple[str, str]:
        """Privacy-gate and store one extraction; returns ``(outcome, reason)``.

        Conversations and mail (``kind`` conversation/email) are stored message
        by message against the thread's fingerprints; everything else keeps a
        line delta of its main text.  The thread is ``(app, thread_scope)``.
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
            return "skipped", block
        self._learn_names(store, extraction)

        s0 = time.perf_counter()
        try:
            if extraction.kind in ("conversation", "email"):
                return self._ingest_messages(store, extraction, trigger, process, at, rules, result)
            return self._ingest_text(store, extraction, trigger, process, at, rules, result)
        except Exception as exc:
            self._log_error("store", exc)
            return "failed", "store_error"
        finally:
            result.store_ms = (time.perf_counter() - s0) * 1000.0

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
        rules: PrivacyRules, result: CaptureResult,
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
        )
        result.new_messages, result.history_messages = written.new, written.history
        if written.capture_id is None:
            return "unchanged", ""
        result.delta_chars = int(getattr(written, "chars", 0) or 0)
        return "captured", ""

    def _ingest_text(
        self, store: CaptureStore, ex: Extraction, trigger: str, process: str | None, at: float,
        rules: PrivacyRules, result: CaptureResult,
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
        capture_id = store.add_capture(thread_id, at, trigger, text, delta, kind=ex.kind, profile=ex.profile)
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
