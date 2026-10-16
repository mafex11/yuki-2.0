"""Global hotkeys.

Two mechanisms, because Windows only gives us one of them.

``RegisterHotKey`` covers every combo that ends in a real key (``"alt+space"``,
``"ctrl+alt+space"``, ``"win+shift+f12"``). It delivers ``WM_HOTKEY`` to the
*thread* that registered, so :class:`HotkeyThread` keeps its own message loop off
the GUI thread (Qt owns that queue) and hands each press over as a Qt signal.

``RegisterHotKey`` cannot express a chord of modifiers on their own -- there is no
virtual key to pass it -- so a modifier-only combo such as the default
``"alt+shift"`` goes to :class:`ChordHookThread` instead. That watches the keyboard
through a ``WH_KEYBOARD_LL`` hook and fires when the held modifiers were exactly
the chord and nothing else was typed. It never swallows a key; the one thing it
adds is a "menu mask" tap of an unassigned key while Alt (or Win) is held for a
chord, so that releasing the chord is not a lone Alt tap to the app underneath.

:class:`HotkeyListener` is what callers use: it sorts the bindings into whichever
mechanism can serve them and re-emits both as one ``pressed(action)`` signal.

Combos are written the way a user would type them. Key names are resolved through
the current keyboard layout where possible, so ``"ctrl+alt+/"`` works on layouts
where ``/`` is not where a US layout puts it.

The default stays Alt+Shift (x12Overlay, after live 10 Oct 21:09:45, where it brought
the ask bar up mid-run and the bar took the run's keyboard). The harm was the bar
taking the foreground, not the chord: pressed while a run holds the desk, the bar now
shows without the keyboard (:meth:`yuki.ui.overlay.Overlay.toggle`). The chord's
menu-mask tap already keeps Windows from switching the input language on a press of
it (:class:`ChordHookThread`), and the shortcut is the user's to change (Settings,
"Open Yuki anywhere").

:class:`KeyClaim` is the one hook here that does hold keys back, and only while the
ask bar shows without the keyboard: the user starts typing the moment the hotkey is
released, and those words are for Yuki, not for the run's window in front (a chat
box holding Yuki's draft, where Enter sends). Their first key that types is held
back and the bar takes the keyboard; the keys held back meanwhile are then typed
into it.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QThread, Signal

from yuki.core.inputtag import is_yuki_input
from yuki.ui.focus import INJECT_MARK, send_mask_key

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from yuki.config import Settings

#: ``fsModifiers`` flags for ``RegisterHotKey``. They double as the canonical id
#: of each modifier, so a chord is just a frozenset of them.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

#: ``SetWindowsHookExW`` arguments, and the key messages a low-level hook sees.
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
_KEY_DOWN = (WM_KEYDOWN, WM_SYSKEYDOWN)
_KEY_UP = (WM_KEYUP, WM_SYSKEYUP)
_KEY_HELD = 0x8000

#: Modifiers whose lone tap means something to Windows or to the app in front:
#: Alt puts the focused window's menu bar into keyboard mode, Win opens Start. A
#: chord containing one of them gets the menu-mask tap (see ChordHookThread).
_MASKED_MODS = frozenset({MOD_ALT, MOD_WIN})

#: Modifier spellings a user might reasonably type.
_MODIFIERS: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
    "cmd": MOD_WIN,
}

#: Every modifier virtual key back to its modifier, so a chord does not care which
#: side of the keyboard it was pressed on. The generic ``VK_SHIFT``/``VK_CONTROL``/
#: ``VK_MENU`` codes are here too: injected input (remappers, ``SendInput``) often
#: uses those rather than the left/right ones.
_VK_TO_MOD: dict[int, int] = {
    0x10: MOD_SHIFT,  # VK_SHIFT
    0xA0: MOD_SHIFT,  # VK_LSHIFT
    0xA1: MOD_SHIFT,  # VK_RSHIFT
    0x11: MOD_CONTROL,  # VK_CONTROL
    0xA2: MOD_CONTROL,  # VK_LCONTROL
    0xA3: MOD_CONTROL,  # VK_RCONTROL
    0x12: MOD_ALT,  # VK_MENU
    0xA4: MOD_ALT,  # VK_LMENU
    0xA5: MOD_ALT,  # VK_RMENU
    0x5B: MOD_WIN,  # VK_LWIN
    0x5C: MOD_WIN,  # VK_RWIN
}

#: Virtual-key codes for keys that have no printable character.
_VK_NAMES: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "printscreen": 0x2C,
    "pause": 0x13,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}

#: Environment variable that may override each action's combo. The defaults live
#: in :class:`yuki.config.Settings` (``ui_hotkey`` / ``ui_cancel_hotkey`` /
#: ``voice_hotkey``); these are the escape hatch for trying a different combo
#: without editing anything.
HOTKEY_ENV: dict[str, str] = {
    "toggle": "YUKI_HOTKEY",
    "cancel": "YUKI_CANCEL_HOTKEY",
    "voice": "YUKI_VOICE_HOTKEY",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """The ``lParam`` payload of a ``WH_KEYBOARD_LL`` callback."""

    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def _event_moment(event_ms: int) -> float:
    """The monotonic moment of a hook event from its own ``time`` field (``GetTickCount`` ms), not the moment the
    callback ran (second review V7: a busy process runs it late). Off by more than a few seconds: now."""
    now = time.monotonic()
    try:
        late = ((int(ctypes.windll.kernel32.GetTickCount()) - int(event_ms)) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return now
    return now - late if 0.0 <= late <= 5.0 else now


class HotkeyError(ValueError):
    """A combo string could not be understood."""


def parse_combo(combo: str) -> tuple[int, int]:
    """Turn ``"ctrl+alt+space"`` into ``(fsModifiers, virtual-key)``.

    Args:
        combo: Modifiers and exactly one key, joined by ``+``. Case-insensitive.

    Returns:
        The ``fsModifiers`` mask (with ``MOD_NOREPEAT``) and the virtual-key code.

    Raises:
        HotkeyError: Empty, modifier-only, or an unrecognised key name. A
            modifier-only combo is not a mistake; it just belongs to
            :func:`chord_modifiers` and :class:`ChordHookThread` instead.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"empty hotkey: {combo!r}")
    mods = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise HotkeyError(f"{combo!r} names more than one non-modifier key")
    if key is None:
        raise HotkeyError(f"{combo!r} has modifiers but no key")
    vk = _virtual_key(key)
    if vk is None:
        raise HotkeyError(f"unknown key {key!r} in {combo!r}")
    return mods | MOD_NOREPEAT, vk


