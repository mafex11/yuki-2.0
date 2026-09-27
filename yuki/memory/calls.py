"""Calls, "just my side": remember what the user said in a call, from their own microphone only.

Decided with the owner (docs/MEMORY.md, "Calls: just my side"): during a call
Yuki may transcribe ONLY the user's microphone - never the call's audio, never
the other participants - on this PC, and only when the user said yes for this
call (or "always" for the app). Nothing is ever inferred about what anyone
else said.

One thread in ``yuki-memory`` (:class:`CallWorker`, ``yuki-memory-calls``):

1. **Detect** (measured, no app list: :mod:`yuki.memory.avsignals`). Every
   ``poll_s`` it reads, content-free, which apps hold the microphone and the
   camera (the ConsentStore, re-read only when Windows changes it; Yuki's own
   processes left out), which of them also play sound (Core Audio render
   sessions, read only while someone holds the microphone), and whether the
   timeline has a meeting row in front. A call is an app that has held the
   microphone for ``hold_s`` while it also plays sound or holds the camera,
   an app the user listed in ``[calls] apps`` holding the microphone, or a
   meeting row. A full-screen game in front using its own voice chat is not a
   call (measured: :func:`yuki.memory.avsignals.gaming_with_voice`); Discord
   or any other app holding the microphone meanwhile still is. It ends once its app has let go of the microphone (and no
   meeting row is in front) for ``end_grace_s``.
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
4. **Keep, do not transcribe.** Shared-mode WASAPI capture of the input
   device the call app records from, into memory; a speech gate (energy
   floor, then Silero VAD on what is louder) drops everything that is not
   speech; speech is cut into rolling chunks at pauses, compressed (Opus),
   encrypted with the store's key (AES-256-GCM, DPAPI-protected) and written
   to the spool next to the database (:meth:`Store.write_spool`) - never in
   plaintext. No speech-to-text model runs during a call: the deferred
   transcriber (:mod:`yuki.memory.transcribe`) does that when the PC is free,
   deletes each chunk's audio once its words are stored, and hands the call
   to the journal (``my_call_side``: facts and the user's own commitments).

Logs are content-free: ``call_detected``, ``call_consent``,
``call_recording_start``/``call_recording_stop``, ``call_chunk`` (audio and
speech seconds, bytes spooled, encode ms, VAD CPU), never words or audio.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.avsignals import (
    AvSignals,
    Holder,
    display_name,
    foreground_fullscreen,
    gaming_with_voice,
    gpu_share,
    own_executables,
)
from yuki.memory.store import (
    CALL_OPEN_STATUSES,
    CALL_READY_EVENT,
    Store,
    app_key,
    call_event_name,
    signal_event,
)

RATE = 16_000
#: One gate frame = one Silero VAD window (512 samples at 16 kHz).
FRAME = 512
FRAME_S = FRAME / RATE

# ---------------------------------------------------------------------------
# Configuration: the privacy file's [calls] table (data, not code paths)
# ---------------------------------------------------------------------------


def _packaged_calls_table() -> dict:
    """The packaged ``[calls]`` table (privacy_default.toml); {} when unreadable."""
    try:
        import tomllib

        from yuki.memory.privacy import DEFAULT_PATH

        with DEFAULT_PATH.open("rb") as fh:
            return tomllib.load(fh).get("calls") or {}
    except Exception:
        return {}


@dataclass(frozen=True)
class CallConfig:
    enabled: bool = True
    #: The user's own additions (empty by default): apps that are a call whenever they hold the
    #: microphone, sound out or not. Every other call is measured (see the module docstring).
    apps: frozenset[str] = frozenset()
    #: The user's own display names (process or package family -> name); default: the app's own name.
    names: tuple[tuple[str, str], ...] = ()
    #: How long an app must have held the microphone (with sound out or the camera) to be a call.
    hold_s: float = 15.0
    poll_s: float = 5.0
    holders_backstop_s: float = 60.0
    end_grace_s: float = 30.0
    ask_timeout_s: float = 120.0
    headphone_form_factors: tuple[str, ...] = ("headphones", "headset", "handset")
    headphone_devices: tuple[str, ...] = ()
    capture_device: str = ""
    stt_model: str = "faster-whisper:large-v3"
    stt_fallback: str = "faster-whisper:small"
    model_dir: str = ""
    languages: tuple[str, ...] = ()
    gate_dbfs: float = -50.0
    vad: bool = True
    vad_threshold: float = 0.5
    min_speech_s: float = 0.5
    chunk_min_s: float = 6.0
    chunk_max_s: float = 25.0
    pause_s: float = 0.7
    # -- speech to text in the deferred worker (the stt_* keys; docs/MEMORY.md "Calls: just my side") --
    stt_beam_size: int = 5
    #: Whisper's temperature fallback: a decode that fails the thresholds below is retried hotter.
    stt_temperatures: tuple[float, ...] = (0.0, 0.4, 0.8)
    #: Whisper's own rules: retry when the text compresses too well (a loop) or its average token
    #: log-probability is too low; a result is silence when no_speech_prob is high AND the log-probability low.
    stt_compression_ratio_threshold: float = 2.4
    stt_log_prob_threshold: float = -1.0
    stt_no_speech_threshold: float = 0.6
    #: Kept segments are dropped when Whisper's no-speech probability is over ``stt_drop_no_speech`` while
    #: its log-probability is under ``stt_drop_log_prob`` (a stricter use of the rule above).
    stt_drop_no_speech: float = 0.2
    stt_drop_log_prob: float = -0.8
    #: A segment is marked unclear for the journal under this average log-probability or over this
    #: no-speech probability; an exact repeat of the previous segment that is unclear and at most
    #: ``stt_repeat_max_tokens`` tokens long is dropped (Whisper's loop on breathing, clicks, game sound).
    stt_unclear_log_prob: float = -0.6
    stt_unclear_no_speech: float = 0.4
    stt_repeat_max_tokens: int = 12
    #: Upper bound of Whisper tokens per second of audio, per language (measured: ~2x the rate of
    #: synthetic speech - en 4, hi 14, ta 13, kn 30, te 25 tokens/s): a decode stuck in a loop stops there.
    stt_tokens_per_s: tuple[tuple[str, float], ...] = (("en", 8.0), ("hi", 28.0), ("ta", 26.0), ("kn", 60.0),
                                                       ("te", 52.0))
    #: A chunk gets ``stt_chunk_limit_s + stt_chunk_limit_rtf * its seconds``; past it no new decode starts
    #: and what was decoded so far is kept.
    stt_chunk_limit_s: float = 10.0
    stt_chunk_limit_rtf: float = 1.0
    #: Names and terms from memory given to Whisper as a hint (hotwords), at most this many tokens; 0 = off.
    stt_vocab_tokens: int = 40
    stt_vocab_days: float = 14.0
    #: Languages the hint is given for (memory's names are in Latin script; with them Whisper translated
    #: Kannada speech into English - measured). Empty = every language.
    stt_vocab_languages: tuple[str, ...] = ("en", "hi")
    #: A style example per language, given to Whisper as the previous text (``[calls.stt_prompts]``).
    stt_prompts: tuple[tuple[str, str], ...] = ()

    def tokens_per_s(self, lang: str | None) -> float:
        rates = dict(self.stt_tokens_per_s)
        return rates.get(lang or "", max(rates.values(), default=60.0))

    def prompt_for(self, lang: str | None) -> str:
        return next((p for k, p in self.stt_prompts if k == lang), "")

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

        def table_of(key: str) -> dict:
            # a user file written before a key existed still gets the packaged value (stt_* only)
            raw = t.get(key)
            if raw is None:
                raw = _packaged_calls_table().get(key)
            return raw if isinstance(raw, dict) else {}

        apps = frozenset(k for k in (app_key(a) for a in strings("apps", ())) if k)
        names = tuple((app_key(k) or "", str(v)) for k, v in (t.get("names") or {}).items() if str(v).strip())
        try:
            temps = tuple(float(x) for x in t.get("stt_temperatures", d.stt_temperatures))
        except (TypeError, ValueError):
            temps = d.stt_temperatures
        try:
            rates = tuple((str(k).casefold(), float(v)) for k, v in table_of("stt_tokens_per_s").items())
        except (TypeError, ValueError):
            rates = d.stt_tokens_per_s
        prompts = tuple((str(k).casefold(), str(v).strip()) for k, v in table_of("stt_prompts").items()
                        if str(v).strip())
        return cls(
            enabled=bool(t.get("enabled", d.enabled)), apps=apps, names=names,
            hold_s=max(0.0, num("hold_s", d.hold_s)),
            poll_s=max(1.0, num("poll_s", d.poll_s)),
            holders_backstop_s=max(5.0, num("holders_backstop_s", d.holders_backstop_s)), end_grace_s=max(0.0, num("end_grace_s", d.end_grace_s)),
            ask_timeout_s=max(10.0, num("ask_timeout_s", d.ask_timeout_s)),
            headphone_form_factors=tuple(s.casefold() for s in strings("headphone_form_factors",
                                                                        d.headphone_form_factors)),
            headphone_devices=strings("headphone_devices", ()),
            capture_device=str(t.get("capture_device") or "").strip(),
            stt_model=str(t.get("call_stt_model") or d.stt_model).strip(),
            stt_fallback=str(t.get("call_stt_fallback") or d.stt_fallback).strip(),
            model_dir=str(t.get("model_dir") or "").strip(),
            languages=tuple(s.casefold() for s in strings("languages", ())),
            gate_dbfs=num("gate_dbfs", d.gate_dbfs), vad=bool(t.get("vad", d.vad)),
            vad_threshold=num("vad_threshold", d.vad_threshold), min_speech_s=num("min_speech_s", d.min_speech_s),
            chunk_min_s=num("chunk_min_s", d.chunk_min_s), chunk_max_s=max(2.0, num("chunk_max_s", d.chunk_max_s)),
            pause_s=num("pause_s", d.pause_s),
            stt_beam_size=max(1, int(num("stt_beam_size", d.stt_beam_size))),
            stt_temperatures=temps or d.stt_temperatures,
            stt_compression_ratio_threshold=num("stt_compression_ratio_threshold", d.stt_compression_ratio_threshold),
            stt_log_prob_threshold=num("stt_log_prob_threshold", d.stt_log_prob_threshold),
            stt_no_speech_threshold=num("stt_no_speech_threshold", d.stt_no_speech_threshold),
            stt_drop_no_speech=num("stt_drop_no_speech", d.stt_drop_no_speech),
            stt_drop_log_prob=num("stt_drop_log_prob", d.stt_drop_log_prob),
            stt_unclear_log_prob=num("stt_unclear_log_prob", d.stt_unclear_log_prob),
            stt_unclear_no_speech=num("stt_unclear_no_speech", d.stt_unclear_no_speech),
            stt_repeat_max_tokens=int(num("stt_repeat_max_tokens", d.stt_repeat_max_tokens)),
            stt_tokens_per_s=rates or d.stt_tokens_per_s,
            stt_chunk_limit_s=max(1.0, num("stt_chunk_limit_s", d.stt_chunk_limit_s)),
            stt_chunk_limit_rtf=max(0.0, num("stt_chunk_limit_rtf", d.stt_chunk_limit_rtf)),
            stt_vocab_tokens=max(0, int(num("stt_vocab_tokens", d.stt_vocab_tokens))),
            stt_vocab_days=max(0.0, num("stt_vocab_days", d.stt_vocab_days)),
            stt_vocab_languages=tuple(s.casefold() for s in strings("stt_vocab_languages", d.stt_vocab_languages)),
            stt_prompts=prompts,
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


def mic_holders(exclude_paths: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Apps using the microphone now (ConsentStore; ``activity_facts()["microphone"]``), minus ``exclude_paths``."""
    from yuki.memory.avsignals import _consent_users

    return [u for u in _consent_users("microphone")
            if not (u.get("path") and str(u["path"]).casefold() in exclude_paths)]


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

    class _ProcUtil(ctypes.Structure):
        _fields_ = [("pid", ctypes.c_uint), ("timeStamp", ctypes.c_ulonglong), ("smUtil", ctypes.c_uint),
                    ("memUtil", ctypes.c_uint), ("encUtil", ctypes.c_uint), ("decUtil", ctypes.c_uint)]

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

    def process_utilization(self, since_s: float = 10.0) -> dict[int, float] | None:
        """Each process's share of the GPU's compute (SM %, mean of NVML's samples over the last
        ``since_s``), ``{pid: percent}``; ``None`` when NVML or the driver cannot say. Content-free."""
        if self._dll is None:
            return None
        import time as _time

        last = ctypes.c_ulonglong(int((_time.time() - since_s) * 1e6))
        count = ctypes.c_uint(0)
        ret = self._dll.nvmlDeviceGetProcessUtilization(self._handle, None, ctypes.byref(count), last)
        if ret not in (0, 7) or not count.value:   # 7: NVML_ERROR_INSUFFICIENT_SIZE (the size query)
            return {} if ret in (0, 7) else None
        samples = (self._ProcUtil * count.value)()
        if self._dll.nvmlDeviceGetProcessUtilization(self._handle, samples, ctypes.byref(count), last) != 0:
            return None
        acc: dict[int, list[int]] = {}
        for s in samples[:count.value]:
            acc.setdefault(int(s.pid), []).append(int(s.smUtil))
        return {pid: sum(v) / len(v) for pid, v in acc.items()}


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


