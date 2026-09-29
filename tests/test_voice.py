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
from yuki.voice.offline import StubDesktop, StubResult

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
    def __init__(self, message: str, intent: str = "new_task", heard: str = "hey yuki do it",
                 context: str | None = None) -> None:
        self.arguments = {"request": message, "intent": intent, **({"context": context} if context else {})}
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


async def _pumping() -> None:
    """Keep the GUI thread's queue moving while a handler awaits a GUI decision (a follow-up)."""
    while True:
        _pump()
        await asyncio.sleep(0.001)


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
        assert params.results == [({"yuki_says": "Found three invoices"}, False),
                                  ({"result": "Total is 48,200", "for_request": "Add up the September invoices in Downloads",
                                    "more_available": False, "how_to_say_it": SPOKEN_NOTE}, True)]
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
        pump = asyncio.create_task(_pumping())
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
        pump.cancel()
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


# ---------------------------------------------------------------------------
# v3: condensation, the narrator gate, progress from real steps, follow-up
# restarts, check_progress, more_of_result, grounding
# ---------------------------------------------------------------------------


def test_condense_keeps_whole_sentences_and_the_rest() -> None:
    from yuki.voice.narrator import condense

    text = ("You spent most of today in VS Code on the voice work. You also had two calls, "
            "one with Priya at 3.5 hours in. Then YouTube for an hour. And a walk.")
    head, rest = condense(text, max_sentences=2, max_chars=200)
    assert head.startswith("You spent most of today") and head.endswith("hours in.")
    assert rest == "Then YouTube for an hour. And a walk."
    head, rest = condense("- first item\n- second item\n- third", max_sentences=2, max_chars=200)
    assert head == "first item second item" and rest == "third"
    long = "word " * 120
    head, rest = condense(long, max_sentences=2, max_chars=100)
    assert len(head) <= 102 and head.endswith("…") and rest


class _State:
    def __init__(self) -> None:
        self.s = {"user_speaking": False, "bot_speaking": False, "generating": False, "owes_reply": False,
                  "busy_tools": 0, "last_bot_stop": 0.0, "user_turns": 0}

    def __call__(self) -> dict:
        return dict(self.s)


def _gate(state: _State, **kw: Any) -> Any:
    from yuki.voice.narrator import NarratorGate

    return NarratorGate(state, settle_s=kw.get("settle", 0.05), settle_max_s=0.5, cooldown_s=kw.get("cool", 0.05),
                        stale_turns=kw.get("stale", 2))


def test_gate_says_each_ready_result_at_once() -> None:
    from yuki.voice.narrator import Item

    async def main() -> None:
        state, got = _State(), []
        gate = _gate(state)
        gate.start()

        def deliver(name: str) -> Any:
            async def d(run_llm: bool, stale: bool) -> None:
                got.append((name, run_llm, stale))
            return d

        # v3.14: a ready result is said at once (no settle for another job's result), each its own turn.
        state.s["jobs_open"] = 1
        gate.enqueue(Item(job=1, kind="final", deliver=deliver("a")))
        await asyncio.sleep(0.05)
        state.s["jobs_open"] = 0
        gate.enqueue(Item(job=2, kind="final", deliver=deliver("b")))
        await asyncio.sleep(0.3)
        assert got == [("a", True, False), ("b", True, False)]
        assert gate.stats["turns"] == 2 and gate.stats["merged"] == 0
        await gate.stop()

    asyncio.run(main())


def test_gate_says_a_lone_result_at_once() -> None:
    """One result, nothing else running: no settle, and only the short gap after Yuki's own speech."""
    import time as _t

    from yuki.voice.narrator import Item, NarratorGate

    async def main() -> None:
        state, got = _State(), []
        gate = NarratorGate(state, settle_s=2.0, settle_max_s=6.0, cooldown_s=2.0, final_gap_s=0.1)
        gate.start()
        state.s["last_bot_stop"] = _t.monotonic()

        async def d(run_llm: bool, stale: bool) -> None:
            got.append(_t.monotonic())

        async def step(run_llm: bool, stale: bool) -> None:
            got.append("step")

        t0 = _t.monotonic()
        gate.enqueue(Item(job=1, kind="final", deliver=d))
        await asyncio.sleep(0.3)
        assert len(got) == 1 and got[0] - t0 < 0.25  # the gap, not the 2 s cooldown or settle
        gate.enqueue(Item(job=2, kind="step", deliver=step))
        await asyncio.sleep(0.3)
        assert got[1:] == []  # a progress line still settles and waits out the cooldown
        await gate.stop()

    asyncio.run(main())


