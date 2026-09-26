"""DWM system backdrop for the Yuki window (Windows 11), with a clear "no" when it cannot.

Windows 11 22H2 (build 22621) and later paint Acrylic or Mica behind a window
when asked through ``DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)``. The blur
is DWM's work, done in the compositor: this process pays nothing for it. The
client area must be transparent where the backdrop should show, so the frame is
extended into it (``DwmExtendFrameIntoClientArea`` with -1 margins) and the Qt
window paints only a tint (:func:`yuki.ui.window.glass.paint_backdrop` with
``dwm=True``).

:func:`apply` returns False (and changes nothing) when the build is older, the
user turned transparency effects off, or any call fails; the window then paints
its opaque fallback. Never called offscreen (ARCHITECTURE.md rule 7: the
mockups paint a stand-in backdrop instead).
"""

from __future__ import annotations

import ctypes
import sys
import winreg
from ctypes import wintypes

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
BACKDROPS = {"auto": 0, "none": 1, "mica": 2, "acrylic": 3, "tabbed": 4}
MIN_BUILD = 22621


class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


def _personalize(name: str, default: int) -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return int(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return default


def transparency_enabled() -> bool:
    """Settings > Personalization > Colors > Transparency effects."""
    return bool(_personalize("EnableTransparency", 1))


def system_prefers_dark() -> bool:
    """Settings > Personalization > Colors > "Choose your default app mode" is Dark."""
    return not bool(_personalize("AppsUseLightTheme", 1))


def supported() -> bool:
    return sys.platform == "win32" and sys.getwindowsversion().build >= MIN_BUILD


def apply(hwnd: int, kind: str = "acrylic", *, dark: bool) -> bool:
    """Ask DWM for ``kind`` ("acrylic" | "mica") behind ``hwnd``; True when it took."""
    if not supported() or not transparency_enabled():
        return False
    try:
        dwm = ctypes.windll.dwmapi
        handle = wintypes.HWND(hwnd)

        def set_int(attr: int, value: int) -> int:
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(handle, attr, ctypes.byref(v), ctypes.sizeof(v))

        set_int(DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        set_int(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        margins = _MARGINS(-1, -1, -1, -1)
        if dwm.DwmExtendFrameIntoClientArea(handle, ctypes.byref(margins)) != 0:
            return False
        return set_int(DWMWA_SYSTEMBACKDROP_TYPE, BACKDROPS.get(kind, 3)) == 0
    except Exception:
        return False
