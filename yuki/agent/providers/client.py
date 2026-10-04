"""Clients per provider, and the voice's role-aware client (plumbing).

* :func:`sync_provider_client` / :func:`async_provider_client`: the client for one provider, with the key from
  ``yuki.agent.providers.keys``: the OpenAI-protocol adapter (``openai_compat``) for OpenAI, Gemini, OpenRouter,
  Ollama, DeepSeek and a custom address; ``anthropic.Anthropic`` with the user's key (model ids mapped as
  ``yuki.core.models`` does) for ``anthropic``; the app's usual Claude client for ``default`` (a backup only).
  A provider that needs a key and has none raises :class:`ProviderSetupError` in plain words (never a silent
  switch to another provider).
* :class:`AsyncRoleClient`: the voice talker's client. With the talker on ``default`` every call goes to the
  client it wraps, untouched (the Pipecat service, the spoken-line check, the progress answer and the router all
  call it). With the talker moved, the call goes to the chosen provider and model, retried there on throttling,
  overload, 5xx or no connection (backoff, ``retry-after`` honoured), then to the user's backup if one is set.

The sync role routing (the thinker and the memory passes) lives in ``yuki.agent.failover.FailoverClient``.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Callable

from yuki.agent.providers.catalog import PROVIDERS, ROLE_WORDS


class ProviderSetupError(RuntimeError):
    """A role points at a provider that can't be used as set up (no key, no model, no address). Plain words."""


def _timeouts(settings: Any, timeout: float | None) -> tuple[float, float]:
    read = float(timeout if timeout is not None else getattr(settings, "model_timeout_s", 90.0) or 90.0)
    connect = float(getattr(settings, "model_connect_timeout_s", 10.0) or 10.0)
    return read, min(connect, read)


def _base_url(provider_id: str) -> str:
    from yuki.agent.providers import config

    if provider_id == "custom":
        base = config.custom_base_url()
        if not base:
            raise ProviderSetupError("The other service has no address yet. Add it in Settings, under AI.")
        return base
    return PROVIDERS[provider_id].base_url


def _key(provider_id: str) -> str | None:
    from yuki.agent.providers.keys import get_key

    info = PROVIDERS[provider_id]
    key = get_key(provider_id)
    if info.needs_key and not key:
        raise ProviderSetupError(f"No {info.label} key is saved. Add it in Settings, under AI.")
    return key


def _model_info(provider_id: str) -> Callable[[str], Any]:
    from yuki.agent.providers.discover import model_facts

    return lambda model: model_facts(provider_id, model)


def sync_provider_client(provider_id: str, settings: Any = None, *, transport: Any = None,
                         timeout: float | None = None) -> Any:
    """The sync client for ``provider_id`` (Anthropic surface). Raises :class:`ProviderSetupError`."""
    info = PROVIDERS.get(provider_id)
    if info is None:
        raise ProviderSetupError("That AI service isn't one I know.")
    read, connect = _timeouts(settings, timeout)
    if info.kind == "default":
        import anthropic

        from yuki.core.models import sync_client

        return sync_client(getattr(settings, "aws_region", None), backend=getattr(settings, "model_backend", None),
                           max_retries=0, timeout=anthropic.Timeout(read, connect=connect, write=30.0, pool=10.0))
    key = _key(provider_id)
    if info.kind == "anthropic":
        import anthropic

        from yuki.core.models import MappedClient

        return MappedClient(anthropic.Anthropic(api_key=key, max_retries=0,
                                                timeout=anthropic.Timeout(read, connect=connect, write=30.0,
                                                                          pool=10.0)))
    from yuki.agent.providers.openai_compat import OpenAICompatClient

    return OpenAICompatClient(info, base_url=_base_url(provider_id), api_key=key, model_info=_model_info(provider_id),
                              timeout=read, connect_timeout=connect, transport=transport)


def async_provider_client(provider_id: str, settings: Any = None, *, transport: Any = None,
                          timeout: float | None = None) -> Any:
    """The async twin of :func:`sync_provider_client`."""
    info = PROVIDERS.get(provider_id)
    if info is None:
        raise ProviderSetupError("That AI service isn't one I know.")
    read, connect = _timeouts(settings, timeout)
    if info.kind == "default":
        from yuki.core.models import async_client

        return async_client(getattr(settings, "aws_region", None), backend=getattr(settings, "model_backend", None),
                            max_retries=0)
    key = _key(provider_id)
    if info.kind == "anthropic":
        import anthropic

        from yuki.core.models import MappedClient

        return MappedClient(anthropic.AsyncAnthropic(api_key=key, max_retries=0,
                                                     timeout=anthropic.Timeout(read, connect=connect, write=30.0,
                                                                               pool=10.0)))
    from yuki.agent.providers.openai_compat import AsyncOpenAICompatClient

    return AsyncOpenAICompatClient(info, base_url=_base_url(provider_id), api_key=key,
                                   model_info=_model_info(provider_id), timeout=read, connect_timeout=connect,
                                   transport=transport)


