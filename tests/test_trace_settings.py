"""capC1 (report 96): Settings -> Record how I work, unlocked: off for a new install, saved to the privacy file the
memory service reads within a second, plain words, and the leave-out list honoured by the trace's gate.

Temp LOCALAPPDATA only: the real settings are never touched.
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest

from yuki.memory.privacy import DEFAULT_PATH, PrivacyConfig, PrivacyRules
from yuki.memory.timeline import PrivacySection
from yuki.memory.trace_privacy import TraceRules, gate
from yuki.ui import ui_settings
from yuki.ui import web_data as W
from yuki.ui import web_window


def test_a_new_install_is_off():
    with DEFAULT_PATH.open("rb") as fh:
        trace = tomllib.load(fh)["trace"]
    assert trace["record_actions"] is False and trace["leave_out"] == [] and trace["ab_hours"] is False
    assert TraceRules.from_dict(trace).record_actions is False
    assert TraceRules.from_dict({}).record_actions is False
    assert TraceRules.from_dict({"record_actions": "yes"}).record_actions is False      # only a real true turns it on
    # decision D3, kept as config for phase C4 (nothing in C1 reads it)
    assert dict(TraceRules.from_dict(trace).learning) == {"fix_after_failure": "at_once", "demo_hint": "at_once",
                                                          "demo_alone_after_times": 2,
                                                          "demo_alone_after_proven_runs": 1}


def test_the_words_are_plain_and_say_what_is_never_kept():
    for words in (ui_settings.RECORD_HINT, ui_settings.RECORD_ON, ui_settings.RECORD_OFF,
                  ui_settings.RECORD_LEAVE_OUT_HINT):
        assert "trace" not in words.lower() and "toml" not in words.lower() and "[" not in words
    assert "Never the letters you type" in ui_settings.RECORD_HINT and "game" in ui_settings.RECORD_HINT
    assert "tray" in ui_settings.RECORD_ON and "30 days" in ui_settings.RECORD_ON
    on = W.record_view({"trace": {"record_actions": True}}, [])
    assert on["on"] is True and on["waiting"] == ui_settings.RECORD_ON


class _Host:
    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, msg: dict) -> None:
        self.posted.append(msg)


def _act(win, host, action: str, **fields) -> dict:
    n = len([m for m in host.posted if m.get("kind") == "reply"])
    win.on_message({"op": "act", "id": f"{action}-{n}", "action": action, **fields})
    end = time.time() + 5
    while time.time() < end:
        replies = [m for m in host.posted if m.get("kind") == "reply"]
        if len(replies) > n:
            return replies[-1]
        time.sleep(0.02)
    raise AssertionError("no reply")


@pytest.fixture
def window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("YUKI_MEMORY_DIR", raising=False)
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               policy_path=tmp_path / "autonomy.json")
    monkeypatch.setattr(win, "_open_memory", lambda: None)
    host = _Host()
    win.host = host
    return win, host, tmp_path / "Yuki" / "memory" / "privacy.toml"


def test_the_switch_reaches_the_memory_service_through_the_privacy_file(window):
    win, host, privacy = window
    config = PrivacyConfig(privacy)                              # the user's copy, made from the packaged defaults
    section = PrivacySection(config, "trace", TraceRules.from_dict)
    assert section.get().record_actions is False
    assert _act(win, host, "record_actions", on=True)["on"] is True
    time.sleep(0.02)
    assert section.get().record_actions is True                  # what the trace's beat reads every second
    assert _act(win, host, "record_leave_out", app="Invented Diary", out=True)["leave_out"] == ["Invented Diary"]
    time.sleep(0.02)
    rules = section.get()
    assert rules.left_out("diary.exe", "Invented Diary") and rules.left_out("INVENTED DIARY", "")
    assert gate(PrivacyRules.load(DEFAULT_PATH), rules, process="diary.exe", app="Invented Diary") == "left_out"
    assert _act(win, host, "record_actions", on=False)["on"] is False
    time.sleep(0.02)
    assert section.get().record_actions is False
    assert config.last_error == ""                               # the watcher's rules still parse
