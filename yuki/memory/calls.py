"""Calls, "just my side": remember what the user said in a call, from their own microphone only.

Decided with the owner (docs/MEMORY.md, "Calls: just my side"): during a call
Yuki may transcribe ONLY the user's microphone - never the call's audio, never
the other participants - on this PC, and only when the user said yes for this
call (or "always" for the app). Nothing is ever inferred about what anyone
else said.

One thread in ``yuki-memory`` (:class:`CallWorker`, ``yuki-memory-calls``):

1. **Detect.** Every ``poll_s`` it reads, content-free, which apps hold the
   microphone (the ConsentStore facts of ``activity_facts()["microphone"]``,
   Yuki's own processes left out) and whether the timeline has a meeting row
   in front. A call is a communication app of the privacy file's ``[calls]
   apps`` holding the microphone, or a meeting row (``[meetings]``). It ends
   once neither has been seen for ``end_grace_s``.
2. **Ask, every call.** A ``calls`` row with status ``asking`` and the named
   event ``Local\\YukiCallReady`` for the UI, which shows a glass card
   (:mod:`yuki.ui.calls`): Yes / No / Always for <app> / Never for <app>. The
   answer comes back through the store (:meth:`Store.answer_call`) and the
   event :func:`yuki.memory.store.call_event_name`. "Always"/"Never" are kept
   per app (``call_prefs``) and answer later calls without a card. No answer
   (card timed out, UI not running, call over) = nothing recorded. While
   nudges are snoozed no card is shown (``not_asked``); memory's pause stops
   everything.
3. **Headphones only.** Before and during recording the default output
   devices' form factor is read (Core Audio, :mod:`yuki.perception.audio`);
   unless it is headphones/headset (or a device the user listed as
   headphones), the microphone is not opened (``waiting_headphones``) and the
   card says why.
4. **Transcribe here.** Shared-mode WASAPI capture of the input device the
   call app records from, in memory only (never written to disk); a simple
   energy gate drops silence; speech is cut into rolling chunks at pauses and
   transcribed by a local backend (:func:`make_backend`, ``call_stt_model``:
   faster-whisper on CUDA, else the CPU fallback). Only text is kept: every
   ``flush_every_s`` and at the end, the utterances go to the store as a
   capture of kind ``my_call_side`` (encrypted), which the journal turns into
   facts and the user's own commitments (:mod:`yuki.memory.journal`).

Logs are content-free: ``call_detected``, ``call_consent``,
``call_recording_start``/``call_recording_stop``, ``call_chunk`` (audio and
speech seconds, ms, device, GPU load), never words.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.store import (
    CALL_OPEN_STATUSES,
    CALL_READY_EVENT,
    Store,
    app_key,
    call_event_name,
    signal_event,
)

RATE = 16_000
FRAME_S = 0.03

# ---------------------------------------------------------------------------
# Configuration: the privacy file's [calls] table (data, not code paths)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallConfig:
    enabled: bool = True
    apps: frozenset[str] = frozenset()
    names: tuple[tuple[str, str], ...] = ()
    poll_s: float = 5.0
    end_grace_s: float = 30.0
    ask_timeout_s: float = 120.0
    headphone_form_factors: tuple[str, ...] = ("headphones", "headset", "handset")
    headphone_devices: tuple[str, ...] = ()
    capture_device: str = ""
    stt_model: str = "faster-whisper:large-v3"
    stt_fallback: str = "faster-whisper:small"
    model_dir: str = ""
    languages: tuple[str, ...] = ()
    gate_dbfs: float = -45.0
    min_speech_s: float = 0.5
    chunk_min_s: float = 6.0
    chunk_max_s: float = 25.0
    pause_s: float = 0.7
    flush_every_s: float = 300.0

    @classmethod
    def from_dict(cls, table: dict | None) -> CallConfig:
        t = table or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(t.get(key, default))
            except (TypeError, ValueError):
                return default

        def strings(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = t.get(key)
            if not isinstance(raw, list):
                return default
            return tuple(str(x).strip() for x in raw if str(x).strip())

        apps = frozenset(k for k in (app_key(a) for a in strings("apps", ())) if k)
        names = tuple((app_key(k) or "", str(v)) for k, v in (t.get("names") or {}).items() if str(v).strip())
        return cls(
            enabled=bool(t.get("enabled", d.enabled)), apps=apps, names=names,
            poll_s=max(1.0, num("poll_s", d.poll_s)), end_grace_s=max(0.0, num("end_grace_s", d.end_grace_s)),
            ask_timeout_s=max(10.0, num("ask_timeout_s", d.ask_timeout_s)),
            headphone_form_factors=tuple(s.casefold() for s in strings("headphone_form_factors",
                                                                        d.headphone_form_factors)),
            headphone_devices=strings("headphone_devices", ()),
            capture_device=str(t.get("capture_device") or "").strip(),
            stt_model=str(t.get("call_stt_model") or d.stt_model).strip(),
            stt_fallback=str(t.get("call_stt_fallback") or d.stt_fallback).strip(),
            model_dir=str(t.get("model_dir") or "").strip(),
            languages=tuple(s.casefold() for s in strings("languages", ())),
            gate_dbfs=num("gate_dbfs", d.gate_dbfs), min_speech_s=num("min_speech_s", d.min_speech_s),
            chunk_min_s=num("chunk_min_s", d.chunk_min_s), chunk_max_s=max(2.0, num("chunk_max_s", d.chunk_max_s)),
            pause_s=num("pause_s", d.pause_s), flush_every_s=max(10.0, num("flush_every_s", d.flush_every_s)),
        )

    def name_of(self, key: str | None, fallback: str = "") -> str:
        return next((v for k, v in self.names if k == key), None) or fallback or (key or "the call")

    def models_path(self) -> Path:
        if self.model_dir:
            return Path(os.path.expandvars(self.model_dir))
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Yuki" / "models" / "whisper"


# ---------------------------------------------------------------------------
# Content-free facts: who holds the microphone, what the output is
# ---------------------------------------------------------------------------


def own_executables() -> frozenset[str]:
    """Image paths of this process and its interpreter(s), casefolded: never a call app.

    A uv/venv ``python.exe`` launches the base interpreter, which is the
    process that holds the microphone, so both are listed.
    """
    paths = {sys.executable, getattr(sys, "_base_executable", "") or ""}
    try:
        import psutil

        me = psutil.Process()
        paths.add(me.exe())
        parent = me.parent()
        if parent is not None and Path(parent.exe()).name.lower().startswith("python"):
            paths.add(parent.exe())
    except Exception:
        pass
    out = set()
    for p in paths:
        if p:
            out.add(str(Path(p)).casefold())
            stem = Path(p)
            for variant in ("python.exe", "pythonw.exe"):   # the UI runs under pythonw
                out.add(str(stem.with_name(variant)).casefold())
    return frozenset(out)


def mic_holders(exclude_paths: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Apps using the microphone now (ConsentStore; ``activity_facts()["microphone"]``), minus ``exclude_paths``."""
    import psutil

    from yuki.perception import system as _system

    processes = _system._process_entries()
    boot_ft = int(psutil.boot_time() * 1e7) + _system._FILETIME_UNIX_EPOCH
    try:
        users = _system._device_users("microphone", processes, boot_ft)
    except FileNotFoundError:
        return []
    return [u for u in users if not (u.get("path") and str(u["path"]).casefold() in exclude_paths)]


