"""The AI services Yuki can use, and what is known about their models (data, no judgement).

A *provider* is where a call goes: ``default`` (what Yuki always used: Claude through Amazon Bedrock, or the
Anthropic API per ``Settings.model_backend``), ``anthropic`` (the Anthropic API with the user's own key for one
role), and the services that speak the OpenAI chat-completions protocol (``openai``, ``gemini`` through Google's
OpenAI-compatible endpoint, ``openrouter``, ``ollama`` through its ``/v1`` endpoint, ``deepseek``, and ``custom``:
any other OpenAI-compatible base URL, e.g. OpenCode Zen or the Kilo gateway).

A *model* entry carries the facts the Settings page and the cost estimate need: whether it can use tools (doing
things needs tools), whether it is a small model (it may get multi-step work wrong), whether it reads images,
whether it takes a reasoning effort, and its list price in US dollars per million tokens. ``None`` means not
known: never guessed. Prices come from each vendor's published price list (checked when the entry was added; the
date is on each table) and only feed the cost *estimate* in the logs. OpenRouter's and Ollama's models are not
listed here: they are read from the services themselves (``yuki.agent.providers.discover``).

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The jobs a user can give to a model (Settings → AI): the voice's quick replies, the agent that does things,
#: the background memory passes.
ROLES: tuple[str, ...] = ("talker", "thinker", "memory")

#: How each role is named to the user (plain words).
ROLE_WORDS: dict[str, str] = {
    "talker": "Voice replies",
    "thinker": "Doing things",
    "memory": "Memory upkeep",
}

#: What each role is for, said to the user.
ROLE_HINTS: dict[str, str] = {
    "talker": "The quick spoken answers when you talk to me. A fast model feels best.",
    "thinker": "Working in your apps for you: clicking, typing, finding things. Needs a model that can use tools.",
    "memory": "Reading what happened in the background to keep my memory up to date. A cheap model is fine.",
}


@dataclass(frozen=True)
class Provider:
    """One AI service.

    Attributes:
        id: The machine name (``openai``).
        label: The plain name shown to the user.
        kind: ``default`` | ``anthropic`` | ``openai`` (the OpenAI chat-completions protocol).
        base_url: The OpenAI-protocol base URL (``.../v1``); empty for ``default`` / ``anthropic`` / ``custom``.
        key_env: Environment variables that hold its key, first wins (a key saved in Settings wins over all).
        needs_key: Whether a call needs a key at all (Ollama does not).
        caching: ``explicit`` (Anthropic's cache breakpoints are sent), ``auto`` (the service caches repeated
            prompt starts by itself and reports cached tokens), ``none``.
        images: Whether the service can take images at all (each model may still lack them).
        free: Every call is free (a local model).
        note: One plain sentence for the Settings page.
    """

    id: str
    label: str
    kind: str
    base_url: str = ""
    key_env: tuple[str, ...] = ()
    needs_key: bool = True
    caching: str = "none"
    images: bool = False
    free: bool = False
    note: str = ""


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("default", "Claude (as now)", "default", caching="explicit", images=True,
             note="What I use today. Nothing to set up."),
    Provider("anthropic", "Anthropic (your key)", "anthropic", key_env=("ANTHROPIC_API_KEY",), caching="explicit",
             images=True, note="Claude with your own Anthropic key."),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", ("OPENAI_API_KEY",), caching="auto",
             images=True, note="GPT models with your OpenAI key."),
    Provider("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
             ("GEMINI_API_KEY", "GOOGLE_API_KEY"), caching="auto", images=True,
             note="Gemini models with your Google AI Studio key."),
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",),
             images=True, note="Many models with one key, some of them free."),
    Provider("ollama", "Ollama (on this PC)", "openai", "http://127.0.0.1:11434/v1", needs_key=False, images=True,
             free=True, note="Models running on this PC. Free and private, but they use your CPU and graphics card."),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", ("DEEPSEEK_API_KEY",), caching="auto",
             note="DeepSeek models with your DeepSeek key."),
    Provider("custom", "Other (OpenAI-compatible)", "openai", needs_key=False, images=True,
             note="Any service that speaks the OpenAI protocol: give its address and key."),
)}

#: The providers a role can be moved to (everything but ``default`` is opt-in).
CHOOSABLE: tuple[str, ...] = tuple(PROVIDERS)


@dataclass(frozen=True)
class ModelInfo:
    """What is known about one model. ``None``: not known (the page says so; nothing is guessed)."""

    id: str
    label: str = ""
    tools: bool | None = None
    small: bool = False
    images: bool | None = None
    reasoning: bool = False
    free: bool = False
    #: US dollars per million tokens: ``input``, ``output``, ``cache_read`` (and ``cache_write``) when known.
    price: dict[str, float] | None = field(default=None, compare=False)
    #: The size in billions of parameters when the service says so (Ollama), else ``None``.
    params_b: float | None = None
    context: int | None = None


def _m(id: str, label: str, *, tools: bool | None = True, small: bool = False, images: bool | None = None,
       reasoning: bool = False, price: dict[str, float] | None = None) -> ModelInfo:
    return ModelInfo(id, label, tools=tools, small=small, images=images, reasoning=reasoning, price=price)


#: Suggested models per provider (the Settings page also lists what the service itself offers).
#: OpenAI list prices from openai.com/api/pricing as of 2025-08 (US$ per 1M tokens).
#: Gemini list prices from ai.google.dev/pricing as of 2025-07 (paid tier, prompts up to 200k tokens).
MODELS: dict[str, tuple[ModelInfo, ...]] = {
    "openai": (
        _m("gpt-5", "GPT-5", images=True, reasoning=True,
           price={"input": 1.25, "output": 10.0, "cache_read": 0.125}),
        _m("gpt-5-mini", "GPT-5 mini", images=True, reasoning=True,
           price={"input": 0.25, "output": 2.0, "cache_read": 0.025}),
        _m("gpt-5-nano", "GPT-5 nano", images=True, reasoning=True, small=True,
           price={"input": 0.05, "output": 0.40, "cache_read": 0.005}),
        _m("gpt-4.1", "GPT-4.1", images=True, price={"input": 2.0, "output": 8.0, "cache_read": 0.50}),
        _m("gpt-4.1-mini", "GPT-4.1 mini", images=True, price={"input": 0.40, "output": 1.60, "cache_read": 0.10}),
        _m("gpt-4.1-nano", "GPT-4.1 nano", images=True, small=True,
           price={"input": 0.10, "output": 0.40, "cache_read": 0.025}),
        _m("gpt-4o-mini", "GPT-4o mini", images=True, small=True,
           price={"input": 0.15, "output": 0.60, "cache_read": 0.075}),
    ),
    "gemini": (
        _m("gemini-2.5-pro", "Gemini 2.5 Pro", images=True, price={"input": 1.25, "output": 10.0}),
        _m("gemini-2.5-flash", "Gemini 2.5 Flash", images=True, price={"input": 0.30, "output": 2.50}),
        _m("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", images=True, small=True,
           price={"input": 0.10, "output": 0.40}),
    ),
    "anthropic": (
        # priced from the app's own Claude table (yuki.config.DEFAULT_PRICING) by their Bedrock ids; ``reasoning``:
        # takes adaptive thinking and an effort (Sonnet 5, Opus 5); Haiku 4.5 takes only a fixed thinking budget
        _m("claude-sonnet-5", "Claude Sonnet 5", images=True, reasoning=True),
        _m("claude-opus-5", "Claude Opus 5", images=True, reasoning=True),
        _m("claude-haiku-4-5-20251001", "Claude Haiku 4.5", images=True),
    ),
    "deepseek": (
        _m("deepseek-chat", "DeepSeek chat"),
        _m("deepseek-reasoner", "DeepSeek reasoner", tools=None),
    ),
}


def provider(provider_id: str) -> Provider | None:
    return PROVIDERS.get((provider_id or "").strip().lower())


def known_model(provider_id: str, model: str) -> ModelInfo | None:
    """The catalog entry for ``model`` on ``provider_id`` (exact id), else ``None``."""
    for info in MODELS.get(provider_id, ()):
        if info.id == model:
            return info
    return None


def qualified(provider_id: str, model: str) -> str:
    """``openai:gpt-5-mini``: one name for a model on a provider (what the logs and the cost estimate use)."""
    return f"{provider_id}:{model}"


def split_qualified(name: str) -> tuple[str, str] | None:
    """``(provider, model)`` for a :func:`qualified` name, else ``None`` (a Bedrock id such as
    ``global.anthropic.claude-haiku-4-5-20251001-v1:0`` has a colon too: only a known provider prefix counts)."""
    head, sep, rest = (name or "").partition(":")
    if sep and rest and head in PROVIDERS and head != "default":
        return head, rest
    return None


__all__ = ["CHOOSABLE", "MODELS", "ModelInfo", "PROVIDERS", "Provider", "ROLES", "ROLE_HINTS", "ROLE_WORDS",
           "known_model", "provider", "qualified", "split_qualified"]
