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


def _enable_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware (best effort, idempotent).

    Returns the name of the mechanism that succeeded, or ``"already-set"`` when
    awareness was configured earlier in the process (for example by a library
    that was imported first).  Never raises: on an old Windows build we simply
    fall back to the older APIs.
    """
    user32 = ctypes.windll.user32
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            return "SetProcessDpiAwarenessContext"
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
    return "already-set"


DPI_AWARENESS = _enable_dpi_awareness()

__all__ = ["__version__", "DPI_AWARENESS"]
