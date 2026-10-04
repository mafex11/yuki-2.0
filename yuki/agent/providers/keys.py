"""The AI services' keys, kept on this PC encrypted with Windows DPAPI (current-user scope).

Settings → AI saves a key here: ``%LOCALAPPDATA%\\Yuki\\ai-keys.dat`` holds one DPAPI blob
(``CryptProtectData``, the same helper the memory data key uses, with its own entropy) of a small JSON map
``{provider: key}``. Only this Windows account on this PC can unwrap it; nothing is ever written in the clear, and
no key goes to a log, the database or the repo (callers get the value; logs get only :func:`key_source`).

A key saved here wins; otherwise the provider's environment variables are honoured (``OPENAI_API_KEY``,
``GEMINI_API_KEY`` then ``GOOGLE_API_KEY``, ``OPENROUTER_API_KEY``, ``ANTHROPIC_API_KEY``, ``DEEPSEEK_API_KEY``;
the process environment first, then ``HKCU\\Environment`` for a variable set after the process started).

:func:`saved_values` lets the secret filter hide a saved key wherever it shows up in captured text, whatever its
format (an exact match on the stored value, no guessing).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from yuki.agent.providers.catalog import PROVIDERS
from yuki.agent.providers.config import ai_dir

#: Extra DPAPI entropy: not a secret (it is in the source); it only keeps another program's blob from being read
#: as ours, and keeps this blob apart from the memory key's.
_ENTROPY = b"yuki-ai-keys-v1"
_DESCRIPTION = "Yuki AI service keys"
_UI_FORBIDDEN = 0x1
#: Longest key accepted (a JWT-style gateway token can be long; anything past this is a paste accident).
MAX_KEY_CHARS = 4096


class KeyStoreError(Exception):
    """The key file exists but this Windows account cannot unwrap it (or it is damaged)."""


def key_path() -> Path:
    return ai_dir() / "ai-keys.dat"


def _protect(data: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(data, _DESCRIPTION, _ENTROPY, None, None, _UI_FORBIDDEN)


def _unprotect(blob: bytes) -> bytes:
    import win32crypt

    try:
        _description, data = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, _UI_FORBIDDEN)
    except Exception as exc:  # pywintypes.error
        raise KeyStoreError("the saved keys can't be read by this Windows account") from exc
    return bytes(data)


class _Store:
    """The decrypted map, re-read when the file changes (another process saved a key)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[str, int, int] | None = None
        self._keys: dict[str, str] = {}

    @staticmethod
    def _stat(path: Path) -> tuple[str, int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (str(path), st.st_mtime_ns, st.st_size)

    def read(self) -> dict[str, str]:
        path = key_path()
        stamp = self._stat(path)
        with self._lock:
            if stamp == self._stamp and (stamp is not None or not self._keys):
                return dict(self._keys)
        keys: dict[str, str] = {}
        if stamp is not None:
            try:
                raw = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
                keys = {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v} if isinstance(raw, dict) else {}
            except KeyStoreError:
                raise
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise KeyStoreError("the saved keys file is damaged") from exc
        with self._lock:
            self._stamp, self._keys = stamp, keys
        return dict(keys)

    def write(self, keys: dict[str, str]) -> None:
        path = key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = _protect(json.dumps(keys, separators=(",", ":")).encode("utf-8"))
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        with self._lock:
            self._stamp, self._keys = self._stat(path), dict(keys)

    def version(self) -> tuple[str, int, int] | None:
        return self._stat(key_path())


_store = _Store()
_write_lock = threading.Lock()


def env_names(provider: str) -> tuple[str, ...]:
    info = PROVIDERS.get(provider)
    return info.key_env if info is not None else ()


def _env(provider: str) -> str | None:
    from yuki.core.models import get_key as env_key

    for name in env_names(provider):
        value = env_key(name)
        if value:
            return value
    return None


def saved_key(provider: str) -> str | None:
    """The key saved in Settings for ``provider`` (``None`` when none, or when the file can't be read)."""
    try:
        return _store.read().get(provider) or None
    except KeyStoreError:
        return None


def get_key(provider: str) -> str | None:
    """The key a call to ``provider`` uses: saved in Settings first, then its environment variables. Never logged."""
    return saved_key(provider) or _env(provider)


def key_source(provider: str) -> str:
    """``saved`` | ``env`` | ``missing`` | ``not_needed`` (content-free: safe to log and show)."""
    info = PROVIDERS.get(provider)
    if saved_key(provider):
        return "saved"
    if _env(provider):
        return "env"
    if info is not None and not info.needs_key:
        return "not_needed"
    return "missing"


def set_key(provider: str, key: str) -> None:
    """Save (or with an empty ``key``, forget) the key for ``provider``. Raises ``ValueError`` on a bad value."""
    if provider not in PROVIDERS or provider == "default":
        raise ValueError("unknown service")
    value = (key or "").strip()
    if len(value) > MAX_KEY_CHARS or any(ch.isspace() for ch in value):
        raise ValueError("That doesn't look like a key: it has spaces in it or is far too long.")
    with _write_lock:
        try:
            keys = _store.read()
        except KeyStoreError:
            keys = {}   # unreadable (another account's file): saving replaces it
        if value:
            keys[provider] = value
        else:
            keys.pop(provider, None)
        _store.write(keys)


def forget_key(provider: str) -> None:
    set_key(provider, "")


def saved_values() -> list[str]:
    """Every saved key's value (for the secret filter's exact-match hiding). Empty when none or unreadable."""
    try:
        return [v for v in _store.read().values() if len(v) >= 8]
    except KeyStoreError:
        return []


#: An environment variable's value is hidden as a key only from this length: every provider's real keys are far
#: longer (OpenAI, Anthropic, OpenRouter, DeepSeek, Gemini: 35 characters and up), while the stand-ins local AI tools
#: tell you to put there ("lm-studio", "sk-no-key-required", "not-needed") are shorter and are ordinary words that
#: must not vanish from memory wherever they appear. A key typed into Settings is hidden from 8 (it was given as one).
ENV_KEY_MIN_CHARS = 20


def secret_values() -> list[str]:
    """Every AI key this PC holds for Yuki: the saved ones and the providers' environment variables (a key in an
    environment variable is as secret as a saved one, whatever its format; a value shorter than
    :data:`ENV_KEY_MIN_CHARS` there is a stand-in, not a key). For exact-match hiding only."""
    out = saved_values()
    for provider in PROVIDERS:
        value = _env(provider)
        if value and len(value) >= ENV_KEY_MIN_CHARS and value not in out:
            out.append(value)
    return out


def version() -> tuple[str, int, int] | None:
    """Changes whenever the key file changes (clients rebuild when it does)."""
    return _store.version()


def redact(text: str, *extra: str | None) -> str:
    """``text`` with every saved key (and ``extra`` values) replaced by ``[key]``: for a provider's error message."""
    out = str(text or "")
    for value in [*secret_values(), *(v for v in extra if v)]:
        if value and len(value) >= 8:
            out = out.replace(value, "[key]")
    return out


__all__ = ["ENV_KEY_MIN_CHARS", "KeyStoreError", "MAX_KEY_CHARS",
           "env_names", "forget_key", "get_key", "key_path", "key_source", "redact",
           "saved_key", "saved_values", "secret_values", "set_key", "version"]
