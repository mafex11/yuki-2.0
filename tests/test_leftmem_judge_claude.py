"""The safety check stays on Claude (leftMem, 4 Oct decision: "let it stay for claude for now").

When Settings → AI moves "Doing things" to another AI, the judge (yuki.agent.judge, the check that approves the
agent's actions) still goes the default Claude way: the agent's Bedrock FailoverClient with its normal chain and
pricing (``FailoverClient.claude_only``). The second look and the helpers keep the routed client. On the default
path nothing changes: the judge's requests are the same bytes, to the same place, as main sends them.

Offline only: Bedrock stand-ins record what they are sent; a fake OpenAI-protocol server (tests/_fake_openai.py)
stands in for the other AI and must see no judge request. Invented names; no network, nothing on the desktop.
"""

from __future__ import annotations

import dataclasses
import json
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

from _fake_openai import FakeOpenAI, Reply, completion  # noqa: E402

from yuki.agent.failover import ClaudePath, FailoverClient, claude_path  # noqa: E402
from yuki.agent.providers import config as ai_config  # noqa: E402
from yuki.agent.providers import discover  # noqa: E402
from yuki.agent.providers.catalog import PROVIDERS  # noqa: E402
from yuki.config import Settings  # noqa: E402

KEY = "sk-test-InventedKeyForTests0123456789abcd"   # invented; never a real key
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "global.anthropic.claude-sonnet-5"

_WORDS = [{"how": "heard", "text": "text Ravi on Chirp and ask when he's joining the huddle"}]
_SEND = {"element": 120, "text": "when are you joining the huddle?", "press_enter": True}
_TARGET = {"hwnd": 7001, "app": "Chirp.exe", "title": "Chirp", "element": 'Edit "Type a message to Ravi"'}


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


def _route(role: str, provider: str, model: str, base_url: str = "") -> None:
    data = ai_config.load()
    data["roles"][role] = {"provider": provider, "model": model, "backup_provider": "", "backup_model": ""}
    if base_url:
        data["custom"] = {"label": "Test gateway", "base_url": base_url}
    ai_config.save(data)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=900, output_tokens=40, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


class _Stream:
    def __init__(self, data: dict[str, Any], tool: str) -> None:
        self.data, self.tool = data, tool

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_usage()))
        text = json.dumps(self.data)
        for i in range(0, len(text), 9):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 9]))
        yield SimpleNamespace(type="message_stop")

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=self.tool, input=self.data)],
                               usage=_usage())


class _Bedrock:
    """A Bedrock stand-in for every region: records (region, params) of each call; ``fail`` models answer 500."""

    HOLD = {"decision": "ask_user", "question": "Send this to Ravi?", "sends": True, "scope": "call",
            "why": "they did not say the words"}
    REVIEW = {"asked": "ask Ravi when he joins", "toward": True, "lasting": True, "asked_effect": True,
              "question": "Send this to Ravi?", "sends": True, "scope": "call"}

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def factory(self, region: str) -> Any:
        def record(params: dict[str, Any]) -> None:
            self.sent.append((region, json.loads(json.dumps(params, default=str))))
            if params.get("model") in self.fail:
                request = httpx.Request("POST", "https://bedrock.invalid")
                raise anthropic.InternalServerError("overloaded", response=httpx.Response(500, request=request),
                                                    body=None)

        def stream(**params: Any) -> _Stream:
            record(params)
            tool = params["tool_choice"]["name"]
            return _Stream(self.REVIEW if tool == "review" else self.HOLD, tool)

        def create(**params: Any) -> Any:
            record(params)
            return SimpleNamespace(content=[], usage=_usage(), stop_reason="end_turn")

        return SimpleNamespace(messages=SimpleNamespace(stream=stream, create=create))


def _agent_client(bedrock: _Bedrock, **kw: Any) -> FailoverClient:
    """The agent's own client, as the loop builds it (``from_settings``, role ``thinker``), on stand-in regions."""
    client = FailoverClient.from_settings(Settings(), factory=bedrock.factory, **kw)
    client._sleep = lambda s: None
    return client


