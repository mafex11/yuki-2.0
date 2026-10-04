"""The Gemini voice (docs/VOICE.md §10): marks, the SSE stream, pieces in order, interruptions, fallback, settings.

Offline only: a fake Gemini (and a fake Aura) served by aiohttp on localhost, no real key, no audio device.
"""

from __future__ import annotations

import asyncio
import base64
import json
from array import array
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.config import Settings
from yuki.voice.expressive import EdgeTrimmer, keep_known_tags, speakable, split_style, strip_tags

RATE = 24_000


# -- the marks ------------------------------------------------------------------------------------------

def test_strip_tags_leaves_clean_words_for_the_chat_and_memory() -> None:
    assert strip_tags("[style: calm and soft] Oh no. [short pause] The build failed.") == "Oh no. The build failed."
    assert strip_tags("<thinking> </thinking> Okay <sigh>.") == "Okay."  # stray markup is never shown
    assert strip_tags("Haha, that's so you! <laugh>") == "Haha, that's so you!"
    assert strip_tags("Okay <sigh> .") == "Okay."
    assert strip_tags("x < y and y > z") == "x < y and y > z"  # not a mark: left alone
    assert strip_tags("") == "" and strip_tags(None) == ""


def test_style_mark_comes_off_the_front_and_only_the_first_counts() -> None:
    assert split_style("[style: warm, teasing] Haha, [laugh] you wish.") == ("warm, teasing", "Haha, [laugh] you wish.")
    assert split_style("Oh <sigh> fine.") == (None, "Oh <sigh> fine.")
    assert split_style("Sure. <style: whispering> Quietly.") == (None, "Sure. Quietly.")  # not at the start
    assert split_style("<style whispering> ok") == ("whispering", "ok")


def test_only_documented_tags_reach_gemini() -> None:
    assert keep_known_tags("Wow [gasp] really [smiles] ok [Short  pause] go [1]") == (
        "Wow <gasp> really ok <short pause> go [1]", 1)  # Gemini's spelling; "[1]" is no mark
    assert speakable("<laugh>") and not speakable("   ")


def _pcm(values: list[int]) -> bytes:
    return array("h", values).tobytes()


def test_edge_trimmer_cuts_the_padding_but_keeps_pauses_inside() -> None:
    quiet, loud = [3] * 4800, [2000, -2000] * 2400          # 200 ms each
    t = EdgeTrimmer(RATE)
    out = b"".join(t.feed(_pcm(c)) for c in (quiet, quiet, loud, quiet, loud, quiet * 3)) + t.finish()
    samples = array("h")
    samples.frombytes(out)
    # lead: 30 ms kept of 400; the 200 ms pause between words kept whole; tail: 150 ms kept of 600
    assert len(samples) == 720 + 4800 + 4800 + 4800 + 3600
    assert abs(samples[0]) < 10 and abs(samples[-1]) < 10    # faded edges: no click


# -- the SSE stream ---------------------------------------------------------------------------------------

