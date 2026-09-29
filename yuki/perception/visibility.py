"""What is on the screen: every top-level surface in Z-order, and how much of each is not covered.

The desktop overview (:mod:`yuki.perception.windows`) lists the windows a user
can switch to.  This module answers the question the memory watcher and the
live "Now" picture need: which of them - and which popups, menus, dialogs and
notification toasts - can the user actually *see* right now, and how much of
each.  Everything comes from OS window facts (``EnumWindows`` Z-order, window
styles, DWM cloaking and frame bounds, monitor rectangles); nothing here reads
pixels, looks at a process name or reads a title's content.

* A **window** is one :func:`~yuki.perception.windows.is_user_window` accepts,
  not minimised.
* A **popup** is any other visible, uncloaked top-level window with a real size
  on a monitor that is not one of the OS's own shell surfaces and not
  click-through (``WS_EX_TRANSPARENT``): menus, drop-downs, tooltips, toasts,
  flyouts, dialogs without a title.  Popups are read only when they appear (the
  watcher), never swept.
* **Visible share**: the part of a surface's on-monitor area that no surface
  above it in Z-order covers, measured on a :data:`CELL` px grid of the virtual
  desktop (about 25k cells here; a few milliseconds).  Click-through windows and
  the shell's own surfaces (desktop, taskbar) cover nothing.

Read-only: window enumeration and property reads only.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import win32con
import win32gui
import win32process

from yuki.perception.windows import (
    _SHELL_WINDOW_CLASSES,
    cloak_state,
    is_user_window,
    list_monitors,
    owner_of,
)

#: Grid cell (px) of the occlusion measure.
CELL = 16
#: A popup smaller than this (px) each way is not a surface anyone reads (a
#: caret host, an input sink, a 16x16 event window).
POPUP_MIN_W = 48
POPUP_MIN_H = 20
#: Below this visible share a window counts as covered.
VISIBLE_MIN_SHARE = 0.03

_dwm = ctypes.windll.dwmapi
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOPMOST = 0x00000008
_WS_EX_NOACTIVATE = 0x08000000


@dataclass
class Surface:
    """One top-level window on screen, with how much of it can be seen."""

    hwnd: int
    pid: int
    #: ``window`` (a user window) or ``popup`` (see the module docstring).
    kind: str
    title: str
    #: What DWM draws (extended frame bounds: no invisible resize borders), screen px.
    rect: tuple[int, int, int, int]
    #: Position in Z-order among the listed surfaces, 0 = top.
    z: int
    is_foreground: bool
    #: Share of its on-monitor area no surface above it covers (0..1).
    visible_share: float
    #: Visible area in grid cells of :data:`CELL` px.
    visible_cells: int
    topmost: bool = False
    #: ``GetWindow(GW_OWNER)``, 0 for none.
    owner: int = 0
    window_class: str = ""


@dataclass
class Layout:
    """Every surface on screen at one instant, top first."""

    surfaces: list[Surface] = field(default_factory=list)
    foreground: int = 0
    taken_at: float = 0.0
    elapsed_ms: float = 0.0
    #: The monitors' rectangles the shares were measured against.
    monitors: list[tuple[int, int, int, int]] = field(default_factory=list)
    #: Per surface hwnd: the cell grid of what was covered *above* it (for :meth:`covered`).
    _above: dict[int, bytearray] = field(default_factory=dict, repr=False)
    _origin: tuple[int, int] = (0, 0)
    _cols: int = 0
    _rows: int = 0

    def by_hwnd(self, hwnd: int) -> Surface | None:
        return next((s for s in self.surfaces if s.hwnd == int(hwnd)), None)

    def visible(self, *, kinds: tuple[str, ...] = ("window", "popup"),
                min_share: float = VISIBLE_MIN_SHARE) -> list[Surface]:
        """The surfaces that can be seen at all (share at least ``min_share``), top first."""
        return [s for s in self.surfaces if s.kind in kinds and s.visible_share >= min_share]

    def covered(self, hwnd: int, rect: tuple[int, int, int, int] | None) -> bool:
        """Whether ``rect`` (screen px) lies wholly under surfaces above ``hwnd`` or off every monitor.

        For leaving hidden parts of a background window out of a read of it:
        an element there cannot be seen, whatever UI Automation says.  Unknown
        windows and rectangles are never called covered.
        """
        grid = self._above.get(int(hwnd))
        if grid is None or rect is None:
            return False
        c0, r0, c1, r1 = self._cells(rect)
        if c1 <= c0 or r1 <= r0:
            return True  # nothing of it is on the measured desktop
        for row in range(r0, r1):
            if 0 in grid[row * self._cols + c0: row * self._cols + c1]:
                return False
        return True

    def _cells(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        ox, oy = self._origin
        c0 = max((rect[0] - ox) // CELL, 0)
        r0 = max((rect[1] - oy) // CELL, 0)
        c1 = min((rect[2] - ox + CELL - 1) // CELL, self._cols)
        r1 = min((rect[3] - oy + CELL - 1) // CELL, self._rows)
        return c0, r0, c1, r1


def frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """What DWM draws of a window (``DWMWA_EXTENDED_FRAME_BOUNDS``), else ``GetWindowRect``."""
    rect = wintypes.RECT()
    try:
        if _dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
                                      ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            if rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    return (left, top, right, bottom) if right > left and bottom > top else None


def _classify(hwnd: int, rect: tuple[int, int, int, int]) -> tuple[str, int, bool] | None:
    """``(kind, ex_style, occludes)`` for a visible top-level window, or ``None`` when it is not on screen."""
    try:
        if win32gui.IsIconic(hwnd) or cloak_state(hwnd):
            return None
        window_class = win32gui.GetClassName(hwnd)
        if window_class in _SHELL_WINDOW_CLASSES:
            return None
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0xFFFFFFFF
    except Exception:
        return None
    if ex_style & _WS_EX_TRANSPARENT:
        return None  # click-through: an overlay drawn over others, covering nothing
    if is_user_window(hwnd):
        return "window", ex_style, True
    if rect[2] - rect[0] < POPUP_MIN_W or rect[3] - rect[1] < POPUP_MIN_H:
        return None
    return "popup", ex_style, True


def layout(*, foreground: int | None = None) -> Layout:
    """Every window and popup on screen now, top first, with its visible share.  Never raises."""
    started = time.perf_counter()
    try:
        fg = int(foreground if foreground is not None else (ctypes.windll.user32.GetForegroundWindow() or 0))
    except Exception:
        fg = 0
    try:
        monitors = [m.bounds for m in list_monitors()]
    except Exception:
        monitors = []
    out = Layout(foreground=fg, taken_at=time.time(), monitors=list(monitors))
    if not monitors:
        out.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return out
    left = min(m[0] for m in monitors)
    top = min(m[1] for m in monitors)
    right = max(m[2] for m in monitors)
    bottom = max(m[3] for m in monitors)
    cols = max((right - left + CELL - 1) // CELL, 1)
    rows = max((bottom - top + CELL - 1) // CELL, 1)
    out._origin, out._cols, out._rows = (left, top), cols, rows
    # 1 = not a pixel of any monitor (the gap between monitors of different sizes), or covered.
    grid = bytearray(b"\x01" * (cols * rows))
    for m in monitors:
        c0, r0 = (m[0] - left) // CELL, (m[1] - top) // CELL
        c1, r1 = min((m[2] - left + CELL - 1) // CELL, cols), min((m[3] - top + CELL - 1) // CELL, rows)
        for row in range(r0, r1):
            grid[row * cols + c0: row * cols + c1] = b"\x00" * (c1 - c0)
    on_monitor = bytes(grid)

    handles: list[int] = []

    def collect(hwnd: int, _extra: object) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd):
                handles.append(int(hwnd))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        pass
    z = 0
    for hwnd in handles:
        rect = frame_bounds(hwnd)
        if rect is None:
            continue
        kind_style = _classify(hwnd, rect)
        if kind_style is None:
            continue
        kind, ex_style, occludes = kind_style
        c0, r0, c1, r1 = out._cells(rect)
        total = free = 0
        for row in range(r0, r1):
            span = on_monitor[row * cols + c0: row * cols + c1]
            total += (c1 - c0) - span.count(1)
            free += grid[row * cols + c0: row * cols + c1].count(0)
        if total <= 0:
            continue  # nowhere on a monitor
        out._above[hwnd] = bytearray(grid)  # what covers it: everything above, before it is marked
        if occludes:
            for row in range(r0, r1):
                grid[row * cols + c0: row * cols + c1] = b"\x01" * (c1 - c0)
        try:
            title = win32gui.GetWindowText(hwnd)
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            window_class = win32gui.GetClassName(hwnd)
        except Exception:
            continue
        out.surfaces.append(Surface(
            hwnd=hwnd, pid=pid, kind=kind, title=title, rect=rect, z=z, is_foreground=hwnd == fg,
            visible_share=round(free / total, 3), visible_cells=free,
            topmost=bool(ex_style & _WS_EX_TOPMOST), owner=owner_of(hwnd), window_class=window_class,
        ))
        z += 1
    out.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return out


def visible_shares(
    rects: list[tuple[int, int, int, int]], monitors: list[tuple[int, int, int, int]], *, cell: int = CELL
) -> list[float]:
    """The occlusion measure alone, for rectangles given top first (tests and offline checks)."""
    if not monitors:
        return [0.0 for _ in rects]
    left, top = min(m[0] for m in monitors), min(m[1] for m in monitors)
    right, bottom = max(m[2] for m in monitors), max(m[3] for m in monitors)
    cols, rows = max((right - left + cell - 1) // cell, 1), max((bottom - top + cell - 1) // cell, 1)
    grid = bytearray(b"\x01" * (cols * rows))
    for m in monitors:
        c0, r0 = (m[0] - left) // cell, (m[1] - top) // cell
        c1, r1 = min((m[2] - left + cell - 1) // cell, cols), min((m[3] - top + cell - 1) // cell, rows)
        for row in range(r0, r1):
            grid[row * cols + c0: row * cols + c1] = b"\x00" * (c1 - c0)
    on_monitor = bytes(grid)
    shares: list[float] = []
    for rect in rects:
        c0, r0 = max((rect[0] - left) // cell, 0), max((rect[1] - top) // cell, 0)
        c1, r1 = min((rect[2] - left + cell - 1) // cell, cols), min((rect[3] - top + cell - 1) // cell, rows)
        total = free = 0
        for row in range(r0, r1):
            total += (c1 - c0) - on_monitor[row * cols + c0: row * cols + c1].count(1)
            free += grid[row * cols + c0: row * cols + c1].count(0)
            grid[row * cols + c0: row * cols + c1] = b"\x01" * max(c1 - c0, 0)
        shares.append(round(free / total, 3) if total > 0 else 0.0)
    return shares


__all__ = ["CELL", "Layout", "Surface", "VISIBLE_MIN_SHARE", "frame_bounds", "layout", "visible_shares"]
