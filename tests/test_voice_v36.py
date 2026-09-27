"""Voice v3.6 (2026-09-27, after the user's 04:45-04:48 test): offline checks.

The wake word's end (the name never reaches speech recognition, the words after it are replayed), the early
hand-off to the thinker with one desktop actor, honest promises on doubtful words, and the narrator (no step
line after the job is past it, the thinker's finishing line said once by code, nothing said while words are
on their way). No microphone, no speakers, no network: synthetic audio, a fake runtime, stand-in services.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])
DATA = Path(__file__).parent / "data" / "voice"


# -- shared stand-ins ------------------------------------------------------------------------------

class FakeRuntime(QObject):
    """The runtime's signals and methods the bridge uses (tool_finished included)."""

    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 1
        self.submitted: list[tuple[int, str]] = []
        self.steers: list[tuple[int, str]] = []
        self.cancels: list[int] = []

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text") -> tuple[str, int]:
        job_id, self.next_id = self.next_id, self.next_id + 1
        self.submitted.append((job_id, text))
        return "worker", job_id

    def steer(self, job_id: int, text: str) -> bool:
        self.steers.append((job_id, text))
        return True

    def answer(self, lane: str, text: str) -> None:
        pass

    def cancel(self, job_id: int) -> str:
        self.cancels.append(job_id)
        return "running"


class Params:
    """A Pipecat function call's params for the bridge's handlers."""

    def __init__(self, request: str = "Open Bluetooth settings", call_id: str = "call-1", intent: str = "new_task",
                 heard: str = "open bluetooth settings", context: str | None = None) -> None:
        self.arguments = {"say_first": "On it.", "request": request, "intent": intent,
                          **({"context": context} if context else {})}
        self.tool_call_id = call_id
        self.function_name = "tell_yuki"
        self.context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": heard}])
        self.results: list[tuple[dict, Any]] = []
        self.pushed: list[Any] = []
        self.llm = SimpleNamespace(push_frame=self._push)

    async def _push(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


async def _settle(n: int = 6) -> None:
    for _ in range(n):
        APP.processEvents()
        await asyncio.sleep(0)


def _bridge(rt: FakeRuntime | None = None, **kw: Any) -> Any:
    from yuki.voice.bridge import VoiceBridge

    rt = rt or FakeRuntime()
    bridge = VoiceBridge(rt, foreground=lambda: None, **kw)
    bridge.attach(asyncio.get_running_loop())
    return rt, bridge


# -- B. the wake word: where the words after the name begin ----------------------------------------------

def test_spotter_reports_where_the_words_after_the_name_begin() -> None:
    """On a one-breath "Yuki, pause the music." the spotter's own token times put the cut right after the name."""
    from yuki.voice.transport import load_clip
    from yuki.voice.wake import MODELS, MODELS_DIR, DEFAULT_MODEL, WakeSpotter

    if not (MODELS[DEFAULT_MODEL].path(MODELS_DIR) / MODELS[DEFAULT_MODEL].encoder).exists():
        pytest.skip("wake-word model not downloaded")
    clip = load_clip(DATA / "one_breath_pause.wav")
    lead = 0.6
    audio = np.concatenate([np.zeros(int(16000 * lead), np.float32), clip, np.zeros(16000, np.float32)])
    spotter = WakeSpotter()
    t0 = 1000.0  # a stand-in monotonic clock for the batches
    hit_at = None
    for i in range(0, len(audio), 1600):
        if spotter.feed(audio[i:i + 1600], t0 + i / 16000):
            hit_at = t0 + (i + 1600) / 16000
            break
    assert hit_at is not None and spotter.name_end is not None
    into_clip = spotter.name_end - t0 - lead
    # The name ("Yuki,") is about half a second of this clip; the words after it start there, before the hit.
    assert 0.3 < into_clip < 0.8, into_clip
    assert spotter.name_end < hit_at


class _Mic:
    """A shared mic stand-in: subscribers only (the listener feeds itself from them)."""

    def __init__(self) -> None:
        self.subs: dict[int, Any] = {}

    def subscribe(self, fn: Any) -> int:
        self.subs[len(self.subs) + 1] = fn
        return len(self.subs)

    def unsubscribe(self, token: int) -> None:
        self.subs.pop(token, None)

    def push(self, samples: np.ndarray, at: float) -> None:
        pcm = (samples * 32767).astype("<i2").tobytes()
        for fn in list(self.subs.values()):
            fn(pcm, at)


def test_listener_hands_on_the_name_end_with_the_wake() -> None:
    from yuki.voice.wake import WakeListener

    class Spotter:
        model = "stub"
        near_misses = 0

        def __init__(self) -> None:
            self.batches = 0
            self.name_end: float | None = None

        def feed(self, samples: np.ndarray, at: float | None = None) -> str | None:
            self.batches += 1
            if self.batches == 3:
                self.name_end = at + 0.05
                return "YUKI"
            return None

        def reset(self) -> None:
            pass

    mic, woke = _Mic(), []
    listener = WakeListener(mic, Spotter, on_wake=lambda at, kw: woke.append((at, listener.name_end)))
    listener.start()
    assert listener.ready.wait(2)
    loud = (np.sin(np.arange(320) / 3.0) * 0.3).astype(np.float32)  # a 20 ms block well above the floor
    at = 50.0
    deadline = time.monotonic() + 3
    while not woke and time.monotonic() < deadline:
        mic.push(loud, at)
        at += 0.02
        time.sleep(0.002)
    listener.stop()
    assert woke, "no wake"
    batch_at, name_end = woke[0]
    assert name_end is not None and abs(name_end - (batch_at + 0.05)) < 1e-6


def _session(tmp: Path) -> Any:
    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "r.csv", voice_wake=False)
    return VoiceSession(settings, QObject(), None, prefs_path=tmp / "voice.json")


