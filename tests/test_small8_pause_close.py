"""small8 (report 73, release 7, item 2): a request that ends while paused for the user's takeover ends its pause.

Live: a mouse takeover paused a run while its model call was out; the call came back with done, the reply went out 11 s
later, and nothing closed the pause, so the next request was read as paused. Here: the agent's own record and the
surfaces' ``closed`` (runtime, island, voice), the window pipe's ``now`` naming the paused request from the runtime, and
the live helper reading that. Stub desk, stub brain, stub watch: no desktop, no input, nothing sent.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.log.events import ErrorEvent, Final, Paused

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live Now picture, and no real window lookups for the stub desk's invented window numbers."""
    import yuki.agent.tools as tools
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_is_own_window", lambda hwnd: False)


def _helpers() -> Any:
    import test_appTakeover as h

    return h


def _takeover_records(tmp: Path) -> list[dict[str, Any]]:
    out = []
    for path in (tmp / "sessions").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("type") == "takeover":
                out.append(rec)
    return out


def _paused_then_done(h: Any, watches: Any) -> Any:
    """The live order: one key goes out; the user grabs the mouse while the next model call is out; that call
    finishes with done."""
    def second(params: dict) -> Any:
        watches.fire("mouse")
        return h._response(h._done(1, "Pressed it; you're back at the top."))

    return h.Brain([h._response(h._press(0, "playpause")), second])


def test_a_done_while_paused_ends_the_pause_before_the_reply(tmp_path: Path) -> None:
    h = _helpers()
    watches = h.Watches()
    agent = h._agent(_paused_then_done(h, watches), tmp_path, h._desk(), watches=watches)
    told: list[tuple] = []
    order: list[str] = []
    agent.on_pause = lambda state, why, s: (told.append((state, why)), order.append(f"told:{state}"))
    events = []
    for ev in agent.run("press play"):
        events.append(ev)
        order.append(type(ev).__name__)
    assert isinstance(events[-1], Final)
    assert told == [("paused", "mouse"), ("closed", "mouse")]
    assert order.index("told:closed") < order.index("Final")      # the surfaces hear it before the reply
    assert not agent.paused and not agent._paused.is_set()
    states = [(r["state"], r.get("how")) for r in _takeover_records(tmp_path)]
    assert states == [("paused", None), ("closed", "finished")]
    assert all(w.stopped for w in watches.made)                    # nothing watches after the run
    assert not any(isinstance(e, Paused) and e.state == "paused" for e in events)   # never held: it had finished


def test_a_stop_while_paused_in_a_model_call_closes_the_pause_too(tmp_path: Path) -> None:
    from yuki.agent.loop import PAUSE_STOP_LINE

    h = _helpers()
    watches = h.Watches()
    holder: dict[str, Any] = {}

    def second(params: dict) -> Any:
        watches.fire("keys")
        holder["agent"].cancel()                                   # Stop pressed while the call is out
        return h._response(h._press(1, "nexttrack"))

    agent = h._agent(h.Brain([h._response(h._press(0, "playpause")), second]), tmp_path, h._desk(), watches=watches)
    holder["agent"] = agent
    told: list[tuple] = []
    agent.on_pause = lambda state, why, s: told.append((state, why))
    events = list(agent.run("play then next"))
    assert isinstance(events[-1], ErrorEvent) and events[-1].text == PAUSE_STOP_LINE   # still said plainly
    assert told[0] == ("paused", "keys") and told[-1] == ("closed", "keys")
    recs = _takeover_records(tmp_path)
    assert recs[-1]["state"] == "closed" and recs[-1]["how"] == "cancelled"
    assert not agent._paused.is_set()


def test_an_end_already_on_record_is_not_written_twice(tmp_path: Path) -> None:
    h = _helpers()
    watches = h.Watches()
    agent = h._agent(h.Brain([h._response(h._press(0, "playpause")), h._response(h._press(1, "nexttrack"))]),
                     tmp_path, h._desk(), watches=watches, limit=0.2)
    told: list[tuple] = []
    agent.on_pause = lambda state, why, s: told.append((state, why))

    events = []
    for ev in agent.run("play it"):
        events.append(ev)
        if getattr(ev, "name", None) == "press" and getattr(ev, "ok", False) and len(told) == 0:
            watches.fire("mouse")
    assert told == [("paused", "mouse"), ("ended", "mouse")]       # nobody resumed in time: told once
    assert [r["state"] for r in _takeover_records(tmp_path)] == ["paused", "ended"]
    assert not agent._paused.is_set()


def test_a_stop_while_held_paused_is_told_closed_once_and_logged_stopped_once(tmp_path: Path) -> None:
    h = _helpers()
    watches = h.Watches()
    agent = h._agent(h.Brain([h._response(h._press(0, "playpause")), h._response(h._press(1, "nexttrack"))]),
                     tmp_path, h._desk(), watches=watches)
    told: list[tuple] = []
    agent.on_pause = lambda state, why, s: told.append((state, why))
    for ev in agent.run("play it"):
        if getattr(ev, "name", None) == "press" and getattr(ev, "ok", False) and not told:
            watches.fire("mouse")
        if isinstance(ev, Paused) and ev.state == "paused":
            threading.Timer(0.05, agent.cancel).start()
    assert told == [("paused", "mouse"), ("closed", "mouse")]
    assert [r["state"] for r in _takeover_records(tmp_path)] == ["paused", "stopped"]