def silero_path() -> Path | None:
    """Silero VAD v6 (ONNX, 1.2 MB) as shipped inside faster-whisper, found without importing it."""
    import importlib.util

    spec = importlib.util.find_spec("faster_whisper")
    for base in (spec.submodule_search_locations or []) if spec else []:
        path = Path(base) / "assets" / "silero_vad_v6.onnx"
        if path.exists():
            return path
    return None


class SileroVad:
    """Silero VAD (ONNX, CPU, one thread, no arena), streamed window by window with its state carried over.

    ``probs(windows)`` takes consecutive 512-sample windows ``(n, 512)`` and
    returns their speech probabilities; the 64-sample context and the LSTM
    state run on across calls, as in faster-whisper's own use of the model.
    """

    CONTEXT = 64

    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        path = path or silero_path()
        if path is None:
            raise FileNotFoundError("silero_vad_v6.onnx not found (faster-whisper assets)")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()
        self.windows = 0
        self.cpu_s = 0.0

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._tail = np.zeros(self.CONTEXT, dtype=np.float32)

    def probs(self, windows: np.ndarray) -> np.ndarray:
        w = np.asarray(windows, dtype=np.float32).reshape(-1, FRAME)
        ctx = np.concatenate([self._tail[None, :], w[:-1, -self.CONTEXT:]], axis=0)
        batch = np.concatenate([ctx, w], axis=1)
        t0 = time.thread_time()
        out, self._h, self._c = self.session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self.cpu_s += time.thread_time() - t0
        self._tail = w[-1, -self.CONTEXT:].copy()
        self.windows += len(w)
        return np.asarray(out, dtype=np.float32).reshape(-1)[: len(w)]


