"""Bring your own AI (report 67): the OpenAI-protocol adapter, the per-role routing, keys, pricing, discovery.

Offline only: a fake OpenAI-protocol server on 127.0.0.1 (tests/_fake_openai.py) with scripted answers, stub
Claude clients, ``ai.json`` and the DPAPI key file in a temp folder (conftest's ``YUKI_AI_DIR`` plus each test's
own). No paid call, no network, nothing on the desktop. One live smoke test runs only if Ollama happens to be
running on this PC (skipped otherwise; nothing is installed or started).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _fake_openai import FakeOpenAI, Reply, chunk, completion, tool_call, usage_chunk  # noqa: E402

from yuki.agent.failover import FailoverClient, classify, make_client  # noqa: E402
from yuki.agent.providers import config as ai_config  # noqa: E402
from yuki.agent.providers import keys as ai_keys  # noqa: E402
from yuki.agent.providers.catalog import PROVIDERS, ModelInfo, known_model, split_qualified  # noqa: E402
from yuki.agent.providers.client import AsyncRoleClient, ProviderSetupError  # noqa: E402
from yuki.agent.providers.openai_compat import (  # noqa: E402
    IMAGE_LEFT_OUT, AsyncOpenAICompatClient, OpenAICompatClient, from_openai_response, to_openai_request,
)
from yuki.config import Settings  # noqa: E402

KEY = "sk-test-InventedKeyForTests0123456789abcd"   # invented; never a real key


@pytest.fixture()
def ai_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YUKI_AI_DIR", str(tmp_path))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                 "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    import yuki.core.models as core_models

    monkeypatch.setattr(core_models, "get_key", lambda name: None)   # no registry keys leak into a test
    ai_config._cache.clear()
    return tmp_path


def _client(server: FakeOpenAI, provider: str = "custom", **kw: Any) -> OpenAICompatClient:
    return OpenAICompatClient(PROVIDERS[provider], base_url=server.url + "/v1", api_key=KEY, **kw)


def _route(ai_dir: Path, role: str, provider: str, model: str, backup: tuple[str, str] | None = None,
           base_url: str = "") -> None:
    data = ai_config.load()
    data["roles"][role] = {"provider": provider, "model": model,
                           "backup_provider": backup[0] if backup else "", "backup_model": backup[1] if backup else ""}
    if base_url:
        data["custom"] = {"label": "Test gateway", "base_url": base_url}
    ai_config.save(data)


# ---------------------------------------------------------------------------------------------------------------------
# 1. translation: Anthropic request -> chat completions
# ---------------------------------------------------------------------------------------------------------------------


def _anthropic_params(**extra: Any) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini", "max_tokens": 500,
        "system": [{"type": "text", "text": "You are Yuki.", "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "Rules here."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "open notes", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "toolu_1", "name": "launch_app", "input": {"name": "Notes"}},
                {"type": "tool_use", "id": "toolu_2", "name": "look_at_window", "input": {"hwnd": 5}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "launched"},
                {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
                 "content": [{"type": "text", "text": "window gone"},
                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
                {"type": "text", "text": "and hurry"}]},
        ],
        "tools": [{"name": "launch_app", "description": "Open an app", "input_schema": {
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            "cache_control": {"type": "ephemeral"}},
            {"type": "web_search_20250305", "name": "web_search"}],
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "high"},
        "stop_sequences": ["STOP"],
        **extra,
    }


def test_request_translation_full() -> None:
    body = to_openai_request(_anthropic_params(), PROVIDERS["openai"], known_model("openai", "gpt-5-mini"))
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "You are Yuki.\n\nRules here."}
    assert msgs[1] == {"role": "user", "content": "open notes"}
    assistant = msgs[2]
    assert assistant["content"] == "On it." and [c["function"]["name"] for c in assistant["tool_calls"]] == \
        ["launch_app", "look_at_window"]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"name": "Notes"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "launched"}
    assert msgs[4]["role"] == "tool" and msgs[4]["content"].startswith("(the tool reported an error) window gone")
    # the tool result's image follows the tool messages (OpenAI takes images), then the user's own words
    assert msgs[5]["role"] == "user" and msgs[5]["content"][0]["type"] == "image_url"
    assert msgs[5]["content"][0]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert msgs[5]["content"][1] == {"type": "text", "text": "and hurry"}
    assert "cache_control" not in json.dumps(body)            # caching is the service's own here
    assert [t["function"]["name"] for t in body["tools"]] == ["launch_app"]   # the server tool has no mapping
    assert body["tool_choice"] == "required" and body["parallel_tool_calls"] is False
    assert body["max_completion_tokens"] == 500 and "max_tokens" not in body
    assert body["reasoning_effort"] == "high"                  # a reasoning model takes the request's effort
    assert body["stop"] == ["STOP"]
    assert "thinking" not in body and "output_config" not in body


def test_request_translation_per_provider() -> None:
    params = _anthropic_params(thinking={"type": "disabled"})
    gemini = to_openai_request(params, PROVIDERS["gemini"], None, stream=True)
    assert gemini["max_tokens"] == 500 and "max_completion_tokens" not in gemini
    assert "reasoning_effort" not in gemini                    # not known as a reasoning model: nothing sent
    assert gemini["stream"] is True and gemini["stream_options"] == {"include_usage": True}
    off = to_openai_request(params, PROVIDERS["openai"], known_model("openai", "gpt-5-mini"))
    assert off["reasoning_effort"] == "minimal"                # thinking off (the voice) -> the fastest setting
    router = to_openai_request(params, PROVIDERS["openrouter"], None)
    assert router["usage"] == {"include": True}                # OpenRouter returns the call's cost
    # a model that can't see images gets a plain note instead of the image
    blind = to_openai_request(params, PROVIDERS["openrouter"], ModelInfo("x", images=False))
    user = [m for m in blind["messages"] if m["role"] == "user"][-1]
    assert IMAGE_LEFT_OUT in user["content"]
    deepseek = to_openai_request(params, PROVIDERS["deepseek"], None)   # the service takes no images at all
    assert IMAGE_LEFT_OUT in [m for m in deepseek["messages"] if m["role"] == "user"][-1]["content"]


def test_tool_choice_named_and_none() -> None:
    p = _anthropic_params(tool_choice={"type": "tool", "name": "launch_app"})
    assert to_openai_request(p, PROVIDERS["custom"])["tool_choice"] == {"type": "function",
                                                                        "function": {"name": "launch_app"}}
    p = _anthropic_params(tool_choice={"type": "auto"})
    assert "tool_choice" not in to_openai_request(p, PROVIDERS["custom"])


def test_response_translation_round_trip() -> None:
    data = completion("Done.", [tool_call("call_a", "done", {"message": "Opened Notes."}),
                                tool_call("call_b", "say", '{"text": "one sec"')], finish="tool_calls",
                      prompt=1200, completion_tokens=40, reasoning_content="I should open it.")
    data["usage"]["prompt_tokens_details"] = {"cached_tokens": 1000}
    data["usage"]["cost"] = 0.00123
    msg = from_openai_response(data, model="gpt-5-mini", thinking=True)
    assert isinstance(msg, anthropic.types.Message)
    assert [b.type for b in msg.content] == ["thinking", "text", "tool_use", "tool_use"]
    assert msg.content[2].input == {"message": "Opened Notes."} and msg.content[2].id == "call_a"
    assert msg.content[3].input == {"_unparsed_arguments": '{"text": "one sec"'}   # nothing guessed
    assert msg.stop_reason == "tool_use"
    assert (msg.usage.input_tokens, msg.usage.cache_read_input_tokens, msg.usage.output_tokens) == (200, 1000, 40)
    assert msg.usage.provider_cost_usd == pytest.approx(0.00123)
    # back again: the assistant turn translates to the same calls
    again = to_openai_request({"model": "m", "messages": [{"role": "assistant", "content": msg.content}]},
                              PROVIDERS["custom"])
    calls = again["messages"][0]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"message": "Opened Notes."}
    # thinking off: the reasoning isn't surfaced; Gemini/Ollama say "stop" on a tool turn: still tool_use
    quiet = from_openai_response(completion(None, [tool_call("c", "done", {"message": "x"})], finish="stop",
                                            reasoning="r"), model="m", thinking=False)
    assert [b.type for b in quiet.content] == ["tool_use"] and quiet.stop_reason == "tool_use"
    assert from_openai_response(completion("cut", finish="length"), model="m").stop_reason == "max_tokens"
    assert from_openai_response(completion("no", finish="content_filter"), model="m").stop_reason == "refusal"


# ---------------------------------------------------------------------------------------------------------------------
# 2. the fake server: plain calls, streams (tool calls split across chunks, parallel calls), errors
# ---------------------------------------------------------------------------------------------------------------------


def test_plain_call_over_http_sends_key_and_body() -> None:
    with FakeOpenAI([Reply(body=completion("Hello."))]) as server:
        msg = _client(server).messages.create(model="m1", max_tokens=10, messages=[{"role": "user", "content": "hi"}])
    assert msg.content[0].text == "Hello." and msg.stop_reason == "end_turn" and msg.model == "m1"
    sent = server.posts()[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    assert sent["json"]["messages"] == [{"role": "user", "content": "hi"}]


STREAM_TOOLS = [
    chunk({"role": "assistant", "content": ""}),
    chunk({"content": "Opening "}),
    chunk({"content": "it."}),
    chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                           "function": {"name": "launch_app", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"say_now": "Opening Notes", '}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"name": "No'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'tes"}'}}]}),
    chunk({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                           "function": {"name": "done", "arguments": '{"message"'}}]}),
    chunk({"tool_calls": [{"index": 1, "function": {"arguments": ': "Opened."}'}}]}),
    chunk({}, finish="tool_calls"),
    usage_chunk(900, 30, cached=800),
]


def test_stream_tool_calls_split_and_parallel() -> None:
    with FakeOpenAI([Reply(chunks=STREAM_TOOLS)]) as server:
        with _client(server).messages.stream(model="m", max_tokens=100,
                                             messages=[{"role": "user", "content": "open notes"}]) as stream:
            events = list(stream)
            final = stream.get_final_message()
    kinds = [e.type for e in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    starts = [e.content_block.type for e in events if e.type == "content_block_start"]
    assert starts == ["text", "tool_use", "tool_use"]
    stops = [e for e in events if e.type == "content_block_stop"]
    assert [getattr(s.content_block, "name", None) for s in stops] == [None, "launch_app", "done"]
    assert stops[1].content_block.input == {"say_now": "Opening Notes", "name": "Notes"}   # complete at its stop
    pieces = [e.partial_json for e in events if e.type == "input_json"]
    assert "".join(pieces[:3]) == '{"say_now": "Opening Notes", "name": "Notes"}'   # streamed in pieces
    snaps = [e.snapshot for e in events if e.type == "input_json"]
    assert snaps[0] == {"say_now": "Opening Notes"}             # the partial input as the SDK parses it
    texts = [e.text for e in events if e.type == "text"]
    assert texts == ["Opening ", "it."]
    delta = next(e for e in events if e.type == "message_delta")
    assert delta.delta.stop_reason == "tool_use" and delta.usage.output_tokens == 30
    # the stop event comes after both tools' blocks closed: the loop's early-run check sees them confirmed
    assert kinds.index("message_delta") > max(i for i, k in enumerate(kinds) if k == "content_block_stop")
    assert [b.type for b in final.content] == ["text", "tool_use", "tool_use"]
    assert final.content[2].input == {"message": "Opened."} and final.stop_reason == "tool_use"
    assert (final.usage.input_tokens, final.usage.cache_read_input_tokens) == (100, 800)


def test_stream_feeds_the_loops_partials() -> None:
    """The agent loop's live partials (yuki.agent.stream_partial) read the adapter's events like Bedrock's."""
    from yuki.agent.stream_partial import stream_partial

    with FakeOpenAI([Reply(chunks=STREAM_TOOLS)]) as server:
        with _client(server).messages.stream(model="m", max_tokens=100, messages=[{"role": "user", "content": "x"}]) as s:
            state: dict[str, Any] = {}
            parts = [p for p in (stream_partial(e, state) for e in s) if p is not None]
    assert ("step", "Opening Notes") in [(p.what, p.text) for p in parts]