def _authorizer(tmp_path: Path, client: Any) -> Any:
    from yuki.agent.judge import Authorizer, PolicyReader

    return Authorizer(lambda: client, Settings(), policy=PolicyReader(tmp_path / "no-policy.json"))


def _judged(tmp_path: Path, client: Any) -> Any:
    return _authorizer(tmp_path, client).check("type_text", _SEND, words=_WORDS, target=_TARGET)


def _expected_requests() -> list[dict[str, Any]]:
    """What main's judge sends for this call (its two stages), built by the judge's own request builder."""
    from yuki.agent.judge import POLICY_DEFAULTS, judge_request
    from yuki.agent.tools import call_traits

    traits = call_traits("type_text", _SEND, role=None, typed_pending=False, files_read=False)
    model = Settings().judge_model
    out = []
    for review in (False, True):
        params = judge_request(_WORDS, "type_text", _SEND, target=_TARGET, levels=dict(POLICY_DEFAULTS),
                               traits=traits, model=model, review=review)
        out.append(json.loads(json.dumps(params, default=str)))
    return out


# -- the thinker moved: the judge stays on Bedrock -----------------------------------------------------------------------


def test_thinker_on_another_service_judge_goes_to_bedrock_with_mains_params(ai_dir: Path, tmp_path: Path) -> None:
    with FakeOpenAI([]) as server:
        _route("thinker", "custom", "gateway-model", base_url=server.url + "/v1")
        bedrock = _Bedrock()
        client = _agent_client(bedrock)
        v = _judged(tmp_path, client)
        posts = server.posts()
    assert posts == []                                                      # the other AI saw no judge request
    assert v is not None and v.error is None and v.decision == "ask_user"   # judged (held: both stages ran)
    assert [r for r, _p in bedrock.sent] == [Settings().aws_region] * 2
    assert [p for _r, p in bedrock.sent] == _expected_requests()            # main's exact params, both stages
    assert [p["model"] for _r, p in bedrock.sent] == [Settings().judge_model] * 2
    assert v.model is None                                                  # priced as the Claude model asked


