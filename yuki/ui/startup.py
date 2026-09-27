"""Start with Windows: the tray's toggle for the ``HKCU\\...\\Run`` value, and its one-time default.

Contract: ``docs/UI.md`` -> "Start with Windows".

The value (``Yuki`` under :data:`RUN_KEY`) runs :func:`launch_command`: the
venv's ``pythonw.exe -m yuki.ui.boot``. ``yuki-ui.exe`` itself is a console
launcher (PE subsystem 3), so starting it at logon would open a terminal
window whose closing kills Yuki; ``pythonw.exe`` is the same interpreter with
no console, and :mod:`yuki.ui.boot` sets the working directory to the project
before starting the tray. The tray starts the memory service as always
(``MemoryControl.ensure_service``), so logon brings both up.

Default on, once: the first run of a tray with this code turns it on and
writes :func:`default_flag_path` (``%LOCALAPPDATA%\\Yuki\\startup-default.json``);
after that only the user's toggle changes it, so turning it off stays off.

Everything here takes the key path as an argument so it can be exercised
against a scratch key (``HKCU\\Software\\YukiTest``) instead of the real Run key.
Only ``HKEY_CURRENT_USER`` is touched: no admin rights, nothing machine-wide.
"""

from __future__ import annotations

import json
import sys
import winreg
from datetime import datetime
from pathlib import Path
from typing import Any

#: The per-user Run key Windows reads at logon.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: The value name under it.
VALUE_NAME = "Yuki"
#: The module the Run value starts (windowless, working directory set).
BOOT_MODULE = "yuki.ui.boot"

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def windowless_python() -> Path:
    """``pythonw.exe`` of this environment (the venv's, when running from it); ``Yuki.exe`` in the
    installed app, also when asked from ``yuki-cli.exe`` (:func:`yuki.core.procs.interpreter`)."""
    from yuki.core.procs import interpreter

    return interpreter()


def launch_command(python: Path | None = None) -> str:
    """The command line the Run value holds: ``"<venv>\\pythonw.exe" -m yuki.ui.boot``."""
    return f'"{python or windowless_python()}" -m {BOOT_MODULE}'


def read_value(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> str | None:
    """The Run value's command line, or ``None`` when it is not set."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return str(value) if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else None


def is_enabled(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    """Whether Yuki starts with Windows (the value is set, whatever it points at)."""
    return read_value(key_path, name) is not None


def is_current(key_path: str = RUN_KEY, name: str = VALUE_NAME, command: str | None = None) -> bool:
    """Whether the value is set and points at this environment's launcher."""
    return read_value(key_path, name) == (command or launch_command())


def enable(key_path: str = RUN_KEY, name: str = VALUE_NAME, command: str | None = None) -> str:
    """Write the Run value (creating the key if needed); returns the command written. Raises ``OSError``."""
    command = command or launch_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    return command


def disable(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    """Remove the Run value; True if one was removed. Raises ``OSError`` on other failures."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return False


def set_enabled(on: bool, key_path: str = RUN_KEY, name: str = VALUE_NAME, *,
                flag: Path | None = None) -> bool:
    """The tray toggle: write or remove the value; records the choice in the flag file. Returns the new state."""
    if on:
        enable(key_path, name)
    else:
        disable(key_path, name)
    _write_flag(flag or default_flag_path(), {"by": "user", "enabled": bool(on)})
    return is_enabled(key_path, name)


def default_flag_path() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\startup-default.json``: written once the default has been applied."""
    from yuki.memory.store import default_memory_dir

    return default_memory_dir().parent / "startup-default.json"


def _write_flag(path: Path, fields: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[str, Any] = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                previous = {}
        record = {**previous, **fields, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        record.setdefault("default_applied_at", record["at"])
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    except OSError:
        pass


def apply_default(key_path: str = RUN_KEY, name: str = VALUE_NAME, *, flag: Path | None = None) -> dict[str, Any]:
    """Turn start-with-Windows on the first time this runs, and never again.

    Returns what happened (content-free, for the UI log): ``{"action":
    "enabled" | "already_on" | "respected" | "error", "enabled": bool}``.
    ``respected``: the default was applied before, so the user's current
    choice stands (off stays off). A value that points at an older
    environment of the same project is refreshed only while it is on.
    """
    flag = flag or default_flag_path()
    try:
        if flag.exists():
            on = is_enabled(key_path, name)
            if on and not is_current(key_path, name):
                enable(key_path, name)   # still on, but the venv moved: keep it on, pointing here
                return {"action": "refreshed", "enabled": True}
            return {"action": "respected", "enabled": on}
        if is_enabled(key_path, name) and is_current(key_path, name):
            _write_flag(flag, {"by": "default", "enabled": True})
            return {"action": "already_on", "enabled": True}
        enable(key_path, name)
        _write_flag(flag, {"by": "default", "enabled": True})
        return {"action": "enabled", "enabled": True}
    except OSError as exc:
        return {"action": "error", "enabled": is_enabled(key_path, name), "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "RUN_KEY", "VALUE_NAME", "launch_command", "read_value", "is_enabled", "is_current", "enable", "disable",
    "set_enabled", "apply_default", "default_flag_path", "windowless_python",
]