def test_stream_whole_call_in_one_chunk_and_reasoning() -> None:
    chunks = [chunk({"reasoning_content": "Thinking about "}), chunk({"reasoning_content": "it."}),
              chunk({"tool_calls": [{"index": 0, "id": "fc_1", "function": {"name": "done",
                                                                           "arguments": {"message": "Hi"}}}]},
                    finish="stop")]
    with FakeOpenAI([Reply(chunks=chunks, done=False)]) as server:   # Gemini-like: whole call, "stop", no [DONE]
        with _client(server).messages.stream(model="m", max_tokens=50, thinking={"type": "adaptive"},
                                             messages=[{"role": "user", "content": "x"}]) as s:
            events = list(s)
            final = s.get_final_message()
    assert [b.type for b in final.content] == ["thinking", "tool_use"]
    assert final.content[0].thinking == "Thinking about it." and final.content[1].input == {"message": "Hi"}
    assert final.stop_reason == "tool_use"
    assert any(e.type == "thinking" for e in events)


def test_raw_stream_create_true() -> None:
    """``create(stream=True)``: the raw events Pipecat's Anthropic service iterates (``beta.messages``)."""
    chunks = [chunk({"content": "Sure."}), chunk({}, finish="stop"), usage_chunk(50, 3)]
    with FakeOpenAI([Reply(chunks=chunks)]) as server:
        raw = _client(server).beta.messages.create(model="m", max_tokens=20, stream=True, betas=["x"],
                                                   messages=[{"role": "user", "content": "x"}])
        events = list(raw)
    assert [e.type for e in events] == ["message_start", "content_block_start", "content_block_delta",
                                        "content_block_stop", "message_delta", "message_stop"]
    assert events[2].delta.text == "Sure." and events[4].usage.input_tokens == 50
    assert "betas" not in server.posts()[0]["json"]