def headphones_now(cfg: CallConfig) -> tuple[bool, dict[str, Any]]:
    """Whether every default output is headphones (see :func:`yuki.perception.audio.output_is_headphones`)."""
    from yuki.perception import audio

    return audio.output_is_headphones(form_factors=cfg.headphone_form_factors,
                                      known_headphones=cfg.headphone_devices)


def capture_device_for(cfg: CallConfig, app: str | None) -> str | None:
    """The input device to record: ``capture_device``, else the one the call app records from, else the
    default communications input (friendly names; None = the system default)."""
    if cfg.capture_device:
        return cfg.capture_device
    from yuki.perception import audio

    try:
        if app:
            import psutil

            pids = {p.pid for p in psutil.process_iter(["name"])
                    if app_key(p.info.get("name")) == app_key(app)}
            for s in audio.capture_sessions():
                if s["state"] == "active" and s["pid"] in pids:
                    return s["device"]
        comms = (audio.default_endpoints().get("capture") or {}).get("communications")
        return comms["name"] if comms else None
    except Exception:
        return None


class Gpu:
    """NVML (``nvml.dll`` from the driver) for GPU load and memory; silent when absent."""

    class _Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class _Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    def __init__(self) -> None:
        self._dll = None
        self._handle = ctypes.c_void_p()
        try:
            dll = ctypes.WinDLL("nvml.dll")
            if dll.nvmlInit_v2() == 0 and dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._handle)) == 0:
                self._dll = dll
        except OSError:
            self._dll = None

    def snapshot(self) -> dict[str, Any] | None:
        if self._dll is None:
            return None
        util, mem = self._Util(), self._Mem()
        if self._dll.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(util)) != 0:
            return None
        self._dll.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(mem))
        return {"gpu_util": int(util.gpu), "vram_used_mb": round(mem.used / 2**20), "vram_total_mb": round(mem.total / 2**20)}


# ---------------------------------------------------------------------------
# Audio: sources (in memory only) and the energy-gated chunker
# ---------------------------------------------------------------------------


