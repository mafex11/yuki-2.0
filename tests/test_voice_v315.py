"""voice v3.15 (docs/VOICE.md, the v3.15 section): alone in a call, a silent TTS piece, the loop's log sink."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from test_voice_gemini import RATE, FakeServers, _levels, _pcm, _service


class SilentGemini(FakeServers):
    """A Gemini whose stream for some texts carries only silence (live 30 Sept: "completed with no audio")."""

    def __init__(self) -> None:
        super().__init__()
        self.silent: set[str] = set()

    async def gemini(self, request: Any) -> Any:
        from aiohttp import web

        body = await request.json()
        part = body["contents"][0]["parts"][0]
        if part["text"] not in self.silent:
            request._read_bytes = json.dumps(body).encode()   # re-readable for the parent
            return await super().gemini(request)
        self.requests.append(part)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        data = base64.b64encode(_pcm([0] * (RATE // 10))).decode()
        event = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/l16; rate=24000",
                                                                       "data": data}}]}}]}
        await response.write(f"data: {json.dumps(event)}\r\n\r\n".encode())
        return response


def test_a_piece_whose_stream_is_silent_is_said_by_aura_and_the_next_tries_gemini_again() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with SilentGemini() as fake:
            fake.silent = {"Sorry, I missed that."}
            events: list = []
            tts = _service(fake, events=events)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("Sorry, I missed that. "), SleepFrame(0.6),
                      LLMTextFrame("Say it again?"), LLMFullResponseEndFrame(), SleepFrame(1.2)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert fake.aura == ["Sorry, I missed that."]                  # that piece: Aura, at once
            assert [p["text"] for p in fake.requests] == ["Sorry, I missed that.", "Say it again?"]
            assert _levels(down)[0] == 777 and len(_levels(down)) == 2      # both heard, in order
            assert [f["reason"] for e, f in events if e == "tts_fallback"] == ["silent"]
            assert not any(e == "error" for e, _ in events)

    asyncio.run(main())


# -- alone in a call is not a call (v3.15) ------------------------------------------------------------------------


def test_alone_in_a_call_reaches_voice_as_no_call_mode_and_a_friend_joining_turns_it_on(tmp_path) -> None:
    import time as _time

    from test_av_signals import DISCORD, Stubs, _worker
    from yuki.memory.api import MemoryClient
    from yuki.memory.callevidence import CallEvidence, Verdict
    from yuki.memory.store import Store
    from yuki.voice.session import call_facing

    def judge(screen):   # stand-in for the screen model: the names under the call, minus the user's own
        joined = "Disconnect" in screen.controls
        return Verdict(in_call=joined, service="Discord" if joined else "",
                       others=(sum(c in ("Tarnesh", "Rohan") for c in screen.controls) if joined else None))

    store = Store.open(tmp_path / "memory.db")
    try:
        now = _time.time()
        stubs = Stubs()
        stubs.hold("mic", DISCORD, now - 30)
        w = _worker(store, stubs)
        w.evidence = CallEvidence(judge_fn=judge, rejudge_s=60.0)
        w.sites_fn = lambda key: 1

        def see(at, controls):
            w.evidence.seen({"process": "discord", "exe": DISCORD, "hwnd": 7, "at": at, "host": None, "title": "",
                             "controls": list(controls)})

        see(now - 5, ["Voice Connected", "Disconnect", "mafex", "Ringing"])
        w.step(now)
        call = MemoryClient(store).call_status()["call"]
        assert call["others"] == 0 and call_facing(call) == (None, None, True)          # normal mode
        w.evidence._judged_at = {k: _time.monotonic() - 9.0 for k in w.evidence._judged_at}
        see(now + 1, ["Voice Connected", "Disconnect", "mafex", "Tarnesh"])            # he joined
        w.step(now + 2)                                    # alone: judged again after 8 s, not a minute
        call = MemoryClient(store).call_status()["call"]
        assert call["others"] == 1 and call_facing(call) == ("Discord", "fresh", False)  # call mode
        assert call_facing({"app": "Zoom", "evidence": "stale", "others": None}) == ("Zoom", "stale", False)
    finally:
        store.close()


def test_a_conversation_started_with_its_speaker_plays_its_audio() -> None:
    """Live 2026-09-30: a conversation started without the standby (its speaker given at once) had no writer
    thread, and every audio frame failed ("no attribute '_executor'"): three of seven conversations were mute."""
    from types import SimpleNamespace

    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice.transport import SpeakerOutput

    written: list[bytes] = []
    out = SpeakerOutput(SimpleNamespace(write=written.append),
                        TransportParams(audio_out_enabled=True, audio_out_sample_rate=24_000))

    async def main() -> None:
        out.get_event_loop = asyncio.get_running_loop
        assert await out.write_audio_frame(OutputAudioRawFrame(b"\1\0" * 240, 24_000, 1)) is True

    asyncio.run(main())
    assert written == [b"\1\0" * 240]


def test_a_heavy_window_is_read_from_the_now_picture_and_a_click_reads_it_itself(monkeypatch) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import voice_scenarios as vs

    import yuki.memory.now as now
    from yuki.voice import hands as hands_module
    from yuki.voice.hands import Hands

    desk = vs._youtube_desk(vs.Timeline())
    hands = Hands(desk)
    hands.snapshot()
    title = desk.title(desk.CHROME)
    hands._read_items(desk.CHROME, title)
    own = [i["name"] for i in hands.items.items]
    assert hands.items.read                                    # a light window: read here, with element ids
    hands._read_ms[desk.CHROME] = hands_module.HEAVY_READ_MS + 200   # measured heavy
    board = {"windows": [{"hwnd": desk.CHROME, "read_at": 1.0, "stale": False, "controls": own[:5],
                          "messages": []}]}
    # v3.17: the hands take memory's Now picture from their own source (a stand-in desk never reaches the real one)
    hands.now_source = lambda: board
    del now
    reads: list[str] = []
    real = hands._dispatcher.dispatch
    monkeypatch.setattr(hands._dispatcher, "dispatch",
                        lambda name, args: (reads.append(name), real(name, args))[1])
    hands._read_items(desk.CHROME, title)
    assert reads == [] and hands.items.read == 0 and [i["name"] for i in hands.items.items] == own[:5]
    number = next(n for n, i in enumerate(hands.items.items, 1) if "Make Up My Mind" in i["name"]) \
        if any("Make Up My Mind" in n for n in own[:5]) else 1
    ok, report, facts = hands._run("click", {"window": desk.CHROME, "item": number, "say_when_done": ""})
    assert "look_at_window" in reads                           # the click found its item in a read of its own
    board["windows"][0]["stale"] = True                        # the window changed after memory read it
    reads.clear()
    hands._read_items(desk.CHROME, title)
    assert reads == ["look_at_window"]                         # the bounded fallback: read here


def test_a_short_first_line_approved_once_needs_no_model_again_so_a_fast_voice_never_waits() -> None:
    from yuki.voice.speechguard import SpeechGuard

    asked: list[str] = []

    def judge(text, said, user):
        asked.append(text)
        return "SAY"

    async def main() -> None:
        guard = SpeechGuard(None, "haiku", judge=judge, timeout_s=0.5)
        first = "Sure, one sec."
        assert (await guard.decide(guard.start(first, first=True), first)).ok   # a reply's first line: asked
        other = SpeechGuard(None, "haiku", judge=judge, timeout_s=0.5)          # the next conversation
        assert (await other.decide(other.start(first, first=True), first)).by == "free"   # known: said at once
        longer = "Checking your invoices now."
        assert (await other.decide(other.start(longer, first=True), longer)).ok  # longer lines: always checked

    asyncio.run(main())
    assert asked == ["Sure, one sec.", "Checking your invoices now."]
