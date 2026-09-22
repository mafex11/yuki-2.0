"""Screenshots of a window, a region, one monitor, or the whole desktop.

A window shot is taken with ``PrintWindow(PW_RENDERFULLCONTENT)`` so an
occluded or partially covered window still captures correctly.  Providers that
refuse to render into a DC (some GPU-composited surfaces) come back blank; that
is detected and the shot falls back to cropping the desktop.

**Captures are 1:1 by default.**  They used to be downscaled to 1280 px wide,
which turned every coordinate the reader saw into a sum it had to do before it
could click: multiply by the inverse scale, add the window's origin, and hope.
On 2026-09-22 that arithmetic missed Spotify's search field three times running,
by 30-100 px each time.  So a shot is now taken at native resolution unless it is
enormous (see ``max_width``), and every capture reports the two numbers that make
a pixel in the image into a point on the screen: where its top-left corner sits in
virtual-desktop coordinates, and the scale it was saved at.  At scale 1.0 - the
normal case - there is no arithmetic left except adding the origin, and for a
whole-monitor or whole-desktop shot not even that.
"""

from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass

import win32gui
import win32ui
from PIL import Image, ImageGrab

import yuki  # noqa: F401  (imported for the DPI-awareness side effect)
from yuki.perception.windows import list_monitors, virtual_screen_bounds

_user32 = ctypes.windll.user32

_PW_RENDERFULLCONTENT = 0x00000002
_DWMWA_EXTENDED_FRAME_BOUNDS = 9

