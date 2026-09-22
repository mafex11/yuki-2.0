"""Synthetic mouse and keyboard input.

Mouse events are sent with ``SetCursorPos`` + ``mouse_event`` at the cursor's
current position rather than through pyautogui's absolute path: pyautogui
normalises absolute coordinates against the *primary* monitor
(``65536 * x // width``) and clamps scroll coordinates to it, so every click on
a secondary monitor lands in the wrong place.  Text is typed with ``SendInput``
(one call per character, each carrying that character's own modifiers) rather
than pyautogui's ``keybd_event`` loop, so Windows reports how many events it
accepted and a silent drop becomes a failed action; key *names* for chords stay
pyautogui's, which are the contract.

Every input function takes an optional ``expect_hwnd``: when given, the
foreground window is checked immediately before anything is sent, and nothing is
sent if another window has taken focus.

Nothing here sleeps: no glide, no per-control delay tables, no settle time.
"""

from __future__ import annotations

import ctypes
import struct
import time

import pyautogui
import win32clipboard
import win32con

from yuki.actions import ActionResult
from yuki.perception.windows import cursor_position, virtual_screen_bounds, window_info

pyautogui.FAILSAFE = False  # a corner-of-screen cursor must not abort Yuki
pyautogui.PAUSE = 0  # we wait on conditions, never on the clock

_user32 = ctypes.windll.user32

_MOUSEEVENTF = {
    "left": (0x0002, 0x0004),  # down, up
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x01000
_WHEEL_DELTA = 120

#: Above this length per-character typing is slow enough that the clipboard is
#: the better transport, and long text is exactly what clipboard paste is for.
_PASTE_THRESHOLD_CHARS = 50

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_MAPVK_VK_TO_VSC = 0


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


# Explicit prototypes: VkKeyScanW returns a SHORT, and without a restype the
# "no such character" answer (-1) comes back as 65535.
_user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
_user32.VkKeyScanW.restype = ctypes.c_short
_user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
_user32.MapVirtualKeyW.restype = ctypes.c_uint
_user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
_user32.SendInput.restype = ctypes.c_uint


def _key_event(vk: int, scan: int, *, up: bool, unicode: bool = False) -> _Input:
    """One keyboard INPUT record."""
    flags = _KEYEVENTF_UNICODE if unicode else 0
    if up:
        flags |= _KEYEVENTF_KEYUP
    return _Input(
        type=_INPUT_KEYBOARD,
        union=_InputUnion(
            ki=_KeyBdInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )


def _char_events(char: str) -> list[_Input]:
    """Key events that produce one character on the current keyboard layout.

    ``VkKeyScanW`` gives the virtual key and the modifiers the active layout
    needs for this character, so the app sees ordinary key presses.  Characters
    the layout cannot produce (emoji, other scripts) fall back to
    ``KEYEVENTF_UNICODE``.

    Real virtual keys matter: Windows 11's Notepad (a WinUI app) processes
    ``VK_PACKET`` asynchronously and collapses a rapid run of them onto the last
    character -- ``"abc def"`` arrives as ``"abc fff"`` -- while ordinary key
    events always arrive intact.
    """
    # A character above the BMP (an emoji) is two UTF-16 code units, which
    # VkKeyScanW cannot even be handed; it always goes as a unicode packet, one
    # event pair per code unit, which is the surrogate pair Windows expects.
    raw = char.encode("utf-16-le")
    scanned = _user32.VkKeyScanW(char) if len(raw) == 2 else -1
    if scanned == -1:
        events: list[_Input] = []
        for unit in struct.unpack(f"<{len(raw) // 2}H", raw):
            events.append(_key_event(0, unit, up=False, unicode=True))
            events.append(_key_event(0, unit, up=True, unicode=True))
        return events
    vk = scanned & 0xFF
    modifier_state = (scanned >> 8) & 0xFF
    modifiers = [
        modifier
        for bit, modifier in ((1, _VK_SHIFT), (2, _VK_CONTROL), (4, _VK_MENU))
        if modifier_state & bit
    ]
    scan = _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
    events = [_key_event(modifier, 0, up=False) for modifier in modifiers]
    events.append(_key_event(vk, scan, up=False))
    events.append(_key_event(vk, scan, up=True))
    events.extend(_key_event(modifier, 0, up=True) for modifier in reversed(modifiers))
    return events


def _send_text(text: str) -> tuple[int, int]:
    """Send ``text`` as key events, one ``SendInput`` call per character.

    Per character, not per string: batching a whole string into one call is
    faster but measurably lossy on this desktop.  With ``KEYEVENTF_UNICODE``
    Windows 11's Notepad collapses a rapid run of ``VK_PACKET`` messages onto the
    last character (``"abc def"`` arrived as ``"abc fff"``); with real virtual
    keys a batch that toggles Shift part-way through loses the shifted run
    (``"Yuki: Hello, World! 42"`` arrived as ``"Yuki: 42"``), because a window
    translates queued key messages against the keyboard state it sees when it
    drains the queue, not the state each event was sent with.  One call per
    character keeps each character atomic with its own modifiers.

    Returns:
        ``(events_sent, events_expected)``; a short count means Windows (or a
        lower-level hook, such as an anti-cheat driver) refused part of the
        input.
    """
    sent = expected = 0
    for char in text.replace("\r\n", "\r").replace("\n", "\r"):
        events = _char_events(char)
        array = (_Input * len(events))(*events)
        expected += len(events)
        sent += int(
            _user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_Input))
        )
    return sent, expected


