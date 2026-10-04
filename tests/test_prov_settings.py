"""Settings → AI and the doctor (report 67): the window's ai_* actions write ai.json and the DPAPI key file, the page
data never carries a key, the doctor reports each job's AI and whether its key is there (never the key).

Temp folders only (``YUKI_AI_DIR`` per test); a fake OpenAI-protocol server for the Test button; no network.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _fake_openai import FakeOpenAI, Reply, completion  # noqa: E402

from yuki.agent.providers import config as ai_config  # noqa: E402
from yuki.agent.providers import keys as ai_keys  # noqa: E402
from yuki.agent.providers import summary  # noqa: E402
from yuki.ui import web_window  # noqa: E402

KEY = "sk-proj-InventedSettingsKey0123456789abcdefXYZ"   # invented


@pytest.fixture()
def ai_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "ai"
    monkeypatch.setenv("YUKI_AI_DIR", str(folder))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                 "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    import yuki.core.models as core_models

    monkeypatch.setattr(core_models, "get_key", lambda name: None)
    ai_config._cache.clear()
    return folder


class _Host:
    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, message: dict) -> None:
        self.posted.append(message)

    def kinds(self, kind: str) -> list[dict]:
        return [m for m in self.posted if m.get("kind") == kind]


def _window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[web_window.WebWindow, _Host]:
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               policy_path=tmp_path / "autonomy.json")
    monkeypatch.setattr(win, "_open_memory", lambda: None)
    host = _Host()
    win.host = host
    return win, host


def _act(win: web_window.WebWindow, host: _Host, action: str, **fields: Any) -> dict:
    n = len(host.kinds("reply"))
    win.on_message({"op": "act", "id": f"{action}-{n}", "action": action, **fields})
    end = time.time() + 10
    while time.time() < end and len(host.kinds("reply")) <= n:
        time.sleep(0.02)
    return host.kinds("reply")[-1]


def test_role_choices_are_written_and_read_back(ai_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    win, host = _window(tmp_path, monkeypatch)
    r = _act(win, host, "ai_role", role="thinker", provider="openrouter", model="vendor/quick-chat:free")
    assert r["ok"] and r["role"]["provider"] == "openrouter" and r["role"]["model"] == "vendor/quick-chat:free"
    assert r["role"]["warning"].startswith("No OpenRouter key yet")             # said in plain words
    saved = json.loads(ai_config.config_path().read_text(encoding="utf-8"))
    assert saved["roles"]["thinker"]["provider"] == "openrouter"
    assert ai_config.route("thinker").primary.name == "openrouter:vendor/quick-chat:free"
    r = _act(win, host, "ai_role", role="thinker", provider="openrouter", model="vendor/quick-chat:free",
             backup_provider="default", backup_model="")
    assert ai_config.route("thinker").backup.provider == "default"
    r = _act(win, host, "ai_role", role="thinker", provider="default")
    assert r["ok"] and ai_config.route("thinker") is None                       # back to Claude as before
    bad = _act(win, host, "ai_role", role="cook", provider="openai", model="x")
    assert bad["ok"] is False and bad["error"] == "Unknown job"


def test_keys_from_the_window_are_encrypted_and_never_echoed(ai_dir: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    win, host = _window(tmp_path, monkeypatch)
    r = _act(win, host, "ai_key", provider="openai", key=KEY)
    assert r["ok"] and r["key"] == "saved" and r["key_words"] == "Saved on this PC, encrypted"
    assert KEY not in json.dumps(r)                                              # the reply never carries it
    assert KEY.encode() not in ai_keys.key_path().read_bytes()
    assert ai_keys.get_key("openai") == KEY
    assert KEY not in json.dumps(win.events) and KEY not in json.dumps(win.metrics, default=str)
    assert KEY not in json.dumps(summary.page())
    r = _act(win, host, "ai_key", provider="openai", key="")
    assert r["ok"] and r["key"] == "missing" and ai_keys.get_key("openai") is None
    r = _act(win, host, "ai_key", provider="openai", key="has a space")
    assert r["ok"] is False and "spaces" in r["error"]


def test_custom_address_and_test_button(ai_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    win, host = _window(tmp_path, monkeypatch)
    assert _act(win, host, "ai_custom", base_url="ftp://nope")["ok"] is False
    assert "key" in _act(win, host, "ai_custom", base_url="https://user:pw@example.invalid/v1")["error"]
    with FakeOpenAI([Reply(body=completion("ok"))]) as server:
        r = _act(win, host, "ai_custom", base_url=server.url + "/v1/")
        assert r["ok"] and ai_config.custom_base_url() == server.url + "/v1"
        t = _act(win, host, "ai_test", provider="custom", model="m")
    assert t["ok"] and t["works"] is True and t["words"].startswith("Works.")
    t = _act(win, host, "ai_test", provider="gemini")
    assert t["works"] is False and t["words"] == "No Google Gemini key yet. Paste it above and press Save."


def test_model_lists_and_ollama_status(ai_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.providers import discover
    from yuki.agent.providers.catalog import ModelInfo

    win, host = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(discover, "list_models", lambda provider, refresh=False: None if provider == "ollama" else [
        ModelInfo("vendor/x:free", "X", tools=True, free=True, price={"input": 0.0, "output": 0.0})])
    r = _act(win, host, "ai_models", provider="ollama")
    assert r["ok"] and r["running"] is False and r["status"] == "Ollama isn't running" and r["models"] == []
    r = _act(win, host, "ai_models", provider="openrouter")
    assert r["models"][0]["free"] is True and r["models"][0]["price"] == "Free"


def test_build_settings_carries_ai_without_keys(ai_dir: Path) -> None:
    from yuki.ui import web_data

    ai_keys.set_key("openrouter", KEY)
    summary.set_role("memory", "ollama", "llama3.2:3b")
    page = web_data._ai_page()
    assert [r["role"] for r in page["roles"]] == ["talker", "thinker", "memory"]
    assert page["roles"][2]["provider_words"] == "Ollama (on this PC)" and page["roles"][2]["key"] == "not_needed"
    router = next(p for p in page["providers"] if p["id"] == "openrouter")
    assert router["key"] == "saved" and router["key_words"] == "Saved on this PC, encrypted"
    assert KEY not in json.dumps(page)
    assert page["apply"] == "Changes apply from your next request. No restart needed."


def test_doctor_reports_each_job_and_never_the_key(ai_dir: Path) -> None:
    from yuki.core.doctor import AI_ROLES_CHECK, FAIL, OK, check_ai_roles

    c = check_ai_roles(offline=True)
    assert c.name == AI_ROLES_CHECK and c.status == OK
    assert c.detail == "Voice replies: Claude (as now); Doing things: Claude (as now); Memory upkeep: Claude (as now)"
    summary.set_role("thinker", "openai", "gpt-5-mini")
    c = check_ai_roles(offline=True)
    assert c.status == FAIL and "Doing things: OpenAI, gpt-5-mini (NO KEY)" in c.detail and "Settings" in c.fix
    ai_keys.set_key("openai", KEY)
    c = check_ai_roles(offline=True)
    assert c.status == OK and "Doing things: OpenAI, gpt-5-mini (key saved)" in c.detail
    assert KEY not in c.detail and KEY not in c.fix
    summary.set_role("memory", "ollama", "llama3.2:3b")
    from yuki.agent.providers import discover

    real = discover.ollama_models
    discover.ollama_models = lambda **kw: None
    try:
        c = check_ai_roles(offline=False)
    finally:
        discover.ollama_models = real
    assert c.status == FAIL and "Ollama isn't running" in c.fix


def test_doctor_runs_the_ai_line_in_the_checklist(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.config import Settings
    from yuki.core import doctor

    for name in dir(doctor):   # every other check stubbed: nothing on this PC is read
        if name.startswith("check_") and name != "check_ai_roles":
            monkeypatch.setattr(doctor, name, lambda *a, _n=name, **k: doctor.Check(_n, doctor.OK, "stub"))
    results = doctor.run_checks(Settings(), offline=True, timeout_s=20.0)
    names = [c.name for c in results]
    assert doctor.AI_ROLES_CHECK in names and names.index(doctor.AI_ROLES_CHECK) == names.index(doctor.MODELS_CHECK) + 1


def test_other_service_without_an_address_is_blocked(ai_dir: Path) -> None:
    from yuki.core.doctor import FAIL, OK, check_ai_roles

    line = summary.set_role("memory", "custom", "some-model")
    assert line["blocked"] and "no address yet" in line["warning"]
    assert check_ai_roles(offline=True).status == FAIL
    summary.set_custom("https://gateway.example.invalid/v1", "My gateway")
    line = summary.role_line("memory")
    assert not line["blocked"] and line["provider_words"] == "My gateway"
    assert check_ai_roles(offline=True).status == OK   # memory upkeep: tool use isn't needed, so no warning


def test_settings_edits_from_many_threads_all_land(ai_dir: Path) -> None:
    """The window runs each action on its own thread: two quick picks never lose one another."""
    import threading

    picks = [("talker", "openai", "gpt-5-mini"), ("thinker", "gemini", "gemini-2.5-flash"),
             ("memory", "ollama", "llama3.2"), ("talker", "openai", "gpt-5-mini")]
    threads = [threading.Thread(target=summary.set_role, args=p) for p in picks * 5]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    roles = ai_config.load()["roles"]
    assert [roles[r]["provider"] for r in ("talker", "thinker", "memory")] == ["openai", "gemini", "ollama"]
