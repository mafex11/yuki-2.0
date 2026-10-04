"""The Anthropic Messages surface Yuki uses, spoken over the OpenAI chat-completions protocol.

Every model call in Yuki is written against the Anthropic SDK (``client.messages.create`` / ``.stream``,
``client.beta.messages.create(stream=True)`` for the voice's Pipecat service). :class:`OpenAICompatClient` and
:class:`AsyncOpenAICompatClient` take exactly those calls and send them to any OpenAI-protocol service (OpenAI,
Google Gemini's OpenAI-compatible endpoint, OpenRouter, Ollama's ``/v1``, DeepSeek, any other compatible base
URL), and hand back the Anthropic SDK's own types, so no caller changes:

* **request**: ``system`` (a string or text blocks) becomes the system message; ``messages`` with ``text``,
  ``image``, ``tool_use`` and ``tool_result`` blocks become chat messages, ``tool_calls`` and ``tool`` messages;
  ``tools`` (``input_schema``) become functions; ``tool_choice`` maps (``any`` -> ``required``);
  ``stop_sequences`` -> ``stop``. Anthropic-only parts are dropped: cache breakpoints (these services cache by
  themselves or not at all), ``thinking``, ``output_config``, ``betas``, ``metadata``. A model marked as a
  reasoning model in the catalog gets ``reasoning_effort`` from the request's effort. Images go only to a service
  and model that take them; otherwise a short note says an image was left out.
* **response**: an ``anthropic.types.Message`` (text, ``tool_use`` blocks with parsed input, and the model's
  reasoning as a ``thinking`` block when the request asked for thinking); ``finish_reason`` -> ``stop_reason``
  (``tool_calls`` -> ``tool_use``, ``length`` -> ``max_tokens``, ``content_filter`` -> ``refusal``); usage with
  cached prompt tokens as ``cache_read_input_tokens`` and OpenRouter's returned cost as ``provider_cost_usd``.
* **stream**: the chunks become the Anthropic raw events in order (``message_start``, ``content_block_start`` /
  ``_delta`` / ``_stop`` for each text, thinking or tool block, ``message_delta`` with the stop reason and usage,
  ``message_stop``). A tool call's arguments split over many chunks stream as ``input_json_delta`` pieces; parallel
  tool calls become one ``tool_use`` block each. ``messages.stream()`` adds the SDK's helper events (``text``,
  ``input_json`` with the partial input, ``content_block_stop`` carrying the block) and ``get_final_message()``.
* **errors**: an HTTP error becomes the Anthropic SDK's own exception for that status (401
  ``AuthenticationError``, 429 ``RateLimitError``, 5xx ``InternalServerError`` ...), with the service's own reason
  in plain words, so ``yuki.agent.failover`` retries and fails over exactly as it does for Bedrock. No connection
  is ``APIConnectionError``; a stream that ends before its end is one too. No key is ever in a message.

Plumbing only: no judgement, nothing decided on what anything says.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Iterator

from yuki.agent.providers.catalog import ModelInfo, Provider

#: How the request's effort maps to ``reasoning_effort`` on a reasoning model.
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

_FINISH = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use",
           "content_filter": "refusal"}

#: Services documented to take ``parallel_tool_calls`` (others may refuse an unknown field, so it isn't sent there).
PARALLEL_FLAG = frozenset({"openai", "openrouter"})

IMAGE_LEFT_OUT ="[An image was here, but this model can't see images.]"
DOCUMENT_LEFT_OUT = "[A document was attached here, but this model can't read attachments.]"


# -- helpers ---------------------------------------------------------------------------------------------------------


def _plain(block: Any) -> dict[str, Any]:
    """A content block as a dict (SDK objects included)."""
    if isinstance(block, dict):
        return block
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    return {"type": getattr(block, "type", None)}


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system or []:
        b = _plain(block)
        if b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
    return "\n\n".join(parts)


def _image_url(block: dict[str, Any]) -> str | None:
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        return f"data:{source.get('media_type') or 'image/png'};base64,{source['data']}"
    if source.get("type") == "url" and source.get("url"):
        return str(source["url"])
    return None


def _tool_result_text(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """A tool result's text, and its image blocks (sent after the tool messages when the model takes images)."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for item in content:
        b = _plain(item)
        if b.get("type") == "text":
            texts.append(str(b.get("text") or ""))
        elif b.get("type") == "image":
            images.append(b)
    return "\n".join(texts), images