def _foreground_mismatch(expect_hwnd: int | None) -> str | None:
    """Reason to refuse to send input, or ``None`` when it is safe to send.

    Synthetic input goes wherever the keyboard focus happens to be, so an action
    aimed at one window must not fire into another that took the foreground in
    the meantime (a launcher finishing, a notification, the user alt-tabbing).
    Callers that know which window they are acting on pass its handle and get a
    plain refusal instead of keystrokes landing somewhere else.  This is
    plumbing safety: it compares handles and never looks at what the window is.
    """
    if not expect_hwnd:
        return None
    actual = int(_user32.GetForegroundWindow())
    if actual == int(expect_hwnd):
        return None
    info = window_info(actual) if actual else None
    title = info.title if info else ""
    return (
        f"Foreground window changed; input not sent "
        f"(expected hwnd {int(expect_hwnd)}, got {actual} {title!r})"
    )


#: How long we are willing to wait for the target app to read the clipboard
#: before restoring the previous contents.
_CLIPBOARD_HANDOFF_S = 0.5

_KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "cmd": "win",
    "windows": "win",
    "super": "win",
    "meta": "win",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "spacebar": "space",
    "plus": "add",
    "minus": "subtract",
}


def _result(
    ok: bool, summary: str, details: dict, started: float
) -> ActionResult:
    return ActionResult(
        ok=ok,
        summary=summary,
        details=details,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def normalize_key(key: str) -> str | None:
    """Map a key name onto a pyautogui key name, or ``None`` if unknown.

    Accepts the pyautogui names plus the readable variants a model is likely to
    emit: ``volume_mute`` -> ``volumemute``, ``Control`` -> ``ctrl``,
    ``Page Up`` -> ``pageup``.
    """
    if not isinstance(key, str) or not key:
        return None
    candidate = key.strip()
    if candidate in pyautogui.KEYBOARD_KEYS:
        return candidate
    lowered = candidate.lower()
    for form in (
        lowered,
        _KEY_ALIASES.get(lowered, ""),
        lowered.replace("_", "").replace("-", "").replace(" ", ""),
        _KEY_ALIASES.get(lowered.replace("_", "").replace("-", "").replace(" ", ""), ""),
    ):
        if form and form in pyautogui.KEYBOARD_KEYS:
            return form
    if len(candidate) == 1:  # a literal character is always typeable
        return candidate
    return None


def _in_screen_bounds(x: int, y: int) -> bool:
    left, top, right, bottom = virtual_screen_bounds()
    return left <= x < right and top <= y < bottom


def _move(x: int, y: int) -> None:
    """Put the cursor exactly on (x, y) - instantly, on any monitor."""
    _user32.SetCursorPos(int(x), int(y))


def click(
    x: int,
    y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    expect_hwnd: int | None = None,
) -> ActionResult:
    """Click at a screen coordinate: instant move, no glide, no post-delay.

    Args:
        x, y: screen pixels (any monitor; validated against the virtual desktop).
        button: ``"left"``, ``"right"`` or ``"middle"``.
        clicks: 1 for a single click, 2 for a double click, and so on.
        expect_hwnd: only send if this window is still in the foreground; when
            it is not, nothing is sent and the result is a failure naming the
            window that took focus instead.
    """
    started = time.perf_counter()
    details = {
        "x": x,
        "y": y,
        "button": button,
        "clicks": clicks,
        "expect_hwnd": expect_hwnd,
    }
    if button not in _MOUSEEVENTF:
        return _result(
            False,
            f"unknown mouse button {button!r}; use left, right or middle",
            details,
            started,
        )
    if clicks < 1:
        return _result(False, f"clicks must be >= 1, got {clicks}", details, started)
    if not _in_screen_bounds(x, y):
        return _result(
            False,
            f"({x},{y}) is outside the desktop {virtual_screen_bounds()}",
            details,
            started,
        )
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    down, up = _MOUSEEVENTF[button]
    _move(x, y)
    for _ in range(clicks):
        _user32.mouse_event(down, 0, 0, 0, 0)
        _user32.mouse_event(up, 0, 0, 0, 0)
    details["cursor"] = cursor_position()
    label = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}.get(
        clicks, f"clicked {clicks}x"
    )
    return _result(True, f"{label} {button} at ({x},{y})", details, started)


