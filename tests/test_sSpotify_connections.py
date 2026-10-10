"""sSpotify: the shipped connection files ``yuki/skills/connections/{spotify,audio}.toml`` as data (PLAN.md D0, D2-D4,
B.2): they load with no error, every rung ends on a machine check and a re-check, focus-taking rungs never run with a
game in front, nothing is proven yet for the no-focus Spotify rungs, nothing asks for setup; the resolver fills the
volume and playlist slots by code (Spotify's volume is its own slider, ``control = in_app``); the requests suite's
contract with a scripted talker; footprint.

Offline: temp databases, a scripted talker (no model call), invented names.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
import tomllib
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import registry as R
from yuki.skills import spotify_route as S
from yuki.skills.menu import menu_text, skill_line
from yuki.skills.resolve import Ask, EvalResolver, Live, Plan, resolve

SHIPPED = C.CONNECTIONS_DIR
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def loaded() -> C.Loaded:
    C.clear_cache()
    got = C.load([SHIPPED])
    assert got.errors == ()
    return got


@pytest.fixture()
def store(tmp_path: Path, loaded: C.Loaded):
    s = Store.open(tmp_path / "memory.db")
    R.seed(s, loaded)
    yield s
    s.close()


# -- the files ---------------------------------------------------------------------------------------------------------


def test_the_shipped_files_load_with_no_error_and_no_warning(loaded) -> None:
    assert loaded.keys == ("volume", "spotify.playlist")
    assert loaded.warnings == ()                                       # every rung has its re-check (D19)
    vol, pl = loaded.get("volume"), loaded.get("spotify.playlist")
    assert (vol.app, vol.end, vol.kind) == ("audio", "volume_change", "change")
    assert (pl.app, pl.end, pl.kind) == ("spotify", "media_context", "change")
    assert vol.state_reader == pl.state_reader == "spotify_route"
    assert [r.variant for r in vol.routes] == ["spotify_in_app", "spotify_mixer", "system", "own_voice", "mixer",
                                               "spotify_keys", "spotify_restore"]      # every no-focus rung first
    assert [r.variant for r in pl.routes] == ["library", "restore", "link", "search", "launch"]
    assert C.load().keys == loaded.keys                                # the bundled folder is these files


def test_every_rung_ends_on_a_machine_check_and_focus_rungs_never_run_with_a_game(loaded) -> None:
    for skill in loaded.skills:
        for r in skill.routes:
            assert r.done is not None and r.recheck is not None, r.key
            if r.needs_focus:
                assert r.game == "never", r.key
            if r.done.probe == "read_back":
                assert r.via == "driver"                               # the driver's own read-back
            assert r.driver == "spotify_route" and r.call in S.ROUTES, r.key
            for check in (r.done, r.recheck):
                if check.probe == "driver":
                    assert check.args["call"] in S.PROBES
    slider = next(r for r in loaded.get("volume").routes if r.variant == "spotify_in_app")
    assert slider.recheck.same_source is True                          # Spotify's slider has no second source yet
    assert not slider.retry_after_acting                                # a relative change is never done twice


def test_nothing_is_proven_yet_and_the_mixer_is_not_linked(loaded) -> None:
    for key, variant in S.PROVEN_ROUTES:
        route = next(r for r in loaded.get(key).routes if r.variant == variant)
        assert route.game == "states" and route.game_states == ()
    assert S.mixer_linked() is False
    assert S.proven(loaded)["mixer_linked"] is False
    data = S.spotify_data()
    assert data["mixer"] == {"slider_moves_mixer": False, "mixer_moves_slider": False}
    assert data["launch_args"] == ["--minimized"] and 0 < data["request_warm_s"] <= 2.5 and data["this_pc_s"] == 2.0


def test_menu_lines_fit_and_show_the_closed_choices(loaded, store) -> None:
    for skill in loaded.skills:
        assert len(skill_line(skill)) <= C.MENU_LINE_CHARS
    vol = skill_line(loaded.get("volume"))
    assert "[mute: on | off]" in vol and "[control: in_app | mixer]" in vol
    text = menu_text(store, loaded=loaded)
    assert "- volume:" in text and "- spotify.playlist:" in text and len(text) <= 2400


def test_zero_setup_no_file_setting_or_data_names_an_account_key_or_token(loaded) -> None:
    from yuki.connect.templates import SITES_DIR

    for path in list(SHIPPED.glob("*.toml")) + [SITES_DIR / "spotify.toml"]:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        C._no_setup(data, path.name)                                   # raises on a setup key or address

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                for k, x in v.items():
                    assert not any(w in str(k).casefold() for w in C.SETUP_WORDS), (path.name, k)
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                assert not any(w in v.casefold() for w in ("api_key", "client_id", "client_secret", "oauth",
                                                           "access_token", "refresh_token")), (path.name, v)

        walk(data)
    from yuki.skills import DEFAULTS

    assert not any(any(w in k for w in C.SETUP_WORDS) for k in DEFAULTS)
    assert not any(any(w in k for w in C.SETUP_WORDS) for k in S.spotify_data())


def test_the_sites_labels_the_routes_rely_on_are_there() -> None:
    from yuki.connect import spotify_desktop as SD

    en = next(lab for lab in SD.all_labels() if lab.lang == "en")
    assert en.controls["volume"][0] == "Slider" and en.row_play and en.row_pause and en.library_grid
    assert SD.playlist_uri("https://xpui.app.spotify.com/playlist/Aa1Bb2") == "spotify:playlist:Aa1Bb2"
    # the Library search route's labels are not in the site data yet: that route says "couldn't" (merge notes)
    assert S.extra_control("library_search") == []


# -- the resolver --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("words,slots,want", [
    ("turn Spotify down a bit", {"target": "Spotify", "change": "down"}, {"target": "spotify", "change": -10.0}),
    ("Spotify louder", {"target": "Spotify", "change": "up"}, {"target": "spotify", "change": 10.0}),
    ("set Spotify to 40", {"target": "Spotify", "level": 40}, {"target": "spotify", "level": 40.0}),
    ("spotify volume 25 percent", {"target": "spotify", "level": 25}, {"target": "spotify", "level": 25.0}),
    ("make Spotify quieter by 20", {"target": "Spotify", "change": -20}, {"target": "spotify", "change": -20.0}),
])
def test_every_spotify_volume_request_resolves_to_its_own_slider(loaded, store, words, slots, want) -> None:
    got = resolve("volume", slots, [words], store=store, loaded=loaded, live=Live())
    assert isinstance(got, Plan)
    assert got.values["control"] == "in_app"
    for k, v in want.items():
        assert got.values[k] == v
    assert got.routes[0].key == "A:spotify_in_app:driver"
    assert "A:mixer:driver" in [r.key for r in got.routes]             # listed, and it refuses Spotify's level


def test_the_target_comes_from_the_words_else_the_one_playing_else_the_pc(loaded, store) -> None:
    one = resolve("volume", {"change": "down"}, ["quieter"], store=store, loaded=loaded,
                  live=Live(playing=("spotify",)))
    assert isinstance(one, Plan) and one.values["target"] == "spotify" and one.filled["target"] == "live"
    two = resolve("volume", {"change": "down"}, ["quieter"], store=store, loaded=loaded,
                  live=Live(playing=("spotify", "chrome")))
    assert isinstance(two, Ask) and two.options == ["spotify", "chrome"]
    call = resolve("volume", {"change": "down"}, ["quieter"], store=store, loaded=loaded,
                   live=Live(playing=("spotify",), in_call=True))
    assert isinstance(call, Ask) and call.slot == "target"             # a call live and no app named: asked
    none = resolve("volume", {"level": 30}, ["volume 30"], store=store, loaded=loaded, live=Live())
    assert isinstance(none, Plan) and none.values["target"] == "system" and "target" in none.named
    named = resolve("volume", {"target": "Chrome", "change": "up"}, ["Chrome louder"], store=store, loaded=loaded,
                    live=Live(playing=("spotify",), in_call=True))
    assert isinstance(named, Plan) and named.values["target"] == "chrome"


def test_the_users_own_step_and_a_mixer_usual_for_spotify(loaded, store) -> None:
    from yuki.skills import prefs

    for _ in range(3):
        prefs.record(store, "volume:change@step", "15", "observed")
    got = resolve("volume", {"target": "Spotify", "change": "down"}, ["Spotify down"], store=store, loaded=loaded,
                  live=Live())
    assert got.values["change"] == -15.0
    prefs.record(store, "volume:control@spotify", "mixer", "said")
    prefs.keep_usual(store, "volume:control@spotify", "mixer", "yes")
    usual = resolve("volume", {"target": "Spotify", "level": 30}, ["Spotify to 30"], store=store, loaded=loaded,
                    live=Live())
    assert usual.values["control"] == "mixer" and usual.routes[0].key == "A:mixer:driver"


def test_with_a_game_in_front_only_rungs_the_data_allows(loaded, store) -> None:
    held = resolve("spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"], store=store,
                   loaded=loaded, live=Live(game="skyrunner", window_state=lambda app: "covered"))
    assert held.kind == "held"
    vol = resolve("volume", {"target": "Spotify", "level": 30}, ["Spotify to 30"], store=store, loaded=loaded,
                  live=Live(game="skyrunner", window_state=lambda app: "covered"))
    assert isinstance(vol, Plan)
    assert [r.key for r in vol.routes] == ["A:spotify_mixer:driver", "A:mixer:driver"]   # no window, no focus
    other = resolve("volume", {"target": "Chrome", "level": 30}, ["Chrome to 30"], store=store, loaded=loaded,
                    live=Live(game="skyrunner"))
    assert isinstance(other, Plan) and other.handoff is False


# -- the requests suite's contract (scripted talker; no model call) -----------------------------------------------------


class _Block:
    def __init__(self, name: str, data: dict) -> None:
        self.type, self.name, self.input = "tool_use", name, data


class _Talker:
    """A scripted talker: each phrasing's run_skill call, as the real one would make it from the words."""

    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.messages = self
        self.calls = 0

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        words = kw["messages"][0]["content"]
        assert "- volume:" in kw["system"][0]["text"] and kw["tools"][0]["name"] == "run_skill"
        data = self.script[words]
        return SimpleNamespace(content=[_Block("run_skill", data)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def test_the_requests_suite_scores_spotify_volume_as_its_own_slider(loaded, store) -> None:
    from yuki.eval.habits_replay import Caps, Fixture, run_requests

    script = {
        "turn Spotify down a bit": {"skill": "volume", "slots": {"target": "Spotify", "change": "down"}},
        "Spotify to 40": {"skill": "volume", "slots": {"target": "Spotify", "level": 40}},
        "a little louder on spotify": {"skill": "volume", "slots": {"target": "spotify", "change": "up"}},
        "Chrome down 20": {"skill": "volume", "slots": {"target": "Chrome", "change": -20}},
        "play Copper Kites": {"skill": "spotify.playlist", "slots": {"playlist": "Copper Kites"}},
    }
    fixtures = [
        Fixture("f1", "turn Spotify down a bit", 1.7e9, "volume", {"target": "spotify", "change": "down"}),
        Fixture("f2", "Spotify to 40", 1.7e9, "volume", {"target": "spotify", "level": 40}),
        Fixture("f3", "a little louder on spotify", 1.7e9, "volume", {"target": "spotify", "change": "up",
                                                                      "control": "in_app"}),
        Fixture("f4", "Chrome down 20", 1.7e9, "volume", {"target": "app", "change": -20}),
        Fixture("f5", "play Copper Kites", 1.7e9, "spotify.playlist", {"playlist": "Copper Kites"}),
    ]
    talker = _Talker(script)
    resolver = EvalResolver(store, client=talker, model="invented-model", prompt=lambda menu: "Talker.\n\n" + menu,
                            loaded=loaded, settings=SimpleNamespace(estimate_cost=lambda model, usage: 0.0001))
    out = run_requests(fixtures, resolver, Caps(0.5, 5.0), repeat=2)
    assert out["bars"]["spotify_in_app"]["pass"] is True and out["bars"]["spotify_in_app"]["fixtures"] == 3
    assert out["bars"]["right"]["pass"] is True and out["bars"]["agree"]["pass"] is True
    assert all(r["right"] == 5 and r["spotify_in_app"] == 3 for r in out["per_run"])
    assert talker.calls == 10


# -- footprint (PLAN.md B.2, C.5) -------------------------------------------------------------------------------------


def test_resolver_p95_under_50_ms_and_little_memory(loaded, store) -> None:
    cases = [("volume", {"target": "Spotify", "change": "down"}, ["turn Spotify down a bit"]),
             ("volume", {"level": 30}, ["volume 30"]),
             ("spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"])]
    for key, slots, words in cases:                                     # warm the caches and lazy imports first
        resolve(key, slots, words, store=store, loaded=loaded, live=Live(playing=("spotify",)))
    times = []
    tracemalloc.start()
    for i in range(120):
        key, slots, words = cases[i % len(cases)]
        t = time.perf_counter()
        resolve(key, slots, words, store=store, loaded=loaded, live=Live(playing=("spotify",)))
        times.append((time.perf_counter() - t) * 1000.0)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    p95 = statistics.quantiles(times, n=20)[-1]
    assert p95 < 50.0, p95
    assert peak < 3 * 1024 * 1024, peak


def test_importing_the_driver_is_cheap_and_starts_no_thread() -> None:
    code = (
        "import threading, time, json\n"
        "import yuki.skills.engine, yuki.skills.connection, yuki.skills.resolve\n"
        "n = threading.active_count()\n"
        "t = time.perf_counter()\n"
        "import yuki.skills.spotify_route as s\n"
        "dt = (time.perf_counter() - t) * 1000.0\n"
        "mods = sorted(m for m in __import__('sys').modules if m.startswith(('yuki.connect', 'yuki.actions', "
        "'yuki.perception', 'comtypes', 'win32')))\n"
        "print(json.dumps({'ms': dt, 'threads': threading.active_count() - n, 'heavy': mods}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["ms"] < 60.0 and got["threads"] == 0
    assert not [m for m in got["heavy"] if m.startswith(("yuki.connect.spotify", "yuki.actions.slider",
                                                          "yuki.perception.audio", "comtypes"))]
