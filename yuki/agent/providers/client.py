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


def fit_claude_params(params: dict[str, Any], info: Any) -> dict[str, Any]:
    """``params`` for a Claude model on the Anthropic API, by the catalog's facts: a model known not to take adaptive
    thinking or an effort (Haiku 4.5: ``reasoning`` false) gets neither (the thinker and the memory passes build
    their requests for Sonnet 5). An unknown model id is sent as it is (nothing guessed)."""
    if info is None or info.reasoning:
        return params
    out = dict(params)
    thinking = out.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        out.pop("thinking")
    config = out.get("output_config")
    if isinstance(config, dict) and "effort" in config:
        rest = {k: v for k, v in config.items() if k != "effort"}
        if rest:
            out["output_config"] = rest
        else:
            out.pop("output_config")
    return out


def _field(block: Any, name: str) -> Any:
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def strip_unsigned_thinking(params: dict[str, Any]) -> dict[str, Any]:
    """``params`` without the reasoning blocks another service produced (a ``thinking`` block with no signature:
    the OpenAI-protocol adapter's). Claude checks every thinking block's signature, so a conversation that began on
    another service and is handed to Claude (the user's backup) would be refused with them. Nothing else changes; a
    request with none is returned as it is."""
    messages = params.get("messages")
    if not isinstance(messages, list):
        return params
    out, changed = [], False
    for message in messages:
        role = _field(message, "role")
        content = _field(message, "content")
        if role == "assistant" and isinstance(content, list):
            kept = [b for b in content if not (_field(b, "type") == "thinking" and not _field(b, "signature"))]
            if len(kept) != len(content):
                changed = True
                base = dict(message) if isinstance(message, dict) else {"role": role}
                message = {**base, "content": kept or [{"type": "text", "text": "(thought it over)"}]}
        out.append(message)
    return {**params, "messages": out} if changed else params


class _FittedMessages:
    """``messages`` / ``beta.messages`` of a Claude client with :func:`fit_claude_params` and
    :func:`strip_unsigned_thinking` applied (whatever the wrapped call returns, coroutines included)."""

    def __init__(self, inner: Any, facts: Callable[[str], Any]) -> None:
        self._inner = inner
        self._facts = facts

    def _fit(self, params: dict[str, Any]) -> dict[str, Any]:
        return strip_unsigned_thinking(fit_claude_params(params, self._facts(str(params.get("model") or ""))))

    def create(self, **params: Any) -> Any:
        return self._inner.create(**self._fit(params))

    def stream(self, **params: Any) -> Any:
        return self._inner.stream(**self._fit(params))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FittedBeta:
    def __init__(self, inner: Any, facts: Callable[[str], Any]) -> None:
        self._inner = inner
        self.messages = _FittedMessages(inner.messages, facts)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class FittedClaudeClient:
    """A Claude client with each request fitted to the Claude model it names: the Anthropic API client (user's key),
    or the app's usual client when it is a role's backup (``backend=None``: only another service's unsigned
    reasoning is taken out)."""

    def __init__(self, inner: Any, facts: Callable[[str], Any], backend: str | None = "anthropic") -> None:
        self._inner = inner
        self.messages = _FittedMessages(inner.messages, facts)
        self.beta = _FittedBeta(inner.beta, facts)
        if backend is not None:
            self.model_backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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

        return FittedClaudeClient(sync_client(
            getattr(settings, "aws_region", None), backend=getattr(settings, "model_backend", None), max_retries=0,
            timeout=anthropic.Timeout(read, connect=connect, write=30.0, pool=10.0)), lambda _model: None, backend=None)
    key = _key(provider_id)
    if info.kind == "anthropic":
        import anthropic

        from yuki.core.models import MappedClient

        return FittedClaudeClient(MappedClient(anthropic.Anthropic(
            api_key=key, max_retries=0, timeout=anthropic.Timeout(read, connect=connect, write=30.0, pool=10.0))),
            _model_info(provider_id))
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

        return FittedClaudeClient(async_client(getattr(settings, "aws_region", None),
                                               backend=getattr(settings, "model_backend", None), max_retries=0),
                                  lambda _model: None, backend=None)
    key = _key(provider_id)
    if info.kind == "anthropic":
        import anthropic

        from yuki.core.models import MappedClient

        return FittedClaudeClient(MappedClient(anthropic.AsyncAnthropic(
            api_key=key, max_retries=0, timeout=anthropic.Timeout(read, connect=connect, write=30.0, pool=10.0))),
            _model_info(provider_id))
    from yuki.agent.providers.openai_compat import AsyncOpenAICompatClient

    return AsyncOpenAICompatClient(info, base_url=_base_url(provider_id), api_key=key,
                                   model_info=_model_info(provider_id), timeout=read, connect_timeout=connect,
                                   transport=transport)


def missing_model(role: str) -> ProviderSetupError:
    return ProviderSetupError(f"No model is picked for {ROLE_WORDS.get(role, role).lower()}. "
                              "Pick one in Settings, under AI.")


# -- the voice talker's client -----------------------------------------------------------------------------------------


