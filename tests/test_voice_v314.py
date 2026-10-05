"""Voice v3.14 ("voice feel"): the tricky logic only; the evidence is the offline replays (docs/VOICE.md v3.14).

Fake model client and stub desks: no microphone, no speakers, no network.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from yuki.agent.loop import Agent, ToolOutcome, action_verdict
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import Final, SessionLogger
from yuki.voice.offline import BrowserDesk, StubDesktop, StubResult


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _response(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        return self.responses.pop(0)


def _agent(client: FakeClient, tmp: Path, backend: Any = None) -> Agent:
    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    return Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                 backend=backend or StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)


# -- the verdict, from the action's own report ---------------------------------------------------------


def test_action_verdict_reads_the_report_never_the_words() -> None:
    def out(name: str, details: dict, ok: bool = True) -> ToolOutcome:
        return ToolOutcome(name=name, ok=ok, summary="whatever it says", payload={"details": details})

    assert action_verdict("click", out("click", {"page_changed": True, "focus_changed": False}))[0] is True
    assert action_verdict("hotkey", out("hotkey", {"foreground_changed": False, "focus_changed": False,
                                                   "foreground_title_changed": False}))[0] is False
    assert action_verdict("press", out("press", {}))[0] is None
    assert action_verdict("type_text", out("type_text", {"readback": "arrived"}))[0] is True
    assert action_verdict("type_text", out("type_text", {"readback": "unreadable"}))[0] is None
    assert action_verdict("run_powershell", out("run_powershell", {}))[0] is None
    assert action_verdict("tab", out("tab", {"verified": True}))[0] is True
    assert action_verdict("window_action", out("window_action", {}))[0] is True
    assert action_verdict("click", out("click", {"page_changed": True}, ok=False))[0] is False


# -- per-step effort: fast first pass, escalated on a measured signal ------------------------------------


def test_unverified_action_escalates_the_next_call_and_holds_its_finish(tmp_path: Path) -> None:
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="hotkey",
                         input={"keys": ["win", "up"], "finishes_with": "It's full screen."})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "It's maximized.", "about": [],
                                                                  "claims": "tried"})),   # fix6: its class
    ])
    events = list(_agent(client, tmp_path).run("make it full screen", effort="low", thinking=False,
                                               escalate=("medium", True)))
    assert isinstance(events[-1], Final) and events[-1].text == "It's maximized."
    first, second = client.calls
    assert first["thinking"] == {"type": "disabled"} and first["output_config"]["effort"] == "low"
    assert second["thinking"]["type"] == "adaptive" and second["output_config"]["effort"] == "medium"
    notes = " ".join(str(b.get("text", "")) for m in second["messages"][-1:] for b in m["content"]
                     if isinstance(b, dict) and b.get("type") == "text")
    assert "not verified" in notes.casefold() or "not said" in notes.casefold()


def test_verified_action_stays_fast_and_finishes_in_one_call(tmp_path: Path) -> None:
    client = FakeClient([_response(_block("tool_use", id="t1", name="hotkey",
                                          input={"keys": ["win", "down"], "finishes_with": "Minimized."}))])
    agent = _agent(client, tmp_path)
    agent.dispatcher.backend.hotkey = lambda *k, **kw: StubResult(summary="ok", details={"foreground_changed": True})
    events = list(agent.run("minimize it", effort="low", thinking=False, escalate=("medium", True)))
    assert events[-1].text == "Minimized." and len(client.calls) == 1
    assert agent.last_summary["escalated"] is None


def test_a_claim_about_a_window_needs_a_look_first(tmp_path: Path) -> None:
    """The 28 Sept bug: "It's already full screen" with no tool call at all in that request."""
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="done",
                         input={"message": "It's already full screen.", "about": [0x5050]})),
        _response(_block("tool_use", id="t2", name="look_at_window", input={"hwnd": 0x5050})),
        _response(_block("tool_use", id="t3", name="done", input={"message": "It's maximized.", "about": [0x5050]})),
    ])
    events = list(_agent(client, tmp_path).run("make it full screen", effort="low", thinking=False,
                                               escalate=("medium", True)))
    assert events[-1].text == "It's maximized." and len(client.calls) == 3
    assert client.calls[1]["thinking"]["type"] == "adaptive"   # the held claim escalated the request


def test_a_second_claim_without_evidence_is_accepted_the_check_never_loops(tmp_path: Path) -> None:
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="done", input={"message": "It's open.", "about": [7]})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "It's open.", "about": [7]})),
    ])
    events = list(_agent(client, tmp_path).run("is it open?"))
    assert events[-1].text == "It's open." and len(client.calls) == 2


def test_step_lines_do_not_defeat_the_loop_breaker(tmp_path: Path) -> None:
    from yuki.agent.loop import acting_input

    assert acting_input({"say_now": "Trying F11.", "keys": ["f11"]}) == {"keys": ["f11"]}
    assert acting_input({"keys": ["f11"]}) == {"keys": ["f11"]}


# -- step lines: said into a noticeable silence, while the step runs, once ----------------------------


def _steps(state: dict[str, Any], said: list[str]) -> Any:
    from yuki.voice.steplines import StepLines

    async def speak(text: str) -> None:
        said.append(text)

    return StepLines(state=lambda: state, speak=speak, silence_s=0.2, after_s=0.1, long_silence_s=5.0)


def test_step_line_waits_for_silence_and_the_step_to_have_run() -> None:
    async def main() -> None:
        state = {"quiet_for": 0.0}
        said: list[str] = []
        steps = _steps(state, said)
        steps.step_started(1, 1)
        steps.offer(1, "Opening Spotify.", seq=1)
        await asyncio.sleep(0.15)
        assert said == []                      # someone spoke a moment ago
        state["quiet_for"] = 1.0
        await asyncio.sleep(0.25)
        assert said == ["Opening Spotify."]
        steps.step_started(1, 2)
        steps.offer(1, "Opening Spotify.", seq=2)   # the same words again: never twice
        await asyncio.sleep(0.25)
        assert said == ["Opening Spotify."]
        await steps.stop()

    asyncio.run(main())


def test_a_step_line_is_never_said_once_its_step_returned_or_its_job_closed() -> None:
    async def main() -> None:
        state = {"quiet_for": 1.0}
        said: list[str] = []
        steps = _steps(state, said)
        steps.step_started(1, 1)
        steps.offer(1, "Switching tabs.", seq=1)
        steps.step_ended(1, 1)                 # a quick step that worked, over before a pause was noticed
        state["quiet_for"] = 6.0               # a long silence after it: the line would be stale now (v3.14b)
        steps.step_started(2, 1)
        steps.offer(2, "Pressing F11.", seq=1)
        steps.step_ended(2, 1, ok=False)       # it failed: its line is past
        steps.step_started(3, 1)
        steps.offer(3, "Reading the page.", seq=1)
        steps.step_started(3, 2)               # the job moved on to its next step
        steps.step_started(4, 1)
        steps.offer(4, "Looking it up.", seq=1)
        steps.closed(4)                        # its outcome is known
        await asyncio.sleep(0.3)
        assert said == []
        steps.step_started(5, 1)
        steps.offer(5, "Opening Warp.", seq=1)  # a slow step still running in the long silence: said
        await asyncio.sleep(0.25)
        assert said == ["Opening Warp."]
        await steps.stop()

    asyncio.run(main())


def test_no_step_line_over_the_user_or_in_a_call_but_a_say_line_waits_only_for_the_user() -> None:
    async def main() -> None:
        state = {"quiet_for": 10.0, "user_speaking": True, "call_mode": True}
        said: list[str] = []
        steps = _steps(state, said)
        steps.step_started(1, 1)
        steps.offer(1, "Opening it.", seq=1)
        steps.offer(1, "Found three of them so far.", kind="say", seq=1)
        await asyncio.sleep(0.2)
        assert said == []
        state["user_speaking"] = False
        await asyncio.sleep(0.25)
        assert said == ["Found three of them so far."]   # a call: no step lines, the say line goes out
        await steps.stop()

    asyncio.run(main())


# -- the talker's click hand: by the read it was shown, found again or refused ------------------------


def _youtube() -> Any:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import voice_scenarios as vs

    return vs._youtube_desk(vs.Timeline())


def test_items_line_lists_page_content_first_and_click_is_verified_by_the_page_changing() -> None:
    from yuki.voice.hands import Hands

    desk = _youtube()
    hands = Hands(desk)
    hands.snapshot()
    hands._read_items(desk.CHROME, desk.title(desk.CHROME))
    snap = hands.snapshot()
    assert "items on the screen" in snap.text and "Make Up My Mind" in snap.text
    number = next(n for n, i in enumerate(hands.items.items, 1) if "Make Up My Mind" in i["name"])
    ok, report, facts = hands._run("click", {"window": desk.CHROME, "item": number, "say_when_done": ""})
    assert ok and facts["verified"] is True and desk.playing
    assert desk.tabs[desk.selected]["page"][0] == "watch"


def test_click_on_a_changed_window_finds_the_item_again_or_refuses_by_code() -> None:
    from yuki.voice.hands import Hands

    desk = _youtube()
    hands = Hands(desk)
    hands.snapshot()
    hands._read_items(desk.CHROME, desk.title(desk.CHROME))
    number = next(n for n, i in enumerate(hands.items.items, 1) if "lofi" in i["name"])
    hands.items.at -= 60.0                            # an old read
    desk.tabs[desk.selected]["page"] = ("watch", "Joji - Glimpse of Us (Official Video)")   # the page changed
    desk.tabs[desk.selected]["name"] = "Joji - Glimpse of Us (Official Video) - YouTube"
    ok, report, facts = hands._run("click", {"window": desk.CHROME, "item": number, "say_when_done": ""})
    assert ok and facts["verified"] is True           # the same item, found again in the watch page's list
    hands.items.at -= 60.0
    number, gone = next((n, i) for n, i in enumerate(hands.items.items, 1) if i["role"] == "Hyperlink")
    assert hands.items.items[0]["role"] != "TabItem"  # page content first, the window's own tabs after it
    desk.tabs[desk.selected]["page"] = ("mail", "")   # nothing of it is on the screen now
    desk.tabs[desk.selected]["name"] = "Inbox - Mail"
    ok, report, facts = hands._run("click", {"window": desk.CHROME, "item": number, "say_when_done": ""})
    assert not ok and facts.get("stale") and "not on it" in report and gone["name"][:20] in report


def test_type_hand_needs_a_text_field_with_the_keyboard_and_reads_it_back() -> None:
    from yuki.voice.hands import Hands

    desk = BrowserDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()
    ok, report, facts = hands._run("type", {"window": desk.CHROME, "text": "hi", "say_when_done": ""})
    assert not ok and facts.get("stale")               # no field has the keyboard: the thinker gets it
    desk.focus_field = {"window": desk.CHROME, "role": "Edit", "name": "Search", "text": ""}
    assert "focus: the text field Edit \"Search\"" in hands.snapshot().text
    ok, report, facts = hands._run("type", {"window": desk.CHROME, "text": "hi", "say_when_done": ""})
    assert ok and facts["verified"] is True and desk.focus_field["text"] == "hi"


# -- the gate: a ready result waits only for speech ------------------------------------------------------


def test_a_ready_result_is_released_right_after_yuki_stops_speaking() -> None:
    from yuki.voice.narrator import Item, NarratorGate

    async def main() -> None:
        state = {"user_turns": 0, "last_bot_stop": time.monotonic(), "jobs_open": 1}
        got: list[str] = []
        gate = NarratorGate(lambda: state, cooldown_s=1.5, final_gap_s=0.3, settle_s=1.5, attention=None)
        gate.start()

        async def deliver(run_llm: bool, stale: bool) -> None:
            got.append("result")

        gate.enqueue(Item(job=1, kind="final", deliver=deliver))
        await asyncio.sleep(0.1)
        assert got == ["result"]   # no 0.3 s gap, no settle for the other job still running
        await gate.stop()

    asyncio.run(main())


# -- the voice cache: her own audio for the same short words, and the check's own SAY remembered --------------


def test_a_short_line_said_again_comes_from_the_cache_without_a_request(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_voice_gemini import FakeServers, _levels
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice.gemini_tts import service_class
    from yuki.voice.pipeline import _clause_tts
    from yuki.voice.ttscache import TTSCache

    class Guard:   # the spoken-line check: SAY by the model the first time, and nothing asked when known
        timeout_s = 2.0

        def __init__(self) -> None:
            self.asked: list[tuple[str, bool]] = []

        def start(self, text: str, *, first: bool = False, known_ok: bool = False) -> Any:
            from yuki.voice.speechguard import Verdict

            self.asked.append((text, known_ok))
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(Verdict(True, by="free" if known_ok else "model"))
            return fut

        async def decide(self, fut: Any, text: str, *, wait_s: float | None = None) -> Any:
            return fut.result()

    async def main() -> None:
        async with FakeServers() as fake:
            cache = TTSCache(tmp_path / "cache")
            guard = Guard()
            for _ in range(2):
                tts = _clause_tts(service_class())(
                    api_key="fake-key", model="m", voice="Leda", base_url=f"{fake.url}/v1beta", fallback_key=None,
                    first_audio_timeout_s=3.0, pause_frame_processing=False, cache=cache)
                tts.set_guard(guard)
                frames = [LLMFullResponseStartFrame(), LLMTextFrame("On it."), LLMFullResponseEndFrame(), SleepFrame(0.8)]
                down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
                assert _levels(down)   # it was heard both times
            assert [p["text"] for p in fake.requests] == ["On it."]   # one request: the second came from disk
            assert guard.asked == [("On it.", False), ("On it.", True)]   # the check's SAY was remembered
            assert cache.stats["hits"] == 1

    asyncio.run(main())



def test_the_voice_cache_never_makes_the_loop_wait_for_its_disk(tmp_path: Path) -> None:
    """``approved`` / ``approve`` run on the voice loop: a ``put`` writing on a slow disk must not hold them."""
    import threading
    import time

    from yuki.voice.ttscache import TTSCache

    cache = TTSCache(tmp_path / "cache")
    cache.preload()
    key = cache.key("gemini", "m", "Leda", None, "On it.")
    with cache._io:   # the disk busy (a put or flush in progress on its worker)
        done = threading.Event()
        threading.Thread(target=lambda: (cache.approve(key), cache.approved(key), done.set()), daemon=True).start()
        t0 = time.monotonic()
        assert done.wait(1.0) and time.monotonic() - t0 < 0.5
    assert cache.approved(key)
    cache.put(key, b"\x00\x01" * 100)   # written with its audio
    again = TTSCache(tmp_path / "cache")
    again.preload()
    assert again.approved(key) and again.get(key) == b"\x00\x01" * 100

def test_a_slow_read_is_left_when_the_user_says_more_and_their_words_go_in_at_once(tmp_path: Path) -> None:
    import threading

    client = FakeClient([
        _response(_block("tool_use", id="t1", name="look_at_window", input={"hwnd": 1})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "The other one it is.", "about": []})),
    ])
    desk = StubDesktop(action_delay_s=0)
    real = desk.get_window_tree

    def slow(hwnd: int, **kw: Any) -> Any:
        time.sleep(3.0)
        return real(hwnd, **kw)

    desk.get_window_tree = slow
    agent = _agent(client, tmp_path, backend=desk)
    threading.Timer(0.3, lambda: agent.steer("no, the other one")).start()
    started = time.monotonic()
    events = list(agent.run("click the mix"))
    assert time.monotonic() - started < 2.0          # not the 3 s read
    assert events[-1].text == "The other one it is."
    sent = json_text(client.calls[1]["messages"][-1])
    assert "no, the other one" in sent and "Not finished" in sent


def json_text(message: dict) -> str:
    import json

    return json.dumps(message.get("content"), default=str)


# -- a question the user went past never holds the worker ---------------------------------------------


def test_new_work_for_the_worker_passes_over_a_question_nobody_answered() -> None:
    from test_voice_v39 import Params, _bridge, _settle

    async def main() -> None:
        rt, bridge = _bridge()
        first = Params("Play the five point five video", call_id="c1", acts=True)
        t1 = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        job_a = rt.submitted[0][0]
        rt.started.emit("worker", job_a, "x")
        rt.asked.emit("worker", job_a, "The Sonnet video, or the mix?")
        await _settle()
        second = Params("Open WhatsApp", call_id="c2", heard="open whatsapp", acts=True)
        t2 = asyncio.create_task(bridge.tell_yuki(second))
        await _settle()
        assert rt.withdrawals == [job_a]                  # the question is passed over, its job withdrawn
        assert rt.submitted[1][2].get("lane") == "worker"
        rt.withdrawn.emit("worker", job_a, "Withdrawn: replaced by the user's newer request.")
        rt.lane_done.emit("worker", job_a, "x", False, [])
        await _settle()
        await asyncio.wait_for(t1, 1)
        assert "withdrawn" in first.results[-1][0]
        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


# -- a "still on it" reply whose job finished meanwhile is never heard -----------------------------------


def test_a_still_on_it_reply_is_dropped_once_its_job_has_finished(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_voice_gemini import FakeServers, _levels
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice.bridge import Job, VoiceBridge
    from yuki.voice.gemini_tts import service_class
    from yuki.voice.pipeline import _clause_tts

    bridge = VoiceBridge(SimpleNamespace(), foreground=lambda: 42)
    bridge.user_turn = lambda: False
    job = Job(job_id=1, request="switch tabs", source="voice", state="running")

    async def main() -> None:
        async with FakeServers() as fake:
            heard = []
            for finished in (False, True):
                bridge._waiting_run = (job, time.monotonic())   # the gate handed the talker a "still on it" line
                job.closing = ("done", "Switched.") if finished else None
                tts = _clause_tts(service_class())(
                    api_key="fake-key", model="m", voice="Leda", base_url=f"{fake.url}/v1beta", fallback_key=None,
                    first_audio_timeout_s=3.0, pause_frame_processing=False)
                tts._yuki_stale = bridge.waiting_reply_stale
                frames = [LLMFullResponseStartFrame(), LLMTextFrame("Still looking for it."),
                          LLMFullResponseEndFrame(), SleepFrame(0.8)]
                down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
                heard.append(bool(_levels(down)))
            assert heard == [True, False]           # said while it ran; dropped once its outcome was known
            assert bridge._waiting_run is None      # asked once per line handed over

    asyncio.run(main())


# -- v3.14b: text from outside never breaks the console log ------------------------------------------


def test_the_log_prints_model_and_tool_text_with_brackets_literally(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    from yuki.ui.uilog import UiLog

    out = io.StringIO()
    logger = SessionLogger(tmp_path, console=Console(file=out, soft_wrap=True, width=200), session_id="t")
    stray = "the [/cyan] tag and [bold]x[/] and a lone [ bracket"
    logger.thinking(stray)          # raised rich's MarkupError out of the agent loop before (2026-09-29)
    logger.tool_call("click", {"named": stray})
    logger.tool_result("click", ok=True, summary=stray, result={}, elapsed_ms=1.0)
    logger.ask_user(stray)
    logger.final(stray)
    logger.error(stray)
    text = out.getvalue()
    assert text.count("[/cyan]") >= 4 and "lone [ bracket" in text   # printed as the words they are
    ui = UiLog(sessions_dir=tmp_path, session_id=None)
    ui.logger.console = Console(file=io.StringIO(), soft_wrap=True)
    ui.event("submit", request=stray)   # the tray's own log line, the user's words in it


# -- v3.14b: an outcome with nothing done is never said as done ---------------------------------------


def test_a_result_with_no_action_for_a_request_that_asked_for_one_says_nothing_was_done() -> None:
    from test_voice_v39 import Params, _bridge, _settle

    async def main() -> None:
        for acted in (False, True):
            rt, bridge = _bridge()
            params = Params("Ok on WhatsApp", call_id=f"c{acted}", heard="ok on whatsapp", acts=True)
            task = asyncio.create_task(bridge.tell_yuki(params))
            await _settle()
            job = rt.submitted[0][0]
            rt.started.emit("worker", job, "x")
            if acted:
                rt.tool_called.emit("worker", job, "launch_app", {"query": "WhatsApp"})
                rt.tool_finished.emit("worker", job, "launch_app", True, "launched WhatsApp")
            rt.finished.emit("worker", job, "Got it.")
            rt.lane_done.emit("worker", job, "x", False, [])
            await _settle(20)
            await asyncio.wait_for(task, 2)
            result = params.results[-1][0]
            assert result.get("result") == "Got it."
            assert bool(result.get("nothing_done")) is (not acted)   # from the job's record, not the words

    asyncio.run(main())


# -- v3.14b: a question nobody answers never holds its lane --------------------------------------------


def test_an_unanswered_question_is_set_aside_and_an_answer_later_carries_it_on(tmp_path: Path) -> None:
    import test_voice_v39  # noqa: F401 - the Qt application

    from yuki.log.events import AskUser, ErrorEvent, Final
    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    class Asker:
        tool_names = None
        last_summary = None

        def __init__(self) -> None:
            self.cancelled, self.cancel_reason, self.awaiting_answer = False, "cancelled", False
            self.runs: list[str] = []
            self.answers: list[str] = []

        def run(self, request: str, **_: Any) -> Any:
            self.runs.append(request)
            self.cancelled = False
            if len(self.runs) == 1:
                self.awaiting_answer = True
                yield AskUser("Which video?")
                self.awaiting_answer = False
                if self.cancelled:
                    yield ErrorEvent("Parked: the question stays open until the user answers it.")
                    return
            yield Final("Playing it.")

        def answer(self, text: str) -> None:
            self.answers.append(text)

        def cancel(self, reason: str = "cancelled") -> None:
            self.cancelled, self.cancel_reason = True, reason

        def take_steers(self) -> list:
            return []

    agent = Asker()
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", ask_park_s=0.3),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=Asker(), memory=SimpleNamespace(session_id="t"))
    seen: list[tuple] = []
    rt.parked.connect(lambda lane, job, text: seen.append(("parked", job)))
    rt.failed.connect(lambda lane, job, text: seen.append(("failed", job)))
    rt.finished.connect(lambda lane, job, text: seen.append(("finished", job)))
    rt.resumed.connect(lambda old, new: seen.append(("resumed", old, new)))
    rt.worker.enqueue(Job(1, "play the five point five video", None, "text"))
    rt.worker._jobs.put(None)
    started = time.monotonic()
    rt.worker.run()                                   # asks; nobody answers; set aside, and the lane is free
    assert seen == [("parked", 1)] and time.monotonic() - started < 3.0
    assert agent.answers[0].startswith("(no answer yet")
    rt.answer("worker", "the Sonnet one")             # answered later: it carries on as a new request
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert seen[1][0] == "resumed" and seen[1][1] == 1 and seen[-1] == ("finished", seen[1][2])
    assert "the Sonnet one" in agent.runs[-1] and "Which video?" in agent.runs[-1]


def test_a_step_line_whose_step_returned_before_its_audio_is_dropped_unheard() -> None:
    async def main() -> None:
        state = {"quiet_for": 6.0}
        said: list[str] = []
        steps = _steps(state, said)
        steps.step_started(1, 1)
        steps.offer(1, "Switching to that tab.", seq=1)
        await asyncio.sleep(0.25)
        assert said == ["Switching to that tab."]      # sent to the voice while the step ran
        steps.step_ended(1, 1)                          # the step returned before the line's first audio
        assert steps.line_stale("Switching to that tab.") is True   # the voice drops it unheard
        assert steps.line_stale("Switching to that tab.") is False  # asked once per line
        steps.step_started(3, 1)
        steps.offer(3, "Reading the page.", seq=1)
        await asyncio.sleep(0.25)
        steps.step_ended(3, 1)
        assert steps.line_stale("Sure, here's the answer.") is False  # another reply is never taken for the line
        steps.step_started(2, 1)
        steps.offer(2, "Opening Warp.", seq=1)
        await asyncio.sleep(0.25)
        assert steps.line_stale() is False              # still running when its audio starts: heard
        await steps.stop()

    asyncio.run(main())
