"""sWeb: the S-2 connection files as data (PLAN.md D0, D2, D3, D17; B.2; the requests suite's slot contract).

Only the shipped files are read (``yuki/skills/connections``); nothing runs on the desktop.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _sweb_world import open_store, world
from yuki.skills import connection as C
from yuki.skills import registry as R
from yuki.skills.menu import MENU_CHARS, menu_lines, talker_tool
from yuki.skills.resolve import Held, Live, Plan, resolve

S2 = ("discord.voice", "message", "youtube.home", "youtube.video", "meet.open")
FILES = ("zen_meet.toml", "youtube.toml", "message.toml", "discord.toml", "whatsapp.toml", "slack.toml")


@pytest.fixture()
def loaded() -> C.Loaded:
    C.clear_cache()
    return C.load()


def test_the_s2_files_load_whole_with_no_error_or_warning(loaded) -> None:
    assert loaded.errors == () and loaded.warnings == ()
    assert set(S2) <= set(loaded.keys) and len(loaded.skills) <= C.MAX_SKILLS
    for name in FILES:
        assert (C.CONNECTIONS_DIR / name).is_file()
    ends = {(s.app, s.end) for s in loaded.skills}
    assert len(ends) == len(loaded.skills)                                   # one per (app, end)
    assert {k: loaded.get(k).end for k in S2} == {"discord.voice": "call_join", "message": "message_sent",
                                                  "youtube.home": "page", "youtube.video": "media_play",
                                                  "meet.open": "site_front"}


def test_slot_names_match_the_requests_suites_label_menu(loaded) -> None:
    from yuki.eval.habits_replay import SLOT_SPEC

    for key in S2:
        skill = loaded.get(key)
        assert set(SLOT_SPEC[key]) <= {s.name for s in skill.slots}, key
    assert loaded.get("message").slot("send").options == SLOT_SPEC["message"]["send"][1]


def test_every_s2_route_takes_focus_so_a_game_in_front_holds_it_at_resolve_time(loaded, tmp_path) -> None:
    for key in S2:
        for route in loaded.get(key).routes:
            assert route.needs_focus is True and route.game == "never", (key, route.key)
    store = open_store(tmp_path)
    world(store)
    R.seed(store, loaded)
    slots = {"discord.voice": {"channel": "lantern-club"}, "message": {"person": "Juno"},
             "youtube.home": {}, "youtube.video": {}, "meet.open": {}}
    for key in S2:
        got = resolve(key, slots[key], ["message Juno join lantern-club"], store=store, loaded=loaded,
                      live=Live(game="skyrunner"))
        assert isinstance(got, Held) and got.say == "You're in a game: after the round?", key
        free = resolve(key, slots[key], ["message Juno join lantern-club"], store=store, loaded=loaded, live=Live())
        assert not isinstance(free, Held), key


def test_sends_and_offers_are_data_meet_is_never_handed_to_the_thinker(loaded) -> None:
    msg = loaded.get("message")
    assert msg.sends and C.sends_with(msg, {"send": "send", "text": "x"}) and not C.sends_with(msg, {"send": "draft"})
    assert loaded.get("message").slot("send").default == "draft"            # a draft unless they said send
    meet = loaded.get("meet.open")
    assert meet.offerable and not meet.handoff and meet.kind == "open" and "join" in meet.wont
    assert meet.learn_from == (("site_front", "zen"), ("call_join", "zen")) and meet.routine_on == (("call_join",
                                                                                                    "zen"),)
    assert not loaded.get("discord.voice").slot("channel").taste           # a channel nobody named is never joined
    assert not loaded.get("youtube.video").slot("video").taste             # a video nobody picked never plays


def test_the_menu_fits_and_names_the_send_choice(loaded) -> None:
    lines = menu_lines(loaded)
    assert all(len(line) <= C.MENU_LINE_CHARS for line in lines)
    assert sum(len(line) + 1 for line in lines) <= MENU_CHARS
    message = next(line for line in lines if line.startswith("- message:"))
    assert "[send: send | draft]" in message
    tool = talker_tool(loaded.keys)
    assert set(S2) <= set(tool["properties"]["skill"]["enum"])


def test_every_driver_call_and_probe_the_files_name_exists(loaded) -> None:
    for key in S2:
        for route in loaded.get(key).routes:
            checks = [c for c in (route.done, route.recheck) if c is not None]
            if route.via == "driver":
                module = importlib.import_module(f"yuki.skills.{route.driver}")
                assert route.call in module.ROUTES, (key, route.call)
            for check in checks:
                if check.probe == "driver":
                    module = importlib.import_module(f"yuki.skills.{check.args['driver']}")
                    assert check.args["call"] in module.PROBES, (key, check.args)


def test_zero_setup_no_file_or_comment_asks_for_an_account_key_or_sign_in() -> None:
    for name in FILES:
        text = (C.CONNECTIONS_DIR / name).read_text(encoding="utf-8").casefold()
        for word in C.SETUP_WORDS:
            assert word not in text, (name, word)


def test_importing_the_drivers_starts_no_thread_and_is_light() -> None:
    code = ("import threading, time, yuki.skills.engine; t = time.perf_counter(); "
            "import yuki.skills.chat, yuki.skills.web_route; "
            "print(threading.active_count(), round((time.perf_counter() - t) * 1000, 1))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=str(Path(__file__).resolve().parents[1])).stdout.split()
    assert out[0] == "1"
    assert float(out[1]) < 60.0                                              # marginal, after the engine


def test_the_resolver_stays_fast_with_the_s2_skills(loaded, tmp_path) -> None:
    store = open_store(tmp_path)
    world(store)
    R.seed(store, loaded)
    cases = [("message", {"person": "Juno", "text": "see you at nine", "send": "send"},
              ["send see you at nine to Juno"]), ("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"]),
             ("youtube.home", {"browser": "Arc"}, ["YouTube in Arc"]), ("meet.open", {}, ["open my meeting"])]
    times = []
    for _ in range(25):
        for key, slots, words in cases:
            t = time.perf_counter()
            got = resolve(key, slots, words, store=store, loaded=loaded, live=Live())
            times.append((time.perf_counter() - t) * 1000.0)
            assert isinstance(got, Plan), (key, got)
    times.sort()
    assert times[int(len(times) * 0.95)] < 50.0
