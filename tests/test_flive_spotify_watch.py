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
    """The stub window's process: an unpackaged Spotify.exe, pid 4242 (no real process is looked up)."""
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
    monkeypatch.setattr(SD, "is_spotify_pid", lambda pid: pid == 4242)
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
        self.stop_fns: list[Any] = []
        self.reads: list[dict[str, Any]] = []
        self.warm_built = True
        self.result = SD.SpotifyRead(status="ok", built=True, player=True, slider=40.0, slider_source="english",
                                     grid=True, rows=_rows(), links={"Rainy Commute": URI_A},
                                     label_source="english", elements=330, ms=120.0)

    def find(self) -> SD.WindowRef | None:
        return self.ref

    def state(self, hwnd: int) -> str:
        return self.state_value

    def warm(self, hwnd: int, budget_s: float, **kw: Any) -> SD.WarmResult:
        self.warms.append(budget_s)
        self.stop_fns.append(kw.get("stop_fn"))
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


def make(game: list[bool] | None = None, *, reader: Any = None,
         **kw: Any) -> tuple[SD.SpotifyWatch, Any, Store, list, list, Clock]:
    reader, store, moments, logs, clock = reader or Reader(), Store(), [], [], Clock()
    game = game if game is not None else [False]
    kw.setdefault("local_game_fn", lambda: False)   # never the real foreground window in a test
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


def _labels(store: Store) -> dict | None:
    raw = store.state.get(SD.LABELS_KEY)
    return json.loads(store.cipher.decrypt(raw)) if raw else None


class TreeReader(Reader):
    """Reads a fixture tree through the real reader code (tests/test_flive_spotify_reader.py's trees)."""

    def __init__(self, root: Any) -> None:
        super().__init__()
        self.root = root

    def read(self, hwnd: int, budget_s: float, **kw: Any) -> SD.SpotifyRead:
        from tests.test_flive_spotify_reader import read as read_fixture

        self.reads.append({"budget": budget_s, **kw})
        args = {k: kw[k] for k in ("want", "learned", "session_playing", "known_names") if k in kw}
        return read_fixture(self.root, **args)


def test_learned_wording_is_kept_by_code_only_after_a_row_flips() -> None:
    from tests.test_flive_spotify_reader import GERMANISH, LIB, spotify_tree

    reader = TreeReader(spotify_tree(labels=GERMANISH))
    watch, _r, store, moments, logs, clock = make(reader=reader)
    watch.on_media_change(started=["Spotify.exe"])          # Late Lanterns playing: a candidate only
    assert SD.LABELS_KEY not in store.state and watch.learned_labels() is None
    assert [m for m in moments if m["kind"] == "media_context"] == []      # no play state from a candidate
    assert watch.stats()["label_candidates"] == 1
    clock.advance(SD.CONTEXT_REPEAT_S + 1)
    watch.on_media_change(started=["Spotify.exe"])          # the same playlist again: no flip, still a candidate
    assert SD.LABELS_KEY not in store.state
    reader.root = spotify_tree([(n, o, n == "Rainy Commute") for n, o, _p in LIB], labels=GERMANISH)
    clock.advance(SD.CONTEXT_REPEAT_S + 1)
    watch.on_media_change(started=["Spotify.exe"])          # another playlist: Late Lanterns flipped -> learned
    raw = store.state[SD.LABELS_KEY]
    assert raw.startswith("enc:") and "abspielen" not in raw                 # kept encrypted
    kept = _labels(store)
    assert kept["play"] == "{name} abspielen" and kept["pause"] == "{name} anhalten"
    ctx = [m for m in moments if m["kind"] == "media_context"]
    assert len(ctx) == 1 and ctx[0]["entity"] == "Rainy Commute" and ctx[0]["facts"]["label_source"] == "learned"
    watch.on_front_change("spotify", "arc")
    assert reader.reads[-1]["learned"]["play"] == "{name} abspielen"
    actions = [f["action"] for t, f in logs if t == "spotify_read" and f.get("trigger") == "labels"]
    assert actions == ["candidate", "confirmed"]
    # a fresh watcher on the same store reads the encrypted wording back
    again = SD.SpotifyWatch(store=store, reader=Reader(), game_front_fn=lambda: False, local_game_fn=lambda: False,
                            spawn=lambda fn: fn())
    assert again.learned_labels()["pause"] == "{name} anhalten"


