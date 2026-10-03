"""How Windows schedules the voice: out of power throttling, and the audio and loop threads on MMCSS (D7, v3.17).

07-voice-realtime §4 (#9) and §6 I: the stalls of 29-30 Sept (11.5 s, 6.2 s, 6.1 s) came with the PC mostly idle and the
process getting next to no CPU, and nothing in the code told Windows the voice is time-critical: no opt-out of power
throttling ("EcoQoS": a background process may be run slowly, on efficient cores and low clocks, to save power), and no
thread registered with the Multimedia Class Scheduler Service (MMCSS, what every audio app uses so its audio threads
are run on time). :mod:`yuki.voice.priority` raised the whole process's class during a conversation; this adds:

* :func:`process_high_qos` / :func:`process_default_qos`: while a conversation is live the process is opted out of
  execution-speed throttling (and its timer requests are honoured even when it has no window in front); afterwards
  Windows manages it again. ``SetProcessInformation(ProcessPowerThrottling)``.
* :class:`ThreadQos`: the calling thread opted out of thread power throttling and registered with an MMCSS task
  (``AvSetMmThreadCharacteristicsW``, task ``Audio``), and both undone by :meth:`ThreadQos.revert` on the same thread.
  Used by the voice loop (while live), the speaker's writer thread and the microphone's callback thread.

What it costs: nothing when the PC is idle. MMCSS keeps its own guard (by default 20 % of the CPU always stays for
everything else), and these threads are short bursts (the loop ~7-12 % of one core in a conversation, measured; the
audio threads far less). Every call is fail-soft: on an older Windows, or when a call is refused, it logs and goes on.
Plumbing only: OS calls, no reading of anything.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Any

#: ``PROCESS_INFORMATION_CLASS.ProcessPowerThrottling`` / ``THREAD_INFORMATION_CLASS.ThreadPowerThrottling``.
PROCESS_POWER_THROTTLING = 4
THREAD_POWER_THROTTLING = 3
POWER_THROTTLING_VERSION = 1
#: ``*_POWER_THROTTLING_EXECUTION_SPEED`` and ``PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION``.
EXECUTION_SPEED = 0x1
IGNORE_TIMER_RESOLUTION = 0x4
#: The MMCSS task the voice's threads join (HKLM ...\Multimedia\SystemProfile\Tasks\Audio: medium category).
MMCSS_TASK = "Audio"


class _Throttling(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


_lock = threading.Lock()
_holders = 0


def _kernel32() -> Any:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    k32.SetThreadInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetThreadInformation.restype = wintypes.BOOL
    return k32


def _avrt() -> Any:
    avrt = ctypes.WinDLL("avrt", use_last_error=True)
    avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
    avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
    avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
    return avrt


def _set_process(control: int, state: int) -> tuple[bool, int]:
    k32 = _kernel32()
    info = _Throttling(POWER_THROTTLING_VERSION, control, state)
    ok = bool(k32.SetProcessInformation(k32.GetCurrentProcess(), PROCESS_POWER_THROTTLING, ctypes.byref(info),
                                        ctypes.sizeof(info)))
    return ok, (0 if ok else ctypes.get_last_error())


def process_high_qos() -> dict[str, Any]:
    """Opt the process out of power throttling (counted: nested holders are fine). Returns facts for the log."""
    global _holders
    with _lock:
        _holders += 1
        if _holders > 1:
            return {"op": "process_high", "already": True}
        try:
            ok, err = _set_process(EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION, 0)
            return {"op": "process_high", "ok": ok, **({"error": err} if not ok else {})}
        except (AttributeError, OSError) as exc:
            return {"op": "process_high", "error": type(exc).__name__}


def process_default_qos() -> dict[str, Any]:
    """Hand the process's throttling back to Windows once the last holder is done."""
    global _holders
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders:
            return {"op": "process_default", "held": _holders}
        try:
            ok, err = _set_process(0, 0)
            return {"op": "process_default", "ok": ok, **({"error": err} if not ok else {})}
        except (AttributeError, OSError) as exc:
            return {"op": "process_default", "error": type(exc).__name__}


