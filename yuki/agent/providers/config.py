"""Which AI does each job: ``%LOCALAPPDATA%\\Yuki\\ai.json`` (written by Settings → AI, read by every process).

Shape (unknown keys are kept, so an older and a newer build can share the file)::

    {
      "version": 1,
      "roles": {
        "talker":  {"provider": "default", "model": "", "backup_provider": "", "backup_model": ""},
        "thinker": {"provider": "openrouter", "model": "some/model:free", ...},
        "memory":  {"provider": "ollama", "model": "llama3.2"}
      },
      "custom": {"label": "My gateway", "base_url": "https://example.invalid/v1"},
      "prices": {"openrouter:some/model": {"input": 0.0, "output": 0.0}},
      "facts": {"openrouter:some/model": {"tools": true, "images": false, "small": false, "context": 32768}}
    }

``prices`` and ``facts`` are what the service's own list said about a model when you picked it in Settings (the
window is the only process that reads those lists): the tray, where the agent runs, and the doctor read them here.

No file, an unreadable file, or a role on ``default`` means what Yuki always did (Claude through Bedrock, or the
Anthropic API per ``Settings.model_backend``): :func:`route` answers ``None`` and the callers take their existing
path untouched. A file that is there but can't be read is not silent: the model clients log
``ai_config_unreadable`` once per version (:func:`take_note`) and the doctor warns (:func:`problem`). A role moved to another provider is a :class:`Route`; a backup is used only when the user set one
(never a silent fall back to Claude).

The tray, the memory service and the window are separate processes: each reads the file again when its
modification time changes (one ``stat`` per model call), so a change applies from the next call without a restart.
Keys are never in this file (``yuki.agent.providers.keys``). ``YUKI_AI_DIR`` moves the folder (tests).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuki.agent.providers.catalog import PROVIDERS, ROLES

#: Moves ai.json and the key file (the test run points it at a temp folder).
DIR_ENV = "YUKI_AI_DIR"


def ai_dir() -> Path:
    forced = (os.environ.get(DIR_ENV) or "").strip()
    if forced:
        return Path(forced)
    from yuki.core.paths import data_dir

    return data_dir()


def config_path() -> Path:
    return ai_dir() / "ai.json"


@dataclass(frozen=True)
class Target:
    """One provider + model."""

    provider: str
    model: str

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Route:
    """Where a role's calls go when it is not on ``default``: the chosen target, then the user's backup (if any).

    ``backup.provider`` may be ``default``: Claude as before, with the model the caller asked for.
    """

    role: str
    primary: Target
    backup: Target | None = None

    def signature(self) -> tuple[Any, ...]:
        return (self.primary, self.backup)


def _clean_role(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in ("provider", "model", "backup_provider", "backup_model"):
        value = raw.get(key)
        out[key] = " ".join(str(value).split()) if isinstance(value, str) else ""
    if out["provider"] not in PROVIDERS:
        out["provider"] = "default"
    if out["backup_provider"] and out["backup_provider"] not in PROVIDERS:
        out["backup_provider"] = ""
    return out


#: A read or a replace Windows refused (another process had the file open that instant) is tried this many times.
_LOCK_TRIES = 6
_LOCK_WAIT_S = 0.01


class LockedError(OSError):
    """ai.json is there but stayed locked by another process for every try (a save racing a read, a virus scan)."""


def _read(path: Path) -> tuple[dict[str, Any] | None, str]:
    """``(data, state)``: ``state`` is ``ok`` (a JSON object), ``missing`` (no file: Claude as always, nothing to
    say), ``unreadable`` (the file is there but isn't a readable JSON object) or ``locked`` (refused every try).
    A UTF-8 byte-order mark (Notepad's) is accepted."""
    for attempt in range(_LOCK_TRIES):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return None, "missing"
        except PermissionError:
            if path.is_dir():
                return None, "unreadable"
            if attempt + 1 < _LOCK_TRIES:
                time.sleep(_LOCK_WAIT_S)
                continue
            return None, "locked"
        except (OSError, ValueError):   # not UTF-8 (UTF-16), a device, ...
            return None, "unreadable"
        try:
            data = json.loads(text)
        except ValueError:
            return None, "unreadable"
        return (data, "ok") if isinstance(data, dict) else (None, "unreadable")
    return None, "locked"   # pragma: no cover - the loop always returns


def load(path: Path | None = None) -> dict[str, Any]:
    """The whole file, cleaned (every role present; ``default`` where unset or unknown). Never raises: a missing,
    unreadable or locked file reads as every role on ``default`` (an edit uses :func:`load_for_edit`)."""
    path = Path(path) if path is not None else config_path()
    data, _state = _read(path)
    return load_from(data or {})


def load_for_edit(path: Path | None = None) -> dict[str, Any]:
    """:func:`load` for a load-modify-save: raises :class:`LockedError` when the file is there but stayed locked, so
    an edit never writes every role back to ``default`` over choices it couldn't read."""
    path = Path(path) if path is not None else config_path()
    data, state = _read(path)
    if state == "locked":
        raise LockedError("Your AI choices file is busy right now. Try again in a moment.")
    return load_from(data or {})


def save(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Write ``data`` (cleaned) atomically; returns what was written. A replace Windows refuses because another
    process is reading the file that instant is tried again a few times (then it raises)."""
    path = Path(path) if path is not None else config_path()
    clean = load_from(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(_LOCK_TRIES):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt + 1 >= _LOCK_TRIES:
                raise
            time.sleep(_LOCK_WAIT_S)
    _cache.clear()
    return clean


def load_from(data: dict[str, Any]) -> dict[str, Any]:
    """The cleaning :func:`load` applies, on an in-memory dict (every role present; ``default`` where unset)."""
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    out = dict(data)
    out["roles"] = {role: _clean_role(roles.get(role)) for role in ROLES} | {
        k: v for k, v in roles.items() if k not in ROLES}
    custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
    out["custom"] = {"label": str(custom.get("label") or "").strip()[:60],
                     "base_url": str(custom.get("base_url") or "").strip()}
    prices = data.get("prices") if isinstance(data.get("prices"), dict) else {}
    out["prices"] = {str(k): v for k, v in prices.items() if isinstance(v, dict)}
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    out["facts"] = {str(k): v for k, v in facts.items() if isinstance(v, dict)}
    out.setdefault("version", 1)
    return out


class _Cache:
    """The parsed file per path, re-read when its modification time or size changes.

    A read Windows refused (a writer had the file that instant) is never kept: that call uses the last good reading
    and the next call reads again. A file that is there but can't be read is noted once per version
    (:func:`take_note`) and told by the doctor (:func:`problem`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple[int, int] | None, dict[str, Any], str]] = {}
        #: path -> stamp of an unreadable version not yet handed to a logger, and the last one handed
        self._notes: dict[str, tuple[int, int]] = {}
        self._noted: dict[str, tuple[int, int]] = {}

    def entry(self, path: Path) -> tuple[dict[str, Any], str]:
        try:
            st = path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        key = str(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == stamp:
                return entry[1], entry[2]
        raw, state = _read(path) if stamp is not None else (None, "missing")
        if state == "locked":
            with self._lock:
                entry = self._entries.get(key)
            return (entry[1], entry[2]) if entry is not None else (load_from({}), "locked")
        data = load_from(raw or {})
        with self._lock:
            self._entries[key] = (stamp, data, state)
            if state == "unreadable" and stamp is not None and self._noted.get(key) != stamp:
                self._notes[key] = stamp
        return data, state

    def get(self, path: Path) -> dict[str, Any]:
        return self.entry(path)[0]

    def take_note(self, path: Path) -> bool:
        self.entry(path)
        key = str(path)
        with self._lock:
            stamp = self._notes.pop(key, None)
            if stamp is None:
                return False
            self._noted[key] = stamp
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _Cache()


def current(path: Path | None = None) -> dict[str, Any]:
    """The file as it is now (cached until it changes)."""
    return _cache.get(Path(path) if path is not None else config_path())


def problem(path: Path | None = None) -> str | None:
    """``unreadable`` when ai.json is there but isn't a readable JSON object (so every job is on Claude), else
    ``None`` (no file is no problem: Claude as always)."""
    state = _cache.entry(Path(path) if path is not None else config_path())[1]
    return "unreadable" if state == "unreadable" else None


def take_note(path: Path | None = None) -> bool:
    """``True`` once per version of an unreadable ai.json in this process: the caller logs a content-free
    ``ai_config_unreadable``, so falling back to Claude over a broken file is never silent."""
    return _cache.take_note(Path(path) if path is not None else config_path())


def route(role: str, path: Path | None = None) -> Route | None:
    """Where ``role``'s calls go, or ``None`` for ``default`` (the existing path, untouched)."""
    choice = current(path)["roles"].get(role) or {}
    provider = choice.get("provider") or "default"
    if provider == "default":
        return None
    backup = None
    if choice.get("backup_provider"):
        backup = Target(choice["backup_provider"], choice.get("backup_model") or "")
    return Route(role, Target(provider, choice.get("model") or ""), backup)


def custom_base_url(path: Path | None = None) -> str:
    return current(path)["custom"]["base_url"]


def saved_price(name: str, path: Path | None = None) -> dict[str, float] | None:
    """The price snapshot Settings saved for a qualified model (OpenRouter's own prices), else ``None``."""
    entry = current(path)["prices"].get(name)
    if not isinstance(entry, dict):
        return None
    try:
        return {k: float(v) for k, v in entry.items() if k in ("input", "output", "cache_read", "cache_write")}
    except (TypeError, ValueError):
        return None


#: The model facts kept in ai.json (``yuki.agent.providers.catalog.ModelInfo`` fields).
FACT_KEYS = ("tools", "images", "small", "context", "params_b", "run_context")


def saved_facts(name: str, path: Path | None = None) -> dict[str, Any] | None:
    """What the service's list said about a qualified model when Settings saved the choice, else ``None``."""
    entry = current(path)["facts"].get(name)
    if not isinstance(entry, dict):
        return None
    return {k: entry[k] for k in FACT_KEYS if k in entry}


__all__ = ["DIR_ENV", "FACT_KEYS", "LockedError", "Route", "Target", "ai_dir", "config_path", "current",
           "custom_base_url", "load", "load_for_edit", "load_from", "problem", "route", "save", "saved_facts", "saved_price", "take_note"]
