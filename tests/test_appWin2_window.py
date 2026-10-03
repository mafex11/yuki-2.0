"""The window for the user's 4 Oct feedback round 3 (appWin2: W4, W7, W8, W9, W10), the Python side: quiet hours
written where the memory service's card gates read them (and read back by those gates), the Settings data for the
switch, and the Playbook's data (procedures as numbered steps, app choices in plain words with no counts). The page
side (the one thread input, the Calls layout, the Playbook layout, the quiet hours control) is checked in a headless
browser by scripts/check_live_patch.js. Invented data only; the real privacy file, ui.json and memory are never
touched."""

from __future__ import annotations

import json
import time
import tomllib
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_web_window import _act, _window
from yuki.ui import ui_settings
from yuki.ui import web_data as W
from yuki.ui import web_window


@pytest.fixture
def privacy(tmp_path, monkeypatch):
    from yuki.ui.window import settings_io

    path = tmp_path / "privacy.toml"
    path.write_text("# the user's file\n[nudges]\nenabled = true\nquiet_start = \"00:30\"\nquiet_end = \"00:30\"\n",
                    encoding="utf-8")
    monkeypatch.setattr(settings_io, "privacy_path", lambda: path)
    return path


def _nudges(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))["nudges"]


# -- W10: quiet hours -------------------------------------------------------------------------------------------------


def test_quiet_hours_are_written_where_the_card_gates_read_them(tmp_path, monkeypatch, privacy):
    from yuki.memory.nudges import NudgeConfig, in_quiet_hours

    win, host = _window(tmp_path, monkeypatch)
    r = _act(win, host, "quiet", start="23:00", end="7:30")
    assert r["ok"] and (r["quiet_start"], r["quiet_end"]) == ("23:00", "07:30")
    table = _nudges(privacy)
    assert (table["quiet_start"], table["quiet_end"]) == ("23:00", "07:30") and table["enabled"] is True
    assert privacy.read_text(encoding="utf-8").startswith("# the user's file")          # comments kept
    # the memory service's own reader holds cards inside them and lets them through outside (wrapping midnight)
    cfg = NudgeConfig.from_dict(table)
    day = datetime(2026, 10, 4)
    assert in_quiet_hours(day.replace(hour=2).timestamp(), cfg) and in_quiet_hours(day.replace(hour=23, minute=10).timestamp(), cfg)
    assert not in_quiet_hours(day.replace(hour=12).timestamp(), cfg)
    assert ui_settings.load(tmp_path / "ui.json").quiet_last == ["23:00", "07:30"]      # what "on" brings back


def test_quiet_hours_off_and_on_again_bring_the_same_hours_back(tmp_path, monkeypatch, privacy):
    from yuki.memory.nudges import NudgeConfig, in_quiet_hours

    win, host = _window(tmp_path, monkeypatch)
    on = _act(win, host, "quiet", on=True)                    # never set before: memory's own default hours
    assert (on["quiet_start"], on["quiet_end"]) == web_window.WebWindow.QUIET_DEFAULT
    assert _act(win, host, "quiet", start="22:30", end="06:00")["ok"]
    off = _act(win, host, "quiet", on=False)
    assert off["ok"] and off["quiet_start"] == off["quiet_end"]
    table = _nudges(privacy)
    assert table["quiet_start"] == table["quiet_end"]                                    # equal = none, as memory reads it
    assert not in_quiet_hours(datetime(2026, 10, 4, 2).timestamp(), NudgeConfig.from_dict(table))
    back = _act(win, host, "quiet", on=True)
    assert (back["quiet_start"], back["quiet_end"]) == ("22:30", "06:00") == (_nudges(privacy)["quiet_start"], _nudges(privacy)["quiet_end"])


def test_quiet_hours_refuse_a_non_time_and_an_empty_range_and_change_nothing(tmp_path, monkeypatch, privacy):
    win, host = _window(tmp_path, monkeypatch)
    assert _act(win, host, "quiet", start="22:00", end="06:00")["ok"]
    before = privacy.read_text(encoding="utf-8")
    for fields in ({"start": "25:00"}, {"end": "soon"}, {"start": "7:5x"}, {"start": "06:00", "end": "06:00"}):
        r = _act(win, host, "quiet", **fields)
        assert r["ok"] is False and r["error"]
    assert privacy.read_text(encoding="utf-8") == before
    assert web_window._hhmm("7:05") == "07:05" and web_window._hhmm(" 23:59 ") == "23:59"
    assert web_window._hhmm("24:00") is None and web_window._hhmm("") is None and web_window._hhmm(None) is None