def test_gate_holds_while_user_speaks_and_folds_stale_results() -> None:
    from yuki.voice.narrator import Item

    async def main() -> None:
        state, got = _State(), []
        gate = _gate(state, stale=2)
        gate.start()

        async def d(run_llm: bool, stale: bool) -> None:
            got.append((run_llm, stale))

        state.s["user_speaking"] = True
        gate.enqueue(Item(job=1, kind="final", deliver=d))
        await asyncio.sleep(0.2)
        assert got == []  # held while the user talks
        state.s["user_turns"] = 1
        gate.poke()
        await asyncio.sleep(0.05)
        assert got == []
        state.s["user_turns"] = 2  # the conversation moved on two turns: stale
        gate.poke()
        await asyncio.sleep(0.05)
        assert got == [(False, True)]  # folded silently, never spoken
        assert gate.stats["folded"] == 1
        await gate.stop()

    asyncio.run(main())


def test_gate_supersedes_progress_and_waits_for_cooldown() -> None:
    import time as _t

    from yuki.voice.narrator import Item

    async def main() -> None:
        state, got, dropped = _State(), [], []
        gate = _gate(state, cool=0.3)
        gate.start()
        state.s["last_bot_stop"] = _t.monotonic()

        def d(name: str) -> Any:
            async def deliver(run_llm: bool, stale: bool) -> None:
                got.append(name)
            return deliver

        gate.enqueue(Item(job=1, kind="step", deliver=d("step1"), drop=lambda: dropped.append("step1")))
        gate.enqueue(Item(job=1, kind="step", deliver=d("step2")))
        await asyncio.sleep(0.15)
        assert got == []  # still in the cooldown after Yuki's own speech
        await asyncio.sleep(0.4)
        assert got == ["step2"] and dropped == ["step1"]
        await gate.stop()

    asyncio.run(main())


def test_steps_come_from_real_tool_calls_paced_by_activity() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None, settings=Settings(voice_progress_lines=2))
        bridge.attach(asyncio.get_running_loop())
        params = Params("What have I done today?")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        for name, inp in (("activity", {"since": "2026-09-25"}), ("activity", {"since": "2026-09-25"}),
                          ("note_to_self", {"text": "x"}), ("recall", {"query": "calls today"}),
                          ("look_at_window", {"hwnd": 1})):
            rt.tool_called.emit("worker", job, name, inp)
        await _settle()
        steps = [r for r, _ in params.results if "progress" in r]
        # v3.9: no spoken line per step (one "still on it" line only after real silence, tests/test_voice_v39.py);
        # the steps are real tool calls, kept for the talker's thinker line and "how's it going?"
        assert steps == []
        line = bridge.work_line() or ""
        assert f"task {job}" in line and "reading a window" in line.lower()
        log = bridge._jobs[job].log.render("running")
        assert "Looking at how your time went" in log and "Remembering" in log and "note_to_self" not in log
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_follow_up_to_a_finishing_task_starts_a_new_one() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        first = Params("Play lo-fi on Spotify")
        task = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.finished.emit("worker", job, "Lo-fi is playing on Spotify.")  # closing, lane_done not yet
        await _settle()
        pump = asyncio.create_task(_pumping())
        follow = Params("Actually use YouTube", intent="follow_up")
        follow_task = asyncio.create_task(bridge.tell_yuki(follow))
        for _ in range(50):
            await asyncio.sleep(0.005)
            if len(rt.submitted) == 2:
                break
        assert len(rt.submitted) == 2 and rt.steers == []  # not steered into a closing task
        assert "Actually use YouTube" in rt.submitted[1][1] and "Play lo-fi on Spotify" in rt.submitted[1][1]
        assert follow.results == []  # it carries the new task's outcome, not a "passed on"
        new = rt.submitted[1][0]
        rt.lane_done.emit("worker", job, "x", False, [])
        rt.started.emit("worker", new, "y")
        rt.finished.emit("worker", new, "Switched to YouTube.")
        rt.lane_done.emit("worker", new, "y", False, [])
        await asyncio.wait_for(asyncio.gather(task, follow_task), 1)
        pump.cancel()
        assert follow.results[-1][0]["result"] == "Switched to YouTube."

    asyncio.run(main())