#: Longest edge a capture is saved at before it is scaled down.  Generous on
#: purpose: this desktop's larger monitor is 2560x1440 and a full-screen window on
#: it is 2576 px wide, so a single window or a single monitor always comes back
#: 1:1 and only the whole 4480 px virtual desktop is ever shrunk.  A PNG this size
#: costs a few hundred kilobytes, which is cheaper than a misplaced click.
DEFAULT_MAX_WIDTH = 2560


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass
class Capture:
    """A PNG plus everything needed to act on what it shows.

    Attributes:
        png: the image bytes.
        origin: top-left corner of what was captured, in virtual-desktop
            coordinates - the same space window bounds, element centres and
            :func:`yuki.actions.click` use.
        source_size: size in real screen pixels of the rectangle captured.
        image_size: size of the PNG, which differs from ``source_size`` only when
            ``scale`` is not 1.
        scale: image pixels per screen pixel.  1.0 means the image is 1:1.
        target: what was asked for, for the log ("window 12345", "monitor 1",
            "region", "desktop").
        rendered_by_window: True when the window painted itself into our bitmap
            (``PrintWindow``), False when the pixels were cropped out of the
            desktop instead.  The difference matters when a window is partly
            covered: a ``PrintWindow`` shot shows the window, a desktop crop shows
            whatever is on top of it.
    """

    png: bytes
    origin: tuple[int, int]
    source_size: tuple[int, int]
    image_size: tuple[int, int]
    scale: float
    target: str
    rendered_by_window: bool

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """The captured rectangle in virtual-desktop coordinates."""
        left, top = self.origin
        return (left, top, left + self.source_size[0], top + self.source_size[1])

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Turn a point in this image into a point on the screen.

        The one piece of arithmetic anyone should ever need, done here so no
        caller has to reinvent it.
        """
        return (
            int(round(self.origin[0] + x / self.scale)),
            int(round(self.origin[1] + y / self.scale)),
        )

    def mapping_text(self) -> str:
        """One line saying how to convert a point in this image to a screen point.

        Plain enough to act on without rereading: when the image is 1:1 and starts
        at the origin it says so and stops, because an instruction to add zero is
        an invitation to get it wrong.
        """
        width, height = self.image_size
        left, top = self.origin
        if self.scale == 1.0 and (left, top) == (0, 0):
            return (
                f"The image is {width}x{height} at 1:1 with the screen and starts at "
                f"the screen origin, so a point in this image IS the screen point - "
                f"click it as you read it, no conversion."
            )
        if self.scale == 1.0:
            return (
                f"The image is {width}x{height} at 1:1 with the screen, covering "
                f"({left},{top}) to ({left + width},{top + height}). To click "
                f"something here: screen_x = {left} + image_x, "
                f"screen_y = {top} + image_y."
            )
        source_width, source_height = self.source_size
        return (
            f"The image is {width}x{height}, a {self.scale:.4g}x scaling of the "
            f"{source_width}x{source_height} screen rectangle ({left},{top}) to "
            f"({left + source_width},{top + source_height}). To click something "
            f"here: screen_x = {left} + image_x / {self.scale:.4g}, "
            f"screen_y = {top} + image_y / {self.scale:.4g}."
        )


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


def _monitor_bounds(index: int) -> tuple[int, int, int, int]:
    """Rectangle of the monitor at ``index`` in :func:`list_monitors` order."""
    monitors = list_monitors()
    if not monitors:
        raise ValueError("no monitors are attached")
    for monitor in monitors:
        if monitor.index == index:
            return monitor.bounds
    available = ", ".join(str(m.index) for m in monitors)
    raise ValueError(f"no monitor {index}; attached monitors are {available}")


def capture_bounds(
    hwnd: int | None = None,
    *,
    region: tuple[int, int, int, int] | None = None,
    monitor: int | None = None,
) -> tuple[int, int, int, int]:
    """Screen rectangle a :func:`screenshot` of this target covers.

    The point of it is coordinate arithmetic: a window shot is rendered by the
    window itself, so its pixels are offsets inside this rectangle, not screen
    coordinates.  Anything wanting to click what it can see in the image has to
    add this origin back, and guessing the origin from ``GetWindowRect`` is wrong
    by the width of the invisible resize border.

    Args:
        hwnd: window handle.
        region: an explicit ``(left, top, right, bottom)`` in virtual-desktop
            coordinates.
        monitor: index of one monitor, as reported by
            :func:`yuki.perception.list_monitors`.

    Returns:
        ``(left, top, right, bottom)`` in virtual-desktop pixels.  With no target
        at all, the whole virtual desktop.

    Raises:
        ValueError: more than one target was given, or the target is not real.
    """
    given = [name for name, value in
             (("hwnd", hwnd), ("region", region), ("monitor", monitor))
             if value is not None]
    if len(given) > 1:
        raise ValueError(f"pass only one of hwnd/region/monitor, got {', '.join(given)}")
    if hwnd is not None:
        if not win32gui.IsWindow(hwnd):
            raise ValueError(f"not a window: hwnd={hwnd}")
        return _frame_bounds(hwnd)
    if monitor is not None:
        return _monitor_bounds(int(monitor))
    if region is not None:
        left, top, right, bottom = (int(v) for v in region)
        if right <= left or bottom <= top:
            raise ValueError(
                f"region {(left, top, right, bottom)} is empty; expected "
                f"(left, top, right, bottom) with right > left and bottom > top"
            )
        return (left, top, right, bottom)
    return virtual_screen_bounds()


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


def _grab(bounds: tuple[int, int, int, int]) -> Image.Image:
    """Pixels of a virtual-desktop rectangle, cropped out of the whole desktop."""
    return ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")


def capture(
    hwnd: int | None = None,
    *,
    region: tuple[int, int, int, int] | None = None,
    monitor: int | None = None,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> Capture:
    """Capture a target and report exactly where its pixels are.

    Exactly one target may be named.  With none, the whole virtual desktop is
    captured, which is the only view that includes every monitor.

    Args:
        hwnd: a window.  Captured by asking the window to render itself, so a
            covered window still comes out right; its own frame, nothing behind
            it.
        region: ``(left, top, right, bottom)`` in virtual-desktop coordinates -
            the coordinates window bounds and element centres are already in, so a
            region can be built straight from an element's ``bounds``.
        monitor: index from :func:`yuki.perception.list_monitors`.  Prefer this
            over the whole desktop: one monitor fits under ``max_width`` here, so
            it comes back 1:1.
        max_width: longest edge the image may have before it is scaled down.  The
            scale it was saved at is always reported; pass a huge number to forbid
            scaling entirely.

    Returns:
        A :class:`Capture`.

    Raises:
        ValueError: more than one target, an unreal target, a minimized window, or
            a rectangle with no area.
    """
    bounds = capture_bounds(hwnd, region=region, monitor=monitor)
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"nothing to capture: rectangle {bounds} has no area")

    rendered_by_window = False
    if hwnd is not None:
        if win32gui.IsIconic(hwnd):
            raise ValueError(
                f"hwnd={hwnd} is minimized; restore it (focus_window) before a screenshot"
            )
        # PrintWindow paints the *whole* window rectangle, invisible resize border
        # included, with the window rect's top-left at bitmap (0, 0).  The frame
        # we report as the origin sits a few pixels inside that, so render at the
        # full window size and crop to the frame: otherwise every pixel in the
        # image would be offset from the screen by the border width.
        w_left, w_top, w_right, w_bottom = win32gui.GetWindowRect(hwnd)
        image = _print_window(hwnd, w_right - w_left, w_bottom - w_top)
        if image is not None:
            image = image.crop(
                (left - w_left, top - w_top, right - w_left, bottom - w_top)
            )
        rendered_by_window = image is not None
        if image is None:
            # The provider would not draw into our DC: take the pixels that are
            # actually on screen instead, which is worth saying because anything
            # on top of the window is now in the shot.
            image = _grab(bounds)
        target = f"window {hwnd}"
    elif monitor is not None:
        image = _grab(bounds)
        target = f"monitor {int(monitor)}"
    elif region is not None:
        image = _grab(bounds)
        target = "region"
    else:
        image = _grab(bounds)
        target = "desktop"

    source_size = (image.width, image.height)
    scale = 1.0
    longest = max(image.width, image.height)
    if max_width > 0 and longest > max_width:
        scale = max_width / longest
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        # Report the scale the image actually has, not the one we asked for: the
        # rounding above is what a reader's arithmetic will be measured against.
        scale = image.width / source_size[0]

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return Capture(
        png=buffer.getvalue(),
        origin=(left, top),
        source_size=source_size,
        image_size=(image.width, image.height),
        scale=round(scale, 6),
        target=target,
        rendered_by_window=rendered_by_window,
    )


def screenshot(
    hwnd: int | None = None,
    *,
    region: tuple[int, int, int, int] | None = None,
    monitor: int | None = None,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> Capture:
    """Screenshot of a window, a region, one monitor, or the whole desktop.

    Native resolution by default; only an image whose longest edge exceeds
    ``max_width`` is scaled down, and the exact scale is reported.  The result is
    a :class:`Capture`: ``.png`` is the image, ``.origin`` and ``.scale`` map a
    point in it back to the screen (``Capture.to_screen``), 1:1 in the normal case.
    Same function as :func:`capture`.

    Raises:
        ValueError: more than one target, an unreal target, a minimized window, or
            a rectangle with no area.
    """
    return capture(hwnd, region=region, monitor=monitor, max_width=max_width)
