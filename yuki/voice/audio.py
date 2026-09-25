"""Audio devices for voice: the shared microphone, the speaker, and the headphone rule.

* :class:`SharedMic` opens one input in WASAPI **shared** mode (never exclusive:
  Discord, Handy and the call memory keep using the same microphone) at 16 kHz
  mono 16-bit, 20 ms blocks, and fans each block out to whoever subscribed:
  the wake-word spotter between conversations, the pipeline during one. It
  keeps the last few seconds in memory only, so the words right after "Yuki"
  reach the conversation that the name starts. Nothing is written anywhere.
* :class:`Speaker` plays 16-bit mono PCM on the default output (shared mode).
* :func:`headphones_check` is the headphone rule (docs/VOICE.md §11): the
  existing Core Audio check (:func:`yuki.perception.audio.output_is_headphones`).

Which microphone: the configured one (``Settings.voice_input_device``, a name
or part of one), else Windows' default *communications* input, found by the
friendly name Core Audio reports and matched against PortAudio's WASAPI device
list (the same names). Everything here imports ``sounddevice`` lazily, so
importing this module costs nothing while voice is off.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

#: The pipeline's input format.
RATE = 16_000
BLOCK_MS = 20
BLOCK_SAMPLES = RATE * BLOCK_MS // 1000

#: How much recent audio the mic keeps in memory for a conversation the wake
#: word starts (the name itself and what follows it).
BACKLOG_S = 3.0

#: A 20 ms block at or above this level counts as voiced for the "end of
#: speech" timing in the voice log. Measurement only; nothing decides on it.
VOICED_DBFS = -42.0


def _wasapi(sd: Any) -> int | None:
    for i, api in enumerate(sd.query_hostapis()):
        if "wasapi" in api["name"].lower():
            return i
    return None


def default_capture_name() -> str | None:
    """Friendly name of Windows' default communications input (console input if none)."""
    try:
        from yuki.perception.audio import default_endpoints

        caps = default_endpoints().get("capture") or {}
        dev = caps.get("communications") or caps.get("console")
        return str(dev["name"]) if dev and dev.get("name") else None
    except Exception:  # noqa: BLE001 - fall back to PortAudio's default
        return None


def find_device(sd: Any, *, kind: str, name: str | None) -> tuple[int | None, dict[str, Any]]:
    """PortAudio index of a WASAPI device by name (exact, then substring), else the default.

    Args:
        kind: ``input`` or ``output``.
        name: Wanted friendly name or part of it; ``None`` for the default.

    Returns:
        ``(index or None, facts)`` where facts name what was chosen and why.
    """
    api = _wasapi(sd)
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    if api is None:
        return None, {"chosen": "portaudio_default", "reason": "no WASAPI host API"}
    devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] == api and d[channels_key] > 0]
    if name:
        want = name.casefold()
        for match in (lambda d: d["name"].casefold() == want,
                      lambda d: want in d["name"].casefold() or d["name"].casefold() in want):
            for i, d in devices:
                if match(d):
                    return i, {"chosen": d["name"], "reason": "named"}
    default = sd.query_hostapis(api)["default_input_device" if kind == "input" else "default_output_device"]
    if default is not None and default >= 0:
        return int(default), {"chosen": sd.query_devices(default)["name"], "reason": "wasapi_default"}
    return None, {"chosen": "portaudio_default", "reason": "no WASAPI default"}


@dataclass
class MicStats:
    """Counters for the voice log (content-free)."""

    blocks: int = 0
    overflows: int = 0
    last_voiced: float = 0.0


