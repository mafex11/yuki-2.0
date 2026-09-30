"""Pipecat transports: the shared microphone and the speaker, and file stand-ins for tests.

Pipecat's own ``LocalAudioTransport`` goes through PyAudio, which can neither
pick Windows' communications input nor ask WASAPI to convert rates in shared
mode; these wrap :class:`yuki.voice.audio.SharedMic` / :class:`Speaker` instead
(docs/VOICE.md §4). The file transports keep every offline test honest: the
same pipeline, real services, audio in from clips on a script and out into WAV
files, paced in real time so turn-taking behaves as it would live. Neither
file transport opens a device.
"""

from __future__ import annotations

import asyncio
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from yuki.voice.audio import BLOCK_MS, BLOCK_SAMPLES, RATE, SharedMic, Speaker

#: Microphone audio goes into the pipeline in frames of this many 20 ms blocks
#: (80 ms): the chunk size Deepgram suggests for Flux, and a quarter of the
#: per-frame work of 20 ms frames while a conversation is open.
FRAME_BLOCKS = 4

#: The kept audio replayed after a wake is squeezed before it goes to speech recognition:
#: Deepgram Flux works through a burst at only ~1.25x real time (measured 2026-09-26: an
#: instant 7 s burst got its end of turn 1.3 s sooner than streaming it live), so every
#: second replayed stays as lag for about four seconds of the conversation. A silence in it
#: longer than this is cut down to it (a pause stays a pause)...
REPLAY_MAX_PAUSE_BLOCKS = 20   # 0.4 s
#: ...and this much of the silence at its end is kept (the live mic carries on from there).
REPLAY_TAIL_BLOCKS = 15        # 0.3 s
#: A kept block is speech when it is this many times above the kept audio's own floor.
REPLAY_VOICE_RATIO = 3.0
REPLAY_MIN_RMS = 10 ** (-55 / 20)


def squeeze_replay(kept: list[tuple[bytes, float]]) -> list[tuple[bytes, float]]:
    """The kept blocks with long silences shortened and the trailing silence trimmed (measured levels only)."""
    if not kept:
        return kept
    rms = np.array([np.sqrt(np.mean((np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0) ** 2))
                    for pcm, _at in kept])
    floor = float(np.percentile(rms, 10))
    voiced = rms > max(REPLAY_MIN_RMS, floor * REPLAY_VOICE_RATIO)
    if not voiced.any():
        return kept[-REPLAY_TAIL_BLOCKS:]
    last = int(np.nonzero(voiced)[0][-1])
    out: list[tuple[bytes, float]] = []
    quiet = 0
    for i, block in enumerate(kept[: last + 1 + REPLAY_TAIL_BLOCKS]):
        quiet = 0 if voiced[i] else quiet + 1
        if quiet <= REPLAY_MAX_PAUSE_BLOCKS:
            out.append(block)
    return out


