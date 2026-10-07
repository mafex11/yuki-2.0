"""Key classification for the action trace (report 78 §4.2), done in the Raw Input handler before anything else.

Each key press becomes a *class* at once, and only the class leaves the handler:

* a **modifier** (Shift, Ctrl, Alt, Win): only the held set is kept;
* a **named key** (Enter, Esc, Tab, Backspace, Delete, Insert, Home/End, PageUp/PageDown, the arrows, F1-F24, the
  menu, media and browser keys), with Shift if held: ``"Shift+Tab"``;
* a **chord**: a key while Ctrl, Alt or Win is held, named from Windows' virtual-key code (``"Ctrl+S"``,
  ``"Ctrl+Shift+T"``, ``"Win+E"``, ``"Alt+F4"``);
* **a character**: anything else, Shift or not. Only a count leaves the handler (decision D7: counts + shortcuts; D8:
  never typed text). Which character it was is never known here: the virtual key is dropped with the class.

Two cases where a held modifier still means typing:

* **AltGr** (right Alt, which Windows reports as Ctrl+Alt): the combination counts as typing when the keyboard layout
  turns it into a character or a dead key. Asked with ``ToUnicodeEx(..., wFlags=0x4)``, which leaves the keyboard's
  dead-key state alone, so accents keep working for the user while the trace is on. Only the *fact* that it made a
  character is kept, never the character.
* **Alt + numpad digits** (Alt+0169): typing, never a chord. The digits themselves count nothing; the character
  they compose counts once when Alt is let go.

Everything here is Windows' fixed key vocabulary (virtual-key codes, scan codes, Raw Input flags); no word lists.
Stdlib only.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass

# Raw Input keyboard flags (RAWKEYBOARD.Flags) and messages.
RI_KEY_BREAK = 0x01
RI_KEY_E0 = 0x02
RI_KEY_E1 = 0x04
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
_FAKE = 0xFF                    # part of an escape sequence (Pause, fake shifts): not a key of its own

#: Keys that only change state: never an action of their own.
_LOCKS = frozenset({0x14, 0x90, 0x91})          # Caps Lock, Num Lock, Scroll Lock

#: Named keys (Windows' virtual-key codes), with the names a chord or a named-key row carries.
NAMED: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0C: "Clear", 0x0D: "Enter", 0x13: "Pause", 0x1B: "Esc",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete", 0x5D: "Menu", 0x5F: "Sleep",
    0xA6: "BrowserBack", 0xA7: "BrowserForward", 0xA8: "BrowserRefresh", 0xA9: "BrowserStop",
    0xAA: "BrowserSearch", 0xAB: "BrowserFavorites", 0xAC: "BrowserHome",
    0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp",
    0xB0: "MediaNext", 0xB1: "MediaPrevious", 0xB2: "MediaStop", 0xB3: "MediaPlayPause",
    0xB4: "LaunchMail", 0xB5: "LaunchMedia", 0xB6: "LaunchApp1", 0xB7: "LaunchApp2",
}
NAMED.update({0x70 + i: f"F{i + 1}" for i in range(24)})
#: Named keys whose repeats fold into one row (a run): arrows, paging, Backspace and Delete.
RUN_KEYS = frozenset({"Left", "Right", "Up", "Down", "PageUp", "PageDown", "Backspace", "Delete"})
#: Repeats of the same run key or chord within this long fold into one row (s); wheel notches likewise. Shared by
#: the worker (one row) and the outcome hooks (one outcome window, one event cap: second verify V6).
FOLD_S = 1.0

#: Names for a chord's main key that is neither a letter, a digit nor a named key (the US-layout names of the OEM
#: virtual keys, a machine identity only).
_OEM = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
        0xDD: "]", 0xDE: "'", 0xE2: "<", 0x20: "Space", 0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.",
        0x6F: "Num/"}

#: Numpad keys as Alt+numpad sees them: the digit keys with Num Lock on, and with it off the same physical keys as
#: navigation keys *without* the E0 prefix (the separate navigation block has it).
_NUMPAD_DIGITS = frozenset(range(0x60, 0x6A))
_NUMPAD_NAV = frozenset({0x2D, 0x23, 0x28, 0x22, 0x25, 0x0C, 0x27, 0x24, 0x26, 0x21})

# Classes.
MODIFIER, NAMED_KEY, CHORD, CHAR, NONE = "modifier", "named", "chord", "char", "none"


@dataclass(frozen=True)
class KeyClass:
    """What one key event was, content-free: ``kind`` (named | chord | char | modifier | none), the key text for
    named keys and chords (``"Ctrl+S"``, ``"Shift+Tab"``), and ``chars``: how many characters it typed (Alt+numpad
    counts its one character when Alt is let go)."""

    kind: str
    key: str = ""
    chars: int = 0


_NOTHING = KeyClass(NONE)
_MOD = KeyClass(MODIFIER)


def key_name(vk: int) -> str:
    """The main key's name in a chord: ``"S"``, ``"5"``, ``"F4"``, ``"Tab"``, ``"/"``; ``"VK_xx"`` otherwise."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    if vk in NAMED:
        return NAMED[vk]
    if vk in _NUMPAD_DIGITS:
        return f"Num{vk - 0x60}"
    if vk in _OEM:
        return _OEM[vk]
    return f"VK_{vk:02X}"


