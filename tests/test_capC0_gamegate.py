"""Report 78 §4.5, phase C0: the hard game gate, logging only.

The running gate (``game_running``) holds while a game program runs (in front or not, windowed or full screen), in
Direct3D exclusive full screen, or while a game voice session is open; since phase F (fGate) the input gate
(``holds``) holds only while a game is in front (or D3D full screen), see tests/test_fGate_gate.py. It keys on game programs, never on Vanguard's always-on tray, and a
caption-less maximised app that does not draw like a game (FL Studio) never counts.

Offline: invented process data (pids, image names, GPU shares) and a fake clock. Two read-only checks of the real
PC: the real process list run through the gate's rule, and one start/stop of the gate's thread (it only listens for
the foreground window changing; a harmless child process stands in for a game).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from yuki.memory import gamegate as G
from yuki.memory.gamegate import GameGate, GameSet, GateView, decide, judge_learn

VALORANT = "VALORANT-Win64-Shipping.exe"
FL = "FL64.exe"
VANGUARD_TRAY = "vgtray.exe"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Store:
    """``nudge_state`` only, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def nudge_state(self, name: str) -> str | None:
        return self.rows.get(name)

    def set_nudge_state(self, name: str, value: str | None) -> None:
        if value is None:
            self.rows.pop(name, None)
        else:
            self.rows[name] = value


class _Log(list):
    def __call__(self, type: str, **fields: Any) -> None:
        self.append((type, fields))

    def of(self, type: str) -> list[dict[str, Any]]:
        return [f for t, f in self if t == type]


def _gate(*, procs: list[tuple[int, str]], fullscreen: set[int] = frozenset(), gpu: dict[int, float | None] | None = None,
          d3d: bool = False, session: bool = False, store: _Store | None = None, privacy: Path | None = None):
    clock, log = _Clock(), _Log()
    images = dict(procs)
    gate = GameGate(store=store, privacy_path=privacy, log=log, game_session_fn=lambda: session,
                    fullscreen_fn=lambda hwnd: hwnd in fullscreen, d3d_fn=lambda: d3d,
                    gpu_fn=lambda pids: (gpu or {}).get(list(pids)[0]), scan_fn=lambda: list(images.items()),
                    image_fn=lambda pid: images.get(pid, ""), own_fn=lambda pid: False, clock=clock,
                    windows_fn=lambda: frozenset(), exe_fn=lambda pid: "")
    return gate, clock, log, images


# -- the lists -------------------------------------------------------------------------------------------------------


def test_the_fixed_list_names_game_programs_never_the_anti_cheat_tray(tmp_path: Path) -> None:
    games = GameSet.load(tmp_path / "missing.toml")
    assert games.is_game(VALORANT) and games.is_game("VALORANT.exe") and games.is_game("valorant-win64-shipping")
    assert not games.is_game(VANGUARD_TRAY) and not games.is_game("vgc.exe") and not games.is_game("RiotClientServices.exe")
    assert not games.is_game(FL) and not games.is_game("FL.exe") and not games.is_game("")


def test_the_user_can_add_games_but_never_remove_the_listed_ones(tmp_path: Path) -> None:
    mine = tmp_path / "privacy.toml"
    mine.write_text('[trace]\ngame_processes = ["Fieldrunner.exe"]\n', encoding="utf-8")
    games = GameSet.load(mine)
    assert games.is_game("fieldrunner.exe") and games.is_game(VALORANT)
    mine.write_text("[trace]\ngame_processes = []\n", encoding="utf-8")          # an emptied copy
    assert GameSet.load(mine).is_game(VALORANT)
    mine.write_text("not = [valid toml", encoding="utf-8")                        # a broken copy
    assert GameSet.load(mine).is_game(VALORANT)


def test_the_packaged_privacy_file_still_parses_for_the_watcher() -> None:
    from yuki.memory.privacy import DEFAULT_PATH, PrivacyRules

    rules = PrivacyRules.load(DEFAULT_PATH)
    assert "1password.exe" in rules.blocked_processes and rules.pause_when_fullscreen


# -- the rule, with invented process data ------------------------------------------------------------------------------


