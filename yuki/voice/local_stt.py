"""Speech recognition on this PC: Parakeet with Silero VAD and Smart Turn, shaped like Deepgram Flux (report 67b §8).

:class:`ParakeetSTTService` is a Pipecat ``SegmentedSTTService`` that also decides where the user's turn ends, the
way Flux does, so the rest of the voice (the turn observer, the fast path, the user aggregator with Pipecat's
external turn strategies) sees what it sees with Flux: a proposed start of turn when speech starts, then once per turn
one final transcript and a proposed end of turn.

How a turn goes:

* Silero VAD (stop after 0.2 s of quiet) runs on every 20 ms of microphone audio. Speech starting opens a turn
  (``ProposedUserStartedSpeakingFrame``: the aggregator turns it into the start of the user's turn and, with
  interruptions on, cuts Yuki's speech, as Flux's start of turn does).
* Each stretch of speech is a segment. When the VAD says it stopped, the segment is decoded at once (Parakeet Unified
  EN 0.6B int8, offline mode, modified beam search with the names memory holds as hotwords) and, alongside, Smart
  Turn v3 judges from the sound whether the user is done. Done: once every segment is decoded, the words go out as one
  ``TranscriptionFrame`` and the turn ends (``ProposedUserStoppedSpeakingFrame``), once the user has been quiet 1.2 s
  (second review V2-2: a "done" at the 0.2 s stop cut sentences at a pause). Not done: the turn stays open; more
  speech adds a segment, and Smart Turn's own silence limit ends it after 3 s of quiet. Speech that resumes before the
  last segment is decoded keeps the same turn going (Flux's "turn resumed").
* The partial words after each segment go to the ``on_update`` handlers (the grounding lookup and the desktop read
  start while the user is still talking, as with Flux's updates).

What it costs (67b, this PC): VAD ~1% of a core while listening; Smart Turn 0.24-0.3 s of 1-2 cores per pause; the
decode 0.8-1.0 s of 2 cores per turn; Parakeet ~720 MB loaded. Nothing is decoded while nobody speaks, and the model
is loaded only at the wake (``yuki.voice.local_models``).

For the conversation it also answers what it asks of Flux's websocket: :meth:`_transport_is_active` (VAD and Smart
Turn are loaded: audio can flow), :meth:`_transport_send_audio` (the standby's warm-up silence: nothing to warm) and the
``on_connected`` event (the standby is ready).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import numpy as np

from pipecat.frames.frames import (
    ErrorFrame, ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame, StartFrame, TranscriptionFrame,
)
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from yuki.voice.local_models import LocalSpeech

#: At most this many hotwords per stream (the memory names first, then the screen's; Flux takes 50-60).
HOTWORDS_MAX = 60
#: A hotword is at most this many words (a name, not a phrase).
HOTWORD_MAX_WORDS = 4


def hotword_text(terms: list[str], cap: int = HOTWORDS_MAX) -> str:
    """The names as sherpa-onnx takes them for one stream: one per line, deduplicated, at most ``cap``.

    Only what the model's word pieces can spell: letters, digits, spaces, apostrophes and hyphens of the Latin
    alphabet (a name in another script would be encoded as unknown pieces and boost nothing, or anything). Formatting
    of the input for the tokenizer, not a judgement of the words."""
    out: list[str] = []
    seen: set[str] = set()
    for term in terms or []:
        text = " ".join(str(term or "").split())
        if not text or len(text.split()) > HOTWORD_MAX_WORDS:
            continue
        if not all((ch.isascii() and (ch.isalnum() or ch in " '-")) for ch in text):
            continue
        if not any(ch.isalpha() for ch in text):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return "\n".join(out)


def decode(recognizer: Any, pcm: bytes, hotwords: str = "", sample_rate: int = 16_000) -> str:
    """One segment of 16-bit mono PCM to text (blocking: run it on the decode thread)."""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if not len(audio):
        return ""
    stream = recognizer.create_stream(hotwords=hotwords) if hotwords else recognizer.create_stream()
    stream.accept_waveform(sample_rate, audio)
    recognizer.decode_stream(stream)
    return " ".join(str(stream.result.text or "").split())


class ParakeetSTTService(SegmentedSTTService):
    """Parakeet on this PC, with its own end of turn (Silero VAD + Smart Turn v3). See the module docstring."""

    #: Silence appended to a segment before it is decoded (s): the model hears the last word end.
    TRAILING_SILENCE_S = 0.3
    #: Smart Turn's "done" ends the turn only after this much quiet since the speech ended (s; second review V2-2):
    #: judged at the VAD's 0.2 s stop it cut the user off at a pause inside a sentence. Most of it passes while the
    #: last words are decoded anyway (the turn's end waited 0.7-1.4 s for the decode before). Measured on the lab's
    #: synth clips (report 86, "Second review"): a 0.6 s pause inside a sentence split 3-4 of 5 turns before, 0 of 5
    #: now; whole sentences end at a median 1.2 s as before. A pause of a second or more still ends the turn.
    COMPLETE_HOLD_S = 1.2
    #: Speech that may be starting again (the VAD's "starting", before its 0.2 s confirm) holds the end this much
    #: longer at most (s): the words resuming a sentence are not cut off by a decision made in their first 0.2 s.
    RESUME_GRACE_S = 0.3

    def __init__(self, *, engine: LocalSpeech, hotwords: list[str] | None = None, log: Any = None,
                 session: str = "", should_interrupt: bool = True, **kwargs: Any) -> None:
        super().__init__(sample_rate=16_000, trailing_silence_secs=self.TRAILING_SILENCE_S,
                         settings=STTSettings(model="parakeet-unified-en-0.6b", language=Language.EN), **kwargs)
        self._engine = engine
        self._hotwords = hotword_text(list(hotwords or []))
        self._log = log
        self._session = session
        self._should_interrupt = should_interrupt
        self._vad: Any = None
        self._turn: Any = None
        self._ready = False
        self._held = False
        # the turn
        self._vad_speaking = False
        self._vad_starting = False  # the VAD hears speech that is not confirmed yet
        self._in_turn = False
        self._texts: list[str] = []
        self._epoch = 0            # bumped when speech resumes or a turn ends: a judgement about older audio is void
        self._pending = 0          # segments queued and not decoded yet
        self._decoded = asyncio.Event()
        self._decoded.set()
        self._speech_end_at: float | None = None
        self._decode_ms: list[float] = []
        self._register_event_handler("on_start_of_turn")
        self._register_event_handler("on_update")
        self._register_event_handler("on_end_of_turn")

    # -- what the conversation asks of a Flux service ------------------------------------------------------------

    def _transport_is_active(self) -> bool:
        return self._ready

    async def _transport_send_audio(self, audio: bytes) -> None:
        del audio   # a local model has no connection to warm

    def recommended_user_turn_strategies(self, *, enable_interruptions: bool) -> Any:
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

        return ExternalUserTurnStrategies(enable_interruptions=enable_interruptions)

    def service_metadata_frame(self) -> Any:
        frame = super().service_metadata_frame()
        frame.user_turn_strategies = self.recommended_user_turn_strategies(enable_interruptions=self._should_interrupt)
        return frame

    @property
    def supports_ttfs(self) -> bool:
        return False   # this service ends the turn itself, as Flux does

    @property
    def wants_wav_segments(self) -> bool:
        return False

    def can_generate_metrics(self) -> bool:
        return True

    def set_hotwords(self, terms: list[str]) -> None:
        """The names to boost from the next segment on (memory's, then the screen's)."""
        self._hotwords = hotword_text(list(terms or []))

    def load_now(self) -> None:
        """The wake (or a conversation built awake): Parakeet starts loading now, on the engine's own thread."""
        if not self._held:
            self._held = True
            self._engine.hold()
        self._engine.recognizer()

    # -- life --------------------------------------------------------------------------------------------------------

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        try:
            self._vad, self._turn = await asyncio.wrap_future(self._engine.turn_models())
        except Exception as exc:  # noqa: BLE001 - without them nothing can be heard: said, and the conversation ends
            await self.push_error(f"local speech recognition could not start: {type(exc).__name__}: {exc}",
                                  exception=exc, fatal=True)
            return
        _reset_vad(self._vad)
        self._turn.clear()
        self._ready = True
        await self._call_event_handler("on_connected")

    async def stop(self, frame: Any) -> None:
        await super().stop(frame)
        self._let_go()

    async def cancel(self, frame: Any) -> None:
        await super().cancel(frame)
        self._let_go()

    def _let_go(self) -> None:
        self._ready = False
        if self._held:
            self._held = False
            self._engine.release()
        if self._decode_ms and self._log is not None:
            ms = sorted(self._decode_ms)
            self._log.event("local_stt", op="summary", session=self._session, segments=len(ms),
                            p50_ms=round(ms[len(ms) // 2]), max_ms=round(ms[-1]))

    # -- audio -------------------------------------------------------------------------------------------------------

    async def process_audio_frame(self, frame: Any, direction: Any) -> None:
        await super().process_audio_frame(frame, direction)   # the segment buffer (1 s kept before speech)
        if not self._ready:
            return
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        from pipecat.audio.vad.vad_analyzer import VADState

        state = await self._vad.analyze_audio(frame.audio)
        self._vad_starting = state == VADState.STARTING
        if state == VADState.SPEAKING and not self._vad_speaking:
            await self._speech_started()
        elif state == VADState.QUIET and self._vad_speaking:
            await self._speech_stopped()
        if self._turn.append_audio(frame.audio, self._vad_speaking) == EndOfTurnState.COMPLETE \
                and self._in_turn and not self._vad_speaking:
            self.create_task(self._commit(self._epoch, "silence"))

    async def _speech_started(self) -> None:
        self._vad_speaking = True
        self._epoch += 1
        self._user_speaking = True   # the segment buffer grows from here
        if not self._in_turn:
            self._in_turn = True
            self._texts = []
            self._turn.update_vad_start_secs(self._vad.params.start_secs)
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            await self._call_event_handler("on_start_of_turn", "")

    async def _speech_stopped(self) -> None:
        self._vad_speaking = False
        self._user_speaking = False
        self._speech_end_at = time.monotonic() - self._vad.params.stop_secs
        pcm = bytes(self._audio_buffer) + self._trailing_silence()
        self._audio_buffer.clear()
        self._record_stt_audio_usage(pcm)
        await self.emit_stt_usage_metrics()
        self._pending += 1
        self._decoded.clear()
        if self._log is not None:   # content-free: how long each stretch of speech was
            self._log.event("local_stt", op="segment", session=self._session, ms=round(len(pcm) / 32),
                            loaded=bool(self._engine.loaded().get("parakeet")))
        await self._segment_queue.put(pcm)
        self.create_task(self._judge(self._epoch))

    async def _handle_user_stopped_speaking(self, frame: Any) -> None:
        """Pipecat's VAD frames are not used here (this service runs its own VAD)."""
        del frame

    async def _judge(self, epoch: int) -> None:
        """Smart Turn on the turn's sound so far (alongside the decode): done means the turn ends."""
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

        try:
            state, _metrics = await self._turn.analyze_end_of_turn()
        except Exception as exc:  # noqa: BLE001 - no verdict: its silence limit still ends the turn
            if self._log is not None:
                self._log.event("local_stt", op="smart_turn", error=type(exc).__name__)
            return
        if state != EndOfTurnState.COMPLETE:
            return
        # V2-2: "done" counts once the user has been quiet COMPLETE_HOLD_S (speech resuming in the meantime bumps the
        # epoch: the same turn goes on), and not while the VAD hears speech that may be starting again.
        if self._speech_end_at is not None:
            wait = self.COMPLETE_HOLD_S - (time.monotonic() - self._speech_end_at)
            if wait > 0:
                await asyncio.sleep(wait)
        grace = time.monotonic() + self.RESUME_GRACE_S
        while self._vad_starting and epoch == self._epoch and time.monotonic() < grace:
            await asyncio.sleep(0.02)
        await self._commit(epoch, "smart_turn")

    async def _commit(self, epoch: int, why: str) -> None:
        """End the turn: wait for its segments, then one transcript and the proposed end (unless speech resumed)."""
        if epoch != self._epoch or not self._in_turn or self._vad_speaking:
            return
        await self._decoded.wait()
        if epoch != self._epoch or not self._in_turn or self._vad_speaking:
            return   # they went on talking while the last words were decoded: the same turn
        self._in_turn = False
        self._epoch += 1
        text = " ".join(t for t in self._texts if t).strip()
        self._texts = []
        self._turn.clear()
        if text:
            await self.push_frame(TranscriptionFrame(
                text, self._user_id, time_now_iso8601(), Language.EN,
                result={"words": [{"word": w} for w in text.split()], "local": {"end": why}}, finalized=True))
        await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
        await self._call_event_handler("on_end_of_turn", text)
        if self._log is not None and self._speech_end_at is not None:
            self._log.event("local_stt", op="turn", session=self._session, end=why, words=len(text.split()),
                            eos_to_final_ms=round((time.monotonic() - self._speech_end_at) * 1000))

    async def run_stt(self, audio: bytes) -> Any:
        """Decode one segment (queued in order by the base class's segment task); the words wait for the turn's end."""
        try:
            try:
                recognizer = await asyncio.wrap_future(self._engine.recognizer())
            except Exception as exc:  # noqa: BLE001 - the model can't load: the conversation can't hear, said so
                yield ErrorFrame(error=f"local speech recognition could not load: {type(exc).__name__}: {exc}",
                                 fatal=True)
                return
            started = time.perf_counter()
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(self._engine.decode_pool, decode, recognizer, audio, self._hotwords)
            self._decode_ms.append((time.perf_counter() - started) * 1000)
            if text:
                self._texts.append(text)
                if self._in_turn:
                    await self._call_event_handler("on_update", " ".join(self._texts))
        finally:
            self._pending = max(0, self._pending - 1)
            if not self._pending:
                self._decoded.set()


def _reset_vad(vad: Any) -> None:
    """A shared Silero analyzer back to its start: quiet, no buffered audio, fresh model state."""
    try:
        vad.set_params(vad.params)
        vad._vad_buffer = b""
        vad._model.reset_states()
    except Exception:  # noqa: BLE001 - only a clean start
        pass


__all__ = ["ParakeetSTTService", "hotword_text", "decode", "HOTWORDS_MAX"]