def test_closing_when_not_paused_does_nothing(tmp_path: Path) -> None:
    h = _helpers()
    agent = h._agent(h.Brain([]), tmp_path, h._desk(), watches=h.Watches())
    told: list[tuple] = []
    agent.on_pause = lambda *a: told.append(a)
    agent._close_pause("finished")
    assert told == [] and _takeover_records(tmp_path) == []


# -- the runtime, the island's app, the voice ------------------------------------------------------------------------


def test_through_the_runtime_the_paused_job_is_closed_and_the_next_run_is_not_paused(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from yuki.config import Settings
    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    QApplication.instance() or QApplication([])
    h = _helpers()
    watches = h.Watches()
    agent = h._agent(_paused_then_done(h, watches), tmp_path, h._desk(), watches=watches)
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=h._agent(h.Brain([]), tmp_path / "fd", h._desk()),
                      memory=SimpleNamespace(session_id="t"))
    states: list[tuple] = []
    finals: list[str] = []
    rt.paused_changed.connect(lambda lane, job, state, why: states.append((job, state, why)))
    rt.finished.connect(lambda lane, job, text: finals.append(text))
    rt.worker.enqueue(Job(7, "press play", None, "text"))
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert states == [(7, "paused", "mouse"), (7, "closed", "mouse")] and finals
    assert rt.paused_id() is None and rt.worker.paused_job() is None


def test_the_voice_takes_closed_as_no_longer_paused_and_still_tells_the_end() -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    h = _helpers()
    bridge, job = h._bridge_job()
    bridge._on_paused_changed("worker", 9, "paused", "mouse")
    assert job.paused
    bridge._on_paused_changed("worker", 9, "closed", "mouse")
    assert not job.paused and not job.paused_out and not job.superseded   # its reply is told as it really ended
    assert "paused" not in (bridge.work_line() or "")


def test_the_app_unpauses_the_island_on_closed() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    h = _helpers()
    ui = h._bare_ui(app)
    try:
        ui.island.strip.start_run(5, line="Pressing play", request="press play")
        ui.island.strip.run_step(5, "Pressing play", 1)
        ui._on_paused_changed("worker", 5, "paused", "mouse")
        assert ui.island.state == "paused"
        ui._on_paused_changed("worker", 5, "closed", "mouse")
        assert ui.island.state != "paused"
        assert ui.events[-1] == ("takeover", {"id": 5, "state": "closed", "why": "mouse"})
    finally:
        ui.island.stop()


# -- the window pipe's now, and the live helper -----------------------------------------------------------------------


def test_now_names_the_paused_request_from_the_runtime() -> None:
    from yuki.ui.window_host import WindowHost

    def host(paused: Any) -> Any:
        rt = SimpleNamespace(running_ids=lambda: {"worker": 7}, worker_busy=True, paused_id=lambda: paused)
        return SimpleNamespace(model=SimpleNamespace(items=[]), ui=SimpleNamespace(runtime=rt),
                               _on_gui=lambda fn: fn())

    assert WindowHost._op_now(host(7), {})["now"]["paused"] == 7
    assert WindowHost._op_now(host(None), {})["now"]["paused"] is None


def _tray() -> Any:
    spec = importlib.util.spec_from_file_location("small8_tray", ROOT / "scripts" / "live" / "tray.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_the_live_helper_reads_paused_from_now_and_falls_back_to_the_log_only_for_an_older_tray(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tray = _tray()
    now = {"runs": [], "lanes": {"worker": 7}, "worker_busy": True, "paused": None}
    client = SimpleNamespace(request=lambda op, **k: {"now": now}, close=lambda: None)
    assert tray.brief_now(client)["paused"] is None
    older = SimpleNamespace(request=lambda op, **k: {"now": {k2: v for k2, v in now.items() if k2 != "paused"}},
                            close=lambda: None)
    assert "paused" not in tray.brief_now(older)

    # wait: a busy worker with no paused request is not paused, whatever an earlier job left in the log
    monkeypatch.setattr(tray, "paused_now", lambda: True)
    seq = iter([{**now, "worker_busy": True}, {**now, "worker_busy": False, "lanes": {}}])
    monkeypatch.setattr(tray, "client", lambda: SimpleNamespace(request=lambda op, **k: {"now": next(seq)},
                                                                close=lambda: None))
    monkeypatch.setattr(tray.time, "sleep", lambda s: None)
    monkeypatch.setattr(tray.sys, "argv", ["tray.py", "wait", "--timeout", "30"])
    assert tray.main() == 0
    seq = iter([{**now, "paused": 7}])
    assert tray.main() == 4

    # the log fallback: a closed pause is not a pause
    logs = tmp_path / "Yuki" / "logs" / "sessions"
    logs.mkdir(parents=True)
    (logs / "20261005-120000-worker.jsonl").write_text(
        json.dumps({"type": "takeover", "state": "paused"}) + "\n"
        + json.dumps({"type": "takeover", "state": "closed", "how": "finished"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.undo()   # only the env var matters below: undo the stubs, keep it simple
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert tray.paused_now() is False
