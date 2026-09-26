"""Audio devices for voice: the shared microphone, the speaker, and the headphone rule.

* :class:`SharedMic` opens one input in WASAPI **shared** mode (never exclusive:
  Discord, Handy and the call memory keep using the same microphone) at 16 kHz
  mono 16-bit, 20 ms blocks, and fans each block out to whoever subscribed:
  the wake-word spotter between conversations, the pipeline during one. It
  keeps the last few seconds in memory only, so the words right after "Yuki"
  reach the conversation that the name starts. Nothing is written anywhere.
* :class:`Speaker` plays 16-bit mono PCM on the default output (shared mode) and
  tells the echo guard's :class:`~yuki.voice.echo.PlaybackTimeline` what it plays.
* :func:`headphones_check` reads the output's form factor through Core Audio
  (:func:`yuki.perception.audio.output_is_headphones`); it picks the echo mode
  (docs/VOICE.md §11): headphones full duplex, speakers echo-cancelled or half duplex.
* :func:`play_pcm` plays a short earcon (a failure's tone) outside a conversation.

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

#: How much recent audio the mic keeps in memory (RAM only, ~380 KB) for a conversation
#: the wake word starts: the name itself and everything said after it until speech
#: recognition is connected. The first wake after the app starts imports Pipecat
#: (3.6-5.5 s measured) and Deepgram connects in 1.2-3 s, so 3 s lost the words right
#: after the name ("Yuki, pause the music" in one breath) on a cold start.
BACKLOG_S = 12.0
#: A block whose peak reaches this is counted as clipped (the input's gain is too hot).
CLIP_PEAK = 0.999

#: A 20 ms block at or above this level always counts as voiced for the "end of
#: speech" timing in the voice log (and the narrator's raw-sound gate).
VOICED_DBFS = -42.0
#: A quieter block counts as voiced when it is this many times above the room's
#: floor (+12 dB) and above an absolute minimum. A Bluetooth headset's mic in
#: hands-free mode delivers speech well below -42 dBFS: on the first live test
#: (2026-09-25) not one block reached it, so every live turn's end-of-speech
#: timing was empty. Measurement only.
VOICED_OVER_FLOOR = 4.0
VOICED_MIN_DBFS = -62.0


def voiced_threshold(floor: float, absolute: float, minimum: float) -> float:
    """The level (RMS, linear) a block needs to count as voiced, given the room's floor.

    Well above the floor always (``VOICED_OVER_FLOOR``, +12 dB); the absolute level (-42 dBFS)
    lowers that only for a quiet room, never below twice the floor (+6 dB). Live 2026-09-26 the
    M-Audio interface's input sat at a -32 dBFS floor, above the absolute level, so every block
    counted as voiced: no end-of-speech timing, no silence squeezed out of the replay after a wake
    (9.8 s kept, 9.0 s replayed), and the raw-sound gate saw one endless stretch. Measurement only.
    """
    relative = max(minimum, floor * VOICED_OVER_FLOOR)
    return max(min(absolute, relative), floor * 2.0, minimum)


#: PortAudio streams this process has open (mic and speakers). PortAudio reads the device
#: list once, when it starts; after a device change or the PC waking from sleep an index
#: can point at a device that is gone. It can only be re-read with no stream open.
_OPEN_STREAMS = 0
_STREAMS_LOCK = threading.Lock()


def _stream_opened(delta: int) -> None:
    global _OPEN_STREAMS
    with _STREAMS_LOCK:
        _OPEN_STREAMS = max(0, _OPEN_STREAMS + delta)


def refresh_devices() -> bool:
    """Re-read PortAudio's device list (after a device change or sleep); only when no stream is open."""
    import sounddevice as sd

    with _STREAMS_LOCK:
        if _OPEN_STREAMS:
            return False
        try:
            sd._terminate()
            sd._initialize()
            return True
        except Exception:  # noqa: BLE001 - the old list stays
            return False


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
    #: Start of the current stretch of voiced blocks (gaps under half a second join them).
    voiced_since: float = 0.0
    voiced_blocks: int = 0
    #: The room's level (RMS, linear): falls at once to a quieter block, rises slowly.
    floor: float = 0.0
    #: Loudest block RMS seen (linear), for the log.
    loudest: float = 0.0
    #: Highest sample seen (linear) and blocks whose peak reached full scale (a hot input gain).
    peak: float = 0.0
    clipped_blocks: int = 0
    #: Blocks the echo guard replaced by silence while Yuki was audible (half duplex).
    gated_blocks: int = 0
    #: Of a two-channel input, how many blocks came from each channel (the louder one is kept).
    channel_picks: list[int] | None = None

    def levels(self) -> dict[str, Any]:
        """Content-free level facts for the log (dBFS)."""
        def db(x: float) -> float | None:
            return round(20 * float(np.log10(x)), 1) if x > 0 else None

        out = {"blocks": self.blocks, "voiced_blocks": self.voiced_blocks, "floor_dbfs": db(self.floor),
               "loudest_dbfs": db(self.loudest), "peak_dbfs": db(self.peak), "clipped_blocks": self.clipped_blocks}
        if self.gated_blocks:
            out["gated_blocks"] = self.gated_blocks
        if self.channel_picks:
            out["channel_picks"] = list(self.channel_picks)
        return out


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
        self._voiced_min = float(10 ** (VOICED_MIN_DBFS / 20.0))
        #: The echo guard of the conversation on speakers (:class:`yuki.voice.echo.EchoGuard`), or ``None``.
        self.echo: Any = None
        #: The capture latency the stream reports (s), for lining Yuki's echo up with what she played.
        self.input_latency_s = 0.02
        #: Facts about the stream as opened (rate, channels), for the log.
        self.format: dict[str, Any] = {}

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

    def subscribe_from(self, fn: Callable[[bytes, float], None], since: float) -> tuple[int, list[tuple[bytes, float]]]:
        """Subscribe and take the kept blocks from ``since`` in one step: none lost, none twice."""
        with self._lock:
            kept = [(pcm, at) for at, pcm in self._backlog if at >= since]
            self._next += 1
            self._subs[self._next] = fn
            return self._next, kept

    def set_echo(self, guard: Any) -> None:
        """Put an echo guard in front of every subscriber (``None``: none)."""
        self.echo = guard

    def open(self) -> dict[str, Any]:
        """Open the stream (no-op when open). Returns facts for the log. Raises on failure."""
        if self._stream is not None:
            return {"device": self.device_used, "opened_as": self.opened_as}
        import sounddevice as sd

        name = self.device or default_capture_name()
        try:
            return self._open(sd, name)
        except Exception:  # noqa: BLE001 - the device list may be stale (a device change, sleep): re-read once
            if not refresh_devices():
                raise
            facts = self._open(sd, name)
            facts["devices_refreshed"] = True
            return facts

    def _open(self, sd: Any, name: str | None) -> dict[str, Any]:
        index, facts = find_device(sd, kind="input", name=name)
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        self.device_used = str(info["name"])
        self._channels = max(1, min(2, int(info["max_input_channels"])))
        self.stats.channel_picks = [0, 0] if self._channels == 2 else None
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
        try:
            self._stream.start()
        except Exception:
            stream, self._stream = self._stream, None
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        _stream_opened(+1)
        try:
            self.input_latency_s = float(self._stream.latency)
        except (TypeError, ValueError, AttributeError):
            self.input_latency_s = 0.02
        self.format = {"device_rate": int(info["default_samplerate"]), "channels": self._channels,
                       "input_latency_ms": round(self.input_latency_s * 1000, 1)}
        return {"device": self.device_used, "opened_as": self.opened_as, "wanted": name, **facts, **self.format}

    def close(self, *, keep_backlog: bool = False) -> None:
        """Close the stream. ``keep_backlog``: only for a moment (PortAudio re-reading its device
        list): the kept audio and the subscribers stay, and ``open()`` carries on."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            _stream_opened(-1)
        self._resample = None
        if not keep_backlog:
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
            picks = self.stats.channel_picks
            if picks is not None and loud < len(picks):
                picks[loud] += 1
        mono = np.ascontiguousarray(block.reshape(-1), dtype=np.float32)
        if self._resample is not None:
            mono = self._resample.resample_chunk(mono)
        self.feed(mono, time.monotonic() - len(mono) / RATE)

    def feed(self, mono: np.ndarray, at: float) -> None:
        """Push float32 mono 16 kHz audio as if it came from the device (tests use this too).

        With an echo guard set (a conversation on speakers), the block is cleaned first, so
        every subscriber, the backlog and the levels see the mic without Yuki's own voice.
        """
        if mono.size == 0:
            return
        st = self.stats
        st.blocks += 1  # every block, gated or not: the device watch reads a still counter as a dead stream
        peak = float(np.max(np.abs(mono)))
        st.peak = max(st.peak, peak)
        if peak >= CLIP_PEAK:
            st.clipped_blocks += 1
        guard = self.echo
        if guard is not None:
            try:
                if guard.expired(at):
                    self.echo = guard = None
                else:
                    mono, gated = guard.process(mono, at)
                    if gated:
                        st.gated_blocks += 1
                        self._fan_out(mono, at)  # silence: the room's floor and "voiced" skip it
                        return
            except Exception:  # noqa: BLE001 - a guard error must never stop the microphone
                self.echo = None
        level = float(np.sqrt(np.mean(mono * mono)))
        st.loudest = max(st.loudest, level)
        if st.floor <= 0.0 or level < st.floor:
            st.floor = max(level, 1e-6)
        else:
            st.floor += (level - st.floor) * 0.002  # rises over ~10 s of steady sound
        if level >= voiced_threshold(st.floor, self._voiced_floor, self._voiced_min):
            st.voiced_blocks += 1
            if at - st.last_voiced > 0.5:
                st.voiced_since = at
            st.last_voiced = at + len(mono) / RATE
        self._fan_out(mono, at)

    def _fan_out(self, mono: np.ndarray, at: float) -> None:
        pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._lock:
            self._backlog.append((at, pcm))
            subs = list(self._subs.values())
        for fn in subs:
            try:
                fn(pcm, at)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop the others
                pass


class Speaker:
    """Blocking 16-bit mono output on a WASAPI shared-mode device.

    ``device`` is the output's friendly name (the tray's choice, or Windows' default as Core
    Audio names it now); ``open()`` reports in ``found`` whether PortAudio had that device
    (its list is read once, when it starts: a device connected later is missing until the
    list is re-read, :func:`refresh_devices`). ``timeline`` (a
    :class:`yuki.voice.echo.PlaybackTimeline`) is told about every chunk just before it is
    written, so the echo guard knows what the mic will hear and when. :meth:`switch` moves
    a running output to another device between two writes.
    """

    def __init__(self, rate: int, device: str | None = None) -> None:
        self.rate = rate
        self.device = device
        self.device_used: str | None = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self.timeline: Any = None
        #: The output latency the stream reports (s).
        self.latency_s = 0.03

    def open(self) -> dict[str, Any]:
        with self._lock:
            return self._open()

    def _open(self) -> dict[str, Any]:
        import sounddevice as sd

        index, facts = find_device(sd, kind="output", name=self.device)
        info = sd.query_devices(index if index is not None else sd.default.device[1])
        self.device_used = str(info["name"])
        stream = sd.RawOutputStream(
            device=index, samplerate=self.rate, channels=1, dtype="int16",
            extra_settings=sd.WasapiSettings(auto_convert=True),
        )
        stream.start()
        self._stream = stream
        _stream_opened(+1)
        try:
            self.latency_s = float(stream.latency)
        except (TypeError, ValueError, AttributeError):
            self.latency_s = 0.03
        return {"device": self.device_used, "wanted": self.device, "index": index, **facts,
                "found": bool(self.device) and facts.get("reason") == "named",
                "output_latency_ms": round(self.latency_s * 1000, 1)}

    def write(self, pcm: bytes, *, gate: bool = True) -> None:
        """Play ``pcm`` (blocking). ``gate=False``: an earcon the echo guard cancels but never silences the mic for."""
        with self._lock:
            stream = self._stream
            if stream is not None:
                timeline = self.timeline
                if timeline is not None:
                    try:
                        timeline.played(pcm, self.rate, gate=gate)
                    except Exception:  # noqa: BLE001 - the echo guard then gates by the tail only
                        pass
                stream.write(pcm)

    def switch(self, device: str | None, *, between: Any = None) -> dict[str, Any]:
        """Move to ``device`` between two writes (what is queued on the old one plays out first).

        ``between()`` runs with no stream of ours open (the session re-reads PortAudio's list there).
        """
        with self._lock:
            self._close()
            if between is not None:
                between()
            self.device = device
            return self._open()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()  # plays what is queued first
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            _stream_opened(-1)


def play_pcm(pcm: bytes, rate: int, device: str | None = None) -> None:
    """Play a short int16 mono clip on ``device`` (``None``: the default output) and wait; never raises.

    For the earcons outside a conversation (a failure's tone): its own short-lived stream.
    """
    speaker = Speaker(rate, device)
    try:
        speaker.open()
        speaker.write(pcm)
    except Exception:  # noqa: BLE001 - the pill still says it
        pass
    finally:
        speaker.close()


def headphones_check(known: tuple[str, ...] = (), device: str | None = None) -> tuple[bool, dict[str, Any]]:
    """Whether the sound goes to headphones now (``(ok, facts)``; never raises).

    ``device``: the output voice will use when the user chose one (tray "Speaker"):
    then that endpoint's own form factor is what counts, not the Windows defaults'.
    """
    try:
        from yuki.perception.audio import E_RENDER, list_endpoints, output_is_headphones

        if device:
            chosen = next((d for d in list_endpoints(E_RENDER) if d.get("name") == device), None)
            if chosen is not None:
                endpoints = {"render": {"console": chosen, "communications": chosen}}
                return output_is_headphones(endpoints, known_headphones=tuple(known))
        return output_is_headphones(known_headphones=tuple(known))
    except Exception as exc:  # noqa: BLE001 - unknown counts as not headphones
        return False, {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Device choice (tray "Microphone" / "Speaker", docs/VOICE.md §12)
# ---------------------------------------------------------------------------

#: ``kind`` -> the word the user sees.
KIND_WORD = {"capture": "Microphone", "render": "Speaker"}


def audio_devices() -> dict[str, Any]:
    """The active capture and render devices (Core Audio, read-only, ~15 ms) and the defaults.

    ``{"capture": [names], "render": [names], "default_capture": name, "default_render": name}``;
    the capture default is Windows' communications input (what voice uses when nothing is
    chosen), the render default the console output (what PortAudio's default plays to).
    Never raises: on a failure the lists are empty and ``error`` says why.
    """
    try:
        from yuki.perception.audio import E_CAPTURE, E_RENDER, default_endpoints, list_endpoints

        defaults = default_endpoints()
        caps = defaults.get("capture") or {}
        cap = caps.get("communications") or caps.get("console") or {}
        ren = (defaults.get("render") or {}).get("console") or {}
        return {"capture": [str(d["name"]) for d in list_endpoints(E_CAPTURE) if d.get("name")],
                "render": [str(d["name"]) for d in list_endpoints(E_RENDER) if d.get("name")],
                "default_capture": cap.get("name"), "default_render": ren.get("name")}
    except Exception as exc:  # noqa: BLE001 - the menu then offers the default only
        return {"capture": [], "render": [], "default_capture": None, "default_render": None,
                "error": f"{type(exc).__name__}: {exc}"}


def resolve_device(kind: str, wanted: str | None, devices: dict[str, Any]) -> tuple[str | None, str]:
    """The device to open for a choice: ``(name, "")``, or ``(None, note)`` when it is not connected.

    ``None`` means Windows' default. A chosen device that is not among the active ones
    (Bluetooth off, unplugged) falls back to the default, with a note for the pill.
    Matching is on the exact name Core Audio lists (plumbing, not a guess).
    """
    if not wanted:
        return None, ""
    if wanted in (devices.get(kind) or []) or devices.get("error"):
        return wanted, ""
    default = devices.get(f"default_{kind}")
    return None, (f"{KIND_WORD.get(kind, kind)} '{wanted}' isn't connected; using "
                  f"{('the default, ' + default) if default else 'the default'}")