@pytest.mark.parametrize("status,cls,kind", [
    (401, anthropic.AuthenticationError, "fatal"),
    (429, anthropic.RateLimitError, "retry"),
    (500, anthropic.InternalServerError, "retry"),
    (503, anthropic.InternalServerError, "retry"),
    (404, anthropic.NotFoundError, "failover"),
    (400, anthropic.BadRequestError, "fatal"),
])
def test_http_errors_are_the_sdks_own(status: int, cls: type, kind: str) -> None:
    body = {"error": {"message": f"Incorrect API key provided: {KEY}" if status == 401 else "nope", "code": status}}
    with FakeOpenAI([Reply(status, body, headers={"retry-after": "2"})]) as server:
        with pytest.raises(cls) as caught:
            _client(server).messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    exc = caught.value
    assert classify(exc) == kind and exc.status_code == status
    assert KEY not in str(exc)                                   # never a key in a message
    assert str(exc).startswith("Other (OpenAI-compatible): ")
    if status == 429:
        assert exc.response.headers.get("retry-after") == "2"    # the failover backoff honours it


def test_stream_errors() -> None:
    cut = [chunk({"content": "Half"})]
    upstream = [{"error": {"message": "Provider returned error", "code": 502}}]
    with FakeOpenAI([Reply(chunks=cut, done=False, cut=True), Reply(chunks=upstream), Reply(429, {"error": "slow"})]) \
            as server:
        client = _client(server)
        with pytest.raises(anthropic.APIConnectionError, match="stopped halfway"):
            with client.messages.stream(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}]) as s:
                list(s)
        with pytest.raises(anthropic.InternalServerError, match="Provider returned error"):
            with client.messages.stream(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}]) as s:
                list(s)
        with pytest.raises(anthropic.RateLimitError):
            with client.messages.stream(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}]) as s:
                list(s)


