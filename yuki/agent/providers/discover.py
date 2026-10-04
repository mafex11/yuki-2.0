"""What each AI service offers right now, and a one-call test of it (for Settings → AI and the doctor).

* :func:`list_models` asks the service for its models: Ollama's ``GET /api/tags`` (the models on this PC, with
  their size; ``None`` when Ollama isn't running), OpenRouter's ``GET /api/v1/models`` (no key needed: every
  model with its price, whether it takes tools and images; the free ones marked), and the ``/models`` list of
  OpenAI, Gemini, DeepSeek or a custom service (ids only, so tool use and price come from the catalog or stay
  "not known"). Each is merged with the catalog's facts.
* :func:`test` sends one tiny request (a few tokens) and answers ``(ok, plain words)``.

Everything here runs off the UI thread (the window's actions each have their own short thread) with short
timeouts, and is cached briefly so opening Settings twice doesn't ask twice.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from yuki.agent.providers.catalog import MODELS, PROVIDERS, ModelInfo, known_model

#: How long a model list is kept before the service is asked again.
LIST_TTL_S = 300.0
#: Below this many billions of parameters a local model is marked small (it may get multi-step work wrong).
SMALL_MODEL_B = 8.0

_lock = threading.Lock()
_lists: dict[str, tuple[float, list[ModelInfo] | None]] = {}
_prices: dict[tuple[str, str], dict[str, float]] = {}
_facts: dict[tuple[str, str], ModelInfo] = {}


def _get(url: str, headers: dict[str, str] | None = None, timeout: float = 6.0, transport: Any = None) -> Any:
    import httpx2

    options: dict[str, Any] = {"timeout": httpx2.Timeout(timeout, connect=min(timeout, 3.0))}
    if transport is not None:
        options["transport"] = transport
    with httpx2.Client(**options) as client:
        response = client.get(url, headers=headers or {})
        if response.status_code >= 400:
            raise _HTTPStatus(response.status_code, _safe_json(response))
        return response.json()


def _post(url: str, body: dict[str, Any], timeout: float = 8.0, transport: Any = None) -> Any:
    import httpx2

    options: dict[str, Any] = {"timeout": httpx2.Timeout(timeout, connect=min(timeout, 3.0))}
    if transport is not None:
        options["transport"] = transport
    with httpx2.Client(**options) as client:
        response = client.post(url, json=body)
        if response.status_code >= 400:
            raise _HTTPStatus(response.status_code, _safe_json(response))
        return response.json()


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


class _HTTPStatus(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _ollama_root(base_url: str | None = None) -> str:
    base = (base_url or PROVIDERS["ollama"].base_url).rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _size_b(details: dict[str, Any]) -> float | None:
    """Ollama's ``parameter_size`` (``"3.2B"``, ``"770M"``) in billions; ``None`` when it isn't given."""
    text = str((details or {}).get("parameter_size") or "").strip().upper()
    if not text:
        return None
    unit = text[-1]
    try:
        number = float(text[:-1])
    except ValueError:
        return None
    return number / 1000 if unit == "M" else number if unit == "B" else number * 1000 if unit == "T" else None


def ollama_models(*, base_url: str | None = None, transport: Any = None, with_capabilities: bool = True
                  ) -> list[ModelInfo] | None:
    """The models Ollama has on this PC, or ``None`` when it isn't running (nothing is started or installed)."""
    root = _ollama_root(base_url)
    try:
        data = _get(f"{root}/api/tags", timeout=2.0, transport=transport)
    except Exception:  # noqa: BLE001 - not running (or not there): say so, never start it
        return None
    out = []
    for item in (data or {}).get("models") or []:
        name = str(item.get("name") or item.get("model") or "")
        if not name:
            continue
        size = _size_b(item.get("details") or {})
        tools = images = None
        if with_capabilities:
            try:   # Ollama 0.6+: what the model can do (tools, vision)
                shown = _post(f"{root}/api/show", {"model": name}, timeout=3.0, transport=transport)
                caps = shown.get("capabilities") if isinstance(shown, dict) else None
                if isinstance(caps, list):
                    tools, images = "tools" in caps, "vision" in caps
            except Exception:  # noqa: BLE001 - an older Ollama: not known
                pass
        out.append(ModelInfo(name, name, tools=tools, small=size is not None and size < SMALL_MODEL_B,
                             images=images, free=True, price={"input": 0.0, "output": 0.0}, params_b=size))
    return out