class _Prefetched:
    """An opened raw stream whose first event was already read (so a failure at its start could be retried).
    ``on_end(stream)`` runs once when the stream has been read to its end (the talker's usage record)."""

    def __init__(self, stream: Any, first: Any, has_first: bool,
                 on_end: Callable[[Any], None] | None = None,
                 on_error: Callable[[BaseException], None] | None = None) -> None:
        self._stream = stream
        self._first = first
        self._has_first = has_first
        self._it = stream.__aiter__()
        self._on_end = on_end
        self._on_error = on_error

    def __aiter__(self) -> "_Prefetched":
        return self

    async def __anext__(self) -> Any:
        if self._has_first:
            self._has_first = False
            return self._first
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            on_end, self._on_end = self._on_end, None
            if on_end is not None:
                try:
                    on_end(self._stream)
                except Exception:  # noqa: BLE001 - bookkeeping never breaks a reply
                    pass
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - said in the log, then raised as it was
            # failed after the reply began (OpenRouter's mid-answer error chunk, a cut stream): the words already
            # passed on can't be taken back, so this is not retried; the turn ends with the error
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception:  # noqa: BLE001
                    pass
            raise

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
        if self._owner.route() is None:   # another service's unsigned reasoning, if any, taken out for Claude
            return await self._default().create(**strip_unsigned_thinking(params))
        if params.get("stream"):
            owner = self._owner

            async def open_raw(client: Any, sent: dict[str, Any]) -> Any:
                name = owner._answering   # set by ``_run`` just before this call (read before the first await)

                held: list[Any] = []

                async def opened() -> tuple[Any, Any, Any, bool]:
                    stream = await (client.beta.messages if self._beta else client.messages).create(**sent)
                    held.append(stream)
                    it = stream.__aiter__()
                    try:
                        return stream, it, await it.__anext__(), True
                    except StopAsyncIteration:
                        return stream, it, None, False

                if owner.first_event_s is None:
                    stream, it, first, has_first = await opened()
                else:
                    try:   # a service that hangs before its first word is given up on (the voice can't wait 90 s)
                        stream, it, first, has_first = await asyncio.wait_for(opened(), owner.first_event_s)
                    except asyncio.TimeoutError as exc:
                        for late in held:   # the opened stream's connection is let go
                            try:
                                await _IterHolder(late, None).close()
                            except Exception:  # noqa: BLE001
                                pass
                        raise FirstEventTimeout(f"no first word within {owner.first_event_s:g} s") from exc
                return _Prefetched(_IterHolder(stream, it), first, has_first,
                                   on_end=lambda done: owner._note_stream(name, done),
                                   on_error=lambda exc: owner._midstream(name, exc))

            return await self._owner._run(params, open_raw)

        async def plain(client: Any, sent: dict[str, Any]) -> Any:
            return await (client.beta.messages if self._beta else client.messages).create(**sent)

        return await self._owner._run(params, plain)

    def stream(self, **params: Any) -> Any:
        if self._owner.route() is None:
            return self._default().stream(**strip_unsigned_thinking(params))
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


class FirstEventTimeout(TimeoutError):
    """A routed reply stream gave no first event within :attr:`AsyncRoleClient.first_event_s`: retried, or with a
    backup set, the backup is tried at once (a hung service rarely answers the same request a moment later)."""


