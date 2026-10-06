"""capC1 (report 96): the trace in the service: nothing runs until the user says yes; consent, the tray pause and the
hard game gate (a gate that failed to start holds) block it at the first step; the A/B hours; Warp hand-off.

The hook thread, the sink's Windows reads and UI Automation are replaced by fakes (nothing registers a hook here);
the privacy file is a temp copy.
"""

from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

import yuki.memory.trace as trace_mod
import yuki.memory.trace_hooks as hooks_mod
import yuki.memory.trace_input as input_mod
from yuki.memory.privacy import PrivacyConfig
from yuki.memory.store import Store


class FakeHooks:
    instances: list = []

    def __init__(self, put, **kw) -> None:
        self.put = put
        self.blocked_calls: list[bool] = []
        self.hwnd = 1
        self.error = ""
        self.counts = {"low": 3, "outcome": 5, "outcome_over": 0}
        self.alive = True
        FakeHooks.instances.append(self)

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        self.alive = False

    def action(self, t) -> None:
        pass

    def set_blocked(self, blocked: bool) -> None:
        self.blocked_calls.append(blocked)


class FakeGate:
    def __init__(self) -> None:
        self.hold = False

    def holds(self) -> bool:
        return self.hold


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hooks_mod, "TraceHooks", FakeHooks)
    real_metrics = input_mod.Metrics
    monkeypatch.setattr(input_mod, "Metrics", lambda: real_metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5,
                                                                   double_w=4, double_h=4, drag_w=4, drag_h=4,
                                                                   read=False))
    monkeypatch.setattr(input_mod, "_physical_cursor", lambda: (lambda: (0, 0)))

    class NoUia:
        def __init__(self) -> None:
            raise OSError("no UI Automation in this test")

    import yuki.memory.trace_uia as uia_mod
    monkeypatch.setattr(uia_mod, "TraceUia", NoUia)
    store = Store.open(tmp_path / "memory.db")
    privacy = PrivacyConfig(tmp_path / "privacy.toml")
    attached: list = []
    gate = FakeGate()
    trace = trace_mod.Trace(store, privacy=privacy, gate=gate, attach=lambda sink: attached.append(sink) or True,
                            log=lambda t, **f: None)
    yield trace, privacy, gate, attached
    trace.stop()
    store.close()


def _consent(privacy: PrivacyConfig, on: bool) -> None:
    path = Path(privacy.path)
    text = path.read_text(encoding="utf-8")
    text = text.replace("record_actions = true", "record_actions = false")
    if on:
        text = text.replace("record_actions = false", "record_actions = true")
    path.write_text(text, encoding="utf-8")
    time.sleep(0.02)


def test_nothing_runs_until_the_user_says_yes(env):
    trace, privacy, gate, attached = env
    trace.beat()
    assert trace.sink is None and trace.state == "not_started" and trace.hooks is None   # no thread, no hook
    assert trace.rules().record_actions is False                      # the packaged default


def test_consent_starts_it_and_the_pause_and_the_game_gate_block_it(env):
    FakeHooks.instances.clear()
    trace, privacy, gate, attached = env
    _consent(privacy, True)
    trace.beat()
    assert trace.sink is not None and trace.state == "on" and attached == [trace.sink]
    assert trace.sink.blocked is False
    trace.set_paused(True)
    assert trace.sink.blocked is True and trace.state == "paused"
    trace.set_paused(False)
    assert trace.state == "on" and len(attached) == 2               # attached again on opening (ends a mouse rest)
    gate.hold = True
    trace.beat()
    assert trace.sink.blocked is True and trace.state == "game"
    assert FakeHooks.instances[-1].blocked_calls[-1] is True       # the per-app hooks come off in a game
    gate.hold = False
    trace.beat()
    _consent(privacy, False)
    trace.beat()
    assert trace.sink.blocked is True and trace.state == "off"


def test_a_gate_that_failed_to_start_holds(env):
    trace, privacy, gate, attached = env
    trace._game_gate = None
    _consent(privacy, True)
    trace.beat()
    assert trace.state == "game" and trace.sink.blocked is True


def test_warp_rows_reach_the_trace_only_while_it_records(env):
    trace, privacy, gate, attached = env
    trace.warp_commands([{"command": "ls", "at": 1.0}])
    assert trace.queue.empty()
    _consent(privacy, True)
    trace.beat()
    trace.stop()                                                     # hold the worker: look at the queue itself
    trace.queue = queue.SimpleQueue()
    trace.hooks.alive = True
    trace.warp_commands([{"command": "ls", "at": 1.0}])
    assert trace.queue.get_nowait()[0] == "cmd"


def test_ab_hours_alternate_recording(env, monkeypatch):
    trace, privacy, gate, attached = env
    path = Path(privacy.path)
    path.write_text(path.read_text(encoding="utf-8").replace("ab_hours = false", "ab_hours = true")
                    .replace("record_actions = false", "record_actions = true"), encoding="utf-8")
    time.sleep(0.02)
    monkeypatch.setattr(trace_mod._AbSampler, "on_now", staticmethod(lambda: False))
    trace.beat()
    assert trace.state == "ab_off" and trace.sink.blocked is True
    monkeypatch.setattr(trace_mod._AbSampler, "on_now", staticmethod(lambda: True))
    trace.beat()
    assert trace.state == "on"


def test_health_is_saved_content_free_for_the_doctor(env):
    trace, privacy, gate, attached = env
    _consent(privacy, True)
    trace.beat()
    trace.save_health()
    import json

    health = json.loads(trace.store.nudge_state("trace:health"))
    assert health["state"] == "on" and "low_events_per_s" in health and "outcome_events_per_s" in health
    assert all(isinstance(v, (int, float, str, type(None))) for v in health.values())
