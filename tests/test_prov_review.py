"""Settings → AI (report 67), the review's fixes: offline only (fake OpenAI-protocol servers, fake clients).

F1 OpenRouter's mid-stream error chunk is an error, never a finished answer; F2 one retry layer for a moved talker;
F3 model facts kept in ai.json for the tray and the doctor (and images left out when not known on a mixed service);
F4 the judge and the second look priced as the model that answered; F5 Ollama's short context warned about; F6 a
moved talker's turn priced per reply stream (backup included, cached tokens counted); F7 another service's unsigned
reasoning taken out before Claude; F8 a talker reply held to one tool call on every service.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _fake_openai import FakeOpenAI, Reply, chunk, completion, tool_call, usage_chunk  # noqa: E402

from yuki.agent.failover import FailoverClient, classify  # noqa: E402
from yuki.agent.providers import config as ai_config  # noqa: E402
from yuki.agent.providers import discover  # noqa: E402
from yuki.agent.providers.catalog import PROVIDERS, ModelInfo, known_model  # noqa: E402
from yuki.agent.providers.client import (  # noqa: E402
    AsyncRoleClient, FittedClaudeClient, price_talker_turn, strip_unsigned_thinking,
)
from yuki.agent.providers.openai_compat import (  # noqa: E402
    IMAGE_LEFT_OUT, AsyncOpenAICompatClient, OpenAICompatClient, StreamTranslator, from_openai_response,
    to_openai_request,
)
from yuki.config import Settings  # noqa: E402

KEY = "sk-test-InventedKeyForTests0123456789abcd"   # invented; never a real key
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture()
def ai_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YUKI_AI_DIR", str(tmp_path))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                 "ANTHROPIC_API_KEY", "OLLAMA_CONTEXT_LENGTH"):
        monkeypatch.delenv(name, raising=False)
    import yuki.core.models as core_models

    import os

    # the process environment only (the variables above were cleared; a test sets the one it needs), never the
    # user's registry
    monkeypatch.setattr(core_models, "get_key", lambda name: os.environ.get(name))
    ai_config._cache.clear()
    with discover._lock:
        discover._facts.clear()
        discover._lists.clear()
        discover._prices.clear()
    return tmp_path


def _route(role: str, provider: str, model: str, backup: tuple[str, str] | None = None, base_url: str = "") -> None:
    data = ai_config.load()
    data["roles"][role] = {"provider": provider, "model": model,
                           "backup_provider": backup[0] if backup else "", "backup_model": backup[1] if backup else ""}
    if base_url:
        data["custom"] = {"label": "Test gateway", "base_url": base_url}
    ai_config.save(data)


def _user(text: str = "x") -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


# -- F1: OpenRouter's mid-stream error ----------------------------------------------------------------------------------

MID_STREAM_ERROR = {"id": "gen-1", "object": "chat.completion.chunk",
                    "error": {"code": "server_error", "message": "Provider disconnected"},
                    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]}


def test_mid_stream_error_chunk_is_an_error_not_an_end() -> None:
    chunks = [chunk({"content": "I'll open the fi"}), MID_STREAM_ERROR]
    with FakeOpenAI([Reply(chunks=chunks), Reply(chunks=[chunk({"content": "Half"}),
                                                         chunk({}, finish="error")])]) as server:
        client = OpenAICompatClient(PROVIDERS["openrouter"], base_url=server.url + "/api/v1", api_key=KEY)
        with pytest.raises(anthropic.InternalServerError, match="Provider disconnected") as caught:
            with client.messages.stream(model="m", max_tokens=20, messages=_user()) as stream:
                stream.get_final_message()
        assert classify(caught.value) == "retry"          # retried, then the backup: never handed on as done
        with pytest.raises(anthropic.InternalServerError):  # a bare "error" finish too
            with client.messages.stream(model="m", max_tokens=20, messages=_user()) as stream:
                stream.get_final_message()


def test_error_beside_choices_in_a_plain_answer() -> None:
    body = {**completion("partial"), "error": {"code": 429, "message": "Rate limited upstream"}}
    with FakeOpenAI([Reply(body=body)]) as server:
        client = OpenAICompatClient(PROVIDERS["openrouter"], base_url=server.url + "/api/v1", api_key=KEY)
        with pytest.raises(anthropic.RateLimitError, match="Rate limited upstream"):
            client.messages.create(model="m", max_tokens=5, messages=_user())


def test_async_raw_stream_raises_on_the_error_chunk() -> None:
    async def go(url: str) -> list[str]:
        client = AsyncOpenAICompatClient(PROVIDERS["openrouter"], base_url=url + "/api/v1", api_key=KEY)
        raw = await client.beta.messages.create(model="m", max_tokens=20, stream=True, messages=_user())
        seen = []
        async for event in raw:
            seen.append(event.type)
        return seen

    with FakeOpenAI([Reply(chunks=[chunk({"content": "Hal"}), MID_STREAM_ERROR])]) as server:
        with pytest.raises(anthropic.InternalServerError):
            asyncio.run(go(server.url))


# -- F2: one retry layer for a moved talker -----------------------------------------------------------------------------


def _layered(routed: bool) -> tuple[Any, list]:
    import httpx

    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.calls = 0

        async def _create_message_stream(self, api_call: Any, params: Any) -> Any:
            self.calls += 1
            raise anthropic.APIConnectionError(message="no connection", request=httpx.Request("POST", "http://x"))

    events: list = []
    hooks = SimpleNamespace(fallbacks=lambda: [], sticky_index=lambda n: 0, routed=lambda: routed,
                            client_for=lambda region: None, model_ok=lambda i, t: events.append(("ok", i)),
                            model_problem=lambda i, t, e, k: events.append(("problem", i, k)))
    llm = _layered_service(Base, MouthMemory(), hooks)()
    llm._yuki_call = None
    return llm, events


def test_moved_talker_gets_one_attempt_from_the_voice_layer() -> None:
    import httpx

    llm, events = _layered(routed=True)
    sent: list = []

    async def api_call(**params: Any) -> Any:          # the role client: it already did its own retries and backup
        sent.append(params)
        raise anthropic.APIConnectionError(message="no connection", request=httpx.Request("POST", "http://x"))

    with pytest.raises(anthropic.APIConnectionError):
        asyncio.run(llm._create_message_stream(api_call, {"model": "m", "stream": True}))
    # one attempt, straight to the role client (not through Pipecat's timeout layer, V1)
    assert len(sent) == 1 and llm.calls == 0 and events == [("problem", 0, "retry")]


def test_talker_on_claude_keeps_its_retry() -> None:
    llm, events = _layered(routed=False)
    with pytest.raises(anthropic.APIConnectionError):
        asyncio.run(llm._create_message_stream(None, {"model": "m", "stream": True}))
    assert llm.calls == 2                                            # as before


# -- F3: facts kept in ai.json; images only where known -----------------------------------------------------------------


def _image_request(model: str) -> dict[str, Any]:
    return {"model": model, "max_tokens": 50, "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "take_screenshot", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "the screen"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]}]}


def _sent_images(body: dict[str, Any]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in body["messages"])


def test_facts_picked_in_the_window_reach_the_tray_and_the_doctor(ai_dir: Path, monkeypatch: pytest.MonkeyPatch
                                                                   ) -> None:
    from yuki.agent.providers import summary

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    model = "vendor/text-only:free"
    # the window read OpenRouter's list (this process) and you picked the model
    with discover._lock:
        discover._facts[("openrouter", model)] = ModelInfo(model, "Text only", tools=True, images=False,
                                                           free=True, context=65_536)
    summary.set_role("thinker", "openrouter", model)
    assert ai_config.current()["facts"][f"openrouter:{model}"] == {"tools": True, "images": False, "small": False,
                                                                   "context": 65_536}
    # the tray: another process, no list read
    with discover._lock:
        discover._facts.clear()
    info = discover.model_facts("openrouter", model)
    assert info is not None and info.tools is True and info.images is False and info.context == 65_536
    body = to_openai_request(_image_request(model), PROVIDERS["openrouter"], info)
    assert not _sent_images(body) and IMAGE_LEFT_OUT in str(body["messages"])
    status, lines, fixes = summary.doctor_lines()
    assert status == "ok" and fixes == [], (status, lines, fixes)   # no "I don't know if it can use tools"


def test_images_unknown_left_out_on_mixed_services_sent_on_vision_services() -> None:
    assert not _sent_images(to_openai_request(_image_request("x"), PROVIDERS["openrouter"], None))
    assert not _sent_images(to_openai_request(_image_request("x"), PROVIDERS["ollama"], None))
    assert not _sent_images(to_openai_request(_image_request("x"), PROVIDERS["custom"], None))
    assert _sent_images(to_openai_request(_image_request("gpt-new"), PROVIDERS["openai"], None))
    assert _sent_images(to_openai_request(_image_request("x"), PROVIDERS["openrouter"],
                                          ModelInfo("x", images=True)))
    assert _sent_images(to_openai_request(_image_request("gpt-5-mini"), PROVIDERS["openai"],
                                          known_model("openai", "gpt-5-mini")))


def test_a_model_typed_by_hand_stays_unknown(ai_dir: Path) -> None:
    from yuki.agent.providers import summary

    summary.set_role("thinker", "ollama", "typed-by-hand")
    assert "facts" not in ai_config.current() or "ollama:typed-by-hand" not in ai_config.current()["facts"]
    assert discover.model_facts("ollama", "typed-by-hand") is None


# -- F4: the judge and the second look priced as the model that answered ------------------------------------------------


class _Stream:
    def __init__(self, events: list) -> None:
        self._events = events

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self._events)

    def get_final_message(self) -> Any:
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                                                 cache_read_input_tokens=0,
                                                                 cache_creation_input_tokens=0))


class _ThreadClient:
    """A client whose ``last_model`` is per thread (as FailoverClient's is): set on the thread that called."""

    def __init__(self, name: str) -> None:
        self._local = threading.local()
        self._name = name
        self.messages = SimpleNamespace(stream=self._stream, create=self._create)

    def last_model(self) -> str | None:
        return getattr(self._local, "model", None)

    def last_routed_model(self) -> str | None:   # a routed call (FailoverClient's): the model that answered it
        return getattr(self._local, "model", None)

    def _stream(self, **params: Any) -> _Stream:
        self._local.model = self._name
        return _Stream([])

    def _create(self, **params: Any) -> Any:
        self._local.model = self._name
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="advise",
                                                        input={"verdict": "proceed", "advice": "Looks fine."})],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def test_judge_cost_is_the_answering_models(ai_dir: Path) -> None:
    from yuki.agent.judge import Authorizer, decide

    settings = Settings()
    v = decide(_ThreadClient("ollama:llama3.2"), {"model": settings.judge_model}, timeout_s=5.0)
    assert v.model == "ollama:llama3.2" and v.usage            # read on the judge's own thread
    owner = SimpleNamespace(settings=settings, _lock=threading.Lock(), cost_usd=0.0, unpriced=set())
    Authorizer._cost(owner, settings.judge_model, v)
    assert v.cost_usd == 0.0                                   # local: free, never Haiku's rate
    v2 = decide(_ThreadClient("custom:some-model"), {"model": settings.judge_model}, timeout_s=5.0)
    Authorizer._cost(owner, settings.judge_model, v2)
    assert v2.cost_usd is None                                 # unknown price: unknown, never invented
    assert owner.unpriced == {"custom:some-model"}             # second review V4: the request total is unknown too