def parse_arguments(raw: Any) -> Any:
    """A tool call's ``arguments`` (a JSON string) as the input dict. Invalid JSON is kept under
    ``_unparsed_arguments`` so the tool reports a bad input and the model can try again (nothing is guessed)."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {"_unparsed_arguments": text}
    return value if isinstance(value, dict) else {"_value": value}


# -- request ---------------------------------------------------------------------------------------------------------


def to_openai_request(params: dict[str, Any], provider: Provider, model_info: ModelInfo | None = None,
                      *, stream: bool = False) -> dict[str, Any]:
    """The chat-completions body for Anthropic ``params`` (see the module docstring)."""
    images_ok = bool(provider.images) and (model_info is None or model_info.images is not False)
    out: list[dict[str, Any]] = []
    system = _system_text(params.get("system"))
    if system:
        out.append({"role": "system", "content": system})
    for message in params.get("messages") or []:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "user")
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts, calls = [], []
            for block in content or []:
                b = _plain(block)
                kind = b.get("type")
                if kind == "text":
                    texts.append(str(b.get("text") or ""))
                elif kind == "tool_use":
                    calls.append({"id": str(b.get("id") or _new_id("call_")), "type": "function",
                                  "function": {"name": str(b.get("name") or ""),
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
                # thinking / redacted_thinking: another provider's reasoning is not sent back
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(texts) if texts else (None if calls else "")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        tool_messages: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        for block in content or []:
            b = _plain(block)
            kind = b.get("type")
            if kind == "tool_result":
                text, images = _tool_result_text(b.get("content"))
                if b.get("is_error"):
                    text = f"(the tool reported an error) {text}".strip()
                tool_messages.append({"role": "tool", "tool_call_id": str(b.get("tool_use_id") or ""),
                                      "content": text or "(no output)"})
                for image in images:
                    url = _image_url(image) if images_ok else None
                    parts.append({"type": "image_url", "image_url": {"url": url}} if url
                                 else {"type": "text", "text": IMAGE_LEFT_OUT})
            elif kind == "text":
                parts.append({"type": "text", "text": str(b.get("text") or "")})
            elif kind == "image":
                url = _image_url(b) if images_ok else None
                parts.append({"type": "image_url", "image_url": {"url": url}} if url
                             else {"type": "text", "text": IMAGE_LEFT_OUT})
            elif kind == "document":
                parts.append({"type": "text", "text": DOCUMENT_LEFT_OUT})
        out.extend(tool_messages)
        if parts:
            if all(p["type"] == "text" for p in parts):
                out.append({"role": role, "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": role, "content": parts})
    body: dict[str, Any] = {"model": params.get("model"), "messages": out}
    limit = params.get("max_tokens")
    if limit:
        body["max_completion_tokens" if provider.id == "openai" else "max_tokens"] = int(limit)
    for key in ("temperature", "top_p"):
        if params.get(key) is not None:
            body[key] = params[key]
    if params.get("stop_sequences"):
        body["stop"] = list(params["stop_sequences"])
    tools = []
    for tool in params.get("tools") or []:
        t = _plain(tool)
        if "input_schema" not in t:   # a server-side tool of Anthropic's: nothing to map it to
            continue
        fn: dict[str, Any] = {"name": t.get("name"), "parameters": t.get("input_schema") or {"type": "object"}}
        if t.get("description"):
            fn["description"] = t["description"]
        tools.append({"type": "function", "function": fn})
    if tools:
        body["tools"] = tools
        choice = params.get("tool_choice")
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind == "any":
                body["tool_choice"] = "required"
            elif kind == "tool" and choice.get("name"):
                body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
            elif kind == "none":
                body["tool_choice"] = "none"
            if choice.get("disable_parallel_tool_use") and provider.id in PARALLEL_FLAG:
                body["parallel_tool_calls"] = False
    if model_info is not None and model_info.reasoning:
        thinking = params.get("thinking") or {}
        effort = ((params.get("output_config") or {}).get("effort") or "medium")
        off = isinstance(thinking, dict) and thinking.get("type") == "disabled"
        body["reasoning_effort"] = "minimal" if off and provider.id == "openai" else _EFFORT.get(effort, "medium")
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    if provider.id == "openrouter":
        body["usage"] = {"include": True}   # OpenRouter returns what the call cost
    return body


def wants_thinking(params: dict[str, Any]) -> bool:
    thinking = params.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive")


# -- response --------------------------------------------------------------------------------------------------------


def usage_from(raw: Any) -> Any:
    """An ``anthropic.types.Usage`` from an OpenAI-protocol ``usage``: uncached input, cached input, output, and
    OpenRouter's returned cost as ``provider_cost_usd`` (``None`` when the service didn't say)."""
    from anthropic.types import Usage

    u = raw if isinstance(raw, dict) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    output = int(u.get("completion_tokens") or 0)
    details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    cached = int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    cost = u.get("cost")
    return Usage(input_tokens=max(0, prompt - cached), output_tokens=output, cache_read_input_tokens=cached,
                 cache_creation_input_tokens=0,
                 provider_cost_usd=float(cost) if isinstance(cost, (int, float)) else None)


def stop_reason(finish: Any, has_tool_calls: bool) -> str:
    if has_tool_calls and finish in (None, "stop", "tool_calls", "function_call"):
        return "tool_use"   # Gemini and Ollama say "stop" on a turn that calls tools
    return _FINISH.get(str(finish or "stop"), "end_turn")


def from_openai_response(data: dict[str, Any], *, model: str, thinking: bool = False) -> Any:
    """An ``anthropic.types.Message`` for one chat-completions response."""
    from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock

    choices = data.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    msg = choice.get("message") or {}
    content: list[Any] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if thinking and isinstance(reasoning, str) and reasoning.strip():
        content.append(ThinkingBlock(type="thinking", thinking=reasoning, signature=""))
    text = msg.get("content")
    if isinstance(text, list):   # a few services answer with content parts
        text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
    if text:
        content.append(TextBlock(type="text", text=str(text)))
    calls = msg.get("tool_calls") or []
    for call in calls:
        fn = call.get("function") or {}
        content.append(ToolUseBlock(type="tool_use", id=str(call.get("id") or _new_id("toolu_")),
                                    name=str(fn.get("name") or ""), input=parse_arguments(fn.get("arguments"))))
    return Message(id=str(data.get("id") or _new_id("msg_")), type="message", role="assistant", model=model,
                   content=content, stop_reason=stop_reason(choice.get("finish_reason"), bool(calls)),
                   stop_sequence=None, usage=usage_from(data.get("usage")))


# -- stream ----------------------------------------------------------------------------------------------------------


class StreamTranslator:
    """OpenAI-protocol stream chunks in, Anthropic raw stream events out (see the module docstring).

    ``feed(chunk)`` -> events; ``finish()`` -> the closing events (raises when the stream never said it ended).
    A tool call's block opens once its name is known and closes when the next block (or the end) begins: services
    send parallel calls one after another (OpenAI, OpenRouter) or each call whole in one chunk (Gemini, Ollama).
    """

    def __init__(self, model: str, *, thinking: bool = False) -> None:
        self.model = model
        self.thinking = thinking
        self.started = False
        self.index = -1               # the Anthropic index of the open block (-1: none)
        self.open_kind: str | None = None
        self.open_tool: Any = None    # the OpenAI tool index of the open tool block
        self.tools: dict[Any, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.done = False
        self.message_id = _new_id("msg_")

    def _start(self, events: list[Any]) -> None:
        from anthropic.types import Message, RawMessageStartEvent, Usage

        if self.started:
            return
        self.started = True
        events.append(RawMessageStartEvent(type="message_start", message=Message(
            id=self.message_id, type="message", role="assistant", model=self.model, content=[], stop_reason=None,
            stop_sequence=None, usage=Usage(input_tokens=0, output_tokens=0))))

    def _close(self, events: list[Any]) -> None:
        from anthropic.types import RawContentBlockStopEvent

        if self.open_kind is None:
            return
        events.append(RawContentBlockStopEvent(type="content_block_stop", index=self.index))
        if self.open_kind == "tool_use" and self.open_tool in self.tools:
            self.tools[self.open_tool]["closed"] = True
        self.open_kind, self.open_tool = None, None

    def _open(self, events: list[Any], kind: str, block: Any, tool: Any = None) -> None:
        from anthropic.types import RawContentBlockStartEvent

        self._close(events)
        self.index += 1
        self.open_kind, self.open_tool = kind, tool
        events.append(RawContentBlockStartEvent(type="content_block_start", index=self.index, content_block=block))

    def feed(self, chunk: dict[str, Any]) -> list[Any]:
        from anthropic.types import (
            InputJSONDelta, RawContentBlockDeltaEvent, TextBlock, TextDelta, ThinkingBlock, ThinkingDelta,
            ToolUseBlock,
        )

        events: list[Any] = []
        if chunk.get("id") and not self.started:
            self.message_id = str(chunk["id"])
        self._start(events)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or int(choice.get("index") or 0) != 0:
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if self.thinking and isinstance(reasoning, str) and reasoning:
                if self.open_kind != "thinking":
                    self._open(events, "thinking", ThinkingBlock(type="thinking", thinking="", signature=""))
                events.append(RawContentBlockDeltaEvent(type="content_block_delta", index=self.index,
                                                        delta=ThinkingDelta(type="thinking_delta", thinking=reasoning)))
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self.open_kind != "text":
                    self._open(events, "text", TextBlock(type="text", text=""))
                events.append(RawContentBlockDeltaEvent(type="content_block_delta", index=self.index,
                                                        delta=TextDelta(type="text_delta", text=text)))
            for position, call in enumerate(delta.get("tool_calls") or []):
                key = call.get("index", position)
                state = self.tools.setdefault(key, {"id": None, "name": None, "args": "", "pending": "",
                                                    "opened": False, "closed": False})
                fn = call.get("function") or {}
                if call.get("id"):
                    state["id"] = str(call["id"])
                if fn.get("name") and not state["opened"]:
                    state["name"] = str(fn["name"])   # sent whole, with the call's first chunk
                piece = fn.get("arguments")
                if isinstance(piece, dict):
                    piece = json.dumps(piece, ensure_ascii=False)
                piece = piece or ""
                state["args"] += piece
                if not state["opened"]:
                    if not state["name"]:
                        state["pending"] += piece   # the name comes first in every service seen; wait for it
                        continue
                    state["opened"] = True
                    state["id"] = state["id"] or _new_id("toolu_")
                    self._open(events, "tool_use", ToolUseBlock(type="tool_use", id=state["id"],
                                                                name=state["name"], input={}), tool=key)
                    piece = state["pending"] + piece
                    state["pending"] = ""
                if piece and not state["closed"] and self.open_tool == key:
                    events.append(RawContentBlockDeltaEvent(
                        type="content_block_delta", index=self.index,
                        delta=InputJSONDelta(type="input_json_delta", partial_json=piece)))
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
        return events

    def finish(self, *, ended: bool) -> list[Any]:
        """The closing events. ``ended``: the stream said it was over (``[DONE]`` or a finish reason)."""
        from anthropic.types import MessageDeltaUsage, RawMessageDeltaEvent, RawMessageStopEvent
        from anthropic.types.raw_message_delta_event import Delta

        if not ended and self.finish_reason is None:
            raise StreamCutError("the answer stopped halfway (the connection closed before it ended)")
        events: list[Any] = []
        self._start(events)
        self._close(events)
        usage = usage_from(self.usage)
        has_tools = any(s["opened"] for s in self.tools.values())
        events.append(RawMessageDeltaEvent(
            type="message_delta", delta=Delta(stop_reason=stop_reason(self.finish_reason, has_tools), stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=usage.output_tokens, input_tokens=usage.input_tokens,
                                    cache_read_input_tokens=usage.cache_read_input_tokens,
                                    cache_creation_input_tokens=0)))
        events.append(RawMessageStopEvent(type="message_stop"))
        self.done = True
        self.final_usage = usage
        return events


class StreamCutError(Exception):
    """A stream that closed before it said it had ended (turned into ``APIConnectionError`` for the callers)."""


def sse_data(line: str) -> str | None:
    """The payload of one server-sent-events line (``data: ...``), else ``None`` (comments, keep-alives)."""
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def note_cost(usage: Any) -> None:
    """Tell the cost estimate what the service itself said this call cost (OpenRouter), on this thread."""
    from yuki.agent.providers.pricing import note_returned, usage_cost_fields
    from yuki.log.requests import usage_tokens

    try:
        note_returned(usage_cost_fields(usage), usage_tokens(usage))
    except Exception:  # noqa: BLE001 - accounting never breaks a call
        pass


# -- errors ----------------------------------------------------------------------------------------------------------


def _reason(body: Any) -> str:
    """The service's own words for an error body (OpenAI, Gemini, OpenRouter and Ollama shapes)."""
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error") or ""
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            raw = meta.get("raw")
            if isinstance(raw, str) and raw and raw not in str(msg):
                msg = f"{msg} ({raw[:200]})" if msg else raw[:200]
            return str(msg)
        if isinstance(err, str):
            return err
    if isinstance(body, str):
        return body[:300]
    return ""