def test_no_connection_is_a_connection_error() -> None:
    client = OpenAICompatClient(PROVIDERS["ollama"], base_url="http://127.0.0.1:9/v1", connect_timeout=1.0)
    with pytest.raises(anthropic.APIConnectionError) as caught:
        client.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    assert classify(caught.value) == "retry" and "Is it running?" in caught.value.message


def test_async_client_raw_and_helper_streams() -> None:
    async def go(url: str) -> tuple[list[str], Any, Any]:
        client = AsyncOpenAICompatClient(PROVIDERS["custom"], base_url=url + "/v1", api_key=KEY)
        raw = await client.beta.messages.create(model="m", max_tokens=20, stream=True,
                                                messages=[{"role": "user", "content": "x"}])
        kinds = [e.type async for e in raw]
        async with client.messages.stream(model="m", max_tokens=20, messages=[{"role": "user", "content": "y"}]) as s:
            final = await s.get_final_message()
        plain = await client.messages.create(model="m", max_tokens=20, messages=[{"role": "user", "content": "z"}])
        await client.aclose()
        return kinds, final, plain

    with FakeOpenAI([Reply(chunks=STREAM_TOOLS), Reply(chunks=STREAM_TOOLS), Reply(body=completion("plain"))]) as server:
        kinds, final, plain = asyncio.run(go(server.url))
    assert kinds[0] == "message_start" and kinds.count("content_block_start") == 3
    assert final.content[1].input == {"say_now": "Opening Notes", "name": "Notes"}
    assert plain.content[0].text == "plain"


# ---------------------------------------------------------------------------------------------------------------------
# 3. the default stays exactly as it was; routing per role
# ---------------------------------------------------------------------------------------------------------------------


def _stub_factory(seen: list) -> Any:
    def factory(region: str) -> Any:
        def create(**params: Any) -> Any:
            seen.append((region, params))
            return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
        return SimpleNamespace(messages=SimpleNamespace(create=create))
    return factory