def chord_modifiers(combo: str) -> frozenset[int] | None:
    """The modifiers of a modifier-only combo, or None if it names a real key.

    Args:
        combo: A combo string such as ``"alt+shift"``. Case-insensitive.

    Returns:
        The set of ``MOD_*`` ids for a combo made of nothing but modifiers, or
        None for anything else -- including an empty string, so that the caller
        sends it to :func:`parse_combo` and gets that function's error message.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods = set()
    for part in parts:
        if part not in _MODIFIERS:
            return None
        mods.add(_MODIFIERS[part])
    return frozenset(mods)


def split_bindings(bindings: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Sort bindings by the mechanism that can actually watch for them.

    Args:
        bindings: ``action -> combo``.

    Returns:
        ``(registerable, chords)``: the combos that name a key, for
        ``RegisterHotKey``, and the modifier-only ones, for the keyboard hook.
        Neither is validated here -- whichever thread owns a combo is the one
        that reports it through ``failed``.
    """
    registerable: dict[str, str] = {}
    chords: dict[str, str] = {}
    for action, combo in bindings.items():
        if chord_modifiers(combo) is None:
            registerable[action] = combo
        else:
            chords[action] = combo
    return registerable, chords


def _virtual_key(key: str) -> int | None:
    """Virtual-key code for one key name, or None if it is not a key."""
    if key in _VK_NAMES:
        return _VK_NAMES[key]
    if len(key) == 1:
        scan = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(key))
        if scan != -1:
            return scan & 0xFF
        if key.isalnum():
            return ord(key.upper())
    return None