class MicInput(BaseInputTransport):
    """Input transport fed by a :class:`SharedMic` subscription.

    Args:
        mic: The open shared microphone.
        params: Pipecat transport params (16 kHz mono in).
        backlog_since: Replay the mic's in-memory blocks from this monotonic
            time first (the words said right after the wake word).
        dormant: Send nothing until :meth:`go_live` (v3.11: a conversation built ahead of the wake word waits
            with no microphone subscription at all, so no audio leaves the PC and none is billed).
    """

    def __init__(self, mic: SharedMic, params: TransportParams, *, backlog_since: float | None = None,
                 dormant: bool = False) -> None:
        super().__init__(params)
        self._mic = mic
        self._token: int | None = None
        self._backlog_since = backlog_since
        self._dormant = dormant
        self._loop: Any = None
        #: Seconds of audio kept since the wake, and how much of it was replayed after squeezing.
        self.kept_s = 0.0
        self.replayed_s = 0.0

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Ready first: Pipecat creates the input queue there, and a frame pushed before it fails
        # (v3.3 replayed the kept audio before this call, so a wake-started conversation would
        # have lost everything; found 2026-09-26 by the first end-to-end run through MicInput).
        await self.set_transport_ready(frame)
        if not self._dormant:
            await self._stream(self._backlog_since)

    async def go_live(self, backlog_since: float | None) -> dict[str, Any]:
        """Start sending: the mic's kept blocks since ``backlog_since`` (the words after the name), then the live mic."""
        if not self._dormant:
            return {}
        self._dormant = False
        started = time.monotonic()
        await self._stream(backlog_since)
        return {"kept_s": round(self.kept_s, 2), "replayed_s": round(self.replayed_s, 2),
                "stream_ms": round((time.monotonic() - started) * 1000, 1)}

    async def _stream(self, backlog_since: float | None) -> None:
        loop = self.get_event_loop()
        pending: list[bytes] = []
        gate = threading.Lock()
        held: list[InputAudioRawFrame] | None = [] if backlog_since is not None else None

        def on_block(pcm: bytes, _at: float) -> None:
            nonlocal held
            pending.append(pcm)
            if len(pending) < FRAME_BLOCKS:
                return
            frame_ = InputAudioRawFrame(audio=b"".join(pending), sample_rate=RATE, num_channels=1)
            pending.clear()
            with gate:
                if held is not None:  # still replaying the kept audio: keep the order
                    held.append(frame_)
                    return
            asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame_), loop)

        if backlog_since is not None:
            # Subscribing and taking the kept audio in one step: the words said after the name
            # while the conversation was starting, then the live mic, with no block lost or doubled.
            self._token, kept = self._mic.subscribe_from(on_block, backlog_since)
            self.kept_s = len(kept) * BLOCK_MS / 1000.0
            kept = squeeze_replay(kept)
            self.replayed_s = len(kept) * BLOCK_MS / 1000.0
            for pcm, _at in kept:
                await self.push_audio_frame(InputAudioRawFrame(audio=pcm, sample_rate=RATE, num_channels=1))
            while True:
                with gate:
                    batch, held = held, ([] if held else None)
                if not batch:
                    break
                for frame_ in batch:
                    await self.push_audio_frame(frame_)
        else:
            self._token = self._mic.subscribe(on_block)

    async def cleanup(self) -> None:
        if self._token is not None:
            self._mic.unsubscribe(self._token)
            self._token = None
        await super().cleanup()


class SpeakerOutput(BaseOutputTransport):
    """Output transport writing to a :class:`Speaker` (blocking writes on one worker thread)."""

    def __init__(self, speaker: Speaker | None, params: TransportParams) -> None:
        super().__init__(params)
        #: ``None`` until a dormant conversation is woken (v3.11: no output stream is held open while it waits, so
        #: the PC can sleep and no audio session shows); :meth:`set_speaker` gives it the one opened then.
        self._speaker = speaker
        # The writer thread. v3.11-v3.14b made it only in set_speaker (the woken standby's path), so a conversation
        # started with its speaker (no standby ready) failed every audio write: live 2026-09-30 three of seven
        # conversations were mute ("'SpeakerOutput' object has no attribute '_executor'", 615 frames), and the
        # user's "Oh, answer, what the fuck?" was said to a reply he never heard (v3.15).
        self._executor: ThreadPoolExecutor | None = None
        if speaker is not None:
            self._start_writer()

    def _start_writer(self) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yuki-voice-out")
            self._executor.submit(lambda: None)   # its thread starts now, not on the loop's first write (v3.14)

    def set_speaker(self, speaker: Speaker) -> None:
        self._speaker = speaker
        self._start_writer()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        speaker = self._speaker
        if speaker is None:
            return True   # dormant: nothing is ever said before the wake, but never fail the pipeline on it
        if self._executor is None:
            self._start_writer()
        await self.get_event_loop().run_in_executor(self._executor, speaker.write, frame.audio)
        return True

    async def cleanup(self) -> None:
        try:
            await super().cleanup()
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=False)


