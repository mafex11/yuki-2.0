"""Echo handling, so voice works on speakers as well as headphones (docs/VOICE.md §11).

On headphones the microphone never hears Yuki. On speakers it does, and without help
speech recognition would transcribe Yuki's own words and the mouth would answer
herself. The mode is chosen per conversation from the output's Core Audio form
factor (:func:`choose_mode`), never from a device name:

* ``full``: headphones. The mic's audio goes through untouched; barge-in works.
* ``aec``: speakers, with WebRTC's echo canceller (AEC3, through livekit's
  ``AudioProcessingModule``; measured against speexdsp in docs/VOICE.md §11). What
  Yuki plays is the reference, so the mic keeps working while she talks and
  barge-in stays on. Until the canceller has shown on the live stream that it
  removes the echo (:attr:`EchoGuard.converged`, measured: at least
  :data:`CONVERGED_ERLE_DB` over :data:`CONVERGED_S` of Yuki's speech), it behaves
  like ``half``.
* ``half``: speakers without the canceller (livekit missing or failing). While
  Yuki is audible, and for a short tail after, the mic's audio is replaced by
  silence for speech recognition and the wake word; barge-in is off.

:class:`PlaybackTimeline` records what Yuki plays, at 16 kHz, on the monotonic
clock of when it is heard (written by the speaker's thread). :class:`EchoGuard`
sits in :class:`yuki.voice.audio.SharedMic` before the fan-out, so the pipeline,
the wake word, the voice log's levels and the narrator's raw-sound gate all get
the cleaned audio. Everything here is measured signal and plumbing: no words.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: The mic path's format (``yuki.voice.audio.RATE``; repeated to keep this module import-light).
RATE = 16_000
#: WebRTC's processing unit: 10 ms.
FRAME = RATE // 100
#: How much of the latency a device reports is trusted when lining the reference up with its
#: echo. The canceller cannot use a reference that arrives after its echo, while AEC3's own
#: delay estimator absorbs one that arrives early; so the timeline assumes Yuki is heard
#: sooner, and the mic hears sooner, than reported. Any error then makes the reference early.
#: (A fixed lead would ask for audio the speaker has not been handed yet: it writes only
#: 10-30 ms ahead of playback.)
LATENCY_TRUST = 0.5
#: The canceller counts as working once Yuki's speech has been reduced by this much...
CONVERGED_ERLE_DB = 12.0
#: ...over this much of her audible speech (measured on the stream, the user not talking over it).
CONVERGED_S = 0.5
#: A 10 ms reference frame louder than this counts as Yuki being audible for the ERLE measure.
REF_ACTIVE_RMS = 10 ** (-50 / 20)

MODES = ("full", "aec", "half")


def choose_mode(headphones: bool, *, preference: str = "auto", aec_available: bool = True) -> tuple[str, str]:
    """``(mode, why)`` for a conversation, from the output's form factor and the setting.

    ``preference`` is ``Settings.voice_echo``: ``auto`` (headphones -> full, speakers -> aec
    when the canceller loads, else half), ``half`` (never the canceller) or ``full`` (treat any
    output as headphones: the user's own choice, e.g. an interface's headphone jack that
    Windows calls "Speakers").
    """
    if headphones:
        return "full", "headphones"
    if preference == "full":
        return "full", "setting"
    if preference == "half":
        return "half", "setting"
    return ("aec", "speakers") if aec_available else ("half", "speakers_no_aec")


class PlaybackTimeline:
    """What Yuki plays, as 16 kHz float32 on the monotonic clock of when it is heard.

    The speaker thread calls :meth:`played` just before each write; the mic's thread
    reads :meth:`reference` and :meth:`audible`. A chunk is heard ``latency_s`` after it
    is written, or right after the audio queued before it, whichever is later.
    """

    def __init__(self, latency_s: float = 0.03, seconds: float = 8.0) -> None:
        self.latency_s = float(latency_s) * LATENCY_TRUST
        self._size = int(seconds * RATE)
        self._ring = np.zeros(self._size, np.float32)
        self._t0 = time.monotonic()
        self._end = 0.0          # when the audio queued so far has been heard (monotonic)
        self._spans: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        self._resample: Any = None
        self._rate = RATE
        self.seconds_played = 0.0

    def _index(self, t: float) -> int:
        return int(round((t - self._t0) * RATE))

    def played(self, pcm: bytes, rate: int, at: float | None = None, *, gate: bool = True) -> None:
        """Record int16 mono ``pcm`` at ``rate`` as written to the output at ``at`` (now).

        ``gate=False``: a reference for the canceller only, never a reason to silence the mic (the
        wake chime: the user goes on with their request over it, and a tone is no words).
        """
        if not pcm:
            return
        now = time.monotonic() if at is None else at
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        if rate != RATE:
            if self._resample is None or self._rate != rate:
                import soxr

                self._resample = soxr.ResampleStream(rate, RATE, 1, dtype="float32")
                self._rate = rate
            samples = self._resample.resample_chunk(samples)
        n = len(samples)
        if not n:
            return
        with self._lock:
            start = max(now + self.latency_s, self._end)
            s = self._index(start)
            if self._end and start - self._end > 0:  # a gap: silence where stale audio could be
                g0, gap = self._index(self._end), min(s - self._index(self._end), self._size)
                self._write(g0, np.zeros(max(0, gap), np.float32))
            self._write(s, samples[-self._size:])
            end = start + n / RATE
            if not gate:
                pass
            elif self._spans and start - self._spans[-1][1] < 0.05:
                self._spans[-1] = (self._spans[-1][0], end)
            else:
                self._spans.append((start, end))
                del self._spans[:-32]
            self._end = end
            self.seconds_played += n / RATE

    def _write(self, index: int, samples: np.ndarray) -> None:
        n = len(samples)
        if not n:
            return
        i = index % self._size
        first = min(n, self._size - i)
        self._ring[i:i + first] = samples[:first]
        if first < n:
            self._ring[:n - first] = samples[first:]

    def reference(self, at: float, n: int) -> np.ndarray:
        """The ``n`` samples heard from ``at`` on (zeros where nothing was playing)."""
        out = np.zeros(n, np.float32)
        with self._lock:
            end = self._end
            if not end or at >= end or at + n / RATE <= end - self._size / RATE:
                return out
            i0 = self._index(at)
            idx = (np.arange(i0, i0 + n)) % self._size
            out[:] = self._ring[idx]
            times = at + np.arange(n) / RATE
            out[(times >= end) | (times < end - self._size / RATE)] = 0.0
        return out

    def audible(self, start: float, end: float, tail_s: float = 0.0) -> bool:
        """Whether anything Yuki played is heard in ``[start, end]``, or ended less than ``tail_s`` before it."""
        with self._lock:
            return any(a <= end and b + tail_s >= start for a, b in reversed(self._spans))

    @property
    def heard_until(self) -> float:
        """When the last audio written will have been heard (monotonic; 0 before any)."""
        return self._end


def load_apm() -> tuple[Any, str]:
    """WebRTC's audio processing module with the echo canceller on: ``(apm, "")`` or ``(None, why)``.

    livekit's native library (~25 MB) is loaded only here, the first time a conversation
    on speakers needs it (0.3-1.6 s, measured), never while voice is off or only listening
    for the name. Noise suppression and gain control stay off (speech recognition does
    better on the plain signal); the high-pass filter only removes rumble.
    """
    try:
        from livekit.rtc import AudioProcessingModule
    except Exception as exc:  # noqa: BLE001 - half-duplex then
        return None, f"livekit unavailable ({type(exc).__name__})"
    try:
        apm = AudioProcessingModule(echo_cancellation=True, noise_suppression=False, high_pass_filter=True,
                                    auto_gain_control=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"echo canceller failed to start ({type(exc).__name__}: {str(exc)[:80]})"
    return apm, ""


class _LiveKitAec:
    """One 10 ms step of the canceller: reference first, then the mic frame (same thread)."""

    def __init__(self, apm: Any) -> None:
        from livekit.rtc import AudioFrame

        self._frame = AudioFrame
        self.apm = apm

    def step(self, mic: np.ndarray, ref: np.ndarray, delay_ms: int) -> np.ndarray:
        rf = self._frame(bytearray((np.clip(ref, -1, 1) * 32767).astype("<i2").tobytes()), RATE, 1, FRAME)
        self.apm.process_reverse_stream(rf)
        self.apm.set_stream_delay_ms(delay_ms)
        mf = self._frame(bytearray((np.clip(mic, -1, 1) * 32767).astype("<i2").tobytes()), RATE, 1, FRAME)
        self.apm.process_stream(mf)
        return np.frombuffer(bytes(mf.data), dtype="<i2").astype(np.float32) * (1.0 / 32768.0)


@dataclass
class EchoStats:
    """Content-free counters for the voice log."""

    blocks: int = 0
    gated_blocks: int = 0
    aec_frames: int = 0
    aec_errors: int = 0
    converged_after_s: float | None = None
    erle_db: list[float] = field(default_factory=list)

    def facts(self) -> dict[str, Any]:
        e = sorted(self.erle_db)
        return {"blocks": self.blocks, "gated_blocks": self.gated_blocks, "aec_frames": self.aec_frames,
                "aec_errors": self.aec_errors, "converged_after_s": self.converged_after_s,
                "erle_db_p50": round(e[len(e) // 2], 1) if e else None,
                "erle_db_min": round(e[0], 1) if e else None}


class EchoGuard:
    """The mic-path processor for one output: ``process(block, at) -> (block, gated)``.

    Args:
        mode: ``full`` | ``aec`` | ``half`` (:func:`choose_mode`).
        timeline: What Yuki plays (:class:`PlaybackTimeline`).
        tail_s: How long after Yuki's audio ends the mic stays gated (half-duplex): the room's
            reverberation plus the error in the device latencies.
        input_latency_s: The capture latency the mic reports (a block's time stamp is when it
            reached us; its sound was that much earlier).
        apm: A WebRTC module (:func:`load_apm`); may be attached later with :meth:`attach_apm`.
    """

    def __init__(self, mode: str, timeline: PlaybackTimeline, *, tail_s: float = 0.4,
                 input_latency_s: float = 0.02, apm: Any = None, log: Any = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown echo mode {mode!r}")
        self.mode = mode
        self.timeline = timeline
        self.tail_s = float(tail_s)
        self.input_latency_s = float(input_latency_s) * LATENCY_TRUST
        self.log = log
        self.stats = EchoStats()
        self.converged = False
        self.closing_at: float | None = None
        self._aec: _LiveKitAec | None = None
        self._in = np.zeros(0, np.float32)
        self._out = np.zeros(FRAME, np.float32)  # one frame of latency, so every call returns its length
        self._in_at = 0.0
        self._win_in = self._win_out = self._win_s = 0.0
        self._started = time.monotonic()
        if apm is not None:
            self.attach_apm(apm)

    def attach_apm(self, apm: Any) -> None:
        """The canceller is ready (loaded off the conversation's critical path)."""
        try:
            self._aec = _LiveKitAec(apm)
        except Exception as exc:  # noqa: BLE001 - stays half-duplex
            self._aec = None
            if self.log is not None:
                self.log.event("echo", state="aec_unavailable", error=f"{type(exc).__name__}: {exc}")

    @property
    def effective(self) -> str:
        """What the guard does right now: ``full``, ``aec`` (converged), or ``half``."""
        if self.mode == "full":
            return "full"
        if self.mode == "aec" and self._aec is not None and self.converged:
            return "aec"
        return "half"

    def close(self) -> None:
        """The conversation ended: keep guarding only until Yuki's last audio and its tail are over."""
        self.closing_at = time.monotonic()

    def expired(self, now: float) -> bool:
        """After :meth:`close`, once nothing Yuki played can still be heard."""
        return self.closing_at is not None and now > self.timeline.heard_until + self.tail_s

    def process(self, mono: np.ndarray, at: float) -> tuple[np.ndarray, bool]:
        """Clean one mic block (float32, 16 kHz, captured at ``at`` as stamped by the mic).

        Returns ``(samples, gated)``: ``gated`` means the block was replaced by silence
        because Yuki was audible and the canceller is not (yet) trusted.
        """
        self.stats.blocks += 1
        if self.mode == "full":
            return mono, False
        n = len(mono)
        heard_at = at - self.input_latency_s
        out = mono
        if self.mode == "aec" and self._aec is not None:
            try:
                out = self._cancel(mono, heard_at)
            except Exception as exc:  # noqa: BLE001 - a failing canceller falls back to half-duplex
                self.stats.aec_errors += 1
                if self.stats.aec_errors == 1 and self.log is not None:
                    self.log.event("echo", state="aec_error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                self._aec = None
                self.converged = False
                out = mono
        if self.effective == "aec":
            return out, False
        if self.timeline.audible(heard_at - 0.01, heard_at + n / RATE, self.tail_s):
            self.stats.gated_blocks += 1
            return np.zeros(n, np.float32), True
        return out, False

    def _cancel(self, mono: np.ndarray, heard_at: float) -> np.ndarray:
        if not len(self._in):
            self._in_at = heard_at
        self._in = np.concatenate([self._in, mono.astype(np.float32, copy=False)])
        done: list[np.ndarray] = []
        while len(self._in) >= FRAME:
            frame, self._in = self._in[:FRAME], self._in[FRAME:]
            ref = self.timeline.reference(self._in_at, FRAME)
            cleaned = self._aec.step(frame, ref, 0)
            self.stats.aec_frames += 1
            self._measure(frame, cleaned, ref)
            done.append(cleaned)
            self._in_at += FRAME / RATE
        if done:
            self._out = np.concatenate([self._out, *done])
        result, self._out = self._out[:len(mono)], self._out[len(mono):]
        return result

    def _measure(self, mic: np.ndarray, cleaned: np.ndarray, ref: np.ndarray) -> None:
        """Echo reduction while Yuki is audible, in half-second windows; the first good one converges."""
        if float(np.sqrt(np.mean(ref * ref))) < REF_ACTIVE_RMS:
            return
        self._win_in += float(np.sum(mic * mic))
        self._win_out += float(np.sum(cleaned * cleaned))
        self._win_s += FRAME / RATE
        if self._win_s < CONVERGED_S:
            return
        erle = 10.0 * float(np.log10((self._win_in + 1e-12) / (self._win_out + 1e-12)))
        self.stats.erle_db.append(erle)
        del self.stats.erle_db[:-200]
        self._win_in = self._win_out = self._win_s = 0.0
        if not self.converged and erle >= CONVERGED_ERLE_DB:
            self.converged = True
            self.stats.converged_after_s = round(time.monotonic() - self._started, 2)
            if self.log is not None:
                self.log.event("echo", state="converged", erle_db=round(erle, 1),
                               after_s=self.stats.converged_after_s)


#: The earcons: notes ``(Hz, seconds)``, level (dBFS) and fade (seconds). Non-speech sounds only (Yuki never speaks
#: first): ``listening`` a soft rising pair (the wake was heard), ``error`` a falling pair (something failed),
#: ``ended`` a softer, slower falling pair (v3.16: a conversation ended by itself; say the name to talk again),
#: ``note`` two short level pings (v3.16: something owed could not be said and is shown on the pill instead).
TONES: dict[str, tuple[tuple[tuple[float, float], ...], float, float]] = {
    "listening": (((660.0, 0.07), (990.0, 0.09)), -20.0, 0.012),
    "error": (((620.0, 0.12), (410.0, 0.2)), -16.0, 0.012),
    "ended": (((587.0, 0.11), (440.0, 0.22)), -26.0, 0.04),
    "note": (((880.0, 0.06), (880.0, 0.06)), -24.0, 0.012),
}


def tone(kind: str, rate: int = 24_000) -> bytes:
    """A short earcon as int16 mono PCM (:data:`TONES`).

    Synthesised here (no file, no network): a wake is acknowledged the moment it is heard,
    and a failure is audible even when the speech services are what failed.
    """
    notes, level_db, fade = TONES[kind]
    level = 10 ** (level_db / 20)
    parts = []
    for freq, dur in notes:
        t = np.arange(int(dur * rate)) / rate
        env = np.minimum(1.0, np.minimum(t, dur - t) / fade)  # fades: no clicks (the end tone's are long and soft)
        parts.append(np.sin(2 * np.pi * freq * t) * env * level)
        parts.append(np.zeros(int(0.02 * rate)))
    return (np.concatenate(parts) * 32767).astype("<i2").tobytes()


__all__ = ["EchoGuard", "PlaybackTimeline", "choose_mode", "load_apm", "tone", "MODES", "TONES"]