def test_consult_cost_is_the_answering_models(ai_dir: Path) -> None:
    from yuki.agent.consult import run_consult

    advice = run_consult(_ThreadClient("ollama:llama3.2"), {"model": HAIKU, "messages": [{"content": "s"}]},
                         signal="test", timeout_s=5.0)
    assert advice.model == "ollama:llama3.2" and advice.advice == "Looks fine."
    assert Settings().estimate_cost(advice.model or HAIKU, advice.usage) == 0.0


def test_judge_through_a_routed_failover_client(ai_dir: Path) -> None:
    from yuki.agent.judge import decide

    with FakeOpenAI([Reply(chunks=[chunk({"content": "no tool"}), chunk({}, finish="stop"),
                                   usage_chunk(1000, 100)])]) as server:
        _route("thinker", "custom", "judge-m", base_url=server.url + "/v1")
        client = FailoverClient.from_settings(Settings(), factory=lambda region: None)
        v = decide(client, {"model": Settings().judge_model, "max_tokens": 50, "messages": _user()}, timeout_s=10.0)
    assert v.model == "custom:judge-m"


# -- F5: Ollama's short context -----------------------------------------------------------------------------------------


def test_ollama_context_warning_for_doing_things(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover, "ollama_server_context", lambda: None)
    capable = ModelInfo("qwen3:14b", tools=True, params_b=14.8)
    words = discover.fit_for("thinker", capable, "ollama")
    assert "context length" in words and "cut off" in words
    assert discover.fit_for("thinker", ModelInfo("q", tools=True, run_context=40_960), "ollama") == ""
    monkeypatch.setattr(discover, "ollama_server_context", lambda: 65_536)
    assert discover.fit_for("thinker", capable, "ollama") == ""
    assert "bigger model" in discover.fit_for("thinker", ModelInfo("s", tools=True, context=8_192), "openrouter")
    assert discover.fit_for("memory", capable, "ollama") == ""


