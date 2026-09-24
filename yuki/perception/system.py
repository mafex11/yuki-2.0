"""Cheap machine facts: clock, uptime, cpu, memory, heaviest processes, and
what the user is engaged in (:func:`activity_facts`: media sessions, microphone
and camera use, the foreground window).

``psutil.process_iter(['memory_info'])`` opens a handle per process and takes
~1.5 s on a busy desktop, which breaks the "perception is fast" rule.  The
process list therefore comes from a single ``NtQuerySystemInformation``
(SystemProcessInformation) call - names and working-set sizes for every process
in one syscall, ~3 ms - with the psutil path kept as a fallback.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import functools
import getpass
import platform
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from ctypes import wintypes
from typing import Any

import psutil

#: CPU percent is measured as a delta between calls.  Priming it at import
#: means the first real call reports a true value without ever sleeping.
psutil.cpu_percent(interval=None)

_TOP_PROCESS_COUNT = 10
_MB = 1024 * 1024

_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_SYSTEM_PROCESS_INFORMATION = 5


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _SystemProcessInformation(ctypes.Structure):
    """Prefix of SYSTEM_PROCESS_INFORMATION up to WorkingSetSize."""

    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UnicodeString),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


def _processes_native() -> list[dict]:
    """Every process with pid, image name and working set, in one syscall."""
    return [
        {"pid": pid, "name": name, "rss_mb": rss_mb}
        for pid, name, rss_mb, _ in _process_entries()
    ]


def _process_entries() -> list[tuple[int, str, float, int]]:
    """``(pid, image name, working set MB, creation FILETIME)`` for every process."""
    ntdll = ctypes.windll.ntdll
    size = 512 * 1024
    for _ in range(8):
        buffer = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(
            ctypes.c_ulong(_SYSTEM_PROCESS_INFORMATION),
            buffer,
            ctypes.c_ulong(size),
            ctypes.byref(needed),
        )
        if status == 0:
            break
        if status & 0xFFFFFFFF != _STATUS_INFO_LENGTH_MISMATCH:
            raise OSError(f"NtQuerySystemInformation failed: {status:#x}")
        size = max(needed.value + 64 * 1024, size * 2)
    else:  # pragma: no cover - the process list kept growing faster than us
        raise OSError("NtQuerySystemInformation: buffer never large enough")

    processes: list[tuple[int, str, float, int]] = []
    offset = 0
    base = ctypes.addressof(buffer)
    while True:
        entry = _SystemProcessInformation.from_address(base + offset)
        name = entry.ImageName.Buffer or ""
        pid = entry.UniqueProcessId or 0
        processes.append(
            (
                int(pid),
                name if name else ("System Idle Process" if pid == 0 else ""),
                round(entry.WorkingSetSize / _MB, 1),
                int(entry.CreateTime),
            )
        )
        if entry.NextEntryOffset == 0:
            break
        offset += entry.NextEntryOffset
    return processes


def _processes_psutil() -> list[dict]:
    """Fallback process list (slow, but portable across Windows builds)."""
    processes: list[dict] = []
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            processes.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "",
                    "rss_mb": round(memory.rss / _MB, 1) if memory else 0.0,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


def _all_processes() -> list[dict]:
    try:
        return _processes_native()
    except Exception:
        return _processes_psutil()


# ---------------------------------------------------------------------------
# Locale
# ---------------------------------------------------------------------------

#: ``LOCALE_NAME_MAX_LENGTH``.
_LOCALE_NAME_MAX = 85
#: ``LOCALE_SENGLISHDISPLAYNAME``: "English (United Kingdom)".
_LOCALE_SENGLISHDISPLAYNAME = 0x00000072
#: Where Windows keeps one key per installed keyboard layout (KLID).
_LAYOUTS_KEY = r"SYSTEM\CurrentControlSet\Control\Keyboard Layouts"

# Private handles: argtypes set here must not leak into the function objects
# other modules share through ``ctypes.windll``.
_kernel32 = ctypes.WinDLL("kernel32")
_user32 = ctypes.WinDLL("user32")
_kernel32.GetUserDefaultUILanguage.restype = wintypes.WORD
_kernel32.GetUserDefaultLocaleName.argtypes = [wintypes.LPWSTR, ctypes.c_int]
_kernel32.GetUserDefaultLocaleName.restype = ctypes.c_int
_kernel32.LCIDToLocaleName.argtypes = [wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD]
_kernel32.LCIDToLocaleName.restype = ctypes.c_int
_kernel32.GetLocaleInfoEx.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int]
_kernel32.GetLocaleInfoEx.restype = ctypes.c_int
_user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_user32.GetKeyboardLayout.restype = ctypes.c_void_p
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int


def _lcid_name(lcid: int) -> str:
    """``LCIDToLocaleName``: 0x0809 -> "en-GB", "" when Windows does not know it."""
    buffer = ctypes.create_unicode_buffer(_LOCALE_NAME_MAX)
    if not _kernel32.LCIDToLocaleName(lcid, buffer, _LOCALE_NAME_MAX, 0):
        return ""
    return buffer.value


def _locale_display_name(name: str) -> str:
    """English display name of a locale ("de-DE" -> "German (Germany)"), or ""."""
    if not name:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    if not _kernel32.GetLocaleInfoEx(name, _LOCALE_SENGLISHDISPLAYNAME, buffer, 256):
        return ""
    return buffer.value


@functools.lru_cache(maxsize=64)
def _layout_text(hkl: int) -> tuple[str, str]:
    """``(KLID, "Layout Text")`` for a keyboard layout handle, from the registry.

    An HKL's low word is the input language, its high word the physical layout:
    a plain language id ("00000407" German), ``0xFnnn`` for a layout variant
    whose registry key carries ``Layout Id`` = ``nnn``, or ``0xEnnn`` for an IME
    whose key is the whole HKL.  Cached per handle - there are only as many as
    the user has keyboards installed - so the registry is read once each.
    """
    device = (hkl >> 16) & 0xFFFF
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _LAYOUTS_KEY) as root:
            klid = ""
            if device & 0xF000 == 0xF000:
                wanted = device & 0x0FFF
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, name) as key:
                            layout_id, _ = winreg.QueryValueEx(key, "Layout Id")
                        if int(str(layout_id), 16) == wanted:
                            klid = name
                            break
                    except (OSError, ValueError):
                        continue
            elif device & 0xF000 == 0xE000:
                klid = f"{hkl:08X}"
            else:
                klid = f"{device:08X}"
            if not klid:
                return "", ""
            with winreg.OpenKey(root, klid) as key:
                text, _ = winreg.QueryValueEx(key, "Layout Text")
            return klid, str(text)
    except OSError:
        return "", ""


def keyboard_layout(hwnd: int | None = None) -> dict:
    """Keyboard layout the thread owning ``hwnd`` (default: foreground) types with.

    Layouts are per thread, so this is the one keystrokes sent to that window
    are translated with.  Returns ``{}`` when there is no such window.
    """
    if hwnd is None:
        hwnd = int(_user32.GetForegroundWindow() or 0)
    if not hwnd:
        return {}
    thread_id = _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    if not thread_id:
        return {}
    hkl = int(_user32.GetKeyboardLayout(thread_id) or 0) & 0xFFFFFFFF
    if not hkl:
        return {}
    language = _lcid_name(hkl & 0xFFFF)
    klid, layout = _layout_text(hkl)
    if layout and language:
        summary = f"{layout} ({language})"
    else:
        summary = layout or language or f"hkl {hkl:08X}"
    return {
        "hwnd": int(hwnd),
        "hkl": f"{hkl:08X}",
        "input_language": language,
        "klid": klid,
        "layout": layout,
        "summary": summary,
    }


def locale_facts(hwnd: int | None = None) -> dict:
    """Display language, regional-format culture, and the typing layout.

    All plain Win32 calls in-process (``GetUserDefaultUILanguage``,
    ``GetUserDefaultLocaleName``, ``GetKeyboardLayout``), a few microseconds in
    all once the layout name is cached, so the overview can carry it every turn.

    Args:
        hwnd: Window whose thread's keyboard layout to report; default the
            foreground window.
    """
    ui_language = _lcid_name(int(_kernel32.GetUserDefaultUILanguage()))
    buffer = ctypes.create_unicode_buffer(_LOCALE_NAME_MAX)
    user_locale = (
        buffer.value if _kernel32.GetUserDefaultLocaleName(buffer, _LOCALE_NAME_MAX) else ""
    )
    return {
        "ui_language": ui_language,
        "user_locale": user_locale,
        "user_locale_display": _locale_display_name(user_locale),
        "keyboard": keyboard_layout(hwnd),
    }


def system_facts(*, top_processes: int = _TOP_PROCESS_COUNT) -> dict:
    """Current machine state as a JSON-serialisable dict.

    Includes local time, uptime, cpu load (percent since the previous call, no
    sleeping), memory totals and the ``top_processes`` heaviest processes by
    resident set size, plus the locale (see :func:`locale_facts`).
    """
    try:
        locale = locale_facts()
    except Exception as exc:  # reported, not raised: the rest is still useful
        locale = {"error": f"{type(exc).__name__}: {exc}"}
    now = time.time()
    boot_time = psutil.boot_time()
    memory = psutil.virtual_memory()
    processes = _all_processes()
    processes.sort(key=lambda item: item["rss_mb"], reverse=True)
    local = _dt.datetime.fromtimestamp(now).astimezone()
    return {
        "time_local": local.isoformat(timespec="seconds"),
        "timestamp": now,
        "timezone": local.tzname() or "",
        "uptime_s": round(now - boot_time, 1),
        "boot_time": boot_time,
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "count_logical": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
        },
        "memory": {
            "total_mb": round(memory.total / _MB, 1),
            "used_mb": round(memory.used / _MB, 1),
            "available_mb": round(memory.available / _MB, 1),
            "percent": memory.percent,
        },
        "process_count": len(processes),
        "top_processes": processes[:top_processes],
        "host": {
            "hostname": socket.gethostname(),
            "user": getpass.getuser(),
            "os": f"{platform.system()} {platform.release()} {platform.version()}",
        },
        "locale": locale,
    }


def format_system_facts(facts: dict) -> str:
    """One-screen text rendering of :func:`system_facts` for the model."""
    memory = facts["memory"]
    cpu = facts["cpu"]
    uptime_h = facts["uptime_s"] / 3600.0
    locale = facts.get("locale") or {}
    keyboard = locale.get("keyboard") or {}
    return "\n".join(
        [
            f"ui language {locale.get('ui_language') or '?'} | "
            f"formats {locale.get('user_locale') or '?'}"
            f"{' (' + locale['user_locale_display'] + ')' if locale.get('user_locale_display') else ''}"
            f" | keyboard {keyboard.get('summary') or '?'}",
            f"{facts['time_local']} ({facts['timezone']}) | up {uptime_h:.1f} h | "
            f"cpu {cpu['percent']:.0f}% of {cpu['count_logical']} threads | "
            f"ram {memory['used_mb']:.0f}/{memory['total_mb']:.0f} MB "
            f"({memory['percent']:.0f}%) | {facts['process_count']} processes",
            f"host {facts['host']['hostname']} user {facts['host']['user']} "
            f"os {facts['host']['os']}",
            "top by memory: "
            + ", ".join(
                f"{p['name']}({p['pid']}) {p['rss_mb']:.0f}MB"
                for p in facts["top_processes"]
            ),
        ]
    )


# ---------------------------------------------------------------------------
# What the user is engaged in: media, microphone / camera, foreground window
# ---------------------------------------------------------------------------

#: Where Windows records, per app, when it last started and stopped using a
#: privacy-gated device. Packaged apps are keys named by package family;
#: desktop apps are under ``NonPackaged``, named by exe path with ``#`` for ``\``.
_CONSENT_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
#: ``(fact name, ConsentStore key)``.
_DEVICES = (("microphone", "microphone"), ("camera", "webcam"))

#: Longest the media-session read may take before it is reported as unanswered.
#: Steady state is a few ms; the cap covers a first call (WinRT import plus the
#: session manager's activation) and a media app that is slow to answer.
_MEDIA_TIMEOUT_S = 0.2

#: ``GlobalSystemMediaTransportControlsSessionPlaybackStatus``.
_PLAYBACK_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
#: ``AsyncStatus.Completed``.
_ASYNC_COMPLETED = 1

#: 100 ns ticks between 1601-01-01 (FILETIME) and 1970-01-01 (Unix).
_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000

#: WinRT calls run on one dedicated thread: a fixed apartment for the cached
#: session manager, and a hard bound on the caller's wait whatever WinRT does.
_media_lock = threading.Lock()
_media_executor: ThreadPoolExecutor | None = None
_media_pending: Future | None = None
#: The session manager, created once on the media thread and only used there.
_media_manager: Any = None


def _prefer_system_cpp_runtime() -> None:
    """Load the system's C++ runtime before WinRT's bundled one.

    ``winrt`` ships an old ``msvcp140.dll`` (14.29) next to its extension
    module. Windows keeps one ``msvcp140.dll`` per process (the first one
    loaded wins), and onnxruntime (the memory embedder) needs 14.40 or newer
    for ``std::mutex``: with WinRT's copy loaded first, the first embedding
    crashes the process with an access violation (the ``yuki-memory`` service
    died this way on 2026-09-24, 1-60 s after every start). The runtime is
    backward compatible, so loading System32's copy first serves both.
    """
    try:
        ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll"))
    except OSError:  # no VC++ redistributable installed: WinRT's copy is all there is
        pass


def _init_media_thread() -> None:
    """Put the media thread in the multithreaded apartment WinRT objects expect."""
    _prefer_system_cpp_runtime()
    try:
        from winrt import _winrt

        _winrt.init_apartment(_winrt.MTA)
    except Exception:  # already initialised, or winrt missing: the read reports it
        pass


def _media_thread() -> ThreadPoolExecutor:
    """The one media thread, started on first use (call with ``_media_lock`` held)."""
    global _media_executor
    if _media_executor is None:
        _media_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="yuki-media", initializer=_init_media_thread
        )
    return _media_executor


def _media_session_manager(deadline: float) -> Any:
    """The session manager, activated once (runs on the media thread).

    The first call imports the WinRT projection, 40-300 ms depending on the
    disk cache, which is why :func:`warm_activity` does it ahead of time.
    """
    global _media_manager
    if _media_manager is None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        operation = Manager.request_async()
        if operation.wait(max(0.0, deadline - time.monotonic())) != _ASYNC_COMPLETED:
            raise TimeoutError("the media session manager did not answer in time")
        _media_manager = operation.get_results()
    return _media_manager


def warm_activity(*, timeout_s: float = 5.0) -> None:
    """Import WinRT and activate the media session manager in the background.

    Returns at once; the work runs on the media thread, so a
    :func:`media_sessions` call arriving meanwhile reports "still waiting"
    rather than blocking. Never raises.
    """
    global _media_pending
    with _media_lock:
        if _media_manager is not None or (
            _media_pending is not None and not _media_pending.done()
        ):
            return
        # A first read too: the first get_sessions() pays ~35 ms of its own.
        _media_pending = _media_thread().submit(
            _read_media_sessions, time.monotonic() + timeout_s
        )


def _read_media_sessions(deadline: float) -> list[dict]:
    """Every media session Windows knows about (runs on the media thread)."""
    manager = _media_session_manager(deadline)
    current = manager.get_current_session()
    current_id = str(current.source_app_user_model_id or "") if current else ""
    sessions: list[dict] = []
    for session in manager.get_sessions():
        app_id = str(session.source_app_user_model_id or "")
        entry: dict = {
            "app_id": app_id,
            "status": "unknown",
            "title": "",
            "artist": "",
            "current": bool(current_id) and app_id == current_id,
        }
        try:
            status = int(session.get_playback_info().playback_status)
            entry["status"] = _PLAYBACK_STATUS.get(status, str(status))
        except Exception as exc:
            entry["error"] = f"playback info: {type(exc).__name__}: {exc}"
        remaining = deadline - time.monotonic()
        operation = session.try_get_media_properties_async() if remaining > 0 else None
        if operation is not None and operation.wait(remaining) == _ASYNC_COMPLETED:
            properties = operation.get_results()
            entry["title"] = str(properties.title or "")
            entry["artist"] = str(properties.artist or "")
        else:
            entry["error"] = "title and artist not read in time"
        sessions.append(entry)
    return sessions


def media_sessions(*, timeout_s: float = _MEDIA_TIMEOUT_S) -> list[dict]:
    """Media sessions (the ones the Windows media flyout shows), bounded in time.

    Each: ``app_id`` (the source app's AppUserModelID), ``status`` (playing,
    paused, stopped, ...), ``title``, ``artist``, ``current`` (the session the
    media keys would act on).

    Raises:
        TimeoutError: The read did not finish within ``timeout_s`` (it goes on
            in the background; calls made meanwhile raise at once).
        Exception: Whatever WinRT raised (package missing, service down).
    """
    global _media_pending
    with _media_lock:
        if _media_pending is not None and not _media_pending.done():
            raise TimeoutError("an earlier media session read is still waiting for Windows")
        deadline = time.monotonic() + timeout_s
        future = _media_thread().submit(_read_media_sessions, deadline)
        _media_pending = future
    try:
        return future.result(timeout=timeout_s + 0.05)
    except FutureTimeout:
        raise TimeoutError(
            f"media sessions did not answer within {timeout_s * 1000:.0f} ms"
        ) from None


def _subkeys(key: Any) -> list[str]:
    """Names of every subkey of an open registry key."""
    import winreg

    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def _device_users(
    key_name: str, processes: list[tuple[int, str, float, int]], boot_ft: int
) -> list[dict]:
    """Apps using one device right now, from the ConsentStore.

    An app is using it when its record has a start time and a stop time of 0.
    Windows leaves records at "stop 0" when an app dies or updates mid-use, so
    a record counts as live only when it started after the last boot and, for
    a desktop app, a process with that image name is running that was created
    before the use started (the process that opened the device still exists).
    """
    import winreg

    created: dict[str, list[int]] = {}
    for _, name, _, create_ft in processes:
        created.setdefault(name.lower(), []).append(create_ft)
    users: dict[str, dict] = {}

    def check(parent: Any, name: str, *, packaged: bool) -> None:
        try:
            with winreg.OpenKey(parent, name) as record:
                start, _ = winreg.QueryValueEx(record, "LastUsedTimeStart")
                stop, _ = winreg.QueryValueEx(record, "LastUsedTimeStop")
        except OSError:
            return
        start, stop = int(start or 0), int(stop or 0)
        if stop != 0 or start <= boot_ft:
            return
        if packaged:
            app, path = name.split("_", 1)[0], ""
        else:
            path = name.replace("#", "\\")
            app = path.rsplit("\\", 1)[-1]
            if not any(ft and ft <= start for ft in created.get(app.lower(), [])):
                return
        since = round((start - _FILETIME_UNIX_EPOCH) / 1e7, 1)
        previous = users.get(app.lower())
        if previous is None or since > previous["since"]:
            users[app.lower()] = {"app": app, "packaged": packaged, "path": path, "since": since}

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_CONSENT_STORE}\\{key_name}") as root:
        for name in _subkeys(root):
            if name == "NonPackaged":
                with winreg.OpenKey(root, name) as desktop:
                    for exe in _subkeys(desktop):
                        check(desktop, exe, packaged=False)
            else:
                check(root, name, packaged=True)
    return sorted(users.values(), key=lambda user: user["since"])


def _foreground_window(processes: list[tuple[int, str, float, int]]) -> dict:
    """The foreground window: hwnd, pid, process, class, title, and whether it is ours."""
    hwnd = int(_user32.GetForegroundWindow() or 0)
    if not hwnd:
        return {"hwnd": None}
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    class_name = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), class_name, 256)
    title = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), title, 512)
    process = next((name for p, name, _, _ in processes if p == pid.value), "")
    return {
        "hwnd": hwnd,
        "pid": int(pid.value),
        "process_name": process,
        "class_name": class_name.value,
        "title": title.value,
        "own_process": int(pid.value) == os.getpid(),
    }


def activity_facts(*, media_timeout_s: float = _MEDIA_TIMEOUT_S) -> dict:
    """What the user is engaged in right now, as plain facts.

    Read-only OS queries, each failure-tolerant (a part that fails is reported
    under ``errors`` and the rest still comes back):

    * ``media``: every media session (:func:`media_sessions`).
    * ``microphone`` / ``camera``: apps using the device now, from the
      CapabilityAccessManager ConsentStore (:func:`_device_users`): ``app``,
      ``packaged``, ``path``, ``since`` (Unix time the use started).
    * ``foreground``: the foreground window with its process and class, and
      ``own_process`` when it is Yuki's own window.

    ``elapsed_ms`` has the time of each part and the total.
    """
    started = time.perf_counter()
    facts: dict = {"foreground": {}, "media": [], "microphone": [], "camera": [], "errors": {}}
    elapsed: dict[str, float] = {}

    mark = time.perf_counter()
    try:
        processes = _process_entries()
    except Exception as exc:
        processes = []
        facts["errors"]["processes"] = f"{type(exc).__name__}: {exc}"
    elapsed["processes"] = round((time.perf_counter() - mark) * 1000, 1)

    mark = time.perf_counter()
    try:
        facts["foreground"] = _foreground_window(processes)
    except Exception as exc:
        facts["errors"]["foreground"] = f"{type(exc).__name__}: {exc}"
    elapsed["foreground"] = round((time.perf_counter() - mark) * 1000, 1)

    mark = time.perf_counter()
    try:
        facts["media"] = media_sessions(timeout_s=media_timeout_s)
    except Exception as exc:
        facts["errors"]["media"] = f"{type(exc).__name__}: {exc}"
    elapsed["media"] = round((time.perf_counter() - mark) * 1000, 1)

    boot_ft = int(psutil.boot_time() * 1e7) + _FILETIME_UNIX_EPOCH
    for fact, key_name in _DEVICES:
        mark = time.perf_counter()
        if not processes:
            facts["errors"][fact] = "process list unavailable, so device use cannot be confirmed"
        else:
            try:
                facts[fact] = _device_users(key_name, processes, boot_ft)
            except FileNotFoundError:
                facts[fact] = []  # nothing has ever asked for this device
            except Exception as exc:
                facts["errors"][fact] = f"{type(exc).__name__}: {exc}"
        elapsed[fact] = round((time.perf_counter() - mark) * 1000, 1)

    elapsed["total"] = round((time.perf_counter() - started) * 1000, 1)
    facts["elapsed_ms"] = elapsed
    return facts