def hotkey_bindings(
    settings: "Settings", env: dict[str, str] | None = None
) -> dict[str, str]:
    """The combo to register for every action.

    Configuration first: the combos are :class:`~yuki.config.Settings` fields, so
    a caller that wants different ones just passes different settings. The
    environment stays as an override for trying a combo out without touching
    anything -- unset or blank falls straight through to the setting.

    Args:
        settings: Where ``ui_hotkey`` and ``ui_cancel_hotkey`` come from.
        env: Mapping to read the overrides from; :data:`os.environ` by default.

    Returns:
        ``action -> combo`` for every action in :data:`HOTKEY_ENV`.
    """
    source = os.environ if env is None else env
    defaults = {
        "toggle": settings.ui_hotkey,
        "cancel": settings.ui_cancel_hotkey,
        "voice": getattr(settings, "voice_hotkey", ""),
    }
    return {
        action: ((source.get(HOTKEY_ENV[action]) or defaults[action]) or "").strip()
        for action in HOTKEY_ENV
    }


class _MessageLoopThread(QThread):
    """Shared plumbing for the two listeners: a thread id, and a way to quit.

    Both mechanisms need a Windows message loop on a thread that is not the GUI
    thread, and both are stopped the same way: by posting ``WM_QUIT`` to that
    loop. Subclasses fill in :meth:`run`.

    Args:
        bindings: ``action -> combo`` for this mechanism.
        parent: Qt parent.

    Signals:
        pressed: The action name whose combo was pressed.
        failed: ``(action, reason)`` when a combo cannot be watched for --
            usually because another program already owns it. The remaining
            combos still work.
    """

    pressed = Signal(str)
    failed = Signal(str, str)

    def __init__(self, bindings: dict[str, str], parent: object | None = None) -> None:
        super().__init__(parent)
        self.bindings = dict(bindings)
        self._thread_id: int | None = None

    def stop(self) -> None:
        """Ask the message loop to exit and wait for the thread to finish."""
        thread_id = self._thread_id
        if thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self.isRunning():
            self.wait(2000)


class HotkeyThread(_MessageLoopThread):
    """Owns the ``RegisterHotKey`` registrations and their message loop.

    Args:
        bindings: ``action -> combo``, e.g. ``{"cancel": "ctrl+alt+space"}``. Every
            combo must name exactly one non-modifier key; a modifier-only one
            belongs to :class:`ChordHookThread`.
        parent: Qt parent.
    """

    def run(self) -> None:  # noqa: D102 - QThread entry point
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        registered: dict[int, str] = {}
        for index, (action, combo) in enumerate(self.bindings.items(), start=1):
            try:
                mods, vk = parse_combo(combo)
            except HotkeyError as exc:
                self.failed.emit(action, str(exc))
                continue
            if user32.RegisterHotKey(None, index, mods, vk):
                registered[index] = action
            else:
                error = ctypes.get_last_error() or ctypes.GetLastError()
                self.failed.emit(action, f"{combo} is unavailable (win32 error {error})")

        if not registered:
            self._thread_id = None
            return

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or the queue broke
                    break
                if msg.message == WM_HOTKEY:
                    action = registered.get(int(msg.wParam))
                    if action is not None:
                        self.pressed.emit(action)
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._thread_id = None


