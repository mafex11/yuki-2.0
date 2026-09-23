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
not sufficient, so ``type_text``, ``hotkey`` and ``press`` additionally wait
(see :func:`wait_for_input_ready`) for the window's own GUI thread to report a
focused control, and refuse rather than sending into a window that is not
listening yet.

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

A click also says what it *hit* and what that *caused*.  Before the button goes
down, the element under the point is named through UIA - role, name, and the
nearest named container ("clicked Button "Magic UI" in ToolBar "Bookmarks"") -
on a ~150 ms budget ("target element unknown" past it).  And the page the
foreground window shows (its Document element: title and address) is read
before and watched after, so "the page changed from "(25) Messaging |
LinkedIn" (linkedin.com/messaging) to "Magic UI" (magicui.design)" is a fact in
the result.  On 2026-09-23 a guessed coordinate hit a bookmark and navigated
the tab away; the result said only "focus unchanged", and the model spent three
minutes hunting for a LinkedIn tab that had merely navigated, where Back would
have done.  ``hotkey`` and ``press`` report the page the same way, because keys
navigate too.

Keyboard actions report what they caused as well: ``hotkey``/``press`` say where
focus went, ``type_text`` names the control that had focus when typing began,
and all three name any *new* top-level window the same process showed while
they ran (a suggestion list, a menu, a popup), from a before/after snapshot of
that process's visible top-level windows.

Nothing here sleeps: no glide, no per-control delay tables, no settle time.  The
one wait after a click is a condition poll for the focus to move, abandoned at a
deadline, because "focus did not move" is a real answer and not worth waiting out.
The waits after keyboard input (focus moving, a new window showing, a cleared
field reading empty) and the page watch after any input (the page differing from
before and titled) are condition polls with deadlines in the same way.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import time
from collections.abc import Callable

import pyautogui
import win32clipboard
import win32con
import win32gui
import win32process

