"""The tray in the Lattice design (3 Oct, items 21 and 22): Yuki's face as the icon, a hover card in every state, and a
short menu.

**The icon** is the island's face cut down to a 5 x 5 dot grid (the mock's "Tray face grids"): the same square eyes
two dots wide, whose height is the expression, and the same mouth row; shut eyes sit where open eyes sit under a dim
lid; a dot tucked into the top-right corner adds the call (mint) or off (hollow) state. Hand-tuned per size, in whole
pixels: 16 px = 3 px pitch and 2 px dots, 20 px = 4 px pitch and 3 px round dots, 24 px = 5 px pitch and 4 px round
dots (32 px = 6 px pitch, 5 px dots). Hearing, speaking and working swap 4 frames; the others stay still. Colours
follow the taskbar's light or dark theme.

=============  ==========================================================
resting        calm open eyes, a soft mouth: say "Yuki"
listening      tall eyes, a small mint mouth
hearing        tall eyes, the mouth widens and narrows (4 x 190 ms)
speaking       calm eyes, one mouth that opens and closes (4 x 225 ms)
working        eyes under a dim lid, the mouth is the step bar (4 x 300 ms)
needs-you      raised eyes, a mint "o" mouth
stopped        squeezed "> <" eyes: voice stopped
call-ask       shut eyes, a soft mouth, no rec dot (the consent card waits)
call           shut eyes, a soft mouth, the mint rec dot (your mic is recorded)
off            the whole face dim, a hollow corner ring: not listening
game           shut eyes, no mouth; the rec dot only while a call records
=============  ==========================================================

**The hover card** grows out of the icon in every state when the pointer rests on it: Windows says so
(``NOTIFYICON_VERSION_4``: ``NIN_POPUPOPEN`` / ``NIN_POPUPCLOSE``; the standard tooltip is suppressed), so nothing
looks at the pointer. It folds back 300 ms after the pointer has left the icon and the card. **Left-click** opens the
Yuki window; **right-click** the menu (Open Yuki, Listen for "Yuki", Pause listening for this call, Settings, More,
Quit). On Windows the icon is Yuki's own ``Shell_NotifyIcon`` (:class:`NativeTray`); elsewhere a
:class:`QSystemTrayIcon` stands in (no hover card).
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSystemTrayIcon, QWidget

# -- the faces -------------------------------------------------------------------------------------------------

TRAY_STATES: tuple[str, ...] = ("resting", "listening", "hearing", "speaking", "working", "needs-you", "stopped",
                                "call-ask", "call", "off", "game")

_E2, _NO, _LID = "XX.XX", ".....", "dd.dd"
_TALL = [_E2, _E2, _E2]
_OPEN = [_NO, _E2, _E2, _NO]

#: The mock's "Tray face grids": per state, its frames (5 rows of 5 cells each) and the ms per frame (0: still).
#: ``X`` lit (text colour), ``d`` the same dimmed, ``M`` mint, ``m`` mint at 50 %, ``.`` nothing.
TRAY_FACES: dict[str, tuple[list[list[str]], int]] = {
    "resting": ([[*_OPEN, ".ddd."]], 0),
    "listening": ([[*_TALL, _NO, ".mMm."]], 0),
    "hearing": ([[*_TALL, _NO, m] for m in ("XXMXX", ".XMX.", "dXMXd", ".XMX.")], 190),
    "speaking": ([[*_OPEN, m] for m in (".XXX.", "XXXXX", ".XXX.", "..X..")], 225),
    "working": ([[_LID, _E2, _E2, _NO, m] for m in ("XMddd", "XMddd", "Xmddd", "Xmddd")], 300),
    "needs-you": ([[_E2, _E2, "..M..", ".M.M.", "..M.."]], 0),
    "stopped": ([["X...X", ".X.X.", "X...X", _NO, "dd.dd"]], 0),
    "call-ask": ([[_NO, _LID, _E2, _NO, ".ddd."]], 0),
    "call": ([[_NO, _LID, _E2, _NO, ".ddd."]], 0),
    "off": ([[_NO, _LID, _LID, _NO, ".ddd."]], 0),
    "game": ([[_NO, _LID, _E2, _NO, _NO]], 0),
}

#: Per icon size, in whole pixels: pitch, dot, offset, and the corner badge box (x, y, side).
TRAY_GRID: dict[int, tuple[int, int, int, tuple[int, int, int]]] = {
    16: (3, 2, 1, (13, 0, 3)),
    20: (4, 3, 1, (16, 0, 4)),
    24: (5, 4, 0, (20, 0, 4)),
    32: (6, 5, 1, (26, 0, 5)),
    64: (12, 9, 4, (50, 2, 12)),
}

#: Theme colours: (lit, mint, dim share) on a dark and on a light taskbar.
TRAY_INK: dict[str, tuple[str, str, float]] = {"dark": ("#F2EFE9", "#6FEBC4", 0.62), "light": ("#1B1B1B", "#0B8A66", 0.5)}


def tray_badge(state: str, *, recording: bool = False) -> str:
    """The corner badge for ``state``: ``"rec"`` (call; game while a call records), ``"off"`` (off), or ``""``."""
    if state == "call" or (state == "game" and recording):
        return "rec"
    if state == "off":
        return "off"
    return ""


def tray_frames(state: str) -> int:
    return len(TRAY_FACES.get(state, TRAY_FACES["resting"])[0])


def tray_frame_ms(state: str) -> int:
    return TRAY_FACES.get(state, TRAY_FACES["resting"])[1]


def _grid_for(size: int) -> tuple[int, int, int, tuple[int, int, int]]:
    """The hand-tuned grid for ``size``, or the largest one under it, centred (175 %: 28 px draws the 24 px grid)."""
    if size in TRAY_GRID:
        return TRAY_GRID[size]
    base = max([s for s in TRAY_GRID if s <= size] or [16])
    p, d, o, (bx, by, bs) = TRAY_GRID[base]
    pad = (size - base) // 2
    return p, d, o + pad, (bx + pad, by + pad, bs)


def tray_face_image(state: str, size: int = 16, frame: int = 0, theme: str = "dark", *, badge: str | None = None,
                    dpr: float = 1.0) -> QImage:
    """One face, drawn on whole device pixels: ``size`` px square (times ``dpr``), frame ``frame`` of the state."""
    frames, _ms = TRAY_FACES.get(state, TRAY_FACES["resting"])
    rows = frames[frame % len(frames)]
    if badge is None:
        badge = tray_badge(state)
    lit_hex, mint_hex, dim = TRAY_INK.get(theme, TRAY_INK["dark"])
    px = int(round(size * dpr))
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p, d, o, (bx, by, bs) = _grid_for(px)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    if size >= 64:                       # the board's check view also draws the unlit dots
        ghost = QColor("#D6D6D6" if theme == "light" else "#2A2D31")
        painter.setBrush(ghost)
        r = d * 0.42 / 2
        for j in range(5):
            for i in range(5):
                painter.drawEllipse(QRectF(o + i * p + d / 2 - r, o + j * p + d / 2 - r, 2 * r, 2 * r))
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch == ".":
                continue
            c = QColor(mint_hex if ch in ("M", "m") else lit_hex)
            if ch == "d":
                c.setAlphaF(dim)
            elif ch == "m":
                c.setAlphaF(0.5)
            painter.setBrush(c)
            x, y = o + i * p, o + j * p
            if d < 3:                     # 16 px: 2 px squares with 0.5 px corners (a circle would blur)
                painter.drawRoundedRect(QRectF(x, y, d, d), d / 4, d / 4)
            else:
                painter.drawEllipse(QRectF(x, y, d, d))
    if badge == "rec":
        painter.setBrush(QColor(mint_hex))
        if bs < 4:
            painter.drawRoundedRect(QRectF(bx, by, bs, bs), 1, 1)
        else:
            painter.drawEllipse(QRectF(bx, by, bs, bs))
    elif badge == "off":
        sw = 2 if bs >= 9 else 1
        ring = QColor(lit_hex)
        ring.setAlphaF(0.8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ring, sw))
        rr = max(0.5, (bs - sw) / 3)
        painter.drawRoundedRect(QRectF(bx + sw / 2, by + sw / 2, bs - sw, bs - sw), rr, rr)
    painter.end()
    if dpr != 1.0:
        img.setDevicePixelRatio(dpr)
    return img


def tray_face_icon(state: str, theme: str = "dark", *, frame: int = 0, badge: str | None = None) -> QIcon:
    """The face as a :class:`QIcon` with every hand-tuned size (16, 20, 24, 32, 64)."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 64):
        icon.addPixmap(QPixmap.fromImage(tray_face_image(state, size, frame, theme, badge=badge)))
    return icon


