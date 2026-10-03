"""A thin WebView2 host in ctypes: one top-level window and one WebView2 in it. No .NET, no Qt, no web server.

The Yuki window is moving to HTML (the Lattice design) rendered by the WebView2 runtime that ships with Windows
(docs/research/revamp-2026-10-02/08-ui-inventory.md §7). This module is the whole native side of that window:

* a plain Win32 window (``CreateWindowExW``) with a graphite background, so nothing flashes white;
* the WebView2 environment and controller, made through the official ``WebView2Loader.dll`` and called through
  their COM vtables (the layouts are the ones in Microsoft's ``WebView2.h``; the indices below were read from the
  SDK's interop metadata, ``Microsoft.Web.WebView2.Core.Raw``, on 2026-10-03);
* the app's files served from disk by **virtual host mapping** (``https://app.yuki.example/`` ->
  ``yuki/ui/web``, ``https://fonts.yuki.example/`` -> ``yuki/ui/fonts``): no localhost port that every other
  process on the PC could read;
* data in and out over the **WebView2 message channel** only (``PostWebMessageAsJson`` /
  ``WebMessageReceived``), and only from our own origin;
* locked down: navigation anywhere but our origin is cancelled (an https link the user clicks opens in the default
  browser instead), new windows and downloads are refused, every permission request (microphone, camera,
  clipboard, location...) is denied, no dev tools, context menu, zoom, browser shortcuts, autofill or host
  objects. The page adds its own strict Content-Security-Policy.
* **frameless** (the Lattice design draws its own caption buttons): no Windows title bar (``WM_NCCALCSIZE``), the
  system's invisible resize borders (left, right, below) and shadow kept; ``WM_NCHITTEST`` answers the page's title
  bar from where the page says it drew it (:func:`caption_hit`: top edge resize, caption buttons as HTMINBUTTON /
  HTMAXBUTTON - snap layouts - / HTCLOSE, the rest of the bar HTCAPTION). WebView2 covers the client area and gets
  the pointer there first, so the page's own buttons and title bar also act through :meth:`WebView2Host.window_op`
  (the system's own move and size loops), and its ``app-region: drag`` is honoured where the runtime supports it.

Why not pywebview: its EdgeChromium backend loads the .NET runtime and WinForms into the process, routes every
resource request through a Python callback, shows its window with ``Form.Show()`` (which activates it) and keeps a
500 ms timer running; it has no virtual host mapping. Its ``WebView2Loader.dll`` is reused here (the official,
redistributable loader), nothing else of it is imported. See docs/research/revamp-2026-10-02/25-build-E1-E2-E5.md.

All COM calls happen on the thread that called :meth:`WebView2Host.run` (an STA with a Win32 message loop).
:meth:`WebView2Host.post` and :meth:`WebView2Host.close` are safe from any thread.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import platform
import queue
import sys
import time
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_long, c_longlong, c_ubyte, c_uint, c_ulong, c_ushort, \
    c_void_p, wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

#: The virtual hosts. ``.example`` is reserved (RFC 2606): it can never be a real site.
APP_HOST = "app.yuki.example"
FONT_HOST = "fonts.yuki.example"

#: The environment variable WebView2 reads extra Chromium switches from (documented by Microsoft; used for the
#: GPU experiments: ``--in-process-gpu``, ``--disable-gpu``).
BROWSER_ARGS_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"

# -- Win32 ------------------------------------------------------------------------------------------------

LRESULT = wintypes.LPARAM
WNDPROC = WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNORMAL, SW_SHOWMAXIMIZED, SW_SHOWNOACTIVATE, SW_RESTORE = 1, 3, 4, 9
WM_DESTROY, WM_MOVE, WM_SIZE, WM_CLOSE = 0x0002, 0x0003, 0x0005, 0x0010
WM_MOUSEACTIVATE, WM_DPICHANGED, WM_APP = 0x0021, 0x02E0, 0x8000
WM_ACTIVATE, WA_INACTIVE = 0x0006, 0
MA_NOACTIVATE = 3
SIZE_MINIMIZED = 1
CS_HREDRAW, CS_VREDRAW = 0x2, 0x1
COINIT_APARTMENTTHREADED = 0x2
#: Posted to the window when :meth:`WebView2Host.post` queued something for the page.
WM_YUKI_POST = WM_APP + 1
WM_YUKI_CLOSE = WM_APP + 2
WM_YUKI_CALL = WM_APP + 3

# -- the frameless window (revamp E3 difference 2, 32-build-E11-E14.md §6): the page draws the Lattice title bar ------
WM_NCCALCSIZE, WM_NCHITTEST, WM_NCMOUSEMOVE = 0x0083, 0x0084, 0x00A0
WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK, WM_NCMOUSELEAVE = 0x00A1, 0x00A2, 0x00A3, 0x02A2
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE = 0xF020, 0xF030, 0xF120
HTCLIENT, HTCAPTION, HTMINBUTTON, HTMAXBUTTON, HTCLOSE = 1, 2, 8, 9, 20
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
#: The border codes the system's own frame answers (left, right, bottom: the invisible resize borders kept outside
#: the client area); the frameless window lets them through untouched.
FRAME_HITS = frozenset({HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT})
#: The page's caption parts and the codes Windows knows them by (HTMAXBUTTON is what lets snap layouts appear).
CAPTION_PARTS = {"min": HTMINBUTTON, "max": HTMAXBUTTON, "close": HTCLOSE}
#: The design's title bar (``.titlebar``: 34 px high, 44 px buttons) until the page reports where it drew it.
CAPTION_CSS_PX = 34
#: The top edge the page leaves for resizing (CSS px), and how far in from each side a top corner reaches.
TOP_RESIZE_CSS_PX = 5
CORNER_CSS_PX = 12
SM_CXFRAME, SM_CYFRAME, SM_CXPADDEDBORDER = 32, 33, 92


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", c_void_p)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND),
                ("dwHoverTime", wintypes.DWORD)]


@dataclass
class CaptionLayout:
    """Where the page drew its title bar, in client pixels (physical): its height, the caption buttons (``min`` /
    ``max`` / ``close`` -> x, y, w, h) and the rectangles inside the bar that are page controls, not the drag area."""

    height: int = 0
    buttons: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    no_drag: list[tuple[int, int, int, int]] = field(default_factory=list)
    top: int = 0           # the top resize edge's height
    corner: int = 0        # how far in from each side a top corner reaches

    @classmethod
    def from_page(cls, msg: dict[str, Any], scale: float) -> "CaptionLayout":
        """The page's ``caption`` message (CSS px, ``dpr``) as client pixels; bad values are dropped."""
        k = float(msg.get("dpr") or scale or 1.0)

        def rect(v: Any) -> tuple[int, int, int, int] | None:
            if not isinstance(v, (list, tuple)) or len(v) != 4:
                return None
            try:
                x, y, w, h = (float(n) for n in v)
            except (TypeError, ValueError):
                return None
            return (round(x * k), round(y * k), round(w * k), round(h * k)) if w > 0 and h > 0 else None

        buttons = {}
        for part in CAPTION_PARTS:
            r = rect((msg.get("buttons") or {}).get(part)) if isinstance(msg.get("buttons"), dict) else None
            if r is not None:
                buttons[part] = r
        try:
            height = max(0, round(float(msg.get("h") or CAPTION_CSS_PX) * k))
        except (TypeError, ValueError):
            height = round(CAPTION_CSS_PX * k)
        return cls(height=height, buttons=buttons,
                   no_drag=[r for r in (rect(v) for v in msg.get("no_drag") or []) if r is not None],
                   top=round(TOP_RESIZE_CSS_PX * k), corner=round(CORNER_CSS_PX * k))


