"""Yuki's eyes.

Four ways to look, cheapest first:

* :func:`get_desktop_overview` - every user window, ~3 ms.
* :func:`system_facts` - clock, load, memory, heaviest processes.
* :func:`get_window_tree` - UI Automation elements of one window.
* :func:`screenshot` - pixels, when UIA has nothing useful to say.

Everything returns plain dataclasses (or dicts) that survive
``dataclasses.asdict`` so the logger can write them verbatim.
"""

from __future__ import annotations

from yuki.perception.screenshot import Capture, capture, capture_bounds, screenshot
from yuki.perception.system import format_system_facts, system_facts
from yuki.perception.tree import (
    FocusInfo,
    UIElement,
    WindowTree,
    child_window_handles,
    focused_element,
    format_window_tree,
    get_window_tree,
)
from yuki.perception.windows import (
    DesktopOverview,
    MonitorInfo,
    WindowInfo,
    accepts_input,
    cursor_position,
    focused_control_hwnd,
    format_overview,
    get_desktop_overview,
    is_user_window,
    list_monitors,
    list_windows,
    monitor_at,
    primary_screen_size,
    screen_size,
    virtual_screen_bounds,
    window_info,
)

__all__ = [
    "Capture",
    "DesktopOverview",
    "FocusInfo",
    "MonitorInfo",
    "UIElement",
    "WindowInfo",
    "WindowTree",
    "accepts_input",
    "capture",
    "capture_bounds",
    "child_window_handles",
    "cursor_position",
    "format_overview",
    "format_system_facts",
    "focused_control_hwnd",
    "focused_element",
    "format_window_tree",
    "get_desktop_overview",
    "get_window_tree",
    "is_user_window",
    "list_monitors",
    "list_windows",
    "monitor_at",
    "primary_screen_size",
    "screen_size",
    "screenshot",
    "system_facts",
    "virtual_screen_bounds",
    "window_info",
]