from yuki.actions import ActionResult
from yuki.perception.windows import (
    accepts_input,
    cursor_position,
    focused_control_hwnd,
    is_cloaked,
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

#: The same watch after a hotkey or key press.  Longer than after a click, because
#: what a shortcut opens (a command bar, a find box, a dialog) has to be built
#: before it can take focus, where a click lands on a control that already
#: exists.  Still a condition poll: it ends the moment focus moves.
_KEY_FOCUS_WATCH_S = 0.4

#: How often that watch re-asks.  One read is ~5 ms, so this is as tight as it is
#: worth being.
_FOCUS_POLL_S = 0.01

#: After typing, how long to watch for a new window of the target process (a
#: suggestion list opens a moment after the characters are processed, not when
#: ``SendInput`` returns).  A condition poll: it ends the moment one shows.
#: ``hotkey``/``press`` need no extra watch of their own - they read the windows
#: once their focus watch has ended.
_POPUP_WATCH_S = 0.15

#: How long ``type_text(clear=True)`` watches the focused control's value for
#: becoming empty after select-all + delete.  Ends the moment it reads empty.
_CLEAR_VERIFY_S = 0.3

#: New windows named individually in a summary; the rest are counted.
_MAX_NEW_WINDOWS_SHOWN = 3

#: Text at least this long goes in by paste rather than keystrokes when the
#: focused control is a writable Value field, or the window was slow to accept
#: input.  A paste is one input event; a burst of keystrokes into a control that
#: is busy redrawing (a suggestion list rebuilding after each character) can be
#: dropped by the application after ``SendInput`` has accepted every one.
_PASTE_INTO_FIELD_CHARS = 12

#: A readiness wait at least this long says the window is busy right now - the
#: condition under which keystrokes get dropped.
_SLOW_READY_MS = 100.0

#: After typing, how long the read-back may wait for the typed text to show up
#: in the control, at most.  The watch ends the moment it does, and also once
#: the contents have stopped changing for :data:`_READBACK_SETTLE_S` without it:
#: characters that were going to arrive have arrived by then.
_READBACK_MAX_S = 2.0
_READBACK_SETTLE_S = 0.35

#: How often the read-back re-reads.  One read is a UIA round trip on its own
#: thread (~5-20 ms), so there is no point asking faster.
_READBACK_POLL_S = 0.03


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


def _describe_outcome(
    before: dict, after: dict, *, page_phrase: str = "", page_changed: bool = False
) -> tuple[str, dict]:
    """A phrase for ``summary`` and the flat facts for ``details``.

    The phrase is what a person would say about the click - "focus now Edit
    'What do you want to play?'", "focus unchanged" - and the dict is the same
    thing without the prose, so the log keeps the handles and the model reading the
    summary does not have to.

    A foreground window that stayed but was retitled says so, before -> after;
    when ``page_phrase`` (see :func:`_describe_page_change`) already tells the
    same story (``page_changed``), only the new title is given.  ``page_phrase``
    goes between the window and the focus.
    """
    focus = after.get("focus")
    title_before = before.get("foreground_title", "")
    facts: dict = {
        "foreground_hwnd": after["foreground_hwnd"],
        "foreground_title": after["foreground_title"],
        "foreground_title_before": title_before,
        "foreground_process": after["foreground_process"],
        "focused_control_hwnd": after["focused_control_hwnd"],
        "foreground_changed": before["foreground_hwnd"] != after["foreground_hwnd"],
        "focus_changed": not _focus_unmoved(before, after),
    }
    retitled = (
        not facts["foreground_changed"]
        and bool(after["foreground_hwnd"])
        and title_before != after["foreground_title"]
    )
    facts["foreground_title_changed"] = retitled
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
    elif retitled:
        still = (
            f'foreground still {after["foreground_process"] or "unknown"} '
            f'(hwnd {after["foreground_hwnd"]})'
        )
        if page_changed:
            parts.append(f"{still}, title now {_quoted(after['foreground_title'], 90)}")
        else:
            parts.append(
                f"{still}, title changed from {_quoted(title_before, 90)} to "
                f"{_quoted(after['foreground_title'], 90)}"
            )
    else:
        parts.append(f"foreground still {who}")
    if page_phrase:
        parts.append(page_phrase)
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
    for flag in ("foreground_changed", "focus_changed", "foreground_title_before", "foreground_title_changed"):
        facts.pop(flag, None)
    focus = state["focus"]
    described = focus.describe() if focus is not None else "unknown"
    phrase = (
        f"keyboard focus is on {described} in "
        f"{state['foreground_process'] or 'no window'}"
    )
    return phrase, facts


def _input_target(state: dict) -> tuple[str, dict]:
    """What input is about to go into: a phrase and ``typed_into_*`` facts.

    Read *before* the first character, so the result can say truthfully where
    the text went even when that is not where it was meant to go - a page under
    a command bar that had not opened yet, say.  Reported, never enforced.
    """
    focus = state.get("focus")
    described = focus.describe() if focus is not None else "unknown"
    facts: dict = {
        "typed_into_hwnd": state["foreground_hwnd"],
        "typed_into_process": state["foreground_process"],
        "typed_into_control_hwnd": state["focused_control_hwnd"],
    }
    if focus is not None and focus.ok:
        facts.update(
            {
                "typed_into_role": focus.role,
                "typed_into_name": focus.name,
                "typed_into_accepts_text": focus.accepts_text,
            }
        )
    return f"{described} in {state['foreground_process'] or 'no window'}", facts


def _window_pid(hwnd: int) -> int:
    """Process id owning ``hwnd``, or 0."""
    if not hwnd:
        return 0
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return 0


def _process_top_windows(pid: int) -> dict[int, tuple[str, str, tuple[int, int, int, int]]]:
    """Visible, uncloaked, non-empty top-level windows of ``pid``.

    Returns:
        ``{hwnd: (title, class_name, (left, top, right, bottom))}``.  Hidden
        windows are left out on purpose: a popup a process keeps around hidden
        and shows when needed has "appeared" when it is shown.
    """
    found: dict[int, tuple[str, str, tuple[int, int, int, int]]] = {}
    if not pid:
        return found

    def collect(hwnd: int, _: object) -> bool:
        try:
            if _window_pid(hwnd) != pid or not win32gui.IsWindowVisible(hwnd):
                return True
            if is_cloaked(hwnd):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right <= left or bottom <= top:
                return True
            found[hwnd] = (
                win32gui.GetWindowText(hwnd),
                win32gui.GetClassName(hwnd),
                (left, top, right, bottom),
            )
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        pass
    return found


def _windows_before(expect_hwnd: int | None) -> tuple[int, dict]:
    """``(pid, windows)`` of the process input is going to: the before-snapshot.

    The process is ``expect_hwnd``'s when given, otherwise the foreground
    window's - whichever the keystrokes are aimed at.
    """
    hwnd = int(expect_hwnd) if expect_hwnd else int(_user32.GetForegroundWindow())
    pid = _window_pid(hwnd)
    return pid, _process_top_windows(pid)


def _windows_appeared(pid: int, before: dict, *, timeout_s: float = 0.0) -> list[dict]:
    """Top-level windows of ``pid`` that are showing now and were not before.

    Polls until at least one has appeared or ``timeout_s`` has passed (0: look
    once).  Returns ``[{"hwnd", "title", "class_name", "bounds"}, ...]``.
    """
    if not pid:
        return []
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        now = _process_top_windows(pid)
        fresh = [hwnd for hwnd in now if hwnd not in before]
        if fresh or time.monotonic() >= deadline:
            break
        time.sleep(_FOCUS_POLL_S)
    return [
        {
            "hwnd": hwnd,
            "title": now[hwnd][0],
            "class_name": now[hwnd][1],
            "bounds": list(now[hwnd][2]),
        }
        for hwnd in fresh
    ]


def _describe_appeared(windows: list[dict]) -> str:
    """``"a new window appeared (class X) at (l,t) WxH"``, or ``""`` for none."""
    if not windows:
        return ""
    described = []
    for window in windows[:_MAX_NEW_WINDOWS_SHOWN]:
        left, top, right, bottom = window["bounds"]
        title = f' "{window["title"][:60]}"' if window["title"] else ""
        described.append(
            f"(class {window['class_name']}){title} at ({left},{top}) "
            f"{right - left}x{bottom - top}"
        )
    more = len(windows) - _MAX_NEW_WINDOWS_SHOWN
    head = (
        "a new window of the same process appeared"
        if len(windows) == 1
        else f"{len(windows)} new windows of the same process appeared"
    )
    return f"{head} " + "; ".join(described) + (f"; and {more} more" if more > 0 else "")


# ---------------------------------------------------------------------------
# What a click is about to hit, and what the input changed
# ---------------------------------------------------------------------------
#: Budget for naming the element under a click before it is sent.  A UIA
#: ``ElementFromPoint`` is a few milliseconds; the frame descent it sometimes
#: needs (see :func:`_start_hit_test`) measured 30-55 ms on a browser window.
#: Past this the click goes out anyway and the result says "target element
#: unknown": naming the target must never be why a click is slow or fails.
_HIT_TEST_S = 0.15

#: The worker stops starting new UIA calls this long before the caller gives up
#: on it, so what it found so far is published in time to be used.
_HIT_MARGIN_S = 0.015

#: Ancestors looked at for a named container ("in ToolBar "Bookmarks"").
_HIT_CONTEXT_LEVELS = 3

#: Deepest the frame descent goes (a browser's bookmark button is 7 levels down).
_HIT_DESCENT_LEVELS = 40

#: Budget for one read of the page a window shows (see :func:`_page_at`):
#: typically one ``ElementFromHandle`` plus one cached ``FindFirst``, 2-20 ms.
_PAGE_READ_S = 0.3

#: After input, how long the page is watched for changing.  A condition poll: it
#: ends the moment the page differs from before and has its title, or when the
#: foreground window changed and its page (if any) has been read.  Navigation
#: commits a few hundred milliseconds after the click that starts it, so input
#: that changes nothing in a window showing a page costs this much - the price
#: of "page unchanged" being a fact rather than a guess.  A window without a
#: page is read once and not watched.
_PAGE_WATCH_S = 0.8

#: How often the page watch re-reads.
_PAGE_POLL_S = 0.04

#: Levels climbed from the element at a window's centre to the page containing
#: it, when no child window holds the page (see :func:`_page_at`).
_PAGE_CLIMB_LEVELS = 40

#: How much of a name / an address a summary shows (details keep them whole).
_NAME_CHARS = 70
_URL_CHARS = 70

_CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
_UIA_BOUNDING_RECT = 30001
_UIA_CONTROL_TYPE = 30003
_UIA_NAME = 30005
_UIA_AUTOMATION_ID = 30011
_UIA_NATIVE_WINDOW_HANDLE = 30020
_UIA_IS_VALUE_AVAILABLE = 30043
_UIA_VALUE = 30045
_UIA_DOCUMENT = 50030  # UIA_DocumentControlTypeId: a page / rich document
_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_CHILDREN = 2
_GA_ROOT = 2


def _clip(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _quoted(text: str, limit: int = _NAME_CHARS) -> str:
    return f'"{_clip(text, limit)}"'


def _role(control_type: object) -> str:
    """UIA ControlType id -> role name, the same names the window tree uses."""
    try:
        from yuki.perception.tree import _role_name

        return _role_name(control_type)
    except Exception:
        return f"ControlType{control_type}"


def _is_address(value: str | None) -> bool:
    """Whether a UIA Value is an address (``https://...``), not document text."""
    try:
        from yuki.perception.tree import is_address
    except Exception:
        return bool(value) and "://" in value and not any(c.isspace() for c in value)
    return is_address(value)


def _contains(bounds: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, right, bottom = bounds
    return left <= x < right and top <= y < bottom


def _area(bounds: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = bounds
    return max(right - left, 0) * max(bottom - top, 0)


def _is_window_root(hwnd: int) -> bool:
    """Whether a UIA element's own handle is a top-level window or the desktop:
    the element *is* the window, which names nothing the summary does not."""
    if not hwnd:
        return False
    try:
        return hwnd == int(win32gui.GetDesktopWindow()) or int(
            win32gui.GetAncestor(hwnd, _GA_ROOT)
        ) == hwnd
    except Exception:
        return False


def _new_automation() -> object:
    """A CUIAutomation object for the calling (worker) thread.

    The comtypes wrapper is the one :mod:`yuki.perception.tree` generated at
    import; imported here, not at module scope, so that sending input never
    depends on UIA having loaded.
    """
    import comtypes.client

    try:
        from yuki.perception.tree import _uia_core

        module = _uia_core()
    except Exception:
        module = comtypes.client.GetModule("UIAutomationCore.dll")
    return comtypes.client.CreateObject(
        _CUIAUTOMATION_CLSID, interface=module.IUIAutomation  # type: ignore[attr-defined]
    )


def _cache_request(automation: object) -> object:
    """One cache request for every property the hit test and page read use."""
    request = automation.CreateCacheRequest()  # type: ignore[attr-defined]
    for prop in (
        _UIA_CONTROL_TYPE,
        _UIA_NAME,
        _UIA_AUTOMATION_ID,
        _UIA_NATIVE_WINDOW_HANDLE,
        _UIA_BOUNDING_RECT,
        _UIA_IS_VALUE_AVAILABLE,
        _UIA_VALUE,
    ):
        request.AddProperty(prop)
    request.TreeScope = _TREE_SCOPE_ELEMENT
    return request


def _element_facts(element: object) -> dict:
    """The cached facts of one element, as plain data."""
    get = element.GetCachedPropertyValue  # type: ignore[attr-defined]
    control_type = get(_UIA_CONTROL_TYPE)
    try:
        rect = element.CachedBoundingRectangle  # type: ignore[attr-defined]
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        bounds = (0, 0, 0, 0)
    value = None
    if get(_UIA_IS_VALUE_AVAILABLE):
        raw = get(_UIA_VALUE)
        if isinstance(raw, str) and raw:
            value = raw[:500]
    try:
        native = int(get(_UIA_NATIVE_WINDOW_HANDLE) or 0)
    except Exception:
        native = 0
    name = get(_UIA_NAME)
    automation_id = get(_UIA_AUTOMATION_ID)
    return {
        "role": _role(control_type),
        "control_type": control_type if isinstance(control_type, int) else 0,
        "name": name.strip() if isinstance(name, str) else "",
        "automation_id": automation_id.strip() if isinstance(automation_id, str) else "",
        "value": value,
        "bounds": bounds,
        "native_hwnd": native,
    }


def _context_facts(facts: dict) -> dict:
    return {key: facts[key] for key in ("role", "name", "automation_id")}


class _Pending:
    """A UIA read running on its own thread with its own COM apartment.

    ``job(automation, shared)`` writes what it finds into ``shared`` as it goes,
    so a caller that stops waiting at the budget still gets everything found up
    to then; a provider that blocks costs the budget, never the calling thread.
    """

    def __init__(self, job: Callable[[object, dict], None], name: str, budget_s: float):
        self.shared: dict = {}
        self.budget_s = budget_s
        self._started = time.perf_counter()
        self._thread = threading.Thread(target=self._run, args=(job,), name=name, daemon=True)
        self._thread.start()

    def _run(self, job: Callable[[object, dict], None]) -> None:
        try:
            import comtypes
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            self.shared["error"] = f"{type(exc).__name__}: {exc}"
            return
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass  # already initialised for this thread
        try:
            job(_new_automation(), self.shared)
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            self.shared["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def wait(self) -> dict:
        """Wait for the job, at most until its budget is spent; ``shared`` then."""
        remaining = self.budget_s - (time.perf_counter() - self._started)
        self._thread.join(max(remaining, 0.0))
        return self.shared

    @property
    def timed_out(self) -> bool:
        return self._thread.is_alive()

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000.0, 1)


def _start_hit_test(x: int, y: int) -> _Pending:
    """Start naming the element at (x, y): what a click there would hit.

    ``ElementFromPoint`` first.  Its answer is trusted only when its rectangle
    contains the point: a browser keeps each tab's page in a child window whose
    rectangle can reach over the browser's own toolbars (measured on this
    desktop: a page window starting at y=0 behind toolbars ending at y=120), and
    for a point in that overlap ``ElementFromPoint`` answers with the page, well
    away from the point, while the real click lands on the toolbar.  In that case
    the element is found by descending the point's top-level window from its UIA
    root, child by child, into the smallest element containing the point -
    leaving out Document elements, because a page's content is what
    ``ElementFromPoint`` answers for, and the frame's tree also carries every
    background tab's page, reported on screen with a stale rectangle that would
    swallow the point.  Rectangles and control types only; nothing here knows
    which program drew the window.  Nothing is clicked, focused or hovered.
    """
    try:
        window = int(win32gui.GetAncestor(win32gui.WindowFromPoint((int(x), int(y))), _GA_ROOT))
    except Exception:
        window = 0
    stop_at = time.monotonic() + _HIT_TEST_S - _HIT_MARGIN_S

    def job(automation: object, shared: dict) -> None:
        from ctypes import wintypes

        shared["window_hwnd"] = window
        request = _cache_request(automation)
        element = automation.ElementFromPointBuildCache(  # type: ignore[attr-defined]
            wintypes.POINT(int(x), int(y)), request
        )
        facts = _element_facts(element) if element else None
        if facts is not None and (
            _contains(facts["bounds"], x, y) or not _area(facts["bounds"])
        ):
            target = {**facts, "via": "point", "context": None}
            shared["target"] = target
            walker = automation.ControlViewWalker  # type: ignore[attr-defined]
            current = element
            for _ in range(_HIT_CONTEXT_LEVELS):
                if time.monotonic() >= stop_at:
                    return
                current = walker.GetParentElementBuildCache(current, request)
                if not current:
                    return
                above = _element_facts(current)
                if _is_window_root(above["native_hwnd"]):
                    return
                if above["name"]:
                    target["context"] = _context_facts(above)
                    return
            return
        if not window:
            shared["error"] = "no window at the point"
            return
        root = automation.ElementFromHandleBuildCache(window, request)  # type: ignore[attr-defined]
        if not root:
            shared["error"] = "the window at the point did not answer"
            return
        path = [_element_facts(root)]
        condition = automation.ControlViewCondition  # type: ignore[attr-defined]
        current = root
        for _ in range(_HIT_DESCENT_LEVELS):
            if time.monotonic() >= stop_at:
                break
            children = current.FindAllBuildCache(_TREE_SCOPE_CHILDREN, condition, request)
            best, best_facts = None, None
            for index in range(children.Length if children else 0):
                child = children.GetElement(index)
                child_facts = _element_facts(child)
                if child_facts["control_type"] == _UIA_DOCUMENT or not _contains(
                    child_facts["bounds"], x, y
                ):
                    continue
                if best_facts is None or _area(child_facts["bounds"]) <= _area(
                    best_facts["bounds"]
                ):
                    best, best_facts = child, child_facts
            if best is None:
                break
            current = best
            path.append(best_facts)
            context = next(
                (
                    _context_facts(above)
                    for above in path[-2:0:-1][:_HIT_CONTEXT_LEVELS]
                    if above["name"] and not _is_window_root(above["native_hwnd"])
                ),
                None,
            )
            shared["target"] = {**best_facts, "via": "frame", "context": context}
        if "target" not in shared:
            shared["error"] = (
                "the element UIA reported at the point does not contain it, and "
                "no element of the window's frame does"
            )

    return _Pending(job, "yuki-uia-hit", _HIT_TEST_S)


def _finish_hit_test(pending: _Pending) -> tuple[dict | None, str]:
    """``(target, "")``, or ``(None, why the target is unknown)``."""
    shared = pending.wait()
    target = shared.get("target")
    if not isinstance(target, dict):
        if shared.get("error"):
            reason = str(shared["error"])
        elif pending.timed_out:
            reason = f"UIA did not answer within {_HIT_TEST_S * 1000:.0f} ms"
        else:
            reason = "UIA named no element at the point"
        return None, reason
    target = dict(target)
    target["context"] = dict(target["context"]) if target.get("context") else None
    target["partial"] = pending.timed_out
    target["hit_ms"] = pending.elapsed_ms
    window = int(shared.get("window_hwnd") or 0)
    info = window_info(window) if window else None
    target["window_hwnd"] = window
    target["window_process"] = info.process_name if info else ""
    target["window_title"] = info.title if info else ""
    return target, ""


def _describe_target(target: dict | None, foreground_hwnd: int) -> str:
    """``Button "Magic UI" in ToolBar "Bookmarks"``, or ``""`` when unknown."""
    if not target:
        return ""
    parts = [target.get("role") or "element"]
    if target.get("name"):
        parts.append(_quoted(target["name"]))
    elif target.get("automation_id"):
        parts.append(f"(id {_quoted(target['automation_id'], 40)})")
    value = target.get("value")
    if target.get("control_type") != _UIA_DOCUMENT and _is_address(value):
        parts.append(f"(url {_short_url(value)})")
    context = target.get("context")
    if context:
        parts.append(f"in {context.get('role') or 'element'} {_quoted(context['name'])}")
    window = target.get("window_hwnd")
    if window and foreground_hwnd and window != foreground_hwnd:
        parts.append(
            f"in another window, {target.get('window_process') or 'unknown'} "
            f"{_quoted(target.get('window_title') or '', 60)} (hwnd {window})"
        )
    return " ".join(parts)


def _client_centre(hwnd: int) -> tuple[int, int] | None:
    """Screen coordinates of the middle of ``hwnd``'s client area."""
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return None
        x, y = win32gui.ClientToScreen(hwnd, ((left + right) // 2, (top + bottom) // 2))
        return int(x), int(y)
    except Exception:
        return None


#: Child windows at a window's centre asked for a page, at most.
_PAGE_CANDIDATE_WINDOWS = 12


def _child_windows_at(hwnd: int, x: int, y: int) -> list[int]:
    """Visible descendant windows of ``hwnd`` whose rectangle holds (x, y).

    In ``EnumChildWindows`` order: each parent's children top of the stacking
    order first, each followed by its own descendants.  Transparent children
    are kept - a browser's page windows are ``WS_EX_TRANSPARENT`` - and so are
    windows under another child, because a frame can lay a transparent input
    layer over the window that draws the page (measured on this desktop: a
    WinUI bridge window over a Chromium page window).  The first of these that
    holds a Document is the page (see :func:`_page_at`); a browser's background
    tabs' page windows are below the front tab's, so the front tab's comes first.
    """
    found: list[int] = []

    def collect(child: int, _: object) -> bool:
        try:
            if win32gui.IsWindowVisible(child) and _contains(
                tuple(win32gui.GetWindowRect(child)), x, y
            ):
                found.append(int(child))
        except Exception:
            pass
        return len(found) < _PAGE_CANDIDATE_WINDOWS

    try:
        win32gui.EnumChildWindows(hwnd, collect, None)
    except Exception:
        pass  # pywin32 reports a callback that stopped the enumeration as an error
    return found


def _page_facts(facts: dict, hwnd: int, via: str) -> dict:
    value = facts.get("value")
    return {
        "hwnd": hwnd,
        "title": facts.get("name", ""),
        "url": value if _is_address(value) else "",
        "via": via,
    }


def _page_at(automation: object, hwnd: int, deadline: float) -> dict | None:
    """The page ``hwnd`` visibly shows: ``{"hwnd", "title", "url", "via"}``.

    Only the Document element is read, never its content.  "Visibly" is decided
    at the centre of the window's client area.  The child windows there (a
    browser draws each tab's page in a child window of its own, the front tab's
    on top; see :func:`_child_windows_at`) are asked in turn, each for its UIA
    root and that root's children, in one cached call, until one holds a
    Document - measured 1-16 ms on a browser window, where the first Document of
    the whole window's tree was a background tab's.  A window with no such
    child has the element at its centre climbed until a Document, giving up on
    reaching the window itself or an element of another window, and not tried
    at all when another window covers the centre.  The Document's Name is the page title and, when its Value is an
    address, the Value is the URL.  ``None``: no page.  Raises
    ``TimeoutError`` when ``deadline`` passes first, so that running out of
    time is never mistaken for "this window shows no page".
    """
    if not hwnd:
        return None
    try:
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
    except Exception:
        return None
    centre = _client_centre(hwnd)
    if centre is None:
        return None
    request = _cache_request(automation)
    condition = automation.CreatePropertyCondition(  # type: ignore[attr-defined]
        _UIA_CONTROL_TYPE, _UIA_DOCUMENT
    )
    for child in _child_windows_at(hwnd, *centre):
        if time.monotonic() >= deadline:
            raise TimeoutError("the page read ran out of time")
        try:
            element = automation.ElementFromHandle(child)  # type: ignore[attr-defined]
        except Exception:
            continue  # closed meanwhile, or its provider would not answer
        if not element:
            continue
        document = element.FindFirstBuildCache(
            _TREE_SCOPE_ELEMENT | _TREE_SCOPE_CHILDREN, condition, request
        )
        if document:
            return _page_facts(_element_facts(document), hwnd, "child window")
    if time.monotonic() >= deadline:
        raise TimeoutError("the page read ran out of time")
    try:
        on_top = int(win32gui.GetAncestor(win32gui.WindowFromPoint(centre), _GA_ROOT))
    except Exception:
        on_top = 0
    if on_top != hwnd:
        return None  # another window covers the centre: its page is not this one's
    from ctypes import wintypes

    element = automation.ElementFromPointBuildCache(  # type: ignore[attr-defined]
        wintypes.POINT(*centre), request
    )
    walker = automation.ControlViewWalker  # type: ignore[attr-defined]
    for _ in range(_PAGE_CLIMB_LEVELS):
        if not element:
            return None
        facts = _element_facts(element)
        if facts["control_type"] == _UIA_DOCUMENT:
            return _page_facts(facts, hwnd, "centre")
        native = facts["native_hwnd"]
        if native:
            try:
                root = int(win32gui.GetAncestor(native, _GA_ROOT))
            except Exception:
                root = 0
            if native == hwnd or root != hwnd:
                return None  # reached the window itself, or not this window at all
        if time.monotonic() >= deadline:
            raise TimeoutError("the page read ran out of time")
        element = walker.GetParentElementBuildCache(element, request)
    return None


def _start_page_read(hwnd: int) -> _Pending:
    """Start reading the page ``hwnd`` shows (the before-snapshot)."""

    def job(automation: object, shared: dict) -> None:
        shared["page"] = _page_at(automation, hwnd, time.monotonic() + _PAGE_READ_S)

    return _Pending(job, "yuki-uia-page-before", _PAGE_READ_S)


def _finish_page_read(pending: _Pending) -> tuple[bool, dict | None, dict]:
    """``(known, page, facts)``: ``known`` is False when the read did not finish."""
    shared = pending.wait()
    facts: dict = {"page_read_ms": pending.elapsed_ms}
    if "page" not in shared:
        facts["page_read_error"] = str(
            shared.get("error") or f"no answer within {_PAGE_READ_S * 1000:.0f} ms"
        )
        return False, None, facts
    return True, shared["page"], facts


def _same_page(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return (a.get("title"), a.get("url")) == (b.get("title"), b.get("url"))


def _titled(page: dict) -> bool:
    """Whether a page has its title yet (a loading page is named by its address)."""
    return bool(page.get("title")) and page.get("title") != page.get("url")


def _page_settled(
    before_hwnd: int, before: dict | None, before_known: bool, hwnd: int, page: dict | None
) -> bool:
    """Whether the page watch has its answer (see :data:`_PAGE_WATCH_S`)."""
    if hwnd != before_hwnd:
        return page is None or _titled(page)  # another window: one answer is enough
    if not before_known or before is None:
        return True  # nothing to compare with: one read
    if page is None:
        return False  # the page is gone for now (mid-navigation): keep looking
    return not _same_page(before, page) and _titled(page)


def _start_page_watch(before_hwnd: int, before: dict | None, before_known: bool) -> _Pending:
    """Start watching the foreground window's page for changing after input."""

    def job(automation: object, shared: dict) -> None:
        deadline = time.monotonic() + _PAGE_WATCH_S
        polls = 0
        while True:
            hwnd = int(_user32.GetForegroundWindow())
            polls += 1
            shared["polls"] = polls
            try:
                # Each read gets its own budget, not what is left of the watch:
                # a read cut short says nothing, and must not replace the last
                # one that finished (the caller takes the latest finished read).
                page = _page_at(automation, hwnd, time.monotonic() + _PAGE_READ_S)
            except TimeoutError:
                shared["timeouts"] = shared.get("timeouts", 0) + 1
            else:
                shared["after"] = (hwnd, page)
                if _page_settled(before_hwnd, before, before_known, hwnd, page):
                    return
            if time.monotonic() >= deadline:
                return
            time.sleep(_PAGE_POLL_S)

    return _Pending(job, "yuki-uia-page-watch", _PAGE_WATCH_S + _PAGE_POLL_S)


def _finish_page_watch(pending: _Pending) -> tuple[bool, int, dict | None, dict]:
    """``(known, hwnd, page, facts)`` from the last poll the watch finished."""
    shared = pending.wait()
    facts: dict = {"page_watch_ms": pending.elapsed_ms, "page_polls": shared.get("polls", 0)}
    if shared.get("timeouts"):
        facts["page_read_timeouts"] = shared["timeouts"]
    after = shared.get("after")
    if not isinstance(after, tuple):
        facts["page_watch_error"] = str(
            shared.get("error") or f"no answer within {_PAGE_WATCH_S * 1000:.0f} ms"
        )
        return False, 0, None, facts
    hwnd, page = after
    return True, int(hwnd), page, facts


def _short_url(url: str, other: str | None = None) -> str:
    """``linkedin.com/messaging`` for ``https://www.linkedin.com/messaging/``.

    Scheme, ``www.``, query and fragment are left out of http(s) addresses,
    unless that would make ``url`` read the same as a different ``other``.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return _clip(url, _URL_CHARS)
    if parts.scheme not in ("http", "https"):
        return _clip(url, _URL_CHARS)
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    short = host + parts.path.rstrip("/")
    if other and other != url and _short_url(other) == short:
        tail = (f"?{parts.query}" if parts.query else "") + (
            f"#{parts.fragment}" if parts.fragment else ""
        )
        short = short + tail if tail else url
    return _clip(short, _URL_CHARS)


def _page_phrase(page: dict, other: dict | None = None) -> str:
    """``"Magic UI" (magicui.design)``; ``magicui.design (no title yet)``."""
    url = page.get("url") or ""
    short = _short_url(url, (other or {}).get("url")) if url else ""
    if not _titled(page):
        return f"{short} (no title yet)" if short else "(untitled)"
    return f"{_quoted(page['title'])} ({short})" if short else _quoted(page["title"])


def _describe_page_change(
    before_hwnd: int,
    before: dict | None,
    after_hwnd: int,
    after: dict | None,
    *,
    before_known: bool = True,
    after_known: bool = True,
) -> tuple[str, dict]:
    """A phrase for ``summary`` and the before/after facts for ``details``.

    ``page_changed`` is True / False, or ``None`` when one side was not read.
    """
    facts: dict = {"page_before": before, "page_after": after}
    if not after_known or not before_known:
        facts["page_changed"] = None
        if after_known and after is not None:
            return f"page now {_page_phrase(after)}", facts
        return "", facts
    facts["page_changed"] = not _same_page(before, after)
    if after_hwnd != before_hwnd:
        if after is None:
            return "", facts
        return f"the new foreground window shows {_page_phrase(after)}", facts
    if before is None and after is None:
        return "", facts
    if before is None:
        return f"a page is showing now: {_page_phrase(after)}", facts
    if after is None:
        return f"the page {_page_phrase(before)} could not be read afterwards", facts
    if _same_page(before, after):
        return f"page unchanged: {_page_phrase(before)}", facts
    if before.get("url") == after.get("url") and _titled(before) and _titled(after):
        if not after.get("url"):  # a document that is not a web page
            return (
                f"the document changed from {_quoted(before['title'])} to "
                f"{_quoted(after['title'])}"
            ), facts
        return (
            f"the page title changed from {_quoted(before['title'])} to "
            f"{_quoted(after['title'])} (same url)"
        ), facts
    return (
        f"the page changed from {_page_phrase(before, after)} to "
        f"{_page_phrase(after, before)}"
    ), facts


def _report_effects(
    before: dict,
    page_hwnd: int,
    page_before: dict | None,
    page_known: bool,
    watch: _Pending,
    *,
    focus_timeout_s: float,
    details: dict,
) -> str:
    """After input: watch focus and the page, and say what changed.

    The focus watch runs here while the page watch runs on its own thread, so
    the two waits overlap.  The window title is read again at the end: the page
    watch can outlast the focus watch, and a navigation retitles the window
    when it commits.
    """
    after = _watch_focus(before, timeout_s=focus_timeout_s)
    after_known, after_hwnd, page_after, watch_facts = _finish_page_watch(watch)
    if after["foreground_hwnd"] and int(_user32.GetForegroundWindow()) == after["foreground_hwnd"]:
        info = window_info(after["foreground_hwnd"])
        if info:
            after = {**after, "foreground_title": info.title}
    page_phrase, page_facts = _describe_page_change(
        page_hwnd,
        page_before,
        after_hwnd,
        page_after,
        before_known=page_known,
        after_known=after_known,
    )
    outcome, facts = _describe_outcome(
        before, after, page_phrase=page_phrase, page_changed=bool(page_facts["page_changed"])
    )
    details.update(facts)
    details.update(page_facts)
    details.update(watch_facts)
    return outcome


def _clear_focused(expect_hwnd: int | None, details: dict) -> tuple[str | None, str]:
    """Select all and delete in the focused control, then check it reads empty.

    Refuses when UIA names a focused control that does not take text: select-all
    + delete aimed at "the field" but landing on a list, a page or a canvas
    could remove something else.  When the control exposes a Value pattern the
    result is verified (a condition poll, :data:`_CLEAR_VERIFY_S`); when it does
    not, the keys are sent and the result says the clear could not be checked.

    Returns:
        ``(failure, phrase)``: ``failure`` is ``None`` when it is safe to type
        next, otherwise why not; ``phrase`` describes what the clear did.
        ``details`` gets ``cleared`` (True / False / None for unverifiable) and
        the value before and after.
    """
    focus = _read_focus()
    known = focus is not None and focus.ok
    details["clear_target"] = focus.describe() if focus is not None else "unknown"
    if known:
        details["clear_value_before"] = focus.value
    if known and not focus.accepts_text:
        details["cleared"] = False
        return (
            f"did not clear: keyboard focus is on {focus.describe()}, which does not "
            f"take text, so select-all + delete there could remove something else",
            "",
        )
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        details["cleared"] = False
        return refusal, ""
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    if not known or not getattr(focus, "has_value", False):
        details["cleared"] = None
        return None, (
            "sent select-all + delete (the control exposes no value, so the clear "
            "could not be checked)"
        )
    deadline = time.monotonic() + _CLEAR_VERIFY_S
    after = _read_focus()
    while (
        after is None or not after.ok or (after.same_as(focus) and after.value)
    ) and time.monotonic() < deadline:
        time.sleep(_FOCUS_POLL_S)
        after = _read_focus()
    if after is None or not after.ok:
        details["cleared"] = None
        return None, "sent select-all + delete (UIA did not answer, so the clear could not be checked)"
    details["clear_value_after"] = after.value
    if not after.same_as(focus):
        details["cleared"] = False
        return f"focus moved to {after.describe()} during select-all + delete", ""
    if after.value:
        details["cleared"] = False
        return (
            f"select-all + delete did not empty {focus.describe()}: its value is "
            f"still {after.value[:80]!r}",
            "",
        )
    details["cleared"] = True
    return None, f"cleared {focus.describe()} (value now empty)"


def _choose_method(text: str, target: object, ready_ms: float | None) -> tuple[bool, str]:
    """``(paste, reason)``: whether to paste ``text`` rather than type it, and why.

    Decided on the text and on facts about the target, never on which app it is:
    non-ASCII or long text is always pasted (see :data:`_PASTE_THRESHOLD_CHARS`);
    text of :data:`_PASTE_INTO_FIELD_CHARS` or more is pasted when the focused
    control reports a writable Value pattern, or when the window took
    :data:`_SLOW_READY_MS` or more to accept input.
    """
    if not text.isascii():
        return True, "the text is not plain ASCII"
    if len(text) > _PASTE_THRESHOLD_CHARS:
        return True, f"the text is longer than {_PASTE_THRESHOLD_CHARS} characters"
    if len(text) >= _PASTE_INTO_FIELD_CHARS:
        if (
            target is not None
            and getattr(target, "ok", False)
            and getattr(target, "has_value", False)
            and getattr(target, "accepts_text", False)
        ):
            return True, (
                "the focused control is a text field with a Value pattern, and one "
                "paste cannot be partly dropped the way a burst of keystrokes can"
            )
        if ready_ms is not None and ready_ms >= _SLOW_READY_MS:
            return True, (
                f"the window took {ready_ms:.0f} ms to accept input, so it is busy "
                f"and a burst of keystrokes could be dropped"
            )
    return False, "short text"


def _read_text(caret_chars: int):  # -> yuki.perception.tree.FocusText | None
    """What the focused control holds, or ``None`` when UIA is unavailable."""
    try:
        from yuki.perception.tree import focused_text
    except Exception:
        return None
    try:
        return focused_text(caret_chars=caret_chars)
    except Exception:
        return None


def _squash(text: str) -> str:
    """Text for "did it arrive" comparison: no whitespace, case-folded.

    A single-line field turns a newline into nothing or a space and some fields
    change case as they go; neither is a dropped character.
    """
    return "".join(text.split()).casefold()


def _shown(text: str, limit: int = 80) -> str:
    """``text`` quoted for a summary, keeping its end when it is long."""
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else "…" + flat[-limit:])


def _read_back(text: str, target: object) -> tuple[bool | None, str, dict]:
    """Check that ``text`` is now in the focused control.

    Polls :func:`_read_text` until the control's contents contain ``text``,
    until they have stopped changing for :data:`_READBACK_SETTLE_S`, or until
    :data:`_READBACK_MAX_S`.

    Returns:
        ``(verdict, phrase, facts)``: ``verdict`` is True when the text is there,
        False when the control was read and it is not, and None when the
        contents could not be read or checked (no Value/Text pattern, a
        password field, focus moved to another control meanwhile).
    """
    want = _squash(text)
    caret_chars = len(text) + 32
    started = time.monotonic()
    deadline = started + _READBACK_MAX_S
    last: str | None = None
    changed_at = started
    while True:
        seen = _read_text(caret_chars)
        now = time.monotonic()
        waited_ms = round((now - started) * 1000.0, 1)
        if seen is None or not seen.ok or not seen.readable:
            reason = getattr(seen, "reason", "") or "UIA is unavailable"
            return None, f"field contents not readable ({reason})", {
                "readback": "unreadable",
                "readback_reason": reason,
                "readback_ms": waited_ms,
            }
        if (
            target is not None
            and getattr(target, "ok", False)
            and not seen.same_control(target)
        ):
            moved_to = " ".join(
                part for part in (seen.role or "element", f'"{seen.name}"' if seen.name else "") if part
            )
            return None, (
                f"field contents not checked: focus moved to {moved_to} while typing"
            ), {"readback": "moved", "readback_ms": waited_ms}
        facts = {
            "readback_source": seen.source,
            "readback_text": seen.text[:500],
            "readback_complete": seen.complete,
            "readback_ms": waited_ms,
        }
        if want in _squash(seen.text):
            facts["readback"] = "arrived"
            where = "the text before the caret is" if seen.source == "caret" else "field now contains"
            return True, f"{where} {_shown(seen.text)}", facts
        if seen.text != last:
            last, changed_at = seen.text, now
        elif now - changed_at >= _READBACK_SETTLE_S or now >= deadline:
            if seen.source == "document" and not seen.complete:
                facts["readback"] = "unreadable"
                return None, "field contents too long to check", facts
            facts["readback"] = "missing"
            return False, _shown(seen.text, 120), facts
        if now >= deadline:
            facts["readback"] = "missing"
            return False, _shown(seen.text, 120), facts
        time.sleep(_READBACK_POLL_S)


def _refuse_unless_ready(
    expect_hwnd: int | None, details: dict, started: float, *, withheld: str
) -> ActionResult | None:
    """Wait for ``expect_hwnd`` to be listening; a failure result if it never is.

    Being in the foreground is not the same as listening: until the window's own
    GUI thread names a focused control, keystrokes are dropped while
    ``SendInput`` reports them accepted.  Bounded by
    :func:`wait_for_input_ready`'s own timeout (about a second), and skipped
    entirely without ``expect_hwnd``.

    Args:
        withheld: what did not happen, for the summary ("nothing was typed").

    Returns:
        ``None`` when it is safe to send; otherwise the failure to return, naming
        where keyboard focus actually is.
    """
    if not expect_hwnd:
        return None
    ready, waited_ms = wait_for_input_ready(expect_hwnd)
    details["ready_ms"] = round(waited_ms, 1)
    details["ready"] = ready
    if ready:
        return None
    info = window_info(int(expect_hwnd))
    who = f"hwnd {int(expect_hwnd)}"
    if info:
        who += f' ({info.process_name} "{info.title}")'
    where, facts = _where_focus_is()
    details.update(facts)
    return _result(
        False,
        f"{who} is not accepting keyboard input yet: no focused control "
        f"after {waited_ms:.0f} ms, so {withheld}; {where}. Focus it again and retry.",
        details,
        started,
    )


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
        ActionResult whose ``summary`` says what the click hit and what it did,
        not just that it happened: the element under the point, named before the
        click (:func:`_start_hit_test`, ~150 ms budget; "target element unknown"
        past it), then which window is in front now (retitled: before -> after),
        which page it shows (before -> after, see :func:`_describe_page_change`)
        and what has keyboard focus.  On 2026-09-23 a guessed coordinate hit a
        bookmark and navigated the tab away, and a result that said only "focus
        unchanged" sent the model hunting three minutes for a tab that had
        merely navigated.  ``details`` carry ``target`` (role, name,
        automation_id, value, bounds, context, window, via) or
        ``target_unknown_reason``; ``page_before``/``page_after``
        (``{hwnd, title, url, via}`` or ``None``) and ``page_changed``
        (``None``: not known); ``foreground_hwnd``/``foreground_title``/
        ``foreground_title_before``, ``focused_control_hwnd``, the focused
        control's ``focus_role``/``focus_name``/``focus_value``,
        ``focus_accepts_text``, and the ``focus_changed`` /
        ``foreground_changed`` / ``foreground_title_changed`` flags.  A click
        that hit nothing says "focus unchanged" and "page unchanged", which is
        the signal that the coordinate was wrong; retrying the same point is
        then pointless.
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
    # Before anything is sent: what is under the point, and which page the
    # foreground window shows - read concurrently, each on a budget, neither
    # able to stop the click.
    page_hwnd = int(_user32.GetForegroundWindow())
    hit = _start_hit_test(x, y)
    page_read = _start_page_read(page_hwnd)
    target, unknown_reason = _finish_hit_test(hit)
    page_known, page_before, page_facts = _finish_page_read(page_read)
    details["target"] = target
    if target is None:
        details["target_unknown_reason"] = unknown_reason
    details.update(page_facts)
    # The reads took time; the guard is asked again right before sending.
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    before = _focus_state()
    down, up = _MOUSEEVENTF[button]
    _move(x, y)
    for _ in range(clicks):
        _user32.mouse_event(down, 0, 0, 0, 0)
        _user32.mouse_event(up, 0, 0, 0, 0)
    watch = _start_page_watch(page_hwnd, page_before, page_known)
    details["cursor"] = cursor_position()
    outcome = _report_effects(
        before,
        page_hwnd,
        page_before,
        page_known,
        watch,
        focus_timeout_s=_FOCUS_WATCH_S,
        details=details,
    )
    return _result(
        True,
        _click_summary(button, clicks, x, y, _describe_target(target, page_hwnd), outcome),
        details,
        started,
    )


def _click_summary(button: str, clicks: int, x: int, y: int, target: str, outcome: str) -> str:
    """``clicked Button "Magic UI" in ToolBar "Bookmarks" at (3200,100); <outcome>``.

    ``target`` is :func:`_describe_target`'s phrase; empty means the hit test
    found nothing, which the summary says rather than hiding.
    """
    verb = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}.get(
        clicks, f"clicked {clicks}x"
    )
    if button != "left":
        verb = f"{button}-{verb}"
    head = f"{verb} {target} at ({x},{y})" if target else (
        f"{verb} at ({x},{y}), target element unknown"
    )
    return f"{head}; {outcome}" if outcome else head


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
    text: str,
    *,
    press_enter: bool = False,
    clear: bool = False,
    expect_hwnd: int | None = None,
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

    With ``clear`` the focused control is emptied first, with select-all +
    delete, as part of the same action (see :func:`_clear_focused`): refused when
    the focused control does not take text, and checked to read empty afterwards
    when it exposes a Value pattern.  A clear that did not work means nothing is
    typed.

    Afterwards the result names any new top-level window of the target process
    that showed while typing (``details["new_windows"]``) - a suggestion list,
    say, that the next Enter would pick from.

    ``SendInput`` accepting every key event is not the application keeping every
    character, so once the text is sent the focused control is read back through
    UIA (see :func:`_read_back`), *before* Enter: the summary says what it holds
    ("field now contains ..."), and if it does not contain the typed text the
    action fails, Enter is not pressed, and the summary says what the field holds
    instead.  A control whose contents cannot be read is reported as such and
    the action goes on as before.  Which path the text took - keystrokes or one
    paste - is chosen by :func:`_choose_method` and reported in
    ``details["method"]``/``details["method_reason"]``.

    Args:
        text: what to type (may be empty with ``clear`` to only clear).
        press_enter: press Enter after the text.
        clear: select all and delete in the focused control first.
        expect_hwnd: only send if this window is still in the foreground *and*
            ready to receive input.

    Returns:
        ActionResult.  ``details`` always carries where the text went or would have
        gone: ``foreground_hwnd``, ``focused_control_hwnd`` and the focused
        control's ``focus_role``/``focus_name``/``focus_accepts_text``.  On
        success the summary also names what had focus just *before* the first
        character (``typed_into_*`` in ``details``): "typed 43 chars into Document
        "(1515) YouTube"" is how text that went into the wrong control shows up.
    """
    started = time.perf_counter()
    if not isinstance(text, str):
        return _result(False, "text must be a string", {"text": text}, started)
    details: dict = {
        "chars": len(text),
        "press_enter": press_enter,
        "clear": bool(clear),
        "expect_hwnd": expect_hwnd,
    }
    refusal = _foreground_mismatch(expect_hwnd)
    if refusal:
        return _result(False, refusal, details, started)
    # Foreground is not the same thing as listening.  Before the first character
    # goes out, give the window a bounded chance to report a focused control; if
    # it never does, say so instead of typing into the void and reporting success.
    if text or press_enter or clear:
        not_ready = _refuse_unless_ready(
            expect_hwnd,
            details,
            started,
            withheld="nothing was typed (typing now would lose characters)",
        )
        if not_ready is not None:
            return not_ready
    if not text and not press_enter and not clear:
        return _result(True, "nothing to type", details, started)

    # Which windows the target process is showing, so anything the typing opens
    # (a suggestion list, a menu) can be named afterwards.
    watched_pid, windows_before = _windows_before(expect_hwnd)

    def appeared(timeout_s: float) -> str:
        windows = _windows_appeared(watched_pid, windows_before, timeout_s=timeout_s)
        details["new_windows"] = windows
        return _describe_appeared(windows)

    def summary(*parts: str) -> str:
        return "; ".join(part for part in parts if part)

    cleared = ""
    if clear:
        failure, cleared = _clear_focused(expect_hwnd, details)
        if failure:
            where, facts = _where_focus_is()
            details.update(facts)
            return _result(
                False,
                summary(f"{failure}; nothing was typed", where, appeared(0.0)),
                details,
                started,
            )
    if not text:
        if press_enter:
            into, facts = _input_target(_focus_state())
            details.update(facts)
            pyautogui.press("enter")
            return _result(
                True,
                summary(cleared, f"pressed enter (no text) in {into}", appeared(_POPUP_WATCH_S)),
                details,
                started,
            )
        return _result(True, summary(cleared, appeared(0.0)), details, started)

    # Where the text is about to go, read before the first character.  Reported
    # on success as well as failure: a field that had not taken focus yet leaves
    # the text in whatever did have it, and "typed 43 chars" alone hides that.
    target_state = _focus_state()
    target = target_state.get("focus")
    into, facts = _input_target(target_state)
    details.update(facts)
    needs_clipboard, method_reason = _choose_method(text, target, details.get("ready_ms"))
    details["method"] = "clipboard" if needs_clipboard else "keystrokes"
    details["method_reason"] = method_reason

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

    preview = text if len(text) <= 60 else text[:60] + "…"
    # SendInput accepting every event is not the app keeping every character: an
    # app busy redrawing can drop them.  Read the control back - before Enter, so
    # a half-arrived text is never submitted.
    verdict, contents, facts = _read_back(text, target)
    details.update(facts)
    if verdict is False:
        where, facts = _where_focus_is()
        details.update(facts)
        return _result(
            False,
            summary(
                cleared,
                f"Windows accepted all {len(text)} characters (sent via "
                f"{details['method']}), but the application did not keep them: "
                f"{into} now holds {contents}, not the text that was typed"
                + ("; Enter was not pressed" if press_enter else ""),
                "select all and delete in the field, then type it again",
                appeared(0.0),
            ),
            details,
            started,
        )
    if press_enter:
        # The read-back took time; check the guard again before submitting.
        refusal = _foreground_mismatch(expect_hwnd)
        if refusal:
            return _result(
                False,
                summary(f"typed {len(text)} chars into {into}, then {refusal}; Enter was not pressed", contents),
                details,
                started,
            )
        pyautogui.press("enter")
    # Where it landed, for the log and for a caller checking its work.  Read after
    # the keystrokes, not before: Enter may well have moved the focus on.
    details.update(_where_focus_is()[1])
    return _result(
        True,
        summary(
            cleared,
            f"typed {len(text)} chars via {details['method']} into {into}: {preview!r}",
            contents + (" (before Enter)" if press_enter and verdict else ""),
            "then pressed Enter" if press_enter else "",
            appeared(_POPUP_WATCH_S),
        ),
        details,
        started,
    )


def _page_before_keys(details: dict) -> tuple[int, dict | None, bool]:
    """``(hwnd, page, known)``: the foreground window's page before keys go out.

    Keyboard navigation (Enter on a link, Alt+Left, Ctrl+L + Enter) changes the
    page just as a click does, so ``hotkey`` and ``press`` take the same
    before-snapshot and run the same watch afterwards.
    """
    hwnd = int(_user32.GetForegroundWindow())
    known, page, facts = _finish_page_read(_start_page_read(hwnd))
    details.update(facts)
    return hwnd, page, known


def hotkey(*keys: str, expect_hwnd: int | None = None) -> ActionResult:
    """Press a key combination, e.g. ``("ctrl","t")``, ``("volume_mute",)``.

    Modifiers are held in order and released in reverse, exactly like a human
    chord.  Media keys (``volume_mute``, ``volume_up``, ``volume_down``,
    ``playpause``) work as single-key "chords".

    With ``expect_hwnd`` the window must also be ready for input (see
    :func:`_refuse_unless_ready`), or nothing is sent.  Afterwards the focus is
    watched for up to :data:`_KEY_FOCUS_WATCH_S` and the result says where it is
    now - "focus now Edit ... (takes text)" or "focus unchanged, still Document
    ..." - with the same ``focus_*``/``*_changed`` details as :func:`click`.  A
    shortcut meant to open a text field that reports "focus unchanged" did not
    open it (yet), and typing next would go into whatever still has focus.  Any
    new top-level window of the target process that showed meanwhile is named
    too (``details["new_windows"]``), and so is what happened to the page the
    foreground window shows (before -> after, watched for up to
    :data:`_PAGE_WATCH_S`; ``page_before``/``page_after``/``page_changed`` in
    ``details``), so a key that navigates says where it went.
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
    not_ready = _refuse_unless_ready(
        expect_hwnd, details, started, withheld="the keys were not sent"
    )
    if not_ready is not None:
        return not_ready
    watched_pid, windows_before = _windows_before(expect_hwnd)
    page_hwnd, page_before, page_known = _page_before_keys(details)
    refusal = _foreground_mismatch(expect_hwnd)  # the page read took time
    if refusal:
        return _result(False, refusal, details, started)
    before = _focus_state()
    pyautogui.hotkey(*resolved)
    watch = _start_page_watch(page_hwnd, page_before, page_known)
    outcome = _report_effects(
        before,
        page_hwnd,
        page_before,
        page_known,
        watch,
        focus_timeout_s=_KEY_FOCUS_WATCH_S,
        details=details,
    )
    details["new_windows"] = _windows_appeared(watched_pid, windows_before)
    popup = _describe_appeared(details["new_windows"])
    return _result(
        True,
        f"pressed {'+'.join(resolved)}; {outcome}" + (f"; {popup}" if popup else ""),
        details,
        started,
    )


def press(
    key: str, *, times: int = 1, expect_hwnd: int | None = None
) -> ActionResult:
    """Press a single key ``times`` times with no delay between presses.

    Guarded, readiness-checked and reported exactly like :func:`hotkey`: the
    result says where keyboard focus is after the presses and what happened to
    the foreground window's page ("the page changed from ... to ...").
    """
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
    not_ready = _refuse_unless_ready(
        expect_hwnd, details, started, withheld="the key was not sent"
    )
    if not_ready is not None:
        return not_ready
    watched_pid, windows_before = _windows_before(expect_hwnd)
    page_hwnd, page_before, page_known = _page_before_keys(details)
    refusal = _foreground_mismatch(expect_hwnd)  # the page read took time
    if refusal:
        return _result(False, refusal, details, started)
    before = _focus_state()
    pyautogui.press(normalized, presses=times, interval=0)
    watch = _start_page_watch(page_hwnd, page_before, page_known)
    outcome = _report_effects(
        before,
        page_hwnd,
        page_before,
        page_known,
        watch,
        focus_timeout_s=_KEY_FOCUS_WATCH_S,
        details=details,
    )
    details["new_windows"] = _windows_appeared(watched_pid, windows_before)
    popup = _describe_appeared(details["new_windows"])
    suffix = f" x{times}" if times > 1 else ""
    return _result(
        True,
        f"pressed {normalized}{suffix}; {outcome}" + (f"; {popup}" if popup else ""),
        details,
        started,
    )


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