#: The old tray states (before 3 Oct) and the face each now draws.
_OLD_STATES = {"armed": "resting", "listening": "listening", "working": "working", "recording": "call",
               "waiting": "resting", "off": "off"}


def tray_icon(listening: bool = True, state: str | None = None) -> QIcon:
    """The tray icon as a :class:`QIcon`: ``state`` one of :data:`TRAY_STATES` (or an older name); ``listening``
    False alone is the "off" face (v3.16: "Yuki isn't listening" visible at a glance)."""
    state = state or ("resting" if listening else "off")
    return tray_face_icon(_OLD_STATES.get(state, state) if state not in TRAY_FACES else state, taskbar_theme())


def tray_state(island_state: str, *, listening: str = "on", recording: bool = False, asking: bool = False,
               fullscreen: bool = False) -> str:
    """The tray's face from the island's state (:func:`yuki.ui.island.resolve`) and what the island doesn't show: a
    request running or a question wins, then a broken voice, then your turn and Yuki speaking, then "not listening",
    then a full screen, then a call (recorded, or its consent card waiting), else resting."""
    if island_state in ("working", "thinking"):
        return "working"
    if island_state == "needs-you":
        return "needs-you"
    if island_state == "voice-stopped":
        return "stopped"
    if island_state in ("hearing", "speaking", "listening"):
        return island_state
    if listening != "on":
        return "off"
    if fullscreen or island_state == "game-quiet":
        return "game"
    if recording:
        return "call"
    if asking:
        return "call-ask"
    return "resting"


