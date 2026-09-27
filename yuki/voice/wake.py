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
evaluated too and missed far more (and, as a second-stage check on the same
audio, raised misses from 5 to 20 of 58 while removing few false wakes).
Sound-alikes ("yucky", "you key") are listed as competing variants in the same
search, so the model says which one it heard. Threshold and boost are
settings, tuned by ``scripts/voice_wake_eval.py`` on generated clips
(docs/VOICE.md §22).

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
        # A second spelling of the name ("▁YOU K I") was tried: it added no hits, only false ones.
        # The other two lines are competing variants in the spotter's own search: phrases that
        # sound like the name. When one of them fits the audio better, the spotter reports it
        # instead of YUKI, and that is not a wake (WakeSpotter.feed). Measured 2026-09-25 on
        # 158 clips: false accepts 14 -> 3 of 100 at the same misses (docs/VOICE.md §22).
        keywords=("▁ Y U K I @YUKI", "▁ Y U CK Y @NOT_YUKI", "▁YOU ▁K E Y @NOT_YUKI"),
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

#: Where the words after the name begin, relative to the spotter's time for the name's last token
#: (seconds; the model emits a token on a 40 ms frame, usually a little after the sound ends).
#: Measured 2026-09-27 (v3.6) on 22 one-breath clips ("Yuki, open Chrome.", "Yuki open settings.",
#: four Aura voices) cut there and sent to Flux: at -0.08 and -0.04 s the name was never in the
#: transcript and the first word was kept in 21 of 22 (one voice's "open" heard as "hoping" at every
#: cut); at -0.16 s pieces of the name came through ("key.", "Hopen", "he paused"); from +0.08 s on the
#: first word was cut ("the music", "and settings").
NAME_END_OFFSET_S = -0.06


def ensure_model(key: str = DEFAULT_MODEL, base: Path = MODELS_DIR,
                 progress: "Callable[[int, int], None] | None" = None) -> Path:
    """Download and unpack the wake-word model once (the installed app's first run; ~18 MB).

    ``progress(done_bytes, total_bytes)`` while downloading. Returns the model folder. An existing
    model is left alone. (``scripts/voice_models.py`` does the same for a dev checkout.)
    """
    import tarfile
    import tempfile
    import urllib.request

    spec = MODELS[key]
    target = spec.path(base)
    if (target / spec.encoder).exists():
        return target
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        with urllib.request.urlopen(spec.url, timeout=60) as response, archive.open("wb") as fh:  # noqa: S310
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(base, filter="data")
    return target


class WakeSpotter:
    """A streaming spotter for "Yuki": feed 16 kHz float32 audio, get ``True`` when it is heard.

    Args:
        model: Key into :data:`MODELS`.
        threshold: Trigger threshold (0-1; higher = stricter).
        boost: Boosting score for the keyword's path.
        base: Models directory.
    """

    #: The keyword that is a wake; any other one the model lists is a competing variant.
    WAKE = "YUKI"

    def __init__(self, model: str = DEFAULT_MODEL, *, threshold: float = 0.2, boost: float = 2.0,
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
        #: Competing variants heard instead of the name (a false wake avoided), for the log.
        self.near_misses = 0
        #: The monotonic time of the current stream's first sample (``None``: not known; set by ``feed``).
        self._t0: float | None = None
        #: When the words after the name began (monotonic), for the last name heard: the spotter's own
        #: time for the name's last token plus :data:`NAME_END_OFFSET_S`. ``None`` when not known.
        self.name_end: float | None = None

    def feed(self, samples: np.ndarray, at: float | None = None) -> str | None:
        """Push audio; returns ``"YUKI"`` when the name is heard, else ``None`` (a competing variant counts as none).

        ``at`` is the monotonic time of ``samples``' first sample. With it, a hit also sets
        :attr:`name_end`: where the words said after the name begin, from the model's own token times
        (so speech recognition can be given only what came after the name, v3.6).
        """
        if self._t0 is None and at is not None:
            self._t0 = at
        self._stream.accept_waveform(RATE, samples)
        heard = None
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            result = self._kws.keyword_spotter.get_result(self._stream)
            keyword = (result.keyword or "").strip()
            if keyword:
                if keyword == self.WAKE:
                    heard = keyword
                    times = list(result.timestamps or [])
                    self.name_end = (self._t0 + times[-1] + NAME_END_OFFSET_S
                                     if times and self._t0 is not None else None)
                else:
                    self.near_misses += 1
                # A fresh stream after any result: its token times start again from the next sample
                # (after ``reset_stream`` they drift by the audio still buffered), so the next hit's
                # time is exact.
                self._stream = self._kws.create_stream()
                self._t0 = None if at is None else at + samples.size / RATE
                if heard:
                    break
        return heard

    def reset(self) -> None:
        self._stream = self._kws.create_stream()
        self._t0 = None


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
        self.near_misses = 0  # sound-alikes the spotter heard instead of the name
        self.errors = 0  # batches the spotter failed on (logged; listening goes on)
        #: For the last wake: when the words after the name began (monotonic; the spotter's own token
        #: times, :data:`NAME_END_OFFSET_S`), or ``None`` when the spotter could not tell. Set before
        #: ``on_wake`` is called (v3.6: speech recognition gets only what was said after the name).
        self.name_end: float | None = None

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
        preroll: collections.deque[tuple[np.ndarray, float]] = collections.deque(
            maxlen=self.PREROLL_BLOCKS // blocks_per_chunk)  # (samples, when they began)
        levels: collections.deque[float] = collections.deque(maxlen=250)  # the last 5 s of 20 ms block levels
        floor = self.GATE_MIN_RMS
        quiet = self.GATE_HOLD_BLOCKS + 1  # blocks since the last loud one (the gate starts closed)
        was_paused = False
        decoding = False  # the gate is open and the spotter has been fed since it opened
        # A spotter that takes the batch's start time reports where the name ended (a test's stand-in may not).
        import inspect

        try:
            timed = "at" in inspect.signature(spotter.feed).parameters
        except (TypeError, ValueError):
            timed = False
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
                if decoding:
                    # The sound episode is over: the next one is decoded from a fresh stream (with its
                    # pre-roll). Seconds of other speech left in the stream (a video, Yuki's residual
                    # echo) cost the name right after it: measured 2 of 5 hits without, see §11.
                    spotter.reset()
                    decoding = False
                preroll.append((samples, at))
                continue
            decoding = True
            if preroll:
                # The gate just opened: the word's start is here (and the batch begins where the pre-roll does).
                samples, at = np.concatenate([*(p for p, _ in preroll), samples]), preroll[0][1]
                preroll.clear()
            started = time.perf_counter()
            try:
                heard = spotter.feed(samples, at) if timed else spotter.feed(samples)
            except Exception as exc:  # noqa: BLE001 - one bad batch must not end listening for good
                self.errors += 1
                if self._log is not None and self.errors in (1, 10, 100):
                    self._log.event("wake", state="decode_error", errors=self.errors,
                                    error=f"{type(exc).__name__}: {str(exc)[:160]}")
                spotter.reset()
                continue
            self.decode_s += time.perf_counter() - started
            self.near_misses = spotter.near_misses
            self.audio_s += samples.size / RATE
            if heard:
                end = getattr(spotter, "name_end", None) if timed else None
                self.name_end = float(end) if end is not None else None
                if self._log is not None:
                    self._log.event("wake", state="heard", keyword=heard, model=spotter.model,
                                    name_end_before_hit_ms=(round((time.monotonic() - self.name_end) * 1000)
                                                            if self.name_end is not None else None))
                spotter.reset()
                self._on_wake(at, heard)