def test_check_progress_answers_from_the_log_only() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        seen: dict[str, str] = {}

        async def answerer(log_text: str, question: str) -> tuple[str, dict]:
            seen["log"], seen["q"] = log_text, question
            return "I'm going through your activity now.", {"ms": 5}

        bridge.progress_answerer = answerer
        task = asyncio.create_task(bridge.tell_yuki(Params("What have I done today?")))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "activity", {"since": "2026-09-25"})
        await _settle()
        progress = Params("")
        progress.arguments = {"question": "how's it going?"}
        await bridge.check_progress(progress)
        assert progress.results[-1][0]["progress"] == "I'm going through your activity now."
        assert "Request: What have I done today?" in seen["log"] and "Looking at how your time went" in seen["log"]
        assert seen["q"] == "how's it going?"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_long_result_is_cut_and_the_rest_waits() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None, settings=Settings(voice_result_sentences=1))
        bridge.attach(asyncio.get_running_loop())
        params = Params("What have I done today?")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.finished.emit("worker", job, "Mostly coding in VS Code. Two calls in the afternoon. YouTube at night.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        final = params.results[-1][0]
        assert final["result"] == "Mostly coding in VS Code." and final["more_available"] is True
        more = Params("")
        await bridge.more_of_result(more)
        assert more.results[-1][0]["more"] == "Two calls in the afternoon."
        await bridge.more_of_result(more)
        assert more.results[-1][0]["more"] == "YouTube at night." and more.results[-1][0]["more_available"] is False
        await bridge.more_of_result(more)
        assert more.results[-1][0]["more"] is None

    asyncio.run(main())


def test_grounding_takes_life_facts_and_never_waits_long() -> None:
    import threading as _th
    import time as _t

    from yuki.voice.grounding import Grounder, format_facts

    release = _th.Event()

    class FakeClient:
        def recall(self, query: str, limit: int = 15, embed_wait_s: float | None = None) -> list[dict]:
            assert embed_wait_s is not None and embed_wait_s < 1.0  # never waits for a model load
            if "slow" in query:
                release.wait(2)
            return [{"kind": "chat", "fact": "The user said: hi", "at": None},
                    {"kind": "fact", "fact": "The user's accountant is Priya.", "at": "2026-09-22T10:00:00",
                     "app": "Gmail", "nature": "fact"},
                    {"kind": "fact", "fact": "The user joked they are a potato.", "at": None, "nature": "humor"}]

    class FakeMemory:
        def call(self, what: str, fn: Any, *, timeout_s: float) -> Any:
            return fn(FakeClient())

    async def main() -> None:
        g = Grounder(FakeMemory(), facts=4)
        g.new_turn()
        g.final("who is my accountant")
        lookup, how = await g.for_call(500)
        assert how == "final" and [f["kind"] for f in lookup.facts] == ["fact", "fact"]
        text = format_facts(lookup.facts)
        assert "Priya" in text and "Gmail" in text and "[humor: not a plain fact]" in text and "hi" not in text
        g.new_turn()
        g.final("slow question")
        started = _t.perf_counter()
        lookup, how = await g.for_call(60)
        assert (_t.perf_counter() - started) < 0.2 and how == "previous"  # did not wait for the slow lookup
        release.set()

    asyncio.run(main())


def test_volatile_block_goes_after_every_cache_breakpoint() -> None:
    from yuki.voice.mouth import MouthMemory, mouth_system_blocks, volatile_block
    from yuki.voice.pipeline import with_volatile

    blocks = mouth_system_blocks(MouthMemory(source="db", standing="Rules:\n- be brief"))
    assert all("[Now:" not in b["text"] for b in blocks)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hey"}]},
        {"role": "user", "content": [{"type": "text", "text": "what time is it", "cache_control": {"type": "ephemeral"}}]},
    ]
    sent = with_volatile(history, volatile_block(lookup="- (fact) x"))
    assert sent[-1]["content"][-1]["text"].startswith("[Now:") and "cache_control" not in sent[-1]["content"][-1]
    assert sent[-1]["content"][0]["cache_control"] and history[-1]["content"][-1]["text"] == "what time is it"
    assert sent[:2] == history[:2]  # nothing before the newest user message changes


