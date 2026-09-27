"""API keys for the voice vendors, read from the environment only (never from a file).

The keys are Windows *user* environment variables. A process started before a
key was set (the tray, started at logon) does not have it in ``os.environ``,
so a missing variable is looked up once more in the registry, where Windows
keeps user variables (``HKCU\\Environment``). Nothing here prints, logs or
stores a key: callers get the value, and the logs get only :func:`key_source`
(``env`` / ``registry`` / ``missing``).
"""

from __future__ import annotations

import os

DEEPGRAM = "DEEPGRAM_API_KEY"
ELEVENLABS = "ELEVENLABS_API_KEY"
GOOGLE = "GOOGLE_API_KEY"


def _from_registry(name: str) -> str | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    try:
        import winreg

        if kind == winreg.REG_EXPAND_SZ:
            value = os.path.expandvars(value)
    except Exception:  # noqa: BLE001 - the plain value is still usable
        pass
    return value.strip() or None


def get_key(name: str) -> str | None:
    """The key's value: ``os.environ`` first, then ``HKCU\\Environment``; ``None`` when unset."""
    value = (os.environ.get(name) or "").strip()
    return value or _from_registry(name)


def key_source(name: str) -> str:
    """Where :func:`get_key` would find ``name``: ``env``, ``registry`` or ``missing``. Content-free."""
    if (os.environ.get(name) or "").strip():
        return "env"
    return "registry" if _from_registry(name) else "missing"
