"""Settings → AI (report 67), the second review's fixes: offline only (fake OpenAI-protocol servers, fake clients).

D1 an unreadable ai.json is logged once and warned about by the doctor; D2 the default path prices the judge as the
model asked (as on main); D3 short environment stand-ins are not hidden as keys; V1 a moved talker has no Pipecat
timeout layer and a first-event limit; V2 unsigned reasoning taken out on the default path too; V3 a mid-stream
failure after the reply began is logged; V4 the judge's and second look's returned amount and unknown prices reach the
request total; V5 a locked ai.json keeps the last good choice and never writes defaults over it; V6 a Modelfile's
short context gets its own advice.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _fake_openai import FakeOpenAI, Reply, chunk, usage_chunk  # noqa: E402

from yuki.agent.failover import FailoverClient  # noqa: E402
from yuki.agent.providers import config as ai_config  # noqa: E402
from yuki.agent.providers import discover  # noqa: E402
from yuki.agent.providers.catalog import PROVIDERS, ModelInfo, known_model  # noqa: E402
from yuki.agent.providers.client import AsyncRoleClient, FirstEventTimeout  # noqa: E402
from yuki.agent.providers.openai_compat import AsyncOpenAICompatClient  # noqa: E402
from yuki.config import Settings  # noqa: E402

KEY = "sk-test-InventedKeyForTests0123456789abcd"   # invented; never a real key
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "global.anthropic.claude-sonnet-5"


@pytest.fixture()
def ai_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YUKI_AI_DIR", str(tmp_path))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                 "ANTHROPIC_API_KEY", "OLLAMA_CONTEXT_LENGTH"):
        monkeypatch.delenv(name, raising=False)
    import yuki.core.models as core_models

    monkeypatch.setattr(core_models, "get_key", lambda name: os.environ.get(name))   # never the user's registry
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


class _Recorder:
    """A Claude client stand-in: records what each call sent; ``fail`` models answer 500."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _check(self, params: dict[str, Any]) -> None:
        self.sent.append(params)
        if params.get("model") in self.fail:
            request = httpx.Request("POST", "https://bedrock.invalid")
            raise anthropic.InternalServerError("overloaded", response=httpx.Response(500, request=request),
                                                body=None)

    def _create(self, **params: Any) -> Any:
        self._check(params)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="advise",
                                                        input={"verdict": "proceed", "advice": "Fine."})],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))

    def _stream(self, **params: Any) -> Any:
        self._check(params)
        return _Stream()


class _Stream:
    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def __iter__(self) -> Any:
        return iter([])

    def get_final_message(self) -> Any:
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                                 cache_read_input_tokens=0,
                                                                 cache_creation_input_tokens=0))


def _failover(log: list | None = None, recorder: _Recorder | None = None, **kw: Any) -> FailoverClient:
    rec = recorder or _Recorder()
    return FailoverClient("us-east-1", lambda region: rec, role="thinker", retries=0, sleep=lambda s: None,
                          log=(lambda e, **f: log.append((e, f))) if log is not None else None, settings=Settings(),
                          **kw)


# -- D1: an unreadable ai.json is never silent --------------------------------------------------------------------------

BROKEN = ['{"roles": {"thinker": {"provider": "openai", "model": "gpt-5-mini"},}}',   # trailing comma
          "[]", "null", "\x00garbage\x01"]


@pytest.mark.parametrize("text", BROKEN)
def test_unreadable_ai_json_is_logged_once_and_the_doctor_warns(ai_dir: Path, text: str) -> None:
    from yuki.agent.providers.summary import doctor_lines

    (ai_dir / "ai.json").write_text(text, encoding="utf-8")
    log: list = []
    client = _failover(log)
    for _ in range(3):
        client.messages.create(model=HAIKU, max_tokens=5, messages=_user())
    assert [e for e, _f in log].count("ai_config_unreadable") == 1          # once per version of the file
    assert ai_config.route("thinker") is None                               # every job on Claude, as before
    status, lines, fixes = doctor_lines()
    assert status == "warn" and any("couldn't be read" in line for line in lines) and fixes


def test_unreadable_utf16_and_a_folder_in_its_place(ai_dir: Path) -> None:
    (ai_dir / "ai.json").write_text('{"version": 1}', encoding="utf-16")
    assert ai_config.problem() == "unreadable"
    (ai_dir / "ai.json").unlink()
    ai_config._cache.clear()
    (ai_dir / "ai.json").mkdir()
    assert ai_config.problem() == "unreadable" and ai_config.route("thinker") is None


