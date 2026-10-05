"""Yuki's voice on this PC: Supertonic 3 or Kokoro through sherpa-onnx, Piper as the never-silent fallback (67b §8).

:class:`SherpaTTSService` is a Pipecat ``TTSService``. The voice is ``family:name`` (``supertonic:F1``,
``kokoro:af_heart``; :data:`yuki.voice.local_models.VOICES`). Each piece of text the pipeline sends (already
clause-first: the conversation wraps this service with ``_clause_tts``, so a reply's first clause comes on its own) is
cut again at its clause marks, and each clause is rendered on the engine's synthesis thread (2 threads) and sent as
soon as it is ready, resampled to the output rate. The first audio of a long sentence then waits for its first clause
only (67b: Supertonic 0.77 s for a short sentence, 2.45 s for a long one, on this PC).

If the chosen voice cannot load (not downloaded, a broken file), Piper's public-domain LJ Speech voice speaks instead
(instant, plain), and the log says so; a voice that plays nothing is reported by the pipeline as before.

The voice's marks (``<sigh>``, ``[style: ...]``) are not something these models can say: the conversation strips them
(``strip_marks``) before the text reaches here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import numpy as np

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language

from yuki.voice.local_models import FALLBACK_VOICE, LocalSpeech, Voice, _generation, parse_voice

#: Marks a clause may end at, for rendering (deterministic punctuation of Yuki's own words).
CLAUSE_MARKS = ",;:"
SENTENCE_MARKS = ".!?"
#: A clause shorter than this many words is joined to the next (a lone "Sure," sounds clipped on its own).
MIN_CLAUSE_WORDS = 3


def clauses(text: str, min_words: int = MIN_CLAUSE_WORDS) -> list[str]:
    """``text`` cut after its clause and sentence marks (a mark followed by a space), short pieces joined forward."""
    pieces: list[str] = []
    current = ""
    for i, ch in enumerate(text):
        current += ch
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if ch in CLAUSE_MARKS + SENTENCE_MARKS and nxt.isspace() and not (ch in CLAUSE_MARKS and _digit_around(text, i)):
            if len(current.split()) >= min_words:
                pieces.append(current.strip())
                current = ""
    if current.strip():
        if pieces and len(current.split()) < min_words:
            pieces[-1] = f"{pieces[-1]} {current.strip()}"
        else:
            pieces.append(current.strip())
    return pieces


def _digit_around(text: str, i: int) -> bool:
    """A comma or colon inside a number or a time ("1,000", "7:30") is not a clause mark."""
    return i > 0 and text[i - 1].isdigit() and i + 1 < len(text) and text[i + 1].isdigit()


def to_pcm(samples: Any, rate: int, out_rate: int) -> bytes:
    """Float samples at ``rate`` as 16-bit mono PCM at ``out_rate``."""
    audio = np.asarray(samples, dtype=np.float32)
    if not len(audio):
        return b""
    if rate != out_rate:
        import soxr

        audio = soxr.resample(audio, rate, out_rate)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class SherpaTTSService(TTSService):
    """A voice on this PC (Supertonic, Kokoro, Piper) for the Pipecat pipeline. See the module docstring."""

    def __init__(self, *, engine: LocalSpeech, voice: str = "", log: Any = None, session: str = "",
                 **kwargs: Any) -> None:
        self._voice: Voice = parse_voice(voice)
        super().__init__(push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model=self._voice.family, voice=self._voice.key, language=Language.EN),
                         **kwargs)
        self._engine = engine
        self._log = log
        self._session = session
        self._held = False
        self._stop = threading.Event()
        self._fell_back = False
        self._first_ms: list[float] = []

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def voice(self) -> Voice:
        return self._voice

    def load_now(self) -> None:
        """The wake (or a conversation built awake): the voice starts loading now, on the engine's own thread."""
        if not self._held:
            self._held = True
            self._engine.hold()
        self._engine.tts(self._voice.family)

    async def stop(self, frame: Any) -> None:
        await super().stop(frame)
        self._let_go()

    async def cancel(self, frame: Any) -> None:
        await super().cancel(frame)
        self._let_go()

    def _let_go(self) -> None:
        self._stop.set()
        if self._held:
            self._held = False
            self._engine.release()
        if self._first_ms and self._log is not None:
            ms = sorted(self._first_ms)
            self._log.event("local_tts", op="summary", session=self._session, voice=self._voice.key,
                            fell_back=self._fell_back, pieces=len(ms), first_p50_ms=round(ms[len(ms) // 2]),
                            first_max_ms=round(ms[-1]))

    async def _model(self) -> tuple[Any, Voice]:
        """The loaded voice, else the fallback voice (said in the log once). Raises when neither loads."""
        try:
            return await asyncio.wrap_future(self._engine.tts(self._voice.family)), self._voice
        except Exception as exc:  # noqa: BLE001 - the plain voice speaks instead
            if not self._fell_back:
                self._fell_back = True
                if self._log is not None:
                    self._log.event("local_tts", op="fallback", session=self._session, voice=self._voice.key,
                                    error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return await asyncio.wrap_future(self._engine.tts(FALLBACK_VOICE.family)), FALLBACK_VOICE

    def render_pcm(self, text: str, out_rate: int = 24_000) -> bytes:
        """One short line as 24 kHz PCM, blocking (the instant acknowledgement's lines, on a worker thread), only when
        the voice is already loaded: never a reason to load it (``b""``: the caller plays a soft tick)."""
        fut = self._engine.tts(self._voice.family) if self._engine.loaded().get(self._voice.family) else None
        if fut is None:
            return b""
        tts = fut.result()
        audio = tts.generate(text, _generation(self._voice.family, self._voice.sid), lambda s, p: 1)
        return to_pcm(audio.samples, audio.sample_rate, out_rate)

    async def run_tts(self, text: str, context_id: str) -> Any:
        """Each clause of ``text`` rendered and sent as soon as it is ready."""
        self._stop.clear()
        started = time.perf_counter()
        first = True
        try:
            await self.start_tts_usage_metrics(text)
            try:
                tts, voice = await self._model()
            except Exception as exc:  # noqa: BLE001 - no voice at all on this PC: the reply plays nothing
                yield ErrorFrame(error=f"the voice on this PC could not load: {type(exc).__name__}")
                return
            loop = asyncio.get_running_loop()
            stop = self._stop
            gen = _generation(voice.family, voice.sid)

            def render(piece: str) -> tuple[Any, int]:
                audio = tts.generate(piece, gen, lambda samples, progress: 0 if stop.is_set() else 1)
                return audio.samples, audio.sample_rate

            for piece in clauses(text) or [text]:
                if stop.is_set() or not piece.strip():
                    break
                samples, rate = await loop.run_in_executor(self._engine.synth_pool, render, piece)
                pcm = await loop.run_in_executor(self._engine.synth_pool, to_pcm, samples, rate, self.sample_rate)
                if not pcm:
                    continue
                if first:
                    first = False
                    self._first_ms.append((time.perf_counter() - started) * 1000)
                    await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(audio=pcm, sample_rate=self.sample_rate, num_channels=1, context_id=context_id)
        except (asyncio.CancelledError, GeneratorExit):
            self._stop.set()   # an interruption: the clause being rendered stops at its next sentence, no more follow
            raise
        finally:
            await self.stop_ttfb_metrics()


__all__ = ["SherpaTTSService", "clauses", "to_pcm"]
