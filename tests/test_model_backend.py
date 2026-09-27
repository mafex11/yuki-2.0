"""The model backend switch (yuki.core.models): id mapping, backend choice, failover chains, clients."""

from __future__ import annotations

import json

import pytest

from yuki.agent.failover import FailoverClient
from yuki.core import models


@pytest.mark.parametrize("bedrock, api", [
    ("global.anthropic.claude-sonnet-5", "claude-sonnet-5"),
    ("us.anthropic.claude-sonnet-5", "claude-sonnet-5"),
    ("global.anthropic.claude-opus-5", "claude-opus-5"),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001"),
    ("anthropic.claude-opus-5", "claude-opus-5"),
    ("claude-sonnet-5", "claude-sonnet-5"),          # already an API id
    ("claude-haiku-4-5", "claude-haiku-4-5"),
])
def test_api_model_id(bedrock: str, api: str) -> None:
    assert models.api_model_id(bedrock) == api


def _no_keys(monkeypatch: pytest.MonkeyPatch, tmp_path, keys: dict[str, str] | None = None) -> None:
    monkeypatch.delenv("YUKI_MODEL_BACKEND", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    keys = keys or {}
    monkeypatch.setattr(models, "get_key", lambda name: keys.get(name))


def test_backend_from_keys(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _no_keys(monkeypatch, tmp_path)
    assert models.resolve_backend() == "bedrock"
    _no_keys(monkeypatch, tmp_path, {models.ANTHROPIC_KEY: "sk-test"})
    assert models.resolve_backend() == "anthropic"
    _no_keys(monkeypatch, tmp_path, {models.ANTHROPIC_KEY: "sk-test", models.BEDROCK_KEY: "tok"})
    assert models.resolve_backend() == "bedrock"


def test_backend_saved_and_forced(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _no_keys(monkeypatch, tmp_path, {models.BEDROCK_KEY: "tok"})
    (tmp_path / "Yuki").mkdir()
    (tmp_path / "Yuki" / "settings.json").write_text(json.dumps({"model_backend": "anthropic"}), encoding="utf-8")
    assert models.resolve_backend() == "anthropic"
    monkeypatch.setenv("YUKI_MODEL_BACKEND", "bedrock")
    assert models.resolve_backend() == "bedrock"


def test_settings_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.config import Settings

    assert Settings(model_backend="anthropic").model_backend == "anthropic"
    with pytest.raises(ValueError):
        Settings(model_backend="openai")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = self
        self.beta = type("B", (), {"messages": self})()

    def create(self, **params):
        self.calls.append(params)
        return "ok"

    stream = count_tokens = create


def test_mapped_client_rewrites_model() -> None:
    inner = _Recorder()
    client = models.MappedClient(inner)
    client.messages.create(model="global.anthropic.claude-sonnet-5", max_tokens=1, messages=[])
    client.beta.messages.create(model="global.anthropic.claude-haiku-4-5-20251001-v1:0", max_tokens=1)
    client.messages.count_tokens(model="us.anthropic.claude-opus-5", messages=[])
    assert [c["model"] for c in inner.calls] == ["claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-5"]


def test_regionless_chain_dedupes_same_model() -> None:
    fallbacks = {"global.anthropic.claude-sonnet-5": [
        {"model": "global.anthropic.claude-sonnet-5", "region": "us-east-1"},
        {"model": "us.anthropic.claude-sonnet-5", "region": "us-east-1"},
        {"model": "global.anthropic.claude-opus-5", "region": None},
    ]}
    bedrock = FailoverClient("ap-south-1", lambda r: None, fallbacks=fallbacks)
    assert len(bedrock.chain("global.anthropic.claude-sonnet-5")) == 4
    api = FailoverClient("ap-south-1", lambda r: None, fallbacks=fallbacks, regionless=True)
    chain = api.chain("global.anthropic.claude-sonnet-5")
    assert [t.model for t in chain] == ["global.anthropic.claude-sonnet-5", "global.anthropic.claude-opus-5"]
    assert {t.region for t in chain} == {"ap-south-1"}


def test_from_settings_builds_backend_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.config import Settings

    monkeypatch.setattr(models, "get_key", lambda name: "sk-ant-test" if name == models.ANTHROPIC_KEY else None)
    client = FailoverClient.from_settings(Settings(model_backend="anthropic"))
    assert client.regionless
    real = client.client_for("ap-south-1")
    assert isinstance(real, models.MappedClient)
    bedrock = FailoverClient.from_settings(Settings(model_backend="bedrock"), factory=lambda r: r)
    assert not bedrock.regionless


def test_async_client_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "get_key", lambda name: "sk-ant-test")
    client = models.async_client(backend="anthropic")
    assert isinstance(client, models.MappedClient)
    import anthropic

    assert isinstance(client._inner, anthropic.AsyncAnthropic)
    assert isinstance(models.async_client("ap-south-1", backend="bedrock"), anthropic.AsyncAnthropicBedrock)
