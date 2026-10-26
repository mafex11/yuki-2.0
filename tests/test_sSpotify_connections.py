"""sSpotify: the shipped connection files ``yuki/skills/connections/{spotify,audio}.toml`` as data (PLAN.md D0, D2-D4,
B.2): nothing asks for setup, the site labels the routes rely on are there; the resolver fills the volume and playlist
slots by code; footprint.

r30: the 10 Oct morning expectations (the volume capability's control default, its route variants and menu lines,
``launch_args``, the requests suite's scripted Spotify volume) were cut: phase L (lSplit, 10 Oct) made the control a
means slot with no default, and Item 7 SPOTIFY (12 Oct) emptied ``launch_args``.

Offline: temp databases, invented names.
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
from typing import Any

import pytest

from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import registry as R
from yuki.skills import spotify_route as S
from yuki.skills.resolve import Ask, Live, Plan, resolve

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