def test_owner_button_wording_is_never_learned_or_kept() -> None:
    """Review r1 M1: an album plays (no Library row in view plays) and one row of three is by another owner."""
    from tests.test_flive_spotify_reader import GERMANISH, MIRA_IVO, spotify_tree

    reader = TreeReader(spotify_tree(MIRA_IVO, labels=GERMANISH))
    watch, _r, store, moments, logs, clock = make(reader=reader)
    for _ in range(3):
        watch.on_media_change(started=["Spotify.exe"])
        clock.advance(SD.CONTEXT_REPEAT_S + 1)
    reader.root = spotify_tree(MIRA_IVO + [("Night Bus", "Ivo Brandt", False)], labels=GERMANISH)
    watch.on_media_change(started=["Spotify.exe"])          # two rows by Ivo: the candidate is contradicted
    assert watch.stats()["label_candidates"] == 0
    reader.root = spotify_tree(MIRA_IVO, labels=GERMANISH)
    reader.state_value = "minimised"
    watch.on_front_change("spotify", "arc")
    clock.advance(61)
    watch.beat()                                             # the daily probe reads the Library too
    assert SD.LABELS_KEY not in store.state and watch.learned_labels() is None
    assert [m for m in moments if m["kind"] == "media_context"] == []
    assert watch.stats()["labels_learned"] == 0
    blob = json.dumps(logs, default=str) + json.dumps(store.state)
    for word in ("Mira", "Okafor", "Ivo", "Brandt", "Liste"):
        assert word not in blob, word


def test_learned_wording_is_dropped_when_contradicted_or_the_data_wording_shows() -> None:
    from tests.test_flive_spotify_reader import GERMANISH, LIB, spotify_tree

    def learned_watch(root: Any) -> tuple[SD.SpotifyWatch, TreeReader, Store, list]:
        reader = TreeReader(root)
        watch, _r, store, _m, logs, _c = make(reader=reader)
        watch._keep_learned(SD.WordingGuess("{name} abspielen", "{name} anhalten", "Deine Sammlung"))
        assert _labels(store) is not None
        return watch, reader, store, logs

    # two rows read "anhalten" at once: not a play button -> dropped
    two = [(n, o, n in ("Late Lanterns", "Rainy Commute")) for n, o, _p in LIB]
    watch, _reader, store, logs = learned_watch(spotify_tree(two, labels=GERMANISH))
    watch.on_media_change(started=["Spotify.exe"])
    assert SD.LABELS_KEY not in store.state and watch.learned_labels() is None
    assert [f.get("reason") for t, f in logs if f.get("action") == "dropped"] == ["two_playing"]
    # Spotify switched to English: the data's wording takes over -> dropped
    watch, _reader, store, logs = learned_watch(spotify_tree())
    watch.on_media_change(started=["Spotify.exe"])
    assert SD.LABELS_KEY not in store.state
    assert [f.get("reason") for t, f in logs if f.get("action") == "dropped"] == ["data"]
    # a value kept in clear (never written by this code) is not trusted
    store = Store()
    store.state[SD.LABELS_KEY] = json.dumps({"play": "{name} x", "pause": "{name} y"})
    assert SD.SpotifyWatch(store=store, reader=Reader(), local_game_fn=lambda: False).learned_labels() is None


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

    def warm_then_game(hwnd: int, budget_s: float, **kw: Any) -> SD.WarmResult:
        game[0] = True
        return original(hwnd, budget_s, **kw)

    reader.warm = warm_then_game   # type: ignore[method-assign]
    watch.on_media_change(started=["Spotify.exe"])
    assert reader.warms and reader.reads == [] and moments == []
    assert logs[-1][1]["status"] == "held_game"


# ---------------------------------------------------------------------------------------------------------------------
# Review round 1 (L4, L5, L9, L10)
# ---------------------------------------------------------------------------------------------------------------------


def test_a_warm_up_gets_the_game_check_and_a_stopped_one_is_not_a_failure() -> None:
    watch, reader, _s, moments, logs, _c = make()
    original = reader.warm

    def stopped(hwnd: int, budget_s: float, **kw: Any) -> SD.WarmResult:
        original(hwnd, budget_s, **kw)
        return SD.WarmResult(before=7, after=40, cold=True, stopped=True, ms=600.0)

    reader.warm = stopped   # type: ignore[method-assign]
    full = reader.result
    reader.result = SD.SpotifyRead(status="not_built", elements=40)   # the tree was still building when it stopped
    watch.on_media_change(started=["Spotify.exe"])
    assert reader.stop_fns and callable(reader.stop_fns[0])
    warm_log = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "warm"][-1]
    assert warm_log["status"] == "held_game" and warm_log["stopped"] is True
    reader.warm, reader.result = original, full  # type: ignore[method-assign]
    watch.on_media_change(started=["Spotify.exe"])          # warmed again at once (not the 10-minute "failed" wait)
    assert len(reader.warms) == 2