def test_a_new_broken_version_is_noted_again(ai_dir: Path) -> None:
    path = ai_dir / "ai.json"
    path.write_text("{", encoding="utf-8")
    assert ai_config.take_note() is True and ai_config.take_note() is False
    path.write_text("{{", encoding="utf-8")                                  # another broken version
    assert ai_config.take_note() is True


def test_missing_file_is_silent_and_ok(ai_dir: Path) -> None:
    from yuki.agent.providers.summary import doctor_lines

    log: list = []
    _failover(log).messages.create(model=HAIKU, max_tokens=5, messages=_user())
    assert log == [] and ai_config.problem() is None
    assert doctor_lines()[0] == "ok"


def test_notepad_byte_order_mark_is_read(ai_dir: Path) -> None:
    (ai_dir / "ai.json").write_text('{"roles": {"thinker": {"provider": "ollama", "model": "llama3.2"}}}',
                                    encoding="utf-8-sig")
    chosen = ai_config.route("thinker")
    assert chosen is not None and chosen.primary.name == "ollama:llama3.2" and ai_config.problem() is None


def test_talker_client_logs_it_too(ai_dir: Path) -> None:
    (ai_dir / "ai.json").write_text("{", encoding="utf-8")
    log: list = []
    client = AsyncRoleClient("talker", SimpleNamespace(), settings=Settings(), log=lambda e, **f: log.append(e))
    assert client.route() is None and client.route() is None
    assert log == ["ai_config_unreadable"]


# -- V5: a locked ai.json -------------------------------------------------------------------------------------------------


def test_a_locked_read_keeps_the_last_good_choice(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _route("thinker", "ollama", "llama3.2")
    assert ai_config.route("thinker") is not None
    path = ai_dir / "ai.json"
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000))   # a writer touched it
    real = ai_config._read
    monkeypatch.setattr(ai_config, "_read", lambda p: (None, "locked"))
    chosen = ai_config.route("thinker")
    assert chosen is not None and chosen.primary.name == "ollama:llama3.2"   # never Claude for that call
    monkeypatch.setattr(ai_config, "_read", real)
    assert ai_config.route("thinker") is not None                          # read again on the next call


