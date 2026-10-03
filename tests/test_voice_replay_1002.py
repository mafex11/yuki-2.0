"""The 2 Oct 2026 21:24-21:28 Discord-call sequence replayed through the real ``Conversation`` (04 §4.1, 07 §1).

Frame order from the content-free logs (``voice-20261002.jsonl``, sessions 02ef, 4a82 and 1908): "Open Chrome." got a
reply the guard dropped as reasoning, the 20 ms breath counted as an answer and the call idled out 10 s later;
"Restore the pages." finished and the talker stayed quiet on it; the "What did I do today?" acknowledgement hit
Aura's dropped connection and its 261-character result was dropped whole behind a "Busy day:" verdict; a marks-only
reply after "make it a little bit less." raised "completed with no audio"; "Hello?" got a marks-only reply and the
session was closed on it.

Here the same moments run through the app's own pieces: ``Conversation`` (its watch, reply watch, owed-output
ledger, narrator gate and turn observer), the real ``VoiceBridge`` with a stand-in runtime, the real Gemini voice
against a fake Gemini and a fake Aura on localhost (Gemini slow for one piece and failing for another, Aura dropping
its pooled connection once), and the real output transport writing to a pretend speaker. Only the talker is
scripted (its replies, in the order the logs show). Run in both speech channels. Every job must end audible, and the
call must not idle out before "Open Chrome." is handled.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])

#: What the talker wrote that the guard caught as reasoning on 2 Oct (the stand-in judge's verdicts: test data).
REASONING = ("I'll open Chrome for them.", "Let me do that now.")
RESULT_2 = "Busy day: coding on T3 Code about 1h20m, then an hour-plus of Valorant."
TALKER_2 = "Busy day: coding on T3 Code for about an hour and twenty, then over an hour of Valorant."


@pytest.fixture(autouse=True)
def _no_real_audio_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 7: nothing here may open a real microphone or speaker."""
    import sounddevice

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a test tried to open a real audio device")

    for name in ("InputStream", "RawInputStream", "OutputStream", "RawOutputStream", "Stream", "RawStream"):
        monkeypatch.setattr(sounddevice, name, refuse)


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.log_dir = None

    def event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        self.events.append((event_type, fields))
        return fields

    def turn(self, row: dict[str, Any]) -> None:
        self.events.append(("turn", row))

    def of(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        return [f for k, f in self.events if k == kind and all(f.get(a) == b for a, b in match.items())]


class PretendSpeaker:
    """Takes the voice's audio at real-time pace and plays nothing."""

    def __init__(self) -> None:
        self.bytes = 0
        self.timeline = None
        self.latency_s = 0.02

    def write(self, pcm: bytes, gate: bool = True) -> None:
        self.bytes += len(pcm)
        time.sleep(len(pcm) / 48_000)


def _harness(tmp_path: Path, channel: str, fake: Any) -> Any:
    from pipecat.frames.frames import LLMRunFrame, TTSTextFrame
    from pipecat.observers.base_observer import BaseObserver
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.transports.base_transport import TransportParams

    from test_voice_v39 import FakeRuntime
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.gemini_tts import service_class
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation, _clause_tts
    from yuki.voice.speechguard import SpeechGuard
    from yuki.voice.transport import SpeakerOutput

    settings = Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", voice_priority_boost=False,
                        voice_step_lines=False, voice_grounding=False, voice_call_idle_s=2.5,
                        voice_speech_channel=channel, voice_final_gap_s=0.1)
    log = Log()
    heard: list[tuple[float, str]] = []
    states: list[tuple[str, str]] = []

    class Talker(FrameProcessor):
        """The scripted talker: passes frames on, and answers a rerun request (LLMRunFrame) by its script."""

        def __init__(self) -> None:
            super().__init__(name="talker")
            self._yuki_call: Any = None
            self._yuki_spoke: set[str] = set()
            self._yuki_early: set[str] = set()
            self._yuki_context: Any = None
            self.on_rerun: Any = None

        async def process_frame(self, frame: Any, direction: Any) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMRunFrame):
                if self.on_rerun is not None:
                    run, self.on_rerun = self.on_rerun, None
                    asyncio.ensure_future(run())
                return
            await self.push_frame(frame, direction)

    talker = Talker()
    tts_events: list = []
    tts = _clause_tts(service_class())(
        api_key="fake-key", model="gemini-3.8-flash-lite-tts", voice="Leda", base_url=f"{fake.url}/v1beta",
        fallback_key="fake-dg", fallback_url=f"{fake.url}/v1/speak", first_audio_timeout_s=1.5, hedge_s=0.3,
        log=lambda e, **f: tts_events.append((e, f)))
    output = SpeakerOutput(PretendSpeaker(), TransportParams(audio_out_enabled=True, audio_out_sample_rate=24_000))

    async def judge(text: str, said: list[str], user: str) -> str:   # stand-in for the guard's model (test data)
        await asyncio.sleep(0.05)
        return "DROP: reasoning" if text in REASONING or text.startswith("Busy day:") else "SAY"

    class Heard(BaseObserver):
        async def on_push_frame(self, data: Any) -> None:
            if isinstance(data.frame, TTSTextFrame) and data.source is output:
                heard.append((time.monotonic(), data.frame.text))

    class Replay(Conversation):
        def _build(self) -> Any:
            from pipecat.pipeline.pipeline import Pipeline
            from pipecat.pipeline.worker import PipelineParams, PipelineWorker

            self._llm, self._tts, self._llm_context, self._stt, self._tools = talker, tts, None, None, None
            guard = SpeechGuard(None, "haiku", judge=judge, timeout_s=1.0, log=self.log, session=self.session,
                                shadow=self.channel == "marker")
            guard.on_drop = self.observer.guarded
            self.guard = self.observer.guard = guard
            tts.set_guard(guard)
            tts._yuki_on_silent = self.observer.tts_silent
            self.observer.output_processor = output
            self.worker = PipelineWorker(
                Pipeline([talker, tts, output]),
                params=PipelineParams(enable_metrics=False, audio_out_sample_rate=24_000),
                idle_timeout_secs=None, enable_rtvi=False, check_dangling_tasks=False,
                observers=[self.observer.as_observer(), Heard()])
            return self.worker

        async def _warm(self) -> None:
            return None

    rt = FakeRuntime()
    bridge = VoiceBridge(rt, log, foreground=lambda: 42, settings=settings)
    conv = Replay(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=MouthMemory(),
                  log=log, speech_end=lambda: None, on_state=lambda s, d: states.append((s, d)), session="replay",
                  call_app="Discord", call_evidence="fresh")
    tones: list[str] = []
    conv.on_tone = tones.append
    # quicker than live, the same order: a rerun after 0.5 s of quiet, an owed outcome by code after 0.4 s
    conv.REPLY_WATCH_AFTER_S, conv.REPLY_WATCH_QUIET_S = 0.5, 0.4
    return SimpleNamespace(conv=conv, bridge=bridge, rt=rt, talker=talker, tts=tts, log=log, heard=heard,
                           states=states, tones=tones, tts_events=tts_events, channel=channel)


