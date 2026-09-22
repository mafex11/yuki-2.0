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


def system_facts(*, top_processes: int = _TOP_PROCESS_COUNT) -> dict:
    """Current machine state as a JSON-serialisable dict.

    Includes local time, uptime, cpu load (percent since the previous call, no
    sleeping), memory totals and the ``top_processes`` heaviest processes by
    resident set size.
    """
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
    }


def format_system_facts(facts: dict) -> str:
    """One-screen text rendering of :func:`system_facts` for the model."""
    memory = facts["memory"]
    cpu = facts["cpu"]
    uptime_h = facts["uptime_s"] / 3600.0
    return "\n".join(
        [
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
