"""Where the window's settings are saved: the privacy file, voice.json and the Run key.

* ``privacy.toml`` (``%LOCALAPPDATA%\\Yuki\\memory``, the memory service re-reads it on change):
  the user's blocked apps and sites (``[user]``) and the coach (``[nudges]``:
  ``enabled``, ``quiet_start``, ``quiet_end``, ``questions_enabled``). The file is
  the user's, with comments: only the one key changes, in place; a missing key
  or table is added at the end of its table / the file. TOML is parsed back
  after every write and the old file is kept if the result would not parse.
* ``voice.json`` (``%LOCALAPPDATA%\\Yuki``): the wake word, microphone, speaker
  and voice id go through yuki-ui when it runs (so the voice session switches
  at once); written here directly only when it does not.
* Start with Windows: :func:`yuki.ui.startup.set_enabled` (HKCU Run value).
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any


def privacy_path() -> Path:
    from yuki.memory.privacy import user_config_path

    return user_config_path()


def voice_prefs_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "voice.json"


def toml_value(value: Any) -> str:
    """A TOML literal for a bool, number, string or list of strings (the only kinds written here)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)   # a JSON string is a valid TOML basic string
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _table_of(line: str) -> str | None:
    s = line.strip()
    if s.startswith("[[") and s.endswith("]]"):
        return "[[" + s[2:-2].strip() + "]]"
    if s.startswith("[") and s.endswith("]") and not s.startswith("[["):
        return s[1:-1].strip()
    return None


def set_toml_key(text: str, table: str, key: str, value: Any) -> str:
    """``text`` with ``[table] key = value`` set, everything else (comments included) kept as it was."""
    lines = text.splitlines()
    literal = f"{key} = {toml_value(value)}"
    current: str | None = ""
    start = end = None
    for i, line in enumerate(lines):
        t = _table_of(line)
        if t is not None:
            if current == table and start is not None and end is None:
                end = i
            current = t
            if t == table:
                start = i
            continue
    if start is not None and end is None:
        end = len(lines)
    if start is None:
        body = lines + ([""] if lines and lines[-1].strip() else []) + [f"[{table}]", literal]
        return "\n".join(body) + "\n"
    i = start + 1
    while i < end:
        raw = lines[i]
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip().strip('"')
            if name == key:
                # a multi-line array continues until its closing bracket
                j = i
                value_part = stripped.split("=", 1)[1]
                depth = value_part.count("[") - value_part.count("]")
                while depth > 0 and j + 1 < end:
                    j += 1
                    depth += lines[j].count("[") - lines[j].count("]")
                lines[i:j + 1] = [literal]
                return "\n".join(lines) + "\n"
        i += 1
    # not in the table yet: after its last non-blank line
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, literal)
    return "\n".join(lines) + "\n"


def update_privacy(table: str, key: str, value: Any, path: Path | None = None) -> None:
    """Set one key in the privacy file (created with just that table when missing)."""
    path = path or privacy_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new = set_toml_key(text, table, key, value)
    parsed = tomllib.loads(new)                       # never write a file the service could not read
    if parsed.get(table, {}).get(key) != (list(value) if isinstance(value, tuple) else value):
        raise ValueError("the privacy file did not take the change")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)


def read_privacy(path: Path | None = None) -> dict[str, Any]:
    try:
        return tomllib.loads((path or privacy_path()).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def blocked(kind: str, path: Path | None = None) -> list[str]:
    """``apps`` or ``sites``: the user's own blocklist from ``[user]``."""
    key = "blocked_apps" if kind == "apps" else "blocked_domains"
    return [str(v) for v in read_privacy(path).get("user", {}).get(key, []) if str(v).strip()]


def set_blocked(kind: str, values: list[str], path: Path | None = None) -> None:
    key = "blocked_apps" if kind == "apps" else "blocked_domains"
    seen: list[str] = []
    for v in values:
        v = " ".join(str(v).split())
        if v and v.casefold() not in {s.casefold() for s in seen}:
            seen.append(v)
    update_privacy("user", key, seen, path)


def write_voice_prefs(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Merge ``changes`` into voice.json (only when yuki-ui is not running to do it)."""
    path = path or voice_prefs_path()
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
        prefs = prefs if isinstance(prefs, dict) else {}
    except (OSError, ValueError):
        prefs = {}
    for k, v in changes.items():
        key = {"mic": "mic_device", "speaker": "speaker_device"}.get(k, k)
        if v in (None, ""):
            prefs.pop(key, None)
        else:
            prefs[key] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
    return prefs