def _inside(x: int, y: int, r: tuple[int, int, int, int]) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


def caption_hit(x: int, y: int, width: int, layout: CaptionLayout, *, maximized: bool) -> int:
    """What a point of the client area is to Windows (``WM_NCHITTEST``) in the frameless window: the top edge resizes
    (corners too) unless maximized; the page's caption buttons are Windows' own minimize / maximize (snap layouts) /
    close; the rest of the title bar drags the window (double-click maximizes, right-click opens the system menu);
    everything else is the page. Pure: the window procedure feeds it client coordinates."""
    if not maximized and layout.top and 0 <= y < layout.top:
        if x < layout.corner:
            return HTTOPLEFT
        if x >= width - layout.corner:
            return HTTOPRIGHT
        return HTTOP
    for part, code in CAPTION_PARTS.items():
        r = layout.buttons.get(part)
        if r is not None and _inside(x, y, r):
            return code
    if 0 <= y < layout.height and not any(_inside(x, y, r) for r in layout.no_drag):
        return HTCAPTION
    return HTCLIENT


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", c_uint), ("style", c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int),
                ("cbWndExtra", c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", c_uint), ("flags", c_uint), ("showCmd", c_uint), ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT), ("rcNormalPosition", wintypes.RECT)]


def _on_a_monitor(rect: tuple[int, int, int, int]) -> bool:
    """Whether a remembered window rect still overlaps a monitor (one may have been unplugged)."""
    try:
        x, y, w, h = (int(v) for v in rect)
        if w < 400 or h < 300:
            return False
        r = wintypes.RECT(x, y, x + w, y + h)
        fn = ctypes.windll.user32.MonitorFromRect
        fn.argtypes = [POINTER(wintypes.RECT), c_uint]
        fn.restype = c_void_p
        return bool(fn(byref(r), 0))   # MONITOR_DEFAULTTONULL
    except Exception:
        return False


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


class Token(ctypes.Structure):
    """``EventRegistrationToken``."""
    _fields_ = [("value", c_longlong)]


class Color(ctypes.Structure):
    """``COREWEBVIEW2_COLOR``."""
    _fields_ = [("A", c_ubyte), ("R", c_ubyte), ("G", c_ubyte), ("B", c_ubyte)]


def _iid(text: str) -> bytes:
    return uuid.UUID(text).bytes_le


IID_IUNKNOWN = _iid("00000000-0000-0000-C000-000000000046")
IID_CONTROLLER2 = _iid("c979903e-d4ca-4228-92eb-47ee3fa96eab")
IID_WEBVIEW2_3 = _iid("a0d6df20-3b92-416d-aa0c-437a9c727857")
IID_WEBVIEW2_4 = _iid("20d02d59-6df2-42dc-bd06-f98a694b1302")
IID_WEBVIEW2_19 = _iid("6921f954-79b0-437f-a997-c85811897c68")
IID_SETTINGS3 = _iid("fdb5ab74-af33-4854-84f0-0a631deb5eba")
IID_SETTINGS4 = _iid("cb56846c-4168-4d53-b04f-03b6d6796ff2")
IID_SETTINGS5 = _iid("183e7052-1d03-43a0-ab99-98e043b66b39")
IID_SETTINGS6 = _iid("11cb3acd-9bc8-43b8-83bf-f40753714f87")
IID_SETTINGS9 = _iid("0528a73b-e92d-49f4-927a-e547dddaa37d")

#: Handler interfaces we implement: name -> (IID, Invoke's argument types after ``this``).
HANDLERS: dict[str, tuple[bytes, tuple[Any, ...]]] = {
    "environment_created": (_iid("4e8a3389-c9d8-4bd2-b6b5-124fee6cc14d"), (c_long, c_void_p)),
    "controller_created": (_iid("6c4819f3-c9b7-4260-8127-c9f5bde7f68c"), (c_long, c_void_p)),
    "navigation_starting": (_iid("9adbe429-f36d-432b-9ddc-f8881fbd76e3"), (c_void_p, c_void_p)),
    "navigation_completed": (_iid("d33a35bf-1c49-4f98-93ab-006e0533fe1c"), (c_void_p, c_void_p)),
    "web_message": (_iid("57213f19-00e6-49fa-8e07-898ea01ecbd2"), (c_void_p, c_void_p)),
    "permission": (_iid("15e1c6a3-c72a-4df3-91d7-d097fbec6bfd"), (c_void_p, c_void_p)),
    "new_window": (_iid("d4c185fe-c81c-4989-97af-2d3fa7ab5651"), (c_void_p, c_void_p)),
    "process_failed": (_iid("79e0aea4-990b-42d9-aa1d-0fcc2e5bc7f1"), (c_void_p, c_void_p)),
    "download_starting": (_iid("efedc989-c396-41ca-83f7-07f845a55724"), (c_void_p, c_void_p)),
    "capture_done": (_iid("697e05e9-3d8f-45fa-96f4-8ffe1ededaf5"), (c_long,)),
}

# vtable slots (IUnknown takes 0-2). From Microsoft.Web.WebView2.Core.Raw, declaration order.
ENV_CREATE_CONTROLLER, ENV_BROWSER_VERSION = 3, 5
CTL_PUT_IS_VISIBLE, CTL_PUT_BOUNDS, CTL_NOTIFY_MOVED, CTL_CLOSE, CTL_GET_WEBVIEW = 4, 6, 23, 24, 25
CTL2_PUT_BACKGROUND = 27
WV_GET_SETTINGS, WV_NAVIGATE = 3, 5
WV_ADD_NAVIGATION_STARTING, WV_ADD_NAVIGATION_COMPLETED = 7, 15
WV_ADD_PERMISSION_REQUESTED, WV_ADD_PROCESS_FAILED = 23, 25
WV_CAPTURE_PREVIEW, WV_POST_JSON, WV_ADD_WEB_MESSAGE = 30, 32, 34
WV_BROWSER_PID, WV_ADD_NEW_WINDOW = 37, 44
WV3_SET_VIRTUAL_HOST = 71
WV4_ADD_DOWNLOAD_STARTING = 75
WV19_PUT_MEMORY_TARGET = 120
SET_PUT_SCRIPT, SET_PUT_WEB_MESSAGE, SET_PUT_SCRIPT_DIALOGS, SET_PUT_STATUS_BAR = 4, 6, 8, 10
SET_PUT_DEV_TOOLS, SET_PUT_CONTEXT_MENUS, SET_PUT_HOST_OBJECTS, SET_PUT_ZOOM = 12, 14, 16, 18
SET3_PUT_ACCELERATOR_KEYS, SET4_PUT_PASSWORD_AUTOSAVE, SET4_PUT_AUTOFILL = 24, 26, 28
SET5_PUT_PINCH_ZOOM, SET6_PUT_SWIPE = 30, 32
#: ``put_IsNonClientRegionSupportEnabled`` (ICoreWebView2Settings9): the page's ``app-region: drag`` moves the window.
SET9_PUT_NON_CLIENT_REGION = 38
NAV_GET_URI, NAV_IS_USER_INITIATED, NAV_PUT_CANCEL = 3, 4, 8
NAVDONE_IS_SUCCESS, NAVDONE_ERROR_STATUS = 3, 4
MSG_GET_SOURCE, MSG_GET_JSON = 3, 4
PERM_GET_KIND, PERM_PUT_STATE = 4, 7
NEWWIN_GET_URI, NEWWIN_PUT_HANDLED = 3, 6
FAILED_GET_KIND = 3
DL_PUT_CANCEL, DL_PUT_HANDLED = 5, 9
HOST_ACCESS_ALLOW, HOST_ACCESS_DENY_CORS = 1, 2
PERMISSION_DENY = 2
MEMORY_TARGET_NORMAL, MEMORY_TARGET_LOW = 0, 1

E_NOINTERFACE = -2147467262   # 0x80004002


_DEF_WINDOW_PROC = ctypes.windll.user32.DefWindowProcW
_DEF_WINDOW_PROC.restype = LRESULT
_DEF_WINDOW_PROC.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


class Com:
    """A raw COM interface pointer: call vtable slot ``index`` with ctypes argument types."""

    __slots__ = ("ptr",)
    _protos: dict[tuple[Any, ...], Any] = {}

    def __init__(self, ptr: int | None) -> None:
        self.ptr = int(ptr or 0)

    def __bool__(self) -> bool:
        return bool(self.ptr)

    def call(self, index: int, argtypes: tuple[Any, ...] = (), *args: Any) -> int:
        vtbl = ctypes.cast(c_void_p(self.ptr), POINTER(POINTER(c_void_p))).contents
        key = (c_long, *argtypes)
        proto = Com._protos.get(key)
        if proto is None:
            proto = Com._protos[key] = WINFUNCTYPE(c_long, c_void_p, *argtypes)
        return int(proto(vtbl[index])(c_void_p(self.ptr), *args))

    def query(self, iid: bytes) -> "Com":
        """``QueryInterface``; an empty pointer when the runtime is too old for that interface."""
        out = c_void_p()
        guid = GUID.from_buffer_copy(iid)
        hr = self.call(0, (POINTER(GUID), POINTER(c_void_p)), byref(guid), byref(out))
        return Com(out.value if hr >= 0 else None)

    def release(self) -> None:
        if self.ptr:
            vtbl = ctypes.cast(c_void_p(self.ptr), POINTER(POINTER(c_void_p))).contents
            WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(c_void_p(self.ptr))
            self.ptr = 0

    def get_string(self, index: int) -> str:
        """A ``get_X(LPWSTR*)`` property (the string is freed with ``CoTaskMemFree``)."""
        out = c_void_p()
        _check(self.call(index, (POINTER(c_void_p),), byref(out)), f"string getter {index}")
        try:
            return ctypes.wstring_at(out.value) if out.value else ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)

    def get_int(self, index: int) -> int:
        out = c_int()
        _check(self.call(index, (POINTER(c_int),), byref(out)), f"int getter {index}")
        return out.value

    def put_bool(self, index: int, value: bool) -> None:
        _check(self.call(index, (c_int,), int(bool(value))), f"bool setter {index}")


