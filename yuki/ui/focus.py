"""Taking the keyboard for one of Yuki's own windows, past the foreground lock.

Windows only lets a process move the foreground if it is, roughly, the process the
user is already talking to (it owns the foreground window, or it provided the last
input event). Yuki is a tray app summoned by a global hotkey, so it is never that
process at the moment it needs focus: ``SetForegroundWindow`` -- and Qt's
``activateWindow()``, which is just that call -- quietly fails and the overlay
appears without the keyboard.

:func:`force_foreground` is the standard way through, in escalating steps, each
checked by asking Windows which window is actually in front afterwards:

1. ``AttachThreadInput(foreground thread -> ours)`` so both threads share one input
   state, then ``BringWindowToTop``, ``SetForegroundWindow``, ``SetActiveWindow``,
   ``SetFocus``, then detach.
2. If that did not stick (typically: the foreground window belongs to an elevated
   process, so the attach is refused), inject one key-up of an unassigned virtual
   key. Injected input makes this process the "last input provider", which lifts
   the lock; the key itself means nothing to any program. Then repeat step 1.
   This happens at most once per call.

Every event Yuki's UI injects carries :data:`INJECT_MARK` in ``dwExtraInfo`` so the
chord hook (:mod:`yuki.ui.hotkey`) can recognise and ignore its own keystrokes.

Also here: :func:`set_no_activate`, which pins ``WS_EX_NOACTIVATE`` on or off, and
:func:`send_mask_key`, the "menu mask" keystroke the chord hook uses so that the
release of Alt after Alt+Shift is not a lone Alt tap.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

#: ``dwExtraInfo`` stamped on every event Yuki's UI injects ("YUKI" in ASCII).
INJECT_MARK = 0x594B5549

#: An unassigned virtual-key code (the same one AutoHotkey uses as its default
#: menu-mask key). No program binds it, so pressing it does nothing -- except
#: count as "a key was pressed", which is exactly what the two tricks need.
MASK_VK = 0xE8

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

_ULONG_PTR = ctypes.c_size_t
_LONG_PTR = ctypes.c_ssize_t


class MOUSEINPUT(ctypes.Structure):
    """Only here so :class:`INPUT` has the size ``SendInput`` checks for."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    """One synthetic keyboard event."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """Only here so :class:`INPUT` has the size ``SendInput`` checks for."""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """``SendInput`` payload."""

    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _bind() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """Private ``user32``/``kernel32`` handles with 64-bit-safe signatures.

    Separate ``WinDLL`` instances, so the argtypes set here never collide with
    whatever other modules set on ``ctypes.windll``.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = _LONG_PTR
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
    set_long.restype = _LONG_PTR
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return user32, kernel32


_user32, _kernel32 = _bind()
_get_window_long = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
_set_window_long = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)


# -- injected keys ----------------------------------------------------------


def _send(events: list[tuple[int, bool]]) -> int:
    """Inject keyboard events, each stamped with :data:`INJECT_MARK`.

    Args:
        events: ``(virtual key, is_key_up)`` pairs, in order.

    Returns:
        How many events Windows accepted (``SendInput``'s return value). Fewer
        than asked means UIPI blocked it -- the foreground app is elevated.
    """
    array = (INPUT * len(events))()
    for slot, (vk, up) in zip(array, events):
        slot.type = INPUT_KEYBOARD
        slot.u.ki = KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=INJECT_MARK
        )
    return int(_user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))


def send_mask_key() -> int:
    """Tap the unassigned :data:`MASK_VK` (down then up).

    Used by the chord hook while Alt (or Win) is held, so that the Alt release
    that ends Alt+Shift is not a lone Alt tap to the app underneath -- which
    would otherwise put that app's menu bar into keyboard mode and have it fight
    the overlay for focus. Called from inside the hook callback on purpose: input
    injected there reaches the system ahead of the event the hook is holding.

    Returns:
        Events accepted by ``SendInput`` (2 on success).
    """
    return _send([(MASK_VK, False), (MASK_VK, True)])


def send_unlock_key() -> int:
    """Inject a single key-up of :data:`MASK_VK` to lift the foreground lock.

    A key-up of a key that was never down, on a key nothing binds: no program
    reacts, no modifier state changes (unlike a synthetic Alt-up, which would
    un-hold a physically held Alt), but it makes this process the last input
    provider, which is what ``SetForegroundWindow`` checks.

    Returns:
        Events accepted by ``SendInput`` (1 on success).
    """
    return _send([(MASK_VK, True)])


