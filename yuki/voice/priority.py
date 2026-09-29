"""The process's scheduling priority while a conversation is live (docs/VOICE.md v3.14b, "no dead air").

Measured 2026-09-29 on this PC with every core busy (other programs; run queue 17 on 16 logical CPUs): the voice loop
stalled 2.5-3.0 s while the whole process got under 100 ms of CPU (``stallwatch``: no thread of ours was busy, the PC
was), and a probe loop beside three busy Python threads woke up late by p99 67-87 ms / max 99-122 ms at normal
priority against p99 8-11 ms / max 9-20 ms at above-normal. So while the user is talking with Yuki the process runs
at ``ABOVE_NORMAL_PRIORITY_CLASS``, and goes back to the class it had when the conversation ends.

What that costs: nothing when the PC is idle (priority is only an order in the run queue); under contention Yuki's
threads run before normal-priority work of other programs, for the seconds a conversation lasts, at the ~7-12 % of
one core a conversation uses (measured in the replays). The whole process rather than the voice thread: its threads
share one interpreter lock, and a normal-priority thread holding it would make a raised loop wait anyway.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any

NORMAL_PRIORITY_CLASS = 0x0020
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000

_lock = threading.Lock()
_holders = 0
_previous: int | None = None


def _k32() -> Any:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.GetPriorityClass.restype = ctypes.c_uint32
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return k32


def boost() -> dict[str, Any]:
    """Raise the process to above-normal (counted: nested calls are fine). Returns facts for the log."""
    global _holders, _previous
    with _lock:
        _holders += 1
        if _holders > 1:
            return {"op": "boost", "already": True}
        try:
            k32 = _k32()
            current = int(k32.GetPriorityClass(k32.GetCurrentProcess()))
            if current not in (0, NORMAL_PRIORITY_CLASS):
                _previous = None   # someone chose another class on purpose (higher, or idle): left as it is
                return {"op": "boost", "left": hex(current)}
            _previous = current
            ok = bool(k32.SetPriorityClass(k32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS))
            return {"op": "boost", "ok": ok, "from": hex(current)}
        except (AttributeError, OSError) as exc:
            return {"op": "boost", "error": type(exc).__name__}


def restore() -> dict[str, Any]:
    """Back to the class it had, once the last holder is done."""
    global _holders, _previous
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders or _previous is None:
            return {"op": "restore", "held": _holders}
        try:
            k32 = _k32()
            ok = bool(k32.SetPriorityClass(k32.GetCurrentProcess(), _previous))
            facts = {"op": "restore", "ok": ok, "to": hex(_previous)}
            _previous = None
            return facts
        except (AttributeError, OSError) as exc:
            return {"op": "restore", "error": type(exc).__name__}


__all__ = ["boost", "restore"]
