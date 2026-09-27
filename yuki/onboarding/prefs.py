"""Yuki's small app preferences: ``%LOCALAPPDATA%\\Yuki\\settings.json``.

Tiny and dependency-free on purpose (the lead may move it to ``yuki/core``).
Holds choices, never secrets: keys live only in user environment variables.
Writes merge into what is there, so other keys in the file are kept.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SETTINGS_FILENAME = "settings.json"
#: The model backends Yuki can use: a pasted Anthropic API key, or AWS Bedrock.
BACKENDS = ("anthropic", "bedrock")


def yuki_local_dir() -> Path:
    """``%LOCALAPPDATA%\\Yuki``."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki"


def settings_path(local_dir: str | Path | None = None) -> Path:
    """The settings file: ``<local_dir or %LOCALAPPDATA%\\Yuki>\\settings.json``."""
    return (Path(local_dir) if local_dir else yuki_local_dir()) / SETTINGS_FILENAME


def load_prefs(local_dir: str | Path | None = None) -> dict:
    """The settings file as a dict; ``{}`` when missing or unreadable."""
    try:
        data = json.loads(settings_path(local_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path, data: dict) -> Path:
    """Write ``data`` as JSON to ``path`` via a temp file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def save_prefs(updates: dict, local_dir: str | Path | None = None) -> Path:
    """Merge ``updates`` into the settings file (other keys kept). An unreadable file is kept as ``.bak``."""
    path = settings_path(local_dir)
    current = load_prefs(local_dir)
    if not current and path.exists():
        try:
            if path.read_text(encoding="utf-8").strip():
                os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    current.update(updates)
    return write_json_atomic(path, current)


def save_backend_choice(backend: str, local_dir: str | Path | None = None) -> Path:
    """Record ``{"model_backend": "anthropic" | "bedrock"}`` in the settings file."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown model backend {backend!r}; expected one of {BACKENDS}")
    return save_prefs({"model_backend": backend}, local_dir)