def test_default_unchanged_without_ai_json(ai_dir: Path) -> None:
    assert not ai_config.config_path().exists()
    for role in ("thinker", "memory", "talker"):
        assert ai_config.route(role) is None
    seen: list = []
    settings = Settings()
    client = FailoverClient.from_settings(settings, factory=_stub_factory(seen))
    assert client.role == "thinker" and make_client(settings).role == "memory"
    params = {"model": settings.model, "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]}
    client.messages.create(**params)
    assert seen == [(settings.aws_region, params)]                # same place, same params, untouched
    assert client.last_model() == settings.model
    # pricing as before: the Claude table
    tokens = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert settings.estimate_cost(settings.model, tokens) == pytest.approx(2.0)


def test_default_unchanged_with_roles_on_default(ai_dir: Path) -> None:
    ai_config.save({"roles": {"thinker": {"provider": "default"}}})
    seen: list = []
    client = FailoverClient.from_settings(Settings(), factory=_stub_factory(seen))
    client.messages.create(model="global.anthropic.claude-sonnet-5", max_tokens=5, messages=[])
    assert seen and seen[0][0] == Settings().aws_region


def test_thinker_routed_to_another_provider(ai_dir: Path) -> None:
    with FakeOpenAI([Reply(body=completion("Hi from the gateway."))]) as server:
        _route(ai_dir, "thinker", "custom", "free-model-1", base_url=server.url + "/v1")
        seen: list = []
        settings = Settings()
        client = FailoverClient.from_settings(settings, factory=_stub_factory(seen))
        msg = client.messages.create(model=settings.model, max_tokens=50, messages=[{"role": "user", "content": "hi"}],
                                     thinking={"type": "adaptive"}, output_config={"effort": "medium"})
        memory = make_client(settings, role="memory")   # memory stays on default: not routed
        assert ai_config.route("memory") is None and memory.role == "memory"
    assert msg.content[0].text == "Hi from the gateway."
    assert seen == []                                          # Claude never called
    assert server.posts()[0]["json"]["model"] == "free-model-1"
    assert client.last_model() == "custom:free-model-1"
    # priced as the model that answered: a custom service's price isn't known -> None (never Claude's rate)
    tokens = {"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert settings.estimate_cost(settings.model, tokens) is None
    assert settings.estimate_cost("custom:free-model-1", tokens) is None


def test_routed_retry_then_backup_never_silent_claude(ai_dir: Path) -> None:
    with FakeOpenAI([Reply(503, {"error": {"message": "busy"}}), Reply(503, {"error": {"message": "busy"}}),
                     Reply(503, {"error": {"message": "busy"}})]) as primary, \
            FakeOpenAI([Reply(body=completion("from backup"))]) as backup:
        _route(ai_dir, "memory", "custom", "m-main", backup=("ollama", "small-local"), base_url=primary.url + "/v1")
        monkey_ollama = PROVIDERS["ollama"]
        import yuki.agent.providers.client as pc

        real = pc.sync_provider_client

        def fake_factory(provider_id: str, settings: Any = None, **kw: Any) -> Any:
            if provider_id == "ollama":   # point the backup at the second fake server
                return OpenAICompatClient(monkey_ollama, base_url=backup.url + "/v1")
            return real(provider_id, settings, **kw)

        pc.sync_provider_client = fake_factory
        try:
            seen: list = []
            log: list = []
            client = FailoverClient.from_settings(Settings(), factory=_stub_factory(seen), role="memory",
                                                  log=lambda e, **f: log.append((e, f)))
            client._sleep = lambda s: None
            client._routed.clear()
            msg = client.messages.create(model="global.anthropic.claude-haiku-4-5-20251001-v1:0", max_tokens=5,
                                         messages=[{"role": "user", "content": "x"}])
        finally:
            pc.sync_provider_client = real
    assert msg.content[0].text == "from backup" and seen == []
    assert len(primary.posts()) == 3                            # retried on its own provider first
    assert [e for e, _ in log].count("model_retry") == 2 and any(e == "model_failover" for e, _ in log)
    assert client.last_model() == "ollama:small-local"
    tokens = {"input_tokens": 5000, "output_tokens": 100, "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert Settings().estimate_cost("global.anthropic.claude-haiku-4-5-20251001-v1:0", tokens) == 0.0   # local


def test_routed_without_backup_raises_and_never_falls_back(ai_dir: Path) -> None:
    with FakeOpenAI([Reply(401, {"error": {"message": "bad key"}})]) as server:
        _route(ai_dir, "thinker", "custom", "m", base_url=server.url + "/v1")
        seen: list = []
        client = FailoverClient.from_settings(Settings(), factory=_stub_factory(seen))
        with pytest.raises(anthropic.AuthenticationError):
            client.messages.create(model="global.anthropic.claude-sonnet-5", max_tokens=5, messages=[])
    assert seen == []


def test_missing_key_or_model_says_so(ai_dir: Path) -> None:
    _route(ai_dir, "thinker", "openai", "gpt-5-mini")
    seen: list = []
    client = FailoverClient.from_settings(Settings(), factory=_stub_factory(seen))
    with pytest.raises(ProviderSetupError, match="No OpenAI key is saved"):
        client.messages.create(model="x", max_tokens=5, messages=[])
    _route(ai_dir, "thinker", "openrouter", "")
    with pytest.raises(ProviderSetupError, match="No model is picked for doing things"):
        client.messages.create(model="x", max_tokens=5, messages=[])
    assert seen == []


def test_agent_loop_end_to_end_on_a_routed_stream(ai_dir: Path, tmp_path: Path) -> None:
    """The real agent loop, streaming, its thinker moved to an OpenAI-protocol service: tools run, the reply lands."""
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.log.events import Final, SessionLogger

    first = [chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "say", "arguments": ""}}]}),
             chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"text": "Looking now."}'}}]}),
             chunk({}, finish="tool_calls"), usage_chunk(3000, 20)]
    second = [chunk({"tool_calls": [{"index": 0, "id": "call_2", "function": {"name": "done", "arguments": ""}}]}),
              chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"message": "All done."}'}}]}),
              chunk({}, finish="tool_calls"), usage_chunk(3100, 12)]
    with FakeOpenAI([Reply(chunks=first), Reply(chunks=second)]) as server:
        _route(ai_dir, "thinker", "custom", "gw-model", base_url=server.url + "/v1")
        s = Settings(log_dir=tmp_path / "sessions", stream=True, requests_csv=tmp_path / "r.csv")
        memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
        agent = Agent(s, SessionLogger(s.sessions_dir, console=None), backend=SimpleNamespace(), prewarm=False,
                      record_requests=False, memory=memory)
        events = list(agent.run("check something"))
    assert isinstance(events[-1], Final) and "All done." in events[-1].text
    bodies = [r["json"] for r in server.posts()]
    assert all(b["model"] == "gw-model" and b["stream"] for b in bodies)
    second_turn = bodies[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in second_turn)
    assert agent.last_summary["model"] == "custom:gw-model"


