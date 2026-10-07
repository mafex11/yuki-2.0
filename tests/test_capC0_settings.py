"""Report 78, phase C0: Settings → Privacy and pause → "Record how I work", off and locked, with a leave-out list.

Nothing records your actions yet and the switch waits for your go-ahead (decision D1): it is shown off, the window
refuses to turn it on, and the leave-out list is kept in the privacy file ready for later. Offline: a temp
LOCALAPPDATA, invented app names, no tray, no memory service. The page side is checked headless by
scripts/check_live_patch.js (RC1).
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest

from yuki.ui import ui_settings
from yuki.ui import web_data as W
from yuki.ui import web_window


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _Host:
    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, msg: dict) -> None:
        self.posted.append(msg)

    def replies(self) -> list[dict]:
        return [m for m in self.posted if m.get("kind") == "reply"]


def _act(win, host, action: str, **fields) -> dict:
    n = len(host.replies())
    win.on_message({"op": "act", "id": f"{action}-{n}", "action": action, **fields})
    assert _wait(lambda: len(host.replies()) > n)
    return host.replies()[-1]


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


def test_the_view_is_off_locked_and_offers_the_apps_not_left_out() -> None:
    v = W.record_view({"trace": {"leave_out": ["Notes", " notes ", "Fieldrunner"]}}, ["Notes", "Calc", "Firefox"])
    assert v["on"] is False and v["locked"] is False        # capC1 (report 96) unlocked it; still off by default
    assert v["label"] == ui_settings.RECORD_LABEL and v["waiting"] == ui_settings.RECORD_OFF
    assert "never what you copy" in v["hint"] and "game" in v["hint"]
    assert v["leave_out"] == ["Notes", "Fieldrunner"] and v["choices"] == ["Calc", "Firefox"]
    assert W.record_view({}, [])["leave_out"] == [] and W.record_view({"trace": "x"}, ["A"])["choices"] == ["A"]


def test_settings_carries_the_switch_even_with_a_memory_that_says_nothing(tmp_path: Path) -> None:
    class _Bare:
        status = activity = call_prefs = None

    d = W.build_settings(_Bare(), policy_path=tmp_path / "autonomy.json", ui_path=tmp_path / "ui.json")
    assert d["record"]["on"] is False and d["record"]["locked"] is False


def test_the_window_saves_the_switch_since_c1(window) -> None:
    win, host, privacy = window
    r = _act(win, host, "record_actions", on=True)           # capC1: the user's go-ahead came (decision D1)
    assert r["ok"] is True and r["on"] is True
    assert tomllib.loads(privacy.read_text(encoding="utf-8"))["trace"]["record_actions"] is True
    assert _act(win, host, "record_actions", on=False)["on"] is False
    assert {"record_actions", "record_leave_out"} <= win.SETTINGS_ACTIONS


def test_the_leave_out_list_is_kept_in_the_privacy_file_and_memory_still_reads_it(window) -> None:
    from yuki.memory.gamegate import GameSet
    from yuki.memory.privacy import PrivacyConfig

    win, host, privacy = window
    config = PrivacyConfig(privacy)                      # the user's copy, made from the packaged defaults
    r = _act(win, host, "record_leave_out", app="Fieldrunner", out=True)
    assert r["ok"] and r["leave_out"] == ["Fieldrunner"]
    assert _act(win, host, "record_leave_out", app="Calc", out=True)["leave_out"] == ["Fieldrunner", "Calc"]
    assert _act(win, host, "record_leave_out", app="fieldrunner", out=False)["leave_out"] == ["Calc"]
    assert _act(win, host, "record_leave_out", app="  ", out=True)["ok"] is False
    data = tomllib.loads(privacy.read_text(encoding="utf-8"))
    assert data["trace"]["leave_out"] == ["Calc"] and data["trace"]["record_actions"] is False
    assert config.rules() is not None and config.last_error == ""          # the watcher's rules still parse
    assert GameSet.load(privacy).is_game("VALORANT.exe")                   # and the game list is untouched