def test_conversation_replays_only_what_came_after_the_name(tmp_path: Path) -> None:
    """With the name's end known, speech recognition gets the audio from there on (the name is not in it)."""
    from yuki.voice.audio import SharedMic
    from yuki.voice.session import WAKE_PREROLL_S

    session = _session(tmp_path)
    seen: list[tuple] = []

    class _Thread:
        def __init__(self, target: Any, args: tuple, **_: Any) -> None:
            seen.append(args)

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    import yuki.voice.session as session_module

    real = session_module.threading.Thread
    session_module.threading.Thread = _Thread
    try:
        session.start_conversation("wake", wake_at=10.0, heard_at=10.4, name_end=9.7)
        session.start_conversation("wake", wake_at=20.0, heard_at=20.4)  # the spotter could not tell
    finally:
        session_module.threading.Thread = real
    (_s1, _v1, listen1, _st1, _c1, cut1), (_s2, _v2, listen2, _st2, _c2, cut2) = seen
    assert listen1 == 9.7 and cut1 is True
    assert listen2 == pytest.approx(20.0 - WAKE_PREROLL_S) and cut2 is False

    # The mic's kept audio from that point: the blocks of the name before it are not replayed.
    mic = SharedMic(None)
    block = np.full(320, 0.1, np.float32)
    for i in range(10):
        mic.feed(block, 9.5 + i * 0.02)  # 9.50 ... 9.68 is the name, 9.70 on the words after it
    for i in range(10):
        mic.feed(block, 9.7 + i * 0.02)
    _token, kept = mic.subscribe_from(lambda *_: None, 9.7)
    assert len(kept) == 10 and min(at for _pcm, at in kept) >= 9.7 - 1e-9


def test_first_words_after_the_name_are_marked_for_the_mouth() -> None:
    from yuki.voice.mouth import volatile_block

    assert "They said your name, Yuki, right before these words" in volatile_block(named=True)["text"]
    assert "your name" not in volatile_block()["text"]


