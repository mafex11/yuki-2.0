"""Settings → AI and the doctor: what each job runs on, in plain words, and the edits Settings makes.

* :func:`roles_state` / :func:`page` build what the window's AI section shows (every role's provider and model,
  each provider's key status, never a key) and :func:`doctor_lines` what ``yuki-cli doctor`` reports.
* :func:`set_role`, :func:`set_key`, :func:`set_custom` are the window's actions (each writes ``ai.json`` or the
  DPAPI key file; the processes pick the change up from their next call, no restart).

User-facing text: second person, plain words, no ids or commands.
"""

from __future__ import annotations

import threading
from typing import Any

from yuki.agent.providers.catalog import PROVIDERS, ROLE_HINTS, ROLE_WORDS, ROLES

#: Shown under each saved key.
SAVED_WORDS = "Saved on this PC, encrypted"
#: How a change applies.
APPLY_WORDS = "Changes apply from your next request. No restart needed."

#: One edit of ai.json at a time (the window runs each action on its own thread).
_edit = threading.Lock()

_KEY_WORDS = {"saved": SAVED_WORDS, "env": "From your environment settings", "missing": "No key yet",
              "not_needed": "No key needed"}


def _provider_words(provider_id: str) -> str:
    info = PROVIDERS.get(provider_id)
    if provider_id == "custom":
        from yuki.agent.providers import config

        label = config.current()["custom"]["label"]
        return label or (info.label if info else provider_id)
    return info.label if info is not None else provider_id


def role_line(role: str) -> dict[str, Any]:
    """One role's choice: provider, model, backup, whether its key is there, and a plain warning when it doesn't fit."""
    from yuki.agent.providers import config, discover
    from yuki.agent.providers.keys import key_source

    choice = config.current()["roles"].get(role) or {}
    provider = choice.get("provider") or "default"
    model = choice.get("model") or ""
    out: dict[str, Any] = {"role": role, "words": ROLE_WORDS[role], "hint": ROLE_HINTS[role], "provider": provider,
                           "provider_words": _provider_words(provider), "model": model,
                           "backup_provider": choice.get("backup_provider") or "",
                           "backup_model": choice.get("backup_model") or "", "warning": "", "blocked": False}
    if provider == "default":
        out["key"] = "not_needed"
        return out
    out["key"] = key_source(provider)
    out["blocked"] = True   # until shown otherwise: this job can't run as set up
    if provider == "custom" and not config.custom_base_url():
        out["warning"] = "The other service has no address yet, so this job can't run. Add it below."
    elif out["key"] == "missing":
        out["warning"] = f"No {PROVIDERS[provider].label} key yet, so this job can't run. Add the key below."
    elif not model:
        out["warning"] = "Pick a model for this job."
    else:
        out["blocked"] = False
        out["warning"] = discover.fit_for(role, discover.model_facts(provider, model), provider)
    return out


def roles_state() -> list[dict[str, Any]]:
    return [role_line(role) for role in ROLES]


def providers_state() -> list[dict[str, Any]]:
    """Each provider's key status and the models already known (no network: the page asks with ``models_reply``).
    OpenRouter's few hundred models are not all carried in the page: its free ones and the ones a job uses."""
    from yuki.agent.providers import config, discover
    from yuki.agent.providers.keys import env_names, key_source

    roles = roles_state()
    chosen = {(r["provider"], r["model"]) for r in roles} | {(r["backup_provider"], r["backup_model"]) for r in roles}
    out = []
    for pid, info in PROVIDERS.items():
        if pid == "default":
            continue
        entry: dict[str, Any] = {"id": pid, "label": _provider_words(pid), "note": info.note,
                                 "needs_key": info.needs_key, "key": key_source(pid),
                                 "env": list(env_names(pid))}
        entry["key_words"] = _KEY_WORDS[entry["key"]]
        if pid == "custom":
            entry["base_url"] = config.current()["custom"]["base_url"]
        models = discover.known_list(pid)
        if pid == "openrouter":
            models = [m for m in discover.free_first(models) if m.free or (pid, m.id) in chosen]
        entry["models"] = discover.describe_list(pid, models) or []
        out.append(entry)
    return out


def ollama_words(models: list[Any] | None) -> str:
    if models is None:
        return "Ollama isn't running"
    if not models:
        return "Ollama is running but has no models yet"
    return f"{len(models)} model{'s' if len(models) != 1 else ''} on this PC"


