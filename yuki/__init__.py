"""Yuki - a hands-free Windows assistant.

Importing :mod:`yuki` makes the process DPI aware before any window geometry is
read or any synthetic input is sent.  Without this, Windows virtualises window
rectangles and cursor coordinates on scaled displays and every click lands in
the wrong place.

The eyes (:mod:`yuki.perception`) and hands (:mod:`yuki.actions`) are kept in
separate subpackages; nothing in them imports the agent layer.
"""

from __future__ import annotations

import ctypes

__version__ = "0.1.0"

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def _is_per_monitor_v2() -> bool:
    """Whether this process already runs per-monitor DPI aware V2 (pure query).

    ``GetDpiAwarenessContextForProcess`` (Windows 10 1803+) is asked first; the
    thread's context is the fallback, which equals the process default unless
    something deliberately changed the thread.
    """
    user32 = ctypes.windll.user32
    try:
        equal = user32.AreDpiAwarenessContextsEqual
        equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        equal.restype = ctypes.c_int
        if hasattr(user32, "GetDpiAwarenessContextForProcess"):
            query = user32.GetDpiAwarenessContextForProcess
            query.argtypes = [ctypes.c_void_p]
            query.restype = ctypes.c_void_p
            context = query(None)
        else:
            query = user32.GetThreadDpiAwarenessContext
            query.argtypes = []
            query.restype = ctypes.c_void_p
            context = query()
        return bool(
            equal(
                ctypes.c_void_p(context),
                ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2),
            )
        )
    except Exception:  # pragma: no cover - APIs missing on very old Windows
        return False


def _enable_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware V2 (best effort, idempotent).

    Returns the name of the mechanism that succeeded, ``"already-per-monitor-v2"``
    when the process was already in that mode (a library imported first, or the
    executable's manifest), or ``"not-per-monitor-v2"`` when awareness had been
    fixed to something weaker before we got here and cannot be changed.  Never
    raises: on an old Windows build we fall back to the older APIs.
    """
    if _is_per_monitor_v2():
        return "already-per-monitor-v2"
    user32 = ctypes.windll.user32
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            return "SetProcessDpiAwarenessContext"
        # Refused: awareness was already fixed for this process.  Whatever it was
        # set to is what we have; the older APIs cannot change it either.
        return "not-per-monitor-v2"
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "SetProcessDpiAwareness"
    except Exception:  # pragma: no cover - shcore missing on very old Windows
        pass
    try:
        if user32.SetProcessDPIAware():
            return "SetProcessDPIAware"
    except Exception:  # pragma: no cover
        pass
    return "not-per-monitor-v2"


DPI_AWARENESS = _enable_dpi_awareness()

__all__ = ["__version__", "DPI_AWARENESS"]
