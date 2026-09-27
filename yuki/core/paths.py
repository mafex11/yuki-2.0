"""Where Yuki's code, logs and data live, in a dev checkout and in the frozen (installed) app.

Dev (``pythonw -m ...`` from the uv venv): the code is the project folder, logs go to
``<project>/logs`` as they always have.

Frozen (PyInstaller onedir, installed per-user by the Inno Setup installer): the code is the
install folder (``%LOCALAPPDATA%\\Programs\\Yuki Assistant`` by default, read-only in spirit:
an upgrade replaces it), so logs go next to the user's data instead:
``%LOCALAPPDATA%\\Yuki\\logs``. The data folder (``%LOCALAPPDATA%\\Yuki``: memory, voice,
settings) is the same in both, so the installed app and a dev checkout share one memory.

Import-light on purpose (stdlib only): the tray, the service and the frozen entry import it
before anything else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The project folder of a dev checkout (``yuki/core/paths.py`` -> two levels up).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """True inside the PyInstaller build (``sys.frozen`` is set by its bootloader)."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder holding the code: the install folder when frozen, the project otherwise."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def data_dir() -> Path:
    """``%LOCALAPPDATA%\\Yuki``: the user's data (memory, voice settings, prefs), dev and frozen alike."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki"


def state_root() -> Path:
    """The base that ``logs/`` is resolved against (``Settings.project_root``).

    The project folder in a dev checkout (logs stay where every doc says they are), the data
    folder when frozen (the install folder is replaced on upgrade and should not collect logs).
    """
    return data_dir() if is_frozen() else PROJECT_ROOT


def logs_dir() -> Path:
    """``<state_root>/logs``."""
    return state_root() / "logs"


__all__ = ["PROJECT_ROOT", "is_frozen", "app_dir", "data_dir", "state_root", "logs_dir"]