def test_thinker_on_ollama_judge_goes_to_bedrock(ai_dir: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeOpenAI([]) as server:   # Ollama's address pointed here: a leaked judge request would be recorded
        monkeypatch.setitem(PROVIDERS, "ollama", dataclasses.replace(PROVIDERS["ollama"], base_url=server.url + "/v1"))
        _route("thinker", "ollama", "qwen3:14b")
        bedrock = _Bedrock()
        client = _agent_client(bedrock)
        v = _judged(tmp_path, client)
        posts = server.posts()
    assert posts == [] and v.error is None
    assert [p for _r, p in bedrock.sent] == _expected_requests()


def test_thinker_on_openrouter_judge_priced_as_claude(ai_dir: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with FakeOpenAI([]) as server:
        monkeypatch.setitem(PROVIDERS, "openrouter",
                            dataclasses.replace(PROVIDERS["openrouter"], base_url=server.url + "/api/v1"))
        _route("thinker", "openrouter", "vendor/typed-model")
        bedrock = _Bedrock()
        auth = _authorizer(tmp_path, _agent_client(bedrock))
        v = auth.check("type_text", _SEND, words=_WORDS, target=_TARGET)
        posts = server.posts()
    assert posts == [] and len(bedrock.sent) == 2
    s = Settings()
    assert v.model is None and auth.unpriced == set()
    # both stages costed at the Claude judge model's own Bedrock price (the normal table), never the thinker's
    assert auth.cost_usd == pytest.approx(2 * s.estimate_cost(s.judge_model, {"input_tokens": 900, "output_tokens": 40,
                                                                               "cache_read_tokens": 0,
                                                                               "cache_write_tokens": 0}), rel=1e-6)


def test_judge_warm_up_and_forced_hand_check_use_bedrock_too(ai_dir: Path, tmp_path: Path) -> None:
    """The warm-up writes the judge's own Claude cache; the voice hands' forced check (hands._judge_client is the
    authorizer's model_client) goes through the same pinned client."""
    from yuki.agent.judge import POLICY_DEFAULTS, decide, judge_request
    from yuki.agent.tools import tool_traits

    with FakeOpenAI([]) as server:
        _route("thinker", "custom", "gateway-model", base_url=server.url + "/v1")
        bedrock = _Bedrock()
        auth = _authorizer(tmp_path, _agent_client(bedrock))
        assert auth.warm() is not None
        forced = auth.model_client()
        assert isinstance(forced, ClaudePath)
        params = judge_request(_WORDS, "click", {"element": 3}, target=_TARGET, levels=dict(POLICY_DEFAULTS),
                               traits=tool_traits("click", {"element": 3}), model=Settings().judge_model)
        decide(forced, params, timeout_s=5.0, need=())
        posts = server.posts()
    assert posts == [] and len(bedrock.sent) == 2
    assert bedrock.sent[0][1]["max_tokens"] == 1 and bedrock.sent[0][1]["model"] == Settings().judge_model


def test_second_look_and_helpers_stay_with_the_thinker(ai_dir: Path, tmp_path: Path) -> None:
    """Pinning the judge changes nothing for the agent's own client: its calls (the thinker, the second look, the
    helpers) still go where Settings → AI sent them, also after the judge ran."""
    from yuki.agent.consult import run_consult

    advise = completion(None, tool_calls=[{"id": "c1", "type": "function", "function": {
        "name": "advise", "arguments": json.dumps({"verdict": "proceed", "advice": "Fine."})}}])
    with FakeOpenAI([Reply(body=advise)]) as server:
        _route("thinker", "custom", "gateway-model", base_url=server.url + "/v1")
        bedrock = _Bedrock()
        client = _agent_client(bedrock)
        _judged(tmp_path, client)
        judge_calls = len(bedrock.sent)
        advice = run_consult(client, {"model": SONNET, "max_tokens": 200,
                                      "messages": [{"role": "user", "content": "summary"}]}, signal="send",
                             timeout_s=10.0)
        posts = server.posts()
    assert judge_calls == 2 and len(bedrock.sent) == 2                      # the second look never reached Bedrock
    assert len(posts) == 1 and posts[0]["json"]["model"] == "gateway-model"
    assert advice.model == "custom:gateway-model"


# -- the default path: byte-identical to main ----------------------------------------------------------------------------


def _main_judge(client: Any, tmp_path: Path) -> Any:
    """Main's judge: the same Authorizer code with the agent's client handed straight to the model calls (what
    ``Authorizer._client`` was before this change)."""
    auth = _authorizer(tmp_path, client)
    auth._client = lambda: client
    return auth


@pytest.mark.parametrize("fail", [(), (Settings().judge_model,)], ids=["plain", "bedrock-failover"])
def test_default_path_byte_identical_to_main(ai_dir: Path, tmp_path: Path, fail: tuple[str, ...]) -> None:
    assert not ai_config.config_path().exists()
    fallbacks = {Settings().judge_model: [{"model": SONNET}]}
    main_rec, new_rec = _Bedrock(fail=fail), _Bedrock(fail=fail)
    main_client = _agent_client(main_rec)
    main_client.fallbacks = fallbacks
    new_client = _agent_client(new_rec)
    new_client.fallbacks = fallbacks
    main_auth, new_auth = _main_judge(main_client, tmp_path), _authorizer(tmp_path, new_client)
    v_main = main_auth.check("type_text", _SEND, words=_WORDS, target=_TARGET)
    v_new = new_auth.check("type_text", _SEND, words=_WORDS, target=_TARGET)
    dump = lambda rec: json.dumps(rec.sent, sort_keys=True, ensure_ascii=False)   # noqa: E731
    assert dump(new_rec) == dump(main_rec)                                  # same bytes, same regions, same order
    models = [p["model"] for _r, p in new_rec.sent]
    # failing: retried on the judge model, then its Bedrock backup, which the review stage goes straight to (sticky)
    assert models == ([Settings().judge_model] * 2 if not fail else [Settings().judge_model] * 3 + [SONNET] * 2)
    keep = ("decision", "scope", "sends", "why", "question", "source", "reasons", "usage", "cost_usd", "error")
    assert {k: v_new.facts()[k] for k in keep} == {k: v_main.facts()[k] for k in keep}
    assert v_new.model is None and v_main.model is None
    assert new_auth.cost_usd == pytest.approx(main_auth.cost_usd) and new_auth.unpriced == main_auth.unpriced
    assert new_client.counters == main_client.counters


def test_default_path_pinned_and_plain_send_the_same(ai_dir: Path) -> None:
    """``claude_only`` on the default path is the plain client: create and stream, a Haiku thinking budget, another
    service's unsigned reasoning taken out the same way, the same per-thread last model."""
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": ""},
                                                 {"type": "text", "text": "Hello."}]},
               {"role": "user", "content": "and now?"}]
    params = {"model": HAIKU, "max_tokens": 9, "messages": history,
              "thinking": {"type": "enabled", "budget_tokens": 1024},
              "tool_choice": {"type": "tool", "name": "verdict"}}
    a, b = _Bedrock(), _Bedrock()
    plain, pinned = _agent_client(a), _agent_client(b).claude_only()
    plain.messages.create(**params)
    pinned.messages.create(**params)
    with plain.messages.stream(**params) as s:
        list(s)
    with pinned.messages.stream(**params) as s:
        list(s)
    assert json.dumps(a.sent, sort_keys=True) == json.dumps(b.sent, sort_keys=True)
    assert [b_["type"] for b_ in b.sent[0][1]["messages"][1]["content"]] == ["text"]
    assert plain.last_model() == pinned.last_model() == HAIKU
    assert pinned.last_routed_model() is None and pinned.prompt_caching() == "explicit"