_PLAIN_STATUS = {
    400: "it didn't accept the request",
    401: "the key was refused (it may be wrong or expired)",
    402: "the account has no credit left",
    403: "the key isn't allowed to use this",
    404: "it doesn't know that model",
    408: "it took too long",
    413: "the request was too big for it",
    422: "it didn't accept the request",
    429: "too many requests right now, or the free limit is used up",
}


def plain_status(status: int) -> str:
    if status in _PLAIN_STATUS:
        return _PLAIN_STATUS[status]
    if status >= 500:
        return "it's having trouble on its side right now"
    return f"it answered with an error ({status})"


def status_error(provider: Provider, status: int, body: Any, request: Any, headers: Any = None,
                 key: str | None = None) -> Exception:
    """The Anthropic SDK exception for ``status`` with the service's reason in plain words (no key in it)."""
    import anthropic
    import httpx2

    from yuki.agent.providers.keys import redact

    classes: dict[int, type[anthropic.APIStatusError]] = {
        400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError, 409: anthropic.ConflictError, 413: anthropic.RequestTooLargeError,
        422: anthropic.UnprocessableEntityError, 429: anthropic.RateLimitError,
    }
    cls = classes.get(status) or (anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError)
    reason = redact(_reason(body), key)
    message = f"{provider.label}: {plain_status(status)}" + (f". It said: {reason}" if reason else "")
    response = httpx2.Response(status, headers=dict(headers or {}), request=request)
    return cls(message, response=response, body=body)


