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

from yuki.perception.screenshot import capture_bounds, screenshot
from yuki.perception.system import format_system_facts, system_facts
from yuki.perception.tree import UIElement, WindowTree, format_window_tree, get_window_tree
from yuki.perception.windows import (
    DesktopOverview,
    WindowInfo,
    cursor_position,
    format_overview,
    get_desktop_overview,
    is_user_window,
    list_windows,
    screen_size,
    virtual_screen_bounds,
    window_info,
)

__all__ = [
    "DesktopOverview",
    "UIElement",
    "WindowInfo",
    "WindowTree",
    "capture_bounds",
    "cursor_position",
    "format_overview",
    "format_system_facts",
    "format_window_tree",
    "get_desktop_overview",
    "get_window_tree",
    "is_user_window",
    "list_windows",
    "screen_size",
    "screenshot",
    "system_facts",
    "virtual_screen_bounds",
    "window_info",
]