class MicSource:
    """Shared-mode WASAPI capture of one input device as 16 kHz mono float32 blocks.

    Blocks go to an in-memory queue as ``(wall time of the first sample,
    samples)``; nothing is written anywhere. Shared mode only: the device
    stays usable by the call app and everything else at the same time. Of a
    multi-channel input the loudest channel of each block is kept (an audio
    interface's mic is usually on one input).
    """

    def __init__(self, device: str | None = None, *, block_s: float = 0.1) -> None:
        self.device = device
        self.block_s = block_s
        self._q: queue.Queue = queue.Queue()
        self._stream: Any = None
        self._resampler: Any = None
        self._rate = RATE
        self.device_used: str | None = None
        self.opened_as: str | None = None
        self.overflows = 0

    def _index(self, sd: Any) -> int | None:
        apis = [i for i, a in enumerate(sd.query_hostapis()) if "wasapi" in a["name"].lower()]
        if not apis:
            return None
        api = apis[0]
        devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] == api and d["max_input_channels"] > 0]
        if self.device:
            want = self.device.casefold()
            for i, d in devices:
                if d["name"].casefold() == want:
                    return i
            for i, d in devices:
                if want in d["name"].casefold() or d["name"].casefold() in want:
                    return i
        default = sd.query_hostapis(api)["default_input_device"]
        return default if default is not None and default >= 0 else None

    def start(self) -> None:
        import sounddevice as sd

        index = self._index(sd)
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        self.device_used = info["name"]
        channels = max(1, min(2, int(info["max_input_channels"])))

        def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status and status.input_overflow:
                self.overflows += 1
            self._q.put((time.time() - frames / float(self._rate), indata.copy()))

        try:
            self._rate = RATE
            self._stream = sd.InputStream(device=index, samplerate=RATE, channels=channels, dtype="float32",
                                          blocksize=int(RATE * self.block_s), callback=callback,
                                          extra_settings=sd.WasapiSettings(auto_convert=True))
            self.opened_as = "wasapi_shared_auto_convert"
        except Exception:
            import av

            self._rate = int(info["default_samplerate"])
            self._resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
            self._stream = sd.InputStream(device=index, samplerate=self._rate, channels=channels, dtype="float32",
                                          blocksize=int(self._rate * self.block_s), callback=callback)
            self.opened_as = "wasapi_shared_native_resampled"
        self._stream.start()

    def _mono(self, block: np.ndarray) -> np.ndarray:
        if block.ndim == 2 and block.shape[1] > 1:
            loudest = int(np.argmax(np.sqrt(np.mean(block.astype(np.float64) ** 2, axis=0))))
            block = block[:, loudest]
        mono = np.ascontiguousarray(block.reshape(-1), dtype=np.float32)
        if self._resampler is None:
            return mono
        import av

        frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = self._rate
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)

    def read(self, timeout_s: float) -> tuple[float, np.ndarray] | None:
        try:
            at, block = self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None
        return at, self._mono(block)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        while not self._q.empty():   # drop what was not read: nothing is kept
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


class ArraySource:
    """An in-memory recording played as if live (offline checks with synthetic WAVs): the same
    ``start``/``read``/``stop`` as :class:`MicSource`, ``realtime=False`` delivers as fast as read."""

    def __init__(self, audio: np.ndarray, *, start_at: float | None = None, block_s: float = 0.1,
                 realtime: bool = False) -> None:
        self.audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.start_at = time.time() if start_at is None else float(start_at)
        self.block = int(RATE * block_s)
        self.realtime = realtime
        self.pos = 0
        self.device_used = "array"
        self.opened_as = "array"
        self.overflows = 0
        self.done = threading.Event()
        self._idle = threading.Event()

    def start(self) -> None:
        self._t0 = time.monotonic()

    def read(self, timeout_s: float) -> tuple[float, np.ndarray] | None:
        if self.pos >= len(self.audio):
            self.done.set()
            self._idle.wait(timeout_s)   # like an empty queue.get(timeout): nothing more is coming
            return None
        if self.realtime:
            due = self._t0 + self.pos / RATE
            delay = due - time.monotonic()
            if delay > timeout_s:
                return None
            if delay > 0:
                threading.Event().wait(delay)
        block = self.audio[self.pos:self.pos + self.block]
        at = self.start_at + self.pos / RATE
        self.pos += len(block)
        return at, block

    def stop(self) -> None:
        pass


@dataclass
class Chunk:
    audio: np.ndarray
    start_at: float
    speech_s: float

    @property
    def seconds(self) -> float:
        return len(self.audio) / RATE