def connection_error(provider: Provider, base_url: str, request: Any, exc: BaseException) -> Exception:
    import anthropic
    import httpx2

    if isinstance(exc, httpx2.TimeoutException) and not isinstance(exc, httpx2.ConnectTimeout):
        return anthropic.APITimeoutError(request=request)   # it answered too slowly (a closed port is below)
    where = base_url.split("/v1")[0] if base_url else provider.label
    hint = " Is it running?" if provider.id == "ollama" else " Check the internet connection."
    return anthropic.APIConnectionError(message=f"Couldn't reach {provider.label} at {where}.{hint}",
                                        request=request)


# -- the clients -----------------------------------------------------------------------------------------------------


class _Base:
    """What the sync and async clients share: the provider, its address, its key, its model facts."""

    def __init__(self, provider: Provider, *, base_url: str = "", api_key: str | None = None,
                 model_info: Callable[[str], ModelInfo | None] | None = None, timeout: float = 90.0,
                 connect_timeout: float = 10.0, transport: Any = None) -> None:
        self.provider = provider
        self.base_url = (base_url or provider.base_url).rstrip("/")
        self._key = api_key
        self._model_info = model_info or (lambda _model: None)
        self._timeout = (timeout, connect_timeout)
        self._transport = transport
        self.model_backend = provider.id
        self.prompt_caching = provider.caching

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if self.provider.id == "openrouter":
            headers["X-Title"] = "Yuki"
        return headers

    def _body(self, params: dict[str, Any], stream: bool) -> dict[str, Any]:
        return to_openai_request(params, self.provider, self._model_info(str(params.get("model") or "")),
                                 stream=stream)

    def _httpx_timeout(self) -> Any:
        import httpx2

        read, connect = self._timeout
        return httpx2.Timeout(read, connect=connect, write=30.0, pool=10.0)

    def _check_body(self, data: Any, request: Any) -> None:
        """An error carried in a 200 body (OpenRouter puts upstream failures there)."""
        if isinstance(data, dict) and isinstance(data.get("error"), dict) and not data.get("choices"):
            code = data["error"].get("code")
            status = code if isinstance(code, int) and 400 <= code < 600 else 502
            raise status_error(self.provider, status, data, request, key=self._key)


