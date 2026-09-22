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
sent if another window has taken focus.  Being in the foreground is necessary but
not sufficient, so ``type_text`` additionally waits (see
:func:`wait_for_input_ready`) for the window's own GUI thread to report a focused
control, and refuses rather than typing into a window that is not listening yet.

Long or non-ASCII text is pasted rather than typed, which means borrowing the
user's clipboard.  It is borrowed, not spent: every format on it is snapshotted
byte-for-byte first and put back afterwards, so an image or a set of copied files
survives Yuki pasting a sentence.

After a click, the result says what the click *did*: the window that is in front
now and the control that has keyboard focus, named by role and text, and whether
that control takes typing.  Without it a missed click is indistinguishable from a
hit - on 2026-09-22 three clicks 30-100 px apart all "succeeded" while Spotify's
search field sat untouched - and the only way to find out was to take a
screenshot and look.  The facts come from ``GetForegroundWindow``,
``GetGUIThreadInfo`` and one UIA focused-element read, which together cost a few
milliseconds.

Nothing here sleeps: no glide, no per-control delay tables, no settle time.  The
one wait after a click is a condition poll for the focus to move, abandoned at a
deadline, because "focus did not move" is a real answer and not worth waiting out.
"""

from __future__ import annotations

import ctypes
import struct
import time

import pyautogui
import win32clipboard
import win32con

from yuki.actions import ActionResult
from yuki.perception.windows import (
    accepts_input,
    cursor_position,
    focused_control_hwnd,
    virtual_screen_bounds,
    window_info,
)

pyautogui.FAILSAFE = False  # a corner-of-screen cursor must not abort Yuki
pyautogui.PAUSE = 0  # we wait on conditions, never on the clock

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

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

# The clipboard and global-memory calls, likewise explicit: GetClipboardData and
# GlobalLock return pointers, and without a restype ctypes truncates them to a
# 32-bit int, which on a 64-bit process silently corrupts every handle.
_user32.OpenClipboard.argtypes = [ctypes.c_void_p]
_user32.OpenClipboard.restype = ctypes.c_int
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = ctypes.c_int
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = ctypes.c_int
_user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
_user32.EnumClipboardFormats.restype = ctypes.c_uint
_user32.GetClipboardData.argtypes = [ctypes.c_uint]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardFormatNameW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClipboardFormatNameW.restype = ctypes.c_int
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.restype = ctypes.c_int
_kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.restype = ctypes.c_void_p


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


def _send_text(text: str, expect_hwnd: int | None = None) -> tuple[int, int, str]:
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

    Typing a string is not instantaneous, so ``expect_hwnd`` is re-checked before
    every character rather than only once before the first.  Checking once is
    barely a guard: on 2026-09-22 Discord took the foreground part-way through
    ``"hello from yuki 215715"`` and the rest of the string was typed into the
    user's chat box, while the action still reported "typed 22 chars".  A
    foreground change now stops the typing there, and the caller is told how far
    it got, so it can say what the window really contains.

    Args:
        text: Characters to send.
        expect_hwnd: Window that must stay in the foreground.  ``None`` disables
            the check, which is only safe when the caller genuinely does not care
            where the text lands.

    Returns:
        ``(events_sent, events_expected, refusal)``.  ``expected`` counts only the
        characters actually attempted, so ``sent != expected`` still means Windows
        (or a lower-level hook, such as an anti-cheat driver) refused part of the
        input.  ``refusal`` is empty unless the foreground changed mid-string, in
        which case it says so and typing stopped at that character.
    """
    sent = expected = 0
    body = text.replace("\r\n", "\r").replace("\n", "\r")
    for index, char in enumerate(body):
        refusal = _foreground_mismatch(expect_hwnd)
        if refusal:
            return sent, expected, f"{refusal} after {index} of {len(body)} characters"
        events = _char_events(char)
        array = (_Input * len(events))(*events)
        expected += len(events)
        sent += int(
            _user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_Input))
        )
    return sent, expected, ""


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