def test_a_locked_read_is_retried(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _route("thinker", "ollama", "llama3.2")
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "ai.json" and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError(13, "in use")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert ai_config.load()["roles"]["thinker"]["provider"] == "ollama" and calls["n"] == 2


def test_an_edit_never_writes_defaults_over_a_locked_file(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.providers import summary

    _route("thinker", "ollama", "llama3.2")
    before = (ai_dir / "ai.json").read_text(encoding="utf-8")
    monkeypatch.setattr(ai_config, "_read", lambda p: (None, "locked"))
    with pytest.raises(ValueError, match="busy"):
        summary.set_role("memory", "ollama", "llama3.2")
    with pytest.raises(ValueError, match="busy"):
        summary.set_custom("https://example.invalid/v1")
    assert (ai_dir / "ai.json").read_text(encoding="utf-8") == before        # nothing written


def test_save_retries_a_refused_replace(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace
    calls = {"n": 0}

    def refused(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(ai_config.os, "replace", refused)
    _route("thinker", "ollama", "llama3.2")
    assert calls["n"] == 3 and ai_config.route("thinker") is not None


# -- D2: the default path prices the judge and the second look as main does ---------------------------------------------


def test_default_path_failover_prices_the_judge_as_asked(ai_dir: Path) -> None:
    from yuki.agent.judge import Authorizer, decide

    rec = _Recorder(fail=(HAIKU,))
    client = _failover(recorder=rec, fallbacks={HAIKU: [{"model": SONNET}]})
    v = decide(client, {"model": HAIKU, "max_tokens": 5, "messages": _user()}, timeout_s=5.0)
    assert [p["model"] for p in rec.sent] == [HAIKU, SONNET]                 # Bedrock failed over to the sibling
    assert v.model is None                                                  # not routed: priced as asked (main)
    s = Settings()
    owner = SimpleNamespace(settings=s, _lock=threading.Lock(), cost_usd=0.0, unpriced=set())
    Authorizer._cost(owner, HAIKU, v)
    assert v.cost_usd == pytest.approx(round(s.estimate_cost(HAIKU, v.usage), 6)) and owner.unpriced == set()


def test_default_path_failover_prices_the_consult_as_asked(ai_dir: Path) -> None:
    from yuki.agent.consult import run_consult

    rec = _Recorder(fail=(SONNET,))
    client = _failover(recorder=rec, fallbacks={SONNET: [{"model": HAIKU}]})
    advice = run_consult(client, {"model": SONNET, "messages": [{"role": "user", "content": "s"}]}, signal="t",
                         timeout_s=5.0)
    assert advice.advice == "Fine." and advice.model is None and advice.returned_usd is None


# -- V4: the judge's and the second look's returned amount and unknown prices -------------------------------------------


def test_routed_judge_keeps_openrouters_returned_amount(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.judge import Authorizer, decide

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    chunks = [chunk({"content": "ok"}), chunk({}, finish="stop"), usage_chunk(1000, 100, cost=0.004)]
    with FakeOpenAI([Reply(chunks=chunks)]) as server:
        monkeypatch.setitem(PROVIDERS, "openrouter",
                            dataclasses.replace(PROVIDERS["openrouter"], base_url=server.url + "/api/v1"))
        _route("thinker", "openrouter", "vendor/typed-model")
        client = FailoverClient.from_settings(Settings(), factory=lambda region: None)
        v = decide(client, {"model": HAIKU, "max_tokens": 50, "messages": _user()}, timeout_s=10.0)
    assert v.model == "openrouter:vendor/typed-model" and v.returned_usd == pytest.approx(0.004)
    owner = SimpleNamespace(settings=Settings(), _lock=threading.Lock(), cost_usd=0.0, unpriced=set())
    Authorizer._cost(owner, HAIKU, v)                                        # priced on another thread's behalf
    assert v.cost_usd == pytest.approx(0.004) and owner.unpriced == set()


def test_unpriced_judge_makes_the_request_total_unknown() -> None:
    from yuki.agent.loop import Agent

    agent = SimpleNamespace(_unpriced=set(), authorizer=SimpleNamespace(unpriced={"openrouter:vendor/typed-model"}))
    assert Agent._all_unpriced(agent) == {"openrouter:vendor/typed-model"}
    assert Agent._all_unpriced(SimpleNamespace(_unpriced={"a"}, authorizer=None)) == {"a"}


# -- V2: unsigned reasoning taken out on the default path ---------------------------------------------------------------

HISTORY = [{"role": "user", "content": "hi"},
           {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": ""},
                                             {"type": "text", "text": "Hello."}]},
           {"role": "assistant", "content": [{"type": "thinking", "thinking": "mine", "signature": "c2lnbmVk"},
                                             {"type": "text", "text": "Again."}]},
           {"role": "user", "content": "and now?"}]


def test_default_path_strips_unsigned_thinking(ai_dir: Path) -> None:
    rec = _Recorder()
    _failover(recorder=rec).messages.create(model=HAIKU, max_tokens=5, messages=HISTORY)
    sent = rec.sent[0]["messages"]
    assert [b["type"] for b in sent[1]["content"]] == ["text"]               # another service's: taken out
    assert [b["type"] for b in sent[2]["content"]] == ["thinking", "text"]   # Claude's own (signed): kept


def test_default_path_leaves_a_clean_request_as_it_is(ai_dir: Path) -> None:
    rec = _Recorder()
    _failover(recorder=rec).messages.create(model=HAIKU, max_tokens=5, messages=HISTORY[2:])
    assert rec.sent[0]["messages"] == HISTORY[2:]


def test_default_talker_strips_unsigned_thinking(ai_dir: Path) -> None:
    sent: list = []

    async def create(**params: Any) -> Any:
        sent.append(params)
        return "opened"

    client = AsyncRoleClient("talker", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))),
                             settings=Settings())
    assert asyncio.run(client.beta.messages.create(model=HAIKU, stream=True, messages=HISTORY)) == "opened"
    assert [b["type"] for b in sent[0]["messages"][1]["content"]] == ["text"]


# -- V3: a failure after the reply began ---------------------------------------------------------------------------------

MID_STREAM_ERROR = {"id": "gen-1", "object": "chat.completion.chunk",
                    "error": {"code": "server_error", "message": "Provider disconnected"},
                    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]}


