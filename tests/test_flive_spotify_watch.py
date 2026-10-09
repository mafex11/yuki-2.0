"""fLive: the memory side's Spotify watcher (PLAN.md D4, F item 7, B.1): warm-up, play-start and leave-front reads,
moments, learned links and labels, the daily state probe, the game hold, content-free logs.

Offline: a stub window reader returns prepared reads (invented names); jobs run inline; a temp store stub with a
real cipher. Nothing touches a window, Spotify or the real database.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from yuki.connect import spotify_desktop as SD
from yuki.memory.crypto import FieldCipher

import pytest

URI_A = "spotify:playlist:1A2b3C4d5E6f7G8h9I0jKl"
STORE_ID = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"


@pytest.fixture(autouse=True)
def _classic_spotify_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub window's process: an unpackaged Spotify.exe (no real process is looked up)."""
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
NAMES = ("Rainy Commute", "Late Lanterns", "Deep Focus Mix", "Mira Okafor")


class Store:
    def __init__(self) -> None:
        self.cipher = FieldCipher(os.urandom(32))
        self.state: dict[str, str] = {}

    def nudge_state(self, name: str) -> str | None:
        return self.state.get(name)

    def set_nudge_state(self, name: str, value: str | None) -> None:
        if value is None:
            self.state.pop(name, None)
        else:
            self.state[name] = value


def _rows(playing: str | None = "Late Lanterns") -> list[SD.Row]:
    return [SD.Row("Rainy Commute", "Mira Okafor", False, playing == "Rainy Commute"),
            SD.Row("Late Lanterns", "Mira Okafor", False, playing == "Late Lanterns"),
            SD.Row("Deep Focus Mix", "Spotify", True, playing == "Deep Focus Mix")]


class Reader:
    def __init__(self) -> None:
        self.ref: SD.WindowRef | None = SD.WindowRef(hwnd=9001, pid=4242, minimised=False)
        self.state_value = "front"
        self.warms: list[float] = []
        self.reads: list[dict[str, Any]] = []
        self.warm_built = True
        self.result = SD.SpotifyRead(status="ok", built=True, player=True, slider=40.0, slider_source="english",
                                     grid=True, rows=_rows(), links={"Rainy Commute": URI_A},
                                     label_source="english", elements=330, ms=120.0)

    def find(self) -> SD.WindowRef | None:
        return self.ref

    def state(self, hwnd: int) -> str:
        return self.state_value

    def warm(self, hwnd: int, budget_s: float) -> SD.WarmResult:
        self.warms.append(budget_s)
        return SD.WarmResult(before=7, after=330 if self.warm_built else 7, built=self.warm_built, cold=True,
                             asked=2, polls=12, ms=1800.0)

    def read(self, hwnd: int, budget_s: float, **kw: Any) -> SD.SpotifyRead:
        self.reads.append({"budget": budget_s, **kw})
        return self.result


class Clock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 10, 9, 0, 0).timestamp()
        self.mono = 5000.0

    def advance(self, s: float) -> None:
        self.wall += s
        self.mono += s


def make(game: list[bool] | None = None, **kw: Any) -> tuple[SD.SpotifyWatch, Reader, Store, list, list, Clock]:
    reader, store, moments, logs, clock = Reader(), Store(), [], [], Clock()
    game = game if game is not None else [False]
    watch = SD.SpotifyWatch(emit=moments.append, log=lambda t, **f: logs.append((t, f)), store=store,
                            game_front_fn=lambda: game[0], reader=reader, spawn=lambda fn: fn(),
                            clock=lambda: clock.wall, mono=lambda: clock.mono, **kw)
    return watch, reader, store, moments, logs, clock


def test_the_window_appearing_warms_it_once() -> None:
    watch, reader, _store, _m, logs, _c = make()
    watch.on_front_change("arc", "spotify")
    watch.on_front_change("spotify", "arc")
    watch.on_front_change("arc", "spotify")
    assert len(reader.warms) == 1 and reader.warms[0] == SD.WARM_BUDGET_S
    warm_logs = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "warm"]
    assert warm_logs and warm_logs[0]["cold"] is True and warm_logs[0]["elements"] == 330


