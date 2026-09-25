"""The wake word: "Yuki", spotted locally on the CPU (docs/VOICE.md §22).

A small streaming keyword spotter (sherpa-onnx, an open-vocabulary zipformer
transducer, int8, one thread) listens to the shared microphone between
conversations. Audio stays in memory: it is fed to the spotter block by block
and dropped; nothing is written or sent anywhere until the name is heard.
Then :class:`WakeListener` calls ``on_wake`` and the conversation starts
(Deepgram, the mouth and the voice), with the last seconds of audio replayed
so what the user said right after the name is heard too.

Open vocabulary means no training: the keyword is written as the model's own
units. The default model (GigaSpeech English, 3.3M parameters) spells words in
BPE pieces, so "Yuki" is ``▁ Y U K I``; the phoneme-based zh-en model was
evaluated too and missed far more. Threshold and boost are settings, tuned by
``scripts/voice_wake_eval.py`` on generated clips (docs/VOICE.md §22).

Decoding only runs while there is sound: a 20 ms block well above the room's
noise floor opens a gate (with half a second of pre-roll, so the start of the
name is not cut) that closes after a second of quiet. A quiet room costs
almost nothing; speech costs about 3.5 % of one core while it lasts.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from yuki.voice.audio import RATE, SharedMic

#: Where voice models live (outside the repo; ``scripts/voice_models.py`` fetches them).
MODELS_DIR = Path.home() / "AppData" / "Local" / "Yuki" / "voice" / "models"


@dataclass(frozen=True)
class KwsModel:
    """One downloadable keyword-spotting model and how "Yuki" is written for it."""

    name: str
    encoder: str
    decoder: str
    joiner: str
    keywords: tuple[str, ...]
    url: str

    def path(self, base: Path = MODELS_DIR) -> Path:
        return base / self.name


#: The candidates evaluated (docs/VOICE.md §22). The first is the default.
MODELS: dict[str, KwsModel] = {
    "gigaspeech-3.3M": KwsModel(
        name="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
        encoder="encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        decoder="decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        joiner="joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        # A second spelling ("▁YOU K I") was tried: it added no hits, only false ones.
        keywords=("▁ Y U K I @YUKI",),
        url="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
            "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
    ),
    "zh-en-3M": KwsModel(
        name="sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
        encoder="encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
        decoder="decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
        joiner="joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
        keywords=("Y UW1 K IY0 @YUKI",),
        url="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
            "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
    ),
}
DEFAULT_MODEL = "gigaspeech-3.3M"


class WakeSpotter:
    """A streaming spotter for "Yuki": feed 16 kHz float32 audio, get ``True`` when it is heard.

    Args:
        model: Key into :data:`MODELS`.
        threshold: Trigger threshold (0-1; higher = stricter).
        boost: Boosting score for the keyword's path.
        base: Models directory.
    """

    def __init__(self, model: str = DEFAULT_MODEL, *, threshold: float = 0.25, boost: float = 2.0,
                 base: Path = MODELS_DIR) -> None:
        import sherpa_onnx

        spec = MODELS[model]
        root = spec.path(base)
        if not (root / spec.encoder).exists():
            raise FileNotFoundError(f"wake-word model missing: {root} (run scripts/voice_models.py)")
        keywords = root / "yuki-keywords.txt"
        keywords.write_text("\n".join(spec.keywords) + "\n", encoding="utf-8")
        self.model = model
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(root / "tokens.txt"),
            encoder=str(root / spec.encoder),
            decoder=str(root / spec.decoder),
            joiner=str(root / spec.joiner),
            keywords_file=str(keywords),
            num_threads=1,
            max_active_paths=4,
            keywords_score=boost,
            keywords_threshold=threshold,
            num_trailing_blanks=1,
            provider="cpu",
        )
        self._stream = self._kws.create_stream()

    def feed(self, samples: np.ndarray) -> str | None:
        """Push audio; returns the keyword heard (``"YUKI"``) or ``None``."""
        self._stream.accept_waveform(RATE, samples)
        heard = None
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            result = self._kws.get_result(self._stream)
            if result:
                heard = result
                self._kws.reset_stream(self._stream)
        return heard

    def reset(self) -> None:
        self._stream = self._kws.create_stream()


class WakeListener:
    """Feeds the shared mic to a :class:`WakeSpotter` on its own thread; calls ``on_wake`` on a hit.

    The mic callback only puts blocks on a queue; decoding (a few ms per
    100 ms of audio) happens here, batched 100 ms at a time. ``pause()``
    stops listening without closing the mic (a conversation is using it).
    """

    #: Decode in 100 ms batches (5 mic blocks): fewer wakeups, same latency to the ear.
    BATCH_BLOCKS = 5
    #: The sound gate: a block this many times above the noise floor (and above
    #: an absolute -55 dBFS) opens it; it closes after this much quiet; this much
    #: audio from before it opened is decoded too.
    GATE_RATIO = 3.0
    GATE_MIN_RMS = 10 ** (-55 / 20)
    GATE_HOLD_BLOCKS = 50   # 1 s of 20 ms blocks
    PREROLL_BLOCKS = 25     # 0.5 s

    def __init__(self, mic: SharedMic, spotter_factory: Callable[[], WakeSpotter],
                 on_wake: Callable[[float, str], None], log: Any = None) -> None:
        self.mic = mic
        self._factory = spotter_factory
        self._on_wake = on_wake
        self._log = log
        self._q: queue.Queue[tuple[bytes, float] | None] = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._token: int | None = None
        self._paused = threading.Event()
        self._pending: list[bytes] = []
        self._pending_at = 0.0
        #: Set once the spotter's model is loaded (measurements wait for it).
        self.ready = threading.Event()
        self.decode_s = 0.0
        self.audio_s = 0.0
        self.heard_s = 0.0  # all audio seen, gated or not

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._paused.clear()
        self._thread = threading.Thread(target=self._run, name="yuki-voice-wake", daemon=True)
        self._thread.start()
        self._token = self.mic.subscribe(self._on_block)

    def _on_block(self, pcm: bytes, at: float) -> None:
        """On the mic's callback thread: collect 100 ms, then hand it over (10 wake-ups a second, not 50)."""
        if self._paused.is_set():
            self._pending.clear()
            return
        if not self._pending:
            self._pending_at = at
        self._pending.append(pcm)
        if len(self._pending) < self.BATCH_BLOCKS:
            return
        chunk, self._pending = b"".join(self._pending), []
        try:
            self._q.put_nowait((chunk, self._pending_at))
        except queue.Full:
            pass  # the spotter fell behind; dropping audio beats growing memory

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        if self._token is not None:
            self.mic.unsubscribe(self._token)
            self._token = None
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run(self) -> None:
        try:
            spotter = self._factory()
        except Exception as exc:  # noqa: BLE001 - reported, the listener just ends
            if self._log is not None:
                self._log.event("wake", state="error", error=f"{type(exc).__name__}: {exc}")
            return
        self.ready.set()
        import collections

        blocks_per_chunk = self.BATCH_BLOCKS
        preroll: collections.deque[np.ndarray] = collections.deque(maxlen=self.PREROLL_BLOCKS // blocks_per_chunk)
        levels: collections.deque[float] = collections.deque(maxlen=250)  # the last 5 s of 20 ms block levels
        floor = self.GATE_MIN_RMS
        quiet = self.GATE_HOLD_BLOCKS + 1  # blocks since the last loud one (the gate starts closed)
        was_paused = False
        while True:
            item = self._q.get()
            if item is None:
                return
            if self._paused.is_set():
                preroll.clear()
                was_paused = True
                continue
            if was_paused:
                spotter.reset()  # no half-heard word from before the conversation
                was_paused = False
            chunk, at = item
            samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
            self.heard_s += samples.size / RATE
            n = samples.size // (RATE // 50)
            rms = np.sqrt(np.mean(samples[: n * (RATE // 50)].reshape(n, -1) ** 2, axis=1)) if n else np.zeros(0)
            gate = max(self.GATE_MIN_RMS, floor * self.GATE_RATIO)
            for level in rms:
                quiet = 0 if level > gate else quiet + 1
            levels.extend(float(x) for x in rms)
            floor = float(np.percentile(levels, 10))  # the room's floor: the quiet end of the last seconds
            if quiet > self.GATE_HOLD_BLOCKS:
                preroll.append(samples)
                continue
            if preroll:
                samples = np.concatenate([*preroll, samples])  # the gate just opened: the word's start is here
                preroll.clear()
            started = time.perf_counter()
            heard = spotter.feed(samples)
            self.decode_s += time.perf_counter() - started
            self.audio_s += samples.size / RATE
            if heard:
                if self._log is not None:
                    self._log.event("wake", state="heard", keyword=heard, model=spotter.model)
                spotter.reset()
                self._on_wake(at, heard)