# ---------------------------------------------------------------------------------------------------------------------
# 4. the voice talker's async client
# ---------------------------------------------------------------------------------------------------------------------


class _AsyncDefault:
    def __init__(self) -> None:
        self.calls: list = []

        async def create(**params: Any) -> Any:
            self.calls.append(params)
            return "default-answer"

        self.messages = SimpleNamespace(create=create, stream=lambda **p: ("default-stream", p))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_talker_default_untouched(ai_dir: Path) -> None:
    default = _AsyncDefault()
    client = AsyncRoleClient("talker", default, settings=Settings())
    assert not client.routed()
    out = asyncio.run(client.beta.messages.create(model="haiku", max_tokens=5, stream=True, messages=[]))
    assert out == "default-answer" and default.calls == [{"model": "haiku", "max_tokens": 5, "stream": True,
                                                          "messages": []}]
    assert client.messages.stream(model="h")[0] == "default-stream"


def test_talker_routed_stream_with_retry(ai_dir: Path) -> None:
    chunks = [chunk({"content": "Hey."}), chunk({}, finish="stop"), usage_chunk(40, 2)]
    with FakeOpenAI([Reply(429, {"error": {"message": "slow down"}}), Reply(chunks=chunks)]) as server:
        _route(ai_dir, "talker", "custom", "voice-fast", base_url=server.url + "/v1")
        default = _AsyncDefault()
        waits: list = []

        async def sleep(s: float) -> None:
            waits.append(s)

        client = AsyncRoleClient("talker", default, settings=Settings(), sleep=sleep)

        async def go() -> list[str]:
            stream = await client.beta.messages.create(model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
                                                       max_tokens=50, stream=True, betas=["b"],
                                                       system=[{"type": "text", "text": "Be brief."}],
                                                       messages=[{"role": "user", "content": "hi"}])
            return [e.type async for e in stream]

        kinds = asyncio.run(go())
    assert kinds[0] == "message_start" and "content_block_delta" in kinds and kinds[-1] == "message_stop"
    assert default.calls == [] and len(waits) == 1
    assert server.posts()[1]["json"]["model"] == "voice-fast"
    assert client.last_model() == "custom:voice-fast"
    from yuki.agent.providers.client import talker_price_model

    assert talker_price_model(Settings(), client) == "custom:voice-fast"


