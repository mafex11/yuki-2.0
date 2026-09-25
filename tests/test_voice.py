"""Offline, stub-driven checks for the voice layer and the brain changes it needs.

No microphone, no speakers, no network: the brain gets a fake model client and
the stub desktop, the bridge gets a fake runtime with the real signal names.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from yuki.agent.loop import Agent
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import Final, Say, SessionLogger
from yuki.voice.bridge import VoiceBridge
from yuki.voice.mouth import SPOKEN_NOTE
from yuki.voice.offline import StubDesktop

APP = QCoreApplication.instance() or QCoreApplication([])


# ---------------------------------------------------------------------------
# Brain: say, steer, cancel checkpoint
# ---------------------------------------------------------------------------


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _response(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class FakeClient:
    """``messages.create`` returning scripted responses; a hook runs before each one."""

    def __init__(self, responses: list[SimpleNamespace], before: Any = None) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.before = before
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        if self.before:
            self.before(len(self.calls))
        return self.responses.pop(0)


def _agent(client: FakeClient, tmp: Path) -> Agent:
    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    return Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                 backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)


def test_say_yields_event_and_run_continues(tmp_path: Path) -> None:
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="say", input={"text": "Found three invoices."}),
                  _block("tool_use", id="t2", name="run_powershell", input={"command": "ls"})),
        _response(_block("tool_use", id="t3", name="done", input={"message": "Total is 48,200."})),
    ])
    events = list(_agent(client, tmp_path).run("add up my invoices"))
    says = [e for e in events if isinstance(e, Say)]
    assert [s.text for s in says] == ["Found three invoices."]
    assert isinstance(events[-1], Final) and events[-1].text == "Total is 48,200."


def test_steer_holds_done_and_reaches_the_model(tmp_path: Path) -> None:
    agent_box: dict[str, Agent] = {}

    def before(n: int) -> None:
        if n == 1:  # the user adds something while the first model call runs
            agent_box["a"].steer("use YouTube, not Spotify")

    client = FakeClient([
        _response(_block("tool_use", id="t1", name="done", input={"message": "Playing on Spotify."})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Playing on YouTube."})),
    ], before=before)
    agent = _agent(client, tmp_path)
    agent_box["a"] = agent
    events = list(agent.run("play my lo-fi playlist"))
    assert isinstance(events[-1], Final) and events[-1].text == "Playing on YouTube."
    second = client.calls[1]["messages"][-1]["content"]
    texts = " ".join(b.get("text", "") for b in second if isinstance(b, dict))
    assert "use YouTube, not Spotify" in texts and "not sent" in texts


def test_cancel_during_text_reply_is_not_final(tmp_path: Path) -> None:
    agent_box: dict[str, Agent] = {}
    client = FakeClient([_response(_block("text", text="All done!"), stop="end_turn")],
                        before=lambda n: agent_box["a"].cancel())
    agent = _agent(client, tmp_path)
    agent_box["a"] = agent
    events = list(agent.run("do the thing"))
    assert not any(isinstance(e, Final) for e in events)
    assert "Cancelled" in events[-1].text


# ---------------------------------------------------------------------------
# Bridge: routing on state, never on words
# ---------------------------------------------------------------------------


class FakeRuntime(QObject):
    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self, lane: str = "worker") -> None:
        super().__init__()
        self.lane, self.next_id = lane, 1
        self.submitted: list[tuple[int, str, str]] = []
        self.steers: list[tuple[int, str]] = []
        self.answers: list[tuple[str, str]] = []
        self.cancels: list[int] = []
        self.running: set[int] = set()

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text") -> tuple[str, int]:
        job_id, self.next_id = self.next_id, self.next_id + 1
        self.submitted.append((job_id, text, source))
        return self.lane, job_id

    def steer(self, job_id: int, text: str) -> bool:
        self.steers.append((job_id, text))
        return job_id in self.running

    def answer(self, lane: str, text: str) -> None:
        self.answers.append((lane, text))

    def cancel(self, job_id: int) -> str:
        self.cancels.append(job_id)
        return "running"


class Params:
    def __init__(self, message: str, intent: str = "new_task", heard: str = "hey yuki do it") -> None:
        self.arguments = {"message": message, "intent": intent}
        self.tool_call_id = "call-1"
        self.context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": heard}])
        self.results: list[tuple[dict, bool]] = []

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties.is_final if properties is not None else True))


def _pump() -> None:
    APP.processEvents()


async def _settle() -> None:
    for _ in range(5):
        _pump()
        await asyncio.sleep(0)


def test_new_task_relays_say_then_final() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        params = Params("Add up the September invoices in Downloads")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        assert rt.submitted and rt.submitted[0][2] == "voice"
        assert '"hey yuki do it"' in rt.submitted[0][1]
        job = rt.submitted[0][0]
        assert bridge.open_voice_jobs() == 1
        rt.started.emit("worker", job, "x")
        rt.said.emit("worker", job, "Found three invoices")
        rt.finished.emit("worker", job, "Total is 48,200")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        assert params.results == [({"yuki_says": "Found three invoices"}, False), ({"result": "Total is 48,200", "how_to_say_it": SPOKEN_NOTE}, True)]
        assert bridge.open_voice_jobs() == 0

    asyncio.run(main())


def test_front_desk_handoff_is_not_reported_as_done() -> None:
    async def main() -> None:
        rt = FakeRuntime(lane="front_desk")
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        params = Params("Open Spotify")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.finished.emit("front_desk", job, "I'd open Spotify; queued.")
        rt.lane_done.emit("front_desk", job, "x", True, ["launch_app"])
        rt.handed_off.emit(job, 9, 1)
        rt.started.emit("worker", 9, "x")
        rt.finished.emit("worker", 9, "Spotify is open.")
        rt.lane_done.emit("worker", 9, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        kinds = [list(r)[0] for r, _ in params.results]
        assert kinds == ["status", "result"]
        assert params.results[-1][0]["result"] == "Spotify is open."

    asyncio.run(main())


def test_follow_up_routes_by_state() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        first = Params("Play my lo-fi playlist")
        task = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        job = rt.submitted[0][0]
        # queued: held, delivered when it starts
        await bridge.tell_yuki(Params("Use YouTube", intent="follow_up"))
        assert rt.steers == []
        rt.running.add(job)
        rt.started.emit("worker", job, "x")
        await _settle()
        assert rt.steers and rt.steers[0][0] == job
        # waiting for an answer: answered, not steered
        rt.asked.emit("worker", job, "Beats or Study?")
        await _settle()
        await bridge.tell_yuki(Params("Study", intent="follow_up"))
        await _settle()
        assert rt.answers and rt.answers[0][0] == "worker"
        assert ({"yuki_asks": "Beats or Study?"}, False) in first.results
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()
        assert rt.cancels == [job]  # the mouth cancelled: the brain's job is cancelled too

    asyncio.run(main())


def test_conversation_ending_does_not_cancel_brain_work() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        task = asyncio.create_task(bridge.tell_yuki(Params("Long task")))
        await _settle()
        bridge.closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()
        assert rt.cancels == []
        bridge.attach(None)
        assert bridge.open_voice_jobs() == 0  # its result will land as a card, not hold a conversation

    asyncio.run(main())


def test_status_lists_typed_and_voice_jobs() -> None:
    rt = FakeRuntime()
    bridge = VoiceBridge(rt, foreground=lambda: None)
    rt.started.emit("worker", 42, "rename the screenshots")
    rt.tool_called.emit("worker", 42, "run_powershell", {"command": "x"})
    snap = bridge.snapshot()
    assert snap[0]["by"] == "typed" and snap[0]["state"] == "running" and snap[0]["current_step"]


# ---------------------------------------------------------------------------
# Keys, hotkeys, log
# ---------------------------------------------------------------------------


def test_key_lookup_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import keys

    monkeypatch.setenv("YUKI_TEST_KEY", "abc")
    assert keys.get_key("YUKI_TEST_KEY") == "abc" and keys.key_source("YUKI_TEST_KEY") == "env"
    monkeypatch.delenv("YUKI_TEST_KEY")
    assert keys.key_source("YUKI_TEST_KEY") == "missing"


def test_voice_hotkey_does_not_clash() -> None:
    from yuki.ui.hotkey import hotkey_bindings

    b = hotkey_bindings(Settings(), env={})
    assert b["voice"] and len({b["toggle"], b["cancel"], b["voice"]}) == 3


def test_voice_log_is_content_free_csv() -> None:
    from yuki.voice.log import TURN_COLUMNS, VoiceLog

    with tempfile.TemporaryDirectory() as tmp:
        log = VoiceLog(Path(tmp) / "voice")
        log.turn({"session": "s", "turn": 1, "mouth_ttft_ms": 800.0, "language": "hi"})
        header = (Path(tmp) / "voice_turns.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
        assert tuple(header) == TURN_COLUMNS
        assert not any(col in header for col in ("transcript", "text", "reply"))


def test_voice_off_imports_nothing_heavy() -> None:
    import subprocess
    import sys

    code = ("import sys, yuki.voice.session, yuki.voice.indicator; "
            "print(any(m in sys.modules for m in ('pipecat', 'sounddevice', 'sherpa_onnx')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False"