def test_settings_data_carries_the_hours_the_switch_brings_back(tmp_path, monkeypatch, privacy):
    from yuki.ui.window import data as D

    monkeypatch.setattr(ui_settings, "load", lambda path=None: ui_settings.UiSettings(quiet_last=["23:00", "07:00"]))
    monkeypatch.setattr(D, "read_voice_prefs", lambda *a, **k: {})
    monkeypatch.setattr(W, "yuki_usage", lambda: None)
    memory = SimpleNamespace(activity=lambda **k: {}, status=lambda: {}, call_prefs=lambda: [])
    co = W.build_settings(memory, policy_path=tmp_path / "autonomy.json")["coach"]
    assert (co["quiet_start"], co["quiet_end"], co["quiet_last"]) == ("00:30", "00:30", ["23:00", "07:00"])


# -- W7: the Playbook's data ------------------------------------------------------------------------------------------


COUNTS = "YouTube: Chrome, 39.3 minutes by 39 visits over 3 days, over Firefox, 12 min"


def _habit(**kw):
    card = {"id": "pb:7", "kind": "habit", "title": "which browser for YouTube", "text": COUNTS, "steps": [],
            "default": "Chrome", "ask": False, "people": ["YouTube"], "apps": ["chrome"], "uses": 2, "last_used": None,
            "confidence": 0.7, "state": "active", "origin": "miner", "since": time.time() - 86400,
            "evidence": {"seen": 39, "days": 3}, "disabled": False}
    return {**card, **kw}


def test_an_app_choice_is_said_plainly_never_with_its_counts():
    ch = W.choice_view(_habit())
    assert ch["line"] == "YouTube: usually Chrome." and ch["subject"] == "YouTube" and ch["title"] == "which browser for YouTube"
    assert "39" not in json.dumps({k: v for k, v in ch.items() if k != "learned"})       # no count in what the row says
    assert W.choice_view(_habit(people=[], default="", ask=True))["line"] == "Which browser for YouTube: I ask which one."
    assert W.choice_view(_habit(people=["Riya"], default="the Signal chat"))["line"] == "Riya: usually the Signal chat."
    assert W.choice_view(_habit(default="", ask=False))["line"] == "YouTube."
    # a habit seen once (the NEW block) says the same plain line, never the miner's counts
    prop = W.proposal_view({**_habit(), "state": "candidate"})
    assert prop["basis"].startswith("YouTube: usually Chrome.") and "minutes" not in prop["basis"] and "visits" not in prop["basis"]


def test_the_playbook_page_splits_procedures_from_app_choices():
    proc = {"id": "pb:3", "kind": "procedure", "title": "Open a video site", "text": "1. Open Firefox 2. New tab",
            "steps": ["Open Firefox", "Open a new tab", "Go to the site"], "apps": ["firefox"], "uses": 4, "confidence": 0.8,
            "state": "active", "origin": "distill", "since": time.time() - 3 * 86400, "evidence": {}, "disabled": False}
    style = {"id": "pb:9", "kind": "style", "title": "Messages to Dev", "text": "Short, no greeting.", "apps": [], "uses": 0,
             "confidence": 0.8, "state": "active", "origin": "user", "since": time.time(), "evidence": {}, "disabled": False}
    memory = SimpleNamespace(workflows=lambda: {"available": True, "procedures": [proc, _habit()], "lessons": [style],
                                                "proposals": [{**_habit(id="pb:8"), "state": "candidate"}] * 3},
                             rules=lambda: [], standing_requests=lambda: [])
    page = W.build_playbook(memory)
    assert [p["ref"] for p in page["procedures"]] == ["pb:3"] and page["procedures"][0]["steps"][0] == "Open Firefox"
    assert [c["ref"] for c in page["choices"]] == ["pb:7", "pb:8"] and page["choices"][1]["tentative"]
    assert all(c["line"] == "YouTube: usually Chrome." for c in page["choices"])
    assert [x["dont"] for x in page["lessons"]] == ["Messages to Dev"]
    assert "visits" not in json.dumps(page) and "39.3" not in json.dumps(page)
