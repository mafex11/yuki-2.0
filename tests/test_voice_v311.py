"""Voice v3.11 (2026-09-28: "while saying yuki it takes like 4 seconds to start hearing ... why does it have to start
the entire voice pipeline when I say yuki?"). Offline checks.

The name's end is timed from the newest audio (never drifting over a long listening stream); the command said in
the same breath is what speech recognition gets, with the name left out; a conversation built ahead waits dormant
(no microphone subscription, no audio sent, no speaker held) and goes live on the wake with the words since the
name; its bridge is bound only when it is live. No microphone, no speakers, no network: fakes and stand-ins only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from yuki.voice.audio import RATE


# -- A. the name's end, from the newest audio -------------------------------------------------------------------

class _FakeKws:
    """A stand-in for sherpa-onnx's spotter: reports the name at a chosen batch, the token timed in stream seconds."""

    def __init__(self, hit_batch: int, token_before_end_s: float) -> None:
        self.hit_batch = hit_batch
        self.before = token_before_end_s
        self.batches = 0
        self.fed_s = 0.0
        self._ready = False
        self.keyword_spotter = self

    def create_stream(self) -> Any:
        self.fed_s = 0.0
        return SimpleNamespace(accept_waveform=self._accept)

    def _accept(self, rate: int, samples: np.ndarray) -> None:
        self.batches += 1
        self.fed_s += samples.size / rate
        self._ready = True

    def is_ready(self, _stream: Any) -> bool:
        ready, self._ready = self._ready, False
        return ready

    def decode_stream(self, _stream: Any) -> None:
        pass

    def get_result(self, _stream: Any) -> Any:
        if self.batches == self.hit_batch:
            return SimpleNamespace(keyword="YUKI", timestamps=[self.fed_s - self.before])
        return SimpleNamespace(keyword="", timestamps=[])


def _spotter(kws: _FakeKws) -> Any:
    from yuki.voice.wake import WakeSpotter

    spotter = WakeSpotter.__new__(WakeSpotter)
    spotter.model = "fake"
    spotter._kws = kws
    spotter._stream = kws.create_stream()
    spotter.near_misses = 0
    spotter._t0 = None
    spotter.name_end = None
    spotter.near_end = None
    spotter._fed = 0
    return spotter


