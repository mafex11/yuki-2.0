"""evalFix: ``scripts/skills_eval.py`` - fixtures collected content-free from a copy (tool names in the run traces, a
skill moment within 3 minutes; never the builders' live-check sessions, turns said to others or Yuki's own words),
the words decrypted only into the fixtures file; the label menu is exactly the shipped capabilities; ``fixtures add``
and ``status``; ``run`` and ``scorecard`` on a copy with the caps; the console carries counts only; the live database
is only read and never opened as a store; nothing is written to the repo.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from test_evalFix_world import (MOMENTS_DDL, SECRET_WORDS, Rebuild, bars, digest, hermetic, live, ts)  # noqa: F401
from yuki.eval import dbcopy
from yuki.eval import habits_replay as H
from yuki.memory.store import Store

ROOT = Path(__file__).resolve().parents[1]
#: The label menu: the shipped capabilities in load order (r30: phase L, lSplit/lWire 10 Oct, replaced the seven
#: hand-written D2 skills; meet.open, youtube.home and youtube.video read as browser.site_tab, web.open_page and
#: web.open_video).
D2 = ("volume", "browser.site_tab", "discord.voice", "display", "message", "spotify.playlist", "app.way",
      "web.open_page", "web.open_video")
#: The end states the target set groups (PLAN C.4).
TARGET_KEYS = {"browser.site_tab", "web.open_page", "web.open_video", "spotify.playlist", "volume", "message",
               "discord.voice"}
SESSION = "20261003-101010-abc123"
LIVE_CHECK = "20261005-035340-def456"          # 35 s after the 5 Oct live-check stamp -035305


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("skills_eval_script", ROOT / "scripts" / "skills_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _script()


def _git_status() -> str:
    got = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True,
                         text=True)
    return got.stdout if got.returncode == 0 else ""


def _run(path: Path, *rows: tuple) -> None:
    """Run-trace step rows: (session, request id, tool)."""
    conn = sqlite3.connect(path)
    for sid, rid, tool in rows:
        conn.execute("INSERT INTO runs(run_key, session_id, request_id, source, step, at, tool, kind)"
                     " VALUES (?,?,?,?,?,?,?,?)", (f"{sid}:{rid}", sid, rid, "agent", 1, 0.0, tool, "action"))
    conn.commit()
    conn.close()


@pytest.fixture()
def turns(live) -> Path:
    """The live stand-in with conversation turns, run traces and moments (all invented)."""
    t0 = ts("2026-10-03", "10:20:00")
    s = Store.open(live)
    try:
        s.add_turn(SESSION, "worker-1", t0, f"turn {SECRET_WORDS[0]} down a bit")                     # tool: volume
        s.add_turn(SESSION, "voice-V1-3", t0 + 600, f"play {SECRET_WORDS[1]}", outcome="voice",
                   addressed="yuki")                                                                 # voice hand
        s.add_turn(SESSION, "worker-2", t0 + 1200, "get me into the standup")                        # moment 60 s after
        s.add_turn(SESSION, "worker-3", t0 + 1800, "write my essay")                                 # no signal
        s.add_turn(SESSION, "worker-4", t0 + 2400, "nothing for 5 minutes")                     # moment 300 s after
        s.add_turn(SESSION, "voice-V1-9", t0 + 3000, "talk to the team", addressed="others")         # said to others
        s.add_turn(SESSION, "worker-5", t0 + 3600, '[The user pressed "Do it" on a card]\nopen spotify')   # Yuki's
        s.add_turn(LIVE_CHECK, "worker-1", t0 + 4200, "scripted live check volume")                  # live check
        s.add_turn(SESSION, "worker-6", t0 + 4800,
                   'Spotify volume down\n\n[Asked by voice. What the user said most recently (newest last): '
                   f'"{SECRET_WORDS[2]}"]')                                                           # heard words
    finally:
        s.close()
    _run(live, (SESSION, "worker-1", "app_volume"), (SESSION, "voice-V1-3-h1", "media_key"),
         (SESSION, "worker-3", "type_text"), (SESSION, "voice-V1-9", "open_url"), (SESSION, "worker-5", "launch_app"),
         (LIVE_CHECK, "worker-1", "set_slider"), (SESSION, "worker-6", "app_volume"))
    conn = sqlite3.connect(live)
    conn.executescript(MOMENTS_DDL)
    conn.executemany("INSERT INTO moments(at, seen_at, kind, app, source, source_id, evidence) VALUES (?,?,?,?,?,?,?)",
                     [(t0 + 1260, t0 + 1265, "call_join", "zen", "calls", 1, "screen"),
                      (t0 + 2700, t0 + 2705, "media_play", "spotify", "media", None, "poll")])
    conn.commit()
    conn.close()
    return live


def test_collect_picks_candidates_by_machine_facts_only(turns):
    with dbcopy.working_copy() as (store, _path):
        cands, counts = S.collect(store)
    by_rid = {c["request_id"]: c for c in cands}
    assert set(by_rid) == {"worker-1", "voice-V1-3", "worker-2", "worker-6"}
    assert by_rid["worker-1"]["candidate"] == "volume" and by_rid["worker-1"]["signals"]["tools"] == ["app_volume"]
    assert by_rid["voice-V1-3"]["candidate"] == "media"                       # the voice hand's run "-h1"
    assert by_rid["worker-2"]["candidate"] == "call" and by_rid["worker-2"]["signals"]["moments"] == ["call_join"]
    assert by_rid["worker-6"]["words"] == SECRET_WORDS[2]                     # what speech recognition heard
    assert counts["left_out_not_to_yuki"] == 1 and counts["left_out_live_check"] == 1
    assert counts["left_out_yuki_words"] == 1 and counts["candidates"] == 4
    assert counts["end:volume"] == 2


def test_more_excluded_sessions_come_from_the_users_file(turns, hermetic):
    (hermetic / "eval").mkdir(exist_ok=True)
    (hermetic / "eval" / "exclude-sessions.txt").write_text("20261003-101010\n", encoding="utf-8")
    with dbcopy.working_copy() as (store, _path):
        cands, counts = S.collect(store)
    assert cands == [] and counts["left_out_live_check"] == 8


def test_collect_on_the_command_line_shows_counts_and_keeps_words_on_this_pc(turns, hermetic, capsys):
    git_before = _git_status()
    before = digest(turns.parent)
    assert S.main(["fixtures", "collect", "--copy-real"]) == 0
    out = capsys.readouterr().out
    for w in SECRET_WORDS:
        assert w not in out
    assert "candidates: 4" in out and "The copy is deleted." in out
    assert "Candidates by session" in out and "20261003-101010 4" in out     # stamps and counts only
    data = json.loads((hermetic / "eval" / "fixtures.json").read_text(encoding="utf-8"))
    assert {f["words"] for f in data["fixtures"]} >= {f"turn {SECRET_WORDS[0]} down a bit", SECRET_WORDS[2]}
    assert digest(turns.parent) == before
    assert _git_status() == git_before
    assert S.main(["fixtures", "collect", "--copy-real"]) == 0               # again: nothing new
    assert "New fixtures: 0" in capsys.readouterr().out
    assert not [p for p in (hermetic / "temp").iterdir() if p.name.startswith(dbcopy.COPY_PREFIX)]


def test_collect_dry_run_writes_nothing(turns, hermetic, capsys):
    assert S.main(["fixtures", "collect", "--copy-real", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "candidates: 4" in out and "4 would be new" in out
    assert not (hermetic / "eval" / "fixtures.json").exists()
    for w in SECRET_WORDS:
        assert w not in out


def test_collect_needs_a_copy_source(hermetic, capsys):
    assert S.main(["fixtures", "collect"]) == 2
    assert "--copy-real" in capsys.readouterr().out


def test_the_label_menu_is_the_shipped_capabilities():
    assert S.SKILL_KEYS == D2
    assert {k for name, keys, _n in S.TARGETS for k in keys} == TARGET_KEYS and TARGET_KEYS <= set(D2)
    assert sum(n for _name, _keys, n in S.TARGETS) == 25
    assert {S.label_key(old) for old in ("meet.open", "youtube.home", "youtube.video")} == {
        "browser.site_tab", "web.open_page", "web.open_video"}


def test_labelling_one_phrasing_at_a_time(hermetic):
    data = S.load_fixtures()
    S.add_fixtures(data, ["spotify down a bit", "play my mix", "open the thing", "message bob"])
    num = {key: str(i) for i, key in enumerate(D2, 1)}                       # r30: the menu's own numbers
    answers = iter([num["volume"], "Spotify", "-10", "", "", "IN_APP", "", "",                  # volume, kept
                    num["spotify.playlist"], "", "my mix", "n",          # playlist: blank slot, end, dropped
                    "s",                                                       # skipped
                    num["message"], "discord", "Bob", "on my way", "maybe", "", "y",   # message: send -> blank
                    ])
    said: list[str] = []
    saves = []
    done = S.label_fixtures(data, lambda q: next(answers), said.append, save=lambda: saves.append(1))
    assert done == {"kept": 2, "dropped": 1, "skipped": 1} and len(saves) == 3
    labels = [f["label"] for f in data["fixtures"]]
    assert labels[0]["skill"] == "volume" and labels[0]["slots"] == {"target": "spotify", "change": -10,
                                                                      "control": "in_app"}
    assert labels[1]["keep"] is False and labels[1]["end"] == "my mix" and labels[1]["slots"] == {}
    assert labels[2] is None
    assert labels[3]["slots"] == {"app": "discord", "person": "Bob", "text": "on my way"}
    menu = [line for line in said if line.startswith("  ") and ". " in line and ":" in line]
    assert [line.split(". ", 1)[1].split(":", 1)[0] for line in menu[:len(D2)]] == list(D2)
    st = S.status(data)
    assert st["kept"] == 2 and st["dropped"] == 1 and st["unlabelled"] == 1
    assert st["per_skill"] == {"message": 1, "volume": 1}


@pytest.mark.parametrize("skill,slot,typed,kept,warned", [
    ("volume", "target", "Spotify", "spotify", False),
    ("volume", "target", "the music", "", True),            # not one of the four: blank, never guessed
    ("volume", "change", "down", "down", False),
    ("volume", "change", "+5", 5, False),
    ("volume", "change", "a bit", "", True),
    ("volume", "level", "30", 30, False),
    ("volume", "level", "150", "", True),
    ("spotify.playlist", "playlist", "  Late   Night ", "Late Night", False),
    ("message", "text", "on my way", "on my way", False),
    ("volume", "mute", "", "", False),
])
def test_slots_are_labelled_in_the_form_the_score_compares(skill, slot, typed, kept, warned):
    value, why = S._slot_value(skill, slot, typed)
    assert value == kept and bool(why) is warned


def test_the_label_menu_and_the_score_share_one_slot_table():
    # r30: the menu is the shipped capabilities (lSplit); a key the score's table has is labelled from that very table
    # (the rest from the capability's own slots), and the table's old hand-written keys read as menu keys
    shared = set(H.SLOT_SPEC) & set(S.SKILL_KEYS)
    assert {"volume", "spotify.playlist", "message", "discord.voice"} <= shared
    assert all(S.slot_spec(key) is H.SLOT_SPEC[key] for key in shared)
    assert {S.label_key(key) for key in set(H.SLOT_SPEC) - shared} <= set(S.SKILL_KEYS)
    assert H.SLOT_SPEC["volume"]["target"] == ("choice", ("spotify", "app", "system", "own_voice"))


def test_q_stops_and_keeps_what_was_labelled(hermetic):
    data = S.load_fixtures()
    S.add_fixtures(data, ["a", "b"])
    answers = iter(["0", "", "y", "q"])
    done = S.label_fixtures(data, lambda _q: next(answers), lambda _s: None)
    assert done["kept"] == 1 and data["fixtures"][0]["label"]["skill"] is None and data["fixtures"][1]["label"] is None


def test_add_writes_blind_phrasings_and_can_label_them_at_once(hermetic, capsys):
    assert S.main(["fixtures", "add", "open my meeting", "get me into the standup", "open my meeting",
                   "--skill", "browser.site_tab"]) == 0
    assert "2 phrasings added" in capsys.readouterr().out
    data = S.load_fixtures()
    assert [f["label"]["skill"] for f in data["fixtures"]] == ["browser.site_tab", "browser.site_tab"]
    assert all(f["origin"] == "added" for f in data["fixtures"])
    with pytest.raises(SystemExit):
        S.main(["fixtures", "add", "x", "--skill", "calendar.open"])          # not a capability key
    assert S.main(["fixtures", "status"]) == 0
    out = capsys.readouterr().out
    assert "site tab 2/4" in out and "open my meeting" not in out


def test_phrasings_added_after_results_were_shown_are_late(hermetic):
    data = S.load_fixtures()
    S.add_fixtures(data, ["first one"], skill="meet.open", now=100.0)
    data["shown_at"] = 200.0
    S.add_fixtures(data, ["second one"], skill="meet.open", now=300.0)
    assert [f.words for f in S.eval_fixtures(data)] == ["first one"]
    assert S.status(data)["late"] == 1


def test_run_refuses_caps_over_the_users_limits(hermetic, capsys):
    assert S.main(["run", "--suite", "requests", "--budget", "3.5", "--dry-run"]) == 2
    assert S.main(["run", "--suite", "requests", "--minutes", "60", "--dry-run"]) == 2
    assert capsys.readouterr().out.count("Refused") == 2


def test_run_dry_run_prints_the_plan_without_a_copy(hermetic, capsys, monkeypatch):
    data = S.load_fixtures()
    S.add_fixtures(data, [f"phrase {i}" for i in range(25)], skill="volume")
    S.save_fixtures(data)
    monkeypatch.setattr(H, "load_resolver", lambda *a, **k: pytest.fail("the resolver was built in a dry run"))
    assert S.main(["run", "--suite", "requests", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "50 talker calls" in out and "no model was called" in out and "phrase" not in out
    S.add_fixtures(data, [f"more {i}" for i in range(25)], skill="volume")
    S.save_fixtures(data)
    assert S.main(["run", "--suite", "requests", "--dry-run", "--repeat", "3"]) == 0
    assert "A real run would be refused: the most it could cost" in capsys.readouterr().out


def test_run_moments_on_a_copy(live, bars, capsys, monkeypatch):
    calls = []

    def runner_for(path, **kw):
        assert callable(kw.get("time_left"))                                   # under the run's clock
        calls.append(path)
        return Rebuild(path)

    monkeypatch.setattr(H, "cli_runner", runner_for)
    before = digest(live.parent)
    assert S.main(["run", "--copy-real", "--suite", "moments", "--sample", "5"]) == 0
    out = capsys.readouterr().out
    assert "E1 (Zen" in out and "Result: PASS" in out and "Hand check: 5 moments" in out
    assert calls and calls[0] != live and not calls[0].exists()                # the copy, deleted after
    assert digest(live.parent) == before


def test_run_requests_without_the_resolver_says_so(live, capsys, monkeypatch):
    # r31: the resolver is built on this branch (resolve.eval_resolver), and it is a real model call: the sweep, fenced
    # from the network, caught this test calling Bedrock with the fixture's words. "Without the resolver" is pinned.
    monkeypatch.setattr(H, "load_resolver", lambda db_path: None)
    data = S.load_fixtures()
    S.add_fixtures(data, ["open my meeting"], skill="meet.open")
    S.save_fixtures(data)
    assert S.main(["run", "--copy-real", "--suite", "requests"]) == 1
    out = capsys.readouterr().out
    assert "Not available" in out and "Result:" not in out
    assert "open my meeting" not in out


def test_run_requests_with_a_resolver_marks_results_shown(live, capsys, monkeypatch):
    built_on = []

    class Stub:
        def resolve(self, words, at):
            if words == "spotify down a bit":
                return {"skill": "volume", "slots": {"target": "spotify", "change": -10, "control": "in_app"},
                        "cost_usd": 0.001}
            return {"skill": "browser.site_tab", "slots": {}, "cost_usd": 0.001}

    def load(db_path):
        built_on.append(db_path)
        return Stub()

    monkeypatch.setattr(H, "load_resolver", load)
    data = S.load_fixtures()
    S.add_fixtures(data, ["open my meeting", "bring up the call"], skill="browser.site_tab")
    S.add_fixtures(data, ["spotify down a bit"], skill="volume")
    data["fixtures"][-1]["label"]["slots"] = {"target": "spotify", "change": "down"}
    S.save_fixtures(data)
    assert S.main(["run", "--copy-real", "--suite", "requests", "--repeat", "2"]) == 0
    out = capsys.readouterr().out
    assert "Result: PASS" in out and "open my meeting" not in out
    assert S.load_fixtures()["shown_at"]
    assert built_on and built_on[0] != live and not built_on[0].exists()        # built on the copy, deleted after


def test_the_scorecard_goes_to_the_eval_folder_content_free(live, hermetic, capsys):
    assert S.main(["scorecard", "--copy-real", "--days", "7"]) == 0
    files = list((hermetic / "eval").glob("scorecard-*.json"))
    assert len(files) == 1
    card = json.loads(files[0].read_text(encoding="utf-8"))
    assert card["days"] == 7 and "background_cost" in card
    text = files[0].read_text(encoding="utf-8")
    assert "meet.google.com" not in text and "Zen" not in text


def test_the_cost_suite(live, capsys):
    assert S.main(["run", "--copy-real", "--suite", "cost"]) == 0
    assert "Background spend: $0.00 a day" in capsys.readouterr().out


def test_suites_of_later_phases_are_refused_plainly(hermetic, capsys):
    assert S.main(["run", "--suite", "lane", "--copy-real"]) == 2
    assert "not on this branch yet" in capsys.readouterr().out


def test_no_route_or_option_needs_an_account_key_or_sign_in():
    text = (ROOT / "scripts" / "skills_eval.py").read_text(encoding="utf-8").casefold()
    text += (ROOT / "yuki" / "eval" / "habits_replay.py").read_text(encoding="utf-8").casefold()
    text += (ROOT / "yuki" / "eval" / "dbcopy.py").read_text(encoding="utf-8").casefold()
    for word in ("api_key", "api key", "token=", "client_id", "oauth", "password", "sign in", "sign-in", "login"):
        assert word not in text