def test_valorant_holds_the_gate_windowed_full_screen_or_behind_another_app() -> None:
    games = GameSet.load()
    windowed = decide(GateView(front=VALORANT, front_fullscreen=False, running=frozenset({VANGUARD_TRAY})), games)
    assert windowed.hold and windowed.reasons == ("game_running",) and windowed.front == "valorant-win64-shipping"
    assert windowed.front_fullscreen is False and windowed.sources == ("list",)
    full = decide(GateView(front=VALORANT, front_fullscreen=True), games)
    assert full.hold and full.front_fullscreen
    behind = decide(GateView(front="Discord.exe", running=frozenset({VALORANT, VANGUARD_TRAY})), games)
    assert behind.hold and behind.front == "" and behind.games == ("valorant-win64-shipping",)
    launching = decide(GateView(front="VALORANT.exe"), games)                  # the launch screen
    assert launching.hold


def test_the_anti_cheat_tray_or_fl_studio_full_screen_never_hold_it() -> None:
    games = GameSet.load()
    tray = decide(GateView(front="Chrome.exe", running=frozenset({VANGUARD_TRAY, "vgc.exe", "RiotClientServices.exe"})),
                  games)
    assert not tray.hold and tray.reasons == ()
    fl = decide(GateView(front=FL, front_fullscreen=True, running=frozenset({FL, VANGUARD_TRAY})), games)
    assert not fl.hold and fl.front == ""


def test_d3d_exclusive_full_screen_and_a_game_voice_session_hold_it() -> None:
    games = GameSet.load()
    assert decide(GateView(front="Unknown.exe", d3d_fullscreen=True), games).reasons == ("d3d_fullscreen",)
    assert decide(GateView(front="Discord.exe", game_session=True), games).reasons == ("game_voice",)


def test_only_direct3d_exclusive_full_screen_teaches_a_new_game() -> None:
    """capQ2b: a Windows fact, never a share of the GPU (FL Studio maximised drew 16% one morning)."""
    assert judge_learn("Fieldrunner.exe", fullscreen=True, d3d=True) == (True, "d3d_exclusive")
    assert judge_learn(FL, fullscreen=True, d3d=False) == (False, "not_exclusive")
    assert judge_learn("Fieldrunner.exe", fullscreen=False, d3d=True) == (False, "not_fullscreen")
    assert judge_learn("Yuki.exe", fullscreen=True, d3d=True, own=True)[0] is False


# -- the gate fed events ------------------------------------------------------------------------------------------------


def test_fl_studio_maximised_is_measured_and_never_counts() -> None:
    gate, clock, log, _ = _gate(procs=[(10, VANGUARD_TRAY), (11, FL), (12, "Discord.exe")], fullscreen={1},
                                gpu={11: 2.5})
    assert not gate.on_foreground(1, 11).hold
    assert gate.next_check_in() == pytest.approx(G.CHECK_AFTER_S[0])
    for after in G.CHECK_AFTER_S:
        clock.t = 1000.0 + after
        assert not gate.on_check().hold
    assert gate.next_check_in() is None
    assert log.of("game_gate_not_game") == [{"app": "fl64", "why": "not_exclusive", "gpu_share": 2.5}]
    assert not log.of("game_gate_learned") and not any(f["hold"] for f in log.of("game_gate"))
    gate.on_foreground(5, 12)
    gate.on_foreground(1, 11)                                    # back to FL: measured again, said once
    for after in G.CHECK_AFTER_S:
        clock.t += after
        gate.on_check()
    assert len(log.of("game_gate_not_game")) == 1 and not gate.holds()


def test_valorant_windowed_then_alt_tab_then_exit_is_logged_at_each_change() -> None:
    gate, clock, log, images = _gate(procs=[(10, VANGUARD_TRAY), (12, "Discord.exe")])
    gate.on_foreground(5, 12)
    assert not gate.holds() and log.of("game_gate") == []
    images[20] = VALORANT
    d = gate.on_foreground(2, 20)                                 # VALORANT in a window
    assert d.hold and gate.holds() and log.of("game_gate")[-1] == {
        "trigger": "foreground", "hold": True, "hold_input": True, "scope": "front", "reasons": ["game_running"],
        "games": ["valorant-win64-shipping"], "front": "valorant-win64-shipping", "front_fullscreen": False,
        "sources": ["list"]}
    gate.on_foreground(5, 12)                                     # Alt+Tab to Discord: still running, input free
    assert gate.game_running() and not gate.holds() and log.of("game_gate")[-1]["front"] == ""
    assert log.of("game_gate")[-1]["hold"] and log.of("game_gate")[-1]["hold_input"] is False
    del images[20]
    clock.t += 3600
    gate.on_exit(20)
    assert not gate.game_running() and log.of("game_gate")[-1]["hold"] is False
    assert gate.stats()["holds"] == 1 and gate.stats()["held_s"] == pytest.approx(3600.0)


