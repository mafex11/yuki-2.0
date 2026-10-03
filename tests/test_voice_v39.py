"""Voice v3.9 (2026-09-27, the 14:16-14:22 session): the talker and the thinker in sync. Offline checks.

Evidence: a bare "Yuki" got no reply; a failed media key was narrated three times; a stale result answered a new
request; tab, media and click jobs looped; the talker offered to close all of Chrome. Covered here: the real
withdraw path (a streaming thinker response dropped at once, shown quietly cancelled), newer requests replacing
older ones (model-judged ``instead``), results bound to the request they answer (late results framed as such,
superseded ones never spoken), one spoken outcome per job (a failed hand passed to the thinker by code, one "still
on it" line only into silence), the talker's view of the thinker, the window read for on-screen requests, the
tabs with their ids for the thinker, the tab close control of a playing tab, and an unanswered turn answered again.
No microphone, no speakers, no network: fakes and stand-ins only.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])


# -- shared stand-ins ------------------------------------------------------------------------------

class FakeRuntime(QObject):
    """The runtime's signals and methods the bridge uses, with v3.9's withdraw and submit lanes."""

    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    withdrawn = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 1
        self.submitted: list[tuple[int, str, dict]] = []
        self.steers: list[tuple[int, str]] = []
        self.cancels: list[int] = []
        self.withdrawals: list[int] = []

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", **kw: Any) -> tuple[str, int]:
        job_id, self.next_id = self.next_id, self.next_id + 1
        self.submitted.append((job_id, text, dict(kw)))
        return "worker", job_id

    def steer(self, job_id: int, text: str) -> bool:
        self.steers.append((job_id, text))
        return True

    def answer(self, lane: str, text: str) -> None:
        pass

    def cancel(self, job_id: int, reason: str = "cancelled") -> str:
        self.cancels.append(job_id)
        return "running"

    def withdraw(self, job_id: int) -> str:
        self.withdrawals.append(job_id)
        return "running"


class Params:
    """A Pipecat function call's params for the bridge's handlers."""

    def __init__(self, request: str = "Play the make up my mind mix", call_id: str = "call-1",
                 intent: str = "new_task", heard: str = "play the make up my mind mix", **extra: Any) -> None:
        self.arguments = {"screen": False, "say_first": "On it.", "request": request, "intent": intent, **extra}
        self.tool_call_id = call_id
        self.function_name = "tell_yuki"
        self.context = SimpleNamespace(get_messages=lambda: [
            {"role": "assistant", "content": "Hey, what's up?"}, {"role": "user", "content": heard}])
        self.results: list[tuple[dict, Any]] = []
        self.pushed: list[Any] = []
        self.llm = SimpleNamespace(push_frame=self._push)

    async def _push(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


async def _settle(n: int = 8) -> None:
    for _ in range(n):
        APP.processEvents()
        await asyncio.sleep(0)


def _bridge(rt: FakeRuntime | None = None, **kw: Any) -> Any:
    from yuki.voice.bridge import VoiceBridge

    rt = rt or FakeRuntime()
    bridge = VoiceBridge(rt, foreground=lambda: 42, **kw)
    bridge.attach(asyncio.get_running_loop())
    return rt, bridge


def _run_llm(props: Any) -> Any:
    return getattr(props, "run_llm", None) if props is not None else None


# -- A. the thinker: a real withdraw path --------------------------------------------------------------

class _SlowStream:
    """A streaming response of many events; ``on_event(n)`` runs as each is read. Records whether it was closed."""

    def __init__(self, owner: "_StreamClient") -> None:
        self.owner = owner

    def __enter__(self) -> "_SlowStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.owner.closed = True

    def __iter__(self) -> Any:
        for n in range(1, 60):
            self.owner.read = n
            self.owner.on_event(n)
            yield SimpleNamespace(type="ping")

    def get_final_message(self) -> Any:
        raise AssertionError("a dropped stream is never read to its end")


class _StreamClient:
    def __init__(self, on_event: Any) -> None:
        self.on_event = on_event
        self.read = 0
        self.closed = False
        self.messages = SimpleNamespace(stream=lambda **p: _SlowStream(self), create=None)


def _agent(client: Any, tmp: Path, *, stream: bool = True, backend: Any = None) -> Any:
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.log.events import SessionLogger
    from yuki.voice.offline import StubDesktop

    settings = Settings(log_dir=tmp / "sessions", stream=stream, requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    return Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                 backend=backend or StubDesktop(action_delay_s=0), prewarm=False, record_requests=False,
                 memory=memory)


def test_withdraw_drops_a_streaming_response_at_once(tmp_path: Path) -> None:
    from yuki.agent.loop import WITHDRAWN
    from yuki.log.events import ErrorEvent, Final

    box: dict[str, Any] = {}
    client = _StreamClient(lambda n: box["a"].cancel(WITHDRAWN) if n == 2 else None)
    agent = _agent(client, tmp_path)
    box["a"] = agent
    events = list(agent.run("play the make up my mind mix"))
    assert not any(isinstance(e, Final) for e in events)
    assert isinstance(events[-1], ErrorEvent) and events[-1].text.startswith("Withdrawn")
    assert client.read <= 3 and client.closed  # dropped at its next event, the connection closed
    assert agent.last_summary["outcome"] == "withdrawn"
    notes = json.dumps(agent.context.messages, default=str)
    assert "withdrawn in favour of their newer request" in notes


def test_withdrawn_run_is_signalled_quietly_and_never_handed_to_the_worker(tmp_path: Path) -> None:
    from yuki.log.events import ErrorEvent
    from yuki.ui.runtime import AgentRuntime, HandsBlockedBackend, Job
    from yuki.ui.uilog import UiLog
    from yuki.voice.offline import StubDesktop

    class FakeAgent:
        tool_names = None
        cancelled = True
        cancel_reason = "withdrawn"
        last_summary = None
        awaiting_answer = False

        def run(self, *a: Any, **k: Any) -> Any:
            yield ErrorEvent("Withdrawn: replaced by the user's newer request.")

        def take_steers(self) -> list:
            return []

        def cancel(self, reason: str = "cancelled") -> None:
            pass

    blocked = HandsBlockedBackend(StubDesktop(action_delay_s=0))
    blocked.attempted = ["click"]  # the front desk had reached for hands before it was withdrawn
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=FakeAgent(),
                      front_desk_agent=FakeAgent(), front_desk_blocked=blocked,
                      memory=SimpleNamespace(session_id="t"))
    seen: list[tuple[str, int]] = []
    rt.withdrawn.connect(lambda lane, job, text: seen.append(("withdrawn", job)))
    rt.failed.connect(lambda lane, job, text: seen.append(("failed", job)))
    blocked.reset = lambda: None  # keep the attempt for this check
    needed = rt.front_desk._drive(Job(7, "close the tab", None, "voice"))
    assert seen == [("withdrawn", 7)] and needed == []


def test_a_queued_request_withdrawn_before_it_starts(tmp_path: Path) -> None:
    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    agent = SimpleNamespace(tool_names=None, cancel=lambda reason="cancelled": None, awaiting_answer=False)
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=agent, memory=SimpleNamespace(session_id="t"))
    seen: list[str] = []
    rt.withdrawn.connect(lambda lane, job, text: seen.append(f"withdrawn {job}"))
    rt.failed.connect(lambda lane, job, text: seen.append(f"failed {job}"))
    rt.worker.enqueue(Job(3, "pause it", None, "voice"))
    assert rt.withdraw(3) == "queued"
    rt.worker._jobs.put(None)
    rt.worker.run()  # the lane's loop, here on this thread: the withdrawn job is skipped, then it ends
    assert seen == ["withdrawn 3"]


def test_submit_to_the_worker_skips_the_front_desk(tmp_path: Path) -> None:
    from yuki.ui.runtime import AgentRuntime
    from yuki.ui.uilog import UiLog

    agent = SimpleNamespace(tool_names=None)
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=agent, memory=SimpleNamespace(session_id="t"))
    rt._worker_outstanding.append(99)  # the worker is busy
    assert rt.submit("what time is it", source="voice")[0] == "front_desk"
    lane, job = rt.submit("close the tab", source="voice", lane="worker", view_hwnd=5)
    assert lane == "worker"
    assert rt.worker.waiting_ids() == [job]


def test_a_request_about_the_screen_gets_the_window_read_with_it(tmp_path: Path) -> None:
    from yuki.voice.offline import StubDesktop

    def resp(*blocks: Any) -> Any:
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage, id="m", model="m",
                               role="assistant", model_dump=lambda **_: {})

    calls: list[dict] = []
    done = SimpleNamespace(type="tool_use", id="t1", name="done", input={"message": "Playing it."})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.append(p) or resp(done)))
    agent = _agent(client, tmp_path, stream=False, backend=StubDesktop(action_delay_s=0))
    list(agent.run("click the mix on the screen", view_hwnd=4097))
    first = json.dumps(calls[0]["messages"], default=str)
    assert "The window in front when they asked, read for you" in first
    calls.clear()
    list(agent.run("what time is it"))
    assert "read for you as the request came in" not in json.dumps(calls[0]["messages"][-1], default=str)


# -- B. the bridge: newer requests win, results bound to their request ------------------------------------

def test_instead_withdraws_the_running_job_and_runs_the_new_one_on_the_worker() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        first = Params("Play the make up my mind mix", call_id="c1")
        t1 = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        job_a = rt.submitted[0][0]
        rt.started.emit("worker", job_a, "x")
        await _settle()
        second = Params("Close the YouTube tab", call_id="c2", intent="instead", heard="no, close the tab",
                        acts=True)
        t2 = asyncio.create_task(bridge.tell_yuki(second))
        await _settle()
        assert rt.withdrawals == [job_a]
        job_b, _text, kw = rt.submitted[1]
        assert kw.get("lane") == "worker"
        # the withdrawn job ends quietly: its call settles with no talker run and nothing to say
        rt.withdrawn.emit("worker", job_a, "Withdrawn: replaced by the user's newer request.")
        rt.lane_done.emit("worker", job_a, "x", False, [])
        await _settle()
        await asyncio.wait_for(t1, 1)
        result, props = first.results[-1]
        assert "withdrawn" in result and "result" not in result and _run_llm(props) is False
        line = bridge.work_line() or ""
        assert "withdrawn for a newer request" in line
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2

    asyncio.run(main())


def test_a_superseded_result_that_raced_the_withdrawal_is_never_spoken() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        first = Params(call_id="c1")
        t1 = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        job_a = rt.submitted[0][0]
        rt.started.emit("worker", job_a, "x")
        second = Params("Close the tab", call_id="c2", intent="instead", heard="no close the tab")
        t2 = asyncio.create_task(bridge.tell_yuki(second))
        await _settle()
        rt.finished.emit("worker", job_a, "Playing the make up my mind mix now.")  # finished before the cancel
        rt.lane_done.emit("worker", job_a, "x", False, [])
        await _settle()
        await asyncio.wait_for(t1, 1)
        assert all("result" not in r for r, _ in first.results)
        assert "withdrawn" in first.results[-1][0]
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2

    asyncio.run(main())


def test_a_result_after_the_user_moved_on_is_framed_as_the_earlier_request() -> None:
    from yuki.voice.mouth import LATE_NOTE

    async def main() -> None:
        rt, bridge = _bridge()
        turns = {"n": 1}
        bridge.user_turns = lambda: turns["n"]
        params = Params("Play the make up my mind mix")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "click", {"element": 9, "finishes_with": "Playing the mix now."})
        rt.tool_finished.emit("worker", job, "click", True, "clicked")
        turns["n"] = 3  # "close it", "close the staff": the user said more meanwhile
        rt.finished.emit("worker", job, "Playing the mix now.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        result, props = params.results[-1]
        assert result.get("late") is True and result["how_to_say_it"].startswith(LATE_NOTE[:40])
        assert result["for_request"] == "Play the make up my mind mix"
        assert not params.pushed  # never said by code as if it answered their newest words
        assert bridge.said_owed_at is None  # a late result may be let go (stay_quiet is not refused)

    asyncio.run(main())


def test_a_verified_result_on_time_is_said_once_by_code_and_bound_to_its_request() -> None:
    """v3.17 (C1): a verified finish is said by code, word for word, once (was v3.10: the talker said it)."""
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.user_turns = lambda: 1
        params = Params("Pause the video")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "press", {"key": "k", "finishes_with": "Paused it."})
        rt.tool_finished.emit("worker", job, "press", True, "pressed k")
        rt.finished.emit("worker", job, "Paused it.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        assert [f.text for f in params.pushed] == ["Paused it."]   # by code, once (C1)
        result, props = params.results[-1]
        assert result["verified"] is True and result["for_request"] == "Pause the video" and props.run_llm is False
        assert result["said"] is True

    asyncio.run(main())


def test_one_still_on_it_line_only_into_silence() -> None:
    async def main() -> None:
        rt, bridge = _bridge(settings=Settings(voice_wait_line_s=0.05, voice_wait_again_s=0.1))
        quiet = {"s": 0.0}
        bridge.quiet_for = lambda: quiet["s"]
        params = Params("Find my September invoices")
        params.arguments["say_first"] = ""  # handed over without an acknowledgement (v3.10: else no line at all)
        bridge.spoke_in_reply = lambda: False
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "read_file", {"say_now": "Reading the invoices.", "path": "x"})
        await asyncio.sleep(0.2)
        await _settle()
        assert not [r for r, _ in params.results if "still_working_on" in r]  # someone is speaking: nothing
        quiet["s"] = 10.0
        await asyncio.sleep(0.5)
        await _settle()
        lines = [r for r, _ in params.results if "still_working_on" in r]
        # v3.14: it names the thinker's own words for the running step, never the tool's generic label
        assert lines and lines[0]["now"] == "Reading the invoices." and lines[0]["for_seconds"] >= 0
        await asyncio.sleep(0.4)
        await _settle()
        # at most voice_progress_lines (2) for a job nobody acknowledged (v3.14: the setting holds)
        assert len([r for r, _ in params.results if "still_working_on" in r]) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_an_acknowledged_job_gets_one_waiting_line_after_real_silence() -> None:
    """v3.14 (was v3.10's "no line at all"): after "On it." and real silence, one "still on it", never a second. The
    09:08 "second voice" came from the tool's generic label; the line now names only the thinker's own words."""
    async def main() -> None:
        rt, bridge = _bridge(settings=Settings(voice_wait_line_s=0.05, voice_wait_again_s=0.1))
        bridge.quiet_for = lambda: 10.0
        params = Params("Find my September invoices")  # say_first "On it."
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        await asyncio.sleep(0.4)
        await _settle()
        lines = [r for r, _ in params.results if "still_working_on" in r]
        assert len(lines) == 1 and "now" not in lines[0]   # no step running: no step named
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_a_quick_job_gets_no_waiting_line() -> None:
    async def main() -> None:
        rt, bridge = _bridge(settings=Settings(voice_wait_line_s=0.3))
        bridge.quiet_for = lambda: 10.0
        params = Params("Pause it")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.finished.emit("worker", job, "Paused it.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        await asyncio.sleep(0.5)
        assert not [r for r, _ in params.results if "still_working_on" in r]

    asyncio.run(main())


def test_the_talker_sees_what_the_thinker_is_doing() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        assert bridge.work_line() is None
        first = asyncio.create_task(bridge.tell_yuki(Params("Play the mix", call_id="a")))
        await _settle()
        job_a = rt.submitted[0][0]
        rt.started.emit("worker", job_a, "x")
        rt.tool_called.emit("worker", job_a, "look_at_window", {"hwnd": 1})
        second = asyncio.create_task(bridge.tell_yuki(Params("Close the tab", call_id="b", acts=True)))
        await _settle()
        line = bridge.work_line() or ""
        assert f'task {job_a} "Play the mix" running' in line and "reading a window" in line.lower()
        assert f'task {rt.submitted[1][0]} "Close the tab" queued' in line
        rt.finished.emit("worker", job_a, "The mix is playing.")
        rt.lane_done.emit("worker", job_a, "x", False, [])
        await _settle()
        assert '"Play the mix" finished' in (bridge.work_line() or "") and "The mix is playing." in bridge.work_line()
        for t in (first, second):
            t.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(main())


def test_the_thinker_gets_the_tabs_with_ids_and_what_the_voice_said() -> None:
    from yuki.voice.hands import Snapshot

    async def main() -> None:
        rt, bridge = _bridge()
        bridge.hands = SimpleNamespace(last=Snapshot(foreground=77, hwnds={77}, tab_ids={77: ["42.1", "42.2"]},
                                                     tabs={77: ["Inbox", "Make Up My Mind - YouTube"]}))
        params = Params("Close the YouTube tab", heard="close the youtube tab")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        text = rt.submitted[0][1]
        assert '2 [42.2] "Make Up My Mind - YouTube"' in text and "The tab tool takes these ids" in text
        assert "What Yuki's voice already said to them" in text and '"On it."' in text and "Hey, what's up?" in text
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_screen_true_reads_the_window_for_the_thinker() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        params = Params("Click the mix, it's on the screen", screen=True)
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        assert rt.submitted[0][2].get("view_hwnd") == 42
        bridge.begin_early("e1", ["click the second video"], screen=True)
        bridge.begin_early("e2", ["what did I do today"], screen=False)
        await _settle()
        assert rt.submitted[1][2].get("view_hwnd") == 42 and "view_hwnd" not in rt.submitted[2][2]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


# -- C. one job, one spoken outcome: a hand that did not take effect ----------------------------------------

class _MediaDesk:
    """A stub backend: a media session that never changes (the key does not take), and nothing else."""

    def __init__(self, media: list | None) -> None:
        self.media = media
        self.pressed: list[str] = []

    def press(self, key: str, times: int = 1, **_: Any) -> Any:
        self.pressed.append(key)
        return SimpleNamespace(ok=True, summary=f"pressed {key}", details={}, elapsed_ms=1.0)

    def activity_facts(self) -> dict:
        return {"media": self.media or []}

    def get_desktop_overview(self) -> dict:
        return {"windows": [], "foreground_hwnd": None}

    def format_overview(self, overview: dict) -> str:
        return ""


def _hands(backend: Any, bridge: Any) -> Any:
    from yuki.voice.hands import Hands

    hands = Hands(backend)
    hands.bridge = bridge
    hands._dispatcher = SimpleNamespace(activity_facts=backend.activity_facts,
                                        overview_text=lambda: ("", {"foreground_hwnd": None}, 0.0))
    return hands


def _hand_params(name: str, args: dict) -> Any:
    p = Params()
    p.function_name = name
    p.arguments = args
    p.tool_call_id = f"h-{name}"
    return p


def test_a_hand_that_did_not_take_is_passed_to_the_thinker_without_a_talker_run() -> None:
    async def main() -> None:
        import yuki.voice.hands as hands_module

        rt, bridge = _bridge()
        announced: list[tuple[dict, bool]] = []

        async def announce(payload: dict, run_llm: bool = True) -> None:
            announced.append((payload, run_llm))

        spoken: list[str] = []

        async def speak(text: str) -> None:
            spoken.append(text)

        bridge.announce, bridge.speak = announce, speak
        desk = _MediaDesk([{"app_id": "Chrome", "status": "playing", "title": "Make Up My Mind", "current": True}])
        hands = _hands(desk, bridge)
        old = hands_module._WATCH_S
        hands_module._WATCH_S = 0.05
        try:
            bridge.user_turns = lambda: 1
            bridge.acked()   # their words were acknowledged already (v3.14: then no talker run at all, as in v3.9)
            params = _hand_params("media_key", {"key": "playpause", "want": "paused", "say_when_done": "Paused."})
            await hands.handle(params)
        finally:
            hands_module._WATCH_S = old
        result, props = params.results[-1]
        assert result.get("passed_on") is True and _run_llm(props) is False  # no talker run narrates it
        await _settle()
        job, text, kw = rt.submitted[0]
        assert kw.get("lane") == "worker" and kw.get("view_hwnd") == 42
        assert "did not take effect" in text and "play the make up my mind mix" in text
        # the thinker's own finishing line is the one outcome, said once, by code (v3.17 C1; v3.10: by the talker)
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "press", {"key": "k", "finishes_with": "Paused it."})
        rt.tool_finished.emit("worker", job, "press", True, "pressed k")
        rt.finished.emit("worker", job, "Paused it.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle(20)
        assert spoken == ["Paused it."]
        assert announced and announced[-1][0].get("verified") is True and announced[-1][0].get("said") is True
        assert announced[-1][1] is False   # no talker run for it

    asyncio.run(main())


def test_a_refused_or_settled_hand_is_not_passed_on() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        desk = _MediaDesk([{"app_id": "Chrome", "status": "paused", "title": "x", "current": True}])
        hands = _hands(desk, bridge)
        params = _hand_params("media_key", {"key": "playpause", "want": "paused", "say_when_done": "Paused."})
        await hands.handle(params)  # already paused: settled, nothing pressed, the talker says so
        assert desk.pressed == [] and "not_verified" in params.results[-1][0]
        assert rt.submitted == []

    asyncio.run(main())


def test_a_media_key_with_no_session_presses_nothing() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        desk = _MediaDesk(None)
        hands = _hands(desk, bridge)
        ok, report, facts = hands._run("media_key", {"key": "stop"})
        assert not ok and desk.pressed == [] and "nothing was pressed" in report and not facts.get("refused")

    asyncio.run(main())


def test_every_hand_halts_after_a_wrong_effect_until_the_user_speaks() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        desk = _MediaDesk(None)
        hands = _hands(desk, bridge)
        hands._run("media_key", {"key": "playpause"})  # no session: nothing could be checked
        ok, report, facts = hands._run("media_key", {"key": "playpause"})
        assert not ok and facts.get("refused") and "stay still" in report
        hands.new_user_turn()
        _ok, report, facts = hands._run("media_key", {"key": "playpause"})
        assert not facts.get("refused")

    asyncio.run(main())


def test_closing_a_whole_window_needs_the_users_words() -> None:
    async def main() -> None:
        from yuki.voice.hands import Snapshot

        _rt, bridge = _bridge()
        hands = _hands(_MediaDesk(None), bridge)
        hands.last = Snapshot(hwnds={5})
        ok, report, facts = hands._run("window", {"window": 5, "action": "close"})
        assert not ok and facts.get("refused") and "Nothing was closed" in report

    asyncio.run(main())


# -- D. the tab close control of a playing tab ---------------------------------------------------------------

def test_the_close_control_is_the_rightmost_button_of_the_tab() -> None:
    from yuki.actions.tabs import pick_close_control

    sound, close = object(), object()
    assert pick_close_control([(close, None)]) is close
    assert pick_close_control([(sound, (1500, 10, 1516, 30)), (close, (1540, 10, 1556, 30))]) is close
    assert pick_close_control([(close, (1540, 10, 1556, 30)), (sound, (1500, 10, 1516, 30))]) is close
    assert pick_close_control([(sound, None), (close, (1540, 10, 1556, 30))]) is None
    assert pick_close_control([]) is None


# -- E. the talker: schema, thinker line, early start, an unanswered turn --------------------------------------

def test_tell_yuki_writes_screen_first_and_can_replace_work() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, TELL_YUKI

    props = list(TELL_YUKI["properties"])
    assert props[0] == "screen" and "screen" in TELL_YUKI["required"]
    assert "instead" in TELL_YUKI["properties"]["intent"]["enum"]
    assert "replaces" in props and "acts" in props
    from yuki.voice.mouth import HANDS_PROMPT

    assert "Your name alone" in MOUTH_PROMPT and "never argue" in HANDS_PROMPT   # v3.17: the hands' own block


def test_the_volatile_block_carries_the_thinker_line() -> None:
    from yuki.voice.mouth import volatile_block

    text = volatile_block(work='task 4 "Play the mix" running for 6 s, now: clicking')["text"]
    assert "Your thinker now" in text and "task 4" in text
    assert "Your thinker now" not in volatile_block()["text"]


def test_the_screen_flag_is_read_from_the_streaming_call() -> None:
    from yuki.voice.pipeline import early_flag

    assert early_flag('{"scr', "screen") is None
    assert early_flag('{"screen": ', "screen") is None
    assert early_flag('{"screen": true, "say_', "screen") is True
    assert early_flag('{"screen":false', "screen") is False


def _conversation(tmp_path: Path) -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation, _Turn

    bridge = SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None)
    conv = Conversation(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), bridge=bridge,
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=SimpleNamespace(
                            event=lambda *a, **k: None), speech_end=lambda: None, on_state=lambda *a: None,
                        session="t")
    queued: list[Any] = []

    async def queue_frame(frame: Any) -> None:
        queued.append(frame)

    conv.worker = SimpleNamespace(queue_frame=queue_frame)
    return conv, queued, _Turn


def test_an_unanswered_turn_is_answered_again_once(tmp_path: Path) -> None:
    conv, queued, Turn = _conversation(tmp_path)
    now = time.monotonic()
    conv.observer.turn = Turn(n=3, trigger="user", opened=now - 2, user_text="Yuki.", cut=True,
                              transcript_at=now - 1.5)
    conv.observer.last_user_at = now - 1.5

    async def main() -> None:
        watch = asyncio.create_task(conv._reply_watch(every_s=0.02))
        await asyncio.sleep(0.3)
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)

    asyncio.run(main())
    assert [type(f).__name__ for f in queued] == ["LLMRunFrame"] and conv.observer.turn.rerun


def test_a_turn_that_was_heard_or_acted_on_is_left_alone(tmp_path: Path) -> None:
    conv, queued, Turn = _conversation(tmp_path)
    now = time.monotonic()
    for turn in (Turn(n=1, trigger="user", opened=now - 2, user_text="pause it", cut=True, transcript_at=now - 1.5,
                      tools=["media_key"]),
                 Turn(n=2, trigger="user", opened=now - 2, user_text="hi", responses=1, transcript_at=now - 1.5,
                      first_audio=now - 1.0, audible=["Hey! "]),   # v3.16: answered = words heard, not any audio
                 Turn(n=3, trigger="result", opened=now - 2, user_text="", responses=1)):
        conv.observer.turn = turn
        assert conv._unanswered() is None
    conv.observer.turn = Turn(n=4, trigger="user", opened=now, user_text="open chrome", transcript_at=now)
    assert conv._unanswered() is None  # its reply may still be on its way


# -- F. the chat: a withdrawn job is quietly cancelled ------------------------------------------------------------

def test_the_chat_shows_a_withdrawn_job_as_cancelled_not_failed() -> None:
    from yuki.ui.chat_model import ChatModel

    model = ChatModel()
    model.apply({"type": "voice_job", "job": 7, "request": "Play the make up my mind mix", "at": 1.0})
    model.apply({"type": "tool_called", "job": 7, "name": "click", "at": 1.5})
    model.apply({"type": "withdrawn", "job": 7, "text": "Withdrawn: replaced by the user's newer request.", "at": 2.0})
    item = next(i for i in model.items if 7 in i.jobs)
    assert item.status != "failed"
    assert all(not s.running and s.ok is not False for s in item.steps)
    assert any("Cancelled" in s.result for s in item.steps)


def test_how_is_it_going_is_answered_once() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        asked: list[str] = []

        async def answerer(log: str, question: str) -> tuple[str, dict]:
            asked.append(question)
            return "Opening YouTube now.", {}

        bridge.progress_answerer = answerer
        task = asyncio.create_task(bridge.tell_yuki(Params("Find a jazz mix")))
        await _settle()
        rt.started.emit("worker", rt.submitted[0][0], "x")
        await _settle()
        params = Params()
        params.arguments = {"question": "what's happening?"}
        bridge.spoke_in_reply = lambda: True  # the reply already answered from the thinker line
        await bridge.check_progress(params)
        result, props = params.results[-1]
        assert result["progress"] is None and _run_llm(props) is False and asked == []
        bridge.spoke_in_reply = lambda: False
        await bridge.check_progress(params)
        assert params.results[-1][0]["progress"] == "Opening YouTube now." and asked == ["what's happening?"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_the_cancel_tool_sends_replacements_to_instead() -> None:
    from pipecat.services.anthropic.llm import AnthropicLLMService

    from yuki.voice.mouth import CANCEL_TELL_YUKI_DESCRIPTION, MouthMemory
    from yuki.voice.pipeline import _layered_service

    llm = _layered_service(AnthropicLLMService, MouthMemory(), object())(
        api_key="x", settings=AnthropicLLMService.Settings(model="m"))

    async def handler(params: Any) -> None:
        pass

    llm.register_function("tell_yuki", handler, cancellable_by_llm=True, cancel_on_interruption=False)
    llm._sync_cancel_tools()
    tool = llm._adapter.builtin_tools["cancel_tell_yuki"]
    assert tool.description == CANCEL_TELL_YUKI_DESCRIPTION and "intent instead" in tool.description


def test_a_tool_call_written_as_text_is_never_the_final_answer(tmp_path: Path) -> None:
    from yuki.agent.loop import written_tool_call
    from yuki.log.events import Final

    assert written_tool_call('\n<invoke name="click">\n<parameter name="element">5</parameter>', ["click"]) == "click"
    assert written_tool_call("Playing it now.", ["click"]) is None
    assert written_tool_call('<invoke name="launch_rockets">', ["click"]) is None

    def resp(*blocks: Any, stop: str = "tool_use") -> Any:
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, id="m", model="m",
                               role="assistant", model_dump=lambda **_: {})

    written = SimpleNamespace(type="text", text='\n<invoke name="click">\n<parameter name="x">5</parameter>\n</invoke>')
    done = SimpleNamespace(type="tool_use", id="t1", name="done", input={"message": "Playing it now."})
    replies = [resp(written), resp(done)]
    calls: list[dict] = []
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.append(p) or replies.pop(0)))
    events = list(_agent(client, tmp_path, stream=False).run("play make up my mind"))
    assert isinstance(events[-1], Final) and events[-1].text == "Playing it now."
    assert "wrote a click call out as text" in json.dumps(calls[1]["messages"], default=str)


def test_a_hand_off_right_after_the_talkers_hands_used_the_page_gets_the_window_read() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.actions.add("your hand", "scroll 12291 down", 'scrolled Document "YouTube" down', ok=True,
                           verified=True)
        task = asyncio.create_task(bridge.tell_yuki(Params("Play make up my mind", acts=True)))
        await _settle()
        assert rt.submitted[0][2].get("view_hwnd") == 42  # no screen mark, but they were just on that page
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_a_spoken_request_gets_the_window_as_a_look_already_made(tmp_path: Path) -> None:
    from yuki.voice.offline import StubDesktop

    def resp(*blocks: Any) -> Any:
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage, id="m", model="m",
                               role="assistant", model_dump=lambda **_: {})

    calls: list[dict] = []
    done = SimpleNamespace(type="tool_use", id="t1", name="done", input={"message": "Playing it."})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.append(p) or resp(done)))
    agent = _agent(client, tmp_path, stream=False, backend=StubDesktop(action_delay_s=0))
    list(agent.run("play make up my mind", view_hwnd=4097, thinking=False))
    messages = calls[0]["messages"]
    assert messages[-2]["role"] == "assistant"
    call = messages[-2]["content"][0]
    assert call["type"] == "tool_use" and call["name"] == "look_at_window" and call["input"] == {"hwnd": 4097}
    result = messages[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == call["id"]


def test_silence_is_refused_once_when_the_user_is_waiting() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        turns = {"n": 4}
        bridge.user_turns = lambda: turns["n"]
        bridge.user_turn = lambda: True
        bridge.call_mode = lambda: False
        bridge.in_exchange = lambda: True
        p = Params()
        await bridge.stay_quiet(p)  # vfix V2: a stay_quiet on the user's words is final, in an exchange too
        assert p.results[-1][0] == {"ok": True}
        await bridge.stay_quiet(p)
        assert p.results[-1][0] == {"ok": True}
        bridge.call_mode = lambda: True  # in a call, quiet is the mouth's judgement at once
        turns["n"] = 5
        await bridge.stay_quiet(p)
        assert p.results[-1][0] == {"ok": True}
        bridge.call_mode = lambda: False
        bridge.user_turn = lambda: False  # the run a held call's report started
        bridge.held_at = time.monotonic()
        bridge.held_call = ("tell_yuki", {"screen": True, "say_first": "Finding it.", "request": "Play the Stellar "
                                          "Blade one", "acts": True, "intent": "new_task"}, Params().context)
        spoken: list[str] = []

        async def speak(text: str) -> None:
            spoken.append(text)

        bridge.speak = speak
        await bridge.stay_quiet(p)
        assert "still waiting" in p.results[-1][0]["not_quiet"]
        await bridge.stay_quiet(p)  # quiet again on a held hand-off: it is done after all, never silence
        await _settle()
        assert spoken == ["Finding it."]
        assert {k: v for k, v in _rt.submitted[-1][2].items() if k != "memory_query"} == {"lane": "worker",
                                                                                         "view_hwnd": 42}

    asyncio.run(main())
