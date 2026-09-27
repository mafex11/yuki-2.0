"""Save keys as Windows *user* environment variables (``HKCU\\Environment``), nowhere else.

A value is written as ``REG_SZ`` under the registry key, set in the current
process's environment, and after the last write Windows is told once
(``WM_SETTINGCHANGE`` "Environment") so processes started from now on see it.
Nothing here prints, logs or stores a value anywhere else.

The registry key is a parameter so tests write to a scratch key
(``Software\\YukiTest\\Environment``) and never touch the real one; the
broadcast only happens for the real key.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import MutableMapping
from ctypes import wintypes

#: The real user-environment key under HKEY_CURRENT_USER.
ENV_KEY = "Environment"

_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002


def broadcast_environment_change(timeout_ms: int = 3000) -> bool:
    """Tell running programs (Explorer first of all) that user environment variables changed."""
    try:
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_size_t
        result = ctypes.c_size_t(0)
        return bool(send(_HWND_BROADCAST, _WM_SETTINGCHANGE, 0, "Environment",
                         _SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)))
    except Exception:  # noqa: BLE001 - best effort; the value is saved either way
        return False


class UserEnv:
    """Writes user environment variables under ``HKCU\\<key_path>`` and into ``environ``."""

    def __init__(self, key_path: str = ENV_KEY, environ: MutableMapping[str, str] | None = None,
                 broadcast: bool | None = None) -> None:
        self.key_path = key_path
        self.environ = os.environ if environ is None else environ
        #: Broadcast only for the real key unless told otherwise.
        self.broadcast = (key_path == ENV_KEY) if broadcast is None else broadcast
        self.written: list[str] = []   # names only

    def set(self, name: str, value: str) -> None:
        """Save ``name`` = ``value`` (stripped) as REG_SZ and in ``environ``. Raises ``OSError``."""
        import winreg

        value = (value or "").strip()
        if not value:
            raise ValueError(f"empty value for {name}")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        self.environ[name] = value
        if name not in self.written:
            self.written.append(name)

    def get(self, name: str) -> str | None:
        """The value saved under the key (not ``environ``); ``None`` when unset."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return value if isinstance(value, str) and value.strip() else None

    def commit(self) -> bool:
        """Broadcast the change once if anything was written (real key only). True if broadcast."""
        if not self.written or not self.broadcast:
            return False
        return broadcast_environment_change()
