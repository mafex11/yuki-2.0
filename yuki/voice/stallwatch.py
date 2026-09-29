"""What every thread did while the voice loop was stalled, measured without the interpreter lock (docs/VOICE.md v3.14b).

The v3.1 watchdog is a Python thread: it can look only once it gets the interpreter lock back, so a stall caused by a
thread holding that lock was always sampled *after* it ended, at whatever the threads did next (offline 29 Sept a
2.4 s stall was pinned on memory's log_turn encryption, which a probe then measured at 1-16 ms). What the operating
system counts does not need the lock: per-thread CPU (Windows ``GetThreadTimes``), the process's CPU and page faults
(``GetProcessTimes``, ``GetProcessMemoryInfo``) and the whole PC's busy share (``GetSystemTimes``), read every
:attr:`StallWatch.sample_s` on this module's own thread and again right after a stall. They say which thread burned
CPU while the loop waited (a thread holding the lock in native code shows its CPU), or none (the process was not
running: the PC's other programs had the CPU, or it was paging), joined to the stall's ``loop_lag`` event as ``cpu``.

A native stack dump during the stall (faulthandler's C watchdog, re-armed while the loop ticked) was tried and removed
the same evening: re-arming ends and starts faulthandler's thread while holding the interpreter lock, and a DLL whose
initialiser calls into Python (``win32ui``, loading on a worker thread) holds the Windows loader lock while it waits for
the interpreter lock: the watchdog thread's exit then waits for the loader lock, and the process deadlocked for good
(offline, one replay in ~20). Nothing here starts or ends a thread after it is built.

Plumbing only: numbers and thread names, nothing about what anyone said.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any

_THREAD_QUERY_LIMITED_INFORMATION = 0x0800


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

    def s(self) -> float:
        return ((self.hi << 32) | self.lo) / 1e7


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _win() -> Any:
    try:
        from ctypes import wintypes

        k32, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
        k32.OpenThread.restype = wintypes.HANDLE
        k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        times = [wintypes.HANDLE] + [ctypes.POINTER(_FT)] * 4
        k32.GetThreadTimes.argtypes = times
        k32.GetProcessTimes.argtypes = times
        k32.GetSystemTimes.argtypes = [ctypes.POINTER(_FT)] * 3
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        return k32, psapi
    except (AttributeError, OSError, ImportError):
        return None


class StallWatch:
    """One per conversation loop. ``tick()`` on every loop wake-up; ``report(started, ended)`` after a stall; ``stop()``."""

    def __init__(self, *, sample_s: float = 0.15, **_: Any) -> None:
        self.sample_s = sample_s
        self._tick = time.monotonic()
        self._native_names: dict[int, str] = {}
        self._stop = False
        self._lock = threading.Lock()
        #: (at, {native id: cpu s}, process cpu s, page faults, (system idle s, system busy s))
        self._samples: list[tuple[float, dict[int, float], float, int, tuple[float, float]]] = []
        self._win = _win()
        self._thread = threading.Thread(target=self._run, name="yuki-voice-stallwatch", daemon=True)
        self._thread.start()

    # -- on the loop -----------------------------------------------------------------------

    def tick(self) -> None:
        """The loop is alive now (a timestamp only)."""
        self._tick = time.monotonic()

    def report(self, started: float, ended: float) -> dict[str, Any]:
        """What the CPU counters say about the stall ``[started, ended]`` (monotonic)."""
        del ended
        out: dict[str, Any] = {}
        after = self._sample()
        with self._lock:
            before = [s for s in self._samples if s[0] <= started]
            base = before[-1] if before else (self._samples[0] if self._samples else None)
        if base is not None:
            span = max(1e-3, after[0] - base[0])
            names = {t.native_id: t.name for t in threading.enumerate() if getattr(t, "native_id", None)}
            names.update(self._native_names)
            busy = sorted(((names.get(tid, str(tid)), round((cpu - base[1].get(tid, cpu)) * 1000))
                           for tid, cpu in after[1].items()), key=lambda x: -x[1])
            idle, work = after[4][0] - base[4][0], after[4][1] - base[4][1]
            out["cpu"] = {
                "window_ms": round(span * 1000),
                "process_ms": round((after[2] - base[2]) * 1000),
                "threads_ms": {name: ms for name, ms in busy[:6] if ms >= 20},
                "page_faults": after[3] - base[3],
                # the whole PC over the same window: busy near 100 % means other programs had the CPU
                "system_busy_pct": round(100 * work / (idle + work)) if idle + work > 0 else None,
            }
        return out

    def stop(self) -> None:
        self._stop = True

    # -- its own thread ----------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop:
            sample = self._sample()
            with self._lock:
                self._samples.append(sample)
                del self._samples[:-40]
            time.sleep(self.sample_s)

    def _sample(self) -> tuple[float, dict[int, float], float, int, tuple[float, float]]:
        now = time.monotonic()
        cpu: dict[int, float] = {}
        proc, faults, system = 0.0, 0, (0.0, 0.0)
        if self._win is None:
            return now, cpu, proc, faults, system
        k32, psapi = self._win
        for t in threading.enumerate():
            tid = getattr(t, "native_id", None)
            if not tid:
                continue
            self._native_names[tid] = t.name
            h = k32.OpenThread(_THREAD_QUERY_LIMITED_INFORMATION, False, tid)
            if not h:
                continue
            c, e, kt, ut = _FT(), _FT(), _FT(), _FT()
            if k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                cpu[tid] = kt.s() + ut.s()
            k32.CloseHandle(h)
        hp = k32.GetCurrentProcess()
        c, e, kt, ut = _FT(), _FT(), _FT(), _FT()
        if k32.GetProcessTimes(hp, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            proc = kt.s() + ut.s()
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if psapi.GetProcessMemoryInfo(hp, ctypes.byref(pmc), pmc.cb):
            faults = int(pmc.PageFaultCount)
        idle, kern, user = _FT(), _FT(), _FT()
        if k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            system = (idle.s(), kern.s() + user.s() - idle.s())   # (kernel time includes idle time)
        return now, cpu, proc, faults, system


__all__ = ["StallWatch"]