class Handler:
    """A COM object we implement: IUnknown plus one ``Invoke``. Kept alive by the host for its whole run."""

    _QI = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
    _REF = WINFUNCTYPE(c_ulong, c_void_p)

    def __init__(self, kind: str, fn: Callable[..., Any], on_error: Callable[[str, BaseException], None]) -> None:
        self.iid, argtypes = HANDLERS[kind]
        self.kind = kind
        self._fn = fn
        self._on_error = on_error
        self._refs = 1
        invoke = WINFUNCTYPE(c_long, c_void_p, *argtypes)
        self._fns = (self._QI(self._query), self._REF(self._addref), self._REF(self._release),
                     invoke(self._invoke))
        self._vtbl = (c_void_p * 4)(*[ctypes.cast(f, c_void_p) for f in self._fns])
        self._obj = (c_void_p * 1)(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._obj)

    def _query(self, this: int, riid: Any, ppv: Any) -> int:
        if bytes(riid.contents) in (IID_IUNKNOWN, self.iid):
            ppv[0] = this
            self._refs += 1
            return 0
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, _this: int) -> int:
        self._refs += 1
        return self._refs

    def _release(self, _this: int) -> int:
        self._refs = max(1, self._refs - 1)   # we own the memory; never freed while the host lives
        return self._refs

    def _invoke(self, _this: int, *args: Any) -> int:
        try:
            self._fn(*args)
        except BaseException as exc:  # noqa: BLE001 - an exception must never unwind into the runtime
            self._on_error(self.kind, exc)
        return 0