def taskbar_theme() -> str:
    """``"light"`` or ``"dark"``: the taskbar's theme (``SystemUsesLightTheme``); dark when it can't be read."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _kind = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return "light" if int(value) else "dark"
    except Exception:
        return "dark"


# -- what the tray says ------------------------------------------------------------------------------------------


def natural_when(epoch: float, now: float | None = None) -> str:
    """"just now", "9:24 PM", "yesterday, 9:24 PM", "25 September, 9:24 PM" (deterministic formatting)."""
    now = time.time() if now is None else now
    if now - epoch < 60:
        return "just now"
    t, n = datetime.fromtimestamp(epoch), datetime.fromtimestamp(now)
    clock = t.strftime("%I:%M %p").lstrip("0")
    days = (n.date() - t.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"yesterday, {clock}"
    return f"{t.day} {t.strftime('%B')}, {clock}"


@dataclass
class TrayCardInfo:
    """What the hover card shows: the face's state, its state line and sub line, the running task (text, done, of,
    current) with what its key does, the last thing done, and whether Yuki listens for its name."""

    state: str = "resting"
    line: str = "Resting"
    sub: str = ""
    task: str = ""
    task_kind: str = ""              # "run" (Stop) | "question" (Answer) | "voice" (Retry) | "" (nothing running)
    done: int = 0
    of: int = 0
    cur: int = -1
    last: str = ""
    listen: bool = True
    game: bool = False


LISTEN_OFF_TEXT = ("I can't hear you or talk. Typing still works. Call notes still record your mic; "
                   "right-click → Pause listening for this call to skip one.")


# -- the native tray icon (Windows) --------------------------------------------------------------------------

WM_USER = 0x0400
WM_APP = 0x8000
WM_CONTEXTMENU = 0x007B
WM_SETTINGCHANGE = 0x001A
WM_DPICHANGED = 0x02E0
NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1
NIN_POPUPOPEN = WM_USER + 6
NIN_POPUPCLOSE = WM_USER + 7
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NOTIFYICON_VERSION_4 = 4
TRAY_CALLBACK = WM_APP + 0x59


def _tray_api() -> Any:
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON)]

    class NOTIFYICONIDENTIFIER(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("guidItem", GUID)]

    shell32 = ctypes.WinDLL("shell32")
    user32 = ctypes.WinDLL("user32")
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
    shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    return NOTIFYICONDATAW, NOTIFYICONIDENTIFIER, shell32, user32


class _TrayHost(QWidget):
    """The tray icon's own hidden window: never shown; Windows posts the icon's messages to it."""

    def __init__(self, owner: "NativeTray") -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("YukiTrayHost")
        self.owner = owner

    def nativeEvent(self, event_type: Any, message: Any) -> Any:  # noqa: N802, D102
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if self.owner._on_message(int(msg.message), int(msg.wParam), int(msg.lParam)):
                return True, 0
        except Exception:
            pass
        return False, 0


class NativeTray(QObject):
    """Yuki's tray icon through ``Shell_NotifyIcon`` (``NOTIFYICON_VERSION_4``, no standard tooltip), with the parts of
    :class:`QSystemTrayIcon`'s interface the app uses (``setIcon``, ``setToolTip``, ``setContextMenu``, ``show``,
    ``hide``, ``geometry``, ``activated``) plus the hover: ``popup_open`` / ``popup_close`` (``NIN_POPUPOPEN`` /
    ``NIN_POPUPCLOSE``), and ``theme_changed`` when the taskbar turns light or dark."""

    activated = Signal(object)          # QSystemTrayIcon.ActivationReason
    popup_open = Signal()
    popup_close = Signal()
    theme_changed = Signal()

    def __init__(self, icon: QIcon | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        (self._NID, self._NII, self._shell, self._user) = _tray_api()
        self._host = _TrayHost(self)
        self._hwnd = int(self._host.winId())
        self._taskbar_created = int(self._user.RegisterWindowMessageW("TaskbarCreated"))
        self._icon = icon or QIcon()
        self._hicon = 0
        self._image_key: Any = None
        self._tip = "Yuki"
        self._menu: Any = None
        self._shown = False
        self.theme = taskbar_theme()

    # -- the icon's data ---------------------------------------------------------------------------------------

    def _data(self, flags: int) -> Any:
        nid = self._NID()
        nid.cbSize = ctypes.sizeof(self._NID)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = TRAY_CALLBACK
        nid.hIcon = self._hicon or None
        nid.szTip = self._tip[:127]
        return nid

    def icon_size(self) -> int:
        """The small-icon size the taskbar draws at (16 at 100 %, 20 at 125 %, 24 at 150 %, 32 at 200 %)."""
        try:
            user32 = ctypes.WinDLL("user32")
            bar = user32.FindWindowW("Shell_TrayWnd", None)
            dpi = int(user32.GetDpiForWindow(bar)) if bar else 0
            if dpi:
                return int(user32.GetSystemMetricsForDpi(49, dpi))        # SM_CXSMICON
            return int(user32.GetSystemMetrics(49)) or 16
        except Exception:
            return 16

    def _set_hicon_from(self, image: QImage) -> None:
        try:
            hicon = int(image.toHICON())
        except Exception:
            hicon = 0
        old = self._hicon
        self._hicon = hicon
        if self._shown:
            self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(NIF_ICON)))
        if old:
            self._user.DestroyIcon(old)

    def setIcon(self, icon: QIcon) -> None:  # noqa: N802 - QSystemTrayIcon's name
        self._icon = icon
        self._image_key = None
        size = self.icon_size()
        self._set_hicon_from(icon.pixmap(size, size).toImage())

    def set_face(self, state: str, frame: int = 0, badge: str = "") -> None:
        """Draw the face for ``state`` (and ``frame``) at the taskbar's size and theme."""
        size = self.icon_size()
        key = (state, frame, badge, size, self.theme)
        if key == self._image_key:
            return
        self._image_key = key
        self._set_hicon_from(tray_face_image(state, size, frame, self.theme, badge=badge))

    def setToolTip(self, text: str) -> None:  # noqa: N802
        text = str(text or "Yuki")
        if text == self._tip:
            return
        self._tip = text
        if self._shown:
            self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(NIF_TIP)))

    def toolTip(self) -> str:  # noqa: N802
        return self._tip

    def setContextMenu(self, menu: Any) -> None:  # noqa: N802
        self._menu = menu

    def contextMenu(self) -> Any:  # noqa: N802
        return self._menu

    def show(self) -> None:
        if self._shown:
            return
        if not self._hicon:
            size = self.icon_size()
            self._set_hicon_from(self._icon.pixmap(size, size).toImage())
        ok = self._shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)))
        nid = self._data(0)
        nid.uVersion = NOTIFYICON_VERSION_4
        self._shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self._shown = bool(ok)

    def hide(self) -> None:
        if self._shown:
            self._shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            self._shown = False
        if self._hicon:
            self._user.DestroyIcon(self._hicon)
            self._hicon = 0

    def isVisible(self) -> bool:  # noqa: N802
        return self._shown

    def geometry(self) -> QRect:
        """The icon's rectangle on screen, in Qt's logical pixels (empty when unknown)."""
        from ctypes import wintypes

        nii = self._NII()
        nii.cbSize = ctypes.sizeof(self._NII)
        nii.hWnd = self._hwnd
        nii.uID = 1
        r = wintypes.RECT()
        if self._shell.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(r)) != 0:
            return QRect()
        return physical_to_logical(QRect(r.left, r.top, r.right - r.left, r.bottom - r.top))

    # -- its messages --------------------------------------------------------------------------------------------

    def _on_message(self, message: int, wparam: int, lparam: int) -> bool:
        if message == TRAY_CALLBACK:
            event = lparam & 0xFFFF
            if event in (NIN_SELECT, NIN_KEYSELECT):
                self.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
            elif event == WM_CONTEXTMENU:
                self.activated.emit(QSystemTrayIcon.ActivationReason.Context)
                self._popup_menu()
            elif event == NIN_POPUPOPEN:
                self.popup_open.emit()
            elif event == NIN_POPUPCLOSE:
                self.popup_close.emit()
            return True
        if message == self._taskbar_created and self._taskbar_created:
            was, self._shown = self._shown, False      # Explorer restarted: the icon must be added again
            if was:
                self.show()
            return True
        if message in (WM_SETTINGCHANGE, WM_DPICHANGED):
            theme = taskbar_theme()
            if theme != self.theme or message == WM_DPICHANGED:
                self.theme = theme
                self._image_key = None
                self.theme_changed.emit()
        return False

    def _popup_menu(self) -> None:
        menu = self._menu
        if menu is None:
            return
        self._user.SetForegroundWindow(self._hwnd)    # so the menu closes when you click elsewhere
        menu.popup(QCursor.pos())