class PairTransport(BaseTransport):
    """An input and an output processor as one Pipecat transport."""

    def __init__(self, input_: FrameProcessor, output: FrameProcessor) -> None:
        super().__init__()
        self._in = input_
        self._out = output

    def input(self) -> FrameProcessor:
        return self._in

    def output(self) -> FrameProcessor:
        return self._out


# ---------------------------------------------------------------------------
# Offline: clips in on a script, WAV out, real-time paced
# ---------------------------------------------------------------------------


def load_clip(path: Path) -> np.ndarray:
    """A WAV clip as float32 mono at 16 kHz (resampled if needed)."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: {8 * width}-bit WAV; 16-bit expected")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        import soxr

        data = soxr.resample(data, rate, RATE).astype(np.float32)
    return data


def speech_end_offset(clip: np.ndarray, *, floor_dbfs: float = -40.0) -> float:
    """Seconds from the clip's start to its last voiced 20 ms block (trailing silence ignored)."""
    floor = 10 ** (floor_dbfs / 20.0)
    blocks = len(clip) // BLOCK_SAMPLES
    last = 0
    for i in range(blocks):
        chunk = clip[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES]
        if float(np.sqrt(np.mean(chunk * chunk))) >= floor:
            last = i + 1
    return last * BLOCK_MS / 1000.0


@dataclass
class ScriptLine:
    """One utterance of an offline script.

    Attributes:
        clip: The audio (float32 mono 16 kHz).
        label: Name for the report (e.g. the clip file).
        after_s: Start this many seconds after the previous line ends ...
        wait_for: ... or, when set, once this condition (a callable returning
            True, checked every block) holds, then ``after_s`` more.
    """

    clip: np.ndarray
    label: str
    after_s: float = 1.0
    wait_for: Callable[[], bool] | None = None
    started_at: float | None = None
    speech_end_at: float | None = None


