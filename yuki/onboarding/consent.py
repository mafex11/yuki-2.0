"""The first-run privacy choices, saved through memory's existing settings.

* **Ambient memory** (what is on screen) has one existing off switch: the
  ``paused`` flag file next to the memory database
  (``yuki.memory.store.PAUSE_FLAG``, the same file the tray's "Pause memory"
  and ``MemoryClient.set_paused`` write). Off = the flag exists (the watcher
  captures nothing), on = no flag.
* **Call memory** is ``[calls] enabled`` in the user's ``privacy.toml``
  (``%LOCALAPPDATA%\\Yuki\\memory\\privacy.toml``; created from the packaged
  defaults first, as ``PrivacyConfig`` does). Only that one value is changed,
  in place: comments, order and every other line are kept, and the result is
  parsed back with ``tomllib`` before it replaces the file.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from datetime import datetime
from pathlib import Path


def memory_paths(memory_dir: str | Path | None = None) -> tuple[Path, Path]:
    """``(privacy.toml, paused flag)`` for ``memory_dir`` or memory's default folder."""
    if memory_dir is not None:
        from yuki.memory.store import PAUSE_FLAG  # noqa: PLC0415 - lazy: the store module imports numpy

        folder = Path(memory_dir)
        return folder / "privacy.toml", folder / PAUSE_FLAG
    from yuki.memory.privacy import user_config_path  # stdlib only
    from yuki.memory.store import PAUSE_FLAG, flag_path

    return user_config_path(), flag_path(None, PAUSE_FLAG)


def set_ambient_memory(enabled: bool, pause_flag: Path) -> None:
    """On: remove the ``paused`` flag. Off: write it (its text is the time, like ``set_paused``)."""
    if enabled:
        try:
            pause_flag.unlink()
        except FileNotFoundError:
            pass
        return
    pause_flag.parent.mkdir(parents=True, exist_ok=True)
    pause_flag.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")


def _table_name(line: str) -> str | None:
    """The table a TOML header line opens (``"calls"`` for ``[calls]``), else ``None``."""
    text = line.strip()
    if not text.startswith("["):
        return None
    head = text.split("#", 1)[0].strip()
    if head.startswith("[[") and head.endswith("]]"):
        return head[2:-2].strip()
    if head.endswith("]"):
        return head[1:-1].strip()
    return None


def set_toml_bool(text: str, table: str, key: str, value: bool) -> str:
    """``text`` with ``[table] key = value`` set, changing as little as possible.

    The first ``key = ...`` line inside ``[table]`` gets the new value (its
    indent and trailing comment kept); without one, the line is added under the
    header; without the table, it is appended. Raises ``ValueError`` when the
    result does not parse or does not hold the value.
    """
    lit = "true" if value else "false"
    lines = text.splitlines(keepends=True)
    current: str | None = None
    header_at: int | None = None
    done = False
    for i, line in enumerate(lines):
        name = _table_name(line)
        if name is not None:
            current = name
            if name == table and header_at is None:
                header_at = i
            continue
        if current != table or "=" not in line:
            continue
        left, right = line.split("=", 1)
        if left.strip() != key:
            continue
        indent = left[: len(left) - len(left.lstrip())]
        comment = ""
        if "#" in right:
            comment = "  #" + right.split("#", 1)[1].rstrip("\r\n")
        newline = line[len(line.rstrip("\r\n")):] or "\n"
        lines[i] = f"{indent}{key} = {lit}{comment}{newline}"
        done = True
        break
    if not done:
        if header_at is not None:
            lines.insert(header_at + 1, f"{key} = {lit}\n")
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n[{table}]\n{key} = {lit}\n")
    out = "".join(lines)
    try:
        parsed = tomllib.loads(out)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"privacy file would not parse after the edit: {exc}") from exc
    if (parsed.get(table) or {}).get(key) is not value:
        raise ValueError(f"privacy file edit did not take: [{table}] {key}")
    return out


def set_call_memory(enabled: bool, privacy_path: Path) -> None:
    """Set ``[calls] enabled`` in the user's privacy file (created from the defaults if missing)."""
    if not privacy_path.exists():
        from yuki.memory.privacy import DEFAULT_PATH

        privacy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_PATH, privacy_path)
    raw = privacy_path.read_bytes()
    text = raw.decode("utf-8-sig")
    out = set_toml_bool(text, "calls", "enabled", enabled)
    if out == text:
        return
    tmp = privacy_path.with_name(privacy_path.name + ".tmp")
    tmp.write_bytes(out.encode("utf-8"))
    os.replace(tmp, privacy_path)


def save_consent(ambient: bool, calls: bool, memory_dir: str | Path | None = None) -> dict:
    """Save both choices; returns ``{"ambient_memory": bool, "call_memory": bool}``."""
    privacy_path, pause_flag = memory_paths(memory_dir)
    set_ambient_memory(ambient, pause_flag)
    set_call_memory(calls, privacy_path)
    return {"ambient_memory": bool(ambient), "call_memory": bool(calls)}