def test_tls_context_is_built_once_and_used_for_secure_websockets() -> None:
    import pipecat.services.websocket_service as ws

    from yuki.voice.pipeline import shared_ssl_context, use_shared_tls

    got: list[dict] = []
    saved = ws.websocket_connect, getattr(ws, "_yuki_shared_tls", False)
    ws.websocket_connect = lambda uri, **kw: got.append({"uri": uri, **kw})
    ws._yuki_shared_tls = False
    try:
        use_shared_tls()
        ws.websocket_connect("wss://api.deepgram.com/v2/listen", close_timeout=1)
        ws.websocket_connect("ws://localhost:1")
    finally:
        ws.websocket_connect, ws._yuki_shared_tls = saved
    assert got[0]["ssl"] is shared_ssl_context() and shared_ssl_context() is shared_ssl_context()
    assert "ssl" not in got[1]


def test_a_stale_quota_read_never_holds_up_a_start(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    import yuki.voice.pipeline as p

    monkeypatch.setattr(p, "get_key", lambda name: "stand-in")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline test")))
    monkeypatch.setitem(p._TTS_QUOTA, "elevenlabs", (5000, 10000, time.monotonic() - 10 * p.QUOTA_TRUST_S))
    started = time.perf_counter()
    assert p.elevenlabs_quota() == (5000, 10000)
    assert time.perf_counter() - started < 0.05


def test_speaker_that_fails_to_open_is_retried_after_rereading_the_device_list(tmp_path: Path) -> None:
    session = _session(tmp_path)
    opened: list[str] = []
    refreshed: list[bool] = []

    class Speaker:
        def __init__(self, rate: int, device: Any) -> None:
            self.device = device

        def open(self) -> dict:
            opened.append("try")
            if len(opened) == 1:
                raise OSError("Error opening RawOutputStream: Invalid device [PaErrorCode -9996]")
            return {"device": "Headphones", "wanted": self.device, "found": True}

        def close(self) -> None:
            pass

    session.speaker_factory = Speaker
    session._refresh_audio_devices = lambda: refreshed.append(True) or True
    speaker, facts = session._open_output("Headphones")
    assert opened == ["try", "try"] and refreshed == [True] and facts["device"] == "Headphones"


# -- A. the early hand-off: the thinker starts the moment the mouth decides, one actor --------------------

def test_early_hand_off_starts_the_thinker_and_the_call_adopts_it() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        assert bridge.begin_early("call-1", ["open bluetooth settings"])
        await _settle()
        assert len(rt.submitted) == 1 and "open bluetooth settings" in rt.submitted[0][1]
        job = rt.submitted[0][0]
        params = Params(call_id="call-1", context="the Settings window the user has open")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        assert len(rt.submitted) == 1  # adopted, never submitted twice
        assert rt.steers and rt.steers[0][0] == job and "From Yuki's voice" in rt.steers[0][1]
        rt.started.emit("worker", job, "x")
        rt.finished.emit("worker", job, "Bluetooth settings are open.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        assert params.results[-1][0]["result"] == "Bluetooth settings are open."

    asyncio.run(main())


def test_early_start_is_void_when_its_call_never_comes() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.begin_early("call-9", ["open spotify"])
        await _settle()
        job = rt.submitted[0][0]
        bridge.settle_early({"call-other"})  # the reply's calls were complete, and it was not among them
        await _settle()
        assert rt.cancels == [job]
        bridge.begin_early("call-10", ["open chrome"])
        await _settle()
        bridge.drop_early()  # the reply was cut by a barge-in before the call was written out
        await _settle()
        assert rt.cancels == [job, rt.submitted[1][0]]

    asyncio.run(main())


def test_one_actor_the_talkers_window_hands_wait_while_the_thinker_runs() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import StubDesktop

    async def main() -> None:
        rt, bridge = _bridge()
        bridge.begin_early("call-1", ["open bluetooth settings"])
        await _settle()
        rt.started.emit("worker", rt.submitted[0][0], "x")
        await _settle()
        assert bridge.desk_job() is not None
        hands = Hands(StubDesktop(action_delay_s=0))
        hands.bridge = bridge
        ok, report, _facts = hands._run("window", {"window": 1, "action": "minimize"})
        assert not ok and "thinker is working on the desktop" in report
        ok, _report, _facts = hands._run("media_key", {"key": "playpause"})  # sound touches no window
        assert ok

    asyncio.run(main())


def test_no_early_start_on_doubtful_words_or_while_a_task_is_open(tmp_path: Path) -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    calls: list[str] = []
    bridge = SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None,
                             begin_early=lambda cid, heard: calls.append(cid) or True)
    conv = Conversation(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), bridge=bridge,
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=None,
                        speech_end=lambda: None, on_state=lambda *a: None, session="t")
    conv.observer.heard = "unsure of 'hemanth' (0.41)"
    conv._early_handoff("a")
    conv.observer.heard = None
    bridge.has_open_voice_job = lambda: True
    conv._early_handoff("b")
    bridge.has_open_voice_job = lambda: False
    conv._early_handoff("c")
    assert calls == ["c"]


