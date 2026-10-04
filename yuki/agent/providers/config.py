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
      "prices": {"openrouter:some/model": {"input": 0.0, "output": 0.0}}
    }

No file, an unreadable file, or a role on ``default`` means what Yuki always did (Claude through Bedrock, or the
Anthropic API per ``Settings.model_backend``): :func:`route` answers ``None`` and the callers take their existing
path untouched. A role moved to another provider is a :class:`Route`; a backup is used only when the user set one
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


def load(path: Path | None = None) -> dict[str, Any]:
    """The whole file, cleaned (every role present; ``default`` where unset or unknown). Never raises."""
    path = Path(path) if path is not None else config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return load_from(data if isinstance(data, dict) else {})


def save(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Write ``data`` (cleaned) atomically; returns what was written."""
    path = Path(path) if path is not None else config_path()
    clean = load_from(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
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
    out.setdefault("version", 1)
    return out


class _Cache:
    """The parsed file per path, re-read when its modification time or size changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}

    def get(self, path: Path) -> dict[str, Any]:
        try:
            st = path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        key = str(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == stamp:
                return entry[1]
        data = load(path) if stamp is not None else load_from({})
        with self._lock:
            self._entries[key] = (stamp, data)
        return data

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _Cache()


def current(path: Path | None = None) -> dict[str, Any]:
    """The file as it is now (cached until it changes)."""
    return _cache.get(Path(path) if path is not None else config_path())


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


__all__ = ["DIR_ENV", "Route", "Target", "ai_dir", "config_path", "current", "custom_base_url", "load", "load_from",
           "route", "save", "saved_price"]
