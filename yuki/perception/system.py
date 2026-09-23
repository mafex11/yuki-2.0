"""Cheap machine facts: clock, uptime, cpu, memory, heaviest processes.

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
import socket
import time
from ctypes import wintypes

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

    processes: list[dict] = []
    offset = 0
    base = ctypes.addressof(buffer)
    while True:
        entry = _SystemProcessInformation.from_address(base + offset)
        name = entry.ImageName.Buffer or ""
        pid = entry.UniqueProcessId or 0
        processes.append(
            {
                "pid": int(pid),
                "name": name if name else ("System Idle Process" if pid == 0 else ""),
                "rss_mb": round(entry.WorkingSetSize / _MB, 1),
            }
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