class Chunker:
    """Speech gate + rolling chunks: speech is cut at pauses, silence never becomes a chunk.

    32 ms frames under ``gate_dbfs`` are silence (cheap: no model runs on
    them); the rest are speech only when Silero VAD says so (``vad``, with
    hysteresis: ``vad_threshold`` to start, 0.15 lower to go on), or, without
    a VAD, whenever they are loud enough. While nothing voiced has
    come, silence is dropped as it arrives (a short pre-roll is kept). A chunk
    ends at a pause of ``pause_s`` once it is ``chunk_min_s`` long, after a
    pause of ``4 * pause_s`` whatever its length (a short "yes"), or at
    ``chunk_max_s``. A chunk with less than ``min_speech_s`` of voiced frames
    is counted and dropped, never transcribed.
    """

    def __init__(self, cfg: CallConfig, vad: SileroVad | None = None) -> None:
        self.cfg = cfg
        self.vad = vad
        self._in_speech = False
        self.frame = FRAME
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
        frames = self._frames(at, samples)
        for t, frame, voiced in zip((t for t, _ in frames), (f for _, f in frames), self._voicing(frames)):
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

    def _voicing(self, frames: list[tuple[float, np.ndarray]]) -> list[bool]:
        if not frames:
            return []
        block = np.stack([f for _, f in frames]).astype(np.float64)
        loud = 20.0 * np.log10(np.sqrt(np.mean(block ** 2, axis=1)) + 1e-12) >= self.cfg.gate_dbfs
        if self.vad is None:
            return [bool(x) for x in loud]
        if not loud.any():               # nothing above the floor: the model is not run
            self._in_speech = False
            return [False] * len(frames)
        probs = self.vad.probs(block.astype(np.float32))
        out = []
        for is_loud, p in zip(loud, probs):
            limit = self.cfg.vad_threshold - (0.15 if self._in_speech else 0.0)
            self._in_speech = bool(is_loud and p >= limit)
            out.append(self._in_speech)
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
    #: Whisper's own signals for the decode this segment came from.
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0
    tokens: int = 0
    #: Recognised with low confidence (see :func:`screen_segments`); the journal is told.
    unclear: bool = False


