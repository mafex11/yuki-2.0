"""Screenshots of the whole screen or of a single window.

A window shot is taken with ``PrintWindow(PW_RENDERFULLCONTENT)`` so an
occluded or partially covered window still captures correctly.  Providers that
refuse to render into a DC (some GPU-composited surfaces) come back blank; that
is detected and the shot falls back to cropping the desktop.
"""

from __future__ import annotations

import ctypes
import io

import win32gui
import win32ui
from PIL import Image, ImageGrab

import yuki  # noqa: F401  (imported for the DPI-awareness side effect)

_user32 = ctypes.windll.user32

_PW_RENDERFULLCONTENT = 0x00000002
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame of a window, excluding the invisible resize border."""
    rect = _Rect()
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd),
        ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
        return (rect.left, rect.top, rect.right, rect.bottom)
    return tuple(win32gui.GetWindowRect(hwnd))  # type: ignore[return-value]


def _print_window(hwnd: int, width: int, height: int) -> Image.Image | None:
    """Ask the window to render itself into a bitmap.  ``None`` on failure."""
    window_dc = None
    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        ok = _user32.PrintWindow(
            ctypes.c_void_p(hwnd),
            memory_dc.GetSafeHdc(),
            ctypes.c_uint(_PW_RENDERFULLCONTENT),
        )
        if not ok:
            return None
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        if image.convert("L").getextrema() == (0, 0):
            return None  # fully black: the provider did not render
        return image
    except Exception:
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        try:
            if memory_dc is not None:
                memory_dc.DeleteDC()
            if source_dc is not None:
                source_dc.DeleteDC()
        except Exception:
            pass
        if window_dc:
            win32gui.ReleaseDC(hwnd, window_dc)


def _capture(hwnd: int | None) -> Image.Image:
    if hwnd is None:
        return ImageGrab.grab().convert("RGB")
    if not win32gui.IsWindow(hwnd):
        raise ValueError(f"not a window: hwnd={hwnd}")
    if win32gui.IsIconic(hwnd):
        raise ValueError(
            f"hwnd={hwnd} is minimized; restore it (focus_window) before a screenshot"
        )
    left, top, right, bottom = _frame_bounds(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"hwnd={hwnd} has no visible area to capture")
    image = _print_window(hwnd, width, height)
    if image is not None:
        return image
    # Fallback: crop the window rectangle out of the (multi-monitor) desktop.
    return ImageGrab.grab(
        bbox=(left, top, right, bottom), all_screens=True
    ).convert("RGB")


def screenshot(hwnd: int | None = None, *, max_width: int = 1280) -> bytes:
    """PNG bytes of one window, or of the primary screen when ``hwnd`` is None.

    The image is downscaled to ``max_width`` preserving aspect ratio.  Window
    shots use the window's own rendering, so coordinates in the full-size image
    are offsets inside the window frame; screen shots are in screen
    coordinates of the primary monitor.

    Raises:
        ValueError: the handle is not a window, is minimized, or has no area.
    """
    image = _capture(hwnd)
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()