class OpenAICompatClient(_Base):
    """A sync client with the Anthropic surface (``messages.create`` / ``.stream``, ``beta.messages``)."""

    def __init__(self, provider: Provider, **kwargs: Any) -> None:
        super().__init__(provider, **kwargs)
        self._http: Any = None
        self.messages = _SyncMessages(self)
        self.beta = _Beta(self.messages)

    def http(self) -> Any:
        if self._http is None:
            import httpx2

            options: dict[str, Any] = {"timeout": self._httpx_timeout()}
            if self._transport is not None:
                options["transport"] = self._transport
            self._http = httpx2.Client(**options)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx2

        client = self.http()
        request = client.build_request("POST", self._url(), json=body, headers=self._headers())
        try:
            response = client.send(request)
        except httpx2.HTTPError as exc:
            raise connection_error(self.provider, self.base_url, request, exc) from exc
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code >= 400:
            raise status_error(self.provider, response.status_code, data, request, response.headers, self._key)
        self._check_body(data, request)
        if not isinstance(data, dict):
            raise status_error(self.provider, 502, "the answer wasn't readable", request, key=self._key)
        return data

    def open_stream(self, body: dict[str, Any]) -> tuple[Any, Iterator[dict[str, Any]], Any]:
        """``(response, chunks, request)``: the stream opened (its status checked), chunks parsed lazily."""
        import httpx2

        client = self.http()
        request = client.build_request("POST", self._url(), json=body, headers=self._headers())
        try:
            response = client.send(request, stream=True)
        except httpx2.HTTPError as exc:
            raise connection_error(self.provider, self.base_url, request, exc) from exc
        if response.status_code >= 400:
            try:
                response.read()
                data = response.json()
            except Exception:  # noqa: BLE001 - the status says enough
                data = None
            finally:
                response.close()
            raise status_error(self.provider, response.status_code, data, request, response.headers, self._key)

        def chunks() -> Iterator[dict[str, Any]]:
            try:
                for line in response.iter_lines():
                    payload = sse_data(line)
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        yield {"__done__": True}
                        return
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    self._check_body(data, request)
                    yield data
            except httpx2.HTTPError as exc:
                raise connection_error(self.provider, self.base_url, request, exc) from exc
            finally:
                response.close()

        return response, chunks(), request