def test_talker_asks_for_one_tool_call_per_reply(ai_dir: Path) -> None:
    """Pipecat's Anthropic service runs only a reply's last tool call: a routed talker asks for one at a time."""
    with FakeOpenAI([Reply(body=completion("ok"))]) as server:
        _route(ai_dir, "talker", "openai", "gpt-5-mini")
        client = AsyncRoleClient("talker", _AsyncDefault(), settings=Settings(), factory=lambda pid:
                                 AsyncOpenAICompatClient(PROVIDERS[pid], base_url=server.url + "/v1", api_key=KEY,
                                                         model_info=lambda m: known_model(pid, m)))
        tools = [{"name": "tell_yuki", "description": "hand over", "input_schema": {"type": "object", "properties": {}}}]
        asyncio.run(client.messages.create(model="haiku", max_tokens=20, tools=tools,
                                           messages=[{"role": "user", "content": "hi"}]))
    body = server.posts()[0]["json"]
    assert body["parallel_tool_calls"] is False and body["model"] == "gpt-5-mini"
    assert body["reasoning_effort"] in ("low", "medium", "minimal")


# ---------------------------------------------------------------------------------------------------------------------
# 5. keys: DPAPI encrypt/decrypt, environment, never in plain files or logs
# ---------------------------------------------------------------------------------------------------------------------


def test_key_store_encrypts_and_round_trips(ai_dir: Path) -> None:
    assert ai_keys.key_source("openai") == "missing"
    ai_keys.set_key("openai", KEY)
    raw = ai_keys.key_path().read_bytes()
    assert KEY.encode() not in raw and b"openai" not in raw          # DPAPI blob: nothing readable
    assert ai_keys.get_key("openai") == KEY and ai_keys.key_source("openai") == "saved"
    assert KEY in ai_keys.saved_values()
    ai_keys.forget_key("openai")
    assert ai_keys.get_key("openai") is None
    with pytest.raises(ValueError):
        ai_keys.set_key("openai", "two words")
    with pytest.raises(ValueError):
        ai_keys.set_key("default", KEY)
    assert ai_keys.key_source("ollama") == "not_needed"
    # nothing about keys ever lands in ai.json
    ai_config.save(ai_config.load())
    assert KEY not in ai_config.config_path().read_text(encoding="utf-8")


def test_env_keys_honoured(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 35)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "d" * 32)
    import yuki.core.models as core_models

    monkeypatch.setattr(core_models, "get_key", lambda name: __import__("os").environ.get(name))
    assert ai_keys.key_source("gemini") == "env" and ai_keys.get_key("deepseek") == "sk-" + "d" * 32
    ai_keys.set_key("gemini", "AIza" + "y" * 35)
    assert ai_keys.get_key("gemini") == "AIza" + "y" * 35            # a key saved in Settings wins


def test_saved_key_never_reaches_a_session_log(ai_dir: Path, tmp_path: Path) -> None:
    """The session log runs the secret filter on every line: a saved key of any format is hidden there."""
    from yuki.log.events import SessionLogger
    from yuki.memory import secret_filter

    odd = "gw_Zx81Qk2Lm0Pn7Rt4Vb9Wc3"   # an invented gateway key in no known vendor format
    ai_keys.set_key("custom", odd)
    secret_filter._saved_cache[0] = -1e9   # looked at again at once (it re-checks every 2 s by itself)
    assert secret_filter.find(f"my key is {odd} ok")
    log = SessionLogger(tmp_path / "s", console=None)
    log.log("note", text=f"pasted {odd} and {KEY}")
    written = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "s").glob("*.jsonl"))
    assert odd not in written and KEY not in written


@pytest.mark.parametrize("sample", [
    "sk-proj-" + "Ab1" * 16,                       # OpenAI
    "sk-or-v1-" + "0a1b" * 16,                     # OpenRouter
    "AIza" + "Q1w2E3r4T5" * 3 + "y6U7i",            # Gemini
    "sk-ant-api03-" + "Zz9" * 10,                  # Anthropic
    "sk-" + "4f7a" * 8,                            # DeepSeek
])
def test_provider_key_formats_caught_in_captured_text(sample: str, ai_dir: Path) -> None:
    from yuki.memory import secret_filter

    assert secret_filter.redact_text(f"export KEY={sample}").count(sample) == 0


# ---------------------------------------------------------------------------------------------------------------------
# 6. pricing and discovery
# ---------------------------------------------------------------------------------------------------------------------