class SharedMic:
    """One shared-mode microphone stream, fanned out to subscribers.

    Args:
        device: Name (or part of it) of the input; ``None``: default communications input.

    Subscribers are called on PortAudio's callback thread with
    ``(pcm16_bytes, monotonic_time_of_block_start)``; they must only hand the
    block over (a queue put, a ``call_soon_threadsafe``), never work on it.
    """

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self.device_used: str | None = None
        self.opened_as: str | None = None
        self.stats = MicStats()
        self._stream: Any = None
        self._subs: dict[int, Callable[[bytes, float], None]] = {}
        self._next = 0
        self._lock = threading.Lock()
        self._backlog: collections.deque[tuple[float, bytes]] = collections.deque(
            maxlen=int(BACKLOG_S * 1000 / BLOCK_MS)
        )
        self._resample: Any = None
        self._channels = 1
        self._voiced_floor = float(10 ** (VOICED_DBFS / 20.0))

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def subscribe(self, fn: Callable[[bytes, float], None]) -> int:
        with self._lock:
            self._next += 1
            self._subs[self._next] = fn
            return self._next

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subs.pop(token, None)

    def backlog(self, since: float) -> list[tuple[bytes, float]]:
        """Blocks kept in memory that started at or after ``since`` (monotonic)."""
        with self._lock:
            return [(pcm, at) for at, pcm in self._backlog if at >= since]

    def open(self) -> dict[str, Any]:
        """Open the stream (no-op when open). Returns facts for the log. Raises on failure."""
        if self._stream is not None:
            return {"device": self.device_used, "opened_as": self.opened_as}
        import sounddevice as sd

        name = self.device or default_capture_name()
        index, facts = find_device(sd, kind="input", name=name)
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        self.device_used = str(info["name"])
        self._channels = max(1, min(2, int(info["max_input_channels"])))
        try:
            self._stream = sd.InputStream(
                device=index, samplerate=RATE, channels=self._channels, dtype="float32",
                blocksize=BLOCK_SAMPLES, callback=self._callback,
                extra_settings=sd.WasapiSettings(auto_convert=True),
            )
            self.opened_as = "wasapi_shared_auto_convert"
        except Exception:  # noqa: BLE001 - the device's own rate, resampled here
            import soxr

            native = int(info["default_samplerate"])
            self._resample = soxr.ResampleStream(native, RATE, 1, dtype="float32")
            self._stream = sd.InputStream(
                device=index, samplerate=native, channels=self._channels, dtype="float32",
                blocksize=native * BLOCK_MS // 1000, callback=self._callback,
            )
            self.opened_as = f"wasapi_shared_{native}hz_resampled"
        self._stream.start()
        return {"device": self.device_used, "opened_as": self.opened_as, "wanted": name, **facts}

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._resample = None
        with self._lock:
            self._backlog.clear()

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status and status.input_overflow:
            self.stats.overflows += 1
        block = indata
        if block.ndim == 2 and block.shape[1] > 1:
            # An interface's mic is usually on one input: keep the louder channel.
            loud = int(np.argmax(np.abs(block).mean(axis=0)))
            block = block[:, loud]
        mono = np.ascontiguousarray(block.reshape(-1), dtype=np.float32)
        if self._resample is not None:
            mono = self._resample.resample_chunk(mono)
        self.feed(mono, time.monotonic() - len(mono) / RATE)

    def feed(self, mono: np.ndarray, at: float) -> None:
        """Push float32 mono 16 kHz audio as if it came from the device (tests use this too)."""
        if mono.size == 0:
            return
        peak = float(np.sqrt(np.mean(mono * mono)))
        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        self.stats.blocks += 1
        if peak >= self._voiced_floor:
            self.stats.last_voiced = at + len(mono) / RATE
        with self._lock:
            self._backlog.append((at, pcm))
            subs = list(self._subs.values())
        for fn in subs:
            try:
                fn(pcm, at)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop the others
                pass


class Speaker:
    """Blocking 16-bit mono output on a WASAPI shared-mode device."""

    def __init__(self, rate: int, device: str | None = None) -> None:
        self.rate = rate
        self.device = device
        self.device_used: str | None = None
        self._stream: Any = None

    def open(self) -> dict[str, Any]:
        import sounddevice as sd

        index, facts = find_device(sd, kind="output", name=self.device)
        info = sd.query_devices(index if index is not None else sd.default.device[1])
        self.device_used = str(info["name"])
        self._stream = sd.RawOutputStream(
            device=index, samplerate=self.rate, channels=1, dtype="int16",
            extra_settings=sd.WasapiSettings(auto_convert=True),
        )
        self._stream.start()
        return {"device": self.device_used, **facts}

    def write(self, pcm: bytes) -> None:
        stream = self._stream
        if stream is not None:
            stream.write(pcm)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def headphones_check(known: tuple[str, ...] = ()) -> tuple[bool, dict[str, Any]]:
    """Whether the sound goes to headphones now (``(ok, facts)``; never raises)."""
    try:
        from yuki.perception.audio import output_is_headphones

        return output_is_headphones(known_headphones=tuple(known))
    except Exception as exc:  # noqa: BLE001 - unknown counts as not headphones
        return False, {"error": f"{type(exc).__name__}: {exc}"}