def test_ollama_server_context_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "32768")
    assert discover.ollama_server_context() == 32768


def test_ollama_listing_reads_both_contexts(ai_dir: Path) -> None:
    tags = {"models": [{"name": "qwen3:14b", "details": {"parameter_size": "14.8B"}}]}
    shown = {"capabilities": ["completion", "tools"], "model_info": {"qwen3.context_length": 40_960},
             "parameters": "temperature 0.6\nnum_ctx 16384"}
    with FakeOpenAI(models={"/api/tags": tags, "/api/show": shown}) as server:
        models = discover.ollama_models(base_url=server.url + "/v1")
    assert models[0].context == 40_960 and models[0].run_context == 16_384
    # 16k is under 32k, and set by the model's own Modelfile: raising Ollama's context length can't help (V6)
    assert "Pick another model" in discover.fit_for("thinker", models[0], "ollama")


# -- F6: a moved talker's turn, priced per reply stream -----------------------------------------------------------------


def _talker(server_url: str, provider: str = "openai", default: Any = None, log: list | None = None
            ) -> AsyncRoleClient:
    async def sleep(_s: float) -> None:
        return None

    return AsyncRoleClient("talker", default, settings=Settings(), sleep=sleep,
                           log=(lambda e, **f: log.append((e, f))) if log is not None else None,
                           factory=lambda pid: AsyncOpenAICompatClient(
                               PROVIDERS[pid], base_url=server_url + "/v1", api_key=KEY,
                               model_info=lambda m: known_model(pid, m)))