class Chunker:
    """Energy gate + rolling chunks: speech is cut at pauses, silence never becomes a chunk.

    30 ms frames under ``gate_dbfs`` are silence. While nothing voiced has
    come, silence is dropped as it arrives (a short pre-roll is kept). A chunk
    ends at a pause of ``pause_s`` once it is ``chunk_min_s`` long, after a
    pause of ``4 * pause_s`` whatever its length (a short "yes"), or at
    ``chunk_max_s``. A chunk with less than ``min_speech_s`` of voiced frames
    is counted and dropped, never transcribed.
    """

    def __init__(self, cfg: CallConfig) -> None:
        self.cfg = cfg
        self.frame = int(RATE * FRAME_S)
        self._buf: list[np.ndarray] = []
        self._voiced: list[bool] = []
        self._start_at: float | None = None
        self._rest = np.zeros(0, np.float32)
        self._rest_at: float | None = None
        self.silent_dropped = 0
        self.silence_s = 0.0

    def _frames(self, at: float, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        if self._rest.size:
            samples = np.concatenate([self._rest, samples])
            at = self._rest_at if self._rest_at is not None else at
        n = len(samples) // self.frame
        frames = [(at + i * FRAME_S, samples[i * self.frame:(i + 1) * self.frame]) for i in range(n)]
        self._rest = samples[n * self.frame:]
        self._rest_at = at + n * FRAME_S
        return frames

    def feed(self, at: float, samples: np.ndarray) -> list[Chunk]:
        out: list[Chunk] = []
        preroll = max(1, int(0.3 / FRAME_S))
        for t, frame in self._frames(at, samples):
            rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) + 1e-12
            voiced = 20.0 * np.log10(rms) >= self.cfg.gate_dbfs
            if not self._buf:
                self._start_at = t
            self._buf.append(frame)
            self._voiced.append(voiced)
            if not any(self._voiced):
                if len(self._buf) > preroll:           # silence before speech: dropped as it comes
                    self._buf.pop(0)
                    self._voiced.pop(0)
                    self._start_at = (self._start_at or t) + FRAME_S
                    self.silence_s += FRAME_S
                continue
            length = len(self._buf) * FRAME_S
            trailing = 0
            for v in reversed(self._voiced):
                if v:
                    break
                trailing += 1
            pause = trailing * FRAME_S
            if (length >= self.cfg.chunk_max_s or (length >= self.cfg.chunk_min_s and pause >= self.cfg.pause_s)
                    or pause >= 4 * self.cfg.pause_s):
                chunk = self._cut(keep_tail=min(trailing, preroll))
                if chunk is not None:
                    out.append(chunk)
        return out

    def _cut(self, keep_tail: int) -> Chunk | None:
        trailing = 0
        for v in reversed(self._voiced):
            if v:
                break
            trailing += 1
        end = len(self._buf) - max(0, trailing - keep_tail)
        audio = np.concatenate(self._buf[:end]) if end > 0 else np.zeros(0, np.float32)
        speech = sum(self._voiced[:end]) * FRAME_S
        start = self._start_at or time.time()
        self.silence_s += (len(self._buf) - end) * FRAME_S
        self._buf, self._voiced, self._start_at = [], [], None
        if speech < self.cfg.min_speech_s:
            self.silent_dropped += 1
            return None
        return Chunk(audio=audio, start_at=start, speech_s=speech)

    def flush(self) -> list[Chunk]:
        if not self._buf or not any(self._voiced):
            self._buf, self._voiced, self._start_at = [], [], None
            return []
        chunk = self._cut(keep_tail=0)
        return [chunk] if chunk is not None else []


# ---------------------------------------------------------------------------
# Speech to text: local backends, pluggable by name (call_stt_model)
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: float
    end: float
    text: str
    lang: str | None = None


def _add_cuda_dirs() -> list[str]:
    """Make cuBLAS/cuDNN findable for CTranslate2: the CUDA toolkit's bin and NVIDIA pip wheels' bin dirs."""
    added = []
    candidates = []
    cuda = os.environ.get("CUDA_PATH")
    if cuda:
        candidates.append(Path(cuda) / "bin")
    try:
        import nvidia  # type: ignore[import-not-found]

        for base in getattr(nvidia, "__path__", []):
            candidates += [p / "bin" for p in Path(base).iterdir() if (p / "bin").is_dir()]
    except Exception:
        pass
    for path in candidates:
        if path.is_dir():
            try:
                os.add_dll_directory(str(path))
                added.append(str(path))
            except OSError:
                pass
    return added