def loader_path() -> Path:
    """``WebView2Loader.dll`` for this Python's architecture: ``YUKI_WEBVIEW2_LOADER``, the PyInstaller bundle's
    ``webview2/`` folder, or the copy inside the installed ``pywebview`` package (located, never imported)."""
    env = os.environ.get("YUKI_WEBVIEW2_LOADER")
    if env:
        return Path(env)
    arch = {"ARM64": "win-arm64"}.get(platform.machine().upper(),
                                       "win-x64" if sys.maxsize > 2 ** 32 else "win-x86")
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle and (Path(bundle) / "webview2" / "WebView2Loader.dll").exists():
        return Path(bundle) / "webview2" / "WebView2Loader.dll"
    spec = importlib.util.find_spec("webview")
    if spec is None or not spec.submodule_search_locations:
        raise FileNotFoundError("WebView2Loader.dll not found (install pywebview or set YUKI_WEBVIEW2_LOADER)")
    return Path(list(spec.submodule_search_locations)[0]) / "lib" / "runtimes" / arch / "native" / "WebView2Loader.dll"


def runtime_version(loader: Path | None = None) -> str | None:
    """The installed WebView2 runtime's version (``None``: not installed, so the window cannot open)."""
    try:
        dll = ctypes.WinDLL(str(loader or loader_path()))
        fn = dll.GetAvailableCoreWebView2BrowserVersionString
        fn.argtypes = [wintypes.LPCWSTR, POINTER(c_void_p)]
        fn.restype = c_long
        out = c_void_p()
        if fn(None, byref(out)) < 0 or not out.value:
            return None
        try:
            return ctypes.wstring_at(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except OSError:
        return None


def is_allowed_navigation(uri: str, app_host: str = APP_HOST) -> bool:
    """Only our own origin may load in the window (``https://app.yuki.example/...``)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == app_host


def is_external_link(uri: str) -> bool:
    """An ordinary web link the user may open in their browser (http or https, any other host)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and parts.hostname not in (APP_HOST, FONT_HOST)


@dataclass
class HostOptions:
    """What :class:`WebView2Host` opens."""

    folders: dict[str, Path]                 # virtual host -> folder
    start_url: str                           # must be on ``app_host``
    user_data_dir: Path                      # WebView2's profile folder
    title: str = "Yuki"
    size: tuple[int, int] = (1440, 900)      # logical px
    #: Where the window was when it last closed (x, y, w, h in physical px, ``ui.json`` ``window``) and whether it
    #: was maximized; used for the normal placement when the rect is still on a monitor.
    rect: tuple[int, int, int, int] | None = None
    maximized: bool = False
    #: ``normal``: the window, shown and brought forward (the tray let this process take the foreground);
    #: ``offscreen``: a tool window beyond the left edge of every monitor, shown without activation and refusing
    #: activation (measurements and tests: it never takes focus and is never seen; with ``frameless`` it has the real
    #: frame, so its hit testing can be measured; the page's window ops are refused there).
    placement: str = "normal"
    #: The Lattice frame: no Windows title bar (the page draws its own caption buttons), the system's resize borders
    #: and shadow kept, the page's title bar answered to Windows as caption / buttons (:func:`caption_hit`).
    frameless: bool = True
    background: tuple[int, int, int] = (0x0B, 0x0B, 0x0C)
    browser_args: str | None = None          # extra Chromium switches (``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``)
    dev_tools: bool = False
    app_host: str = APP_HOST
    on_message: Callable[[Any], None] | None = None   # each JSON message the page posts (parsed)
    on_ready: Callable[["WebView2Host"], None] | None = None   # navigation to the start page began
    on_event: Callable[..., None] | None = None        # ``on_event(kind, **fields)``: content-free log lines
    #: ``on_shown(reason)`` on the UI thread when the window can be seen again: ``restore`` (from minimised; once per
    #: edge, ``WM_SIZE`` also fires on every resize) or ``activate`` (brought to the front from behind other windows:
    #: WebView2 changes nothing there, so the page hears no ``visibilitychange``; 42 verification, missing 5).
    on_shown: Callable[[str], None] | None = None
    marks: dict[str, float] = field(default_factory=dict)   # epoch times of each stage (filled by the host)


class WebView2Host:
    """See the module docstring."""

    def __init__(self, options: HostOptions) -> None:
        if not is_allowed_navigation(options.start_url, options.app_host):
            raise ValueError("the start page must be on the app's own virtual host")
        self.o = options
        self.marks = options.marks
        self.hwnd: int = 0
        self._env = Com(None)
        self._controller = Com(None)
        self._webview = Com(None)
        self._handlers: list[Handler] = []
        self._outbox: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._calls: queue.SimpleQueue[Callable[[], Any]] = queue.SimpleQueue()
        self._wndproc = WNDPROC(self._proc)
        self._class = f"YukiWebView2-{os.getpid()}"
        self._brush = 0
        self._captures: list[tuple[Handler, Com, Callable[[bool], None] | None]] = []
        self.browser_pid: int | None = None
        self.runtime: str | None = None
        self.exit_code = 0
        self._closing = False
        self._low = False   # the memory target is Low (set while minimised)
        self._minimized = False   # for on_shown's edge
        #: Called on the UI thread when the window starts to close, before it is destroyed (read its placement).
        self.on_closing: Callable[[], Any] | None = None
        # the frameless frame: where the page drew its title bar (until it says: the design's height, no buttons)
        scale = self._scale()
        self.caption = CaptionLayout(height=round(CAPTION_CSS_PX * scale), top=round(TOP_RESIZE_CSS_PX * scale),
                                     corner=round(CORNER_CSS_PX * scale))
        self._hover: str | None = None      # the caption button under the pointer (Windows' own hit test)
        self._pressed: str | None = None
        self._maximized: bool | None = None
        #: Whether WebView2 took ``IsNonClientRegionSupportEnabled`` (the page's ``app-region: drag``); None: not asked.
        self.non_client_regions: bool | None = None

    # -- thread-safe API ------------------------------------------------------------------------------

    def post(self, message: Any) -> None:
        """Send ``message`` (JSON-able) to the page; from any thread."""
        self._outbox.put(json.dumps(message, ensure_ascii=False, default=str))
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(self.hwnd), WM_YUKI_POST, 0, 0)

    def close(self) -> None:
        """Close the window (and so the process's message loop); from any thread."""
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(self.hwnd), WM_YUKI_CLOSE, 0, 0)

    def invoke(self, fn: Callable[[], Any]) -> None:
        """Run ``fn`` on the window's thread (where every WebView2 call must happen); from any thread."""
        self._calls.put(fn)
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(self.hwnd), WM_YUKI_CALL, 0, 0)

    # -- UI-thread API ---------------------------------------------------------------------------------

    def bring_forward(self) -> None:
        """Restore the window if minimised and bring it to the front (the tray passed this process the right to;
        never for the off-screen window). UI thread."""
        if not self.hwnd or self.o.placement == "offscreen":
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(wintypes.HWND(self.hwnd)):
            user32.ShowWindow(wintypes.HWND(self.hwnd), SW_RESTORE)
        user32.SetForegroundWindow(wintypes.HWND(self.hwnd))

    def placement_now(self) -> tuple[tuple[int, int, int, int] | None, bool]:
        """(the restored rect x, y, w, h in physical px, maximized) to remember for the next opening. UI thread."""
        if not self.hwnd or self.o.placement == "offscreen":
            return None, False
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(wintypes.HWND(self.hwnd), byref(wp)):
            return None, False
        r = wp.rcNormalPosition
        return (r.left, r.top, r.right - r.left, r.bottom - r.top), wp.showCmd == SW_SHOWMAXIMIZED

    # -- the frameless frame (UI thread) ----------------------------------------------------------------------

    @staticmethod
    def _scale() -> float:
        try:
            return ctypes.windll.user32.GetDpiForSystem() / 96.0
        except (AttributeError, OSError):
            return 1.0

    def set_caption(self, msg: dict[str, Any]) -> None:
        """The page says where it drew its title bar (CSS px; on load and on every resize): Windows' hit test answers
        from it. UI thread."""
        self.caption = CaptionLayout.from_page(msg, self._scale())
        self._event("caption_layout", h=self.caption.height, buttons=sorted(self.caption.buttons))
        if self.hwnd:   # the page is up now: it learns whether the window is maximized (its restore glyph)
            self._maximized = bool(ctypes.windll.user32.IsZoomed(wintypes.HWND(self.hwnd)))
            self.post({"kind": "window", "maximized": self._maximized, "frameless": self.o.frameless,
                       "native_drag": bool(self.non_client_regions)})

    def window_op(self, op: str) -> bool:
        """The page's caption buttons and title bar, when the page itself got the pointer (WebView2 covers the client
        area): ``minimize``, ``maximize`` (toggles restore), ``close``, ``drag`` (the system's own move loop from
        where the pointer is: Aero snap, double-click and the system menu as a title bar has them), ``resize-top`` /
        ``resize-topleft`` / ``resize-topright`` (the system's own sizing loop). Never for the off-screen window.
        UI thread; returns whether it ran."""
        if not self.hwnd or self.o.placement == "offscreen":
            return False
        user32 = ctypes.windll.user32
        hwnd = wintypes.HWND(self.hwnd)
        if op == "minimize":
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
        elif op == "maximize":
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE if user32.IsZoomed(hwnd) else SC_MAXIMIZE, 0)
        elif op == "close":
            self._begin_close()
        elif op in ("drag", "resize-top", "resize-topleft", "resize-topright"):
            code = {"drag": HTCAPTION, "resize-top": HTTOP, "resize-topleft": HTTOPLEFT,
                    "resize-topright": HTTOPRIGHT}[op]
            if code != HTCAPTION and user32.IsZoomed(hwnd):
                return False
            pt = wintypes.POINT()
            user32.GetCursorPos(byref(pt))
            # the system's move / size loop starts as if the press were on our own frame (the pointer is still down;
            # the page's own capture ends with the loop's): the pattern frameless WebView2 hosts use
            user32.ReleaseCapture()
            user32.PostMessageW(hwnd, WM_NCLBUTTONDOWN, code, (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF))
        else:
            return False
        return True

    def _caption_state(self, hover: str | None, pressed: str | None) -> None:
        if (hover, pressed) != (self._hover, self._pressed):
            self._hover, self._pressed = hover, pressed
            self.post({"kind": "caption", "hover": hover, "pressed": pressed})

    def _nc_calcsize(self, hwnd: int, wparam: int, lparam: int) -> int:
        """No title bar: the client area starts at the window's top (the system keeps its invisible resize borders
        left, right and below, and the shadow); maximized, the top is pulled in by the frame that hangs off the
        monitor."""
        params = ctypes.cast(c_void_p(lparam), POINTER(NCCALCSIZE_PARAMS)).contents
        top = params.rgrc[0].top
        _DEF_WINDOW_PROC(hwnd, WM_NCCALCSIZE, wparam, lparam)
        params.rgrc[0].top = top
        if ctypes.windll.user32.IsZoomed(wintypes.HWND(hwnd)):
            try:
                dpi = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
                frame = (ctypes.windll.user32.GetSystemMetricsForDpi(SM_CYFRAME, dpi)
                         + ctypes.windll.user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi))
            except (AttributeError, OSError):
                frame = 8
            params.rgrc[0].top += frame
        return 0

    def _nc_hittest(self, hwnd: int, lparam: int) -> int:
        """Windows asks what the pointer is over (``WM_NCHITTEST``): the system's own resize borders first, then the
        page's title bar (:func:`caption_hit`)."""
        hit = int(_DEF_WINDOW_PROC(hwnd, WM_NCHITTEST, 0, lparam))
        if hit in FRAME_HITS:
            return hit
        pt = wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
        ctypes.windll.user32.ScreenToClient(wintypes.HWND(hwnd), byref(pt))
        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(wintypes.HWND(hwnd), byref(rect))
        if not (0 <= pt.x < rect.right and 0 <= pt.y < rect.bottom):
            return hit
        return caption_hit(pt.x, pt.y, rect.right, self.caption,
                           maximized=bool(ctypes.windll.user32.IsZoomed(wintypes.HWND(hwnd))))

    def _nc_button(self, msg: int, wparam: int) -> bool:
        """A press or release on one of the caption buttons as Windows hit-tested them: handled here (the system would
        draw its old buttons), acting on release over the same button. Returns whether it was one."""
        part = next((p for p, c in CAPTION_PARTS.items() if c == wparam), None)
        if msg == WM_NCMOUSEMOVE:
            if part is not None or self._hover is not None:
                if part is not None:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x00000002 | 0x00000010,   # LEAVE|NONCLIENT
                                          wintypes.HWND(self.hwnd), 0)
                    ctypes.windll.user32.TrackMouseEvent(byref(tme))
                self._caption_state(part, self._pressed if part == self._pressed else None)
            return False   # the system also sees the move (the snap layouts flyout over HTMAXBUTTON)
        if part is None:
            return False
        if msg == WM_NCLBUTTONDOWN:
            self._caption_state(part, part)
        elif msg == WM_NCLBUTTONUP:
            pressed = self._pressed
            self._caption_state(part, None)
            if pressed == part:
                self.window_op({"min": "minimize", "max": "maximize", "close": "close"}[part])
        return True

    def _maximized_changed(self) -> None:
        zoomed = bool(ctypes.windll.user32.IsZoomed(wintypes.HWND(self.hwnd)))
        if zoomed != self._maximized:
            self._maximized = zoomed
            self.post({"kind": "window", "maximized": zoomed})

    def set_memory_low(self, low: bool) -> bool:
        """``MemoryUsageTargetLevel`` Low or Normal (WebView2 trims caches and lets the OS page memory out)."""
        wv19 = self._webview.query(IID_WEBVIEW2_19) if self._webview else Com(None)
        if not wv19:
            return False
        try:
            return wv19.call(WV19_PUT_MEMORY_TARGET, (c_int,), MEMORY_TARGET_LOW if low else MEMORY_TARGET_NORMAL) >= 0
        finally:
            wv19.release()

    def capture(self, path: Path, done: Callable[[bool], None] | None = None) -> bool:
        """Write a PNG of the page (our own content only, never the screen) to ``path``. For offline checks of a
        page built from a fixture; ``done(ok)`` is called on the UI thread."""
        if not self._webview:
            return False
        stream = c_void_p()
        create = ctypes.windll.shlwapi.SHCreateStreamOnFileEx
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL, c_void_p,
                           POINTER(c_void_p)]
        create.restype = c_long
        if create(str(path), 0x00001001, 0x80, True, None, byref(stream)) < 0:   # STGM_CREATE | STGM_WRITE
            return False
        holder = Com(stream.value)

        def finished(error: int) -> None:
            holder.release()
            if done is not None:
                done(error >= 0)

        handler = Handler("capture_done", finished, self._handler_error)
        self._captures.append((handler, holder, done))
        hr = self._webview.call(WV_CAPTURE_PREVIEW, (c_int, c_void_p, c_void_p), 0, holder.ptr, handler.ptr)
        if hr < 0:
            holder.release()
            return False
        return True

    # -- run --------------------------------------------------------------------------------------------

    def run(self) -> int:
        """Open the window and run its message loop until it closes; returns the exit code."""
        self.marks.setdefault("run", time.time())
        ole32, user32 = ctypes.windll.ole32, ctypes.windll.user32
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        try:
            self._create_window()
            self._create_environment()
            msg = wintypes.MSG()
            get = user32.GetMessageW
            get.argtypes = [POINTER(wintypes.MSG), wintypes.HWND, c_uint, c_uint]
            get.restype = c_int
            while get(byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
        finally:
            self._teardown()
            ole32.CoUninitialize()
        return self.exit_code

    def _event(self, kind: str, **fields: Any) -> None:
        if self.o.on_event is not None:
            try:
                self.o.on_event(kind, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _handler_error(self, kind: str, exc: BaseException) -> None:
        self._event("webview_error", where=kind, error=f"{type(exc).__name__}: {exc}")

    def _handler(self, kind: str, fn: Callable[..., Any]) -> Handler:
        h = Handler(kind, fn, self._handler_error)
        self._handlers.append(h)
        return h

    # -- the window ------------------------------------------------------------------------------------

    def _create_window(self) -> None:
        user32, kernel32, gdi32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.gdi32
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        hinst = kernel32.GetModuleHandleW(None)
        r, g, b = self.o.background
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self._brush = gdi32.CreateSolidBrush(wintypes.COLORREF(r | (g << 8) | (b << 16)))
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, c_void_p]
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), CS_HREDRAW | CS_VREDRAW, self._wndproc, 0, 0, hinst, None,
                         user32.LoadCursorW(None, c_void_p(32512)), self._brush, None, self._class, None)
        user32.RegisterClassExW.argtypes = [POINTER(WNDCLASSEXW)]
        if not user32.RegisterClassExW(byref(wc)):
            raise ctypes.WinError()
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           c_int, c_int, c_int, c_int, wintypes.HWND, wintypes.HMENU,
                                           wintypes.HINSTANCE, c_void_p]
        w, h = self._scaled(self.o.size)
        if self.o.placement == "offscreen":
            vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)   # SM_X/YVIRTUALSCREEN
            # the off-screen window has the real frame when frameless (so measurements and checks run its code)
            ex, style, x, y = (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_OVERLAPPEDWINDOW if self.o.frameless
                               else WS_POPUP, vx - w - 400, vy)
        else:
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            ex, style, x, y = 0, WS_OVERLAPPEDWINDOW, max(0, (sw - w) // 2), max(0, (sh - h) // 2)
            if self.o.rect and _on_a_monitor(self.o.rect):
                x, y, w, h = self.o.rect
        hwnd = user32.CreateWindowExW(ex, self._class, self.o.title, style, x, y, w, h, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError()
        self.hwnd = int(hwnd)
        if self.o.placement != "offscreen" or self.o.frameless:
            dark = c_int(1)   # a dark frame (DWMWA_USE_IMMERSIVE_DARK_MODE): the border and the system menu
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(self.hwnd), 20, byref(dark), 4)
        if self.o.frameless:   # the frame is recalculated with no title bar (WM_NCCALCSIZE)
            user32.SetWindowPos(wintypes.HWND(self.hwnd), None, 0, 0, 0, 0,
                                0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010)   # FRAMECHANGED|NOSIZE|NOMOVE|NOZORDER|NOACTIVATE
        user32.ShowWindow(wintypes.HWND(self.hwnd), SW_SHOWNOACTIVATE if self.o.placement == "offscreen"
                          else SW_SHOWMAXIMIZED if self.o.maximized else SW_SHOWNORMAL)
        if self.o.placement != "offscreen":
            user32.SetForegroundWindow(wintypes.HWND(self.hwnd))
        self.marks["window"] = time.time()

    @staticmethod
    def _scaled(size: tuple[int, int]) -> tuple[int, int]:
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except (AttributeError, OSError):
            dpi = 96
        return int(size[0] * dpi / 96), int(size[1] * dpi / 96)

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        try:
            if msg == WM_YUKI_POST:
                self._drain()
                return 0
            if msg == WM_YUKI_CALL:
                while True:
                    try:
                        fn = self._calls.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001
                        self._handler_error("invoke", exc)
                return 0
            if msg in (WM_YUKI_CLOSE, WM_CLOSE):
                self._begin_close()
                return 0
            if self.o.frameless:
                if msg == WM_NCCALCSIZE and wparam:
                    return self._nc_calcsize(hwnd, wparam, lparam)
                if msg == WM_NCHITTEST:
                    return self._nc_hittest(hwnd, lparam)
                if msg in (WM_NCMOUSEMOVE, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK) and \
                        self._nc_button(msg, wparam):
                    return 0
                if msg == WM_NCMOUSELEAVE:
                    self._caption_state(None, None)
            if msg == WM_SIZE:
                self._fit(minimized=wparam == SIZE_MINIMIZED)
                if self.o.frameless and wparam != SIZE_MINIMIZED:
                    self._maximized_changed()
            elif msg == WM_MOVE and self._controller:
                self._controller.call(CTL_NOTIFY_MOVED)
            elif msg == WM_ACTIVATE and (wparam & 0xFFFF) != WA_INACTIVE and not (wparam >> 16) & 0xFFFF:
                self._shown("activate")   # activated and not minimised (the high word)
            elif msg == WM_MOUSEACTIVATE and self.o.placement == "offscreen":
                return MA_NOACTIVATE
            elif msg == WM_DPICHANGED:
                rect = ctypes.cast(c_void_p(lparam), POINTER(wintypes.RECT)).contents
                user32.SetWindowPos(wintypes.HWND(hwnd), None, rect.left, rect.top, rect.right - rect.left,
                                    rect.bottom - rect.top, 0x0014)   # SWP_NOZORDER | SWP_NOACTIVATE
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(self.exit_code)
                return 0
        except Exception as exc:  # noqa: BLE001 - never unwind into user32
            self._handler_error("wndproc", exc)
        return _DEF_WINDOW_PROC(hwnd, msg, wparam, lparam)

    def _fit(self, minimized: bool = False) -> None:
        if not self._controller:
            return
        if minimized:   # hidden: no rendering, and let WebView2 give memory back (08 §7.4 levers; measured
            # 2026-10-03: resident memory 384 MB -> 52 MB within 30 s, docs/research/revamp-2026-10-02/25-...)
            self._controller.put_bool(CTL_PUT_IS_VISIBLE, False)
            self._low = self.set_memory_low(True)
            self._minimized = True
            return
        was_minimized, self._minimized = self._minimized, False
        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(wintypes.HWND(self.hwnd), byref(rect))
        self._controller.call(CTL_PUT_BOUNDS, (wintypes.RECT,), rect)
        self._controller.put_bool(CTL_PUT_IS_VISIBLE, True)
        if self._low:
            self.set_memory_low(False)
            self._low = False
        if was_minimized:
            self._shown("restore")

    def _shown(self, reason: str) -> None:
        if self.o.on_shown is not None:
            try:
                self.o.on_shown(reason)
            except Exception as exc:  # noqa: BLE001
                self._handler_error("on_shown", exc)

    def _begin_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.on_closing is not None:
            try:
                self.on_closing()
            except Exception as exc:  # noqa: BLE001
                self._handler_error("on_closing", exc)
        if self._controller:
            try:
                self._controller.call(CTL_CLOSE)
            except OSError:
                pass
        ctypes.windll.user32.DestroyWindow(wintypes.HWND(self.hwnd))

    def _teardown(self) -> None:
        for com in (self._webview, self._controller, self._env):
            try:
                com.release()
            except OSError:
                pass
        if self.hwnd:
            unregister = ctypes.windll.user32.UnregisterClassW
            unregister.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            unregister(self._class, ctypes.windll.kernel32.GetModuleHandleW(None))
        if self._brush:
            ctypes.windll.gdi32.DeleteObject(wintypes.HGDIOBJ(self._brush))
            self._brush = 0

    # -- WebView2 --------------------------------------------------------------------------------------

    def _create_environment(self) -> None:
        loader = loader_path()
        self.runtime = runtime_version(loader)
        if self.runtime is None:
            raise RuntimeError("The WebView2 runtime is not installed")
        args = self.o.browser_args
        if args is not None:   # set for this process only, before the environment reads it
            os.environ[BROWSER_ARGS_ENV] = args
            ctypes.windll.kernel32.SetEnvironmentVariableW(BROWSER_ARGS_ENV, args)
        self.o.user_data_dir.mkdir(parents=True, exist_ok=True)
        dll = ctypes.WinDLL(str(loader))
        create = dll.CreateCoreWebView2EnvironmentWithOptions
        create.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, c_void_p, c_void_p]
        create.restype = c_long
        handler = self._handler("environment_created", self._on_environment)
        self.marks["environment_asked"] = time.time()
        _check(create(None, str(self.o.user_data_dir), None, handler.ptr), "CreateCoreWebView2Environment")

    def _fatal(self, where: str, exc: BaseException) -> None:
        """Setup failed: say so and close (the window would otherwise sit empty forever)."""
        self._handler_error(where, exc)
        self.exit_code = 2
        self.close()

    def _on_environment(self, error: int, env: int) -> None:
        try:
            self._setup_environment(error, env)
        except Exception as exc:  # noqa: BLE001
            self._fatal("environment", exc)

    def _on_controller(self, error: int, controller: int) -> None:
        try:
            self._setup_controller(error, controller)
        except Exception as exc:  # noqa: BLE001
            self._fatal("controller", exc)

    def _setup_environment(self, error: int, env: int) -> None:
        _check(error, "creating the WebView2 environment")
        self.marks["environment"] = time.time()
        self._env = Com(env)
        self._env.call(1)   # AddRef: we keep it beyond this callback
        handler = self._handler("controller_created", self._on_controller)
        _check(self._env.call(ENV_CREATE_CONTROLLER, (wintypes.HWND, c_void_p), wintypes.HWND(self.hwnd),
                              handler.ptr), "CreateCoreWebView2Controller")

    def _setup_controller(self, error: int, controller: int) -> None:
        _check(error, "creating the WebView2 controller")
        self.marks["controller"] = time.time()
        self._controller = Com(controller)
        self._controller.call(1)   # AddRef
        c2 = self._controller.query(IID_CONTROLLER2)
        if c2:
            r, g, b = self.o.background
            c2.call(CTL2_PUT_BACKGROUND, (Color,), Color(255, r, g, b))
            c2.release()
        wv = c_void_p()
        _check(self._controller.call(CTL_GET_WEBVIEW, (POINTER(c_void_p),), byref(wv)), "get_CoreWebView2")
        self._webview = Com(wv.value)
        self._lock_down()
        self._map_folders()
        token = Token()
        for slot, kind, fn in ((WV_ADD_NAVIGATION_STARTING, "navigation_starting", self._on_navigation_starting),
                               (WV_ADD_NAVIGATION_COMPLETED, "navigation_completed", self._on_navigation_completed),
                               (WV_ADD_WEB_MESSAGE, "web_message", self._on_web_message),
                               (WV_ADD_PERMISSION_REQUESTED, "permission", self._on_permission),
                               (WV_ADD_NEW_WINDOW, "new_window", self._on_new_window),
                               (WV_ADD_PROCESS_FAILED, "process_failed", self._on_process_failed)):
            _check(self._webview.call(slot, (c_void_p, POINTER(Token)), self._handler(kind, fn).ptr, byref(token)),
                   f"subscribing to {kind}")
        wv4 = self._webview.query(IID_WEBVIEW2_4)
        if wv4:
            wv4.call(WV4_ADD_DOWNLOAD_STARTING, (c_void_p, POINTER(Token)),
                     self._handler("download_starting", self._on_download).ptr, byref(token))
            wv4.release()
        pid = c_uint()
        if self._webview.call(WV_BROWSER_PID, (POINTER(c_uint),), byref(pid)) >= 0:
            self.browser_pid = int(pid.value)
        self._fit()
        self.marks["navigate"] = time.time()
        _check(self._webview.call(WV_NAVIGATE, (wintypes.LPCWSTR,), self.o.start_url), "Navigate")
        if self.o.on_ready is not None:
            self.o.on_ready(self)

    def _lock_down(self) -> None:
        out = c_void_p()
        _check(self._webview.call(WV_GET_SETTINGS, (POINTER(c_void_p),), byref(out)), "get_Settings")
        s = Com(out.value)
        try:
            s.put_bool(SET_PUT_SCRIPT, True)
            s.put_bool(SET_PUT_WEB_MESSAGE, True)
            s.put_bool(SET_PUT_SCRIPT_DIALOGS, False)
            s.put_bool(SET_PUT_STATUS_BAR, False)
            s.put_bool(SET_PUT_DEV_TOOLS, self.o.dev_tools)
            s.put_bool(SET_PUT_CONTEXT_MENUS, self.o.dev_tools)
            s.put_bool(SET_PUT_HOST_OBJECTS, False)
            s.put_bool(SET_PUT_ZOOM, False)
            for iid, slots in ((IID_SETTINGS3, (SET3_PUT_ACCELERATOR_KEYS,)),
                               (IID_SETTINGS4, (SET4_PUT_PASSWORD_AUTOSAVE, SET4_PUT_AUTOFILL)),
                               (IID_SETTINGS5, (SET5_PUT_PINCH_ZOOM,)), (IID_SETTINGS6, (SET6_PUT_SWIPE,))):
                newer = s.query(iid)
                if newer:
                    for slot in slots:
                        newer.put_bool(slot, False)
                    newer.release()
            if self.o.frameless:
                # the page's title bar (``app-region: drag``) moves the window natively where the runtime supports it
                # (WebView2 1.0.2420+); elsewhere the page asks the host to start the move (``window_op("drag")``)
                s9 = s.query(IID_SETTINGS9)
                self.non_client_regions = False
                if s9:
                    try:
                        self.non_client_regions = s9.call(SET9_PUT_NON_CLIENT_REGION, (c_int,), 1) >= 0
                    finally:
                        s9.release()
                self._event("webview_non_client_regions", on=self.non_client_regions)
        finally:
            s.release()

    def _map_folders(self) -> None:
        wv3 = self._webview.query(IID_WEBVIEW2_3)
        if not wv3:
            raise RuntimeError("This WebView2 runtime is too old (no virtual host mapping)")
        try:
            for host, folder in self.o.folders.items():
                # The app's own host serves pages and scripts to itself only. Other hosts (the fonts) are read
                # cross-origin, and @font-face fetches are CORS requests: they must allow CORS or every face fails
                # to load (measured 2026-10-03). They hold only public files, and only our origin ever loads here.
                access = HOST_ACCESS_DENY_CORS if host == self.o.app_host else HOST_ACCESS_ALLOW
                _check(wv3.call(WV3_SET_VIRTUAL_HOST, (wintypes.LPCWSTR, wintypes.LPCWSTR, c_int), host,
                                str(Path(folder).resolve()), access), f"mapping {host}")
        finally:
            wv3.release()

    def _drain(self) -> None:
        while True:
            try:
                text = self._outbox.get_nowait()
            except queue.Empty:
                return
            if self._webview:
                self._webview.call(WV_POST_JSON, (wintypes.LPCWSTR,), text)

    # -- events -----------------------------------------------------------------------------------------

    def _on_navigation_starting(self, _sender: int, args: int) -> None:
        a = Com(args)
        uri = a.get_string(NAV_GET_URI)
        if is_allowed_navigation(uri, self.o.app_host):
            return
        a.put_bool(NAV_PUT_CANCEL, True)
        user = bool(a.get_int(NAV_IS_USER_INITIATED))
        if user and is_external_link(uri):
            ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, SW_SHOWNORMAL)
        self._event("webview_navigation_blocked", user=user, external=is_external_link(uri))

    def _on_navigation_completed(self, _sender: int, args: int) -> None:
        a = Com(args)
        ok = bool(a.get_int(NAVDONE_IS_SUCCESS))
        self.marks.setdefault("navigation_completed", time.time())
        if not ok:
            self._event("webview_navigation_failed", status=a.get_int(NAVDONE_ERROR_STATUS))

    def _on_web_message(self, _sender: int, args: int) -> None:
        a = Com(args)
        if not is_allowed_navigation(a.get_string(MSG_GET_SOURCE), self.o.app_host):
            return
        try:
            message = json.loads(a.get_string(MSG_GET_JSON))
        except ValueError:
            return
        if self.o.on_message is not None:
            self.o.on_message(message)

    def _on_permission(self, _sender: int, args: int) -> None:
        a = Com(args)
        kind = a.get_int(PERM_GET_KIND)
        a.call(PERM_PUT_STATE, (c_int,), PERMISSION_DENY)
        self._event("webview_permission_denied", kind=kind)

    def _on_new_window(self, _sender: int, args: int) -> None:
        a = Com(args)
        uri = a.get_string(NEWWIN_GET_URI)
        a.put_bool(NEWWIN_PUT_HANDLED, True)   # handled, with no new window: nothing opens inside Yuki
        if is_external_link(uri):
            ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, SW_SHOWNORMAL)

    def _on_download(self, _sender: int, args: int) -> None:
        a = Com(args)
        a.put_bool(DL_PUT_CANCEL, True)
        a.put_bool(DL_PUT_HANDLED, True)
        self._event("webview_download_refused")

    def _on_process_failed(self, _sender: int, args: int) -> None:
        kind = Com(args).get_int(FAILED_GET_KIND)
        self._event("webview_process_failed", kind=kind)
        if kind == 0:   # the browser process is gone: this window cannot recover
            self.exit_code = 3
            self.close()


__all__ = ["APP_HOST", "FONT_HOST", "HostOptions", "WebView2Host", "loader_path", "runtime_version",
           "is_allowed_navigation", "is_external_link", "CaptionLayout", "caption_hit", "CAPTION_PARTS"]