def models_reply(provider_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """The window's ``ai_models`` answer: a service's models as the page lists them (free ones first), asked of the
    service itself (network, or the local Ollama; run off the UI thread)."""
    from yuki.agent.providers import discover

    models = discover.list_models(provider_id, refresh=refresh)
    out: dict[str, Any] = {"models": discover.describe_list(provider_id, discover.free_first(models or [])) or []}
    if provider_id == "ollama":
        out["running"] = models is not None
        out["status"] = ollama_words(models)
    return out


def page() -> dict[str, Any]:
    """The AI section's data (no network)."""
    return {"roles": roles_state(), "providers": providers_state(), "apply": APPLY_WORDS,
            "choices": [{"id": pid, "label": _provider_words(pid)} for pid in PROVIDERS]}


def doctor_lines() -> tuple[str, list[str], list[str]]:
    """``(status, lines, fixes)`` for the doctor: ``ok`` / ``warn`` / ``fail``, one line per role (never a key)."""
    lines, fixes, status = [], [], "ok"
    for line in roles_state():
        if line["provider"] == "default":
            lines.append(f"{line['words']}: Claude (as now)")
            continue
        key = {"saved": "key saved", "env": "key from the environment", "missing": "NO KEY",
               "not_needed": "no key needed"}[line["key"]]
        lines.append(f"{line['words']}: {line['provider_words']}, {line['model'] or 'no model picked'} ({key})")
        if line["blocked"]:
            status = "fail"
            fixes.append(f"{line['words']}: {line['warning']} (Settings, AI)")
        elif line["warning"] and status == "ok":
            status = "warn"
            fixes.append(f"{line['words']}: {line['warning']}")
        if line["backup_provider"]:
            lines[-1] += f", backup {_provider_words(line['backup_provider'])}" + (
                f" {line['backup_model']}" if line["backup_model"] else "")
    return status, lines, fixes


# -- the window's actions ------------------------------------------------------------------------------------------------


def set_role(role: str, provider: str, model: str = "", backup_provider: str | None = None,
             backup_model: str | None = None, price: dict[str, float] | None = None) -> dict[str, Any]:
    """Point ``role`` at ``provider``/``model`` (``default``: back to Claude as before). Returns the role's new line."""
    from yuki.agent.providers import config

    if role not in ROLES:
        raise ValueError("unknown job")
    if provider not in PROVIDERS:
        raise ValueError("unknown service")
    with _edit:
        _write_role(config, role, provider, model, backup_provider, backup_model, price)
    return role_line(role)


def _write_role(config: Any, role: str, provider: str, model: str, backup_provider: str | None,
                backup_model: str | None, price: dict[str, float] | None) -> None:
    data = config.load()
    entry = dict(data["roles"].get(role) or {})
    entry["provider"] = provider
    entry["model"] = "" if provider == "default" else " ".join(str(model or "").split())[:200]
    if backup_provider is not None:
        if backup_provider and backup_provider not in PROVIDERS:
            raise ValueError("unknown service")
        entry["backup_provider"] = backup_provider
        entry["backup_model"] = " ".join(str(backup_model or "").split())[:200] if backup_provider else ""
    data["roles"][role] = entry
    if price is not None and provider != "default" and entry["model"]:
        try:
            data["prices"][f"{provider}:{entry['model']}"] = {k: float(v) for k, v in price.items()
                                                              if k in ("input", "output", "cache_read")}
        except (TypeError, ValueError):
            pass
    # what the service's own list said about the picked models (tools, images, size, context): the tray, where the
    # agent runs, and the doctor never read the lists, so they read these (yuki.agent.providers.discover.model_facts)
    from yuki.agent.providers.discover import listed_facts

    for pid, mid in ((provider, entry["model"]), (entry.get("backup_provider") or "", entry.get("backup_model") or "")):
        if pid and pid != "default" and mid:
            facts = listed_facts(pid, mid)
            if facts:
                data["facts"][f"{pid}:{mid}"] = facts
    config.save(data)


def set_key(provider: str, key: str) -> dict[str, Any]:
    """Save (or with ``""`` forget) a provider's key. Returns its new status (never the key)."""
    from yuki.agent.providers.keys import key_source
    from yuki.agent.providers.keys import set_key as store

    store(provider, key)
    source = key_source(provider)
    return {"key": source, "key_words": _KEY_WORDS[source]}


def set_custom(base_url: str, label: str = "") -> dict[str, Any]:
    """The other OpenAI-compatible service's address (and a name for it)."""
    from urllib.parse import urlsplit

    from yuki.agent.providers import config

    url = (base_url or "").strip().rstrip("/")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("That address doesn't look right. It should start with https://")
        if parts.username or parts.password:
            raise ValueError("Leave the key out of the address and paste it in the key box instead.")
    with _edit:
        data = config.load()
        data["custom"] = {"base_url": url, "label": " ".join(str(label or "").split())[:60]}
        config.save(data)
    return {"base_url": url}


def openrouter_price(model: str) -> dict[str, float] | None:
    """OpenRouter's own price for ``model`` from its list (read earlier in this process), to save with the choice."""
    from yuki.agent.providers.discover import cached_price

    return cached_price("openrouter", model)


__all__ = ["APPLY_WORDS", "SAVED_WORDS", "doctor_lines", "models_reply", "ollama_words", "openrouter_price", "page",
           "providers_state", "role_line", "roles_state", "set_custom", "set_key", "set_role"]
