"""fGate (PLAN.md Phase F item 3, F4): the game gate narrowed to "a game IN FRONT" for passive input capture and passive
UI Automation reads, and for cards (plus a 120 s return hold); background CPU jobs still wait for any game running;
``[trace] game_gate = "running"`` brings the old rule back in one edit.

Offline: invented process data, a fake clock, temp databases and temp privacy files; the hook thread, the sink's
Windows reads and UI Automation are fakes. Nothing reads the live desktop.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yuki.memory import gamegate as G
from yuki.memory.gamegate import Decision, GameGate, GameSet, GateView, decide

#: These tests stub the game facts themselves: a test-run stub of the live game check (review r2 L8)
#: stays off for them.
LIVE_GAMEGATE = True

VALORANT = "VALORANT-Win64-Shipping.exe"


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


def _gate(privacy_path: Path | None = None, **state: Any):
    clock, log = _Clock(), _Log()
    s = {"procs": {}, "fullscreen": set(), "d3d": False, "session": False, **state}
    gate = GameGate(privacy_path=privacy_path, log=log, game_session_fn=lambda: s["session"],
                    fullscreen_fn=lambda hwnd: hwnd in s["fullscreen"], d3d_fn=lambda: s["d3d"],
                    gpu_fn=lambda pids: None, scan_fn=lambda: list(s["procs"].items()),
                    image_fn=lambda pid: s["procs"].get(pid, ""), own_fn=lambda pid: False, clock=clock,
                    windows_fn=lambda: frozenset(), exe_fn=lambda pid: "")
    return gate, clock, log, s


# -- the rule ---------------------------------------------------------------------------------------------------------


def test_decide_splits_input_from_running() -> None:
    games = GameSet.load()
    front = decide(GateView(front=VALORANT, running=frozenset({VALORANT})), games)
    assert front.hold and front.hold_input and front.scope == "front" and front.front == "valorant-win64-shipping"
    behind = decide(GateView(front="Discord.exe", running=frozenset({VALORANT})), games)
    assert behind.hold and not behind.hold_input and behind.scope == "none"         # trace and cards may run
    voice = decide(GateView(front="Discord.exe", game_session=True), games)
    assert voice.hold and not voice.hold_input                                       # a voice session alone: no
    d3d = decide(GateView(front="Unknown.exe", d3d_fullscreen=True), games)
    assert d3d.hold_input and d3d.scope == "d3d"
    learning = decide(GateView(front="Unknown.exe", front_fullscreen=True, learning=True), games)
    assert learning.hold_input and learning.scope == "learning" and not learning.hold
    old = decide(GateView(front="Discord.exe", running=frozenset({VALORANT})), games, scope="running")
    assert old.hold_input and old.scope == "running"                                 # the one-edit way back
    assert decide(GateView(front="Discord.exe"), games, scope="running").hold_input is False


def test_the_game_gate_switch_is_stricter_only(tmp_path: Path) -> None:
    assert G.gate_scope(None) == "front"                                             # the packaged default
    mine = tmp_path / "privacy.toml"
    mine.write_text('[trace]\ngame_gate = "Running "\n', encoding="utf-8")
    assert G.gate_scope(mine) == "running"
    for other in ("front", "off", "none", "", "1"):
        mine.write_text(f'[trace]\ngame_gate = "{other}"\n', encoding="utf-8")
        assert G.gate_scope(mine) == "front"                                         # never wider than "front"
    mine.write_text("[trace]\ngame_gate = 3\n", encoding="utf-8")
    assert G.gate_scope(mine) == "front"


def test_the_packaged_privacy_file_ships_front_and_the_return_hold() -> None:
    import tomllib

    from yuki.memory.privacy import DEFAULT_PATH

    data = tomllib.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    assert data["trace"]["game_gate"] == "front"
    assert data["nudges"]["return_hold_s"] == 120
    from yuki.memory.nudges import NudgeConfig

    assert NudgeConfig.from_dict(data["nudges"]).return_hold_s == 120.0


# -- the live gate --------------------------------------------------------------------------------------------------


def test_a_game_behind_discord_frees_input_but_not_cpu_jobs() -> None:
    gate, clock, log, s = _gate(procs={20: VALORANT, 12: "Discord.exe"})
    gate.on_foreground(2, 20)
    assert gate.holds() and gate.game_running() and gate.front_game() == "valorant-win64-shipping"
    gate.on_foreground(5, 12)                                                        # Alt+Tab out
    assert not gate.holds() and gate.game_running() and gate.front_game() == ""
    assert G.gate_holds(gate) is False and G.running_holds(gate) is True
    assert log.of("game_gate")[-1]["hold_input"] is False and log.of("game_gate")[-1]["scope"] == "none"
    s["d3d"] = True
    clock.t += G.D3D_FRESH_S
    assert gate.holds() and gate.front_game() == "d3d"                              # exclusive full screen, live
    assert G.running_holds(None) is True and G.gate_holds(None) is True              # no gate: both hold


def test_running_scope_from_the_privacy_file_brings_back_the_old_rule(tmp_path: Path) -> None:
    mine = tmp_path / "privacy.toml"
    mine.write_text('[trace]\ngame_gate = "running"\n', encoding="utf-8")
    gate, *_ = _gate(privacy_path=mine, procs={20: VALORANT, 12: "Discord.exe"})
    gate.on_foreground(5, 12)
    assert gate.holds() and gate.decision().scope == "running" and gate.scope == "running"
    gate2, _c, _l, s2 = _gate(privacy_path=mine, procs={12: "Discord.exe"}, session=True)
    gate2.on_foreground(5, 12)
    assert gate2.holds()                                                             # the voice session, as before


def test_on_change_fires_once_per_change_on_the_thread_that_saw_it() -> None:
    gate, *_ = _gate(procs={20: VALORANT, 12: "Discord.exe"})
    seen: list[Decision] = []
    gate.on_change(seen.append)
    gate.on_foreground(5, 12)                                                        # a game runs behind: one change
    gate.on_foreground(5, 12)                                                        # same decision: nothing
    gate.on_foreground(2, 20)                                                        # to the front
    gate.on_foreground(2, 20)
    gate.on_foreground(5, 12)
    assert [d.hold_input for d in seen] == [False, True, False]

    def boom(decision: Decision) -> None:
        raise RuntimeError("a broken subscriber")

    gate.on_change(boom)
    gate.on_foreground(2, 20)                                                        # never stops the gate
    assert gate.holds() and seen[-1].hold_input


def test_an_unknown_full_screen_program_holds_input_until_judged_once_per_run() -> None:
    gate, clock, log, s = _gate(procs={30: "Slides.exe", 12: "Discord.exe"}, fullscreen={4})
    gate.on_foreground(4, 30)
    assert gate.holds() and gate.decision().scope == "learning" and not gate.game_running()
    for after in G.CHECK_AFTER_S:
        clock.t += after
        gate.on_check()
    assert not gate.holds() and log.of("game_gate_not_game")                        # judged: not a game
    gate.on_foreground(5, 12)
    gate.on_foreground(4, 30)                                                        # again in this run: not held
    assert not gate.holds()


# -- the trace obeys it at once ---------------------------------------------------------------------------------------


class _FakeGate:
    def __init__(self) -> None:
        self.hold = False
        self.callbacks: list = []

    def holds(self) -> bool:
        return self.hold

    def on_change(self, callback) -> None:
        self.callbacks.append(callback)

    def games(self) -> GameSet:
        return GameSet.load()


@pytest.fixture
def trace_env(tmp_path: Path, monkeypatch):
    import yuki.memory.trace as trace_mod
    import yuki.memory.trace_hooks as hooks_mod
    import yuki.memory.trace_input as input_mod
    import yuki.memory.trace_uia as uia_mod
    from tests.test_trace_controller import FakeHooks
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.store import Store

    monkeypatch.setattr(hooks_mod, "TraceHooks", FakeHooks)
    real_metrics = input_mod.Metrics
    monkeypatch.setattr(input_mod, "Metrics", lambda: real_metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5,
                                                                   double_w=4, double_h=4, drag_w=4, drag_h=4,
                                                                   read=False))
    monkeypatch.setattr(input_mod, "_physical_cursor", lambda: (lambda: (0, 0)))

    class NoUia:
        def __init__(self) -> None:
            raise OSError("no UI Automation in this test")

    monkeypatch.setattr(uia_mod, "TraceUia", NoUia)
    store = Store.open(tmp_path / "memory.db")
    privacy = PrivacyConfig(tmp_path / "privacy.toml")
    text = Path(privacy.path).read_text(encoding="utf-8").replace("record_actions = false", "record_actions = true")
    Path(privacy.path).write_text(text, encoding="utf-8")
    gate = _FakeGate()
    trace = trace_mod.Trace(store, privacy=privacy, gate=gate, attach=lambda sink: True, log=lambda t, **f: None)
    yield trace, gate
    trace.stop()
    store.close()


def test_the_sink_flips_inside_on_change_without_a_beat(trace_env) -> None:
    trace, gate = trace_env
    assert len(gate.callbacks) == 1                                                  # subscribed at construction
    trace.beat()
    assert trace.sink is not None and trace.sink.blocked is False and trace.state == "on"
    gate.hold = True
    gate.callbacks[0](Decision(hold=True, hold_input=True, scope="front"))           # the gate's own thread
    assert trace.sink.blocked is True and trace.hooks.blocked_calls[-1] is True     # no beat in between
    assert trace.gate_blocks == 1 and not trace.open_at(__import__("time").time() + 0.001)
    trace.beat()
    assert trace.state == "game"
    gate.hold = False
    gate.callbacks[0](Decision(hold=True, hold_input=False, scope="none"))          # a game behind: nothing here
    assert trace.sink.blocked is True                                                # opening is the beat's
    trace.beat()
    assert trace.sink.blocked is False and trace.state == "on"


def test_a_beat_reopening_while_the_gate_begins_to_hold_blocks_again(trace_env) -> None:
    """Review r2 L6: the gate starts holding while a beat re-opens (its _gate_changed sees the sink still blocked and
    does nothing); the beat sees it right after re-opening and blocks again, not one beat later."""
    trace, gate = trace_env
    trace.beat()
    gate.hold = True
    gate.callbacks[0](Decision(hold=True, hold_input=True, scope="front"))
    trace.beat()
    assert trace.state == "game"
    gate.hold = False

    def attach(sink) -> bool:
        gate.hold = True                                                             # back in front mid-reopen
        gate.callbacks[0](Decision(hold=True, hold_input=True, scope="front"))
        return True

    trace._attach = attach
    trace.beat()
    assert trace.sink.blocked is True and trace.hooks.blocked_calls[-1] is True and trace.state == "game"


def test_a_lookup_aimed_at_a_game_root_pid_is_refused(tmp_path: Path) -> None:
    from tests.test_trace_worker import APP, HWND, Rig

    rig = Rig(tmp_path)
    try:
        rig.w._game_pid = lambda pid: pid == APP                                     # the app under the click: a game
        rig.w._root_of = lambda hwnd: hwnd
        rig.w._pid_of = lambda hwnd: next((p for p, h in HWND.items() if h == hwnd), 0)
        rig.click()
        rig.settle()
        assert rig.w.counters["lookup_refused_game"] >= 1
        assert not [c for c in rig.uia.calls if c[0] in ("point", "descend", "event")]   # nothing asked of it
        # its top-level window's process alone (the pid of the point unknown) is enough
        assert rig.w._game_target((HWND[APP], 0)) is True
        rig.w._game_pid = lambda pid: (_ for _ in ()).throw(OSError("unreadable"))
        assert rig.w._game_target((HWND[APP], APP)) is True                         # fails closed
    finally:
        rig.store.close()


def test_a_hook_into_a_game_process_is_refused() -> None:
    from yuki.memory.trace_hooks import TraceHooks

    hooks = TraceHooks(lambda item: None, pid_of=lambda hwnd: (hwnd * 10,), own_pid=1,
                       game_pid=lambda pid: pid == 550)
    hooks.blocked = False
    calls: list = []
    hooks._hook_set = lambda ranges: calls.append(hooks._pids) or [7]
    hooks._unhook = lambda handles: handles.clear()
    hooks._follow(55)                                                                # the game's window in front
    assert calls == [] and hooks._pids == () and hooks.counts["hooks_refused_game"] == 1
    hooks._follow(12)                                                                # another app: hooked as before
    assert calls == [(120,)]


# -- cards: a game in front, plus the 120 s return hold -------------------------------------------------------------


NOW = datetime(2026, 10, 8, 8, 30).timestamp()


@pytest.fixture
def store(tmp_path: Path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _present(store, start: float, end: float, process: str, app: str, *, step: float = 30.0, **kw) -> None:
    from yuki.memory.store import TimelineRow

    t = start
    while t < end:
        e = min(end, t + step)
        store.save_timeline(TimelineRow(None, t, e, "present", process, app, active_s=e - t, **kw))
        t = e


def test_cards_hold_only_with_a_game_in_front_and_come_back_after_120_s(store) -> None:
    from yuki.memory.nudges import NudgeConfig, observe

    cfg = NudgeConfig()
    _present(store, NOW - 3 * 3600, NOW - 1800, "warp", "Warp")
    _present(store, NOW - 1800, NOW, "valorant-win64-shipping", "VALORANT")         # windowed, in front
    assert observe(store, NOW, cfg, {"game_front": True}).gate == "game"
    assert observe(store, NOW, cfg).gate == "game"                    # replay: the open stretch's program
    assert observe(store, NOW, cfg, {"game_front": False, "fullscreen": False}).gate == "fullscreen"  # just left it


def test_the_8_oct_shape_frees_the_cards_two_minutes_after_the_game_left_the_front(store) -> None:
    """8 Oct (memory log): VALORANT last in front 08:41:52, its process closed 08:56:56, its voice row ended 09:00:42;
    the cards were held until 09:00:46. Now: free from 08:43:52 (critique M9: derived from the stretch's program)."""
    from yuki.memory.nudges import NudgeConfig, observe

    day = datetime(2026, 10, 8)
    at = lambda h, m, s=0: day.replace(hour=h, minute=m, second=s).timestamp()      # noqa: E731
    cfg = NudgeConfig()
    _present(store, at(7, 30), at(8, 5), "warp", "Warp")
    _present(store, at(8, 5), at(8, 41, 52), "valorant-win64-shipping", "VALORANT", fullscreen=True)
    _present(store, at(8, 41, 52), at(9, 5), "zen", "Zen")
    row = store.start_call(app="VALORANT", process="valorant-win64-shipping", via="game", at=at(8, 10),
                           status="declined", kind="game")
    store.update_call(row, last_alive_at=at(9, 0, 30))                               # the voice row lives on
    gates = {}
    t = at(8, 40)
    while t <= at(9, 2):
        gates[t] = observe(store, t, cfg).gate
        t += 15
    assert all(g in ("game", "fullscreen") for t, g in gates.items() if at(8, 40) <= t < at(8, 43, 52))
    # review r2 L1: the B.1 bar's literal wording, "card gate None": nothing else holds the cards in this shape (the
    # user was present through the game). The real 8 Oct replay reads ``settling`` 08:44:00-08:49:45 after the hold
    # because that morning's VALORANT stretch was all "away" (the welcome-back rule, not the game gate).
    free = min(t for t, g in gates.items() if t > at(8, 41, 52) and g is None)
    assert at(8, 43, 52) <= free <= at(8, 43, 52) + 15
    assert all(g is None for t, g in gates.items() if t >= free)


def test_return_hold_is_a_setting(store) -> None:
    from yuki.memory.nudges import NudgeConfig, observe

    _present(store, NOW - 3600, NOW - 200, "valorant", "VALORANT", fullscreen=True)
    _present(store, NOW - 200, NOW, "discord", "Discord")
    assert observe(store, NOW, NudgeConfig()).gate not in ("game", "fullscreen")     # 200 s > 120 s
    assert observe(store, NOW, NudgeConfig.from_dict({"return_hold_s": 300})).gate == "fullscreen"