def test_talker_midstream_error_is_logged(ai_dir: Path) -> None:
    log: list = []

    async def sleep(_s: float) -> None:
        return None

    async def go(url: str) -> tuple[list[str], str]:
        client = AsyncRoleClient("talker", None, settings=Settings(), sleep=sleep,
                                 log=lambda e, **f: log.append((e, f)),
                                 factory=lambda pid: AsyncOpenAICompatClient(
                                     PROVIDERS[pid], base_url=url + "/v1", api_key=KEY,
                                     model_info=lambda m: known_model(pid, m)))
        stream = await client.beta.messages.create(model=HAIKU, max_tokens=20, stream=True, messages=_user())
        texts = []
        try:
            async for event in stream:
                delta = getattr(event, "delta", None)
                if getattr(delta, "text", None):
                    texts.append(delta.text)
        except anthropic.InternalServerError as exc:
            return texts, type(exc).__name__
        return texts, ""

    with FakeOpenAI([Reply(chunks=[chunk({"content": "I'll open the fi"}), MID_STREAM_ERROR])]) as server:
        _route("talker", "custom", "m", base_url=server.url + "/v1")
        texts, error = asyncio.run(go(server.url))
    assert texts == ["I'll open the fi"] and error == "InternalServerError"
    events = [e for e, _f in log]
    assert events == ["model_midstream_error"] and log[0][1]["target"] == "custom:m" and log[0][1]["status"] == 502


def test_thinker_midstream_error_is_logged(ai_dir: Path) -> None:
    log: list = []
    with FakeOpenAI([Reply(chunks=[chunk({"content": "Opening"}), MID_STREAM_ERROR])]) as server:
        _route("thinker", "custom", "m", base_url=server.url + "/v1")
        client = FailoverClient.from_settings(Settings(), factory=lambda region: None,
                                              log=lambda e, **f: log.append((e, f)))
        with pytest.raises(anthropic.InternalServerError):
            with client.messages.stream(model=HAIKU, max_tokens=20, messages=_user()) as stream:
                for _event in stream:
                    pass
    assert [e for e, _f in log] == ["model_midstream_error"] and log[0][1]["target"] == "custom:m"


def test_default_stream_logs_nothing_new(ai_dir: Path) -> None:
    log: list = []
    with _failover(log).messages.stream(model=HAIKU, max_tokens=5, messages=_user()) as stream:
        list(stream)
    assert log == []


# -- V1: a moved talker: no Pipecat timeout layer; a first-event limit --------------------------------------------------


def _layered(routed: bool) -> tuple[Any, list]:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.wrapped = 0

        async def _create_message_stream(self, api_call: Any, params: Any) -> Any:   # Pipecat's 5 s retry layer
            self.wrapped += 1
            return await api_call(**params)

    hooks = SimpleNamespace(fallbacks=lambda: [], sticky_index=lambda n: 0, routed=lambda: routed,
                            client_for=lambda region: None, model_ok=lambda i, t: None,
                            model_problem=lambda i, t, e, k: None)
    llm = _layered_service(Base, MouthMemory(), hooks)()
    llm._yuki_call = None
    calls: list = []

    async def api_call(**params: Any) -> Any:
        calls.append(params)

        async def events() -> Any:
            yield SimpleNamespace(type="message_start")
        return events()

    return llm, [api_call, calls]


def test_moved_talker_skips_pipecats_timeout_layer() -> None:
    llm, (api_call, calls) = _layered(routed=True)
    asyncio.run(llm._create_message_stream(api_call, {"model": "m", "stream": True}))
    assert llm.wrapped == 0 and len(calls) == 1


def test_talker_on_claude_keeps_pipecats_layer() -> None:
    llm, (api_call, calls) = _layered(routed=False)
    asyncio.run(llm._create_message_stream(api_call, {"model": "m", "stream": True}))
    assert llm.wrapped == 1 and len(calls) == 1                              # as before