def test_a_game_already_running_at_start_holds_from_the_first_look() -> None:
    gate, _clock, log, _ = _gate(procs=[(10, VANGUARD_TRAY), (20, VALORANT), (12, "Chrome.exe")])
    assert gate.on_foreground(5, 12, trigger="start").hold
    assert log.of("game_gate")[0]["trigger"] == "start"


def test_a_new_game_seen_in_exclusive_full_screen_is_learned_and_remembered() -> None:
    """capQ2b: learned from Direct3D exclusive full screen (a Windows fact) in two stretches; the GPU share is kept
    in the log as measured, never decisive."""
    store = _Store()
    gate, clock, log, s = _live_gate(procs={30: "Fieldrunner.exe", 12: "Discord.exe"}, fullscreen={4},
                                     gpu={30: 61.0})
    gate._store = store

    def stretch() -> Any:
        s["d3d"] = False
        gate.on_foreground(5, 12)
        s["d3d"] = True                                            # the game goes exclusive full screen
        gate.on_foreground(4, 30)
        clock.t += G.CHECK_AFTER_S[0]
        d = gate.on_check()
        s["d3d"] = False
        gate.on_foreground(5, 12)
        return d

    stretch()                                                      # one stretch: a candidate, not yet a game
    assert not gate.decision().hold and not log.of("game_gate_learned")
    assert log.of("game_gate_evidence") == [{"app": "fieldrunner", "gpu_share": 61.0, "stretches": 1, "step": 1}]
    assert gate.stats()["candidates"] == 1 and gate.stats()["learned"] == 0
    stretch()                                                      # a second, separate stretch
    assert gate.decision().hold and gate.decision().sources == ("learned",)
    assert log.of("game_gate_learned") == [{"game": "fieldrunner", "gpu_share": 61.0, "stretches": 2}]
    kept = json.loads(store.rows[G.LEARNED_STATE])
    assert kept["fieldrunner"]["evidence"] == G.SOUND_EVIDENCE and kept["fieldrunner"]["stretches"] == 2
    assert set(kept) == {"fieldrunner"}
    again, *_ = _gate(procs=[(31, "Fieldrunner.exe"), (12, "Discord.exe")], store=store)
    assert again.on_foreground(5, 12).hold                          # remembered, and windowed or behind counts


def test_a_full_screen_app_left_before_its_check_is_not_judged() -> None:
    gate, clock, log, _ = _gate(procs=[(30, "Slides.exe"), (12, "Discord.exe")], fullscreen={4}, gpu={30: 90.0})
    gate.on_foreground(4, 30)
    gate.on_foreground(5, 12)
    assert gate.next_check_in() is None
    assert not log.of("game_gate_learned") and not log.of("game_gate_not_game")


def test_the_game_voice_session_holds_without_a_window_change() -> None:
    gate, *_ = _gate(procs=[(12, "Discord.exe")], session=True)
    assert gate.game_running() and not gate.holds()               # fGate: a voice session alone holds no input


def _live_gate(**state: Any):
    """A gate whose readings can change between events (``state`` is read at each call)."""
    clock, log = _Clock(), _Log()
    s = {"procs": {}, "fullscreen": set(), "gpu": {}, "d3d": False, "session": False, **state}
    gate = GameGate(log=log, game_session_fn=lambda: s["session"], fullscreen_fn=lambda hwnd: hwnd in s["fullscreen"],
                    d3d_fn=lambda: s["d3d"], gpu_fn=lambda pids: s["gpu"].get(list(pids)[0]),
                    scan_fn=lambda: list(s["procs"].items()), image_fn=lambda pid: s["procs"].get(pid, ""),
                    own_fn=lambda pid: False, clock=clock, windows_fn=lambda: frozenset(), exe_fn=lambda pid: "")
    return gate, clock, log, s


