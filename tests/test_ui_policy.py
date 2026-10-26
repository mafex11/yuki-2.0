"""The stored autonomy policy (yuki.ui.policy): defaults from the user's rules, ceilings, a file that can't be
hand-edited past a rule, and keys from other builds kept. Temp files only."""

from __future__ import annotations

import json

import pytest

from yuki.ui import policy


def test_defaults_follow_the_users_rules(tmp_path):
    levels = policy.load(tmp_path / "autonomy.json")              # no file: the defaults
    assert levels == policy.DEFAULTS
    assert levels["send_messages"] == "ask" and policy.BY_KEY["send_messages"].ceiling == "ask"
    assert levels["money"] == "suggest" and policy.BY_KEY["money"].ceiling == "ask"
    # qBrain (18 Oct) added the "tidy" dial: tidy-ups inside the user's own requests
    assert set(levels) == {"read_screen", "open_apps", "click_type", "send_messages", "files", "money", "tidy"}
    assert policy.LEVELS == ("suggest", "show_plan", "ask", "do_and_tell")
    assert any("plain instruction" in rule for rule in policy.FIXED_RULES)


def test_set_level_persists_and_refuses_past_a_ceiling(tmp_path):
    path = tmp_path / "autonomy.json"
    assert policy.set_level("files", "do_and_tell", path)["files"] == "do_and_tell"
    assert policy.set_level("send_messages", "suggest", path)["send_messages"] == "suggest"   # more careful: fine
    with pytest.raises(policy.PolicyError, match="at most"):
        policy.set_level("send_messages", "do_and_tell", path)
    with pytest.raises(policy.PolicyError):
        policy.set_level("money", "do_and_tell", path)
    with pytest.raises(policy.PolicyError):
        policy.set_level("teleport", "ask", path)
    with pytest.raises(policy.PolicyError):
        policy.set_level("files", "always", path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == policy.VERSION and raw["levels"]["files"] == "do_and_tell"
    assert raw["levels"]["send_messages"] == "suggest" and raw["fixed_rules"] == list(policy.FIXED_RULES)
    assert policy.reset(path) == policy.DEFAULTS


def test_a_hand_edited_file_cannot_lift_a_kind_past_its_rule(tmp_path):
    path = tmp_path / "autonomy.json"
    path.write_text(json.dumps({"levels": {"send_messages": "do_and_tell", "money": "do_and_tell", "files": 7,
                                           "open_apps": "suggest", "warp": "do_and_tell"},
                                "future_key": {"keep": True}}), encoding="utf-8")
    levels = policy.load(path)
    assert levels["send_messages"] == "ask" and levels["money"] == "suggest" and levels["files"] == "show_plan"
    assert levels["open_apps"] == "suggest" and "warp" not in levels
    policy.set_level("click_type", "do_and_tell", path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["future_key"] == {"keep": True}                    # a newer build's keys survive a save
    assert raw["levels"]["send_messages"] == "ask"                # and the bad value is written back corrected
    path.write_text("not json", encoding="utf-8")
    assert policy.load(path) == policy.DEFAULTS


def test_describe_is_what_the_window_shows(tmp_path):
    d = policy.describe(path=tmp_path / "autonomy.json")
    assert [lv["key"] for lv in d["levels"]] == list(policy.LEVELS)
    assert [k["key"] for k in d["kinds"]] == [k.key for k in policy.KINDS]
    send = next(k for k in d["kinds"] if k["key"] == "send_messages")
    assert send == {"key": "send_messages", "words": "Send messages", "level": "ask", "default": "ask", "max": "ask",
                    "note": policy.BY_KEY["send_messages"].note}
    json.dumps(d)                                                  # it crosses the pipe as JSON


def test_the_default_location_is_under_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert policy.policy_path() == tmp_path / "Yuki" / "autonomy.json"