def test_the_thinker_is_asked_to_act_first_on_a_plain_target() -> None:
    from yuki.voice.mouth import brain_request

    text = brain_request("Open Bluetooth settings", ["open bluetooth settings"])
    assert "take that action in your first reply" in text and "looking at a window first" in text


# -- D. the narrator ---------------------------------------------------------------------------------------

class _State:
    def __init__(self, **over: Any) -> None:
        self.s = {"user_speaking": False, "bot_speaking": False, "generating": False, "owes_reply": False,
                  "busy_tools": 0, "last_bot_stop": 0.0, "user_turns": 0, "jobs_open": 0, **over}

    def __call__(self) -> dict:
        return dict(self.s)


def _gate(state: _State, **kw: Any) -> Any:
    from yuki.voice.narrator import NarratorGate

    return NarratorGate(state, settle_s=0.02, settle_max_s=0.2, cooldown_s=0.02, stale_turns=3,
                        final_gap_s=0.0, **kw)


def test_action_steps_are_never_narrated_and_passed_steps_are_dropped() -> None:
    async def main() -> None:
        rt, bridge = _bridge(settings=Settings(voice_progress_lines=2))
        state = _State(last_bot_stop=time.monotonic())  # Yuki just spoke: progress waits for a real pause
        gate = _gate(state, progress_pause_s=30.0)
        bridge.gate = gate
        params = Params()
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "look_at_window", {"hwnd": 5})
        await _settle()
        assert [i.kind for i in gate.pending] == ["step"]  # waiting for a pause in Yuki's speech
        rt.tool_called.emit("worker", job, "open_url", {"url": "ms-settings:bluetooth"})
        await _settle()
        assert gate.pending == []  # past the looking step; and an action is never a progress line
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_the_same_step_again_keeps_its_waiting_line() -> None:
    async def main() -> None:
        rt, bridge = _bridge(settings=Settings(voice_progress_lines=2))
        gate = _gate(_State(last_bot_stop=time.monotonic()), progress_pause_s=30.0)
        bridge.gate = gate
        task = asyncio.create_task(bridge.tell_yuki(Params("What have I done today?")))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "activity", {"since": "2026-09-27"})
        rt.tool_called.emit("worker", job, "activity", {"since": "2026-09-26"})
        await _settle()
        assert [i.kind for i in gate.pending] == ["step"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_the_thinkers_finishing_line_is_said_once_by_code() -> None:
    from pipecat.frames.frames import TTSSpeakFrame

    async def main() -> None:
        rt, bridge = _bridge()
        params = Params()
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.tool_called.emit("worker", job, "open_url", {"url": "ms-settings:bluetooth",
                                                        "finishes_with": "Bluetooth settings are open."})
        rt.tool_finished.emit("worker", job, "open_url", True, "opened ms-settings:bluetooth")
        rt.finished.emit("worker", job, "Bluetooth settings are open.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        spoken = [f.text for f in params.pushed if isinstance(f, TTSSpeakFrame)]
        assert spoken == ["Bluetooth settings are open."]
        result, props = params.results[-1]
        assert result["said"] is True and props.run_llm is False  # no mouth run: nothing to reword or repeat

    asyncio.run(main())


def test_a_final_that_is_not_the_actions_line_still_goes_to_the_mouth() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        params = Params()
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.tool_called.emit("worker", job, "open_url", {"url": "x", "finishes_with": "It's open."})
        rt.tool_finished.emit("worker", job, "open_url", False, "could not open it")  # failed: no finish
        rt.finished.emit("worker", job, "I couldn't open it.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        result, props = params.results[-1]
        assert result["result"] == "I couldn't open it." and "said" not in result and props.run_llm is True

    asyncio.run(main())


def test_gate_runs_the_mouth_for_the_last_item_that_does_not_speak_itself() -> None:
    from yuki.voice.narrator import Item

    async def main() -> None:
        got: list[tuple[str, bool]] = []
        gate = _gate(_State())
        gate.start()

        def deliver(name: str) -> Any:
            async def d(run_llm: bool, stale: bool) -> None:
                got.append((name, run_llm))
            return d

        gate.enqueue(Item(job=1, kind="final", deliver=deliver("mouth")))
        gate.enqueue(Item(job=2, kind="final", deliver=deliver("said"), self_spoken=True))
        await asyncio.sleep(0.2)
        await gate.stop()
        assert sorted(got) == [("mouth", True), ("said", False)]

    asyncio.run(main())


def test_gate_waits_while_words_are_on_their_way_to_the_voice() -> None:
    from yuki.voice.narrator import Item

    async def main() -> None:
        got: list[str] = []
        state = _State(speech_pending=True)
        gate = _gate(state)
        gate.start()

        async def d(run_llm: bool, stale: bool) -> None:
            got.append("result")

        gate.enqueue(Item(job=1, kind="final", deliver=d))
        await asyncio.sleep(0.15)
        assert got == []  # Yuki is about to speak: not now
        state.s["speech_pending"] = False
        await asyncio.sleep(0.3)
        await gate.stop()
        assert got == ["result"]

    asyncio.run(main())


def test_observer_counts_words_on_their_way_until_yuki_speaks(tmp_path: Path) -> None:
    from pipecat.frames.frames import BotStartedSpeakingFrame, LLMTextFrame

    from yuki.voice.pipeline import TurnObserver

    obs = TurnObserver(settings=Settings(log_dir=tmp_path / "s"), log=None, memory=None, session="t",
                       speech_end=lambda: None, on_state=lambda *a: None, tts_provider="deepgram",
                       activity=lambda: None)
    obs.on_frame(LLMTextFrame("Opening Bluetooth settings for you."))
    assert obs.live_state()["speech_pending"] is True
    obs.on_frame(BotStartedSpeakingFrame())
    assert obs.live_state()["speech_pending"] is False


# -- C. honest promises ------------------------------------------------------------------------------------

class _SpecGate:
    is_speculating = False


class _Base:
    """Pipecat's LLM service, as far as the layered service's overrides use it."""

    def __init__(self, *a: Any, **k: Any) -> None:
        self._speculation_gate = _SpecGate()
        self.ran: list[Any] = []
        self.pushed: list[Any] = []

    async def run_function_calls(self, calls: Any) -> None:
        self.ran.append(list(calls))

    async def push_frame(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    def create_task(self, coro: Any, name: str = "") -> Any:
        return asyncio.ensure_future(coro)


def _service(hold: bool) -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    events: list[str] = []
    hooks = SimpleNamespace(speculation=lambda *a: None, would_hold=lambda name: hold,
                            words_dropped=lambda n: events.append(f"dropped {n}"), calls_made=lambda ids: None,
                            early_withdrawn=lambda: None, handoff_began=lambda cid: None)
    llm = _layered_service(_Base, MouthMemory(), hooks)()
    llm.events = events
    return llm


def test_a_doubtful_turns_words_are_never_said_with_a_held_action() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame

    async def main() -> None:
        llm = _service(hold=True)
        llm._yuki_words = []  # a turn whose words recognition was unsure of
        await llm.push_frame(LLMTextFrame("I think you meant Yuki, yeah? Settings coming up."))
        assert llm.pushed == []  # held while the reply is judged
        await llm.run_function_calls([SimpleNamespace(function_name="open_app", tool_call_id="c1")])
        await llm.push_frame(LLMFullResponseEndFrame())
        assert not any(isinstance(f, LLMTextFrame) for f in llm.pushed)  # the promise was never spoken
        assert llm.events == ["dropped 1"] and "c1" in llm._yuki_unspoken  # and never put back in the context

    asyncio.run(main())


def test_a_doubtful_turns_words_are_said_when_nothing_is_held() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame

    async def main() -> None:
        llm = _service(hold=False)
        llm._yuki_words = []
        await llm.push_frame(LLMTextFrame("The Claude tabs, three and four?"))
        await llm.push_frame(LLMFullResponseEndFrame())  # a question alone: said at the reply's end
        texts = [f.text for f in llm.pushed if isinstance(f, LLMTextFrame)]
        assert texts == ["The Claude tabs, three and four?"]
        assert isinstance(llm.pushed[-1], LLMFullResponseEndFrame)

    asyncio.run(main())


def test_yukis_own_name_is_never_a_doubtful_word_and_the_cut_first_word_is_forgiven(tmp_path: Path) -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation, heard_note

    conv = Conversation(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                        bridge=SimpleNamespace(), transport=None, memory=None, mouth_memory=MouthMemory(), log=None,
                        speech_end=lambda: None, on_state=lambda *a: None, session="t", keyterms=["Hemanth"])
    assert "Yuki" not in conv.observer.keyterms and "Hemanth" in conv.observer.keyterms
    words = [{"word": "yucky", "confidence": 0.95}, {"word": "open", "confidence": 0.97},
             {"word": "system", "confidence": 0.93}, {"word": "setting", "confidence": 0.9}]
    assert heard_note(words, conv.observer.keyterms)[0] is None
    cut = [{"word": "pause", "confidence": 0.55}, {"word": "the", "confidence": 0.99},
           {"word": "music", "confidence": 0.98}]
    assert heard_note(cut, [], cut_before=True)[0] is None  # the cut, not the word
    assert "pause" in (heard_note(cut, [])[0] or "")
    late = cut[:2] + [{"word": "moosic", "confidence": 0.5}]
    assert "moosic" in (heard_note(late, [], cut_before=True)[0] or "")


def test_staying_quiet_on_the_first_words_after_the_name_is_refused_once() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        bridge.user_turn = lambda: True
        bridge.named_turn = lambda: True
        first, second = Params(), Params()
        await bridge.stay_quiet(first)
        await bridge.stay_quiet(second)
        assert "not_quiet" in first.results[0][0]
        assert second.results[0][0] == {"ok": True}  # once: a second one is its considered choice

    asyncio.run(main())


def test_open_url_hand_verifies_a_settings_page_by_its_window() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import StubDesktop, StubResult

    class Desk(StubDesktop):
        def __init__(self, hwnd: Any) -> None:
            super().__init__(action_delay_s=0)
            self.hwnd = hwnd

        def open_url(self, url: str, **kwargs: Any) -> Any:
            result = StubResult(summary=f"opened {url} with the default handler")
            result.details = {"url": url, "handler": "", "hwnd": self.hwnd}
            return result

    ok, _report, facts = Hands(Desk(20485))._run("open_url", {"url": "ms-settings:bluetooth"})
    assert ok and facts["verified"] is True
    ok, _report, facts = Hands(Desk(None))._run("open_url", {"url": "ms-settings:bluetooth"})
    assert ok and facts["verified"] is None  # no window seen: it stays unverified and says so


# -- v3.7: faster replies, one-breath wakes, and three old bugs -------------------------------------------

def test_first_clause_goes_to_the_voice_at_once_and_the_rest_by_sentence() -> None:
    from pipecat.utils.text.base_text_aggregator import AggregationType
    from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

    from yuki.voice.pipeline import clause_aggregator

    async def run(tokens: list[str]) -> list[str]:
        agg = clause_aggregator(SimpleTextAggregator)(aggregation_type=AggregationType.SENTENCE)
        out: list[str] = []
        for token in tokens:
            async for item in agg.aggregate(token):
                out.append(item.text)
        rest = await agg.flush()
        return out + ([f"END {rest.text}"] if rest else [])

    assert asyncio.run(run(["Sure", ", one", " sec", ", I'll", " look", ". Then", " more."])) == [
        "Sure, one sec,", "I'll look.", "END Then more."]
    assert asyncio.run(run(["It's", " 3.5", " degrees", ". Cold."])) == ["It's 3.5 degrees.", "END Cold."]
    assert asyncio.run(run(["Hey", ", how", " are you?"])) == ["END Hey, how are you?"]  # "Hey," alone waits


def test_words_before_a_tool_call_are_said_when_the_call_starts() -> None:
    from pipecat.frames.frames import AggregatedTextFrame, LLMFullResponseStartFrame, LLMTextFrame

    from yuki.voice.pipeline import _clause_tts, _words_done_frame

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            from pipecat.services.tts_service import TextAggregationMode

            self._text_aggregation_mode = TextAggregationMode.SENTENCE
            self.said: list[str] = []

        async def process_frame(self, frame: Any, direction: Any) -> None:
            if isinstance(frame, LLMTextFrame):
                async for item in self._text_aggregator.aggregate(frame.text):
                    self.said.append(item.text)

        async def stop_text_aggregation_metrics(self) -> None:
            pass

        async def _push_tts_frames(self, frame: Any, *a: Any) -> None:
            assert isinstance(frame, AggregatedTextFrame)
            self.said.append(frame.text)

    async def main() -> None:
        tts = _clause_tts(Base)()
        await tts.process_frame(LLMFullResponseStartFrame(), None)
        await tts.process_frame(LLMTextFrame("Let me look."), None)
        assert tts.said == []  # a whole sentence, waiting for the next word
        await tts.process_frame(_words_done_frame(), None)
        assert tts.said == ["Let me look."]  # said as the call starts, not when it is written out

    asyncio.run(main())


def test_the_desktop_read_while_they_talk_serves_the_reply() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import StubDesktop

    async def main() -> None:
        hands = Hands(StubDesktop(action_delay_s=0))
        hands.snapshot()
        snap, how = await hands.for_call(250)
        assert how == "ahead" and snap is hands.last
        hands.actions.add("your hand", "window 1 minimize", "minimized", ok=True, verified=True)
        _snap, how = await hands.for_call(250)
        assert how == "fresh"  # something was done on the desktop since: read again

    asyncio.run(main())


def test_pause_on_paused_music_presses_nothing_and_says_so() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import StubDesktop

    class Desk(StubDesktop):
        def __init__(self, status: str) -> None:
            super().__init__(action_delay_s=0)
            self.status = status

        def activity_facts(self) -> dict:
            return {"media": [{"app_id": "Chrome", "status": self.status, "title": "x", "current": True}]}

        def press(self, key: str, **kwargs: Any) -> Any:
            self.status = "playing" if self.status == "paused" else "paused"
            return super().press(key, **kwargs)

    desk = Desk("paused")
    ok, report, facts = Hands(desk)._run("media_key", {"key": "playpause", "want": "paused"})
    assert ok and "already paused" in report and facts["verified"] is None and desk.calls == []
    desk = Desk("playing")
    ok, _report, facts = Hands(desk)._run("media_key", {"key": "playpause", "want": "paused"})
    assert ok and facts["verified"] is True and desk.status == "paused"


def test_short_clear_words_are_not_doubt_but_long_unsure_ones_are() -> None:
    from yuki.voice.pipeline import heard_note

    clear = [{"word": "Luke", "confidence": 0.9}, {"word": "pass", "confidence": 0.62},
             {"word": "me", "confidence": 0.95}, {"word": "the", "confidence": 0.55},
             {"word": "charger", "confidence": 0.97}]
    assert heard_note(clear, [])[0] is None
    misheard = [{"word": "Soaki", "confidence": 0.52}, {"word": "is", "confidence": 0.9},
                {"word": "coming", "confidence": 0.9}]
    assert "Soaki" in (heard_note(misheard, [])[0] or "")
    garbled = [{"word": "me", "confidence": 0.31}, {"word": "OTP", "confidence": 0.9}]
    assert "me" in (heard_note(garbled, [])[0] or "")


def test_a_sound_alike_is_rechecked_and_the_rescue_is_not_treated_as_certain() -> None:
    from yuki.voice.wake import WakeListener

    class Spotter:
        model = "stub"
        WAKE = "YUKI"

        def __init__(self) -> None:
            self.near_misses = 0
            self.batches = 0
            self.name_end: float | None = None
            self.near_end: float | None = None

        def feed(self, samples: np.ndarray, at: float | None = None) -> str | None:
            self.batches += 1
            if self.batches == 3:
                self.near_misses += 1  # "you key" won
                self.near_end = at + 0.04
            return None

        def reset(self) -> None:
            pass

    class Check:
        def __init__(self, verdict: bool) -> None:
            self.verdict, self.seen = verdict, 0

        def heard(self, samples: np.ndarray) -> bool:
            self.seen = samples.size
            return self.verdict

    for verdict in (True, False):
        mic, woke = _Mic(), []
        listener = WakeListener(mic, Spotter, on_wake=lambda at, kw: woke.append((listener.name_end, listener.rescued)),
                                check=Check(verdict))
        listener.start()
        assert listener.ready.wait(2)
        loud = (np.sin(np.arange(320) / 3.0) * 0.3).astype(np.float32)
        at, deadline = 70.0, time.monotonic() + 2
        while time.monotonic() < deadline and not woke and listener.check.seen == 0:
            mic.push(loud, at)
            at += 0.02
            time.sleep(0.002)
        time.sleep(0.2)
        listener.stop()
        assert listener.check.seen > 0
        if verdict:
            assert woke and woke[0][1] is True and woke[0][0] is not None
        else:
            assert not woke


def test_the_front_desk_reaches_for_hands_and_nothing_runs(tmp_path: Path) -> None:
    """A request that needs hands while the worker is busy: the front desk calls the action, the blocked backend
    records it (the runtime then queues it for the worker), and nothing touches the desktop."""
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.agent.tools import Dispatcher
    from yuki.log.events import SessionLogger
    from yuki.ui.runtime import HandsBlockedBackend
    from yuki.voice.offline import StubDesktop

    def response(*blocks: Any) -> Any:
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage, id="m", model="m",
                               role="assistant", model_dump=lambda **_: {})

    calls: list[dict] = []
    replies = [response(SimpleNamespace(type="tool_use", id="t1", name="run_powershell",
                                        input={"command": "mkdir x", "risk": {"reversible": True, "user_asked": True,
                                                                              "effect": "a folder"}})),
               response(SimpleNamespace(type="tool_use", id="t2", name="done",
                                        input={"message": "That comes right after the running task."}))]
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.append(p) or replies.pop(0)))
    inner = StubDesktop(action_delay_s=0)
    blocked = HandsBlockedBackend(inner)
    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  dispatcher=Dispatcher(blocked), tool_names=None, prewarm=False, record_requests=False,
                  memory=memory)
    list(agent.run("make a folder called receipts in downloads"))
    assert "run_powershell" in {t["name"] for t in calls[0]["tools"]}  # it can reach for hands
    assert blocked.attempted == ["run_powershell"] and inner.calls == []