class ThreadQos:
    """The calling thread on time: out of thread power throttling and in an MMCSS task, until :meth:`revert`.

    Both are per thread, so :meth:`apply` and :meth:`revert` run on the thread itself. ``facts`` says what happened
    (for the log): ``throttling`` (opted out), ``mmcss`` (joined, its task index) or the error code Windows gave.
    """

    def __init__(self, task: str = MMCSS_TASK) -> None:
        self.task = task
        self.thread = 0
        self._mmcss: Any = None
        self.facts: dict[str, Any] = {}

    def apply(self, *, mmcss: bool = True) -> dict[str, Any]:
        self.thread = threading.get_ident()
        facts: dict[str, Any] = {"thread": threading.current_thread().name}
        try:
            k32 = _kernel32()
            info = _Throttling(POWER_THROTTLING_VERSION, EXECUTION_SPEED, 0)
            ok = bool(k32.SetThreadInformation(k32.GetCurrentThread(), THREAD_POWER_THROTTLING, ctypes.byref(info),
                                               ctypes.sizeof(info)))
            facts["throttling"] = "off" if ok else f"error {ctypes.get_last_error()}"
        except (AttributeError, OSError) as exc:
            facts["throttling"] = type(exc).__name__
        if mmcss:
            try:
                index = wintypes.DWORD(0)
                handle = _avrt().AvSetMmThreadCharacteristicsW(self.task, ctypes.byref(index))
                if handle:
                    self._mmcss = handle
                    facts["mmcss"] = self.task
                else:
                    facts["mmcss"] = f"error {ctypes.get_last_error()}"
            except (AttributeError, OSError) as exc:
                facts["mmcss"] = type(exc).__name__
        self.facts = facts
        return facts

    def revert(self) -> dict[str, Any]:
        """Undo :meth:`apply` (on the same thread): leave the MMCSS task, throttling back to Windows."""
        facts: dict[str, Any] = {"thread": threading.current_thread().name}
        if self.thread and threading.get_ident() != self.thread:
            return {**facts, "skipped": "another thread"}
        handle, self._mmcss = self._mmcss, None
        if handle:
            try:
                facts["mmcss"] = "left" if _avrt().AvRevertMmThreadCharacteristics(handle) else "error"
            except (AttributeError, OSError) as exc:
                facts["mmcss"] = type(exc).__name__
        try:
            k32 = _kernel32()
            info = _Throttling(POWER_THROTTLING_VERSION, 0, 0)
            ok = bool(k32.SetThreadInformation(k32.GetCurrentThread(), THREAD_POWER_THROTTLING, ctypes.byref(info),
                                               ctypes.sizeof(info)))
            facts["throttling"] = "default" if ok else "error"
        except (AttributeError, OSError) as exc:
            facts["throttling"] = type(exc).__name__
        return facts


#: Threads already on time once (by thread id), for callers that run on threads they do not own (the microphone's
#: callback thread, made by PortAudio): applied once per thread, never reverted (it ends with its stream).
_applied: dict[int, dict[str, Any]] = {}


def ensure_thread(task: str = MMCSS_TASK, *, mmcss: bool = True) -> dict[str, Any] | None:
    """:class:`ThreadQos` applied to the calling thread once (``None``: done before). Never raises."""
    ident = threading.get_ident()
    if ident in _applied:
        return None
    try:
        facts = ThreadQos(task).apply(mmcss=mmcss)
    except Exception as exc:  # noqa: BLE001 - scheduling hints never break audio
        facts = {"error": type(exc).__name__}
    _applied[ident] = facts
    if len(_applied) > 64:
        for old in list(_applied)[:32]:
            _applied.pop(old, None)
    return facts


__all__ = ["ThreadQos", "ensure_thread", "process_default_qos", "process_high_qos", "MMCSS_TASK"]