def missing_model(role: str) -> ProviderSetupError:
    return ProviderSetupError(f"No model is picked for {ROLE_WORDS.get(role, role).lower()}. "
                              "Pick one in Settings, under AI.")


# -- the voice talker's client -----------------------------------------------------------------------------------------


class _Prefetched:
    """An opened raw stream whose first event was already read (so a failure at its start could be retried)."""

    def __init__(self, stream: Any, first: Any, has_first: bool) -> None:
        self._stream = stream
        self._first = first
        self._has_first = has_first
        self._it = stream.__aiter__()

    def __aiter__(self) -> "_Prefetched":
        return self

    async def __anext__(self) -> Any:
        if self._has_first:
            self._has_first = False
            return self._first
        return await self._it.__anext__()

    async def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _OpenedManager:
    """``async with client.messages.stream(...)`` on a routed call: opened (first event read) with retries."""

    def __init__(self, owner: "AsyncRoleClient", params: dict[str, Any]) -> None:
        self._owner = owner
        self._params = params
        self._manager: Any = None

    async def __aenter__(self) -> Any:
        async def open_one(client: Any, params: dict[str, Any]) -> Any:
            manager = client.messages.stream(**params)
            stream = await manager.__aenter__()
            try:
                it = stream.__aiter__()
                try:
                    first, has_first = await it.__anext__(), True
                except StopAsyncIteration:
                    first, has_first = None, False
            except BaseException as exc:
                try:
                    await manager.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._manager = manager
            return _PrefetchedHelper(stream, it, first, has_first)

        return await self._owner._run(self._params, open_one)

    async def __aexit__(self, *exc: Any) -> Any:
        if self._manager is not None:
            return await self._manager.__aexit__(*exc)
        return None


class _PrefetchedHelper:
    """The helper stream (``messages.stream``) with its first event read; ``get_final_message`` drains the rest."""

    def __init__(self, stream: Any, it: Any, first: Any, has_first: bool) -> None:
        self._stream = stream
        self._it = it
        self._first = first
        self._has_first = has_first

    def __aiter__(self) -> "_PrefetchedHelper":
        return self

    async def __anext__(self) -> Any:
        if self._has_first:
            self._has_first = False
            return self._first
        return await self._it.__anext__()

    async def get_final_message(self) -> Any:
        async for _ in self:
            pass
        return await self._stream.get_final_message()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncRoleMessages:
    def __init__(self, owner: "AsyncRoleClient", beta: bool) -> None:
        self._owner = owner
        self._beta = beta

    def _default(self) -> Any:
        base = self._owner._default
        return base.beta.messages if self._beta else base.messages

    async def create(self, **params: Any) -> Any:
        if self._owner.route() is None:
            return await self._default().create(**params)
        if params.get("stream"):
            async def open_raw(client: Any, sent: dict[str, Any]) -> Any:
                stream = await (client.beta.messages if self._beta else client.messages).create(**sent)
                it = stream.__aiter__()
                try:
                    first, has_first = await it.__anext__(), True
                except StopAsyncIteration:
                    first, has_first = None, False
                return _Prefetched(_IterHolder(stream, it), first, has_first)

            return await self._owner._run(params, open_raw)

        async def plain(client: Any, sent: dict[str, Any]) -> Any:
            return await (client.beta.messages if self._beta else client.messages).create(**sent)

        return await self._owner._run(params, plain)

    def stream(self, **params: Any) -> Any:
        if self._owner.route() is None:
            return self._default().stream(**params)
        return _OpenedManager(self._owner, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._default(), name)


class _IterHolder:
    """A stream and the iterator already taken from it (so the prefetch keeps reading the same one)."""

    def __init__(self, stream: Any, it: Any) -> None:
        self._stream = stream
        self._it = it

    def __aiter__(self) -> Any:
        return self._it

    async def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncBeta:
    def __init__(self, owner: "AsyncRoleClient") -> None:
        self.messages = _AsyncRoleMessages(owner, beta=True)


