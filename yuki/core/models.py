"""Which model service Yuki talks to: Amazon Bedrock (the owner's setup) or the Anthropic API (anyone with a key).

``model_backend``:

* ``"bedrock"`` — ``anthropic.AnthropicBedrock`` / ``AsyncAnthropicBedrock``, auth from the
  ``AWS_BEARER_TOKEN_BEDROCK`` user environment variable, a region per client (failover across
  regions). Model ids are Bedrock ids (``global.anthropic.claude-sonnet-5``).
* ``"anthropic"`` — ``anthropic.Anthropic`` / ``AsyncAnthropic`` with ``ANTHROPIC_API_KEY``. The
  whole codebase keeps naming models by their Bedrock ids (settings, memory workers, pricing, the
  failover chains); the client returned here rewrites the ``model`` of every request to the
  first-party id on the way out (:func:`api_model_id`: ``global.anthropic.claude-sonnet-5`` ->
  ``claude-sonnet-5``, ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` -> ``claude-haiku-4-5-20251001``).
  Regions mean nothing there: every region gets the same client.

Which one (:func:`resolve_backend`, first that answers):

1. ``YUKI_MODEL_BACKEND`` environment variable;
2. ``model_backend`` in ``%LOCALAPPDATA%\\Yuki\\settings.json`` (written by the first-run onboarding);
3. the keys present: a Bedrock token -> ``bedrock``; else an Anthropic key -> ``anthropic``;
   else ``bedrock`` (the doctor then says which key is missing).

Keys are only ever read from the environment (``os.environ``, then ``HKCU\\Environment`` for a
variable set after this process started); nothing here writes or logs a key.

Stdlib only at import; the SDK loads when a client is built.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

Backend = Literal["bedrock", "anthropic"]
BACKENDS: tuple[str, ...] = ("bedrock", "anthropic")

BEDROCK_KEY = "AWS_BEARER_TOKEN_BEDROCK"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"

#: Bedrock inference-profile prefixes (geography) that may lead a model id.
_PROFILE_PREFIXES = ("global", "us", "eu", "apac", "jp", "au", "ca", "us-gov")


def get_key(name: str) -> str | None:
    """A key from ``os.environ``, then ``HKCU\\Environment``; ``None`` when unset. Never logged."""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
        if not isinstance(value, str):
            return None
        if kind == winreg.REG_EXPAND_SZ:
            value = os.path.expandvars(value)
        return value.strip() or None
    except OSError:
        return None


def prefs_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "settings.json"


def _saved_backend() -> str | None:
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("model_backend") if isinstance(data, dict) else None
    return value if value in BACKENDS else None


def resolve_backend() -> Backend:
    """The backend this process uses (see the module docstring for the order)."""
    forced = (os.environ.get("YUKI_MODEL_BACKEND") or "").strip().lower()
    if forced in BACKENDS:
        return forced  # type: ignore[return-value]
    saved = _saved_backend()
    if saved:
        return saved  # type: ignore[return-value]
    if get_key(BEDROCK_KEY):
        return "bedrock"
    if get_key(ANTHROPIC_KEY):
        return "anthropic"
    return "bedrock"


def key_name(backend: str) -> str:
    """The environment variable that authenticates ``backend``."""
    return ANTHROPIC_KEY if backend == "anthropic" else BEDROCK_KEY


def has_key(backend: str | None = None) -> bool:
    return bool(get_key(key_name(backend or resolve_backend())))


def api_model_id(model: str) -> str:
    """A Bedrock model id as the Anthropic API names it (anything else passes through).

    Parses Bedrock's fixed id format: ``[<profile>.]anthropic.<name>[-v<major>:<minor>]``.
    """
    text = (model or "").strip()
    head, _, rest = text.partition(".")
    if rest and head in _PROFILE_PREFIXES:
        text = rest
    if text.startswith("anthropic."):
        text = text[len("anthropic."):]
    else:
        return model   # not a Bedrock id: already an API id or an alias the caller resolves
    base, sep, version = text.rpartition("-v")
    if sep and ":" in version and all(part.isdigit() for part in version.split(":")):
        text = base
    return text


# -- clients -------------------------------------------------------------------------------------


class _MappedMessages:
    """``client.messages`` / ``client.beta.messages`` with ``model`` rewritten to the API id."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @staticmethod
    def _map(params: dict[str, Any]) -> dict[str, Any]:
        if "model" in params and isinstance(params["model"], str):
            params = {**params, "model": api_model_id(params["model"])}
        return params

    def create(self, **params: Any) -> Any:
        return self._inner.create(**self._map(params))

    def stream(self, **params: Any) -> Any:
        return self._inner.stream(**self._map(params))

    def count_tokens(self, **params: Any) -> Any:
        return self._inner.count_tokens(**self._map(params))

    def parse(self, **params: Any) -> Any:
        return self._inner.parse(**self._map(params))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _MappedBeta:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.messages = _MappedMessages(inner.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class MappedClient:
    """An ``Anthropic``/``AsyncAnthropic`` client that accepts Bedrock model ids (sync and async alike:
    the wrapped methods return whatever the SDK returns, coroutines included)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.messages = _MappedMessages(inner.messages)
        self.beta = _MappedBeta(inner.beta)
        self.model_backend = "anthropic"

    def with_options(self, **options: Any) -> "MappedClient":
        return MappedClient(self._inner.with_options(**options))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _ensure_bedrock_env() -> None:
    """The Bedrock client reads its token from ``os.environ``: bring a registry-only value in."""
    if not (os.environ.get(BEDROCK_KEY) or "").strip():
        value = get_key(BEDROCK_KEY)
        if value:
            os.environ[BEDROCK_KEY] = value


def sync_client(region: str | None = None, *, backend: str | None = None, **options: Any) -> Any:
    """A sync client for ``backend`` (default :func:`resolve_backend`); ``options`` go to the SDK
    constructor (``max_retries``, ``timeout``, ``http_client``)."""
    import anthropic

    backend = backend or resolve_backend()
    if backend == "anthropic":
        return MappedClient(anthropic.Anthropic(api_key=get_key(ANTHROPIC_KEY), **options))
    _ensure_bedrock_env()
    return anthropic.AnthropicBedrock(aws_region=region or os.environ.get("AWS_REGION", "ap-south-1"), **options)


def async_client(region: str | None = None, *, backend: str | None = None, **options: Any) -> Any:
    """The async twin of :func:`sync_client` (the voice mouth)."""
    import anthropic

    backend = backend or resolve_backend()
    if backend == "anthropic":
        return MappedClient(anthropic.AsyncAnthropic(api_key=get_key(ANTHROPIC_KEY), **options))
    _ensure_bedrock_env()
    return anthropic.AsyncAnthropicBedrock(aws_region=region or os.environ.get("AWS_REGION", "ap-south-1"),
                                           **options)


__all__ = [
    "Backend", "BACKENDS", "BEDROCK_KEY", "ANTHROPIC_KEY", "get_key", "prefs_path", "resolve_backend", "key_name",
    "has_key", "api_model_id", "MappedClient", "sync_client", "async_client",
]