def test_d3d_exclusive_full_screen_holds_at_once_without_a_window_change() -> None:
    """Review fix: a game not on the list goes exclusive full screen with no foreground event."""
    gate, clock, log, s = _live_gate(procs={40: "Skyrider.exe", 12: "Discord.exe"})
    gate.on_foreground(5, 40)
    assert not gate.holds()
    s["d3d"] = True
    clock.t += G.D3D_FRESH_S                                      # the cached reading has aged out
    assert gate.holds()                                           # read live, no event needed
    assert not log.of("game_gate")                                # ...and logged at the next beat
    d = gate.beat()
    assert d is not None and d.hold and log.of("game_gate")[-1]["trigger"] == "d3d"
    assert log.of("game_gate")[-1]["reasons"] == ["d3d_fullscreen"]
    s["d3d"] = False
    clock.t += 1.0
    assert gate.beat() is None                                    # not due yet: no work between beats
    clock.t += G.BEAT_EVERY_S
    assert not gate.beat().hold and log.of("game_gate")[-1]["trigger"] == "d3d"


def test_a_game_voice_session_opening_and_closing_is_logged_by_the_beat() -> None:
    """Review fix: the voice session changes without a window change; the beat logs it as ``game_voice``."""
    gate, clock, log, s = _live_gate(procs={12: "Discord.exe"})
    gate.on_foreground(5, 12)
    s["session"] = True
    assert gate.game_running()
    clock.t += G.BEAT_EVERY_S
    gate.beat()
    assert log.of("game_gate") == [{"trigger": "game_voice", "hold": True, "hold_input": False, "scope": "none",
                                    "reasons": ["game_voice"], "games": [], "front": "", "front_fullscreen": False,
                                    "sources": []}]
    s["session"] = False
    clock.t += G.BEAT_EVERY_S
    gate.beat()
    assert log.of("game_gate")[-1]["trigger"] == "game_voice" and log.of("game_gate")[-1]["hold"] is False
    assert not gate.game_running()


def test_the_gate_fails_closed() -> None:
    """Review fix: no gate holds, and a started gate whose thread died holds whatever its last decision was."""
    assert G.gate_holds(None) is True
    gate, *_ = _live_gate(procs={12: "Discord.exe"})
    gate.on_foreground(5, 12)
    assert G.gate_holds(gate) is False
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    gate._thread = dead                                           # as if the thread raised and exited
    assert not gate.alive and gate.holds() and G.gate_holds(gate)


def _stretch(gate, clock, s: dict, hwnd: int, pid: int, *, exclusive: bool) -> None:
    """One full-screen stretch of ``pid`` in front, exclusive or not at its checks; back to Discord after."""
    s["d3d"] = False
    gate.on_foreground(5, 12)
    s["d3d"] = exclusive
    gate.on_foreground(hwnd, pid)
    for after in G.CHECK_AFTER_S:
        if gate.next_check_in() is None:
            break
        clock.t += after
        gate.on_check()
    s["d3d"] = False
    gate.on_foreground(5, 12)


def test_a_candidate_no_longer_exclusive_is_corrected_and_forgotten() -> None:
    """Review fix, on the capQ2b facts: one exclusive stretch makes a candidate; a later full-screen stretch that is
    not exclusive takes it back, and the program is forgotten from the stored evidence at zero."""
    store = _Store()
    gate, clock, log, s = _live_gate(procs={11: "Slideshow.exe", 12: "Discord.exe"}, fullscreen={1},
                                     gpu={11: 40.0})
    gate._store = store
    _stretch(gate, clock, s, 1, 11, exclusive=True)                # 1 stretch: a candidate, never a game yet
    assert not gate.holds() and not log.of("game_gate_learned")
    assert json.loads(store.rows[G.LEARNED_STATE])["slideshow"]["stretches"] == 1
    _stretch(gate, clock, s, 1, 11, exclusive=False)               # 1 -> 0: forgotten
    assert json.loads(store.rows[G.LEARNED_STATE]) == {} and gate.stats()["candidates"] == 0
    assert not gate.holds() and not log.of("game_gate_unlearned")


def test_a_learned_game_played_borderless_keeps_the_gate_held() -> None:
    """Second verification V1: a game learned from two exclusive stretches, later played borderless full screen (how
    most games run), stays learned and the gate holds the whole session, past every check."""
    store = _Store()
    gate, clock, log, s = _live_gate(procs={11: "Skyrunner.exe", 12: "Discord.exe"}, fullscreen={1},
                                     gpu={11: 40.0})
    gate._store = store
    _stretch(gate, clock, s, 1, 11, exclusive=True)
    _stretch(gate, clock, s, 1, 11, exclusive=True)
    assert log.of("game_gate_learned")[0]["game"] == "skyrunner"
    for _ in range(3):                                             # three borderless sessions
        s["d3d"] = False
        gate.on_foreground(1, 11)
        assert gate.holds()
        for after in G.CHECK_AFTER_S:
            if gate.next_check_in() is None:
                break
            clock.t += after
            gate.on_check()
            assert gate.holds()                                    # held at the 20 s and 90 s checks
        gate.on_foreground(5, 12)
        assert gate.game_running() and not gate.holds()            # still running behind Discord: input free
    assert not log.of("game_gate_unlearned")
    assert json.loads(store.rows[G.LEARNED_STATE])["skyrunner"]["stretches"] == 2