class _Beta:
    def __init__(self, messages: Any) -> None:
        self.messages = messages


class _SyncMessages:
    def __init__(self, owner: OpenAICompatClient) -> None:
        self._owner = owner

    def create(self, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if k not in ("betas", "extra_headers", "extra_body")}
        if params.pop("stream", False):
            return _SyncRawStream(self._owner, params)
        data = self._owner.post(self._owner._body(params, False))
        message = from_openai_response(data, model=str(params.get("model") or ""), thinking=wants_thinking(params))
        note_cost(message.usage)
        return message

    def stream(self, **params: Any) -> "_SyncStreamManager":
        params = {k: v for k, v in params.items() if k not in ("betas", "extra_headers", "extra_body", "stream")}
        return _SyncStreamManager(self._owner, params)

    def count_tokens(self, **_params: Any) -> Any:
        raise NotImplementedError(f"{self._owner.provider.label} can't count tokens ahead of a call")


def _raw_events(owner: Any, params: dict[str, Any], chunks: Iterator[dict[str, Any]], request: Any,
                translator: StreamTranslator) -> Iterator[Any]:
    """The Anthropic raw events of one opened stream (the translator's ``final_usage`` set at the end)."""
    import anthropic

    ended = False
    try:
        for chunk in chunks:
            if chunk.get("__done__"):
                ended = True
                break
            yield from translator.feed(chunk)
        closing = translator.finish(ended=ended)
        note_cost(translator.final_usage)
        yield from closing
    except StreamCutError as exc:
        raise anthropic.APIConnectionError(message=f"{owner.provider.label}: {exc}", request=request) from exc