def test_warm_tree_stops_between_polls_when_a_game_comes_to_the_front() -> None:
    from tests.test_flive_spotify_reader import CHILD, HWND, GrowingAutomation, _stub_time, spotify_tree

    clock, now, sleep = _stub_time()
    tree = SD.UiaTree(GrowingAutomation(clock, spotify_tree()), HWND)
    calls = []

    def game() -> bool:
        calls.append(1)
        return len(calls) >= 3   # a game comes to the front during the third check

    result = SD.warm_tree(tree, [CHILD], clock=now, sleep=sleep, stop_fn=game)
    assert result.stopped is True and result.built is False and result.polls == 1
    assert clock[0] - 1000.0 < 1.0                          # stopped long before the 3 s budget


def test_the_gate_is_ored_with_the_local_foreground_read() -> None:
    watch, reader, _s, moments, logs, _c = make(local_game_fn=lambda: True)   # the gate says no game; lagging
    watch.on_media_change(started=["Spotify.exe"])
    assert reader.reads == [] and reader.warms == [] and moments == []
    assert logs[-1][1]["status"] == "held_game"


def test_volume_change_carries_the_previous_read_time_and_the_play_start_baseline() -> None:
    watch, reader, _s, moments, _l, clock = make()
    watch.on_media_change(started=["Spotify.exe"])          # play-start read: slider 40 is the baseline
    t_play = clock.wall
    clock.advance(300)
    reader.result.slider = 55.0
    watch.on_front_change("spotify", "arc")                 # the first leave-front read already compares
    vc = [m for m in moments if m["kind"] == "volume_change"]
    assert len(vc) == 1 and vc[0]["value_from"] == 40.0 and vc[0]["value"] == 55.0
    assert vc[0]["facts"]["prev_read_at"] == round(t_play, 3) and vc[0]["at"] == clock.wall
    assert vc[0]["facts"]["trigger"] == "leave_front"


def test_a_spotify_never_brought_to_the_front_is_probed_from_layout_changes() -> None:
    watch, reader, store, _m, logs, clock = make()
    reader.state_value = "visible"
    watch.on_layout([{"hwnd": 1, "pid": 77, "share": 0.9}, {"hwnd": 9001, "pid": 4242, "share": 0.4}], foreground=1)
    clock.advance(30)
    watch.on_layout([{"hwnd": 1, "pid": 77, "share": 0.9}, {"hwnd": 9001, "pid": 4242, "share": 0.4}], foreground=1)
    watch.beat()
    assert not any(t == "spotify_probe" for t, _f in logs)  # the same place: the timer was not restarted
    clock.advance(31)
    watch.beat()
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 1 and probes[0]["probe_state"] == "visible"
    # minimised (gone from the layout): the timer restarts, the probe runs 60 s later
    reader.state_value = "minimised"
    watch.on_layout([{"hwnd": 1, "pid": 77, "share": 1.0}], foreground=1)
    clock.advance(59)
    watch.beat()
    assert len([1 for t, _f in logs if t == "spotify_probe"]) == 1
    clock.advance(2)
    watch.beat()
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 2 and probes[1]["probe_state"] == "minimised"


def test_a_spotify_minimised_from_the_start_is_probed_after_its_play_start() -> None:
    watch, reader, _s, _m, logs, clock = make()
    reader.state_value = "minimised"
    watch.on_media_change(started=["Spotify.exe"])          # never in front, never on the layout
    clock.advance(61)
    watch.beat()
    probes = [f for t, f in logs if t == "spotify_probe"]
    assert len(probes) == 1 and probes[0]["probe_state"] == "minimised"


def test_with_only_the_image_known_the_store_session_still_counts() -> None:
    watch, reader, _s, _m, _l, _c = make()                   # the autouse identity: image only (package unreadable)
    watch.on_front_change("arc", "spotify")                 # the window is seen: its process is known
    reads = len(reader.reads)
    watch.on_media_change(started=[(STORE_ID, "Spotify", "x")])
    assert len(reader.reads) == reads + 1
    watch.on_media_change(started=[("308046B0AF4A39CB", "Chrome", "x")])
    assert len(reader.reads) == reads + 1