def _same_words(text: str) -> str:
    """Casefolded words without punctuation, for "is this the same phrase again"."""
    import unicodedata

    kept = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text.casefold())
    return " ".join(kept.split())


def screen_segments(segments: Sequence[Segment], cfg: CallConfig,
                    previous: str | None = None) -> tuple[list[Segment], dict[str, int], str | None]:
    """Whisper's own signals decide what of a chunk is kept: ``(kept, dropped counts by reason, last phrase)``.

    * ``no_speech``: Whisper's no-speech probability over ``stt_drop_no_speech`` while its average
      log-probability is under ``stt_drop_log_prob`` - it heard no speech and was unsure of its words.
    * ``repetitive``: compression ratio over ``stt_compression_ratio_threshold`` even after the
      temperature fallback (a decoding loop: "भाग भाग भाग ...").
    * ``repeat``: the same phrase as the previous segment (``previous`` carries it across the chunks
      of a call), unclear and at most ``stt_repeat_max_tokens`` tokens: the loop Whisper falls into on
      breathing, clicks and game sound ("कर दो" six times running).

    A kept segment is marked ``unclear`` when its log-probability is under ``stt_unclear_log_prob`` or
    its no-speech probability over ``stt_unclear_no_speech``.
    """
    kept: list[Segment] = []
    dropped: dict[str, int] = {}
    last = previous
    for s in segments:
        phrase = _same_words(s.text)
        reason = None
        if not phrase:
            reason = "empty"
        elif s.no_speech_prob > cfg.stt_drop_no_speech and s.avg_logprob < cfg.stt_drop_log_prob:
            reason = "no_speech"
        elif s.compression_ratio > cfg.stt_compression_ratio_threshold:
            reason = "repetitive"
        else:
            s.unclear = s.avg_logprob < cfg.stt_unclear_log_prob or s.no_speech_prob > cfg.stt_unclear_no_speech
            if phrase == last and s.unclear and s.tokens <= cfg.stt_repeat_max_tokens:
                reason = "repeat"
        if reason is not None:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(s)
        if phrase:
            last = phrase
    return kept, dropped, last


def vocabulary_hint(subjects: Iterable[str], count_tokens: Callable[[str], int], budget_tokens: int,
                    *, min_facts: int = 2) -> str:
    """Names and terms from memory for Whisper's hint (``hotwords``): ``"Claude, Corvana, ..."``.

    ``subjects``: what memory's models named as the subject of each fact (portrait and conversation
    facts, the questions' unknowns, the names the user goes by on screen) - who and what the facts are
    about, as the models wrote them. A word of them counts when it is the subject of at least
    ``min_facts`` facts (it recurs in the user's life) and Whisper's own tokenizer needs two tokens or
    more for it in every casing (Whisper does not know it as one word; a common word or one it knows
    needs no hint - the measured filter, no capital-letter guess about what a name is). Words are ranked
    by facts x (tokens - 1) and taken until the hint would pass ``budget_tokens``: a short hint, so that it
    guides spelling without being heard where nobody said it.
    """
    if budget_tokens <= 0:
        return ""
    counts: dict[str, int] = {}
    forms: dict[str, dict[str, int]] = {}
    for text in subjects:
        seen: set[str] = set()
        for raw in (text or "").split():
            word = ""
            for ch in raw + " ":
                if ch.isalpha():
                    word += ch
                    continue
                if len(word) > 1:
                    seen.add(word)
                word = ""
        for word in seen:
            key = word.casefold()
            counts[key] = counts.get(key, 0) + 1
            forms.setdefault(key, {})[word] = forms.setdefault(key, {}).get(word, 0) + 1
    scored = []
    for key, n in counts.items():
        if n < min_facts:
            continue
        form = max(forms[key].items(), key=lambda kv: kv[1])[0]
        cost = min(count_tokens(" " + form), count_tokens(" " + form.lower()), count_tokens(" " + form.capitalize()))
        if cost >= 2:
            scored.append((n * (cost - 1), n, form))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    chosen: list[str] = []
    for _score, _n, form in scored:
        if count_tokens(" " + ", ".join([*chosen, form])) > budget_tokens:
            continue
        chosen.append(form)
    return ", ".join(chosen)


class ChunkTimeout(Exception):
    """A chunk ran past its time limit (:class:`_DecodeGuard`)."""


