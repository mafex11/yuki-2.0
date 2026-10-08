"""capQ2b (report 104): what makes a program a game is a machine fact, never a share of the GPU.

On 8 Oct a music program, maximised and holding the microphone, drew 16% of the GPU (the old line was 15%): the call
worker took its microphone for a game's voice chat, the trace's game gate held for 18 minutes and the user was asked
about its "game voice". Now a program is a game only by the fixed anti-cheat list (or the user's additions), Windows'
own game detection (GameConfigStore, by full path), Direct3D exclusive full screen (now, or learned from it), and an
entry learned from the GPU share alone is dropped by code on the next start.

Offline: invented programs and paths, stubbed Windows facts, temp databases. Nothing reads the live desktop, the
microphone, the speakers or the GPU (the one real read is of Windows' game store, read-only).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from test_av_signals import Stubs, _worker  # noqa: F401 - shared stubs
from yuki.memory import gamegate as G
from yuki.memory.avsignals import Holder, gaming_with_voice
from yuki.memory.gamegate import GameGate, GameSet

BEATS = r"C:\Program Files\Beatline Audio\Beatline 24\BL64.exe"            # an invented music program
SHOOTER = r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe"
STARFALL = r"D:\Games\Starfall\Starfall.exe"                              # an invented game Windows detected
OTHER_JAVA = r"C:\Program Files\Java\bin\javaw.exe"
GAME_JAVA = r"C:\Users\someone\AppData\Roaming\Blocklauncher\java\bin\javaw.exe"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Log(list):
    def __call__(self, type: str, **fields: Any) -> None:
        self.append((type, fields))

    def of(self, type: str) -> list[dict[str, Any]]:
        return [f for t, f in self if t == type]


@pytest.fixture()
def facts(monkeypatch):
    """Windows' game store and the shell's D3D state, stubbed for :func:`gamegate.judge_program` (no live gate)."""
    state = {"windows": frozenset({G._norm_path(STARFALL), G._norm_path(GAME_JAVA)}), "d3d": False}
    monkeypatch.setattr(G, "windows_games", lambda: state["windows"])
    monkeypatch.setattr(G, "_default_d3d", lambda: state["d3d"])
    monkeypatch.setattr(G, "_LIVE", None)
    monkeypatch.setattr(G, "_FALLBACK", None)
    return state


def _holder(exe: str) -> Holder:
    return Holder(exe.rsplit("\\", 1)[-1], False, exe, 0.0)


# -- the judgement ----------------------------------------------------------------------------------------------------


def test_a_music_program_full_screen_with_the_mic_and_16_percent_gpu_is_not_a_game(facts):
    gaming, why = gaming_with_voice(_holder(BEATS), fullscreen=True, shows_page=False, pids=[42],
                                    gpu_fn=lambda pids: 16.0)
    assert not gaming and why["game"] == "no_fact" and why["gpu_share"] == 16.0   # the share is logged, not decisive
    gaming, _ = gaming_with_voice(_holder(BEATS), fullscreen=True, shows_page=False, pids=[42],
                                  gpu_fn=lambda pids: 90.0)
    assert not gaming                                                              # whatever it draws


def test_a_game_by_the_list_by_windows_detection_or_by_exclusive_full_screen(facts):
    by_list = gaming_with_voice(_holder(SHOOTER), fullscreen=True, shows_page=False, gpu_fn=lambda p: 1.0)
    assert by_list[0] and by_list[1]["game"] == "list"
    by_windows = gaming_with_voice(_holder(STARFALL), fullscreen=True, shows_page=False, gpu_fn=None)
    assert by_windows[0] and by_windows[1]["game"] == "windows" and "gpu_share" not in by_windows[1]
    assert not gaming_with_voice(_holder(r"E:\Elsewhere\Starfall.exe"), fullscreen=True, shows_page=False,
                                 gpu_fn=None)[0]                                   # the same name elsewhere: no
    assert gaming_with_voice(_holder(GAME_JAVA), fullscreen=True, shows_page=False, gpu_fn=None)[0]
    assert not gaming_with_voice(_holder(OTHER_JAVA), fullscreen=True, shows_page=False, gpu_fn=None)[0]
    facts["d3d"] = True                                                            # exclusive full screen now
    by_d3d = gaming_with_voice(_holder(r"C:\Games\Skyrider\Skyrider.exe"), fullscreen=True, shows_page=False,
                               gpu_fn=None)
    assert by_d3d[0] and by_d3d[1]["game"] == "d3d"
    # still never a game in a window, on a web page, or holding the camera
    assert not gaming_with_voice(_holder(SHOOTER), fullscreen=False, shows_page=False, gpu_fn=None)[0]
    assert not gaming_with_voice(_holder(SHOOTER), fullscreen=True, shows_page=True, gpu_fn=None)[0]


def test_judge_program_on_a_game_set():
    games = GameSet.load(None, {"skyrider": {"stretches": 2, "evidence": G.SOUND_EVIDENCE}}, [STARFALL])
    no_d3d = lambda: False  # noqa: E731
    assert G.judge_program("BL64.exe", BEATS, games=games, d3d_fn=no_d3d) == (False, "no_fact")
    assert G.judge_program("VALORANT.exe", "", games=games, d3d_fn=no_d3d) == (True, "list")
    assert G.judge_program("Starfall.exe", STARFALL, games=games, d3d_fn=no_d3d) == (True, "windows")
    assert G.judge_program("Skyrider.exe", "", games=games, d3d_fn=no_d3d) == (True, "learned")
    assert G.judge_program("BL64.exe", BEATS, games=games, d3d_fn=lambda: True) == (True, "d3d")


# -- the gate ---------------------------------------------------------------------------------------------------------


def _gate(procs: dict[int, tuple[str, str]], *, fullscreen=frozenset(), gpu: float | None = None, d3d=False,
          store: Any = None, windows=frozenset()):
    clock, log = _Clock(), _Log()
    s = {"d3d": d3d}
    gate = GameGate(store=store, log=log, fullscreen_fn=lambda hwnd: hwnd in fullscreen, d3d_fn=lambda: s["d3d"],
                    gpu_fn=lambda pids: gpu, scan_fn=lambda: [(p, n) for p, (n, _e) in procs.items()],
                    image_fn=lambda pid: procs.get(pid, ("", ""))[0], own_fn=lambda pid: False, clock=clock,
                    windows_fn=lambda: frozenset(G._norm_path(w) for w in windows),
                    exe_fn=lambda pid: procs.get(pid, ("", ""))[1])
    return gate, clock, log, s


def test_the_gate_never_holds_for_the_music_program_and_never_learns_it():
    gate, clock, log, _s = _gate({11: ("BL64.exe", BEATS), 12: ("Discord.exe", r"C:\Apps\Discord.exe")},
                                 fullscreen={1}, gpu=16.0)
    for _ in range(3):                                            # three separate maximised stretches
        gate.on_foreground(5, 12)
        assert not gate.on_foreground(1, 11).hold
        for after in G.CHECK_AFTER_S:
            clock.t += after
            assert not gate.on_check().hold
    assert not gate.holds() and not log.of("game_gate_learned") and gate.stats()["candidates"] == 0
    assert log.of("game_gate_not_game") == [{"app": "bl64", "why": "not_exclusive", "gpu_share": 16.0}]


def test_the_gate_holds_for_a_game_windows_detected_by_its_path_only():
    procs = {21: ("Starfall.exe", STARFALL), 22: ("javaw.exe", OTHER_JAVA), 12: ("Discord.exe", r"C:\Apps\Discord.exe")}
    gate, _clock, log, _s = _gate(procs, windows={STARFALL, GAME_JAVA})
    d = gate.on_foreground(5, 12, trigger="start")
    assert d.hold and d.games == ("starfall",) and d.sources == ("windows",)       # running behind Discord
    assert gate.stats()["windows_detected"] == 2
    d = gate.on_foreground(2, 21)
    assert d.front == "starfall"
    gate.on_exit(21)
    assert not gate.holds()                                        # another javaw.exe is no game


def _gate_late_windows(procs):
    """A gate whose Windows game store starts empty and can gain a path later (Windows writes it after the window)."""
    clock, log = _Clock(), _Log()
    windows: set[str] = set()
    gate = GameGate(log=log, fullscreen_fn=lambda hwnd: hwnd == 1, d3d_fn=lambda: False, gpu_fn=lambda pids: 30.0,
                    scan_fn=lambda: [(p, n) for p, (n, _e) in procs.items()],
                    image_fn=lambda pid: procs.get(pid, ("", ""))[0], own_fn=lambda pid: False, clock=clock,
                    windows_fn=lambda: frozenset(G._norm_path(w) for w in windows),
                    exe_fn=lambda pid: procs.get(pid, ("", ""))[1])
    return gate, clock, log, windows


def test_a_game_windows_detects_after_its_window_came_up_holds_at_the_next_check():
    """Review F1: a first run of a game no list names; Windows adds it to its store after it came to the front."""
    procs = {21: ("Starfall.exe", STARFALL), 12: ("Discord.exe", r"C:\Apps\Discord.exe")}
    gate, clock, _log, windows = _gate_late_windows(procs)
    gate.on_foreground(5, 12, trigger="start")
    assert not gate.on_foreground(1, 21).hold
    windows.add(STARFALL)                                          # Windows writes the game's path now
    clock.t += G.CHECK_AFTER_S[0]
    d = gate.on_check()
    assert d.hold and d.sources == ("windows",) and gate.holds()
    assert gate.next_check_in() is None                            # a game now: no longer measured


def test_a_game_windows_detects_later_holds_at_the_next_beat_without_a_window_switch():
    procs = {21: ("Starfall.exe", STARFALL), 12: ("Discord.exe", r"C:\Apps\Discord.exe")}
    gate, clock, _log, windows = _gate_late_windows(procs)
    gate.on_foreground(5, 12, trigger="start")
    gate.on_foreground(2, 21)                                      # windowed: no checks planned
    assert gate.next_check_in() is None and not gate.holds()
    windows.add(STARFALL)
    clock.t += G.BEAT_EVERY_S
    d = gate.beat()
    assert d is not None and d.hold and d.games == ("starfall",) and gate.holds()
    windows.discard(STARFALL)                                      # the music program's path never joins: nothing
    procs[11] = ("BL64.exe", BEATS)
    gate.on_exit(21)
    clock.t += G.BEAT_EVERY_S
    assert not gate.beat().hold


def test_exclusive_full_screen_holds_at_once_and_teaches_the_program():
    gate, clock, log, s = _gate({30: ("Skyrider.exe", r"C:\Games\Skyrider.exe"), 12: ("Discord.exe", "")},
                                fullscreen={4})
    for _ in range(G.LEARN_AFTER_STRETCHES):
        s["d3d"] = False
        gate.on_foreground(5, 12)
        s["d3d"] = True
        assert gate.on_foreground(4, 30).reasons == ("d3d_fullscreen",)
        clock.t += G.CHECK_AFTER_S[0]
        gate.on_check()
    s["d3d"] = False
    d = gate.on_foreground(5, 12)                                  # windowed or behind: still a game now
    assert d.hold and d.sources == ("learned",) and log.of("game_gate_learned")[0]["game"] == "skyrider"


# -- the learned set re-checked on start -------------------------------------------------------------------------------


def test_a_wrongly_learned_music_program_is_dropped_on_the_next_start(tmp_path):
    from yuki.memory import trace_store
    from yuki.memory.store import Store

    store = Store.open(tmp_path / "memory.db")
    try:
        wrong = {"bl64": {"stretches": 3, "gpu_share": 16.0, "first_seen": 1.0, "last_seen": 2.0,
                          "evidence": "fullscreen_gpu"},
                 "oldone": {"first_seen": 1.0, "gpu_share": 50.0},                       # a C0 entry, no evidence
                 "skyrider": {"stretches": 2, "first_seen": 1.0, "last_seen": 2.0, "evidence": G.SOUND_EVIDENCE}}
        trace_store.save_game_evidence(store, wrong)
        store.set_nudge_state(G.LEARNED_STATE, json.dumps(wrong))
        log = _Log()
        gate = GameGate(store=store, log=log, fullscreen_fn=lambda h: False, d3d_fn=lambda: False,
                        gpu_fn=lambda p: None, scan_fn=lambda: [(11, "BL64.exe")], image_fn=lambda pid: "BL64.exe",
                        own_fn=lambda pid: False, clock=_Clock(), windows_fn=lambda: frozenset(),
                        exe_fn=lambda pid: BEATS)
        assert sorted(f["app"] for f in log.of("game_gate_unlearned")) == ["bl64", "oldone"]
        assert all(f["why"] == "no_windows_fact" for f in log.of("game_gate_unlearned"))
        assert set(trace_store.load_game_evidence(store)) == {"skyrider"}             # the table, by code
        assert set(json.loads(store.nudge_state(G.LEARNED_STATE))) == {"skyrider"}
        assert not gate.on_foreground(1, 11, trigger="start").hold                    # the music program runs: no
        assert gate.games().is_game("Skyrider.exe")                                    # a Windows fact stays
        log2 = _Log()
        GameGate(store=store, log=log2, windows_fn=lambda: frozenset())
        assert not log2.of("game_gate_unlearned")                                      # once: nothing left to drop
    finally:
        store.close()


# -- the call recorder's game voice session ---------------------------------------------------------------------------


def test_the_game_voice_session_and_its_question_only_for_a_game(facts, tmp_path):
    from yuki.memory.store import Store

    now = time.time()
    store = Store.open(tmp_path / "calls.db")
    try:
        stubs = Stubs()
        stubs.hold("mic", BEATS, now - 60)
        stubs.play(42, BEATS)
        w = _worker(store, stubs)
        w.foreground_fn = lambda: {"hwnd": 1, "pid": 42, "exe": BEATS}
        w.gpu_fn = lambda pids: 16.0
        w.step(now)
        w.step(now + 5)
        assert w.game is None and w._gaming is None and not store.calls()   # no game row: nothing to ask about
    finally:
        store.close()
    store = Store.open(tmp_path / "calls2.db")
    try:
        stubs = Stubs()
        stubs.hold("mic", SHOOTER, now - 60)
        stubs.play(10, SHOOTER)
        w = _worker(store, stubs)
        w.foreground_fn = lambda: {"hwnd": 1, "pid": 10, "exe": SHOOTER}
        w.gpu_fn = lambda pids: 2.0                                     # a menu screen: little GPU, still a game
        w.step(now)
        assert w.game is not None and store.call_record(w.game.call_id).kind == "game"
    finally:
        store.close()


# -- Windows' store, read-only -----------------------------------------------------------------------------------------


def test_windows_game_store_reads_and_caches_until_it_changes():
    reads = []
    cache = G.WindowsGames(lambda: reads.append(1) or frozenset({"c:\\games\\a.exe"}), watch=False)
    assert cache.paths() == frozenset({"c:\\games\\a.exe"}) and cache.paths() and len(reads) == 1
    t = {"now": 0.0}

    class _NoWatch:                                                  # the store's key missing: no notification
        ok = False

        def __init__(self, subkey: str) -> None:
            pass

        def changed(self) -> bool:
            return False

    import yuki.memory.gamegate as gg

    real_watch = gg._RegWatch
    gg._RegWatch = _NoWatch
    try:
        unwatched = G.WindowsGames(lambda: reads.append(1) or frozenset(), clock=lambda: t["now"])
        unwatched.paths()
        unwatched.paths()
        assert unwatched.reads == 1                                  # not on every ask
        t["now"] += G.WindowsGames.UNWATCHED_EVERY_S
        unwatched.paths()
        assert unwatched.reads == 2                                  # a backstop, minutes apart
    finally:
        gg._RegWatch = real_watch
    real = G.read_windows_games()                                    # the real store, read-only
    assert isinstance(real, frozenset) and all(p == G._norm_path(p) for p in real)
    assert not any(p.endswith("\\bl64.exe") or p.endswith("\\fl64.exe") for p in real)
