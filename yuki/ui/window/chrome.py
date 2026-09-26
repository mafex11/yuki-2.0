"""A frameless window that still behaves like a Windows 11 window: drag, resize, Snap, snap layouts.

Qt's ``FramelessWindowHint`` removes the caption and the resize frame, and with
them Aero Snap, the snap-layout flyout, the minimise/maximise animations and
DWM's shadow. The window takes them back the way native apps do:

* the ``WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX`` styles are
  added back after Qt created the window (so Windows treats it as a normal
  resizable, snappable window);
* ``WM_NCCALCSIZE`` returns 0, so that frame takes no room: the client area is
  the whole window (inset by the frame when maximised, or it would spill off
  the monitor);
* ``WM_NCHITTEST`` says what is under the pointer (:func:`hit_test`): the
  resize edges, the caption (empty parts of the top band and the sidebar's
  head: drag to move, double-click to maximise, drag to an edge to snap), the
  maximise button (``HTMAXBUTTON``: Windows 11 shows its snap layouts on hover),
  and the client everywhere else;
* ``DwmExtendFrameIntoClientArea`` by one pixel keeps DWM's shadow, and
  ``DWMWA_WINDOW_CORNER_PREFERENCE`` asks for Windows 11's rounded corners.

Offscreen (tests, renders) none of this runs; :func:`hit_test` is plain maths.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCLBUTTONDBLCLK = 0x00A3
WM_NCMOUSEMOVE = 0x00A0
WM_NCMOUSELEAVE = 0x02A2
WM_SETTINGCHANGE = 0x001A
WM_DPICHANGED = 0x02E0

HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0004, 0x0010
SPI_SETDESKWALLPAPER = 0x0014

#: Width of the resize edge, in logical px.
EDGE = 6


def hit_test(x: float, y: float, w: float, h: float, *, maximized: bool, caption: Callable[[float, float], bool],
             max_button: Callable[[float, float], bool] | None = None, edge: float = EDGE) -> int:
    """What is at window point (x, y) (logical px) for ``WM_NCHITTEST``.

    ``caption(x, y)``: the point is on a draggable surface (no control under it);
    ``max_button(x, y)``: it is on the window's own maximise button.
    """
    if x < 0 or y < 0 or x >= w or y >= h:
        return 0   # HTNOWHERE
    if not maximized:
        left, right, top, bottom = x < edge, x >= w - edge, y < edge, y >= h - edge
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
    if max_button is not None and max_button(x, y):
        return HTMAXBUTTON
    if caption(x, y):
        return HTCAPTION
    return HTCLIENT


class _MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


def _user32():
    return ctypes.windll.user32


def make_native_frame(hwnd: int) -> bool:
    """Give a Qt frameless window back its Windows frame behaviour (see the module docstring)."""
    if sys.platform != "win32":
        return False
    try:
        u = _user32()
        get = u.GetWindowLongPtrW
        get.restype = ctypes.c_ssize_t
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        put = u.SetWindowLongPtrW
        put.restype = ctypes.c_ssize_t
        put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        style = get(hwnd, GWL_STYLE)
        put(hwnd, GWL_STYLE, style | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        dwm = ctypes.windll.dwmapi
        margins = _MARGINS(0, 0, 1, 0)
        dwm.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
        pref = ctypes.c_int(2)   # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        u.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                       SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def frame_thickness(hwnd: int) -> tuple[int, int]:
    """(x, y) thickness of the resize frame in physical px (how far a maximised window spills over)."""
    try:
        u = _user32()
        dpi = u.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
        pad = u.GetSystemMetricsForDpi(92, dpi)          # SM_CXPADDEDBORDER
        return u.GetSystemMetricsForDpi(32, dpi) + pad, u.GetSystemMetricsForDpi(33, dpi) + pad   # SM_CXFRAME/CYFRAME
    except Exception:
        return 8, 8


def is_maximized(hwnd: int) -> bool:
    try:
        return bool(_user32().IsZoomed(wintypes.HWND(hwnd)))
    except Exception:
        return False


def on_nccalcsize(hwnd: int, wparam: int, lparam: int) -> int | None:
    """``WM_NCCALCSIZE``: no non-client area (inset by the frame when maximised). None: not handled."""
    if not wparam:
        return None
    if is_maximized(hwnd):
        params = _NCCALCSIZE_PARAMS.from_address(lparam)
        fx, fy = frame_thickness(hwnd)
        r = params.rgrc[0]
        r.left += fx
        r.right -= fx
        r.top += fy
        r.bottom -= fy
    return 0


def lparam_point(lparam: int) -> tuple[int, int]:
    """Screen point (physical px) packed in an ``LPARAM`` (signed 16-bit halves)."""
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    return x, y


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = wintypes.POINT(x, y)
    _user32().ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt))
    return pt.x, pt.y


def message(msg_ptr: int) -> tuple[int, int, int, int]:
    """(hwnd, message, wParam, lParam) of the ``MSG`` Qt hands to ``nativeEvent``."""
    m = wintypes.MSG.from_address(int(msg_ptr))
    return int(m.hWnd or 0), int(m.message), int(m.wParam or 0), int(m.lParam or 0)