class _DecodeGuard:
    """Stands in for CTranslate2's Whisper inside faster-whisper: counts ``generate`` calls and tokens, and
    refuses to start another once the chunk's time is up. One ``generate`` is bounded by ``max_new_tokens``,
    so a chunk never runs much past its limit (a temperature-fallback loop on noise used to take 300 s)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.deadline: float | None = None
        self.calls = 0
        self.tokens = 0

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise ChunkTimeout()
        out = self._inner.generate(*args, **kwargs)
        self.calls += 1
        self.tokens += sum(len(seq) for r in out for seq in r.sequences_ids)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _add_cuda_dirs() -> list[str]:
    """Make cuBLAS/cuDNN findable for CTranslate2 (:func:`yuki.core.cuda.add_dll_dirs`: pip nvidia-* wheels,
    the frozen build, ``%LOCALAPPDATA%/Yuki/cuda``, the CUDA toolkit), on ``PATH`` too."""
    from yuki.core.cuda import add_dll_dirs

    return add_dll_dirs()


class FasterWhisperBackend:
    """``faster-whisper:<model>``: CTranslate2 Whisper, int8 (int8_float16 on CUDA)."""

    #: Whisper's decoder context (prompt + generated tokens).
    MAX_LENGTH = 448

    def __init__(self, model: str, *, device: str, model_dir: Path, languages: Sequence[str] = (),
                 cpu_threads: int = 4, options: CallConfig | None = None) -> None:
        self.model_name = model
        self.device = device
        self.model_dir = Path(model_dir)
        self.languages = tuple(languages)
        self.cpu_threads = cpu_threads
        self.options = options or CallConfig()
        self.model: Any = None
        self.guard: _DecodeGuard | None = None
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
        self.guard = _DecodeGuard(self.model.model)
        self.model.model = self.guard
        self.load_ms = (time.perf_counter() - t0) * 1000.0

    def count_tokens(self, text: str) -> int:
        """Whisper tokens of ``text`` (its own tokenizer)."""
        return len(self.model.hf_tokenizer.encode(text, add_special_tokens=False).ids)

    def pick_language(self, audio: np.ndarray) -> tuple[str | None, float]:
        """The most likely language by Whisper's language detection (on the speech only), among ``languages``
        when some are configured; None = let Whisper choose (nothing to detect on)."""
        try:
            top, top_p, all_probs = self.model.detect_language(audio, vad_filter=True)
        except Exception:   # e.g. nothing left after its VAD
            return None, 0.0
        if not self.languages:
            return top, float(top_p)
        probs = dict(all_probs or [])
        best = max(self.languages, key=lambda code: probs.get(code, 0.0))
        return best, float(probs.get(best, 0.0))

    def transcribe(self, audio: np.ndarray, *, hint: str = "") -> tuple[list[Segment], dict[str, Any]]:
        """Every segment Whisper decodes, with its own signals (:func:`screen_segments` decides what is kept).

        The language is detected first and the chunk decoded once in it (it used to be decoded twice when
        Whisper's first guess was not one of ``languages``). ``hint`` (memory's names and terms) goes in as
        faster-whisper's ``hotwords``, the language's style example from ``stt_prompts`` as the previous
        text. Each decode may produce at most ``stt_tokens_per_s`` x the chunk's seconds, and no new decode
        starts past the chunk's time limit (``info["timed_out"]``; what was decoded before is kept).
        """
        o = self.options
        seconds = len(audio) / RATE
        lang, lang_prob = self.pick_language(audio)
        prompt = o.prompt_for(lang)
        if o.stt_vocab_languages and lang not in o.stt_vocab_languages:
            hint = ""
        used = (self.count_tokens(" " + prompt) if prompt else 0) + (self.count_tokens(" " + hint) if hint else 0)
        room = self.MAX_LENGTH - used - 8          # the prompt's special tokens and the start sequence
        max_new = int(max(1, min(room, 16 + o.tokens_per_s(lang) * seconds)))
        # beam search on the GPU; greedy on the CPU, where beam search costs ~1.6x for the same words (measured)
        kwargs = dict(beam_size=o.stt_beam_size if self.device == "cuda" else 1, vad_filter=True,
                      condition_on_previous_text=False, task="transcribe", temperature=list(o.stt_temperatures),
                      compression_ratio_threshold=o.stt_compression_ratio_threshold,
                      log_prob_threshold=o.stt_log_prob_threshold, no_speech_threshold=o.stt_no_speech_threshold,
                      initial_prompt=prompt or None, hotwords=hint or None, max_new_tokens=max_new)
        guard = self.guard
        calls0, tokens0 = (guard.calls, guard.tokens) if guard else (0, 0)
        if guard is not None:
            guard.deadline = time.perf_counter() + o.stt_chunk_limit_s + o.stt_chunk_limit_rtf * seconds
        out: list[Segment] = []
        timed_out = False
        try:
            segments, info = self.model.transcribe(audio, language=lang, **kwargs)
            lang = lang or info.language
            for s in segments:
                out.append(Segment(float(s.start), float(s.end), (s.text or "").strip(), lang,
                                   avg_logprob=float(s.avg_logprob), no_speech_prob=float(s.no_speech_prob),
                                   compression_ratio=float(s.compression_ratio), tokens=len(s.tokens)))
        except ChunkTimeout:
            timed_out = True
        finally:
            if guard is not None:
                guard.deadline = None
        return out, {"lang": lang, "lang_prob": round(lang_prob, 3), "timed_out": timed_out,
                     "max_new_tokens": max_new, "decodes": (guard.calls - calls0) if guard else None,
                     "tokens": (guard.tokens - tokens0) if guard else None}

    def close(self) -> None:
        self.model = None
        import gc

        gc.collect()


def make_backend(spec: str, *, device: str, model_dir: Path, languages: Sequence[str] = (),
                 options: CallConfig | None = None) -> Any:
    """A speech-to-text backend by name: ``faster-whisper:<model>`` (``large-v3``, ``large-v3-turbo``, ``small``, ...).

    Raises ``ValueError`` for an unknown backend. (Handy's Nemotron 3.5 GGUF
    needs transcribe.cpp, which has no Python binding, and does not cover
    Kannada, Tamil or Telugu: docs/MEMORY.md.)
    """
    kind, _, model = (spec or "").partition(":")
    if kind == "faster-whisper" and model:
        return FasterWhisperBackend(model, device=device, model_dir=model_dir, languages=languages, options=options)
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
            backend = make_backend(spec, device=device, model_dir=cfg.models_path(), languages=cfg.languages,
                                   options=cfg)
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


def encode_speech(audio: np.ndarray, *, bitrate: int = 24_000) -> tuple[bytes, str]:
    """16 kHz mono float32 -> ``(bytes, codec)``: Opus in Ogg (PyAV's libopus, in memory), else int16 + zlib.

    Never touches the disk: the caller encrypts the bytes before anything is written.
    """
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
    try:
        import io

        import av

        buf = io.BytesIO()
        with av.open(buf, "w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=RATE)
            stream.bit_rate = int(bitrate)
            stream.layout = "mono"
            frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = RATE
            for packet in stream.encode(frame):
                out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        return buf.getvalue(), "opus"
    except Exception:
        import zlib

        return zlib.compress(pcm.tobytes(), 6), "pcm16-zlib"


def decode_speech(payload: bytes, codec: str) -> np.ndarray:
    """:func:`encode_speech`'s bytes back to 16 kHz mono float32 (in memory)."""
    if codec == "pcm16-zlib":
        import zlib

        return np.frombuffer(zlib.decompress(payload), dtype=np.int16).astype(np.float32) / 32768.0
    import io

    from faster_whisper.audio import decode_audio

    return np.asarray(decode_audio(io.BytesIO(payload), sampling_rate=RATE), dtype=np.float32)


@dataclass
class RecordingStats:
    chunks: int = 0
    chunks_silent: int = 0
    audio_s: float = 0.0
    speech_s: float = 0.0
    bytes: int = 0
    encode_ms: list[float] = field(default_factory=list)
    cpu_s: float = 0.0
    errors: int = 0


class RecordingSession:
    """Records one call's user side on its own thread: gate, compress, encrypt, spool. No speech-to-text here.

    :meth:`pause` closes the microphone (output left headphones), :meth:`stop`
    ends it. Chunks the gate passes are Opus-compressed in memory, encrypted
    with the store's key and written to the spool (:meth:`Store.write_spool`);
    the deferred transcriber (:mod:`yuki.memory.transcribe`) turns them into
    text later, when the PC is free, and deletes them.
    """

    def __init__(self, store: Store, *, call_id: int, app: str, process: str | None, cfg: CallConfig,
                 source_factory: Callable[[], Any], log: Callable[..., Any]) -> None:
        self.store = store
        self.call_id = call_id
        self.app = app
        self.process = process
        self.cfg = cfg
        self._source_factory = source_factory
        self.log = log
        vad = None
        if cfg.vad:
            try:
                vad = SileroVad()
            except Exception as exc:   # the energy gate alone still keeps silence out
                log("call_vad_unavailable", call_id=call_id, error=f"{type(exc).__name__}: {exc}")
        self.chunker = Chunker(cfg, vad)
        self.stats = RecordingStats()
        self._seq = 0
        self._source: Any = None
        self._source_lock = threading.Lock()
        self._stop = threading.Event()
        self._drain = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self.device: str | None = None
        self.opened_as: str | None = None
        self.stop_reason: str | None = None
        self.backend = None   # no speech-to-text during a call

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
        """Close the microphone now; what was heard so far is still spooled."""
        with self._source_lock:
            source, self._source = self._source, None
        if source is not None:
            source.stop()

    def stop(self, reason: str, *, timeout_s: float = 30.0) -> RecordingStats:
        """Close the microphone, spool what is left; returns the stats."""
        self.stop_reason = reason
        self.pause()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        return self.stats

    def wait_drained(self, timeout_s: float) -> bool:
        """Offline checks: wait until an :class:`ArraySource` was read to its end."""
        return self._drain.wait(timeout_s)

    # -- thread --------------------------------------------------------------

    def _run(self) -> None:
        c0 = time.thread_time()
        try:
            while not self._stop.is_set():
                source = self._source
                if source is None:
                    self._stop.wait(0.25)
                    continue
                got = source.read(0.25)
                if got is None:
                    if getattr(source, "done", None) is not None and source.done.is_set():
                        for chunk in self.chunker.flush():
                            self._spool(chunk)
                        self._drain.set()
                    continue
                at, samples = got
                for chunk in self.chunker.feed(at, samples):
                    self._spool(chunk)
            for chunk in self.chunker.flush():
                self._spool(chunk)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.recording", error=f"{type(exc).__name__}: {exc}",
                     traceback=traceback.format_exc())
        finally:
            self.stats.cpu_s = time.thread_time() - c0
            self.stats.chunks_silent = self.chunker.silent_dropped
            self._update_row()

    def _spool(self, chunk: Chunk) -> None:
        t0 = time.perf_counter()
        try:
            payload, codec = encode_speech(chunk.audio)
            self._seq += 1
            self.store.write_spool(self.call_id, self._seq, start_at=chunk.start_at, audio_s=chunk.seconds,
                                   speech_s=chunk.speech_s, payload=payload, codec=codec)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.spool", error=f"{type(exc).__name__}: {exc}")
            return
        ms = (time.perf_counter() - t0) * 1000.0
        st = self.stats
        st.chunks += 1
        st.audio_s += chunk.seconds
        st.speech_s += chunk.speech_s
        st.bytes += len(payload)
        st.encode_ms.append(ms)
        st.chunks_silent = self.chunker.silent_dropped
        vad = self.chunker.vad
        self.log("call_chunk", call_id=self.call_id, audio_s=round(chunk.seconds, 2), speech_s=round(chunk.speech_s, 2),
                 bytes=len(payload), codec=codec, ms=round(ms, 1), silent_dropped=st.chunks_silent,
                 vad_cpu_s=round(vad.cpu_s, 2) if vad is not None else None)
        self._update_row()
        del chunk   # the audio goes with it; only the encrypted file is left

    def _update_row(self) -> None:
        st = self.stats
        try:
            self.store.update_call(self.call_id, chunks=st.chunks, chunks_silent=st.chunks_silent,
                                   speech_s=round(st.speech_s, 2), audio_s=round(st.audio_s, 2))
        except Exception:
            pass


