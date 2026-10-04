"""Model failover: retry with backoff, then fail over to a configured backup (plumbing, no judgement).

When a Bedrock call fails for a reason that says nothing about the request -- throttled (429),
overloaded (529), a server error (5xx), a timeout, no connection, or the model or inference
profile not being there (404, 424) -- the same call is retried on the same target with capped
exponential backoff (``retry-after`` honoured up to the cap), then sent to the next target of the
chain configured for that model (``Settings.model_fallbacks``): the same model through another
region or inference profile, then a sibling model. A request the service rejects as such (400,
401, 403, 413, 422) is never retried or failed over: every target would reject it too.

After a failover works, calls for that model go straight to the target that worked for
``sticky_s`` (the primary is tried again after that), so every request of an outage does not pay
for the retries again.

Streaming: :meth:`_Messages.stream` opens the stream *and reads its first event* before handing
it back, so a stream that fails to start (throttling is often reported as the first event on
Bedrock) is retried and failed over like a plain call. Once an event has been handed on, a
failure is the caller's: tools may already have run on it.

Every retry and failover is logged (``log(event_type, **fields)``): ``model_retry``,
``model_failover`` (from, to, reason) and ``model_failover_ok`` / ``model_failover_exhausted``.
The target a call actually used is :meth:`FailoverClient.last_model` (per thread), so its cost
is priced with the right model.

Settings → AI (report 67): a client built for a role (``from_settings`` -> ``thinker``, ``make_client`` ->
``memory``) checks ``yuki.agent.providers.config.route(role)`` on every call (one ``stat`` of ``ai.json``). On
``default`` nothing below changes. A role moved to another provider (OpenAI, Gemini, OpenRouter, Ollama, DeepSeek,
another OpenAI-compatible service, or Anthropic with the user's key) is served by a client of this same class whose
places are ``@<provider>``: the same retries and backoff on that provider, then the user's backup if one is set;
``last_model`` is then ``provider:model``.

Usage::

    client = FailoverClient.from_settings(settings, log=logger.log)
    client.messages.create(**params)          # same surface as anthropic.AnthropicBedrock
    with client.messages.stream(**params) as stream: ...
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

#: HTTP statuses retried on the same target, then failed over.
RETRY_STATUSES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
#: HTTP statuses failed over at once (the model or profile is not there / broke), not retried.
FAILOVER_STATUSES: frozenset[int] = frozenset({404, 424})


@dataclass(frozen=True)
class Target:
    """One place a call can go: a model id through a region's endpoint."""

    model: str
    region: str

    def label(self) -> str:
        return f"{self.model}@{self.region}"


def classify(exc: BaseException) -> str:
    """``retry`` | ``failover`` | ``fatal`` for one failed call, from the SDK's own exception types.

    Parsing the structured API response (status code), never message text.
    """
    try:
        import anthropic
    except Exception:  # pragma: no cover - the SDK is a dependency
        return "fatal"
    if isinstance(exc, anthropic.APIConnectionError):   # APITimeoutError is a subclass
        return "retry"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in RETRY_STATUSES or status >= 500:
            return "retry"
        if status in FAILOVER_STATUSES:
            return "failover"
        return "fatal"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retry"
    return "fatal"


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a ``retry-after`` header on the failed response, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def adapt_params(params: dict[str, Any], model: str) -> dict[str, Any]:
    """``params`` sent to ``model`` instead of the model they were built for.

    A fixed-budget thinking config (``{"type": "enabled", "budget_tokens": N}``, what a Haiku 4.5
    call carries) is refused by Sonnet 5 / Opus 5, which take adaptive thinking: it becomes
    adaptive on another model. Everything else is sent as it was.
    """
    out = {**params, "model": model}
    thinking = params.get("thinking")
    if model != params.get("model") and isinstance(thinking, dict) and thinking.get("type") == "enabled":
        out["thinking"] = {"type": "adaptive"}
    return out