class ChordHookThread(_MessageLoopThread):
    """Watches for modifier-only chords with a low-level keyboard hook.

    ``RegisterHotKey`` has no way to say "Alt and Shift and nothing else", so this
    is the other half: a ``WH_KEYBOARD_LL`` hook installed on this thread, with the
    message loop such a hook requires. Every event is handed straight to
    ``CallNextHookEx`` and its result returned untouched -- the hook reads the
    keyboard, it never eats a key from anybody.

    A chord fires when, and only when, all of these hold: the set of held
    modifiers was at some point exactly the chord (either order, left or right
    keys), no other key was pressed while it was held, and the last of the chord's
    keys has come back up. So Alt+Shift toggles the overlay as it is released,
    while Alt+Shift+Tab and Alt+Shift+P are left to whoever wanted them.

    Firing on release (not on the key-down that completes the chord) is what
    keeps that rule: at key-down time there is no telling whether a Tab is about
    to follow. The cost of firing on release is that the window underneath sees
    the Alt go up, and an Alt press and release with no key between them is the
    "activate the menu bar" gesture -- that window then sits in menu mode and
    competes with the overlay for the keyboard. So, like AutoHotkey's menu mask
    key, the hook taps an unassigned virtual key (:data:`~yuki.ui.focus.MASK_VK`,
    stamped :data:`~yuki.ui.focus.INJECT_MARK`) while Alt is still held, once per
    press, and only for a chord that contains Alt or Win and is armed and clean:

    - when the chord is completed by a non-Alt/Win key (Alt already down), at
      that key-down -- the tap lands after Alt-down whatever the injection order;
    - otherwise at the first chord key-up: if that is Shift (Alt still held),
      the tap again lands before Alt-up; if it is the Alt/Win key-up itself, the
      tap is sent before that key-up is passed on -- input injected from inside
      a low-level hook is delivered ahead of the event the hook is holding,
      which is the behaviour AutoHotkey's mask relies on.

    The user's keys are all passed on untouched; the tap is extra, and the hook
    ignores its own injected events (the ask bar's :class:`KeyClaim`, a separate hook
    that runs only while the bar shows without the keyboard, is the one that holds
    keys back). Side effect: Windows' own Alt+Shift
    input-language switch requires the chord with no key in between, so with the
    mask it no longer cycles the layout on the same press. That is deliberately
    not suppressed by eating keys (see ``docs/UI.md``); Win+Space still switches.

    Args:
        bindings: ``action -> combo``, e.g. ``{"toggle": "alt+shift"}``. Every combo
            must name two or more modifiers and no other key -- a single modifier
            would fire on every stray press of it, so it is refused.
        parent: Qt parent.
    """

    def __init__(self, bindings: dict[str, str], parent: object | None = None) -> None:
        super().__init__(bindings, parent)
        #: chord -> action; the only thing the hook callback looks anything up in.
        self._chords: dict[frozenset[int], str] = {}
        #: Modifier virtual keys currently down.
        self._held: set[int] = set()
        #: The chord that has been fully held, and the action it fires.
        self._armed: tuple[frozenset[int], str] | None = None
        #: Something other than a modifier was pressed: this press is not ours.
        self._tainted = False
        #: The menu mask has been tapped during this press (reset when all
        #: modifiers are up), so autorepeat does not tap it again.
        self._masked = False
        #: Set by :meth:`_observe` when the callback should tap the mask now.
        self._mask_now = False
        self._user32: ctypes.WinDLL | None = None

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            self._watch()
        finally:
            self._thread_id = None

    def _watch(self) -> None:
        """Install the hook and pump messages until asked to quit."""
        for action, combo in self.bindings.items():
            mods = chord_modifiers(combo)
            if mods is None or len(mods) < 2:
                self.failed.emit(action, f"{combo} is not a chord of two or more modifiers")
                continue
            owner = self._chords.get(mods)
            if owner is not None:
                self.failed.emit(action, f"{combo} is already bound to {owner}")
                continue
            self._chords[mods] = action
        if not self._chords:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hook_proc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32 = user32
        self._held.clear()
        self._armed = None
        self._tainted = False
        self._masked = False
        self._mask_now = False
        try:   # percFix: the takeover watch counts key downs from here (a fast tap included); one store per key down
            from yuki.actions.leases import note_key_down
        except Exception:  # noqa: BLE001
            note_key_down = None

        def on_key(n_code: int, w_param: int, l_param: int) -> int:
            action: str | None = None
            try:
                if n_code == HC_ACTION:
                    event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    # Our own injected keys (the mask tap, the focus unlock) are
                    # not the user's typing: they must not taint or end a chord.
                    if (event.dwExtraInfo or 0) != INJECT_MARK:
                        if note_key_down is not None and int(w_param) in _KEY_DOWN:
                            note_key_down(_event_moment(int(event.time)))
                        action = self._observe(int(event.vkCode), int(w_param))
                        if self._mask_now:
                            self._mask_now = False
                            send_mask_key()
            except Exception:  # noqa: BLE001 - a raising callback takes the hook down
                action = None
            result = user32.CallNextHookEx(None, n_code, w_param, l_param)
            if action is not None:
                # Queued across to the GUI thread, so this returns straight away.
                self.pressed.emit(action)
            return result

        # Windows keeps calling this for the life of the hook, so the trampoline has
        # to outlive this scope's temporaries: the name holds the only reference.
        callback = hook_proc(on_key)
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
        if not handle:
            error = ctypes.get_last_error()
            for action in self._chords.values():
                self.failed.emit(action, f"keyboard hook unavailable (win32 error {error})")
            return

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or the queue broke
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(handle)
            self._user32 = None

    def _observe(self, vk: int, message: int) -> str | None:
        """Fold one key event into the chord state, and say what it fires.

        Kept cheap on purpose: Windows drops a low-level hook that dawdles
        (``LowLevelHooksTimeout``, 300 ms by default), and every keystroke on the
        machine comes through here.

        Args:
            vk: Virtual-key code from the hook payload.
            message: The callback's ``wParam``, one of the ``WM_KEY*`` messages.

        Returns:
            The action to fire, or None -- which is the answer almost every time.
        """
        mod = _VK_TO_MOD.get(vk)
        if message in _KEY_DOWN:
            if mod is None:
                # Alt+Shift+Tab belongs to the window switcher, not to us.
                if self._held:
                    self._tainted = True
                return None
            self._held.add(vk)
            current = self._canonical()
            if current not in self._chords:
                self._resync(vk)
                current = self._canonical()
            action = self._chords.get(current)
            if action is not None:
                self._armed = (current, action)
                if (
                    not self._masked
                    and not self._tainted
                    and current & _MASKED_MODS
                    and mod not in _MASKED_MODS
                ):
                    # Alt/Win went down earlier, so a tap now sits between its
                    # down and its eventual up.
                    self._masked = True
                    self._mask_now = True
            return None

        if message not in _KEY_UP or mod is None:
            return None
        self._held.discard(vk)
        current = self._canonical()
        fired: str | None = None
        if self._armed is not None:
            chord, action = self._armed
            if (
                not self._masked
                and not self._tainted
                and mod in chord
                and (mod in _MASKED_MODS or current & chord & _MASKED_MODS)
            ):
                # A key of the armed chord is coming up with no tap yet. If Alt/Win
                # is still held, the tap lands before its up whatever the order;
                # if this *is* the Alt/Win up, the hook is still holding it, and
                # input injected here is delivered ahead of it.
                self._masked = True
                self._mask_now = True
            if not current & chord:  # the last of the chord's keys just came up
                if not self._tainted:
                    fired = action
                self._armed = None
        if not self._held:
            self._tainted = False
            self._masked = False
        return fired

    def _canonical(self) -> frozenset[int]:
        """The held modifiers, side-independent: ``{MOD_ALT, MOD_SHIFT}``."""
        return frozenset(_VK_TO_MOD[held] for held in self._held)

    def _resync(self, pressing: int) -> None:
        """Drop modifiers Windows says are not actually down.

        Key-ups go missing whenever the secure desktop takes over (Win+L, a UAC
        prompt), and a single phantom held modifier would stop every chord from
        matching for the rest of the session. Asking only when the chord did *not*
        match keeps this off the common path; the key being pressed right now is
        exempt, because its state is not committed until this hook returns.

        Args:
            pressing: Virtual key of the press being handled.
        """
        user32 = self._user32
        if user32 is None:
            return
        self._held = {
            held
            for held in self._held
            if held == pressing or user32.GetAsyncKeyState(held) & _KEY_HELD
        }