def summarize(stats: RecordingStats) -> dict[str, Any]:
    ms = sorted(stats.encode_ms)

    def pct(p: float) -> float | None:
        return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1) if ms else None

    return {"chunks": stats.chunks, "chunks_silent": stats.chunks_silent, "audio_s": round(stats.audio_s, 1),
            "speech_s": round(stats.speech_s, 1), "spooled_bytes": stats.bytes,
            "encode_ms": {"p50": pct(0.5), "max": pct(1.0)}, "cpu_s": round(stats.cpu_s, 2), "errors": stats.errors}



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


class _ConsentStoreWatch:
    """A change notification on the ConsentStore's microphone key (``RegNotifyChangeKeyValue``, subtree,
    values): Windows writes it when an app starts or stops using the microphone, so the holders are read
    only then instead of on every poll."""

    _KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def __init__(self) -> None:
        import winreg

        self.ok = False
        self._evt = None
        self._k = ctypes.WinDLL("kernel32")
        self._a = ctypes.WinDLL("advapi32")
        self._k.CreateEventW.restype = ctypes.c_void_p
        self._k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._k.WaitForMultipleObjects.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        self._k.WaitForMultipleObjects.restype = ctypes.c_uint32
        self._k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._a.RegNotifyChangeKeyValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p,
                                                    ctypes.c_int]
        self._a.RegNotifyChangeKeyValue.restype = ctypes.c_long
        try:
            self._key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY, 0, winreg.KEY_NOTIFY | winreg.KEY_READ)
        except OSError:
            self._key = None
            return
        self._evt = self._k.CreateEventW(None, False, False, None)
        self.ok = bool(self._evt) and self._arm()

    def _arm(self) -> bool:
        # REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET over the subtree, asynchronous (sets the event)
        return self._a.RegNotifyChangeKeyValue(ctypes.c_void_p(int(self._key)), 1, 0x1 | 0x4,
                                               ctypes.c_void_p(self._evt), 1) == 0

    def wait(self, event: _NamedEvent, timeout_s: float) -> str:
        ms = int(max(0.0, timeout_s) * 1000)
        if event.handle:
            handles = (ctypes.c_void_p * 2)(event.handle, self._evt)
            r = self._k.WaitForMultipleObjects(2, handles, False, ms)
            which = {0: "event", 1: "registry"}.get(r, "timeout")
        else:
            handles = (ctypes.c_void_p * 1)(self._evt)
            r = self._k.WaitForMultipleObjects(1, handles, False, ms)
            which = "registry" if r == 0 else "timeout"
        if which == "registry":
            self.ok = self._arm()
        return which

    def close(self) -> None:
        try:
            if self._key is not None:
                self._key.Close()
            if self._evt:
                self._k.CloseHandle(ctypes.c_void_p(self._evt))
        except Exception:
            pass


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
        signals: AvSignals | None = None,
        headphones_fn: Callable[[CallConfig], tuple[bool, dict]] = headphones_now,
        source_factory: Callable[[CallConfig, str | None], Any] | None = None,
        config: CallConfig | None = None,
        foreground_fn: Callable[[], dict | None] = foreground_fullscreen,
        gpu_fn: Callable[..., float | None] = gpu_share,
    ) -> None:
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.db_path = Path(db_path) if db_path is not None else store.path
        self._section = PrivacySection(privacy, "calls", CallConfig.from_dict) if config is None else None
        self._config = config
        self._log_fn = log
        self.meeting_fn = meeting_fn
        #: The full-screen window in front (``{"hwnd", "pid", "exe"}`` or None) and the GPU share reader:
        #: a full-screen game with voice chat is not a call (yuki.memory.avsignals.gaming_with_voice).
        self.foreground_fn = foreground_fn
        self.gpu_fn = gpu_fn
        self._gaming_logged: set[str] = set()
        #: The measured facts (shared with the timeline in the service). Holders are read again only when
        #: Windows changed a ConsentStore key or every ``holders_backstop_s``; a poll between is a cached read.
        #: ``holders_fn`` (a stub of the microphone holders alone, no sound out, no camera) is the old test hook.
        if signals is None:
            signals = (AvSignals(frozenset(), mic_fn=holders_fn, camera_fn=list, render_fn=list, watch=False,
                                 backstop_s=0.0) if holders_fn is not None else AvSignals(own_executables()))
        self.signals = signals
        #: The wake-up on a ConsentStore change (the loop's wait), only with real signals.
        self._watch_registry = holders_fn is None and bool(getattr(signals, "_watch", False))
        self._held: set[str] = set()
        self.headphones_fn = headphones_fn
        self.source_factory = source_factory or (lambda cfg, app: MicSource(capture_device_for(cfg, app)))
        self.active: _Active | None = None
        self.paused = False
        self._event: _NamedEvent | None = None
        self._stop = threading.Event()
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
        watch = _ConsentStoreWatch() if self._watch_registry else None
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
                timeout = self.cfg.poll_s * (1 if self._errors < 3 else 6)
                if watch is not None and watch.ok:
                    watch.wait(self._event, timeout)   # the signals see the same change on their next read
                else:
                    self._event.wait(timeout)
        finally:
            if self.active is not None:
                self._end(self.active, "service_stop")
                self.active = None
            if watch is not None:
                watch.close()
            self._event.close()
            self._log("calls_stop")

    def stop(self) -> None:
        self._stop.set()
        if self._event is not None:
            self._event.set()

    # -- one pass ------------------------------------------------------------

    @property
    def holder_reads(self) -> int:
        return int(self.signals.counters.get("holder_reads", 0))

    def _candidates(self, cfg: CallConfig, now: float | None = None) -> list[tuple[str, str, str | None, str]]:
        """``(app key, via, meeting service, display name)`` of what makes a call now; mic holders first.

        Measured: an app that has held the microphone for ``hold_s`` while it plays sound (an active
        output stream) or holds the camera; an app of the user's ``[calls] apps`` holding the
        microphone; the timeline's meeting row in front. Also sets :attr:`_held` (every microphone
        holder now), which keeps a call going through a quiet moment.
        """
        now = time.time() if now is None else float(now)
        meeting = None
        try:
            meeting = self.meeting_fn()
        except Exception:
            meeting = None
        meeting_key = app_key((meeting or {}).get("process")) if meeting else None
        snap = self.signals.read()
        mic: tuple[Holder, ...] = snap.mic
        self._held = {h.key for h in mic if h.key}
        cameras = {h.key for h in snap.camera if h.key}
        held_long = [h for h in mic if h.key and now - h.since >= cfg.hold_s]
        # sound out is read only for an app that could become a call by it: not one the camera, the user's list
        # or a meeting already makes a call, and not the call going on (its microphone keeps it going)
        going = self.active.key if self.active is not None else None
        playing = {h.key for h in self.signals.playing(
            h for h in held_long
            if h.key not in cameras and h.key not in cfg.apps and h.key not in (meeting_key, going))}
        out: list[tuple[str, str, str | None, str]] = []
        front: dict | None = None
        front_read = False
        for h in mic:
            key = h.key
            if not key:
                continue
            measured = h in held_long and (key in playing or key in cameras)
            if measured and key != meeting_key and key != going:
                if not front_read:
                    front_read = True
                    try:
                        front = self.foreground_fn()
                    except Exception:
                        front = None
                if front and h.matches(exe=str(front.get("exe") or "")):
                    gaming, facts = gaming_with_voice(h, fullscreen=True, shows_page=False, camera=snap.camera,
                                                      pids=[int(front.get("pid") or 0)], gpu_fn=self.gpu_fn)
                    if gaming:
                        measured = False   # a full-screen game's own voice chat: gaming, not a call
                        if key not in self._gaming_logged:
                            self._gaming_logged.add(key)
                            self._log("call_skipped_gaming", app=key, **facts)
            if key in cfg.apps or key == meeting_key or measured:
                service = (meeting or {}).get("service") if key == meeting_key else None
                out.append((key, "mic", service, cfg.name_of(key, service or display_name(h))))
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
        cands = self._candidates(cfg, now)
        act = self.active
        if act is not None:
            # a call goes on while its app holds the microphone (a quiet moment plays no sound) or is a candidate
            if act.key in self._held or any(k == act.key for k, *_ in cands):
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
        session = RecordingSession(
            self.store, call_id=act.call_id, app=act.app, process=act.key, cfg=cfg,
            source_factory=lambda: self.source_factory(cfg, act.key), log=self._log,
        )
        session.start()
        act.session = session
        self.store.update_call(act.call_id, recording_started_at=time.time())
        self._log("call_recording_start", call_id=act.call_id, app=act.app, device=session.device,
                  opened_as=session.opened_as, vad=session.chunker.vad is not None, spool="encrypted, deferred")

    def _stop_recording(self, act: _Active, reason: str) -> None:
        session, act.session = act.session, None
        if session is None:
            return
        stats = session.stop(reason)
        info = summarize(stats)
        self.store.update_call(act.call_id, recording_stopped_at=time.time(), stop_reason=reason)
        vad = session.chunker.vad
        self._log("call_recording_stop", call_id=act.call_id, app=act.app, reason=reason,
                  silence_dropped_s=round(session.chunker.silence_s, 1),
                  vad_windows=vad.windows if vad is not None else None,
                  vad_cpu_s=round(vad.cpu_s, 2) if vad is not None else None, **info)

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
    "capture_device_for", "own_executables", "summarize", "Gpu", "RATE", "Segment", "screen_segments",
    "vocabulary_hint", "ChunkTimeout",
]
