"""What a call on another provider cost: known prices, OpenRouter's own, free and local at 0, else unknown.

Nothing is invented: a model with no known price is ``None`` (the logs and the request summary then say
"unpriced"), never another model's rate.

* Ollama (local) and an OpenRouter ``:free`` variant (OpenRouter's documented id form for its free models) cost 0.
* A price Settings saved when you picked an OpenRouter model (OpenRouter's own list price for it), or one read from
  OpenRouter's model list in this process.
* The catalog's published list prices (``yuki.agent.providers.catalog``).
* Claude through the Anthropic API: the app's own Claude table (``Settings.pricing``), matched by model id.
* The exact amount OpenRouter returned for the last call on this thread, when the tokens being priced are that
  call's (``note_returned``).

The callers price with the model *they asked for* (the memory workers name their Claude model); after a routed
call, :func:`routed_name` says which provider model actually answered on this thread, so the estimate uses it.
"""

from __future__ import annotations

import threading
from typing import Any

from yuki.agent.providers.catalog import PROVIDERS, known_model, split_qualified

_local = threading.local()

ZERO = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def note_routed(requested: str | None, name: str | None) -> None:
    """The last call on this thread asked for ``requested`` and was answered by ``name`` (qualified); ``None``
    clears it (a call that went the default way)."""
    _local.routed = (requested, name) if requested and name else None


def routed_name(requested: str) -> str | None:
    """The qualified model that answered the last call on this thread, when that call asked for ``requested``."""
    rec = getattr(_local, "routed", None)
    return rec[1] if rec and rec[0] == requested else None


def note_returned(cost: float | None, tokens: dict[str, int]) -> None:
    """The amount the service returned for the last call on this thread (OpenRouter), with its token counts."""
    _local.returned = (float(cost), dict(tokens)) if isinstance(cost, (int, float)) else None


def _returned_for(tokens: dict[str, int]) -> float | None:
    rec = getattr(_local, "returned", None)
    if not rec:
        return None
    cost, seen = rec
    keys = ("input_tokens", "output_tokens", "cache_read_tokens")
    return cost if all(int(tokens.get(k) or 0) == int(seen.get(k) or 0) for k in keys) else None


def is_free(provider_id: str, model: str) -> bool:
    info = PROVIDERS.get(provider_id)
    if info is not None and info.free:
        return True
    return provider_id == "openrouter" and model.endswith(":free")


def rates(name: str, pricing: dict[str, dict[str, float]] | None = None) -> dict[str, float] | None:
    """``$/1M tokens`` for a qualified model, or ``None`` when not known."""
    split = split_qualified(name)
    if split is None:
        return None
    provider_id, model = split
    if is_free(provider_id, model):
        return dict(ZERO)
    from yuki.agent.providers import config, discover

    saved = config.saved_price(name)
    if saved is not None:
        return saved
    found = discover.cached_price(provider_id, model)
    if found is not None:
        return found
    info = known_model(provider_id, model)
    if info is not None and info.free:
        return dict(ZERO)
    if info is not None and info.price:
        return dict(info.price)
    if provider_id == "anthropic" and pricing:
        from yuki.core.models import api_model_id

        for bedrock_id, table in pricing.items():
            if api_model_id(bedrock_id) == model:
                return dict(table)
    return None


def cost(name: str, tokens: dict[str, int], pricing: dict[str, dict[str, float]] | None = None) -> float | None:
    """US dollars for ``tokens`` (``usage_tokens`` keys) on a qualified model; ``None`` when not known."""
    split = split_qualified(name)
    if split is not None and split[0] == "openrouter":
        returned = _returned_for(tokens)
        if returned is not None:
            return returned
    table = rates(name, pricing)
    if table is None:
        return None
    return (int(tokens.get("input_tokens") or 0) * float(table.get("input", 0.0))
            + int(tokens.get("cache_write_tokens") or 0) * float(table.get("cache_write", table.get("input", 0.0)))
            + int(tokens.get("cache_read_tokens") or 0) * float(table.get("cache_read", table.get("input", 0.0)))
            + int(tokens.get("output_tokens") or 0) * float(table.get("output", 0.0))) / 1_000_000


def price_words(name: str, pricing: dict[str, dict[str, float]] | None = None) -> str:
    """The price in plain words for the Settings page: ``Free``, ``$0.25 in, $2.00 out per million words``, or
    ``Price not known``."""
    table = rates(name, pricing)
    if table is None:
        return "Price not known"
    if not any(float(table.get(k) or 0) for k in ("input", "output")):
        return "Free"
    return f"${float(table.get('input') or 0):.2f} in, ${float(table.get('output') or 0):.2f} out per million tokens"


def usage_cost_fields(usage: Any) -> float | None:
    """OpenRouter's returned cost from a response's usage (``provider_cost_usd``), else ``None``."""
    value = getattr(usage, "provider_cost_usd", None)
    if value is None and isinstance(usage, dict):
        value = usage.get("provider_cost_usd")
    return float(value) if isinstance(value, (int, float)) else None


__all__ = ["ZERO", "cost", "is_free", "note_returned", "note_routed", "price_words", "rates", "routed_name",
           "usage_cost_fields"]