# -- the ask bar's claim on the user's keys (x12Overlay review r1 M3) ------------------------------------------------

#: How long the claim holds the user's keys back for the ask bar to take the keyboard (s). Past it they are dropped,
#: never sent on to the window they were not for, and every key passes again.
CLAIM_HOLD_S = 2.5
#: At most this many key events are held back (more are dropped).
CLAIM_KEEP = 128
VK_ESCAPE = 0x1B
#: Text sent as characters, not keys (a dictation tool's words): ``scanCode`` holds the character.
VK_PACKET = 0xE7
VK_LSHIFT, VK_LCONTROL = 0xA0, 0xA2
_LSHIFT_SCAN, _LCONTROL_SCAN = 0x2A, 0x1D
LLKHF_EXTENDED = 0x01
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_CHAR = 2
PM_NOREMOVE = 0x0000
#: Modifiers a key is a shortcut with (for the app in front or for Windows, not words for Yuki): Ctrl, Alt, Win.
_SHORTCUT_MODS = frozenset({MOD_CONTROL, MOD_ALT, MOD_WIN})
#: Modifiers that belong to the system while held (Alt+Tab, Win+D): never held back, even once the claim is made.
_SYSTEM_MODS = frozenset({MOD_ALT, MOD_WIN})

#: One key event held back: ``(vk, scan, hook flags, is_up, shift held, ctrl held)``.
HeldKey = tuple[int, int, int, bool, bool, bool]