def _servers() -> Any:
    from test_voice_gemini import FakeServers

    class Servers(FakeServers):
        def __init__(self) -> None:
            super().__init__()
            self.fail: set[str] = set()
            self.aura_drops = 0

        async def gemini(self, request: Any) -> Any:
            from aiohttp import web

            body = await request.json()
            text = body["contents"][0]["parts"][0]["text"]
            if text in self.fail:
                self.requests.append(body["contents"][0]["parts"][0])
                return web.json_response({"error": {"code": 500, "status": "INTERNAL"}}, status=500)
            request._read_bytes = json.dumps(body).encode()
            return await super().gemini(request)

        async def speak(self, request: Any) -> Any:
            if self.aura_drops > 0:
                self.aura_drops -= 1
                await request.read()
                request.transport.close()
                return None
            return await super().speak(request)

    return Servers()


async def _until(cond: Any, timeout: float, what: str) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for: {what}")


async def _replay(h: Any) -> dict[str, Any]:
    from pipecat.frames.frames import (
        FunctionCallInProgressFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
        TranscriptionFrame, TTSSpeakFrame,
    )

    from test_voice_v39 import Params
    from yuki.voice.mouth import ReplyChannel

    conv, bridge, rt, talker = h.conv, h.bridge, h.rt, h.talker
    marker = h.channel == "marker"
    found: dict[str, Any] = {}

    async def pump() -> None:
        while True:
            APP.processEvents()
            await asyncio.sleep(0.01)

    def heard_text() -> str:
        return " ".join(t for _at, t in h.heard)

    async def user(text: str) -> None:
        await conv.worker.queue_frame(TranscriptionFrame(text=text, user_id="u", timestamp="t"))
        await _until(lambda: conv.observer.turn is not None and conv.observer.turn.user_text == text, 2, text)

    async def reply(text: str | None = None, *, tools: tuple[str, ...] = (), user_turn: bool = True,
                    then: tuple[str, ...] = ()) -> None:
        """One talker run, as the service hands it on: its speech (marker mode: after its marker line), its calls."""
        call = SimpleNamespace(user_turn=user_turn, first_text=None)
        talker._yuki_call = call
        await talker.push_frame(LLMFullResponseStartFrame())
        if text is not None:
            if marker:
                channel = ReplyChannel()
                speech = "".join(channel.feed(text[i:i + 7]) for i in range(0, len(text), 7))
                channel.finish()
                conv._marker_report(call, channel)
            else:
                speech = text
            if speech:
                call.first_text = time.monotonic()
                for part in speech.split(" "):
                    await talker.push_frame(LLMTextFrame(text=part + " "))
        for n, name in enumerate(tools):
            await talker.push_frame(FunctionCallInProgressFrame(function_name=name, tool_call_id=f"{name}-{n}-"
                                                                f"{time.monotonic()}", arguments={}))
        await talker.push_frame(LLMFullResponseEndFrame())
        for line in then:
            await asyncio.sleep(0.3)
            await talker.push_frame(TTSSpeakFrame(text=line))

    async def tell(request: str, say_first: str, result_run: Any) -> int:
        params = Params(request, call_id=f"tell-{time.monotonic()}", heard=request, say_first=say_first)
        params.llm = talker

        async def result_callback(result: dict, properties: Any = None) -> None:
            params.results.append((result, properties))
            if getattr(properties, "run_llm", True) and "status" not in result:
                asyncio.ensure_future(result_run(result))

        params.result_callback = result_callback
        before = len(rt.submitted)
        asyncio.ensure_future(bridge.tell_yuki(params))
        await _until(lambda: len(rt.submitted) > before, 3, f"submit {request}")
        job = rt.submitted[-1][0]
        rt.started.emit("worker", job, request)
        return job

    pump_task = asyncio.create_task(pump())
    run_task = asyncio.create_task(conv.run())
    await _until(lambda: conv.worker is not None and conv._bound, 10, "the conversation is live")
    await asyncio.sleep(0.5)

    # -- session 02ef: "Open Chrome." -> a reply of reasoning (the guard's catch / no marker line), nothing heard ---
    async def open_chrome() -> None:   # the rerun: the open_app hand, its say_first and its done line by code
        await reply(None, tools=("open_app",), then=("Opening Chrome.", "Chrome's open."))

    await user("Open Chrome.")
    talker.on_rerun = open_chrome
    await reply(" ".join(REASONING) if not marker else "I'll open Chrome for them.\nLet me do that now.")
    await _until(lambda: "Chrome's open." in heard_text(), 15, "Chrome's open. heard")
    found["running_when_chrome_opened"] = conv.reason == "running"

    # -- session 4a82: "Restore the pages." -> job 1 -> the talker stays quiet on its result ---------------------------
    async def quiet_on_result(result: dict) -> None:
        q = Params("x", call_id=f"q-{time.monotonic()}")
        q.llm = talker
        await reply("quiet\n" if marker else None, tools=("stay_quiet",), user_turn=False)
        await bridge.stay_quiet(q)

    await user("Restore the pages.")
    await reply(None, tools=("tell_yuki",))
    job1 = await tell("Restore the pages.", "Got it, restoring your pages.", quiet_on_result)
    await asyncio.sleep(1.0)
    rt.finished.emit("worker", job1, "Pages restored.")
    rt.lane_done.emit("worker", job1, "Restore the pages.", False, [])
    await _until(lambda: "Pages restored." in heard_text(), 15, "Pages restored. heard")

    # -- "What did I do today?": its ack on Aura over a dropped connection; its result behind "Busy day:" ------------
    async def say_result(result: dict) -> None:
        await reply(("say\n" + TALKER_2) if marker else TALKER_2, user_turn=False)

    fake = h.fake
    fake.fail = {"Let me go through your day."}
    fake.aura_drops = 1
    await user("What did I do today?")
    await reply(None, tools=("tell_yuki",))
    job2 = await tell("What did I do today?", "Let me go through your day.", say_result)
    await _until(lambda: "Let me go through your day." in heard_text(), 10, "the ack heard")
    await asyncio.sleep(0.5)
    rt.finished.emit("worker", job2, RESULT_2)
    rt.lane_done.emit("worker", job2, "What did I do today?", False, [])
    await _until(lambda: "Busy day" in heard_text(), 15, "the day's result heard")

    # -- session 1908: a marks-only reply after an action, then "Hello?" answered by a mark alone ---------------------
    await user("make it a little bit less.")
    await reply("say\n[smiles]" if marker else "[smiles]", tools=("app_volume",))
    await asyncio.sleep(1.0)

    async def sorry() -> None:
        await reply(("say\n" if marker else "") + "Sorry! I turned it down a little.")

    await user("Hello?")
    talker.on_rerun = sorry
    await reply("say\n[smiles]" if marker else "[smiles]")
    await _until(lambda: "turned it down a little" in heard_text(), 15, "Hello? answered")
    found["running_when_hello_answered"] = conv.reason == "running"

    # -- then the call goes quiet: it may end now, and says so -----------------------------------------------------------
    reason = await asyncio.wait_for(run_task, timeout=20)
    pump_task.cancel()
    found.update(reason=reason, stop=dict(conv.stop_facts), heard=heard_text(),
                 ledger=conv.ledger.summary() if conv.ledger else None)
    return found