#: The talker's limit for a routed reply's first event (seconds): above a cold local model's or a queued free model's
#: usual wait, far below the 90 s read limit a hung service would otherwise run into on each try.
TALKER_FIRST_EVENT_S = 15.0


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
        first_event_s: The limit for a routed reply stream's first event (``None``: only the read timeout).
    """

    def __init__(self, role: str, default: Any, *, settings: Any = None, log: Callable[..., Any] | None = None,
                 sleep: Callable[[float], Any] | None = None, factory: Callable[[str], Any] | None = None,
                 first_event_s: float | None = None) -> None:
        self.role = role
        self.first_event_s = first_event_s
        self._default = default
        self.settings = settings
        self._log = log
        self._sleep = sleep or asyncio.sleep
        self._factory = factory or (lambda provider_id: async_provider_client(provider_id, settings))
        self._clients: dict[tuple[Any, ...], Any] = {}
        self.messages = _AsyncRoleMessages(self, beta=False)
        self.beta = _AsyncBeta(self)
        self._last: str | None = None
        #: The target of the call being opened now (``provider:model``, or the Claude id for a ``default`` backup).
        self._answering: str | None = None
        #: ``(model name, usage tokens or None, returned cost or None)`` per routed raw stream read to its end since
        #: the last :meth:`take_stream_usage` (the voice prices each reply turn with them).
        self._streams: list[tuple[str, dict[str, int] | None, float | None]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._default, name)

    def route(self) -> Any:
        from yuki.agent.providers.config import route, take_note

        chosen = route(self.role)
        if chosen is None and take_note():   # ai.json is there but unreadable: on Claude because of that, said once
            self._event("ai_config_unreadable", role=self.role)
        return chosen

    def routed(self) -> bool:
        """Whether this role's calls go to another provider right now (the voice then skips its Bedrock chain)."""
        return self.route() is not None

    def last_model(self) -> str | None:
        """``provider:model`` of the last routed call (the Claude model id when the user's ``default`` backup
        answered it; ``None`` after a call that went the default way)."""
        return self._last

    def _note_stream(self, name: str | None, stream: Any) -> None:
        """A routed raw stream (the Pipecat service's reply) was read to its end: keep what it used and cost. The
        OpenAI-protocol adapter's own count is complete (cached prompt tokens included, which Pipecat reads only from
        the stream's first event); a Claude stream's count is Pipecat's (``None`` here)."""
        if not name:
            return
        from yuki.agent.providers.pricing import usage_cost_fields

        translator = getattr(stream, "translator", None)
        skipped = getattr(translator, "skipped_calls", 0)
        if isinstance(skipped, int) and skipped:
            # the reply held more tool calls than the voice runs: only the first was passed on (said in the log)
            self._event("tool_calls_skipped", role=self.role, target=name, count=skipped)
        usage = getattr(translator, "final_usage", None)
        tokens = None
        if usage is not None:
            from yuki.log.requests import usage_tokens

            tokens = usage_tokens(usage)
        self._streams = [*self._streams[-49:], (name, tokens, usage_cost_fields(usage) if usage is not None else None)]

    def _midstream(self, name: str | None, exc: BaseException) -> None:
        """A routed reply stream failed after it began (content-free: who, the status and the error's type)."""
        self._event("model_midstream_error", role=self.role, target=name, status=getattr(exc, "status_code", None),
                    error=type(exc).__name__)

    def take_stream_usage(self) -> list[tuple[str, dict[str, int] | None, float | None]]:
        """The routed raw streams read since the last call (and forget them): see :meth:`_note_stream`."""
        taken, self._streams = self._streams, []
        return taken

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
            if target.provider == "default":   # the voice's own Claude client: another service's reasoning taken out
                sent = strip_unsigned_thinking(sent)
            for attempt in range(retries + 1):
                try:
                    client = self._default if target.provider == "default" and self._default is not None \
                        else self._client(target.provider)
                    self._answering = target.name if target.provider != "default" else target.model
                    result = await call(client, sent)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified from the SDK's own types
                    last = exc
                    kind = classify(exc)
                    if isinstance(exc, FirstEventTimeout) and index + 1 < len(chain):
                        kind = "failover"   # hung: straight to the user's backup
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
                    self._last = target.name if target.provider != "default" else target.model
                    if index:
                        self._event("model_failover_ok", role=self.role, used=target.name)
                    return result
        assert last is not None
        raise last


def role_async_client(role: str, default: Any, settings: Any = None, *, log: Callable[..., Any] | None = None) -> Any:
    """The client the voice uses for ``role``: :class:`AsyncRoleClient` around its usual client (the talker's routed
    replies with a first-event limit, :data:`TALKER_FIRST_EVENT_S`)."""
    return AsyncRoleClient(role, default, settings=settings, log=log,
                           first_event_s=TALKER_FIRST_EVENT_S if role == "talker" else None)


def talker_price_model(settings: Any, client: Any = None) -> str:
    """The model to price the talker's calls with: the routed ``provider:model`` when the talker is moved, else
    ``Settings.voice_mouth_model`` (as before)."""
    from yuki.agent.providers.config import route

    r = route("talker")
    if r is None or not r.primary.model:
        return getattr(settings, "voice_mouth_model", "")
    last = client.last_model() if client is not None and callable(getattr(client, "last_model", None)) else None
    return last or r.primary.name


def price_talker_turn(settings: Any, client: Any, usage: dict[str, int]) -> float | None:
    """The cost of one voice reply turn (``usage``: the tokens Pipecat counted for it).

    Talker on ``default``: ``Settings.voice_mouth_model``'s price, as before. Talker moved: each routed reply stream
    the turn read is priced as the model that actually answered it (the user's backup included), with the adapter's
    own token count (cached prompt tokens included) or OpenRouter's returned amount; a stream with no count of its
    own (Claude) is priced from Pipecat's count. ``None`` when any price isn't known (never invented)."""
    take = getattr(client, "take_stream_usage", None)
    records = take() if callable(take) else []
    if not records:
        return settings.estimate_cost(talker_price_model(settings, client), usage)
    if any(tokens is None for _name, tokens, _returned in records):
        return settings.estimate_cost(records[-1][0], usage)
    total = 0.0
    for name, tokens, returned in records:
        cost = returned if returned is not None else settings.estimate_cost(name, tokens or {})
        if cost is None:
            return None
        total += float(cost)
    return total


__all__ = ["TALKER_FIRST_EVENT_S", "AsyncRoleClient", "FirstEventTimeout", "FittedClaudeClient", "ProviderSetupError",
           "async_provider_client", "fit_claude_params", "missing_model", "price_talker_turn", "role_async_client",
           "sync_provider_client", "talker_price_model"]