class FasterWhisperBackend:
    """``faster-whisper:<model>``: CTranslate2 Whisper, int8 (int8_float16 on CUDA)."""

    def __init__(self, model: str, *, device: str, model_dir: Path, languages: Sequence[str] = (),
                 cpu_threads: int = 4) -> None:
        self.model_name = model
        self.device = device
        self.model_dir = Path(model_dir)
        self.languages = tuple(languages)
        self.cpu_threads = cpu_threads
        self.model: Any = None
        self.load_ms = 0.0

    @property
    def name(self) -> str:
        return f"faster-whisper:{self.model_name}"

    def load(self) -> None:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        if self.device == "cuda":
            _add_cuda_dirs()
        compute = "int8_float16" if self.device == "cuda" else "int8"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.model = WhisperModel(self.model_name, device=self.device, compute_type=compute,
                                  download_root=str(self.model_dir), cpu_threads=self.cpu_threads)
        # one tiny pass: a missing CUDA library shows here, not mid-call
        list(self.model.transcribe(np.zeros(RATE // 2, np.float32), language="en", beam_size=1)[0])
        self.load_ms = (time.perf_counter() - t0) * 1000.0

    def transcribe(self, audio: np.ndarray) -> tuple[list[Segment], dict[str, Any]]:
        # beam 5 on the GPU; greedy on the CPU, where beam search costs ~1.6x for the same words (measured)
        kwargs = dict(beam_size=5 if self.device == "cuda" else 1, vad_filter=True,
                      condition_on_previous_text=False, task="transcribe")
        segments, info = self.model.transcribe(audio, language=None, **kwargs)
        lang = info.language
        segs = list(segments)
        if self.languages and lang not in self.languages:
            probs = dict(info.all_language_probs or [])
            best = max(self.languages, key=lambda code: probs.get(code, 0.0))
            segments, info = self.model.transcribe(audio, language=best, **kwargs)
            segs, lang = list(segments), best
        out = []
        for s in segs:
            text = (s.text or "").strip()
            if not text or (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0):
                continue   # Whisper's own no-speech rule: silence it "heard" words in
            out.append(Segment(float(s.start), float(s.end), text, lang))
        return out, {"lang": lang, "lang_prob": round(float(info.language_probability or 0.0), 3)}

    def close(self) -> None:
        self.model = None
        import gc

        gc.collect()


def make_backend(spec: str, *, device: str, model_dir: Path, languages: Sequence[str] = ()) -> Any:
    """A speech-to-text backend by name: ``faster-whisper:<model>`` (``large-v3``, ``large-v3-turbo``, ``small``, ...).

    Raises ``ValueError`` for an unknown backend. (Handy's Nemotron 3.5 GGUF
    needs transcribe.cpp, which has no Python binding, and does not cover
    Kannada, Tamil or Telugu: docs/MEMORY.md.)
    """
    kind, _, model = (spec or "").partition(":")
    if kind == "faster-whisper" and model:
        return FasterWhisperBackend(model, device=device, model_dir=model_dir, languages=languages)
    raise ValueError(f"unknown speech-to-text backend {spec!r}")


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def load_backend(cfg: CallConfig, log: Callable[..., Any] | None = None) -> Any:
    """``call_stt_model`` on CUDA when there is a GPU, else (or when that fails) ``call_stt_fallback`` on the CPU."""
    attempts = []
    if cuda_available():
        attempts.append((cfg.stt_model, "cuda"))
    attempts.append((cfg.stt_fallback or cfg.stt_model, "cpu"))
    last: Exception | None = None
    for spec, device in attempts:
        try:
            backend = make_backend(spec, device=device, model_dir=cfg.models_path(), languages=cfg.languages)
            backend.load()
            return backend
        except Exception as exc:
            last = exc
            if log is not None:
                log("call_stt_load_failed", model=spec, device=device, error=f"{type(exc).__name__}: {exc}")
    raise RuntimeError(f"no speech-to-text backend could load: {last}")


# ---------------------------------------------------------------------------
# One recording: source -> chunker -> backend -> utterances -> store
# ---------------------------------------------------------------------------


@dataclass
class RecordingStats:
    chunks: int = 0
    chunks_silent: int = 0
    audio_s: float = 0.0
    speech_s: float = 0.0
    utterances: int = 0
    captures: int = 0
    transcribe_ms: list[float] = field(default_factory=list)
    rtf: list[float] = field(default_factory=list)
    cpu_s: float = 0.0
    gpu_util_max: int | None = None
    vram_used_mb_max: int | None = None
    errors: int = 0


class RecordingSession:
    """Records one call's user side on its own thread; :meth:`pause` closes the microphone, :meth:`stop` ends it.

    The backend is loaded at the first chunk with speech (a silent call never
    loads a model); audio waits in memory meanwhile. Utterances are handed to
    the store every ``flush_every_s`` and at the end; audio never is.
    """

    def __init__(self, store: Store, *, call_id: int, app: str, process: str | None, cfg: CallConfig,
                 source_factory: Callable[[], Any], backend: Callable[[], Any], log: Callable[..., Any],
                 gpu: Gpu | None = None) -> None:
        self.store = store
        self.call_id = call_id
        self.app = app
        self.process = process
        self.cfg = cfg
        self._source_factory = source_factory
        self._backend_fn = backend
        self.backend: Any = None
        self.log = log
        self.gpu = gpu
        self.chunker = Chunker(cfg)
        self.stats = RecordingStats()
        self._pending: list[dict[str, Any]] = []
        self._pending_since = time.time()
        self._source: Any = None
        self._source_lock = threading.Lock()
        self._stop = threading.Event()
        self._drain = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self.device: str | None = None
        self.opened_as: str | None = None
        self.stop_reason: str | None = None

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        self.resume()
        self._thread = threading.Thread(target=self._run, name="yuki-memory-call-rec", daemon=True)
        self._thread.start()

    @property
    def listening(self) -> bool:
        return self._source is not None

    def resume(self) -> None:
        with self._source_lock:
            if self._source is None:
                source = self._source_factory()
                source.start()
                self.device = getattr(source, "device_used", None)
                self.opened_as = getattr(source, "opened_as", None)
                self._source = source

    def pause(self) -> None:
        """Close the microphone now (output left headphones); what was heard so far is still transcribed."""
        with self._source_lock:
            source, self._source = self._source, None
        if source is not None:
            source.stop()

    def stop(self, reason: str, *, timeout_s: float = 120.0) -> RecordingStats:
        """Close the microphone, transcribe what is left, hand the rest to the store; returns the stats."""
        self.stop_reason = reason
        self.pause()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass
        return self.stats

    def wait_drained(self, timeout_s: float) -> bool:
        """Offline checks: wait until an :class:`ArraySource` was read to its end."""
        return self._drain.wait(timeout_s)

    # -- thread --------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                source = self._source
                if source is None:
                    self._stop.wait(0.25)
                    self._maybe_flush(final=False)
                    continue
                got = source.read(0.25)
                if got is None:
                    if getattr(source, "done", None) is not None and source.done.is_set():
                        for chunk in self.chunker.flush():
                            self._transcribe(chunk)
                        self._drain.set()
                    self._maybe_flush(final=False)
                    continue
                at, samples = got
                for chunk in self.chunker.feed(at, samples):
                    self._transcribe(chunk)
                self._maybe_flush(final=False)
            for chunk in self.chunker.flush():
                self._transcribe(chunk)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.recording", error=f"{type(exc).__name__}: {exc}",
                     traceback=traceback.format_exc())
        finally:
            self._maybe_flush(final=True)

    def _transcribe(self, chunk: Chunk) -> None:
        import psutil

        self.stats.chunks_silent = self.chunker.silent_dropped
        if self.backend is None:
            self.backend = self._backend_fn()
            self.log("call_stt_ready", call_id=self.call_id, stt_model=self.backend.name, stt_device=self.backend.device,
                     load_ms=round(getattr(self.backend, "load_ms", 0.0)))
            self.store.update_call(self.call_id, stt_model=self.backend.name, stt_device=self.backend.device)
        proc = psutil.Process()
        c0 = sum(proc.cpu_times()[:2])
        t0 = time.perf_counter()
        try:
            segments, info = self.backend.transcribe(chunk.audio)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.transcribe", error=f"{type(exc).__name__}: {exc}")
            return
        ms = (time.perf_counter() - t0) * 1000.0
        cpu = sum(proc.cpu_times()[:2]) - c0
        gpu = self.gpu.snapshot() if self.gpu is not None else None
        for s in segments:
            self._pending.append({"at": round(chunk.start_at + s.start, 2), "end": round(chunk.start_at + s.end, 2),
                                  "text": s.text, "lang": s.lang})
        st = self.stats
        st.chunks += 1
        st.audio_s += chunk.seconds
        st.speech_s += chunk.speech_s
        st.utterances += len(segments)
        st.transcribe_ms.append(ms)
        st.rtf.append(ms / 1000.0 / max(chunk.seconds, 1e-6))
        st.cpu_s += cpu
        if gpu:
            st.gpu_util_max = max(st.gpu_util_max or 0, gpu["gpu_util"])
            st.vram_used_mb_max = max(st.vram_used_mb_max or 0, gpu["vram_used_mb"])
        self.log("call_chunk", call_id=self.call_id, audio_s=round(chunk.seconds, 2), speech_s=round(chunk.speech_s, 2),
                 ms=round(ms, 1), rtf=round(st.rtf[-1], 3), cpu_s=round(cpu, 2), segments=len(segments),
                 lang=info.get("lang"), stt_device=self.backend.device, gpu=gpu)
        del chunk   # the audio goes with it

    def _maybe_flush(self, *, final: bool) -> None:
        due = final or (self._pending and time.time() - self._pending_since >= self.cfg.flush_every_s)
        if not due:
            return
        items, self._pending = self._pending, []
        self._pending_since = time.time()
        st = self.stats
        st.chunks_silent = self.chunker.silent_dropped
        if items:
            try:
                cid = self.store.add_call_side(self.call_id, app=self.app, process=self.process,
                                               started_at=items[0]["at"], ended_at=items[-1]["end"],
                                               utterances=items, final=final)
                if cid is not None:
                    st.captures += 1
            except Exception as exc:
                st.errors += 1
                self.log("error", where="calls.flush", error=f"{type(exc).__name__}: {exc}")
        try:
            self.store.update_call(
                self.call_id, chunks=st.chunks, chunks_silent=st.chunks_silent, speech_s=round(st.speech_s, 2),
                audio_s=round(st.audio_s, 2), utterances=st.utterances, transcribe_ms=round(sum(st.transcribe_ms), 1))
        except Exception:
            pass


def summarize(stats: RecordingStats) -> dict[str, Any]:
    ms = sorted(stats.transcribe_ms)

    def pct(p: float) -> float | None:
        return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1) if ms else None

    return {"chunks": stats.chunks, "chunks_silent": stats.chunks_silent, "audio_s": round(stats.audio_s, 1),
            "speech_s": round(stats.speech_s, 1), "utterances": stats.utterances, "captures": stats.captures,
            "chunk_ms": {"p50": pct(0.5), "p90": pct(0.9), "max": pct(1.0)},
            "rtf_mean": round(sum(stats.rtf) / len(stats.rtf), 3) if stats.rtf else None,
            "cpu_s": round(stats.cpu_s, 1), "gpu_util_max": stats.gpu_util_max,
            "vram_used_mb_max": stats.vram_used_mb_max, "errors": stats.errors}


# ---------------------------------------------------------------------------
# The worker: detect, ask, check headphones, record, end
# ---------------------------------------------------------------------------


class _NamedEvent:
    """A named auto-reset event this process creates (the UI sets it after an answer)."""

    def __init__(self, name: str) -> None:
        k = ctypes.windll.kernel32
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetEvent.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        self.handle = k.CreateEventW(None, False, False, name)

    def wait(self, timeout_s: float) -> bool:
        if not self.handle:
            threading.Event().wait(timeout_s)
            return False
        return self._k.WaitForSingleObject(self.handle, int(max(0.0, timeout_s) * 1000)) == 0

    def set(self) -> None:
        if self.handle:
            self._k.SetEvent(self.handle)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


@dataclass
class _Active:
    call_id: int
    key: str
    app: str
    via: str
    service: str | None
    detected_at: float
    last_seen: float
    session: RecordingSession | None = None
    status: str = "asking"
    headphones: bool | None = None


class CallWorker:
    """Detects calls, asks, and records the user's side (see the module docstring). One thread.

    Every outside fact comes through a replaceable function, so the whole
    flow runs offline on a temp database with synthetic audio.
    """

    def __init__(
        self, store: Store, *, db_path: Path | None = None, privacy: Any = None,
        log: Callable[..., Any] | None = None, meeting_fn: Callable[[], dict | None] = lambda: None,
        holders_fn: Callable[[], list[dict]] | None = None,
        headphones_fn: Callable[[CallConfig], tuple[bool, dict]] = headphones_now,
        source_factory: Callable[[CallConfig, str | None], Any] | None = None,
        backend_fn: Callable[[CallConfig], Any] | None = None,
        config: CallConfig | None = None,
    ) -> None:
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.db_path = Path(db_path) if db_path is not None else store.path
        self._section = PrivacySection(privacy, "calls", CallConfig.from_dict) if config is None else None
        self._config = config
        self._log_fn = log
        self.meeting_fn = meeting_fn
        own = own_executables()
        self.holders_fn = holders_fn or (lambda: mic_holders(own))
        self.headphones_fn = headphones_fn
        self.source_factory = source_factory or (lambda cfg, app: MicSource(capture_device_for(cfg, app)))
        self.backend_fn = backend_fn or (lambda cfg: load_backend(cfg, self._log))
        self.active: _Active | None = None
        self.paused = False
        self._event: _NamedEvent | None = None
        self._stop = threading.Event()
        self._gpu: Gpu | None = None
        self._errors = 0

    # -- plumbing ------------------------------------------------------------

    @property
    def cfg(self) -> CallConfig:
        return self._config if self._config is not None else self._section.get()

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:
                pass

    def _tell_ui(self) -> None:
        signal_event(CALL_READY_EVENT)

    def _quiet(self, now: float) -> bool:
        try:
            until = float(self.store.nudge_state("quiet_until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        return until > now

    # -- lifecycle -----------------------------------------------------------

    def recover(self) -> int:
        """Calls left open by a previous run (crash, restart) are ended now."""
        n = 0
        for c in self.store.calls(statuses=CALL_OPEN_STATUSES):
            self.store.update_call(c.id, status="ended", ended_at=time.time(), stop_reason="service_restart")
            n += 1
        return n

    def run(self, stop: threading.Event | None = None, pause_path: Path | None = None) -> None:
        stop = stop or self._stop
        self._stop = stop
        self._event = _NamedEvent(call_event_name(self.db_path))
        recovered = self.recover()
        self._log("calls_start", recovered=recovered, poll_s=self.cfg.poll_s, enabled=self.cfg.enabled)
        try:
            while not stop.is_set():
                self.paused = bool(pause_path is not None and pause_path.exists())
                try:
                    self.step()
                    self._errors = 0
                except Exception as exc:
                    self._errors += 1
                    self._log("error", where="calls.step", error=f"{type(exc).__name__}: {exc}",
                              traceback=traceback.format_exc())
                self._event.wait(self.cfg.poll_s * (1 if self._errors < 3 else 6))
        finally:
            if self.active is not None:
                self._end(self.active, "service_stop")
                self.active = None
            self._event.close()
            self._log("calls_stop")

    def stop(self) -> None:
        self._stop.set()
        if self._event is not None:
            self._event.set()

    # -- one pass ------------------------------------------------------------

    def _candidates(self, cfg: CallConfig) -> list[tuple[str, str, str | None, str]]:
        """``(app key, via, meeting service, display name)`` of what makes a call now; mic holders first."""
        meeting = None
        try:
            meeting = self.meeting_fn()
        except Exception:
            meeting = None
        meeting_key = app_key((meeting or {}).get("process")) if meeting else None
        out: list[tuple[str, str, str | None, str]] = []
        for h in self.holders_fn():
            key = app_key(h.get("app"))
            if key and (key in cfg.apps or key == meeting_key):
                service = (meeting or {}).get("service") if key == meeting_key else None
                out.append((key, "mic", service, cfg.name_of(key, service or str(h.get("app") or ""))))
        if meeting and meeting_key:
            service = meeting.get("service")
            out.append((meeting_key, "meeting", service, service or cfg.name_of(meeting_key, meeting.get("app") or "")))
        return out

    def step(self, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        cfg = self.cfg
        if self.paused or not cfg.enabled:
            if self.active is not None:
                self._end(self.active, "paused" if self.paused else "disabled")
                self.active = None
            return
        cands = self._candidates(cfg)
        act = self.active
        if act is not None:
            if any(k == act.key for k, *_ in cands):
                act.last_seen = now
            elif now - act.last_seen >= cfg.end_grace_s:
                self._end(act, "call_ended")
                self.active = None
                act = None
        if act is None and cands:
            key, via, service, name = cands[0]
            act = self._begin(cfg, key, via, service, name, now)
            self.active = act
        if act is not None:
            self._drive(cfg, act, now)

    def _begin(self, cfg: CallConfig, key: str, via: str, service: str | None, name: str, now: float) -> _Active:
        pref = self.store.call_pref(key)
        ok, facts = self._headphones(cfg)
        if pref == "never":
            status, consent, by = "declined", "never", "pref"
        elif pref == "always":
            status, consent, by = "asking", "always", "pref"
        elif self._quiet(now):
            status, consent, by = "not_asked", None, "quiet"
        else:
            status, consent, by = "asking", None, None
        call_id = self.store.start_call(app=name, process=key, via=via, service=service, at=now, status=status,
                                        consent=consent, consent_by=by, output_form_factor=facts.get("form_factor"),
                                        output_device=facts.get("device"), headphones=ok)
        act = _Active(call_id, key, name, via, service, now, now, status=status, headphones=ok)
        self._log("call_detected", call_id=call_id, app=name, process=key, via=via, service=service,
                  headphones=ok, form_factor=facts.get("form_factor"), asked=status == "asking" and consent is None)
        if consent is not None or by == "quiet":
            self._log("call_consent", call_id=call_id, app=name, answer=consent or "none", by=by)
        self._tell_ui()
        return act

    def _headphones(self, cfg: CallConfig) -> tuple[bool, dict[str, Any]]:
        try:
            return self.headphones_fn(cfg)
        except Exception as exc:
            return False, {"form_factor": None, "device": None, "error": f"{type(exc).__name__}: {exc}"}

    def _set_status(self, act: _Active, status: str, **fields: Any) -> None:
        if status != act.status or fields:
            act.status = status
            self.store.update_call(act.call_id, status=status, **fields)
            self._tell_ui()

    def _drive(self, cfg: CallConfig, act: _Active, now: float) -> None:
        row = self.store.call_record(act.call_id)
        if row is None:
            return
        if row.consent is not None and act.status == "asking" and row.consent_by == "card":
            self._log("call_consent", call_id=act.call_id, app=act.app, answer=row.consent, by="card",
                      headphones=act.headphones, after_s=round((row.consent_at or now) - act.detected_at, 1))
        if row.status in ("declined", "unanswered") or row.status == "stopped":
            act.status = row.status
            return
        if row.stop_requested:
            if act.session is not None:
                self._stop_recording(act, "user")
            self._set_status(act, "stopped", stop_reason="user")
            return
        if row.consent is None:
            if row.status == "asking" and now - act.detected_at >= cfg.ask_timeout_s:
                if self.store.answer_call(act.call_id, "none", by="ui_timeout"):
                    act.status = "unanswered"
                    self._log("call_consent", call_id=act.call_id, app=act.app, answer="none", by="ui_timeout")
                    self._tell_ui()
            else:
                act.status = row.status
            return
        if row.consent not in ("yes", "always"):
            act.status = row.status
            return
        ok, facts = self._headphones(cfg)
        if ok != act.headphones:
            self._log("call_headphones", call_id=act.call_id, ok=ok, form_factor=facts.get("form_factor"))
            act.headphones = ok
            self.store.update_call(act.call_id, headphones=ok, output_form_factor=facts.get("form_factor"),
                                   output_device=facts.get("device"))
        if not ok:
            if act.session is not None and act.session.listening:
                act.session.pause()
                self._log("call_recording_paused", call_id=act.call_id, reason="not_headphones",
                          form_factor=facts.get("form_factor"))
            self._set_status(act, "waiting_headphones")
            return
        if act.session is None:
            self._start_recording(cfg, act)
        elif not act.session.listening:
            act.session.resume()
            self._log("call_recording_resumed", call_id=act.call_id)
        self._set_status(act, "recording")

    def _start_recording(self, cfg: CallConfig, act: _Active) -> None:
        if self._gpu is None:
            self._gpu = Gpu()
        session = RecordingSession(
            self.store, call_id=act.call_id, app=act.app, process=act.key, cfg=cfg,
            source_factory=lambda: self.source_factory(cfg, act.key), backend=lambda: self.backend_fn(cfg),
            log=self._log, gpu=self._gpu,
        )
        session.start()
        act.session = session
        self.store.update_call(act.call_id, recording_started_at=time.time())
        self._log("call_recording_start", call_id=act.call_id, app=act.app, device=session.device,
                  opened_as=session.opened_as, stt_model=cfg.stt_model, gpu=self._gpu.snapshot())

    def _stop_recording(self, act: _Active, reason: str) -> None:
        session, act.session = act.session, None
        if session is None:
            return
        stats = session.stop(reason)
        info = summarize(stats)
        self.store.update_call(act.call_id, recording_stopped_at=time.time(), stop_reason=reason)
        self._log("call_recording_stop", call_id=act.call_id, app=act.app, reason=reason,
                  stt_model=getattr(session.backend, "name", None) if session.backend else None,
                  stt_device=getattr(session.backend, "device", None) if session.backend else None,
                  silence_dropped_s=round(session.chunker.silence_s, 1), **info)

    def _end(self, act: _Active, reason: str) -> None:
        if act.session is not None:
            self._stop_recording(act, reason)
        row = self.store.call_record(act.call_id)
        if row is not None and row.status == "asking" and row.consent is None:
            self.store.answer_call(act.call_id, "none", by="call_ended")
            self._log("call_consent", call_id=act.call_id, app=act.app, answer="none", by="call_ended")
        self.store.update_call(act.call_id, status="ended", ended_at=time.time(),
                               **({} if row is not None and row.stop_reason else {"stop_reason": reason}))
        self._log("call_ended", call_id=act.call_id, app=act.app, reason=reason,
                  seconds=round(time.time() - act.detected_at, 1))
        self._tell_ui()


__all__ = [
    "CallConfig", "CallWorker", "Chunker", "Chunk", "MicSource", "ArraySource", "RecordingSession",
    "FasterWhisperBackend", "make_backend", "load_backend", "mic_holders", "headphones_now",
    "capture_device_for", "own_executables", "summarize", "Gpu", "RATE",
]