def test_mouth_memory_does_not_embed() -> None:
    from yuki.voice.mouth import fetch_memory

    seen: dict[str, Any] = {}

    class Mem:
        def context(self, **kw: Any) -> Any:
            seen.update(kw)
            return SimpleNamespace(todos=[], portrait=None, standing=None, resume=None, source="db", error=None,
                                   elapsed_ms=1.0)

    fetch_memory(Mem())
    assert seen["query"] == ""  # a query would load the ~600 MB embedder for know-how the mouth never reads


# ---------------------------------------------------------------------------
# 2026-09-25 live-test fixes
# ---------------------------------------------------------------------------


def test_action_marked_as_finishing_ends_the_request_in_one_call(tmp_path: Path) -> None:
    from yuki.agent.tools import FINISHES_FIELD, TOOLS_BY_NAME

    assert FINISHES_FIELD in TOOLS_BY_NAME["run_powershell"]["input_schema"]["properties"]
    assert FINISHES_FIELD not in TOOLS_BY_NAME["look_at_desktop"]["input_schema"]["properties"]
    client = FakeClient([_response(_block("tool_use", id="t1", name="hotkey",
                                          input={"keys": ["win", "down"], FINISHES_FIELD: "Discord's minimized."}))])
    agent = _agent(client, tmp_path)
    # the input layer's own watch saw the effect (the window in front changed), as it does for a minimize (v3.14)
    agent.dispatcher.backend.hotkey = lambda *keys, **kw: StubResult(
        summary="pressed win+down; the foreground changed", details={"foreground_changed": True})
    events = list(agent.run("minimize discord"))
    assert isinstance(events[-1], Final) and events[-1].text == "Discord's minimized."
    assert len(client.calls) == 1  # no second call only to say it worked


def test_finishing_mark_needs_a_verified_report(tmp_path: Path) -> None:
    """v3.14: a finishes_with on an action whose report cannot show the effect is not the outcome: one more call."""
    from yuki.agent.tools import FINISHES_FIELD

    client = FakeClient([
        _response(_block("tool_use", id="t1", name="hotkey",
                         input={"keys": ["f11"], FINISHES_FIELD: "It's full screen."})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "It's maximized, not full screen.",
                                                                  "about": []})),
    ])
    events = list(_agent(client, tmp_path).run("make it full screen"))
    assert events[-1].text == "It's maximized, not full screen." and len(client.calls) == 2


def test_finishing_mark_is_dropped_when_the_action_fails(tmp_path: Path) -> None:
    from yuki.agent.tools import FINISHES_FIELD

    client = FakeClient([
        _response(_block("tool_use", id="t1", name="hotkey", input={"keys": [], FINISHES_FIELD: "Done."})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "That didn't work."})),
    ])
    events = list(_agent(client, tmp_path).run("minimize discord"))
    assert events[-1].text == "That didn't work." and len(client.calls) == 2


def test_voice_request_runs_at_the_voice_effort(tmp_path: Path) -> None:
    client = FakeClient([_response(_block("tool_use", id="t1", name="done", input={"message": "Six."}))] * 2)
    agent = _agent(client, tmp_path)
    list(agent.run("what time is it in tokyo", effort="low"))
    list(agent.run("typed request"))
    assert [c["output_config"]["effort"] for c in client.calls] == ["low", "medium"]


