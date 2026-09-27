"""First-run onboarding (yuki/onboarding): offline.

Registry writes go to a scratch key (HKCU\\Software\\YukiTest\\Environment, deleted after);
the real HKCU\\Environment, %LOCALAPPDATA%\\Yuki and memory are never touched. The
dialog runs offscreen with the key test and the download stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import winreg
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import yuki.onboarding as ob
from yuki.onboarding import consent, prefs
from yuki.onboarding.envvars import ENV_KEY, UserEnv

SCRATCH_PARENT = r"Software\YukiTest"
SCRATCH_KEY = SCRATCH_PARENT + r"\Environment"
FAKE_ANTHROPIC = "sk-ant-api03-TESTKEY-not-real-0123456789abcdefghijklmnop"
FAKE_BEDROCK = "ABSKtestbedrocktoken-not-real-0123456789"
FAKE_DEEPGRAM = "dg-test-not-real-0123456789abcdef"
FAKE_ELEVEN = "sk_eleven_test_not_real_0123456789"


def _delete_tree(root: int, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + child)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


@pytest.fixture
def scratch_env():
    _delete_tree(winreg.HKEY_CURRENT_USER, SCRATCH_PARENT)
    environ: dict[str, str] = {}
    env = UserEnv(SCRATCH_KEY, environ=environ)
    yield env
    _delete_tree(winreg.HKEY_CURRENT_USER, SCRATCH_PARENT)


# -- needs_onboarding ------------------------------------------------------------------------


def test_needs_onboarding_without_any_model_key():
    assert ob.needs_onboarding(get_key=lambda name: None) is True


@pytest.mark.parametrize("present", [ob.ANTHROPIC_KEY, ob.BEDROCK_KEY])
def test_existing_user_with_a_key_is_never_asked(present, tmp_path):
    # no marker at all (an existing user who never saw onboarding): still not asked
    assert ob.load_marker(tmp_path) is None
    assert ob.needs_onboarding(get_key=lambda name: "x" if name == present else None) is False


def test_voice_keys_alone_do_not_count():
    keys = {ob.DEEPGRAM_KEY: "a", ob.ELEVENLABS_KEY: "b"}
    assert ob.needs_onboarding(get_key=keys.get) is True


def test_completed_marker_but_key_removed_asks_again(tmp_path):
    ob.write_marker("anthropic", [], True, False, tmp_path)
    assert ob.load_marker(tmp_path)["backend"] == "anthropic"
    assert ob.needs_onboarding(get_key=lambda name: None) is True


def test_default_lookup_reads_environ_then_registry(monkeypatch):
    import yuki.voice.keys as keys

    for name in ob.MODEL_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(keys, "_from_registry", lambda name: None)
    assert ob.needs_onboarding() is True
    monkeypatch.setattr(keys, "_from_registry", lambda name: "tok" if name == ob.BEDROCK_KEY else None)
    assert ob.needs_onboarding() is False
    monkeypatch.setattr(keys, "_from_registry", lambda name: None)
    monkeypatch.setenv(ob.ANTHROPIC_KEY, "k")
    assert ob.needs_onboarding() is False


def test_import_is_light():
    code = ("import sys, yuki.onboarding as ob; ob.needs_onboarding(get_key=lambda n: None); "
            "heavy = [m for m in ('anthropic','numpy','PySide6','pipecat','yuki.agent','yuki.memory.store') "
            "if m in sys.modules]; print(','.join(heavy))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == ""


# -- prefs -----------------------------------------------------------------------------------


def test_backend_choice_merges_and_keeps_other_keys(tmp_path):
    path = prefs.settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark", "nested": {"a": 1}}), encoding="utf-8")
    prefs.save_backend_choice("bedrock", tmp_path)
    assert prefs.load_prefs(tmp_path) == {"theme": "dark", "nested": {"a": 1}, "model_backend": "bedrock"}
    prefs.save_backend_choice("anthropic", tmp_path)
    assert prefs.load_prefs(tmp_path)["model_backend"] == "anthropic"
    assert prefs.load_prefs(tmp_path)["theme"] == "dark"


def test_backend_choice_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        prefs.save_backend_choice("openai", tmp_path)


def test_prefs_missing_or_corrupt(tmp_path):
    assert prefs.load_prefs(tmp_path) == {}
    prefs.settings_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert prefs.load_prefs(tmp_path) == {}
    prefs.save_backend_choice("anthropic", tmp_path)
    assert prefs.load_prefs(tmp_path) == {"model_backend": "anthropic"}
    assert (tmp_path / "settings.json.bak").read_text(encoding="utf-8") == "{not json"


# -- marker ----------------------------------------------------------------------------------


def test_marker_holds_only_the_allowed_fields(tmp_path):
    path = ob.write_marker("bedrock", ["download", "voice", "bogus"], False, True, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "completed_at", "backend", "skipped", "consent"}
    assert data["backend"] == "bedrock"
    assert data["skipped"] == ["voice", "download"]
    assert data["consent"] == {"ambient_memory": False, "call_memory": True}


def test_marker_rejects_unknown_backend(tmp_path):
    with pytest.raises(ValueError):
        ob.write_marker(FAKE_ANTHROPIC, [], True, True, tmp_path)
    assert not ob.marker_path(tmp_path).exists()


# -- env var writer (scratch registry key) -----------------------------------------------------


def test_user_env_writes_reg_sz_to_scratch_key(scratch_env):
    scratch_env.set("YUKI_TEST_VALUE", "  hello  ")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SCRATCH_KEY) as key:
        value, kind = winreg.QueryValueEx(key, "YUKI_TEST_VALUE")
    assert (value, kind) == ("hello", winreg.REG_SZ)
    assert scratch_env.environ["YUKI_TEST_VALUE"] == "hello"
    assert scratch_env.get("YUKI_TEST_VALUE") == "hello"
    assert scratch_env.written == ["YUKI_TEST_VALUE"]
    assert scratch_env.broadcast is False           # scratch key: no broadcast
    assert scratch_env.commit() is False


def test_user_env_rejects_empty(scratch_env):
    with pytest.raises(ValueError):
        scratch_env.set("YUKI_TEST_VALUE", "   ")


def test_real_key_broadcasts_by_default():
    assert UserEnv().key_path == ENV_KEY and UserEnv().broadcast is True


# -- consent -> privacy settings -------------------------------------------------------------


def test_set_toml_bool_changes_one_line_only():
    from yuki.memory.privacy import DEFAULT_PATH

    text = DEFAULT_PATH.read_text(encoding="utf-8")
    out = consent.set_toml_bool(text, "calls", "enabled", False)
    assert tomllib.loads(out)["calls"]["enabled"] is False
    assert tomllib.loads(out)["terminal"]["enabled"] is True   # other tables' "enabled" untouched
    diff = [(a, b) for a, b in zip(text.splitlines(), out.splitlines()) if a != b]
    assert len(text.splitlines()) == len(out.splitlines())
    assert diff == [("enabled = true", "enabled = false")]
    assert consent.set_toml_bool(out, "calls", "enabled", True) == text


def test_set_toml_bool_adds_missing_key_and_table():
    out = consent.set_toml_bool("[calls]\nhold_s = 15\n", "calls", "enabled", False)
    assert tomllib.loads(out)["calls"] == {"enabled": False, "hold_s": 15}
    out = consent.set_toml_bool("version = 1\n[pause]\nwhen_locked = true", "calls", "enabled", True)
    assert tomllib.loads(out) == {"version": 1, "pause": {"when_locked": True}, "calls": {"enabled": True}}


def test_save_consent_writes_pause_flag_and_calls(tmp_path):
    saved = consent.save_consent(ambient=False, calls=False, memory_dir=tmp_path)
    assert saved == {"ambient_memory": False, "call_memory": False}
    assert (tmp_path / "paused").exists()
    rules = tomllib.loads((tmp_path / "privacy.toml").read_text(encoding="utf-8"))
    assert rules["calls"]["enabled"] is False
    # the memory side reads the same file the same way
    from yuki.memory.calls import CallConfig

    assert CallConfig.from_dict(rules["calls"]).enabled is False

    consent.save_consent(ambient=True, calls=True, memory_dir=tmp_path)
    assert not (tmp_path / "paused").exists()
    assert tomllib.loads((tmp_path / "privacy.toml").read_text(encoding="utf-8"))["calls"]["enabled"] is True


def test_default_memory_paths_follow_yuki_memory_dir():
    privacy_path, flag = consent.memory_paths()
    root = Path(os.environ["YUKI_MEMORY_DIR"])   # tests/conftest.py points it at a temp dir
    assert privacy_path == root / "privacy.toml" and flag == root / "paused"


# -- the dialog, offscreen -------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wait(qapp, predicate, timeout_s=5.0):
    import time

    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _dialog(qapp, scratch_env, tmp_path, downloader=None, checker=None):
    from yuki.onboarding.dialog import OnboardingDialog

    calls = []

    def fake_download(progress):
        calls.append("download")
        progress(50, 200)
        progress(200, 200)
        return tmp_path / "model"

    local, memory = tmp_path / "local", tmp_path / "memory"
    dialog = OnboardingDialog(env=scratch_env, local_dir=local, memory_dir=memory,
                              downloader=downloader or fake_download,
                              key_checker=checker or (lambda b, k, r: (True, "It works.")))
    dialog.show()
    return dialog, local, memory, calls


def _all_text(tmp_path: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tmp_path.rglob("*") if p.is_file())


def test_dialog_anthropic_full_run(qapp, scratch_env, tmp_path):
    d, local, memory, calls = _dialog(qapp, scratch_env, tmp_path)
    assert d.step == 0 and not d.next_btn.isEnabled() and d.backend == "anthropic"
    d.model_key.setText(FAKE_ANTHROPIC)
    assert d.next_btn.isEnabled()
    d.test_key()
    assert _wait(qapp, lambda: d.test_result.isVisible())
    assert "works" in d.test_result.text()
    d.next()
    assert d.step == 1
    d.deepgram_key.setText(FAKE_DEEPGRAM)
    d.elevenlabs_key.setText(FAKE_ELEVEN)
    d.next()
    assert d.step == 2 and not d.next_btn.isEnabled()      # explicit choice required
    d.ambient_choice.set_value(True)
    assert not d.next_btn.isEnabled()
    d.calls_choice.set_value(False)
    assert d.next_btn.isEnabled()
    d.next()
    assert d.step == 3
    assert _wait(qapp, lambda: d.download_state == "done")
    assert calls == ["download"] and d.bar.value() == 1000
    d.next()
    assert d.completed

    assert scratch_env.get(ob.ANTHROPIC_KEY) == FAKE_ANTHROPIC
    assert scratch_env.get(ob.DEEPGRAM_KEY) == FAKE_DEEPGRAM
    assert scratch_env.get(ob.ELEVENLABS_KEY) == FAKE_ELEVEN
    assert scratch_env.get(ob.BEDROCK_KEY) is None
    assert prefs.load_prefs(local) == {"model_backend": "anthropic"}
    marker = ob.load_marker(local)
    assert marker["backend"] == "anthropic" and marker["skipped"] == []
    assert marker["consent"] == {"ambient_memory": True, "call_memory": False}
    assert not (memory / "paused").exists()
    assert tomllib.loads((memory / "privacy.toml").read_text(encoding="utf-8"))["calls"]["enabled"] is False
    text = _all_text(tmp_path)
    for secret in (FAKE_ANTHROPIC, FAKE_DEEPGRAM, FAKE_ELEVEN):
        assert secret not in text                     # no key in any file written


def test_dialog_bedrock_skips_voice_and_download(qapp, scratch_env, tmp_path):
    import threading

    gate = threading.Event()

    def slow_download(progress):
        gate.wait(5)

    d, local, memory, _calls = _dialog(qapp, scratch_env, tmp_path, downloader=slow_download)
    d.bedrock_radio.setChecked(True)
    assert d.region.isVisibleTo(d) and d.region.text() == "ap-south-1"
    d.model_key.setText(FAKE_BEDROCK)
    d.region.setText("us-east-1")
    d.next()
    d.deepgram_key.setText("typed then skipped")
    d.skip()                                          # "Skip voice for now" clears the fields
    assert d.step == 2
    d.ambient_choice.set_value(False)
    d.calls_choice.set_value(True)
    d.next()
    assert d.step == 3 and d.download_state == "running" and d.skip_btn.isVisible()
    d.skip()                                          # "Download later"
    gate.set()
    assert d.completed
    assert scratch_env.get(ob.BEDROCK_KEY) == FAKE_BEDROCK
    assert scratch_env.get(ob.AWS_REGION) == "us-east-1"
    assert scratch_env.get(ob.DEEPGRAM_KEY) is None and scratch_env.get(ob.ANTHROPIC_KEY) is None
    assert scratch_env.environ == {ob.BEDROCK_KEY: FAKE_BEDROCK, ob.AWS_REGION: "us-east-1"}
    assert prefs.load_prefs(local)["model_backend"] == "bedrock"
    marker = ob.load_marker(local)
    assert marker["skipped"] == ["voice", "download"]
    assert marker["consent"] == {"ambient_memory": False, "call_memory": True}
    assert (memory / "paused").exists()
    assert FAKE_BEDROCK not in _all_text(tmp_path)


def test_dialog_cancel_saves_nothing(qapp, scratch_env, tmp_path):
    d, local, memory, calls = _dialog(qapp, scratch_env, tmp_path)
    d.model_key.setText(FAKE_ANTHROPIC)
    d.next()
    d.back()
    assert d.step == 0
    d.back()                                          # "Cancel"
    assert not d.completed and d.result() == 0
    assert scratch_env.get(ob.ANTHROPIC_KEY) is None and scratch_env.written == []
    assert not local.exists() and not memory.exists() and calls == []


def test_dialog_failed_download_still_finishes(qapp, scratch_env, tmp_path):
    def broken(progress):
        raise ConnectionError("offline")

    d, local, _memory, _calls = _dialog(qapp, scratch_env, tmp_path, downloader=broken)
    d.model_key.setText(FAKE_ANTHROPIC)
    d.next(); d.skip()
    d.ambient_choice.set_value(True); d.calls_choice.set_value(True)
    d.next()
    assert _wait(qapp, lambda: d.download_state == "failed")
    assert "ConnectionError" in d.download_status.text() and d.next_btn.isEnabled()
    d.next()
    assert d.completed and ob.load_marker(local)["skipped"] == ["voice", "download"]


def test_dialog_close_on_last_step_counts_as_done(qapp, scratch_env, tmp_path):
    import threading

    gate = threading.Event()
    d, local, _memory, _calls = _dialog(qapp, scratch_env, tmp_path, downloader=lambda p: gate.wait(5))
    d.model_key.setText(FAKE_ANTHROPIC)
    d.next(); d.skip()
    d.ambient_choice.set_value(True); d.calls_choice.set_value(True)
    d.next()
    d.reject()                                        # window close / Esc after saving
    gate.set()
    assert d.completed and "download" in ob.load_marker(local)["skipped"]


def test_stale_key_test_result_is_ignored(qapp, scratch_env, tmp_path):
    import threading

    gate = threading.Event()

    def slow_check(backend, key, region):
        gate.wait(5)
        return False, "That key was refused."

    d, *_ = _dialog(qapp, scratch_env, tmp_path, checker=slow_check)
    d.model_key.setText("first")
    thread = d.test_key()
    d.model_key.setText("second")                    # edited while the test runs
    gate.set()
    thread.join(5)
    for _ in range(20):
        qapp.processEvents()
    assert not d.test_result.isVisible()


def test_check_model_key_empty_is_offline():
    from yuki.onboarding.checks import check_model_key

    assert check_model_key("anthropic", "  ") == (False, "Paste a key first.")