def test_claude_path_helper() -> None:
    a = _Bedrock()
    client = FailoverClient("us-east-1", a.factory, role="thinker")
    view = claude_path(client)
    assert isinstance(view, ClaudePath) and claude_path(view) is view and client.claude_only() is view
    stand_in = SimpleNamespace(messages=None)
    assert claude_path(stand_in) is stand_in                                # a test's or a replay's double as is
    assert view.region == "us-east-1" and view.counters is client.counters


def test_pinned_client_shares_sticky_failover(ai_dir: Path) -> None:
    """One outage, one set of retries: a failover the judge found is the agent's too (the same sticky state)."""
    rec = _Bedrock(fail=(HAIKU,))
    client = _agent_client(rec)
    client.fallbacks = {HAIKU: [{"model": SONNET}]}
    client.claude_only().messages.create(model=HAIKU, max_tokens=5, messages=[])
    rec.fail = ()
    client.messages.create(model=HAIKU, max_tokens=5, messages=[])
    assert [p["model"] for _r, p in rec.sent][-1] == SONNET                 # straight to the backup that worked


# -- the words ------------------------------------------------------------------------------------------------------------


def test_settings_and_doctor_say_it(ai_dir: Path) -> None:
    from yuki.agent.providers import summary

    lines = {r["role"]: r for r in summary.roles_state()}
    assert lines["thinker"]["note"] == "The safety check that approves actions always uses Claude."
    assert lines["talker"]["note"] == "" and lines["memory"]["note"] == ""
    status, doctor, _fixes = summary.doctor_lines()
    assert status == "ok" and doctor[-1] == "Safety check that approves actions: always Claude"
    _route("thinker", "ollama", "qwen3:14b")
    assert summary.role_line("thinker")["note"] == summary.SAFETY_WORDS
    assert summary.doctor_lines()[1][-1] == summary.SAFETY_DOCTOR_LINE


def test_judge_lock_is_per_authorizer_thread_safe(ai_dir: Path, tmp_path: Path) -> None:
    """Judges on two threads (the agent's and the voice hands') share the pinned view safely."""
    rec = _Bedrock()
    client = _agent_client(rec)
    auth = _authorizer(tmp_path, client)
    views: list[Any] = []
    threads = [threading.Thread(target=lambda: views.append(auth.model_client())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(v) for v in views}) == 1