class ClaimState:
    """The claim's state, kept apart from the hook so it can be read plainly: :meth:`on_key` for every key event the
    hook sees (on the hook's thread), :meth:`release` from the GUI.

    * ``armed``: the user's first key-down that types (``types(vk)``: it makes a character on the layout in front;
      Enter, Backspace, Space and Tab do; so does text a dictation tool sends) with no Ctrl, Alt or Win held is held
      back and the claim is made
      (``claimed``). Esc is held back too and closes the bar (``escaped``). A shortcut, a modifier, an arrow or a media
      key passes: those are for the app in front or for Windows;
    * ``claiming``: every key-down and its key-up is held back, with whether Shift and Ctrl were held, until the bar
      has the keyboard (:meth:`release`), or for :data:`CLAIM_HOLD_S` at most (then they are dropped);
    * ``escaped``: only Esc's own key-up is held back;
    * ``off``: everything passes.

    Yuki's own keys (its stamp, :func:`yuki.core.inputtag.is_yuki_input`) always pass and are never counted: the run
    keeps typing where it works until the user types to Yuki. The modifiers the user holds are followed from their
    own events (no key state is polled, so no other program's "pressed since" bit is used up), seeded once at start.
    """

    def __init__(self, *, types: Callable[[int], bool], now: Callable[[], float] = time.monotonic) -> None:
        self.mode = "armed"
        self.types, self.now = types, now
        self.dropped = 0
        self._held: list[HeldKey] = []
        self._downs: set[int] = set()
        self._mods: set[int] = set()
        self._since = 0.0
        self._lock = threading.Lock()

    def seed(self, held: Callable[[int], bool]) -> None:
        """The modifiers held as the claim starts (``held(vk)``: Windows' state of one key)."""
        with self._lock:
            self._mods = {_VK_TO_MOD[vk] for vk in _VK_TO_MOD if held(vk)}

    def on_key(self, vk: int, scan: int, flags: int, message: int, extra: int) -> tuple[bool, str | None]:
        """One key event: ``(hold it back, what it signals)``; the signal is ``claimed``, ``escaped`` or None."""
        if is_yuki_input(extra):
            return False, None
        mod = _VK_TO_MOD.get(vk)
        down = message in _KEY_DOWN
        if not down and message not in _KEY_UP:
            return False, None
        with self._lock:
            if mod is not None:
                if down:
                    self._mods.add(mod)
                else:
                    self._mods.discard(mod)
                return False, None
            if self.mode == "claiming" and self.now() - self._since > CLAIM_HOLD_S:
                self._expire()
            mode = self.mode
            if mode == "off":
                return False, None
            if not down:
                if vk not in self._downs:
                    return False, None   # a key that went down before the claim: its own window gets its key-up
                self._downs.discard(vk)
                if mode == "claiming":
                    self._keep((vk, scan, flags, True, False, False))
                return True, None
            if mode == "armed":
                if self._mods & _SHORTCUT_MODS:
                    return False, None
                if vk == VK_ESCAPE:
                    self.mode = "escaped"
                    self._downs.add(vk)
                    return True, "escaped"
                try:
                    typing = vk == VK_PACKET or bool(self.types(vk))
                except Exception:  # noqa: BLE001 - unknown: not taken for words
                    typing = False
                if not typing:
                    return False, None
                self.mode = "claiming"
                self._since = self.now()
                self._keep((vk, scan, flags, False, MOD_SHIFT in self._mods, False))
                self._downs.add(vk)
                return True, "claimed"
            if mode == "claiming":
                if self._mods & _SYSTEM_MODS:
                    return False, None
                self._keep((vk, scan, flags, False, MOD_SHIFT in self._mods, MOD_CONTROL in self._mods))
                self._downs.add(vk)
                return True, None
            return False, None

    def _keep(self, key: HeldKey) -> None:
        if len(self._held) < CLAIM_KEEP:
            self._held.append(key)
        else:
            self.dropped += 1

    def _expire(self) -> None:
        self.dropped += len(self._held)
        self._held.clear()
        self._downs.clear()
        self.mode = "off"

    def release(self) -> tuple[list[HeldKey], bool, bool]:
        """End the claim (everything passes from here): the keys held back, oldest first, and whether the user holds
        Shift and Ctrl now."""
        with self._lock:
            held, self._held = self._held, []
            self._downs.clear()
            self.mode = "off"
            return held, MOD_SHIFT in self._mods, MOD_CONTROL in self._mods