def openrouter_models(*, transport: Any = None) -> list[ModelInfo]:
    """OpenRouter's whole list: price (US$ per 1M tokens), tools, images, free marked (no key needed)."""
    data = _get("https://openrouter.ai/api/v1/models", timeout=8.0, transport=transport)
    out = []
    for item in (data or {}).get("data") or []:
        mid = str(item.get("id") or "")
        if not mid:
            continue
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        try:
            price = {"input": float(pricing.get("prompt") or 0) * 1_000_000,
                     "output": float(pricing.get("completion") or 0) * 1_000_000}
            if pricing.get("input_cache_read") is not None:
                price["cache_read"] = float(pricing["input_cache_read"]) * 1_000_000
        except (TypeError, ValueError):
            price = None
        params = item.get("supported_parameters")
        modality = (item.get("architecture") or {}).get("input_modalities")
        free = mid.endswith(":free") or (price is not None and not price["input"] and not price["output"])
        out.append(ModelInfo(mid, str(item.get("name") or mid), tools=("tools" in params) if isinstance(params, list)
                             else None, images=("image" in modality) if isinstance(modality, list) else None,
                             free=free, price=price, context=item.get("context_length")))
    return out


def plain_models(provider_id: str, *, key: str | None, base_url: str = "", transport: Any = None) -> list[ModelInfo]:
    """A service's ``/models`` ids (OpenAI, Gemini, DeepSeek, custom), with the catalog's facts where known."""
    info = PROVIDERS[provider_id]
    base = (base_url or info.base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _get(f"{base}/models", headers=headers, timeout=6.0, transport=transport)
    out = []
    for item in (data or {}).get("data") or []:
        mid = str(item.get("id") or "")
        if provider_id == "gemini" and mid.startswith("models/"):
            mid = mid[len("models/"):]
        if not mid:
            continue
        out.append(known_model(provider_id, mid) or ModelInfo(mid, mid))
    return out


def list_models(provider_id: str, *, refresh: bool = False, transport: Any = None) -> list[ModelInfo] | None:
    """The models to offer for ``provider_id``: the service's own list merged with the catalog (cached
    :data:`LIST_TTL_S`). ``None`` only for Ollama when it isn't running. A service that can't be asked (no key,
    offline) offers the catalog's suggestions."""
    from yuki.agent.providers import config
    from yuki.agent.providers.keys import get_key

    now = time.monotonic()
    with _lock:
        hit = _lists.get(provider_id)
        if hit is not None and not refresh and now - hit[0] < LIST_TTL_S:
            return hit[1]
    found: list[ModelInfo] | None
    try:
        if provider_id == "ollama":
            found = ollama_models(transport=transport)
        elif provider_id == "openrouter":
            found = openrouter_models(transport=transport)
        elif provider_id in ("openai", "gemini", "deepseek", "custom"):
            key = get_key(provider_id)
            base = config.custom_base_url() if provider_id == "custom" else ""
            if (provider_id != "custom" and not key) or (provider_id == "custom" and not base):
                found = []
            else:
                found = plain_models(provider_id, key=key, base_url=base, transport=transport)
        else:
            found = []
    except Exception:  # noqa: BLE001 - offline / refused: the catalog's suggestions
        found = None if provider_id == "ollama" else []
    if found is not None:
        listed = {m.id for m in found}
        found = [*[m for m in MODELS.get(provider_id, ()) if m.id not in listed], *found] \
            if provider_id not in ("openrouter", "ollama") else found
    with _lock:
        _lists[provider_id] = (now, found)
        for m in found or []:
            _facts[(provider_id, m.id)] = m
            if m.price is not None and provider_id == "openrouter":
                _prices[(provider_id, m.id)] = dict(m.price)
    return found


def cached_price(provider_id: str, model: str) -> dict[str, float] | None:
    """A price read from the service's own list in this process (OpenRouter), else ``None``."""
    with _lock:
        price = _prices.get((provider_id, model))
    return dict(price) if price is not None else None


def model_facts(provider_id: str, model: str) -> ModelInfo | None:
    """What is known about one model: the catalog, else what the service's list said (this process)."""
    known = known_model(provider_id, model)
    if known is not None:
        return known
    with _lock:
        return _facts.get((provider_id, model))


def fit_for(role: str, info: ModelInfo | None) -> str:
    """A plain warning when ``info`` doesn't suit ``role``, else ``""`` (facts only: tools, size)."""
    if role != "thinker":
        if info is not None and info.tools is False and role == "talker":
            return "This model can't use tools, so it can't hand work over or use your to-do list from voice."
        return ""
    if info is None or info.tools is None:
        return "I don't know if this model can use tools. Doing things needs tools; if it can't, requests will fail."
    if info.tools is False:
        return "This model can't use tools, so it can't do things for you. Pick another for this job."
    if info.small:
        return "This is a small model. It may get multi-step work in your apps wrong."
    return ""


def describe(info: ModelInfo, provider_id: str) -> dict[str, Any]:
    """One model as the Settings page shows it."""
    from yuki.agent.providers.pricing import is_free, price_words

    out = {k: v for k, v in asdict(info).items() if k in ("id", "label", "tools", "small", "images", "free")}
    out["label"] = info.label or info.id
    out["free"] = bool(info.free) or is_free(provider_id, info.id)
    if provider_id == "openrouter" and info.price is not None:
        out["price"] = "Free" if out["free"] else price_words_from(info.price)
    else:
        out["price"] = price_words(f"{provider_id}:{info.id}")
    if info.params_b is not None:
        out["size"] = f"{info.params_b:g}B"
    return out


def price_words_from(price: dict[str, float]) -> str:
    return f"${float(price.get('input') or 0):.2f} in, ${float(price.get('output') or 0):.2f} out (per million tokens)"


def test(provider_id: str, model: str = "", *, transport: Any = None, settings: Any = None) -> tuple[bool, str]:
    """One tiny call to ``provider_id`` (``model``, else a cheap one it offers): ``(ok, plain words)``. Never raises."""
    from yuki.agent.providers.client import sync_provider_client
    from yuki.agent.providers.keys import key_source

    info = PROVIDERS.get(provider_id)
    if info is None or provider_id == "default":
        return False, "Pick a service to test."
    if info.needs_key and key_source(provider_id) == "missing":
        return False, f"No {info.label} key yet. Paste it above and press Save."
    if provider_id == "ollama":
        models = ollama_models(transport=transport, with_capabilities=False)
        if models is None:
            return False, "Ollama isn't running on this PC."
        if not models:
            return False, "Ollama is running but has no models yet."
        model = model or models[0].id
    if not model:
        model = _cheapest(provider_id)
    if not model:
        return False, "Pick a model first, then test."
    try:
        client = sync_provider_client(provider_id, settings, transport=transport, timeout=20.0)
        client.messages.create(model=model, max_tokens=8, messages=[{"role": "user", "content": "Say ok."}])
    except Exception as exc:  # noqa: BLE001 - said in plain words
        return False, plain_error(exc)
    return True, f"Works. {info.label} answered with {model}."


def _cheapest(provider_id: str) -> str:
    models = MODELS.get(provider_id, ())
    small = [m for m in models if m.small]
    return (small or list(models) or [ModelInfo("")])[0].id


def plain_error(exc: BaseException) -> str:
    """An exception from a call as one plain sentence: the status in plain words and the service's own reason
    when it gave one (read from the structured error body, never a key)."""
    import anthropic

    from yuki.agent.providers.client import ProviderSetupError
    from yuki.agent.providers.keys import redact
    from yuki.agent.providers.openai_compat import _reason, plain_status

    if isinstance(exc, ProviderSetupError):
        return str(exc)
    if isinstance(exc, anthropic.APITimeoutError):
        return "It took too long to answer. Try again in a moment."
    if isinstance(exc, anthropic.APIConnectionError):
        return redact(getattr(exc, "message", "") or "Couldn't connect. Check the internet connection.")[:300]
    if isinstance(exc, anthropic.APIStatusError):
        reason = redact(_reason(getattr(exc, "body", None)))
        words = plain_status(int(exc.status_code))
        return (words[0].upper() + words[1:] + (f". It said: {reason}" if reason else ".")).strip()[:300]
    if isinstance(exc, NotImplementedError):
        return redact(str(exc))[:300]
    return f"That didn't work ({type(exc).__name__})."


def describe_list(provider_id: str, models: list[ModelInfo] | None) -> list[dict[str, Any]] | None:
    if models is None:
        return None
    return [describe(m, provider_id) for m in models]


__all__ = ["LIST_TTL_S", "SMALL_MODEL_B", "cached_price", "describe", "describe_list", "fit_for", "list_models",
           "model_facts", "ollama_models", "openrouter_models", "plain_error", "plain_models", "test"]