class _HungOrQuick:
    """An async provider client: ``hung`` never sends a first event, else one text reply."""

    def __init__(self, hung: bool) -> None:
        self.hung = hung
        self.posts = 0
        self.closed = 0
        owner = self

        class Raw:
            def __aiter__(self) -> "Raw":
                return self

            async def __anext__(self) -> Any:
                if owner.hung:
                    await asyncio.sleep(30)
                if getattr(self, "done", False):
                    raise StopAsyncIteration
                self.done = True
                return SimpleNamespace(type="message_start")

            async def close(self) -> None:
                owner.closed += 1

        async def create(**params: Any) -> Any:
            owner.posts += 1
            return Raw()

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))
        self.messages = self.beta.messages


def _hung_talker(ai_dir: Path, clients: dict[str, _HungOrQuick], log: list, backup: bool) -> AsyncRoleClient:
    async def sleep(_s: float) -> None:
        return None

    _route("talker", "openai", "gpt-5-mini", backup=("ollama", "llama3.2") if backup else None)
    return AsyncRoleClient("talker", None, settings=Settings(), sleep=sleep, log=lambda e, **f: log.append(e),
                           factory=lambda pid: clients[pid], first_event_s=0.2)


def test_hung_primary_goes_straight_to_the_backup(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    clients = {"openai": _HungOrQuick(hung=True), "ollama": _HungOrQuick(hung=False)}
    log: list = []
    client = _hung_talker(ai_dir, clients, log, backup=True)

    async def go() -> list:
        stream = await client.beta.messages.create(model=HAIKU, max_tokens=5, stream=True, messages=_user())
        return [e.type async for e in stream]

    assert asyncio.run(go()) == ["message_start"]
    assert clients["openai"].posts == 1 and clients["openai"].closed == 1   # given up on once, connection let go
    assert clients["ollama"].posts == 1 and "model_failover" in log and client.last_model() == "ollama:llama3.2"


def test_hung_primary_without_backup_is_retried_then_raises(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    clients = {"openai": _HungOrQuick(hung=True)}
    log: list = []
    client = _hung_talker(ai_dir, clients, log, backup=False)
    with pytest.raises(FirstEventTimeout):
        asyncio.run(client.beta.messages.create(model=HAIKU, max_tokens=5, stream=True, messages=_user()))
    assert clients["openai"].posts == 3 and log.count("model_retry") == 2    # model_retries=2: bounded, ~0.6 s


def test_the_voice_talker_gets_the_limit() -> None:
    from yuki.agent.providers.client import TALKER_FIRST_EVENT_S, role_async_client

    assert role_async_client("talker", None).first_event_s == TALKER_FIRST_EVENT_S
    assert role_async_client("memory", None).first_event_s is None


# -- D3: environment stand-ins are not keys ------------------------------------------------------------------------------


@pytest.mark.parametrize("stand_in", ["lm-studio", "sk-no-key-required", "not-needed"])
def test_short_environment_stand_ins_are_not_hidden(ai_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                    stand_in: str) -> None:
    from yuki.agent.providers.keys import secret_values
    from yuki.memory import secret_filter

    monkeypatch.setenv("OPENAI_API_KEY", stand_in)
    monkeypatch.setattr(secret_filter, "_saved_cache", [-1e9, ()])
    assert stand_in not in secret_values()
    text = f"Mira said: open {stand_in}, then set the model"
    assert secret_filter.redact_text(text) == text


def test_a_real_length_environment_key_is_still_hidden(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.providers.keys import secret_values
    from yuki.memory import secret_filter

    value = "zz" + "Q7x" * 12                                                 # invented, no vendor format
    monkeypatch.setenv("DEEPSEEK_API_KEY", value)
    monkeypatch.setattr(secret_filter, "_saved_cache", [-1e9, ()])
    assert value in secret_values()
    assert value not in secret_filter.redact_text(f"the key is {value} ok")


# -- V6: a Modelfile's short context -------------------------------------------------------------------------------------


def test_modelfile_short_context_gets_its_own_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover, "ollama_server_context", lambda: 65536)
    info = ModelInfo("m", "m", tools=True, context=40960, run_context=16384)
    words = discover.fit_for("thinker", info, "ollama")
    assert "set up to read only 16,384 tokens" in words and "Ollama's settings" not in words
    roomy = ModelInfo("m", "m", tools=True, context=131072, run_context=None)
    assert discover.fit_for("thinker", roomy, "ollama") == ""                 # the server's 64k is enough
    monkeypatch.setattr(discover, "ollama_server_context", lambda: None)
    assert "Ollama's settings" in discover.fit_for("thinker", roomy, "ollama")   # F5's advice where it helps