def replay_events(held: list[HeldKey], *, shift_now: bool, ctrl_now: bool) -> list[tuple[int, int, int]]:
    """``(vk, scan, dwFlags)`` records that type ``held`` again, in order, each key-down with the Shift and Ctrl it
    had (pressed or let go around it), and the modifiers as the user holds them now at the end."""
    out: list[tuple[int, int, int]] = []
    shift, ctrl = shift_now, ctrl_now
    for vk, scan, flags, up, want_shift, want_ctrl in held:
        if vk == VK_PACKET:   # a character as it came: no modifiers apply
            out.append((0, int(scan), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)))
            continue
        if not up:
            if want_shift != shift:
                out.append((VK_LSHIFT, _LSHIFT_SCAN, 0 if want_shift else KEYEVENTF_KEYUP))
                shift = want_shift
            if want_ctrl != ctrl:
                out.append((VK_LCONTROL, _LCONTROL_SCAN, 0 if want_ctrl else KEYEVENTF_KEYUP))
                ctrl = want_ctrl
        out.append((int(vk), int(scan),
                    (KEYEVENTF_EXTENDEDKEY if flags & LLKHF_EXTENDED else 0) | (KEYEVENTF_KEYUP if up else 0)))
    if shift != shift_now:
        out.append((VK_LSHIFT, _LSHIFT_SCAN, 0 if shift_now else KEYEVENTF_KEYUP))
    if ctrl != ctrl_now:
        out.append((VK_LCONTROL, _LCONTROL_SCAN, 0 if ctrl_now else KEYEVENTF_KEYUP))
    return out


_CLAIM_U32: ctypes.WinDLL | None = None


def _claim_user32() -> ctypes.WinDLL:
    """A private ``user32`` with the prototypes the claim uses (the shared ``windll.user32`` is not ours to set)."""
    global _CLAIM_U32
    if _CLAIM_U32 is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        u32.GetKeyboardLayout.restype = ctypes.c_void_p
        u32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        u32.MapVirtualKeyExW.restype = wintypes.UINT
        u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u32.GetAsyncKeyState.restype = ctypes.c_short
        _CLAIM_U32 = u32
    return _CLAIM_U32


def key_types_text(vk: int) -> bool:
    """Whether ``vk`` makes a character on the keyboard layout of the window in front (letters, digits, punctuation,
    Space, Enter, Backspace, Tab); arrows, F-keys and media keys do not. One table lookup, no state changed."""
    u32 = _claim_user32()
    hwnd = u32.GetForegroundWindow()
    thread = int(u32.GetWindowThreadProcessId(hwnd, None)) if hwnd else 0
    return bool(int(u32.MapVirtualKeyExW(int(vk), MAPVK_VK_TO_CHAR, u32.GetKeyboardLayout(thread))) & 0x7FFFFFFF)


def key_held(vk: int) -> bool:
    """Windows' state of one key now (the "held" bit only)."""
    return bool(int(_claim_user32().GetAsyncKeyState(int(vk))) & 0x8000)


def send_keys(records: list[tuple[int, int, int]]) -> int:
    """Inject ``(vk, scan, dwFlags)`` keyboard records, each stamped :data:`~yuki.ui.focus.INJECT_MARK` (so no hook of
    Yuki's takes them for the user's); how many Windows accepted."""
    if not records:
        return 0
    from yuki.ui.focus import INPUT, INPUT_KEYBOARD, KEYBDINPUT

    array = (INPUT * len(records))()
    for slot, (vk, scan, flags) in zip(array, records):
        slot.type = INPUT_KEYBOARD
        slot.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=INJECT_MARK)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u32.SendInput.restype = wintypes.UINT
    return int(u32.SendInput(len(records), array, ctypes.sizeof(INPUT)))