async def _drain(client: AsyncRoleClient, **params: Any) -> list[Any]:
    stream = await client.beta.messages.create(stream=True, max_tokens=50, **params)
    return [e async for e in stream]


def test_routed_turn_priced_with_its_cached_tokens(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    chunks = [chunk({"content": "Hey."}), chunk({}, finish="stop"), usage_chunk(10_000, 100, cached=8_000)]
    with FakeOpenAI([Reply(chunks=chunks)]) as server:
        _route("talker", "openai", "gpt-5-mini")
        client = _talker(server.url)
        asyncio.run(_drain(client, model=HAIKU, messages=_user()))
    # what Pipecat counted: the uncached input only (it reads cached tokens from the first event, which has none)
    pipecat = {"input_tokens": 2_000, "output_tokens": 100, "cache_read_tokens": 0, "cache_write_tokens": 0}
    cost = price_talker_turn(Settings(), client, pipecat)
    assert cost == pytest.approx((2_000 * 0.25 + 8_000 * 0.025 + 100 * 2.0) / 1_000_000)
    assert client.take_stream_usage() == []                    # taken once


def test_backup_claude_answer_is_priced_as_claude(ai_dir: Path) -> None:
    class Default:
        def __init__(self) -> None:
            self.sent: list = []

            async def create(**params: Any) -> Any:
                self.sent.append(params)

                async def events() -> Any:
                    yield SimpleNamespace(type="message_start")
                    yield SimpleNamespace(type="message_stop")
                return events()

            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))
            self.messages = self.beta.messages

    default = Default()
    busy = Reply(429, {"error": {"message": "free limit used up"}})
    with FakeOpenAI([busy, busy, busy]) as server:
        _route("talker", "custom", "free-model", backup=("default", ""), base_url=server.url + "/v1")
        client = _talker(server.url, default=default)
        history = [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": ""},
                                                     {"type": "text", "text": "Hello."}]},
                   {"role": "user", "content": "and now?"}]
        asyncio.run(_drain(client, model=HAIKU, messages=history))
    assert client.last_model() == HAIKU                        # the backup answered: Claude, by its id
    sent = default.sent[0]["messages"][1]["content"]
    assert [b["type"] for b in sent] == ["text"]               # F7: the unsigned reasoning never reaches Claude
    usage = {"input_tokens": 1_000, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0}
    s = Settings()
    assert price_talker_turn(s, client, usage) == pytest.approx(s.estimate_cost(HAIKU, usage))
    assert s.estimate_cost(HAIKU, usage) > 0                   # never the free primary's 0