def test_pricing_rules(ai_dir: Path) -> None:
    from yuki.agent.providers import pricing

    s = Settings()
    tokens = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert s.estimate_cost("openai:gpt-5-mini", tokens) == pytest.approx(0.25 + 2.0)
    assert s.estimate_cost("ollama:llama3.2", tokens) == 0.0
    assert s.estimate_cost("openrouter:some/model:free", tokens) == 0.0
    assert s.estimate_cost("openrouter:some/paid-model", tokens) is None          # unknown, never invented
    assert s.estimate_cost("anthropic:claude-sonnet-5", tokens) == pytest.approx(12.0)   # the Claude table
    assert s.estimate_cost("deepseek:deepseek-chat", tokens) is None
    ai_config.save({"prices": {"openrouter:some/paid-model": {"input": 1.0, "output": 3.0}}})
    assert s.estimate_cost("openrouter:some/paid-model", tokens) == pytest.approx(4.0)
    # OpenRouter's returned amount wins for the call it came with
    pricing.note_returned(0.0042, {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0})
    assert s.estimate_cost("openrouter:some/paid-model", {"input_tokens": 10, "output_tokens": 2}) == 0.0042
    assert split_qualified("global.anthropic.claude-haiku-4-5-20251001-v1:0") is None
    assert pricing.price_words("ollama:x") == "Free" and pricing.price_words("custom:x") == "Price not known"


def test_ollama_listing_and_capabilities(ai_dir: Path) -> None:
    from yuki.agent.providers import discover

    tags = {"models": [{"name": "llama3.2:3b", "details": {"parameter_size": "3.2B"}},
                       {"name": "qwen3:14b", "details": {"parameter_size": "14.8B"}}]}
    with FakeOpenAI(models={"/api/tags": tags, "/api/show": {"capabilities": ["completion", "tools"]}}) as server:
        models = discover.ollama_models(base_url=server.url + "/v1")
    assert [m.id for m in models] == ["llama3.2:3b", "qwen3:14b"]
    assert models[0].small and not models[1].small and models[1].tools is True and models[0].free
    assert discover.ollama_models(base_url="http://127.0.0.1:9/v1") is None   # not running: said, never started
    assert "small model" in discover.fit_for("thinker", models[0])
    assert discover.fit_for("thinker", models[1]) == ""
    assert "can't use tools" in discover.fit_for("thinker", ModelInfo("x", tools=False))
    assert "don't know" in discover.fit_for("thinker", ModelInfo("x", tools=None))
    assert discover.fit_for("memory", ModelInfo("x", tools=False)) == ""


def test_openrouter_listing_marks_free(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.providers import discover

    data = {"data": [
        {"id": "vendor/free-one:free", "name": "Free One", "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools", "temperature"], "architecture": {"input_modalities": ["text"]}},
        {"id": "vendor/paid", "name": "Paid", "pricing": {"prompt": "0.000001", "completion": "0.000004"},
         "supported_parameters": ["temperature"], "architecture": {"input_modalities": ["text", "image"]}},
    ]}
    with FakeOpenAI(models={"/api/v1/models": data}) as server:
        real_get = discover._get
        monkeypatch.setattr(discover, "_get", lambda url, *a, **k: real_get(
            url.replace("https://openrouter.ai", server.url), *a, **k))
        models = discover.list_models("openrouter", refresh=True)
    free, paid = models
    assert free.free and free.tools is True and paid.tools is False and paid.images is True
    assert paid.price == {"input": pytest.approx(1.0), "output": pytest.approx(4.0)}
    assert Settings().estimate_cost("openrouter:vendor/paid", {"input_tokens": 1_000_000}) == pytest.approx(1.0)
    shown = discover.describe(free, "openrouter")
    assert shown["free"] is True and shown["price"] == "Free"


def test_provider_test_button_plain_reasons(ai_dir: Path) -> None:
    from yuki.agent.providers import discover

    ok, words = discover.test("openai")
    assert not ok and words == "No OpenAI key yet. Paste it above and press Save."
    with FakeOpenAI([Reply(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}}),
                     Reply(body=completion("ok"))]) as server:
        ai_config.save({"custom": {"label": "", "base_url": server.url + "/v1"}})
        ai_keys.set_key("custom", KEY)
        ok, words = discover.test("custom", "m")
        assert not ok and words.startswith("The key was refused") and KEY not in words
        ok, words = discover.test("custom", "m")
        assert ok and words == "Works. Other (OpenAI-compatible) answered with m."


def test_live_ollama_smoke_if_running() -> None:
    """One real call to a local Ollama, only when it is already running with a model (never installed or started)."""
    from yuki.agent.providers import discover

    models = discover.ollama_models(with_capabilities=False)
    if not models:
        pytest.skip("Ollama isn't running on this PC (or has no models)")
    client = OpenAICompatClient(PROVIDERS["ollama"], timeout=120.0)
    msg = client.messages.create(model=models[0].id, max_tokens=8, messages=[{"role": "user", "content": "Say ok."}])
    assert msg.stop_reason in ("end_turn", "max_tokens")