class KeyClassifier:
    """Turns Raw Input key events into :class:`KeyClass` values, keeping only which modifiers are held.

    Args:
        altgr_types: ``(vk, scan, shift) -> bool``: whether Ctrl+Alt+``vk`` makes a character or a dead key on the
            keyboard layout in front (default :func:`altgr_types`, ``ToUnicodeEx`` with ``wFlags=0x4``).
        still_held: ``(vk) -> bool``: whether a modifier is really still down (default ``GetAsyncKeyState``), asked
            only for a modifier believed held, so one whose release went to another desktop (the lock screen) does
            not stick.
    """

    def __init__(self, *, altgr_types: Callable[[int, int, bool], bool] | None = None,
                 still_held: Callable[[int], bool] | None = None) -> None:
        self._altgr = altgr_types if altgr_types is not None else altgr_types_default
        self._still = still_held if still_held is not None else _async_held
        self.ctrl = self.alt = self.shift = self.win = False
        self._numpad = 0            # Alt+numpad digits entered since Alt went down

    # -- state ------------------------------------------------------------------------------------------------------

    def held(self) -> tuple[bool, bool, bool, bool]:
        return self.ctrl, self.alt, self.shift, self.win

    def _resync(self) -> None:
        """Drop a modifier whose release was never seen (it went to the secure desktop)."""
        try:
            if self.ctrl and not self._still(VK_CONTROL):
                self.ctrl = False
            if self.alt and not self._still(VK_MENU):
                self.alt = False
                self._numpad = 0
            if self.shift and not self._still(VK_SHIFT):
                self.shift = False
            if self.win and not (self._still(VK_LWIN) or self._still(VK_RWIN)):
                self.win = False
        except Exception:  # noqa: BLE001 - keep what we believe
            pass

    def _modifier(self, vk: int, down: bool, flags: int) -> KeyClass | None:
        if vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
            if flags & RI_KEY_E0:        # a fake shift Windows inserts around the navigation keys: not the user's
                return _MOD
            self.shift = down
            return _MOD
        if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
            self.ctrl = down
            return _MOD
        if vk in (VK_MENU, VK_LMENU, VK_RMENU):
            if down:
                if not self.alt:
                    self._numpad = 0
                self.alt = True
                return _MOD
            self.alt = False
            typed, self._numpad = self._numpad, 0
            return KeyClass(CHAR, chars=1) if typed else _MOD    # Alt+0169: one character, counted at Alt's release
        if vk in (VK_LWIN, VK_RWIN):
            self.win = down
            return _MOD
        return None

    # -- the one entry point --------------------------------------------------------------------------------------

    def classify(self, vk: int, scan: int, flags: int, message: int) -> KeyClass:
        """One Raw Input keyboard event (``VKey``, ``MakeCode``, ``Flags``, ``Message``) as its class.

        Key-ups only change the held set (and finish an Alt+numpad character); autorepeated downs count again (a
        held key types again)."""
        vk = int(vk) & 0xFFFF
        if vk == _FAKE or vk == 0:
            return _NOTHING
        down = int(message) in (WM_KEYDOWN, WM_SYSKEYDOWN) and not (int(flags) & RI_KEY_BREAK)
        mod = self._modifier(vk, down, int(flags))
        if mod is not None:
            return mod
        if not down or vk in _LOCKS:
            return _NOTHING
        if self.ctrl or self.alt or self.win:
            self._resync()
        ctrl, alt, shift, win = self.ctrl, self.alt, self.shift, self.win
        if alt and not ctrl and not win and (vk in _NUMPAD_DIGITS or (vk in _NUMPAD_NAV and not flags & RI_KEY_E0)):
            self._numpad += 1           # Alt+numpad entry: typing, never a chord; counted when Alt goes up
            return _NOTHING
        if ctrl and alt and not win and vk not in NAMED:
            try:
                altgr = bool(self._altgr(vk, int(scan), shift))
            except Exception:  # noqa: BLE001 - unknown: read it as a chord (no character is ever kept either way)
                altgr = False
            if altgr:
                return KeyClass(CHAR, chars=1)
        if ctrl or alt or win:
            parts = [p for p, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift), ("Win", win)) if on]
            return KeyClass(CHORD, "+".join([*parts, key_name(vk)]))
        if vk in NAMED:
            return KeyClass(NAMED_KEY, ("Shift+" if shift else "") + NAMED[vk])
        return KeyClass(CHAR, chars=1)