#: How long an activated window is given to report a focused control before we
#: stop waiting for it.  Measured on this desktop: Windows 11 Notepad needs about
#: 50 ms after ``SetForegroundWindow``, so a second is generous without being a
#: delay anyone notices when it is not needed.
_READY_S = 1.0

#: How often :func:`wait_for_input_ready` re-asks.  Finer than the launch/focus
#: watchers, because the whole wait is usually over inside a few polls.
_READY_POLL_S = 0.005


def wait_for_input_ready(hwnd: int, *, timeout_s: float = _READY_S) -> tuple[bool, float]:
    """Wait until ``hwnd`` would actually receive keystrokes.

    This is the condition, not a sleep: it polls
    :func:`yuki.perception.windows.accepts_input` - the window is in the
    foreground *and* its own GUI thread reports keyboard focus on one of its
    controls - and returns the moment both are true.

    A window that has just been launched or activated is in the foreground before
    it is listening.  ``SendInput`` cheerfully accepts every event sent into that
    gap and reports success while the characters are dropped on the floor, which
    on this desktop turned a typed ``"hello world"`` into ``"ld"``.  Waiting for
    the window's own thread to name a focused control closes it.

    Returns:
        ``(ready, waited_ms)``.  ``ready`` is False only if the condition never
        held within ``timeout_s``; ``waited_ms`` is how long the wait took either
        way, for the caller to report.
    """
    started = time.perf_counter()
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        if accepts_input(hwnd):
            return True, (time.perf_counter() - started) * 1000.0
        if time.monotonic() >= deadline:
            return False, (time.perf_counter() - started) * 1000.0
        time.sleep(_READY_POLL_S)


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


#: How long the focus is watched for a change after a click.  A condition poll
#: with a deadline, not a settle sleep: a click that lands on a text field moves
#: the focus within a few milliseconds and the poll ends there, while a click that
#: hit nothing costs this much and is reported honestly as having moved nothing.
_FOCUS_WATCH_S = 0.2

#: How often that watch re-asks.  One read is ~5 ms, so this is as tight as it is
#: worth being.
_FOCUS_POLL_S = 0.01


def _read_focus():  # -> yuki.perception.tree.FocusInfo | None
    """What UIA says has focus, or ``None`` when UIA is unavailable.

    Imported here rather than at module scope: :mod:`yuki.perception.tree` builds a
    comtypes wrapper for UIAutomationCore at import, and sending a keystroke should
    not depend on that having happened.
    """
    try:
        from yuki.perception.tree import focused_element
    except Exception:
        return None
    try:
        return focused_element()
    except Exception:
        return None


def _focus_state() -> dict:
    """Everything cheap about where input would go right now.

    Three layers, coarsest first: which top-level window is in front, which of its
    controls its own GUI thread reports focus on, and - the only one that can see
    inside a window that keeps its whole UI in one HWND - what UIA says the focused
    element actually is.
    """
    foreground = int(_user32.GetForegroundWindow())
    info = window_info(foreground) if foreground else None
    return {
        "foreground_hwnd": foreground,
        "foreground_title": info.title if info else "",
        "foreground_process": info.process_name if info else "",
        "focused_control_hwnd": focused_control_hwnd(foreground) if foreground else 0,
        "focus": _read_focus(),
    }


def _watch_focus(before: dict, *, timeout_s: float = _FOCUS_WATCH_S) -> dict:
    """Poll until the focus differs from ``before``, or the deadline passes.

    A click's effect on focus is not instantaneous, so reading it back straight
    away would report the old state as often as the new one.  This waits for the
    condition - any of the three layers changed - and returns the moment it holds.
    """
    deadline = time.monotonic() + max(timeout_s, 0.0)
    state = _focus_state()
    while _focus_unmoved(before, state) and time.monotonic() < deadline:
        time.sleep(_FOCUS_POLL_S)
        state = _focus_state()
    return state