class _SyncRawStream:
    """``create(stream=True)``: an iterable of raw events (also a context manager, like the SDK's ``Stream``)."""

    def __init__(self, owner: OpenAICompatClient, params: dict[str, Any]) -> None:
        self.response, chunks, request = owner.open_stream(owner._body(params, True))
        self.translator = StreamTranslator(str(params.get("model") or ""), thinking=wants_thinking(params))
        self._events = _raw_events(owner, params, chunks, request, self.translator)

    def __iter__(self) -> Iterator[Any]:
        return self._events

    def __next__(self) -> Any:
        return next(self._events)

    def close(self) -> None:
        try:
            self.response.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "_SyncRawStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class _SyncMessageStream:
    """``messages.stream()`` once entered: the SDK's helper events, ``get_final_message()``, ``text_stream``."""

    def __init__(self, raw: _SyncRawStream) -> None:
        self._raw = raw
        self._snapshot: Any = None
        self._json: dict[int, bytes] = {}
        self._iter = self._events()

    def _events(self) -> Iterator[Any]:
        from anthropic.lib.streaming._messages import accumulate_event, build_events

        for event in self._raw:
            self._snapshot = accumulate_event(event=event, current_snapshot=self._snapshot, json_bufs=self._json)
            yield from build_events(event=event, message_snapshot=self._snapshot)

    def __iter__(self) -> Iterator[Any]:
        return self._iter

    def __next__(self) -> Any:
        return next(self._iter)

    @property
    def text_stream(self) -> Iterator[str]:
        for event in self:
            if getattr(event, "type", None) == "text":
                yield event.text

    def until_done(self) -> None:
        for _ in self:
            pass

    def get_final_message(self) -> Any:
        self.until_done()
        final = self._snapshot
        usage = getattr(self._raw.translator, "final_usage", None)
        if final is not None and usage is not None:
            final.usage = usage   # the provider's own cost travels with it
        return final

    def get_final_text(self) -> str:
        message = self.get_final_message()
        return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) if getattr(b, "type", "") == "text")

    def close(self) -> None:
        self._raw.close()

    @property
    def current_message_snapshot(self) -> Any:
        return self._snapshot

    @property
    def response(self) -> Any:
        return self._raw.response


class _SyncStreamManager:
    def __init__(self, owner: OpenAICompatClient, params: dict[str, Any]) -> None:
        self._owner = owner
        self._params = params
        self._stream: _SyncMessageStream | None = None

    def __enter__(self) -> _SyncMessageStream:
        self._stream = _SyncMessageStream(_SyncRawStream(self._owner, self._params))
        return self._stream

    def __exit__(self, *exc: Any) -> None:
        if self._stream is not None:
            self._stream.close()


# -- async -------------------------------------------------------------------------------------------------------------


class AsyncOpenAICompatClient(_Base):
    """The async twin (the voice's Pipecat service calls ``await client.beta.messages.create(stream=True)``)."""

    def __init__(self, provider: Provider, **kwargs: Any) -> None:
        super().__init__(provider, **kwargs)
        self._http: Any = None
        self.messages = _AsyncMessages(self)
        self.beta = _Beta(self.messages)

    def http(self) -> Any:
        if self._http is None:
            import httpx2

            options: dict[str, Any] = {"timeout": self._httpx_timeout(),
                                       "limits": httpx2.Limits(max_connections=10, max_keepalive_connections=4,
                                                               keepalive_expiry=300.0)}
            if self._transport is not None:
                options["transport"] = self._transport
            self._http = httpx2.AsyncClient(**options)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx2

        client = self.http()
        request = client.build_request("POST", self._url(), json=body, headers=self._headers())
        try:
            response = await client.send(request)
        except httpx2.HTTPError as exc:
            raise connection_error(self.provider, self.base_url, request, exc) from exc
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code >= 400:
            raise status_error(self.provider, response.status_code, data, request, response.headers, self._key)
        self._check_body(data, request)
        if not isinstance(data, dict):
            raise status_error(self.provider, 502, "the answer wasn't readable", request, key=self._key)
        return data

    async def open_stream(self, body: dict[str, Any]) -> tuple[Any, Any, Any]:
        import httpx2

        client = self.http()
        request = client.build_request("POST", self._url(), json=body, headers=self._headers())
        try:
            response = await client.send(request, stream=True)
        except httpx2.HTTPError as exc:
            raise connection_error(self.provider, self.base_url, request, exc) from exc
        if response.status_code >= 400:
            try:
                await response.aread()
                data = response.json()
            except Exception:  # noqa: BLE001
                data = None
            finally:
                await response.aclose()
            raise status_error(self.provider, response.status_code, data, request, response.headers, self._key)

        async def chunks() -> Any:
            try:
                async for line in response.aiter_lines():
                    payload = sse_data(line)
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        yield {"__done__": True}
                        return
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    self._check_body(data, request)
                    yield data
            except httpx2.HTTPError as exc:
                raise connection_error(self.provider, self.base_url, request, exc) from exc
            finally:
                await response.aclose()

        return response, chunks(), request