@dataclass
class Script:
    """The whole offline input: lines in order, then silence until the session ends."""

    lines: list[ScriptLine]
    noise_dbfs: float = -60.0
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ScriptedInput(BaseInputTransport):
    """Plays a :class:`Script` into the pipeline as 20 ms blocks, in real time.

    Between lines it sends low-level noise (a real mic is never digital
    silence). Also feeds each block to an optional ``tap`` (the voice log's
    end-of-speech clock, the conversation recorder).
    """

    def __init__(self, script: Script, params: TransportParams, *, tap: Callable[[np.ndarray, float], None] | None = None) -> None:
        super().__init__(params)
        self.script = script
        self._tap = tap
        self._task: asyncio.Task | None = None
        self._pending: list[bytes] = []

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._task = self.create_task(self._play(), "scripted-input")

    async def cleanup(self) -> None:
        if self._task is not None:
            await self.cancel_task(self._task)
            self._task = None
        await super().cleanup()

    async def _send(self, block: np.ndarray, at: float) -> None:
        if self._tap is not None:
            self._tap(block, at)
        self._pending.append((np.clip(block, -1, 1) * 32767).astype("<i2").tobytes())
        if len(self._pending) >= FRAME_BLOCKS:
            pcm, self._pending = b"".join(self._pending), []
            await self.push_audio_frame(InputAudioRawFrame(audio=pcm, sample_rate=RATE, num_channels=1))

    async def _play(self) -> None:
        rng = np.random.default_rng(7)
        noise_amp = 10 ** (self.script.noise_dbfs / 20.0)
        clock = time.monotonic()

        async def tick(block: np.ndarray) -> None:
            nonlocal clock
            await self._send(block, clock)
            clock += BLOCK_MS / 1000.0
            if self._pending:
                return  # a frame is still filling
            delay = clock - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)  # pacing a stand-in device clock, not a settle wait

        def noise() -> np.ndarray:
            return (rng.standard_normal(BLOCK_SAMPLES) * noise_amp).astype(np.float32)

        for line in self.script.lines:
            if line.wait_for is not None:
                while not line.wait_for():
                    await tick(noise())
            for _ in range(int(line.after_s * 1000 / BLOCK_MS)):
                await tick(noise())
            line.started_at = clock
            end = speech_end_offset(line.clip)
            line.speech_end_at = clock + end
            clip = line.clip
            pad = (-len(clip)) % BLOCK_SAMPLES
            clip = np.concatenate([clip, np.zeros(pad, np.float32)])
            for i in range(len(clip) // BLOCK_SAMPLES):
                await tick(clip[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES] + noise())
        self.script.done.set()
        while True:
            await tick(noise())


class WavOutput(BaseOutputTransport):
    """Output transport that "plays" into memory at real-time pace and writes WAVs.

    ``timeline`` holds every output sample at its wall-clock position, so a
    conversation WAV can be built with the user's clips on the other channel.
    ``segments`` holds each stretch of speech (a reply) separately.
    """

    def __init__(self, params: TransportParams, *, rate: int) -> None:
        super().__init__(params)
        self.rate = rate
        self.t0 = time.monotonic()
        self.chunks: list[tuple[float, bytes]] = []
        self._clock: float | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        now = time.monotonic()
        if self._clock is None or self._clock < now:
            self._clock = now
        audio = frame.audio
        if frame.sample_rate != self.rate:
            import soxr

            data = np.frombuffer(audio, dtype="<i2").astype(np.float32)
            audio = soxr.resample(data, frame.sample_rate, self.rate).astype("<i2").tobytes()
        if any(audio):
            self.chunks.append((self._clock, audio))
        self._clock += len(audio) / 2 / self.rate
        delay = self._clock - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)  # the stand-in device plays in real time
        return True

    def replies(self, gap_s: float = 0.6) -> list[tuple[float, np.ndarray]]:
        """Stretches of output separated by at least ``gap_s`` of nothing: ``[(start, samples)]``."""
        out: list[tuple[float, list[bytes]]] = []
        last_end = -1e9
        for at, audio in self.chunks:
            if at - last_end > gap_s or not out:
                out.append((at, []))
            out[-1][1].append(audio)
            last_end = at + len(audio) / 2 / self.rate
        return [(at, np.frombuffer(b"".join(parts), dtype="<i2")) for at, parts in out]


def write_wav(path: Path, samples: np.ndarray, rate: int, channels: int = 1) -> None:
    """16-bit PCM WAV (``samples`` int16, interleaved when ``channels`` > 1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def conversation_wav(path: Path, script: Script, output: WavOutput, *, rate: int) -> None:
    """Stereo WAV of the whole run: left = the user's clips, right = Yuki, on one clock."""
    ends = [output.t0]
    for line in script.lines:
        if line.started_at is not None:
            ends.append(line.started_at + len(line.clip) / RATE)
    for at, audio in output.chunks:
        ends.append(at + len(audio) / 2 / rate)
    start = min([output.t0] + [line.started_at for line in script.lines if line.started_at])
    total = int((max(ends) - start + 0.5) * rate)
    left = np.zeros(total, np.float32)
    right = np.zeros(total, np.float32)
    for line in script.lines:
        if line.started_at is None:
            continue
        clip = line.clip
        if rate != RATE:
            import soxr

            clip = soxr.resample(clip, RATE, rate).astype(np.float32)
        i = int((line.started_at - start) * rate)
        left[i:i + len(clip)] += clip[: max(0, total - i)]
    for at, audio in output.chunks:
        data = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
        i = int((at - start) * rate)
        right[i:i + len(data)] += data[: max(0, total - i)]
    stereo = np.stack([left, right], axis=1).reshape(-1)
    write_wav(path, (np.clip(stereo, -1, 1) * 32767).astype("<i2"), rate, channels=2)


__all__ = [
    "MicInput", "SpeakerOutput", "PairTransport", "ScriptedInput", "Script", "ScriptLine", "WavOutput",
    "load_clip", "speech_end_offset", "write_wav", "conversation_wav",
]