class KeyClaim(QThread):
    """The ask bar's claim on the user's keys while it shows without the keyboard (see :class:`ClaimState`): a
    ``WH_KEYBOARD_LL`` hook on its own thread, installed by :meth:`start` and removed by :meth:`stop`, so it exists
    only for those few seconds. Unlike :class:`ChordHookThread` it holds keys back -- the user's, never Yuki's own.

    A hook Windows gives up on (the process too busy to answer in time) lets every key through: the claim fails open,
    to the window in front, never closed.

    Signals:
        claimed: The user's first key that types was held back: the bar takes the keyboard, then :meth:`release`.
        escaped: The user pressed Esc: the bar closes.
        failed: ``(reason)`` the hook could not be installed (the bar still takes a click).
    """

    claimed = Signal()
    escaped = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, *, state: ClaimState | None = None,
                 send: Callable[[list[tuple[int, int, int]]], int] = send_keys) -> None:
        super().__init__(parent)
        self.state = state if state is not None else ClaimState(types=key_types_text)
        self._send = send
        self._thread_id: int | None = None
        self._stop_asked = False

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self._watch()
        finally:
            self._thread_id = None

    def _watch(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        msg = wintypes.MSG()
        # this thread's message queue exists from here, so a WM_QUIT posted by stop() is never lost
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        try:
            self.state.seed(key_held)
        except Exception:  # noqa: BLE001 - no modifiers known held
            pass
        state = self.state

        def on_key(n_code: int, w_param: int, l_param: int) -> int:
            hold, signal = False, None
            try:
                if n_code == HC_ACTION:
                    event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    hold, signal = state.on_key(int(event.vkCode), int(event.scanCode), int(event.flags),
                                                int(w_param), int(event.dwExtraInfo or 0))
            except Exception:  # noqa: BLE001 - a raising callback takes the hook down: pass the key on
                hold, signal = False, None
            if signal == "claimed":
                self.claimed.emit()   # queued across to the GUI thread
            elif signal == "escaped":
                self.escaped.emit()
            if hold:
                return 1
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        callback = hook_proc(on_key)   # the name holds the trampoline's only reference for the hook's life
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
        if not handle:
            self.failed.emit(f"keyboard hook unavailable (win32 error {ctypes.get_last_error()})")
            return
        try:
            if self._stop_asked:
                return
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or the queue broke
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(handle)

    def stop(self) -> None:
        """Remove the hook and end the thread (also when called before the thread has started its loop)."""
        self._stop_asked = True
        deadline = time.monotonic() + 2.0
        while self.isRunning() and time.monotonic() < deadline:
            thread_id = self._thread_id
            if thread_id:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            if self.wait(25):
                break

    def release(self, *, replay: bool) -> int:
        """End the claim: with ``replay`` (the ask bar has the keyboard now) the keys held back are typed into it,
        with the Shift and Ctrl they had; without, they are dropped. How many key records were sent."""
        held, shift_now, ctrl_now = self.state.release()
        if not replay:
            self.state.dropped += len(held)
            return 0
        if not held:
            return 0
        try:
            return self._send(replay_events(held, shift_now=shift_now, ctrl_now=ctrl_now))
        except Exception:  # noqa: BLE001 - not on Windows
            return 0


class HotkeyListener(QObject):
    """One ``pressed`` signal, however many mechanisms the bindings need.

    Args:
        bindings: ``action -> combo``, as :func:`hotkey_bindings` returns it.
        parent: Qt parent.

    Signals:
        pressed: The action name whose combo was pressed.
        failed: ``(action, reason)`` for a combo that could not be watched for;
            the others keep working.
    """

    pressed = Signal(str)
    failed = Signal(str, str)

    def __init__(self, bindings: dict[str, str], parent: object | None = None) -> None:
        super().__init__(parent)
        self.bindings = dict(bindings)
        registerable, chords = split_bindings(self.bindings)
        self.threads: list[_MessageLoopThread] = []
        if registerable:
            self.threads.append(HotkeyThread(registerable, self))
        if chords:
            self.threads.append(ChordHookThread(chords, self))
        for thread in self.threads:
            thread.pressed.connect(self.pressed)
            thread.failed.connect(self.failed)

    def start(self) -> None:
        """Start listening on every mechanism the bindings asked for."""
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        """Stop every listener and wait for its thread."""
        for thread in self.threads:
            thread.stop()