def physical_to_logical(r: QRect) -> QRect:
    """A rectangle in physical screen pixels (Windows) to Qt's logical pixels, on the screen it is on."""
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
        mon = user32.MonitorFromPoint(wintypes.POINT(r.center().x(), r.center().y()), 2)
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return r
        device = str(info.szDevice)
        screen = next((s for s in QGuiApplication.screens() if s.name() == device), None)
        if screen is None:
            return r
        dpr = float(screen.devicePixelRatio() or 1.0)
        g = screen.geometry()
        m = info.rcMonitor
        x = g.x() + (r.x() - m.left) / dpr
        y = g.y() + (r.y() - m.top) / dpr
        return QRect(int(round(x)), int(round(y)), int(round(r.width() / dpr)), int(round(r.height() / dpr)))
    except Exception:
        return r


def make_tray(app: Any, icon: QIcon | None = None) -> Any:
    """Yuki's own tray icon on Windows (:class:`NativeTray`, with the hover card), else a :class:`QSystemTrayIcon`."""
    if QGuiApplication.platformName() == "windows":
        try:
            return NativeTray(icon, app)
        except Exception:
            pass
    return QSystemTrayIcon(icon or QIcon(), app)


# -- keeping the tray on the island's state ---------------------------------------------------------------------


class TrayWatch(QObject):
    """Keeps the tray's face, its accessible text and the hover card on Yuki's state. ``tray`` is a
    :class:`NativeTray` (or a :class:`QSystemTrayIcon`, without a hover card); ``island`` the
    :class:`~yuki.ui.island.Island`; ``waiting`` how many cards wait; ``extra`` the text's other lines; ``voice`` the
    voice session (``wake_enabled``, ``set_wake``); ``open_window`` opens the Yuki window.

    Nothing runs on a timer except: the face's frames while it is hearing, speaking or working (190-300 ms, the
    mock's), and the card's call timer once a second while the card is open."""

    def __init__(self, tray: Any, island: Any, *, waiting: Callable[[], int] = lambda: 0,
                 extra: Callable[[], str] = lambda: "", voice: Any = None,
                 open_window: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.tray, self.island = tray, island
        self.waiting, self.extra = waiting, extra
        self.voice = voice
        self.open_window = open_window or (lambda: None)
        self.state = ""
        self.badge = ""
        self.frame = 0
        self.card: Any = None
        self._icons: dict[tuple, QIcon] = {}
        self._frames = QTimer(self)
        self._frames.timeout.connect(self._next_frame)
        self._card_tick = QTimer(self)
        self._card_tick.setInterval(1000)
        self._card_tick.timeout.connect(self._fill_card)
        self._fold = QTimer(self)
        self._fold.setSingleShot(True)
        self._fold.setInterval(300)
        self._fold.timeout.connect(self._hide_card)
        island.state_changed.connect(lambda _s: self.refresh())
        for name, slot in (("popup_open", self.show_card), ("popup_close", self.pointer_left),
                           ("theme_changed", self._repaint)):
            sig = getattr(tray, name, None)
            if sig is not None:
                sig.connect(slot)
        activated = getattr(tray, "activated", None)
        if activated is not None:
            activated.connect(lambda _reason: self._hide_card(now=True))

    # -- the state -----------------------------------------------------------------------------------------------

    def _recording(self) -> bool:
        call = self.island.inputs.call
        return bool(call) and call.get("status") == "recording"

    def current(self) -> str:
        i = self.island.inputs
        return tray_state(self.island.state, listening=i.listening, recording=self._recording(),
                          asking=bool(getattr(i, "call_asking", "")), fullscreen=bool(i.fullscreen))

    def words(self) -> str:
        """The icon's accessible text (Windows' tooltip slot; screen readers read it): the state line, then the
        call's timer while one is kept, then the other lines."""
        info = self.card_info()
        lines = [f"Yuki: {info.line}"]
        if info.sub:
            lines.append(info.sub)
        extra = self.extra()
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def refresh(self) -> None:
        state = self.current()
        badge = tray_badge(state, recording=self._recording())
        if (state, badge) != (self.state, self.badge):
            self.state, self.badge = state, badge
            self.frame = 0
            self._paint()
            ms = tray_frame_ms(state)
            from yuki.ui import lattice as L

            if ms and tray_frames(state) > 1 and not L.reduced():
                self._frames.start(ms)
            else:
                self._frames.stop()
        self.tray.setToolTip(self.words())
        if self.card is not None and self.card.isVisible() and not self.card.closing:
            self._fill_card()

    def _paint(self) -> None:
        set_face = getattr(self.tray, "set_face", None)
        if callable(set_face):
            set_face(self.state, self.frame, self.badge)
            return
        key = (self.state, self.frame, self.badge)
        if key not in self._icons:
            self._icons[key] = tray_face_icon(self.state, taskbar_theme(), frame=self.frame, badge=self.badge)
        self.tray.setIcon(self._icons[key])

    def _repaint(self) -> None:
        self._icons.clear()
        self._paint()

    def _next_frame(self) -> None:
        n = tray_frames(self.state)
        if n <= 1:
            self._frames.stop()
            return
        self.frame = (self.frame + 1) % n
        self._paint()

    # -- the hover card ------------------------------------------------------------------------------------------

    def _hotkey(self) -> str:
        combo = (getattr(self.island, "bindings", {}) or {}).get("voice") or ""
        return "+".join(part.strip().capitalize() for part in str(combo).split("+") if part.strip())

    def card_info(self) -> TrayCardInfo:
        """The card's rows for the state now, from what the island and the voice know (no content is logged)."""
        i = self.island.inputs
        state = self.current()
        app = str((i.call or {}).get("app") or getattr(i, "call_asking", "") or "the call")
        timer = self.island.call_time() if self._recording() else ""
        hot = self._hotkey()
        info = TrayCardInfo(state=state, listen=bool(getattr(self.voice, "wake_enabled", i.listening != "off")),
                            game=state == "game")
        last = getattr(self.island, "last_final", None)
        info.last = f"{last[0]} · {natural_when(last[2])}" if last and last[0] else "Nothing yet"
        run = i.run
        if state == "resting":
            info.line = "Resting"
            n = int(self.waiting() or 0)
            info.sub = (f"{n} card{'s' if n != 1 else ''} waiting for you" if n else
                        (f"Say “Yuki” or press {hot}" if hot else "Say “Yuki”"))
        elif state == "listening":
            info.line, info.sub = "Listening", (f"Talk now. {hot} to stop" if hot else "Talk now")
        elif state == "hearing":
            heard = " ".join(w for w, _c in (i.caption or [])[-8:]) or i.heard
            info.line, info.sub = "Hearing you", (f"“{heard}”" if heard else "Talk now")
        elif state == "speaking":
            info.line, info.sub = "Speaking", "Say “stop” to cut me off"
        elif state == "working":
            asked = ""
            if run is not None:
                from yuki.ui.island import _clean

                asked = _clean(run.request)
            info.line = f"Working on: {asked}" if asked else "Working"
            if run is not None:
                n = max(1, run.steps)
                info.sub = f"step {n} of {run.of}" if run.of else f"step {n}"
                info.task, info.task_kind = (run.line or "Getting ready…"), "run"
                info.of = run.of if run.of else max(n, 1)
                info.done, info.cur = n - 1, n - 1
            else:
                info.sub = i.transient or "Working it out"
        elif state == "needs-you":
            info.line = "Needs you"
            info.sub = "Say it, or press Answer" if i.question_voice else "Answer in the ask bar"
            info.task, info.task_kind = i.question, "question"
        elif state == "stopped":
            info.line, info.sub = "Voice stopped", i.voice_error or "Something broke"
            info.task, info.task_kind = "Try again, or pick a microphone in Settings", "voice"
        elif state == "call-ask":
            info.line, info.sub = f"On {app} · not recording", "Waiting for your answer on the card"
        elif state == "call":
            info.line, info.sub = f"On {app} · {timer}", "Recording only your mic"
        elif state == "off":
            info.line = "Voice is off"
            info.sub = {"quiet": "Quiet for a while", "call_off": "Paused for this call"}.get(
                i.listening, "I’m not listening or speaking")
        elif state == "game":
            info.line = "Quiet while you play"
            info.sub = f"On {app} · {timer} · your mic only" if timer else "Cards wait until you’re back"
        return info

    def show_card(self) -> None:
        """Windows says the pointer rests on the icon: the card grows out of it."""
        self._fold.stop()
        from yuki.ui.lattice_cards import TrayHoverCard

        if self.card is None:
            self.card = TrayHoverCard()
            self.card.hovered.connect(lambda inside: self._fold.stop() if inside else self._fold.start())
            self.card.stop.connect(self._on_stop)
            self.card.answer.connect(self._on_answer)
            self.card.retry.connect(lambda: self.island.retry_voice.emit())
            self.card.open_yuki.connect(self._on_open)
            self.card.listen_toggled.connect(self._on_listen)
        if self.card.isVisible() and not self.card.closing:
            return
        self._fill_card()
        try:
            rect = self.tray.geometry()
        except Exception:
            rect = QRect()
        if not rect.isValid() or rect.width() <= 0:
            pos = QCursor.pos()
            rect = QRect(pos.x() - 8, pos.y() - 8, 16, 16)
        self.card.open_from(rect)
        self._card_tick.start()
        log = getattr(self.island, "_log", None)
        if callable(log):
            log("tray_card", state=self.state)

    def pointer_left(self) -> None:
        """The pointer left the icon: the card folds back 300 ms later unless the pointer is on the card."""
        if self.card is not None and self.card.isVisible() and not self.card.underMouse():
            self._fold.start()

    def _hide_card(self, now: bool = False) -> None:
        self._fold.stop()
        if self.card is not None and self.card.isVisible() and (now or not self.card.underMouse()):
            self._card_tick.stop()
            self.card.fold()

    def _fill_card(self) -> None:
        if self.card is None:
            return
        if not self.card.isVisible():
            self._card_tick.stop()
            return
        self.card.set_info(self.card_info())

    def _on_stop(self) -> None:
        stop = getattr(self.island, "_on_stop", None)
        if callable(stop):
            stop()

    def _on_answer(self) -> None:
        self.island.answer_clicked.emit(int(self.island.inputs.question_id or 0))
        self._hide_card(now=True)

    def _on_open(self) -> None:
        self._hide_card(now=True)
        self.open_window()

    def _on_listen(self, on: bool) -> None:
        setter = getattr(self.voice, "set_wake", None)
        if callable(setter):
            setter(bool(on))
        self.refresh()


# -- the menu ---------------------------------------------------------------------------------------------------


def build_menu(ui: Any, app: Any, *, more: Any = None) -> Any:
    """The right-click menu (the mock's ``.traymenu``): Open Yuki, Listen for “Yuki” on / off, Pause listening for
    this call (only during a call), Settings, More (everything the old menu had), Quit Yuki."""
    from yuki.ui.lattice_cards import TrayMenu

    menu = TrayMenu()
    open_item = menu.item("Open Yuki", icon="memory", right="click")
    open_item.triggered.connect(lambda _c=False: ui.window_host.open_window())
    listen_item = menu.item("Listen for “Yuki”", icon="mic", right="on")
    listen_item.triggered.connect(lambda _c=False: ui.voice.set_wake(not bool(ui.voice.wake_enabled)))
    pause_item = menu.item("Pause listening for this call", icon="pause")
    pause_item.triggered.connect(
        lambda _c=False: ui.voice.set_call_off(getattr(ui.voice, "listening_state", "on") != "call_off"))
    menu.addSeparator()
    settings_item = menu.item("Settings", icon="settings")
    settings_item.triggered.connect(lambda _c=False: ui.window_host.open_window("settings"))
    if more is not None:
        more.setTitle("More")
        menu.addMenu(more)
    quit_item = menu.item("Quit Yuki", icon="x")
    quit_item.triggered.connect(lambda _c=False: app.quit())

    def follow() -> None:
        on = bool(getattr(ui.voice, "wake_enabled", True))
        menu.set_right(listen_item, "on" if on else "off", live=on)
        state = getattr(ui.voice, "listening_state", "on")
        in_call = bool(getattr(ui.voice, "call_active", False)) or state == "call_off"
        pause_item.setVisible(in_call)
        pause_item.setText("Listen again in this call" if state == "call_off" else "Pause listening for this call")

    menu.aboutToShow.connect(follow)
    follow()
    return menu


__all__ = ["LISTEN_OFF_TEXT", "NativeTray", "TRAY_FACES", "TRAY_GRID", "TRAY_STATES", "TrayCardInfo", "TrayWatch",
           "build_menu", "make_tray", "natural_when", "taskbar_theme", "tray_badge", "tray_face_icon",
           "tray_face_image", "tray_icon", "tray_state"]