def test_default_talker_turn_priced_as_before(ai_dir: Path) -> None:
    client = AsyncRoleClient("talker", SimpleNamespace(), settings=Settings())
    usage = {"input_tokens": 1_000, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0}
    s = Settings()
    assert price_talker_turn(s, client, usage) == s.estimate_cost(s.voice_mouth_model, usage)
    assert price_talker_turn(s, None, usage) == s.estimate_cost(s.voice_mouth_model, usage)


# -- F7: another service's reasoning never sent to Claude ---------------------------------------------------------------


def test_strip_unsigned_thinking() -> None:
    from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

    params = {"model": "m", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [ThinkingBlock(type="thinking", thinking="plan", signature=""),
                                          ToolUseBlock(type="tool_use", id="t", name="say", input={})]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "signed", "signature": "abc"},
                                          TextBlock(type="text", text="ok")]}]}
    out = strip_unsigned_thinking(params)
    assert [getattr(b, "type", None) for b in out["messages"][1]["content"]] == ["tool_use"]
    assert out["messages"][2] is params["messages"][2]          # a signed (Claude) block stays
    clean = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    assert strip_unsigned_thinking(clean) is clean


def test_claude_clients_strip_before_sending() -> None:
    seen: list = []
    inner = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: seen.append(p)),
                            beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **p: seen.append(p))))
    client = FittedClaudeClient(inner, lambda _m: None, backend=None)
    client.messages.create(model="m", max_tokens=5, messages=[
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": ""},
                                          {"type": "text", "text": "y"}]}])
    assert seen[0]["messages"][0]["content"] == [{"type": "text", "text": "y"}]
    assert not hasattr(client, "model_backend")                # the app's own client keeps its own backend


# -- F8: a talker reply holds one tool call on every service ------------------------------------------------------------

TWO_CALLS = [
    chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "add_todo", "arguments": '{"text": "milk"}'}}]}),
    chunk({"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "tell_yuki", "arguments": '{"ask": "x"}'}}]}),
    chunk({}, finish="tool_calls"), usage_chunk(50, 10),
]
TOOLS = [{"name": "add_todo", "description": "a", "input_schema": {"type": "object", "properties": {}}},
         {"name": "tell_yuki", "description": "b", "input_schema": {"type": "object", "properties": {}}}]


def test_talker_on_gemini_gets_one_call_and_the_rest_is_logged(ai_dir: Path, monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    log: list = []
    with FakeOpenAI([Reply(chunks=TWO_CALLS)]) as server:
        _route("talker", "gemini", "gemini-2.5-flash")
        client = _talker(server.url, log=log)
        events = asyncio.run(_drain(client, model=HAIKU, messages=_user(), tools=TOOLS))
        body = server.posts()[0]["json"]
    assert "parallel_tool_calls" not in body                   # Gemini can't be asked
    starts = [e.content_block.name for e in events if e.type == "content_block_start"]
    assert starts == ["add_todo"]                              # one call, whole; never the last one silently
    assert ("tool_calls_skipped", {"role": "talker", "target": "gemini:gemini-2.5-flash", "count": 1}) in log


def test_one_call_translator_and_plain_answer() -> None:
    t = StreamTranslator("m", one_call=True)
    events = [e for c in TWO_CALLS[:-1] for e in t.feed(c)] + t.finish(ended=True)
    assert [e.content_block.name for e in events if e.type == "content_block_start"] == ["add_todo"]
    assert t.skipped_calls == 1
    both = StreamTranslator("m")
    events = [e for c in TWO_CALLS[:-1] for e in both.feed(c)] + both.finish(ended=True)
    assert len([e for e in events if e.type == "content_block_start"]) == 2   # the thinker keeps parallel calls
    data = completion(None, [tool_call("a", "add_todo", {}), tool_call("b", "tell_yuki", {})], finish="tool_calls")
    assert [b.name for b in from_openai_response(data, model="m", one_call=True).content] == ["add_todo"]
    assert len(from_openai_response(data, model="m").content) == 2