@pytest.mark.parametrize("drift", [0.0, 0.004, 0.02])
def test_the_names_end_is_timed_from_the_newest_audio_not_the_streams_start(drift: float) -> None:
    """Live 2026-09-27/28 the name's end came out 2.2-12.9 s before the hit after long listening (a stream that stays
    open while a video plays, and a mic clock that runs apart from the PC's): what was replayed began before the
    name, "Yuki." became a turn of its own and the command waited behind seconds of old audio."""
    from yuki.voice.wake import NAME_END_OFFSET_S

    batches = 3000   # five minutes of 100 ms batches in one open stream
    kws = _FakeKws(hit_batch=batches, token_before_end_s=0.3)
    spotter = _spotter(kws)
    chunk = np.zeros(RATE // 10, np.float32)
    at = 1000.0
    heard = None
    for _ in range(batches):
        heard = spotter.feed(chunk, at)
        last_at = at
        at += 0.1 * (1.0 + drift)   # the PC's clock: the mic delivers 100 ms of samples every 100 ms (1+drift)
    assert heard == "YUKI"
    batch_end = last_at + chunk.size / RATE
    expected = batch_end - 0.3 + NAME_END_OFFSET_S   # the name ended 0.3 s of audio before the newest sample
    assert abs(spotter.name_end - expected) < 0.01, (spotter.name_end, expected)


def test_a_fresh_stream_starts_its_own_clock_again() -> None:
    kws = _FakeKws(hit_batch=5, token_before_end_s=0.2)
    spotter = _spotter(kws)
    chunk = np.zeros(RATE // 10, np.float32)
    for i in range(5):
        spotter.feed(chunk, 10.0 + i * 0.1)
    assert spotter._fed == 0   # a result starts a new stream: its samples count from zero
    spotter.reset()
    assert spotter._t0 is None and spotter._fed == 0


# -- B. the dormant microphone input ----------------------------------------------------------------------------

class _Mic:
    """The shared mic's subscribe/backlog API over a list of kept blocks."""

    def __init__(self, kept: list[tuple[bytes, float]]) -> None:
        self.kept = kept
        self.subs: dict[int, Any] = {}

    def subscribe(self, fn: Any) -> int:
        self.subs[len(self.subs) + 1] = fn
        return len(self.subs)

    def subscribe_from(self, fn: Any, since: float) -> tuple[int, list[tuple[bytes, float]]]:
        token = self.subscribe(fn)
        return token, [(pcm, at) for pcm, at in self.kept if at >= since]

    def unsubscribe(self, token: int) -> None:
        self.subs.pop(token, None)


def _block(level: float) -> bytes:
    return (np.full(RATE // 50, level, np.float32) * 32767).astype("<i2").tobytes()


def test_a_dormant_input_sends_nothing_until_it_goes_live_then_the_words_since_the_name() -> None:
    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice.transport import MicInput

    kept = [(_block(0.2), 10.0 + i * 0.02) for i in range(50)]   # one second kept, the name up to 10.4 s
    mic = _Mic(kept)
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000)
    pushed: list[Any] = []

    async def main() -> None:
        inp = MicInput(mic, params, dormant=True)

        async def push_audio_frame(frame: Any) -> None:
            pushed.append(frame)

        async def ready(_frame: Any) -> None:
            pass

        inp.push_audio_frame = push_audio_frame
        inp.set_transport_ready = ready

        async def base_start(_self: Any, _frame: Any) -> None:
            return None

        import pipecat.transports.base_input as base_input

        original = base_input.BaseInputTransport.start
        base_input.BaseInputTransport.start = base_start
        try:
            inp.get_event_loop = asyncio.get_running_loop
            await inp.start(StartFrame())
            assert mic.subs == {} and pushed == []   # dormant: not even subscribed
            facts = await inp.go_live(10.4)
            assert len(mic.subs) == 1
            assert pushed and all(isinstance(f, InputAudioRawFrame) for f in pushed)
            assert facts["kept_s"] == pytest.approx(0.6, abs=0.05)   # only what came after the name
            again = await inp.go_live(10.0)
            assert again == {} and len(mic.subs) == 1   # live once
        finally:
            base_input.BaseInputTransport.start = original

    asyncio.run(main())


def test_a_speaker_output_without_a_speaker_drops_nothing_and_fails_nothing() -> None:
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice.transport import SpeakerOutput

    out = SpeakerOutput(None, TransportParams(audio_out_enabled=True, audio_out_sample_rate=24_000))
    written: list[bytes] = []

    async def main() -> None:
        assert await out.write_audio_frame(OutputAudioRawFrame(b"\0\0" * 240, 24_000, 1)) is True
        out.set_speaker(SimpleNamespace(write=written.append))
        out.get_event_loop = asyncio.get_running_loop
        assert await out.write_audio_frame(OutputAudioRawFrame(b"\1\0" * 240, 24_000, 1)) is True

    asyncio.run(main())
    assert written == [b"\1\0" * 240]


# -- C. the dormant conversation --------------------------------------------------------------------------------

def _conversation(**kw: Any) -> Any:
    from yuki.config import Settings
    from yuki.voice.pipeline import Conversation

    bridge = SimpleNamespace(attach=lambda loop: None, hands=None, open_voice_jobs=lambda: 0)
    conv = Conversation(settings=Settings(voice_hands=False), bridge=bridge, transport=SimpleNamespace(),
                        memory=None, mouth_memory=None, log=SimpleNamespace(event=lambda *a, **k: None),
                        speech_end=lambda: None, on_state=lambda s, d: None, session="t", **kw)
    return conv


def test_a_dormant_conversation_is_ready_only_when_connected_and_is_woken_once() -> None:
    conv = _conversation(dormant=True)
    assert conv.dormant and not conv.ready
    conv._stt = SimpleNamespace(_transport_is_active=lambda: True)
    assert not conv.ready   # connected socket, but no connection seen yet
    conv.stt_ready_at = 1.0

    async def main() -> None:
        conv._loop = asyncio.get_running_loop()
        activated: list[Any] = []

        async def fake_activate(*args: Any) -> None:
            activated.append(args)

        conv._activate = fake_activate
        assert conv.ready
        assert conv.activate(listen_from=5.0, woken_by_name=True, heard_at=6.0) is True
        assert conv.activate(listen_from=5.0) is False   # a second wake never takes it twice
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(activated) == 1 and not conv.dormant

    asyncio.run(main())


def test_a_dormant_conversation_never_binds_the_shared_bridge() -> None:
    conv = _conversation(dormant=True)
    assert conv._bound is False
    bound: list[Any] = []
    conv.bridge.attach = bound.append
    conv._llm = SimpleNamespace(_yuki_call=None, _yuki_spoke=set(), _yuki_early=set())
    conv._llm_context = None
    conv._loop = "loop"
    conv._bind_bridge()
    conv._bind_bridge()
    assert bound == ["loop"]   # once, and only when asked (the woken or awake conversation)


def test_the_stable_layers_are_cached_for_an_hour() -> None:
    from yuki.agent.prompt import STABLE_CACHE_CONTROL, system_blocks
    from yuki.voice.mouth import MouthMemory, mouth_system_blocks

    assert STABLE_CACHE_CONTROL == {"type": "ephemeral", "ttl": "1h"}
    blocks = mouth_system_blocks(MouthMemory(source="memory", standing="- short"), tail=["tail"])
    marked = [b["cache_control"] for b in blocks if "cache_control" in b]
    assert marked and all(m == STABLE_CACHE_CONTROL for m in marked)
    assert all(b["cache_control"] == STABLE_CACHE_CONTROL for b in system_blocks(cacheable=True)
               if "cache_control" in b)