# -- window style -----------------------------------------------------------


def set_no_activate(hwnd: int, enabled: bool) -> bool:
    """Make ``WS_EX_NOACTIVATE`` match ``enabled`` on a top-level window.

    Args:
        hwnd: Native window handle (``int(widget.winId())``).
        enabled: True for a window that must never take focus (the status strip),
            False for one that must be able to (the overlay).

    Returns:
        True if the style had to be changed.
    """
    style = int(_get_window_long(hwnd, GWL_EXSTYLE))
    wanted = (style | WS_EX_NOACTIVATE) if enabled else (style & ~WS_EX_NOACTIVATE)
    if wanted == style:
        return False
    _set_window_long(hwnd, GWL_EXSTYLE, wanted)
    _user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )
    return True


# -- foreground -------------------------------------------------------------


@dataclass
class FocusResult:
    """What :func:`force_foreground` did, for the ``focus_path`` log event.

    Attributes:
        ok: The window is the foreground window afterwards (Windows says so).
        path: Which step got it there: ``already`` (it was in front),
            ``set_foreground`` (no attach was needed -- the foreground thread was
            ours or there was none), ``attach_thread_input``, ``input_unlock``
            (only after the injected key-up), or ``failed``.
        hwnd: The window being activated.
        foreground_before: Foreground window when the call started.
        foreground_after: Foreground window when it ended.
        attached: ``AttachThreadInput`` succeeded on the first attempt.
        attach_error: Win32 error when it did not (5 = access denied: elevated
            foreground app), else 0.
        set_foreground_returned: ``SetForegroundWindow``'s return on each attempt.
        injected: Events ``SendInput`` accepted for the unlock key (0 = not tried
            or blocked).
        elapsed_ms: Time spent.
    """

    ok: bool
    path: str
    hwnd: int
    foreground_before: int
    foreground_after: int
    attached: bool
    attach_error: int
    set_foreground_returned: list[bool]
    injected: int
    elapsed_ms: float


def _foreground() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def _attempt(hwnd: int) -> tuple[bool, bool, int, bool]:
    """One pass of the attach / bring-to-top / set-foreground / focus sequence.

    Returns:
        ``(in_front, attached, attach_error, set_foreground_returned)``.
    """
    foreground = _foreground()
    foreground_thread = (
        int(_user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    )
    our_thread = int(_kernel32.GetCurrentThreadId())
    attached = False
    attach_error = 0
    if foreground_thread and foreground_thread != our_thread:
        attached = bool(_user32.AttachThreadInput(foreground_thread, our_thread, True))
        if not attached:
            attach_error = ctypes.get_last_error()
    try:
        _user32.BringWindowToTop(hwnd)
        returned = bool(_user32.SetForegroundWindow(hwnd))
        _user32.SetActiveWindow(hwnd)
        _user32.SetFocus(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(foreground_thread, our_thread, False)
    return _foreground() == hwnd, attached, attach_error, returned


def force_foreground(hwnd: int, *, allow_unlock: bool = True) -> FocusResult:
    """Bring one of this process's windows to the foreground with the keyboard.

    Must be called on the thread that owns ``hwnd`` (the Qt GUI thread), because
    ``SetActiveWindow``/``SetFocus`` only act on the caller's own windows.

    Args:
        hwnd: Native handle of the window (``int(widget.winId())``), already shown.
        allow_unlock: Permit the one injected key-up if the first attempt fails.

    Returns:
        A :class:`FocusResult`; ``ok`` is what Windows reports, not what the calls
        returned.
    """
    started = time.perf_counter()
    before = _foreground()
    if before == hwnd:
        return FocusResult(
            ok=True,
            path="already",
            hwnd=hwnd,
            foreground_before=before,
            foreground_after=before,
            attached=False,
            attach_error=0,
            set_foreground_returned=[],
            injected=0,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    ok, attached, attach_error, returned = _attempt(hwnd)
    returns = [returned]
    injected = 0
    if ok:
        path = "attach_thread_input" if attached else "set_foreground"
    elif allow_unlock:
        injected = send_unlock_key()
        ok, _, _, returned = _attempt(hwnd)
        returns.append(returned)
        path = "input_unlock" if ok else "failed"
    else:
        path = "failed"
    return FocusResult(
        ok=ok,
        path=path,
        hwnd=hwnd,
        foreground_before=before,
        foreground_after=_foreground(),
        attached=attached,
        attach_error=attach_error,
        set_foreground_returned=returns,
        injected=injected,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