def test_play_start_reads_the_library_and_notes_the_playlist() -> None:
    watch, reader, store, moments, logs, _c = make()
    watch.on_media_change(started=[("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "Spotify", "x")], stopped=[])
    assert len(reader.warms) == 1   # warmed first, off any request path
    assert reader.reads[-1]["session_playing"] is True and set(reader.reads[-1]["want"]) == {"slider", "library",
                                                                                              "links"}
    ctx = [m for m in moments if m["kind"] == "media_context"]
    assert len(ctx) == 1
    m = ctx[0]
    assert m["entity"] == "Late Lanterns" and m["entity_keep"] is True and m["app"] == "spotify"
    assert m["means"] == "library_row" and m["source"] == "spotify" and m["facts"]["owner"] == "other"
    # the link of another row on screen was learned, kept encrypted
    raw = store.state[SD.LINKS_KEY]
    assert "Rainy" not in raw and "1A2b3C" not in raw
    assert SD.learned_links(store) == {"Rainy Commute": URI_A}
    read_log = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "play_start"][0]
    assert read_log["first"] is True and read_log["rows"] == 3 and read_log["playing_found"] is True


def test_a_second_trigger_of_the_same_start_is_not_a_second_moment() -> None:
    watch, _r, _s, moments, _l, clock = make()
    watch.on_media_change(started=["Spotify.exe"])
    clock.advance(10)
    watch.on_media_change(started=["Spotify.exe"])
    assert len([m for m in moments if m["kind"] == "media_context"]) == 1
    clock.advance(SD.CONTEXT_REPEAT_S)
    watch.on_media_change(started=["Spotify.exe"])
    assert len([m for m in moments if m["kind"] == "media_context"]) == 2


def test_a_spotify_owned_playlist_is_digest_only() -> None:
    watch, reader, store, moments, _l, _c = make()
    reader.result.rows = _rows(playing="Deep Focus Mix")
    reader.result.links = {}
    watch.on_media_change(started=["Spotify.exe"])
    m = [m for m in moments if m["kind"] == "media_context"][0]
    assert m["entity"] == "Deep Focus Mix" and m["entity_keep"] is False and m["facts"]["owner"] == "spotify"
    assert SD.LINKS_KEY not in store.state


def test_another_apps_media_start_reads_nothing() -> None:
    watch, reader, _s, moments, _l, _c = make()
    watch.on_media_change(started=[("308046B0AF4A39CB", "Chrome", "x")])
    assert reader.reads == [] and reader.warms == [] and moments == []


def test_leaving_the_front_reads_the_slider_and_notes_a_change() -> None:
    watch, reader, _s, moments, _l, _c = make()
    watch.on_front_change("spotify", "arc")
    assert reader.reads[-1]["want"] == ("slider",)
    assert [m for m in moments if m["kind"] == "volume_change"] == []   # the first read is the baseline
    reader.result.slider = 55.0
    watch.on_front_change("arc", "spotify")
    watch.on_front_change("spotify", "arc")
    vc = [m for m in moments if m["kind"] == "volume_change"]
    assert len(vc) == 1 and vc[0]["value_from"] == 40.0 and vc[0]["value"] == 55.0 and vc[0]["means"] == "in_app"
    assert vc[0]["app"] == "spotify" and vc[0]["source"] == "spotify"


def test_nothing_is_read_or_probed_with_a_game_in_front() -> None:
    game = [True]
    watch, reader, store, moments, logs, clock = make(game)
    reader.state_value = "minimised"
    watch.on_media_change(started=["Spotify.exe"])
    watch.on_front_change("spotify", "valorant-win64-shipping")
    watch.on_front_change("arc", "spotify")
    clock.advance(120)
    watch.beat()
    assert reader.reads == [] and reader.warms == [] and moments == []
    assert all(f.get("status") == "held_game" for t, f in logs if t == "spotify_read")
    assert not any(t == "spotify_probe" for t, _f in logs)


def test_the_daily_probe_runs_once_per_state_after_60_s_never_with_a_game() -> None:
    game = [False]
    watch, reader, store, _m, logs, clock = make(game)
    reader.state_value = "minimised"
    watch.on_front_change("spotify", "arc")             # leaves the front, minimised: the probe's clock starts
    clock.advance(30)
    watch.beat()
    assert not any(t == "spotify_probe" for t, _f in logs)
    clock.advance(30)
    watch.beat()                                        # 60 s minimised, no game
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 1 and probes[0]["probe_state"] == "minimised"
    st = json.loads(store.state[SD.STATUS_KEY])
    assert st["probes"]["minimised"]["day"] == "2026-10-10" and st["probes"]["minimised"]["rows"] == 3
    # the same state again the same day: no second probe
    watch.on_front_change("arc", "spotify")
    watch.on_front_change("spotify", "arc")
    clock.advance(61)
    watch.beat()
    assert len([1 for t, _f in logs if t == "spotify_probe"]) == 1
    # another state: covered, but a game comes to the front at the probe's time -> held, then probed later
    watch.on_front_change("arc", "spotify")
    reader.state_value = "covered"
    watch.on_front_change("spotify", "arc")
    game[0] = True
    clock.advance(61)
    watch.beat()
    assert len([1 for t, _f in logs if t == "spotify_probe"]) == 1
    game[0] = False
    clock.advance(61)
    watch.beat()
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 2 and probes[1]["probe_state"] == "covered"


def test_a_state_change_restarts_the_60_s() -> None:
    watch, reader, _s, _m, logs, clock = make()
    reader.state_value = "visible"
    watch.on_front_change("spotify", "arc")
    clock.advance(40)
    reader.state_value = "minimised"
    clock.advance(20)
    watch.beat()                       # 60 s away, but minimised only just now
    assert not any(t == "spotify_probe" for t, _f in logs)
    clock.advance(60)
    watch.beat()
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 1 and probes[0]["probe_state"] == "minimised"


def test_the_probe_switch_turns_it_off() -> None:
    watch, reader, _s, _m, logs, clock = make(settings=SD.SpotifySettings(state_probe=False))
    reader.state_value = "minimised"
    watch.on_front_change("spotify", "arc")
    clock.advance(120)
    watch.beat()
    assert not any(t == "spotify_probe" for t, _f in logs)


def test_a_warm_up_that_did_not_build_is_not_repeated_on_every_trigger() -> None:
    watch, reader, _s, _m, _l, clock = make()
    reader.warm_built = False
    reader.result = SD.SpotifyRead(status="not_built", elements=7)
    watch.on_media_change(started=["Spotify.exe"])
    watch.on_front_change("spotify", "arc")
    assert len(reader.warms) == 1 and len(reader.reads) == 2
    clock.advance(SD.WARM_RETRY_S + 1)
    watch.on_media_change(started=["Spotify.exe"])
    assert len(reader.warms) == 2


def test_learned_wording_is_kept_by_code() -> None:
    watch, reader, store, _m, _l, _c = make()
    reader.result.learned = {"play": "{name} abspielen", "pause": "{name} anhalten", "grid": "Deine Sammlung"}
    watch.on_media_change(started=["Spotify.exe"])
    kept = json.loads(store.state[SD.LABELS_KEY])
    assert kept["play"] == "{name} abspielen" and kept["pause"] == "{name} anhalten"
    reader.result.learned = None
    watch.on_front_change("spotify", "arc")
    assert reader.reads[-1]["learned"]["play"] == "{name} abspielen"


def test_no_name_link_or_id_in_any_log_or_the_status() -> None:
    game = [False]
    watch, reader, store, _m, logs, clock = make(game)
    watch.on_layout([{"hwnd": 9001, "pid": 4242}])
    watch.on_media_change(started=["Spotify.exe"])
    reader.state_value = "covered"
    watch.on_front_change("spotify", "arc")
    clock.advance(61)
    watch.beat()
    blob = json.dumps(logs, default=str) + store.state.get(SD.STATUS_KEY, "")
    for word in NAMES + ("spotify:playlist", "1A2b3C4d5E6f7G8h9I0jKl", "xpui"):
        assert word not in blob, word


def test_settings_from_the_privacy_table() -> None:
    s = SD.SpotifySettings.from_dict({"enabled": False, "read_budget_s": 9, "state_probe": False, "bogus": 1})
    assert s.enabled is False and s.read_budget_s == 2.0 and s.state_probe is False
    assert SD.SpotifySettings.from_dict(None) == SD.SpotifySettings()
    watch, reader, _s, _m, _l, _c = make(settings=SD.SpotifySettings(enabled=False))
    watch.on_media_change(started=["Spotify.exe"])
    assert reader.reads == []


def test_the_session_is_matched_to_the_spotify_windows_own_process(monkeypatch: pytest.MonkeyPatch) -> None:
    watch, reader, _s, moments, _l, _c = make()
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": STORE_ID, "family": "", "image": "Spotify.exe"})
    watch.on_front_change("arc", "spotify")            # the window is seen: its process is known
    reads = len(reader.reads)
    watch.on_media_change(started=["Spotify.exe"])      # a classic id: not this Store build's session
    assert len(reader.reads) == reads
    watch.on_media_change(started=[(STORE_ID, "Spotify", "x")])
    assert len(reader.reads) == reads + 1


def test_a_game_coming_to_the_front_during_the_warm_up_holds_the_read() -> None:
    game = [False]
    watch, reader, _s, moments, logs, _c = make(game)
    original = reader.warm

    def warm_then_game(hwnd: int, budget_s: float) -> SD.WarmResult:
        game[0] = True
        return original(hwnd, budget_s)

    reader.warm = warm_then_game   # type: ignore[method-assign]
    watch.on_media_change(started=["Spotify.exe"])
    assert reader.warms and reader.reads == [] and moments == []
    assert logs[-1][1]["status"] == "held_game"