def _focus_unmoved(before: dict, after: dict) -> bool:
    """Whether focus is on the same thing it was before."""
    if before["foreground_hwnd"] != after["foreground_hwnd"]:
        return False
    if before["focused_control_hwnd"] != after["focused_control_hwnd"]:
        return False
    old, new = before.get("focus"), after.get("focus")
    if old is None or new is None:
        return True  # no UIA to tell us otherwise; the Win32 layers agreed
    return new.same_as(old)


def _describe_outcome(before: dict, after: dict) -> tuple[str, dict]:
    """A phrase for ``summary`` and the flat facts for ``details``.

    The phrase is what a person would say about the click - "focus now Edit
    'What do you want to play?'", "focus unchanged" - and the dict is the same
    thing without the prose, so the log keeps the handles and the model reading the
    summary does not have to.
    """
    focus = after.get("focus")
    facts: dict = {
        "foreground_hwnd": after["foreground_hwnd"],
        "foreground_title": after["foreground_title"],
        "foreground_process": after["foreground_process"],
        "focused_control_hwnd": after["focused_control_hwnd"],
        "foreground_changed": before["foreground_hwnd"] != after["foreground_hwnd"],
        "focus_changed": not _focus_unmoved(before, after),
    }
    if focus is not None and focus.ok:
        facts.update(
            {
                "focus_role": focus.role,
                "focus_name": focus.name,
                "focus_value": focus.value,
                "focus_accepts_text": focus.accepts_text,
                "focus_shortcut": focus.shortcut,
            }
        )
    parts: list[str] = []
    who = (
        f'{after["foreground_process"] or "unknown"} "{after["foreground_title"]}" '
        f'(hwnd {after["foreground_hwnd"]})'
        if after["foreground_hwnd"]
        else "no window"
    )
    if facts["foreground_changed"]:
        parts.append(f"foreground now {who}")
    else:
        parts.append(f"foreground still {who}")
    described = focus.describe() if focus is not None else "unknown"
    if facts["focus_changed"]:
        parts.append(f"focus now {described}")
    elif described in ("unknown", "nothing"):
        parts.append(f"focus unchanged ({described})")
    else:
        parts.append(f"focus unchanged, still {described}")
    return "; ".join(parts), facts


def _where_focus_is() -> tuple[str, dict]:
    """Where keyboard input would go right now: a phrase and the flat facts.

    For ``type_text``, which reports a single moment rather than a before/after,
    so the ``*_changed`` flags :func:`_describe_outcome` adds are left out.
    """
    state = _focus_state()
    facts = _describe_outcome(state, state)[1]
    facts.pop("foreground_changed", None)
    facts.pop("focus_changed", None)
    focus = state["focus"]
    described = focus.describe() if focus is not None else "unknown"
    phrase = (
        f"keyboard focus is on {described} in "
        f"{state['foreground_process'] or 'no window'}"
    )
    return phrase, facts


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

    Returns:
        ActionResult whose ``summary`` says what the click did, not just that it
        happened - which window is in front now and what has keyboard focus - and
        whose ``details`` carry ``foreground_hwnd``, ``focused_control_hwnd``, the
        focused control's ``focus_role``/``focus_name``/``focus_value``,
        ``focus_accepts_text``, and the ``focus_changed`` /
        ``foreground_changed`` flags.  A click that hit nothing says "focus
        unchanged", which is the signal that the coordinate was wrong; retrying the
        same point is then pointless.
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
    before = _focus_state()
    down, up = _MOUSEEVENTF[button]
    _move(x, y)
    for _ in range(clicks):
        _user32.mouse_event(down, 0, 0, 0, 0)
        _user32.mouse_event(up, 0, 0, 0, 0)
    details["cursor"] = cursor_position()
    label = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}.get(
        clicks, f"clicked {clicks}x"
    )
    outcome, facts = _describe_outcome(before, _watch_focus(before))
    details.update(facts)
    return _result(True, f"{label} {button} at ({x},{y}); {outcome}", details, started)