@pytest.mark.parametrize("channel", ["guard", "marker"])
def test_the_2_oct_call_replayed_every_job_is_heard_and_no_call_idle_cuts_a_request(tmp_path: Path,
                                                                                   channel: str) -> None:
    async def main() -> tuple[dict[str, Any], Any]:
        async with _servers() as fake:
            fake.delays = {"Opening Chrome.": 1.0}   # Gemini's first byte late (21:24:30): hedged with Aura
            h = _harness(tmp_path, channel, fake)
            h.fake = fake
            return await _replay(h), h

    found, h = asyncio.run(main())
    log = h.log
    # no call idle before "Open Chrome." was handled (live: it ended the conversation 10.3 s after the guard drop)
    assert found["running_when_chrome_opened"] and found["running_when_hello_answered"]
    # every job ended audible: "Pages restored." (the talker stayed quiet: said by code) and the day's result
    assert "Pages restored." in found["heard"] and "Busy day" in found["heard"]
    assert found["ledger"]["owed"] >= 2 and found["ledger"]["unheard"] == 0 and found["ledger"]["shown"] == 0
    # the acknowledgement survived Aura's dropped connection; the hedge took the slow first byte
    assert [f["error"] for e, f in h.tts_events if e == "tts_retry"] == ["ServerDisconnectedError"]
    assert any(e == "tts_hedge" and f.get("op") == "winner" for e, f in h.tts_events)
    # "Open Chrome." and "Hello?" were each answered again once; nothing was given up on
    reruns = log.of("reply_watch", op="rerun")
    assert len(reruns) == 2 and not log.of("reply_watch", op="gave_up") and h.tones == []
    # no "completed with no audio", and no red pill with a service's raw text
    assert not log.of("error") and not [d for s, d in h.states if s == "error"]
    # then the call idled out, with nothing owed or unanswered
    assert found["reason"] == "call_idle"
    assert found["stop"]["unanswered"] is False and found["stop"]["owed"] == 0 and found["stop"]["held"] is False
    # v3.17 (C1): both outcomes are short, so each is said by code word for word the moment it is ready, with no talker
    # run at all (before: "Pages restored." after the talker's stay_quiet, the day's result behind a guard verdict or
    # by the talker); the ledger saw each one heard.
    direct = [f.get("via") for f in log.of("owed", op="heard") if f.get("direct")]
    assert direct == ["code", "code"]
    if channel == "marker":
        # the talker's own reasoning never reached the voice: the reply had no marker line
        assert log.of("marker", unmarked=True)
