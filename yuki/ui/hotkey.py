"""Global hotkeys.

Windows delivers system-wide hotkeys as ``WM_HOTKEY`` messages to the *thread*
that called ``RegisterHotKey``, so that thread needs its own message loop and must
not be the GUI thread (Qt owns that queue). :class:`HotkeyThread` registers the
combos, blocks in ``GetMessageW``, and hands each press to the GUI thread as a Qt
signal -- the only channel between the two.

Combos are written the way a user would type them: ``"alt+space"``,
``"ctrl+alt+space"``, ``"win+shift+f12"``. Key names are resolved through the
current keyboard layout where possible, so ``"ctrl+alt+/"`` works on layouts where
``/`` is not where a US layout puts it.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QThread, Signal

#: ``fsModifiers`` flags for ``RegisterHotKey``.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

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

#: Environment variables holding the combo for each action, and their defaults.
HOTKEY_ENV: dict[str, tuple[str, str]] = {
    "toggle": ("YUKI_HOTKEY", "alt+space"),
    "cancel": ("YUKI_CANCEL_HOTKEY", "ctrl+alt+space"),
}


class HotkeyError(ValueError):
    """A combo string could not be understood."""


def parse_combo(combo: str) -> tuple[int, int]:
    """Turn ``"ctrl+alt+space"`` into ``(fsModifiers, virtual-key)``.

    Args:
        combo: Modifiers and exactly one key, joined by ``+``. Case-insensitive.

    Returns:
        The ``fsModifiers`` mask (with ``MOD_NOREPEAT``) and the virtual-key code.

    Raises:
        HotkeyError: Empty, modifier-only, or an unrecognised key name.
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


def hotkeys_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Read the configured combo for every action from the environment.

    Yuki's :class:`~yuki.config.Settings` has no UI fields yet, so the overlay
    reads ``YUKI_HOTKEY`` / ``YUKI_CANCEL_HOTKEY`` instead of inventing a config
    field it cannot add. Empty or unset falls back to the default.

    Args:
        env: Mapping to read; :data:`os.environ` by default.

    Returns:
        ``action -> combo`` for every action in :data:`HOTKEY_ENV`.
    """
    source = os.environ if env is None else env
    return {
        action: (source.get(var) or default).strip()
        for action, (var, default) in HOTKEY_ENV.items()
    }


class HotkeyThread(QThread):
    """Owns the ``RegisterHotKey`` registrations and their message loop.

    Args:
        bindings: ``action -> combo``, e.g. ``{"toggle": "alt+space"}``.
        parent: Qt parent.

    Signals:
        pressed: The action name whose combo was pressed.
        failed: ``(action, reason)`` when a combo could not be registered --
            usually because another program already owns it. The remaining
            combos still work.
    """

    pressed = Signal(str)
    failed = Signal(str, str)

    def __init__(self, bindings: dict[str, str], parent: object | None = None) -> None:
        super().__init__(parent)
        self.bindings = dict(bindings)
        self._thread_id: int | None = None

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

    def stop(self) -> None:
        """Ask the message loop to exit and wait for the thread to finish."""
        thread_id = self._thread_id
        if thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self.isRunning():
            self.wait(2000)