_GMEM_MOVEABLE = 0x0002

#: Total bytes we are willing to hold in Python while the clipboard is borrowed.
#: A 4K screenshot arrives as ~33 MB of CF_DIB plus a CF_DIBV5 copy plus a PNG, so
#: the budget has to be roomy; it exists only to stop something pathological from
#: turning "type a sentence" into a memory spike.
_CLIPBOARD_SNAPSHOT_BUDGET = 128 * 1024 * 1024

#: Formats whose clipboard "data" is not a block of global memory but a GDI or
#: application handle (or nothing at all, for owner-rendered data).  Copying the
#: bytes behind those handles is not what duplicating them means, and handing the
#: same handle back would give two owners the right to free it, so they are read
#: past and reported instead.  This is a Win32 fact about handle types, not a
#: judgement about content.
_UNDUPLICABLE_FORMATS: frozenset[int] = frozenset(
    {
        2,  # CF_BITMAP        - HBITMAP
        3,  # CF_METAFILEPICT  - METAFILEPICT wrapping an HMETAFILE
        9,  # CF_PALETTE       - HPALETTE
        14,  # CF_ENHMETAFILE  - HENHMETAFILE
        0x0080,  # CF_OWNERDISPLAY     - no data; the owner paints it
        0x0083,  # CF_DSPMETAFILEPICT
        0x008E,  # CF_DSPENHMETAFILE
    }
)

#: Names for the built-in formats, for the log only.  Registered formats
#: (``>= 0xC000``, e.g. "PNG", "HTML Format") carry their own name in Windows.
_CLIPBOARD_FORMAT_NAMES: dict[int, str] = {
    1: "CF_TEXT",
    2: "CF_BITMAP",
    3: "CF_METAFILEPICT",
    4: "CF_SYLK",
    5: "CF_DIF",
    6: "CF_TIFF",
    7: "CF_OEMTEXT",
    8: "CF_DIB",
    9: "CF_PALETTE",
    10: "CF_PENDATA",
    11: "CF_RIFF",
    12: "CF_WAVE",
    13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE",
    15: "CF_HDROP",
    16: "CF_LOCALE",
    17: "CF_DIBV5",
    0x0080: "CF_OWNERDISPLAY",
    0x0081: "CF_DSPTEXT",
    0x0082: "CF_DSPBITMAP",
    0x0083: "CF_DSPMETAFILEPICT",
    0x008E: "CF_DSPENHMETAFILE",
}


def _format_name(fmt: int) -> str:
    """Readable name for a clipboard format id, for logging."""
    known = _CLIPBOARD_FORMAT_NAMES.get(fmt)
    if known:
        return known
    buffer = ctypes.create_unicode_buffer(256)
    if _user32.GetClipboardFormatNameW(fmt, buffer, len(buffer)) > 0:
        return buffer.value
    if 0x0200 <= fmt <= 0x02FF:
        return f"CF_PRIVATE+{fmt - 0x0200}"
    if 0x0300 <= fmt <= 0x03FF:
        return f"CF_GDIOBJ+{fmt - 0x0300}"
    return f"format {fmt}"


def _open_clipboard(attempts: int = 20) -> bool:
    """Take the clipboard lock, retrying briefly while someone else holds it."""
    for _ in range(attempts):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.005)
    return False


class _ClipboardSnapshot:
    """Every duplicable format that was on the clipboard, and what was not.

    Attributes:
        items: ``(format id, bytes)`` in the order Windows enumerated them, which
            is the owner's own priority order - most-descriptive first - and the
            order they are put back in.
        kept: Names of the formats captured.
        skipped: Names of the formats that could not be captured, with why.
        empty: True when the clipboard held nothing at all.
    """

    __slots__ = ("items", "kept", "skipped", "empty")

    def __init__(self) -> None:
        self.items: list[tuple[int, bytes]] = []
        self.kept: list[str] = []
        self.skipped: list[str] = []
        self.empty = False