def test_remember_hand_off_is_saved_quietly_and_rules_refresh() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        refreshed: list[bool] = []

        async def on_remembered() -> None:
            refreshed.append(True)

        bridge.on_remembered = on_remembered
        params = Params("Stop calling me babe", intent="remember")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job, text, _src = rt.submitted[0]
        assert "remember_rule" in text  # the brain is told to save it
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "remember_rule", {"text": "Stop calling me babe"})
        rt.finished.emit("worker", job, "Got it, no more babe.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        assert [list(r)[0] for r, _ in params.results] == ["saved"]  # no progress line, nothing to say
        assert refreshed == [True]

    asyncio.run(main())


def test_silent_hand_off_speaks_its_own_words() -> None:
    class LLM:
        def __init__(self) -> None:
            self.pushed: list[Any] = []

        async def push_frame(self, frame: Any) -> None:
            self.pushed.append(frame)

    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        for spoke in (False, True):
            bridge.spoke_in_reply = lambda spoke=spoke: spoke
            params = Params("Minimize Discord")
            params.arguments["say_first"] = "Sure, one sec."
            params.llm = LLM()
            task = asyncio.create_task(bridge.tell_yuki(params))
            await _settle()
            said = [getattr(f, "text", None) for f in params.llm.pushed]
            assert said == ([] if spoke else ["Sure, one sec."])
            task.cancel()
            bridge.closing = True

    asyncio.run(main())


def test_end_of_speech_falls_back_to_the_last_word_heard() -> None:
    import time as _t

    from yuki.voice.pipeline import TurnObserver

    obs = TurnObserver(settings=Settings(), log=None, memory=None, session="t", speech_end=lambda: None,
                       on_state=lambda *a: None, tts_provider="elevenlabs", activity=lambda: None)
    obs._user_started_at = _t.monotonic()
    obs.on_update("minimize")
    obs.on_update("minimize discord")
    heard = obs._words_at
    obs.on_update("minimize discord")  # no new word: the time stays
    assert obs._words_at == heard
    when, how = obs._end_of_speech(_t.monotonic())
    assert how == "stt" and when == heard


def test_quiet_mic_still_marks_voiced_audio() -> None:
    import numpy as np

    from yuki.voice.audio import SharedMic

    mic = SharedMic()
    rng = np.random.default_rng(1)
    t = 100.0
    for _ in range(50):  # a quiet room, -70 dBFS
        mic.feed((rng.standard_normal(320) * 10 ** (-70 / 20)).astype(np.float32), t)
        t += 0.02
    assert mic.stats.last_voiced == 0.0
    for _ in range(25):  # speech at -50 dBFS: under the old fixed -42 dBFS, well over this room
        mic.feed((rng.standard_normal(320) * 10 ** (-50 / 20)).astype(np.float32), t)
        t += 0.02
    assert abs(mic.stats.last_voiced - t) < 0.03 and mic.stats.levels()["voiced_blocks"] >= 20


def test_turn_csv_columns_follow_the_pipeline() -> None:
    from yuki.voice.log import TURN_COLUMNS

    order = [TURN_COLUMNS.index(c) for c in ("eos_to_transcript_ms", "eos_to_first_token_ms", "eos_to_first_audio_ms",
                                             "llm_api_ttft_ms", "tts_ttfb_ms", "mouth_cost_usd")]
    assert order == sorted(order) and len(set(TURN_COLUMNS)) == len(TURN_COLUMNS)


def test_embedder_pipe_serves_only_the_key_holder_and_never_blocks(tmp_path: Path) -> None:
    import time as _t

    import numpy as np

    from yuki.memory.crypto import FieldCipher
    from yuki.memory.embed_ipc import EmbedClient, EmbedServer

    class Emb:
        model_name = "fake"

        def __init__(self, delay: float = 0.0) -> None:
            self.delay = delay

        def embed(self, texts: list[str]) -> Any:
            _t.sleep(self.delay)
            return np.ones((len(texts), 4), np.float32) / 2

        def stats(self) -> dict:
            return {"loaded": True}

    db = tmp_path / "memory.db"
    key = FieldCipher(b"k" * 32)
    slow = Emb()
    server = EmbedServer(key, db, embedder=lambda: slow).start()
    try:
        client = EmbedClient(key, db, local=lambda: (_ for _ in ()).throw(AssertionError("no local load")))
        vecs = client.embed(["hello", "there"], 1.0)
        assert vecs.shape == (2, 4) and client.last_source == "remote"
        slow.delay = 1.0  # the service is busy (loading its model): the caller goes on without
        started = _t.perf_counter()
        assert client.embed(["x"], 0.2) is None and _t.perf_counter() - started < 0.6
        slow.delay = 0.0
        stranger = EmbedClient(FieldCipher(b"z" * 32), db, local=lambda: Emb())
        assert stranger.embed(["x"], 1.0) is not None and stranger.last_source == "local"
        assert server.stats["refused"] >= 1
    finally:
        server.stop()
    absent = EmbedClient(key, tmp_path / "other.db", local=lambda: Emb())
    assert absent.embed(["x"], 0.5) is not None and absent.last_source == "local"


# ---------------------------------------------------------------------------
# v3.2: hands, one spoken result, the voice thinker, devices
# ---------------------------------------------------------------------------


def test_a_result_is_the_newest_thing_the_mouth_sees() -> None:
    """The words a reply said before its tool call go back before the call (no "." padding)."""
    import json

    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.anthropic.llm import AnthropicLLMService

    from yuki.voice.pipeline import chronological

    as_pipecat_writes_it = [
        {"role": "user", "content": "Minimize Discord."},
        {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function",
                                              "function": {"name": "window", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": json.dumps({"done": "Discord is now minimized"})},
        {"role": "assistant", "content": "Sure."},
    ]
    svc = AnthropicLLMService(api_key="x", settings=AnthropicLLMService.Settings(
        model="global.anthropic.claude-haiku-4-5-20251001-v1:0"))
    before = svc._get_llm_invocation_params(LLMContext(messages=list(as_pipecat_writes_it)))["messages"]
    assert before[-1]["content"] == [{"type": "text", "text": "."}]  # the padding that made "What's up?"
    ordered = chronological(as_pipecat_writes_it, {"t1"})
    assert [m["role"] for m in ordered] == ["user", "assistant", "assistant", "tool"]
    after = svc._get_llm_invocation_params(LLMContext(messages=ordered))["messages"]
    assert after[-1]["role"] == "user" and after[-1]["content"][0]["type"] == "tool_result"
    assert after[1]["content"][0] == {"type": "text", "text": "Sure."}  # merged: words, then the call
    # A reply that had no words is left as it was.
    assert chronological(as_pipecat_writes_it, set()) == as_pipecat_writes_it


def test_check_progress_is_refused_when_nobody_asked() -> None:
    async def main() -> None:
        rt = FakeRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        bridge.user_turn = lambda: False  # the run was started by a progress line, not the user
        params = Params("")
        params.arguments = {"question": "how's it going"}
        await bridge.check_progress(params)
        assert params.results and params.results[0][0]["progress"] is None
        bridge.user_turn = lambda: True
        params2 = Params("")
        params2.arguments = {"question": "how's it going"}
        await bridge.check_progress(params2)
        assert "Nothing is running" in params2.results[0][0]["note"]

    asyncio.run(main())


def test_progress_waits_for_a_real_pause_and_a_result_replaces_it() -> None:
    import time as _t

    from yuki.voice.narrator import Item, NarratorGate

    async def main() -> None:
        state, got = _State(), []
        gate = NarratorGate(state, settle_s=0.05, settle_max_s=0.5, cooldown_s=0.05, final_gap_s=0.05,
                            progress_pause_s=0.5)
        gate.start()
        state.s["last_bot_stop"] = _t.monotonic()  # the acknowledgement just ended

        def d(name: str) -> Any:
            async def deliver(run_llm: bool, stale: bool) -> None:
                got.append(name)
            return deliver

        gate.enqueue(Item(job=1, kind="step", deliver=d("step")))
        await asyncio.sleep(0.25)
        assert got == []  # no progress line 0.25 s after Yuki spoke
        gate.enqueue(Item(job=1, kind="final", deliver=d("final")))
        await asyncio.sleep(0.2)
        assert got == ["final"]  # the result replaced it: one spoken line for the job
        state.s["last_bot_stop"] = _t.monotonic()
        gate.enqueue(Item(job=2, kind="step", deliver=d("step2")))
        await asyncio.sleep(0.75)
        assert got == ["final", "step2"]  # a job still running after a real pause gets its line
        await gate.stop()

    asyncio.run(main())


class _Desk(StubDesktop):
    """A stub desktop with two windows and a media session, for the hands."""

    def __init__(self) -> None:
        super().__init__(action_delay_s=0)
        self.playing = True

    def get_desktop_overview(self) -> dict[str, Any]:
        o = super().get_desktop_overview()
        o["windows"] = o["windows"][:2]
        return o

    def activity_facts(self) -> dict[str, Any]:
        return {"media": [{"app_id": "Spotify", "status": "playing" if self.playing else "paused",
                           "title": "Song", "artist": "A", "current": True}]}

    def press(self, key: str, **kwargs: Any) -> Any:
        if key == "playpause":
            self.playing = not self.playing
        return self._act("press", f"pressed {key}", key, **kwargs)


def test_hands_act_only_on_windows_they_were_shown() -> None:
    from yuki.voice.hands import Hands

    desk = _Desk()
    hands = Hands(desk)
    snap = hands.snapshot()
    assert "Spotify" in snap.text and "media: Spotify \"Song\" by A playing" in snap.text
    assert snap.hwnds == {0x1001, 0x1002}
    ok, report, _ = hands._run("window", {"window": 0x1003, "action": "minimize"})
    assert not ok and "not in the desktop block" in report and not desk.calls  # never guessed
    ok, report, _ = hands._run("window", {"window": 0x1002, "action": "minimize"})
    assert ok and desk.calls[-1][0] == "window_action"
    ok, report, _ = hands._run("press_keys", {"window": 0x1001, "keys": ["k"]})
    assert not ok and "no hand called" in report  # v3.5: the talker has no keyboard (blind Ctrl+W, 26 Sept)
    ok, report, _ = hands._run("media_key", {"key": "playpause"})
    assert ok and "media: Spotify now paused (was Spotify playing)" in report  # the measured effect
    ok, report, _ = hands._run("scroll", {"window": 0x1001, "direction": "down", "amount": 3})
    assert ok and desk.calls[-1][2]["dy"] == -3 and desk.calls[-1][2]["expect_hwnd"] == 0x1001


def test_hands_report_back_and_a_failure_says_hand_over() -> None:
    from yuki.voice.hands import Hands

    async def main() -> None:
        hands = Hands(_Desk())
        hands.snapshot()
        spoken: list[str] = []

        async def push_frame(frame: Any) -> None:
            spoken.append(frame.text)

        good = Params("")
        good.llm = SimpleNamespace(push_frame=push_frame)
        good.function_name, good.arguments = "window", {"window": 0x1001, "action": "maximize",
                                                         "say_when_done": "Done, it's big."}
        await hands.handle(good)
        assert spoken == ["Done, it's big."] and good.results[0][0]["said"] == "Done, it's big."  # no model call
        bad = Params("")
        bad.llm = good.llm
        bad.function_name, bad.arguments = "window", {"window": 42, "action": "close", "say_when_done": "Closed."}
        await hands.handle(bad)
        assert spoken == ["Done, it's big."]  # a failed hand never says its done line
        assert "failed" in bad.results[0][0] and "tell_yuki" in bad.results[0][0]["how_to_say_it"]

    asyncio.run(main())


def test_no_hands_without_a_worker_backend() -> None:
    """A stand-in brain (no worker) gives the mouth no hands, never the real desktop."""
    assert VoiceBridge(FakeRuntime(), foreground=lambda: None).hands is None


def test_voice_request_runs_lean_without_thinking(tmp_path: Path) -> None:
    client = FakeClient([_response(_block("tool_use", id="t1", name="done", input={"message": "Ok."}))] * 2)
    agent = _agent(client, tmp_path)
    list(agent.run("scroll down", effort="low", thinking=False, lean=True, model="haiku"))
    list(agent.run("typed request"))
    first, second = client.calls
    assert first["thinking"] == {"type": "disabled"} and second["thinking"]["type"] == "adaptive"
    assert "haiku" in first["model"] and second["model"] == agent.settings.model


def test_a_missing_device_falls_back_to_the_default_with_a_note() -> None:
    from yuki.voice.audio import resolve_device

    devices = {"capture": ["Headset (WH-XB900N)"], "render": ["Speakers"], "default_capture": "Mic Array",
               "default_render": "Speakers"}
    assert resolve_device("capture", "Headset (WH-XB900N)", devices) == ("Headset (WH-XB900N)", "")
    name, note = resolve_device("capture", "AirPods", devices)
    assert name is None and "AirPods" in note and "Mic Array" in note
    assert resolve_device("render", None, devices) == (None, "")


def test_device_choice_is_remembered_and_shown(tmp_path: Path) -> None:
    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp_path / "sessions", requests_csv=tmp_path / "r.csv")
    session = VoiceSession(settings, FakeRuntime(), None, prefs_path=tmp_path / "voice.json")
    assert session.device_choice("capture") is None and "Mic: system default" in session.devices_text()
    shown: list[str] = []
    session.devices_changed.connect(shown.append)
    session.set_device("capture", "Headset (WH-XB900N)")
    session.set_device("render", "Speakers (4- M-Audio AIR 192 4)")
    again = VoiceSession(settings, FakeRuntime(), None, prefs_path=tmp_path / "voice.json")
    assert again.device_choice("capture") == "Headset (WH-XB900N)"
    assert again.device_choice("render") == "Speakers (4- M-Audio AIR 192 4)"
    assert shown and "Speaker: Speakers (4- M-Audio AIR 192 4)" in shown[-1]
    session.set_device("capture", None)
    assert again.device_choice("capture") is None