def _get_clipboard_text() -> str | None:
    """Current clipboard text, or ``None`` when it holds something else."""
    for _ in range(20):  # the clipboard is a shared lock; retry briefly
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.005)  # another process holds the clipboard lock
            continue
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return None
        except Exception:
            return None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    return None


def _set_clipboard_text(text: str | None) -> bool:
    """Replace the clipboard with ``text`` (or empty it when ``None``)."""
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.005)  # another process holds the clipboard lock
            continue
        try:
            win32clipboard.EmptyClipboard()
            if text is not None:
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            return True
        except Exception:
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    return False


def _wait_clipboard_released(deadline: float) -> bool:
    """Wait until no other window holds the clipboard open.

    An app handling Ctrl+V opens the clipboard while reading it.  Polling
    ``GetOpenClipboardWindow`` lets us restore the user's clipboard as soon as
    the paste has been consumed instead of sleeping a guessed interval.
    """
    saw_consumer = False
    while time.monotonic() < deadline:
        owner = _user32.GetOpenClipboardWindow()
        if owner:
            saw_consumer = True
        elif saw_consumer:
            return True
        time.sleep(0.005)
    return saw_consumer


def type_text(
    text: str, *, press_enter: bool = False, expect_hwnd: int | None = None
) -> ActionResult:
    """Type into whatever has keyboard focus.

    Short ASCII text is sent as key events, one ``SendInput`` call per character,
    and the action fails if Windows does not accept them all.  Text that is long
    or non-ASCII is pasted instead: the clipboard is swapped, Ctrl+V is sent, and
    the previous contents are restored once the target app has read it.

    Args:
        text: what to type.
        press_enter: press Enter after the text.
        expect_hwnd: only send if this window is still in the foreground.
    """
    started = time.perf_counter()
    if not isinstance(text, str):
        return _result(False, "text must be a string", {"text": text}, started)
    needs_clipboard = len(text) > _PASTE_THRESHOLD_CHARS or not text.isascii()
    details: dict = {
        "chars": len(text),
        "method": "clipboard" if needs_clipboard else "keystrokes",
        "press_enter": press_enter,
        "expect_hwnd": expect_hwnd,
    }
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    if not text:
        if press_enter:
            pyautogui.press("enter")
            return _result(True, "pressed enter (no text)", details, started)
        return _result(True, "nothing to type", details, started)

    if needs_clipboard:
        previous = _get_clipboard_text()
        if not _set_clipboard_text(text):
            return _result(False, "could not write to the clipboard", details, started)
        pyautogui.hotkey("ctrl", "v")
        consumed = _wait_clipboard_released(
            time.monotonic() + _CLIPBOARD_HANDOFF_S
        )
        restored = _set_clipboard_text(previous)
        details["clipboard_consumed"] = consumed
        details["clipboard_restored"] = restored
    else:
        sent, expected = _send_text(text)
        details["events_sent"] = sent
        details["events_expected"] = expected
        if sent != expected:
            return _result(
                False,
                f"Windows accepted only {sent} of {expected} key events; the text "
                f"in the window is incomplete",
                details,
                started,
            )

    if press_enter:
        pyautogui.press("enter")
    preview = text if len(text) <= 60 else text[:60] + "…"
    return _result(
        True,
        f"typed {len(text)} chars via {details['method']}"
        f"{' + enter' if press_enter else ''}: {preview!r}",
        details,
        started,
    )