def test_sse_audio_reads_inline_data_and_raises_on_errors() -> None:
    from yuki.voice.gemini_tts import GeminiError, request_body, sse_audio

    event = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/l16; rate=24000",
                                                                   "data": base64.b64encode(b"\x01\x02").decode()}}]}}]}
    assert sse_audio(event) == [b"\x01\x02"]
    assert sse_audio({"candidates": [{"finishReason": "STOP"}]}) == []
    with pytest.raises(GeminiError) as info:
        sse_audio({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
    assert info.value.status == 429
    body = request_body("Hi.", "Leda", "calm and soft")
    assert body["contents"][0]["parts"][0] == {"text": "Hi.", "speechMetadata": {"style": "calm and soft"}}
    assert "speechMetadata" not in request_body("Hi.", "Leda", None)["contents"][0]["parts"][0]
    assert body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Leda"


# -- a fake Gemini and a fake Aura ---------------------------------------------------------------------------

def _loud(ms: int, value: int) -> bytes:
    return _pcm([value, -value] * (RATE * ms // 2000))


class FakeServers:
    """Gemini's streamGenerateContent (SSE) and Aura's /v1/speak on one localhost port."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.aura: list[str] = []
        self.delays: dict[str, float] = {}      # text -> seconds before the first audio
        self.status = 200
        self.cancelled = 0
        self.url = ""

    async def gemini(self, request: Any) -> Any:
        from aiohttp import web

        body = await request.json()
        part = body["contents"][0]["parts"][0]
        self.requests.append(part)
        if self.status != 200:
            return web.json_response({"error": {"code": self.status, "status": "RESOURCE_EXHAUSTED"}},
                                     status=self.status)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            await asyncio.sleep(self.delays.get(part["text"], 0.0))
            value = 1000 + 37 * len(part["text"])   # each piece's audio has its own level
            for _ in range(3):
                data = base64.b64encode(_loud(100, value)).decode()
                event = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/l16; rate=24000",
                                                                               "data": data}}]}}]}
                await response.write(f"data: {json.dumps(event)}\r\n\r\n".encode())
                await asyncio.sleep(0.01)
        except (asyncio.CancelledError, ConnectionResetError):
            self.cancelled += 1
            raise
        return response

    async def model(self, request: Any) -> Any:
        from aiohttp import web

        return web.json_response({"name": "models/x"})

    async def speak(self, request: Any) -> Any:
        from aiohttp import web

        self.aura.append((await request.json())["text"])
        return web.Response(body=_loud(200, 777), content_type="application/octet-stream")

    async def __aenter__(self) -> FakeServers:
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/v1beta/models/{name}", self.gemini)
        app.router.add_get("/v1beta/models/{name}", self.model)
        app.router.add_post("/v1/speak", self.speak)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._runner.cleanup()


def _service(fake: FakeServers, *, events: list | None = None, fallback: bool = True, timeout_s: float = 3.0) -> Any:
    from yuki.voice.gemini_tts import service_class
    from yuki.voice.pipeline import _clause_tts

    return _clause_tts(service_class())(
        api_key="fake-key", model="gemini-3.8-flash-lite-tts", voice="Leda", base_url=f"{fake.url}/v1beta",
        fallback_key="fake-dg" if fallback else None, fallback_url=f"{fake.url}/v1/speak",
        first_audio_timeout_s=timeout_s, log=(lambda e, **f: events.append((e, f))) if events is not None else None,
        pause_frame_processing=False)


def _levels(frames: list[Any]) -> list[int]:
    """The level of each run of audio frames, in the order they came out (one per piece)."""
    from pipecat.frames.frames import TTSAudioRawFrame

    runs: list[int] = []
    for frame in frames:
        if isinstance(frame, TTSAudioRawFrame):
            samples = array("h")
            samples.frombytes(frame.audio)
            level = max(samples)
            if level > 300 and (not runs or runs[-1] != level):
                runs.append(level)
    return runs


def test_pieces_are_fetched_in_parallel_and_played_in_order_with_one_style() -> None:
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame, TTSStoppedFrame,
        TTSTextFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with FakeServers() as fake:
            fake.delays = {"Oh nice,": 0.6}   # the first piece is slow, the second fast
            events: list = []
            tts = _service(fake, events=events)
            tokens = ["[style: warm and teasing]", " Oh", " nice,", " you", " did", " it!", " [laugh]", " [smiles]",
                      " Want", " more?"]
            frames = [LLMFullResponseStartFrame(), *[LLMTextFrame(t) for t in tokens], LLMFullResponseEndFrame(),
                      SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            texts = [p["text"] for p in fake.requests]
            assert sorted(texts) == sorted(["Oh nice,", "you did it!", "<laugh> Want more?"])  # style off, made-up tag dropped
            assert all(p.get("speechMetadata") == {"style": "warm and teasing"} for p in fake.requests)
            # played in the order written, although the second piece's audio came first
            assert _levels(down) == [1000 + 37 * 8, 1000 + 37 * 11, 1000 + 37 * 18]
            # each piece's words come out after its audio; the reply ends once
            order = [("audio" if isinstance(f, TTSAudioRawFrame) else f.text) for f in down
                     if isinstance(f, (TTSAudioRawFrame, TTSTextFrame))]
            first_text = order.index("[style: warm and teasing] Oh nice,")
            assert order[:first_text] and set(order[:first_text]) == {"audio"}
            assert sum(isinstance(f, TTSStoppedFrame) for f in down) == 1
            reply = [f for e, f in events if e == "tts_reply"][0]
            assert reply["pieces"] == 3 and reply["style"] and reply["dropped_tags"] == 1 and reply["tags"] == 1

    asyncio.run(main())


def test_an_interruption_cancels_the_requests_and_nothing_more_is_played() -> None:
    from pipecat.frames.frames import (
        InterruptionFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with FakeServers() as fake:
            fake.delays = {"Let me tell you a long story,": 1.0}
            tts = _service(fake)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("Let me tell you a long story, "),
                      SleepFrame(0.3), InterruptionFrame(), SleepFrame(1.2)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert not any(isinstance(f, TTSAudioRawFrame) for f in down)
            assert len(fake.requests) == 1 and not tts._replies

    asyncio.run(main())


def test_a_refused_request_is_said_by_aura_without_marks_and_so_is_the_rest() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice import gemini_tts

    async def main() -> None:
        async with FakeServers() as fake:
            fake.status = 429
            events: list = []
            tts = _service(fake, events=events)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("[style: calm and soft] Oh no. [sigh] "),
                      SleepFrame(0.5), LLMTextFrame("It failed. Again."), LLMFullResponseEndFrame(), SleepFrame(1.0)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert _levels(down) == [777]
            assert fake.aura == ["Oh no.", "It failed.", "Again."]   # marks stripped for Aura
            assert len(fake.requests) == 1                           # after the refusal, straight to Aura
            assert [f["status"] for e, f in events if e == "tts_fallback"] == [429]
            ok, why = gemini_tts.gemini_status("fake-key", "gemini-3.8-flash-lite-tts")
            assert not ok and "429" in why                           # the next conversation starts on Aura

    try:
        asyncio.run(main())
    finally:
        gemini_tts.forget_status()


def test_no_audio_in_time_falls_back_to_aura() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with FakeServers() as fake:
            fake.delays = {"Hello there.": 2.0}
            tts = _service(fake, timeout_s=0.4)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("Hello there."), LLMFullResponseEndFrame(),
                      SleepFrame(1.2)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert fake.aura == ["Hello there."] and _levels(down) == [777]

    asyncio.run(main())


# -- the other voices, the chat, memory, settings ---------------------------------------------------------------

def test_aura_and_elevenlabs_are_sent_the_words_without_marks() -> None:
    from yuki.voice.pipeline import _clause_tts

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            from pipecat.services.tts_service import TextAggregationMode

            self._text_aggregation_mode = TextAggregationMode.SENTENCE

        def _prepare_text_for_tts(self, text: str) -> str:
            return text + " "

    assert _clause_tts(Base, strip_marks=True)()._prepare_text_for_tts("[style: calm] Oh [sigh] fine.") == "Oh fine. "
    assert _clause_tts(Base)()._prepare_text_for_tts("Oh <sigh> fine.") == "Oh <sigh> fine. "


def test_the_first_clause_never_splits_inside_a_style_mark() -> None:
    from pipecat.utils.text.base_text_aggregator import AggregationType
    from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

    from yuki.voice.pipeline import clause_aggregator

    async def run(tokens: list[str]) -> list[str]:
        agg = clause_aggregator(SimpleTextAggregator)(aggregation_type=AggregationType.SENTENCE)
        out = [item.text for token in tokens async for item in agg.aggregate(token)]
        return out

    async def collect() -> list[str]:
        return await run(["[style: warm,", " teasing]", " Oh", " nice,", " you", " did", " it."])

    assert asyncio.run(collect())[0] == "[style: warm, teasing] Oh nice,"


class _Memory:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def log_turn(self, **kw: Any) -> None:
        self.turns.append(kw)


def test_chat_and_memory_get_clean_words() -> None:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, BotStoppedSpeakingFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
        LLMTextFrame, TranscriptionFrame, TTSTextFrame,
    )

    from yuki.voice.pipeline import TurnObserver

    memory, chat = _Memory(), []
    obs = TurnObserver(settings=Settings(), log=None, memory=memory, session="t", speech_end=lambda: None,
                       on_state=lambda *a: None, tts_provider="gemini", activity=lambda: None)
    obs.on_chat = chat.append
    output = object()
    obs.output_processor = output
    obs.on_frame(TranscriptionFrame(text="I told him the bug was a feature", user_id="u", timestamp="t"))
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMTextFrame(text="[laugh] No way, he bought that?"))
    spoken = TTSTextFrame("[laugh] No way, he bought that?", aggregated_by="sentence")
    obs.on_frame(spoken)
    obs.on_played(SimpleNamespace(frame=spoken, source=output))
    obs.on_frame(BotStartedSpeakingFrame())
    obs.on_played(SimpleNamespace(frame=BotStoppedSpeakingFrame(), source=output))
    obs.on_frame(BotStoppedSpeakingFrame())
    obs.on_frame(LLMFullResponseEndFrame())
    obs.close()
    said = [e["text"] for e in chat if e["type"] == "assistant_speech"]
    assert said and all(text == "No way, he bought that?" for text in said)
    assert memory.turns[0]["reply_text"] == "No way, he bought that?"


def test_voice_json_chooses_the_voice_and_the_gemini_settings() -> None:
    from yuki.voice.session import conversation_settings

    s = Settings()
    assert (s.voice_tts_provider, s.voice_gemini_voice, s.voice_gemini_model) == (
        "gemini", "Leda", "gemini-3.8-flash-lite-tts")
    got = conversation_settings(s, {"tts_provider": "ElevenLabs", "gemini_voice": "Kore",
                                    "gemini_style": "calm and soft", "tts_voice": "abc"})
    assert (got.voice_tts_provider, got.voice_gemini_voice, got.voice_gemini_style, got.voice_tts_voice) == (
        "elevenlabs", "Kore", "calm and soft", "abc")
    assert conversation_settings(s, {"tts_provider": "robot"}).voice_tts_provider == "gemini"  # unknown: ignored


def test_preflight_uses_aura_when_there_is_no_google_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import gemini_tts, pipeline

    keys = {"DEEPGRAM_API_KEY": "dg", "AWS_BEARER_TOKEN_BEDROCK": "b"}
    monkeypatch.setattr(pipeline, "get_key", keys.get)
    monkeypatch.setattr(pipeline, "_registry_env", keys.get)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "b")
    gemini_tts.forget_status()
    facts = pipeline.preflight(Settings(), need_headphones=False)
    assert facts["tts_provider"] == "deepgram" and "GOOGLE_API_KEY" in facts["tts_note"]
    keys["GOOGLE_API_KEY"] = "g"
    monkeypatch.setattr(gemini_tts, "_check_request", lambda key, model, timeout_s: (True, "ok"))
    assert pipeline.preflight(Settings(), need_headphones=False)["tts_provider"] == "gemini"
    gemini_tts.forget_status()


def test_the_mouth_is_told_about_its_voice_marks() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, VOICE_MARKS_PROMPT, mouth_prompt

    voice = VOICE_MARKS_PROMPT   # vfix4: its own section, in the core only when the voice performs marks
    assert "[style:" in voice and "[sigh]" in voice
    assert VOICE_MARKS_PROMPT in mouth_prompt("marker", marks=True) and "[sigh]" not in mouth_prompt("marker", marks=False)
    assert "<" not in voice  # angle-bracket marks made Haiku write XML-like markup as speech (2026-09-27)
    assert "tags of any kind" not in MOUTH_PROMPT


def test_markup_written_as_words_is_never_said_or_shown() -> None:
    # Live offline run 2026-09-27: the mouth once wrote "<stay_quiet>\n</stay_quiet>" as its words.
    assert strip_tags("<stay_quiet>\n</stay_quiet>") == ""
    assert keep_known_tags("<stay_quiet> </invoke>") == ("", 2)
    assert not speakable("<stay_quiet> </invoke>") and speakable("<laugh>")