# -- Windows ----------------------------------------------------------------------------------------------------------

_DONT_CHANGE_STATE = 0x4        # ToUnicodeEx wFlags: leave the keyboard's dead-key state as it is (Windows 10 1607+)
_user32 = None


def _u32():
    global _user32
    if _user32 is None:
        from ctypes import wintypes as W

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.ToUnicodeEx.argtypes = [W.UINT, W.UINT, ctypes.POINTER(ctypes.c_ubyte), W.LPWSTR, ctypes.c_int, W.UINT,
                                  W.HKL]
        u.ToUnicodeEx.restype = ctypes.c_int
        u.GetForegroundWindow.restype = W.HWND
        u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        u.GetWindowThreadProcessId.restype = W.DWORD
        u.GetKeyboardLayout.argtypes = [W.DWORD]
        u.GetKeyboardLayout.restype = W.HKL
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        _user32 = u
    return _user32


def _async_held(vk: int) -> bool:
    return bool(_u32().GetAsyncKeyState(int(vk)) & 0x8000)


def altgr_types_default(vk: int, scan: int, shift: bool) -> bool:
    """Whether Ctrl+Alt (+Shift) + ``vk`` makes a character or a dead key on the keyboard layout of the window in
    front. ``ToUnicodeEx`` with ``wFlags=0x4`` leaves the dead-key state alone. The buffer is cleared before
    returning; only the yes/no leaves."""
    u = _u32()
    thread = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    layout = u.GetKeyboardLayout(thread)
    state = (ctypes.c_ubyte * 256)()
    for key in (VK_CONTROL, VK_LCONTROL, VK_MENU, VK_RMENU):
        state[key] = 0x80
    if shift:
        state[VK_SHIFT] = state[VK_LSHIFT] = 0x80
    buf = ctypes.create_unicode_buffer(8)
    try:
        n = u.ToUnicodeEx(int(vk), int(scan), state, buf, 8, _DONT_CHANGE_STATE, layout)
    finally:
        ctypes.memset(buf, 0, ctypes.sizeof(buf))
    return n != 0           # >0: characters, <0: a dead key (it will compose one)


__all__ = ["CHAR", "CHORD", "FOLD_S", "KeyClass", "KeyClassifier", "MODIFIER", "NAMED", "NAMED_KEY", "NONE", "RUN_KEYS",
           "altgr_types_default", "key_name"]