def test_evidence_is_capped_and_one_windowed_stretch_does_not_drop_a_well_known_game() -> None:
    gate, clock, log, s = _live_gate(procs={30: "Fieldrunner.exe", 12: "Discord.exe"}, fullscreen={4})
    for _ in range(6):
        _stretch(gate, clock, s, 4, 30, exclusive=True)
    assert G.stretches(gate._evidence["fieldrunner"]) == G.EVIDENCE_CAP
    _stretch(gate, clock, s, 4, 30, exclusive=False)               # borderless: a learned game keeps its count
    assert gate.game_running() and not log.of("game_gate_unlearned")


def test_listed_games_are_never_measured_and_old_entries_start_as_candidates() -> None:
    store = _Store()
    store.rows[G.LEARNED_STATE] = json.dumps({"fieldrunner": {"first_seen": 1.0, "gpu_share": 50.0}})
    gate, clock, _log, _ = _gate(procs=[(20, VALORANT), (30, "Fieldrunner.exe")], fullscreen={2}, store=store)
    gate.on_foreground(2, 20)
    assert gate.next_check_in() is None                           # on the fixed list: nothing to measure
    assert gate.decision().games == ("valorant-win64-shipping",)  # the old one-stretch entry is a candidate only


def test_a_game_without_a_wait_handle_is_dropped_at_the_next_window_switch() -> None:
    gate, clock, _log, images = _gate(procs=[(20, VALORANT), (12, "Discord.exe")])
    assert gate.on_foreground(5, 12).hold                         # tracked by the start scan, no thread: no handle
    del images[20]
    clock.t += 10                                                 # well before the next scan
    assert not gate.on_foreground(5, 12).hold


# -- read-only checks of the real PC ----------------------------------------------------------------------------------


def test_the_real_process_list_through_the_rule() -> None:
    """Read-only: whatever runs now, the anti-cheat tray and FL Studio are never games, and every game the gate sees
    is on its list."""
    procs = G._default_scan()
    assert procs and all(isinstance(pid, int) and name for pid, name in procs)
    games = GameSet.load()
    d = decide(GateView(running=frozenset(name for _pid, name in procs)), games)
    assert all(games.source(g) == "list" for g in d.games)
    names = {G.stem(n) for _p, n in procs}
    for never in ("vgtray", "vgc", "fl64", "fl", "riotclientservices"):
        assert never not in d.games
        if never in names:
            assert not games.is_game(never)


def test_the_thread_starts_listens_and_wakes_when_a_game_exits(tmp_path: Path) -> None:
    """One real start/stop: the foreground hook is passive, and a harmless child process stands in for a game
    (named one through the user's own list) so its exit wakes the thread."""
    mine = tmp_path / "privacy.toml"
    mine.write_text('[trace]\ngame_processes = ["Fieldrunner.exe"]\n', encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    log = _Log()
    try:
        gate = GameGate(privacy_path=mine, log=log, fullscreen_fn=lambda h: False, d3d_fn=lambda: False,
                        gpu_fn=lambda pids: None, scan_fn=lambda: [(child.pid, "Fieldrunner.exe")],
                        image_fn=lambda pid: "Fieldrunner.exe" if pid == child.pid else "Other.exe",
                        own_fn=lambda pid: False).start()
        assert gate.alive and gate.game_running()
        assert log.of("game_gate_start") and log.of("game_gate")[0]["games"] == ["fieldrunner"]
        child.kill()
        child.wait(10)
        deadline = time.time() + 5
        while gate.game_running() and time.time() < deadline:
            time.sleep(0.02)
        assert not gate.game_running() and log.of("game_gate")[-1]["trigger"] == "game_exit"
        gate.stop()
        assert not gate.alive and log.of("game_gate_stop")
        assert gate.holds() and gate.game_running()               # a stopped gate fails closed
    finally:
        if child.poll() is None:
            child.kill()