def _snapshot_clipboard() -> _ClipboardSnapshot | None:
    """Copy the whole clipboard out, format by format.

    Restoring only the text (which is all a paste needs to put *in*) silently
    destroys anything else the user had copied: an image, a group of files, a
    spreadsheet range.  So every format is enumerated and the bytes behind each
    global-memory handle are copied into Python.

    Returns:
        The snapshot, or ``None`` if the clipboard could not even be opened - in
        which case the caller should leave it alone rather than guess.
    """
    if not _open_clipboard():
        return None
    snapshot = _ClipboardSnapshot()
    total = 0
    try:
        fmt = _user32.EnumClipboardFormats(0)
        if fmt == 0:
            snapshot.empty = True
            return snapshot
        while fmt:
            name = _format_name(fmt)
            if fmt in _UNDUPLICABLE_FORMATS or 0x0200 <= fmt <= 0x03FF:
                snapshot.skipped.append(f"{name} (handle, not copyable)")
            else:
                try:
                    handle = _user32.GetClipboardData(fmt)
                    size = _kernel32.GlobalSize(handle) if handle else 0
                    if not handle:
                        snapshot.skipped.append(f"{name} (no data)")
                    elif not size:
                        snapshot.skipped.append(f"{name} (not global memory)")
                    elif total + size > _CLIPBOARD_SNAPSHOT_BUDGET:
                        snapshot.skipped.append(f"{name} ({size} bytes, over budget)")
                    else:
                        pointer = _kernel32.GlobalLock(handle)
                        if not pointer:
                            snapshot.skipped.append(f"{name} (could not be locked)")
                        else:
                            try:
                                data = ctypes.string_at(pointer, size)
                            finally:
                                _kernel32.GlobalUnlock(handle)
                            snapshot.items.append((fmt, data))
                            snapshot.kept.append(name)
                            total += size
                except Exception as exc:  # one awkward format must not lose the rest
                    snapshot.skipped.append(f"{name} ({type(exc).__name__})")
            fmt = _user32.EnumClipboardFormats(fmt)
    finally:
        _user32.CloseClipboard()
    return snapshot