class AsyncRoleClient:
    """An async Anthropic-surface client for one role (the voice talker): the wrapped client while the role is on
    ``default``, the chosen provider (with retries, then the user's backup) when it is not.

    Args:
        role: ``talker`` (or any role in ``yuki.agent.providers.catalog.ROLES``).
        default: The client used while the role is on ``default`` (the voice's keep-alive Bedrock client).
        settings: For timeouts, retries and the default backend (a backup set to ``default``).
        log: ``log(event, **fields)`` for each retry and failover (content-free); ``None`` to not log.
        sleep: The async wait between retries (tests).
        factory: ``factory(provider_id) -> async client`` (tests); default :func:`async_provider_client`.
    """

    def __init__(self, role: str, default: Any, *, settings: Any = None, log: Callable[..., Any] | None = None,
                 sleep: Callable[[float], Any] | None = None, factory: Callable[[str], Any] | None = None) -> None:
        self.role = role
        self._default = default
        self.settings = settings
        self._log = log
        self._sleep = sleep or asyncio.sleep
        self._factory = factory or (lambda provider_id: async_provider_client(provider_id, settings))
        self._clients: dict[tuple[Any, ...], Any] = {}
        self.messages = _AsyncRoleMessages(self, beta=False)
        self.beta = _AsyncBeta(self)
        self._last: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._default, name)

    def route(self) -> Any:
        from yuki.agent.providers.config import route

        return route(self.role)

    def routed(self) -> bool:
        """Whether this role's calls go to another provider right now (the voice then skips its Bedrock chain)."""
        return self.route() is not None

    def last_model(self) -> str | None:
        """``provider:model`` of the last routed call (``None`` after a default one)."""
        return self._last

    def prompt_caching(self) -> str:
        """``explicit`` | ``auto`` | ``none``: the prompt cache of where this role's calls go now."""
        chosen = self.route()
        if chosen is None:
            return "explicit"
        info = PROVIDERS.get(chosen.primary.provider)
        return info.caching if info is not None else "none"

    def _client(self, provider_id: str) -> Any:
        from yuki.agent.providers import config, keys

        stamp = (provider_id, keys.version(), config.custom_base_url() if provider_id == "custom" else "")
        client = self._clients.get(stamp)
        if client is None:
            client = self._factory(provider_id)
            self._clients = {k: v for k, v in self._clients.items() if k[0] != provider_id}
            self._clients[stamp] = client
        return client

    def _event(self, event: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    async def _run(self, params: dict[str, Any], call: Callable[[Any, dict[str, Any]], Any]) -> Any:
        from yuki.agent.failover import _retry_after, adapt_params, classify

        route = self.route()
        if route is None:   # moved back to default between the check and now
            return await call(self._default, params)
        if not route.primary.model:
            raise missing_model(self.role)
        requested = str(params.get("model") or "")
        if self.role == "talker" and params.get("tools"):
            # Pipecat's Anthropic service runs one tool call per reply (the last): a service that may answer with
            # several at once is asked for one
            choice = params.get("tool_choice") if isinstance(params.get("tool_choice"), dict) else {"type": "auto"}
            params = {**params, "tool_choice": {**choice, "disable_parallel_tool_use": True}}
        chain = [route.primary]
        if route.backup is not None:
            from yuki.agent.providers.config import Target

            chain.append(Target(route.backup.provider, route.backup.model or (
                requested if route.backup.provider == "default" else "")))
        retries = max(0, int(getattr(self.settings, "model_retries", 2) or 0)) if self.settings is not None else 2
        base_s = float(getattr(self.settings, "model_retry_base_s", 0.6) or 0.6)
        max_s = float(getattr(self.settings, "model_retry_max_s", 4.0) or 4.0)
        last: BaseException | None = None
        for index, target in enumerate(chain):
            if not target.model:
                continue
            sent = adapt_params({**params, "model": requested}, target.model)
            for attempt in range(retries + 1):
                try:
                    client = self._default if target.provider == "default" and self._default is not None \
                        else self._client(target.provider)
                    result = await call(client, sent)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified from the SDK's own types
                    last = exc
                    kind = classify(exc)
                    if kind == "fatal":
                        raise
                    if kind == "retry" and attempt < retries:
                        wait = min(max_s, base_s * (2 ** attempt)) * (0.8 + 0.4 * random.random())
                        hinted = _retry_after(exc)
                        if hinted is not None:
                            wait = max(wait, min(max_s, hinted))
                        self._event("model_retry", role=self.role, target=target.name, attempt=attempt + 1,
                                    wait_s=round(wait, 2), status=getattr(exc, "status_code", None),
                                    error=type(exc).__name__)
                        await self._sleep(wait)
                        continue
                    if index + 1 < len(chain):
                        self._event("model_failover", role=self.role, **{"from": target.name},
                                    to=chain[index + 1].name, status=getattr(exc, "status_code", None),
                                    error=type(exc).__name__)
                    break
                else:
                    self._last = target.name if target.provider != "default" else None
                    if index:
                        self._event("model_failover_ok", role=self.role, used=target.name)
                    return result
        assert last is not None
        raise last


def role_async_client(role: str, default: Any, settings: Any = None, *, log: Callable[..., Any] | None = None) -> Any:
    """The client the voice uses for ``role``: :class:`AsyncRoleClient` around its usual client."""
    return AsyncRoleClient(role, default, settings=settings, log=log)


def talker_price_model(settings: Any, client: Any = None) -> str:
    """The model to price the talker's calls with: the routed ``provider:model`` when the talker is moved, else
    ``Settings.voice_mouth_model`` (as before)."""
    from yuki.agent.providers.config import route

    r = route("talker")
    if r is None or not r.primary.model:
        return getattr(settings, "voice_mouth_model", "")
    last = client.last_model() if client is not None and callable(getattr(client, "last_model", None)) else None
    return last or r.primary.name


__all__ = ["AsyncRoleClient", "ProviderSetupError", "async_provider_client", "missing_model", "role_async_client",
           "sync_provider_client", "talker_price_model"]