def hotkey(*keys: str, expect_hwnd: int | None = None) -> ActionResult:
    """Press a key combination, e.g. ``("ctrl","t")``, ``("volume_mute",)``.

    Modifiers are held in order and released in reverse, exactly like a human
    chord.  Media keys (``volume_mute``, ``volume_up``, ``volume_down``,
    ``playpause``) work as single-key "chords".
    """
    started = time.perf_counter()
    details: dict = {"keys": list(keys), "expect_hwnd": expect_hwnd}
    if not keys:
        return _result(False, "no keys given", details, started)
    resolved: list[str] = []
    for key in keys:
        normalized = normalize_key(key)
        if normalized is None:
            return _result(
                False,
                f"unknown key {key!r} (use pyautogui key names, e.g. ctrl, alt, "
                f"win, enter, f5, volume_mute)",
                details,
                started,
            )
        resolved.append(normalized)
    details["resolved"] = resolved
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    pyautogui.hotkey(*resolved)
    return _result(True, f"pressed {'+'.join(resolved)}", details, started)


def press(
    key: str, *, times: int = 1, expect_hwnd: int | None = None
) -> ActionResult:
    """Press a single key ``times`` times with no delay between presses."""
    started = time.perf_counter()
    details: dict = {"key": key, "times": times, "expect_hwnd": expect_hwnd}
    normalized = normalize_key(key)
    if normalized is None:
        return _result(False, f"unknown key {key!r}", details, started)
    if times < 1:
        return _result(False, f"times must be >= 1, got {times}", details, started)
    details["resolved"] = normalized
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    pyautogui.press(normalized, presses=times, interval=0)
    suffix = f" x{times}" if times > 1 else ""
    return _result(True, f"pressed {normalized}{suffix}", details, started)


def scroll(
    x: int, y: int, *, dy: int = 0, dx: int = 0, expect_hwnd: int | None = None
) -> ActionResult:
    """Scroll at a point: ``dy`` wheel notches up (+) / down (-), ``dx`` right.

    The cursor is moved to (x, y) first because Windows delivers wheel events to
    the window under the cursor.
    """
    started = time.perf_counter()
    details = {"x": x, "y": y, "dy": dy, "dx": dx, "expect_hwnd": expect_hwnd}
    if not _in_screen_bounds(x, y):
        return _result(
            False,
            f"({x},{y}) is outside the desktop {virtual_screen_bounds()}",
            details,
            started,
        )
    if dy == 0 and dx == 0:
        return _result(False, "nothing to scroll: dy and dx are both 0", details, started)
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    _move(x, y)
    if dy:
        _user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(dy) * _WHEEL_DELTA, 0)
    if dx:
        _user32.mouse_event(_MOUSEEVENTF_HWHEEL, 0, 0, int(dx) * _WHEEL_DELTA, 0)
    parts = []
    if dy:
        parts.append(f"{abs(dy)} notch(es) {'up' if dy > 0 else 'down'}")
    if dx:
        parts.append(f"{abs(dx)} notch(es) {'right' if dx > 0 else 'left'}")
    return _result(True, f"scrolled {' and '.join(parts)} at ({x},{y})", details, started)