def _restore_clipboard(snapshot: _ClipboardSnapshot | None) -> tuple[bool, list[str]]:
    """Put a snapshot back, best effort.

    Each format gets a fresh ``GMEM_MOVEABLE`` block holding the same bytes.
    Ownership of a block passes to the system the moment ``SetClipboardData``
    accepts it, so an accepted block is never freed here and a rejected one always
    is.

    Returns:
        ``(everything restored, names that failed)``.  ``snapshot`` of ``None``
        means there was nothing to restore and the clipboard is left untouched.
    """
    if snapshot is None:
        return False, []
    if not _open_clipboard():
        return False, ["clipboard busy"]
    failed: list[str] = []
    try:
        _user32.EmptyClipboard()
        for fmt, data in snapshot.items:
            handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, max(len(data), 1))
            if not handle:
                failed.append(f"{_format_name(fmt)} (out of memory)")
                continue
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                _kernel32.GlobalFree(handle)
                failed.append(f"{_format_name(fmt)} (could not be locked)")
                continue
            try:
                if data:
                    ctypes.memmove(pointer, data, len(data))
            finally:
                _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(fmt, handle):
                _kernel32.GlobalFree(handle)  # still ours: the clipboard refused it
                failed.append(f"{_format_name(fmt)} (rejected)")
    except Exception as exc:  # pragma: no cover - needs a live clipboard to fail
        failed.append(f"{type(exc).__name__}: {exc}")
    finally:
        _user32.CloseClipboard()
    return not failed, failed


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

    "The previous contents" means all of them, not just the text.  Every clipboard
    format is snapshotted and put back, so an image or a set of copied files is
    still there afterwards; ``details`` records exactly which formats came back
    and which could not (see :func:`_snapshot_clipboard`).

    With ``expect_hwnd`` the window is not only checked for being in the
    foreground but given up to a second to report a focused control before the
    first character goes out (:func:`wait_for_input_ready`); a window that never
    does gets no keystrokes at all and the caller is told, because characters sent
    into that gap are dropped while ``SendInput`` reports success.
    ``details["ready_ms"]`` records the wait.

    When typing does not happen, or only partly happens, the result names what
    actually has keyboard focus (role, text, and whether it takes text at all).
    "Nothing was typed" plus "focus is on a Button" is a diagnosis; "nothing was
    typed" on its own is a shrug.

    Args:
        text: what to type.
        press_enter: press Enter after the text.
        expect_hwnd: only send if this window is still in the foreground *and*
            ready to receive input.

    Returns:
        ActionResult.  ``details`` always carries where the text went or would have
        gone: ``foreground_hwnd``, ``focused_control_hwnd`` and the focused
        control's ``focus_role``/``focus_name``/``focus_accepts_text``.
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
    # Foreground is not the same thing as listening.  Before the first character
    # goes out, give the window a bounded chance to report a focused control; if
    # it never does, say so instead of typing into the void and reporting success.
    if expect_hwnd and (text or press_enter):
        ready, waited_ms = wait_for_input_ready(expect_hwnd)
        details["ready_ms"] = round(waited_ms, 1)
        details["ready"] = ready
        if not ready:
            info = window_info(int(expect_hwnd))
            who = f"hwnd {int(expect_hwnd)}"
            if info:
                who += f' ({info.process_name} "{info.title}")'
            where, facts = _where_focus_is()
            details.update(facts)
            return _result(
                False,
                f"{who} is not accepting keyboard input yet: no focused control "
                f"after {waited_ms:.0f} ms, so nothing was typed (typing now would "
                f"lose characters); {where}. Focus it again and retry.",
                details,
                started,
            )
    if not text:
        if press_enter:
            pyautogui.press("enter")
            return _result(True, "pressed enter (no text)", details, started)
        return _result(True, "nothing to type", details, started)

    if needs_clipboard:
        previous = _snapshot_clipboard()
        if not _set_clipboard_text(text):
            return _result(False, "could not write to the clipboard", details, started)
        # Snapshotting and rewriting the clipboard takes long enough for the
        # foreground to change, so the guard is asked again immediately before the
        # paste -- and the clipboard is put back before refusing, so a refusal
        # leaves nothing of the user's behind.
        refusal = _foreground_mismatch(expect_hwnd)
        if refusal:
            _restore_clipboard(previous)
            return _result(False, refusal, details, started)
        pyautogui.hotkey("ctrl", "v")
        consumed = _wait_clipboard_released(
            time.monotonic() + _CLIPBOARD_HANDOFF_S
        )
        restored, failed = _restore_clipboard(previous)
        details["clipboard_consumed"] = consumed
        details["clipboard_restored"] = restored
        details["clipboard_kept"] = previous.kept if previous else None
        details["clipboard_skipped"] = previous.skipped if previous else None
        if failed:
            details["clipboard_restore_failed"] = failed
    else:
        sent, expected, refusal = _send_text(text, expect_hwnd)
        details["events_sent"] = sent
        details["events_expected"] = expected
        if refusal:
            details["interrupted"] = refusal
            where, facts = _where_focus_is()
            details.update(facts)
            return _result(
                False,
                f"{refusal}; only the first part of the text reached the window; "
                f"{where}. Focus it again and retype the whole thing.",
                details,
                started,
            )
        if sent != expected:
            where, facts = _where_focus_is()
            details.update(facts)
            return _result(
                False,
                f"Windows accepted only {sent} of {expected} key events; the text "
                f"in the window is incomplete; {where}",
                details,
                started,
            )

    if press_enter:
        pyautogui.press("enter")
    preview = text if len(text) <= 60 else text[:60] + "…"
    # Where it landed, for the log and for a caller checking its work.  Read after
    # the keystrokes, not before: Enter may well have moved the focus on.
    details.update(_where_focus_is()[1])
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