class FailoverClient:
    """A drop-in for ``anthropic.AnthropicBedrock`` (``messages.create`` / ``messages.stream``) with failover.

    Args:
        region: The primary region (the one every chain's first target uses).
        factory: ``factory(region) -> client``: builds the real client for a region (called once per
            region, lazily). Tests pass a stub.
        fallbacks: ``Settings.model_fallbacks`` (model id -> list of ``{"model", "region"}``).
        retries: Retries on one target before moving on.
        base_s / max_s: Backoff: ``base_s * 2**attempt`` plus jitter, capped at ``max_s``.
        sticky_s: How long a working backup keeps the calls of its model.
        log: ``log(event_type, **fields)`` for every retry and failover; ``None`` to not log.
        sleep: Waits between retries (``threading.Event().wait`` by default: no busy loop).
        clock: Monotonic clock (tests).
        role: The job these calls do (``thinker``, ``memory``; ``yuki.agent.providers``). ``None``: no routing.
            While the role is on ``default`` (no ``ai.json``, or the role left as it was) every call takes the
            path below, untouched. When the user moved the role to another provider in Settings → AI, the call
            goes to that provider and model instead, through a client of this same class (retries and backoff
            on that provider, then the user's backup if one is set; never a silent switch back to Claude).
        settings: The settings the routed clients take their timeouts from (``from_settings`` passes them).
    """

    def __init__(
        self,
        region: str,
        factory: Callable[[str], Any],
        *,
        fallbacks: dict[str, list[dict[str, Any]]] | None = None,
        retries: int = 2,
        base_s: float = 0.6,
        max_s: float = 4.0,
        sticky_s: float = 300.0,
        log: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        regionless: bool = False,
        role: str | None = None,
        settings: Any = None,
    ) -> None:
        #: The Anthropic API backend: one endpoint, so a region in a chain means nothing and two
        #: entries naming the same model there are one target (``yuki.core.models``).
        self.regionless = regionless
        self.role = role
        self.settings = settings
        #: (route, key file stamp, custom address, requested model) -> the routed client of that route
        self._routed: dict[tuple[Any, ...], FailoverClient] = {}
        self.region = region
        self._factory = factory
        self.fallbacks = fallbacks or {}
        self.retries = max(0, int(retries))
        self.base_s = base_s
        self.max_s = max_s
        self.sticky_s = sticky_s
        self._log = log
        self._sleep = sleep or (lambda s: threading.Event().wait(s))
        self._clock = clock
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        #: model id -> (index of the working target in its chain, until when)
        self._sticky: dict[str, tuple[int, float]] = {}
        self._local = threading.local()
        self.counters = {"calls": 0, "retries": 0, "failovers": 0, "exhausted": 0}
        self.messages = _Messages(self)

    @classmethod
    def from_settings(cls, settings: Any, *, log: Callable[..., Any] | None = None,
                      factory: Callable[[str], Any] | None = None, role: str | None = "thinker") -> "FailoverClient":
        """The client for ``settings`` (region, chains, retries, timeouts), SDK retries off (these are ours).

        ``role``: whose calls these are (the agent's: ``thinker``, the default), for Settings → AI.
        """
        import os

        region = getattr(settings, "aws_region", None) or os.environ.get("AWS_REGION", "ap-south-1")
        backend = getattr(settings, "model_backend", None) or "bedrock"
        if factory is None:
            read = float(getattr(settings, "model_timeout_s", 90.0))
            connect = float(getattr(settings, "model_connect_timeout_s", 10.0))

            def factory(r: str) -> Any:
                import anthropic

                from yuki.core.models import sync_client

                return sync_client(r, backend=backend, max_retries=0,
                                   timeout=anthropic.Timeout(read, connect=connect, write=30.0, pool=10.0))

        return cls(
            region, factory, regionless=backend == "anthropic",
            fallbacks=getattr(settings, "model_fallbacks", None) or {},
            retries=getattr(settings, "model_retries", 2), base_s=getattr(settings, "model_retry_base_s", 0.6),
            max_s=getattr(settings, "model_retry_max_s", 4.0), sticky_s=getattr(settings, "failover_sticky_s", 300.0),
            log=log, role=role, settings=settings,
        )

    # -- plumbing --------------------------------------------------------------------------

    def last_model(self) -> str | None:
        """The model id the last call on this thread actually used (after any failover)."""
        return getattr(self._local, "model", None)

    def last_target(self) -> str | None:
        """``model@region`` of the last call on this thread."""
        return getattr(self._local, "target", None)

    def client_for(self, region: str) -> Any:
        with self._lock:
            client = self._clients.get(region)
            if client is None:
                client = self._factory(region)
                self._clients[region] = client
            return client

    def chain(self, model: str) -> list[Target]:
        """The primary target, then the configured backups (duplicates dropped)."""
        out = [Target(model, self.region)]
        seen = {self._same(model)}
        for entry in self.fallbacks.get(model, ()) or ():
            if not isinstance(entry, dict) or not entry.get("model"):
                continue
            target = Target(str(entry["model"]), self.region if self.regionless
                            else str(entry.get("region") or self.region))
            key = self._same(target.model) if self.regionless else target
            if target not in out and key not in seen:
                out.append(target)
                seen.add(key)
        return out

    def _same(self, model: str) -> Any:
        if not self.regionless:
            return Target(model, self.region)
        from yuki.core.models import api_model_id

        return api_model_id(model)

    def _event(self, event_type: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event_type, **fields)
        except Exception:
            pass

    def _backoff(self, attempt: int, exc: BaseException) -> float:
        wait = min(self.max_s, self.base_s * (2 ** attempt)) * (0.8 + 0.4 * random.random())
        hinted = _retry_after(exc)
        if hinted is not None:
            wait = max(wait, min(self.max_s, hinted))
        return round(wait, 2)

    def _start_index(self, model: str, chain: list[Target]) -> int:
        with self._lock:
            sticky = self._sticky.get(model)
            if sticky is None:
                return 0
            index, until = sticky
            if self._clock() >= until or index >= len(chain):
                del self._sticky[model]
                self._event("model_failover_reset", model=model, reason="sticky time over: trying the primary again")
                return 0
            return index

    def _succeeded(self, model: str, chain: list[Target], index: int, started: float, attempts: int) -> None:
        target = chain[index]
        self._local.model, self._local.target = target.model, target.label()
        if index > 0:
            with self._lock:
                fresh = model not in self._sticky or self._sticky[model][0] != index
                self._sticky[model] = (index, self._clock() + self.sticky_s)
            if fresh or attempts:
                self._event("model_failover_ok", requested=model, used=target.label(),
                            attempts=attempts, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    # -- the call ---------------------------------------------------------------------------

    def run(self, params: dict[str, Any], call: Callable[[Any, dict[str, Any]], Any]) -> Any:
        """``call(client, params)`` on each target in turn until one works (see the module docstring)."""
        if self.role is not None:
            from yuki.agent.providers.config import route
            from yuki.agent.providers.pricing import note_routed

            chosen = route(self.role)
            if chosen is not None:
                return self._run_routed(chosen, params, call)
            note_routed(None, None)
        model = str(params.get("model") or "")
        chain = self.chain(model)
        start = self._start_index(model, chain)
        started = time.perf_counter()
        self.counters["calls"] += 1
        last: BaseException | None = None
        attempts = 0
        for index in range(start, len(chain)):
            target = chain[index]
            sent = adapt_params(params, target.model)
            for attempt in range(self.retries + 1):
                try:
                    result = call(self.client_for(target.region), sent)
                except Exception as exc:
                    kind = classify(exc)
                    last = exc
                    attempts += 1
                    reason = f"{type(exc).__name__}: {getattr(exc, 'status_code', '') or ''} {exc}".strip()[:300]
                    if kind == "fatal":
                        raise
                    if kind == "retry" and attempt < self.retries:
                        wait = self._backoff(attempt, exc)
                        self.counters["retries"] += 1
                        self._event("model_retry", target=target.label(), attempt=attempt + 1, wait_s=wait,
                                    error_kind=kind, reason=reason)
                        self._sleep(wait)
                        continue
                    if index + 1 < len(chain):
                        self.counters["failovers"] += 1
                        self._event("model_failover", requested=model, **{"from": target.label()},
                                    to=chain[index + 1].label(), error_kind=kind, reason=reason, attempts=attempt + 1)
                    break
                else:
                    self._succeeded(model, chain, index, started, attempts)
                    return result
        self.counters["exhausted"] += 1
        self._event("model_failover_exhausted", requested=model, tried=[t.label() for t in chain[start:]],
                    reason=f"{type(last).__name__}: {last}"[:300] if last else None)
        with self._lock:
            self._sticky.pop(model, None)
        assert last is not None
        raise last

    # -- Settings → AI: a role moved to another provider ------------------------------------------------------------

    def _routed_client(self, chosen: Any, requested: str) -> "FailoverClient":
        """The client of ``chosen`` (a ``yuki.agent.providers.config.Route``): its targets are ``"@<provider>"``
        places, built by ``yuki.agent.providers.client.sync_provider_client``; rebuilt when a key or the custom
        address changes."""
        from yuki.agent.providers import config, keys
        from yuki.agent.providers.client import sync_provider_client

        backup = chosen.backup
        backup_model = (backup.model or (requested if backup.provider == "default" else "")) if backup else ""
        stamp = (chosen.signature(), keys.version(), config.custom_base_url(), backup_model)
        with self._lock:
            client = self._routed.get(stamp)
        if client is not None:
            return client
        settings = self.settings

        def factory(place: str) -> Any:
            return sync_provider_client(place.lstrip("@"), settings)

        fallbacks = {}
        if backup is not None and backup_model:
            fallbacks[chosen.primary.model] = [{"model": backup_model, "region": f"@{backup.provider}"}]
        client = FailoverClient(f"@{chosen.primary.provider}", factory, fallbacks=fallbacks, retries=self.retries,
                                base_s=self.base_s, max_s=self.max_s, sticky_s=self.sticky_s, log=self._log,
                                sleep=self._sleep, clock=self._clock)
        with self._lock:
            self._routed = {k: v for k, v in self._routed.items() if k[0] != stamp[0]}
            self._routed[stamp] = client
        return client

    def _run_routed(self, chosen: Any, params: dict[str, Any], call: Callable[[Any, dict[str, Any]], Any]) -> Any:
        from yuki.agent.providers.client import missing_model
        from yuki.agent.providers.pricing import note_routed

        if not chosen.primary.model:
            raise missing_model(self.role or "")
        requested = str(params.get("model") or "")
        client = self._routed_client(chosen, requested)
        self.counters["calls"] += 1
        result = client.run({**params, "model": chosen.primary.model}, call)
        used, target = client.last_model() or chosen.primary.model, client.last_target() or ""
        provider = target.rpartition("@")[2] or chosen.primary.provider
        name = used if provider == "default" else f"{provider}:{used}"
        self._local.model, self._local.target = name, f"{used}@{provider}"
        note_routed(requested, None if provider == "default" else name)
        return result


class _Messages:
    """``client.messages``: ``create`` and ``stream`` with failover."""

    def __init__(self, owner: FailoverClient) -> None:
        self._owner = owner

    def create(self, **params: Any) -> Any:
        return self._owner.run(params, lambda client, p: client.messages.create(**p))

    def stream(self, **params: Any) -> "_FailoverStream":
        return _FailoverStream(self._owner, params)

    def count_tokens(self, **params: Any) -> Any:
        return self._owner.client_for(self._owner.region).messages.count_tokens(**params)


class _OpenedStream:
    """The underlying stream with its first event already read (iterate it; ``get_final_message``)."""

    def __init__(self, stream: Any, first: Any, rest: Iterator[Any], has_first: bool) -> None:
        self._stream = stream
        self._first = first
        self._rest = rest
        self._has_first = has_first

    def __iter__(self) -> Iterator[Any]:
        if self._has_first:
            self._has_first = False
            yield self._first
        yield from self._rest

    def get_final_message(self) -> Any:
        for _ in self:   # drain whatever the caller did not read
            pass
        return self._stream.get_final_message()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _FailoverStream:
    """Context manager: opens the stream (first event read) on the first target that works."""

    def __init__(self, owner: FailoverClient, params: dict[str, Any]) -> None:
        self._owner = owner
        self._params = params
        self._manager: Any = None

    def __enter__(self) -> _OpenedStream:
        def open_one(client: Any, params: dict[str, Any]) -> _OpenedStream:
            manager = client.messages.stream(**params)
            stream = manager.__enter__()
            try:
                events = iter(stream)
                try:
                    first, has_first = next(events), True
                except StopIteration:
                    first, has_first = None, False
            except BaseException as exc:
                try:
                    manager.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
                raise
            self._manager = manager
            return _OpenedStream(stream, first, events, has_first)

        return self._owner.run(self._params, open_one)

    def __exit__(self, *exc: Any) -> Any:
        if self._manager is not None:
            return self._manager.__exit__(*exc)
        return None


def make_client(settings: Any = None, *, log: Callable[..., Any] | None = None,
                role: str | None = "memory") -> FailoverClient:
    """A failover Bedrock client for ``settings`` (default :class:`yuki.config.Settings`).

    The one-line change for any worker that builds ``anthropic.AnthropicBedrock(aws_region=...)``
    itself (the memory workers): ``self._client = make_client(settings, log=self._log)``. ``role``: whose calls
    these are for Settings → AI (the memory passes, ``memory``, by default).
    """
    if settings is None:
        from yuki.config import Settings

        settings = Settings()
    return FailoverClient.from_settings(settings, log=log, role=role)