class _AsyncMessages:
    def __init__(self, owner: AsyncOpenAICompatClient) -> None:
        self._owner = owner

    async def create(self, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if k not in ("betas", "extra_headers", "extra_body")}
        if params.pop("stream", False):
            stream = _AsyncRawStream(self._owner, params)
            await stream.open()
            return stream
        data = await self._owner.post(self._owner._body(params, False))
        return from_openai_response(data, model=str(params.get("model") or ""), thinking=wants_thinking(params))

    def stream(self, **params: Any) -> "_AsyncStreamManager":
        params = {k: v for k, v in params.items() if k not in ("betas", "extra_headers", "extra_body", "stream")}
        return _AsyncStreamManager(self._owner, params)

    async def count_tokens(self, **_params: Any) -> Any:
        raise NotImplementedError(f"{self._owner.provider.label} can't count tokens ahead of a call")


class _AsyncRawStream:
    """``await create(stream=True)``: an async iterable of raw events (``async for event in stream``)."""

    def __init__(self, owner: AsyncOpenAICompatClient, params: dict[str, Any]) -> None:
        self._owner = owner
        self._params = params
        self.response: Any = None
        self.translator = StreamTranslator(str(params.get("model") or ""), thinking=wants_thinking(params))
        self._gen: Any = None

    async def open(self) -> None:
        import anthropic

        self.response, chunks, request = await self._owner.open_stream(self._owner._body(self._params, True))
        owner, translator = self._owner, self.translator

        async def events() -> Any:
            ended = False
            try:
                async for chunk in chunks:
                    if chunk.get("__done__"):
                        ended = True
                        break
                    for event in translator.feed(chunk):
                        yield event
                for event in translator.finish(ended=ended):
                    yield event
            except StreamCutError as exc:
                raise anthropic.APIConnectionError(message=f"{owner.provider.label}: {exc}",
                                                   request=request) from exc

        self._gen = events()

    def __aiter__(self) -> Any:
        return self._gen

    async def __anext__(self) -> Any:
        return await self._gen.__anext__()

    async def close(self) -> None:
        try:
            if self.response is not None:
                await self.response.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def __aenter__(self) -> "_AsyncRawStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class _AsyncMessageStream:
    def __init__(self, raw: _AsyncRawStream) -> None:
        self._raw = raw
        self._snapshot: Any = None
        self._json: dict[int, bytes] = {}
        self._gen = self._events()

    async def _events(self) -> Any:
        from anthropic.lib.streaming._messages import accumulate_event, build_events

        async for event in self._raw:
            self._snapshot = accumulate_event(event=event, current_snapshot=self._snapshot, json_bufs=self._json)
            for out in build_events(event=event, message_snapshot=self._snapshot):
                yield out

    def __aiter__(self) -> Any:
        return self._gen

    async def __anext__(self) -> Any:
        return await self._gen.__anext__()

    @property
    async def text_stream(self) -> Any:   # pragma: no cover - parity with the SDK
        async for event in self:
            if getattr(event, "type", None) == "text":
                yield event.text

    async def until_done(self) -> None:
        async for _ in self:
            pass

    async def get_final_message(self) -> Any:
        await self.until_done()
        final = self._snapshot
        usage = getattr(self._raw.translator, "final_usage", None)
        if final is not None and usage is not None:
            final.usage = usage
        return final

    async def get_final_text(self) -> str:
        message = await self.get_final_message()
        return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) if getattr(b, "type", "") == "text")

    async def close(self) -> None:
        await self._raw.close()

    @property
    def current_message_snapshot(self) -> Any:
        return self._snapshot


class _AsyncStreamManager:
    def __init__(self, owner: AsyncOpenAICompatClient, params: dict[str, Any]) -> None:
        self._owner = owner
        self._params = params
        self._stream: _AsyncMessageStream | None = None

    async def __aenter__(self) -> _AsyncMessageStream:
        raw = _AsyncRawStream(self._owner, self._params)
        await raw.open()
        self._stream = _AsyncMessageStream(raw)
        return self._stream

    async def __aexit__(self, *exc: Any) -> None:
        if self._stream is not None:
            await self._stream.close()


__all__ = ["AsyncOpenAICompatClient", "DOCUMENT_LEFT_OUT", "IMAGE_LEFT_OUT", "OpenAICompatClient", "StreamCutError",
           "StreamTranslator", "from_openai_response", "parse_arguments", "plain_status", "sse_data", "status_error",
           "stop_reason", "to_openai_request", "usage_from", "wants_thinking"]
