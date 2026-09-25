"""One voice conversation: the Pipecat pipeline, its measurements and its end (docs/VOICE.md §4, §9, §13).

``audio in -> Deepgram Flux (transcript + end of turn) -> user aggregator ->
mouth (Haiku on Bedrock, async tell_yuki / yuki_status) -> TTS (ElevenLabs or
Deepgram Aura) -> audio out -> assistant aggregator``

:class:`Conversation` builds it and runs it on the calling asyncio loop (the
voice thread's; never the Qt thread). It ends when nobody has spoken and no
result is owed for ``Settings.voice_idle_s``, when asked to stop, or on a fatal
service error. A running brain task holds a quiet conversation open for at
most ``voice_hold_max_s`` so its result can be spoken.

:class:`TurnObserver` watches the frames and writes the content-free turn
record (stage timings, tokens, cost, language), drives the voice state the UI
shows, and hands each spoken exchange to memory's turn log (encrypted), marked
as voice: ``request_id`` ``voice-<n>`` and ``outcome`` ``voice``.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable

from yuki.config import Settings
from yuki.voice.keys import DEEPGRAM, ELEVENLABS, get_key
from yuki.voice.mouth import MOUTH_PROMPT, TELL_YUKI, YUKI_STATUS, MouthMemory, mouth_system_blocks

#: Default voices per provider when ``Settings.voice_tts_voice`` is empty.
DEFAULT_VOICES = {"elevenlabs": "cgSgspJ2msm6clMCkdW9", "deepgram": "aura-2-thalia-en"}

#: The voice states the UI shows.
STATES = ("starting", "listening", "hearing", "thinking", "speaking", "working", "error", "off")


class VoiceUnavailable(Exception):
    """Voice cannot start or went down: the text says why, for the UI and the log."""


def hold_until_connected(ready: Callable[[], bool], log: Any, max_s: float = 10.0) -> Any:
    """A processor that holds microphone audio until speech-to-text is connected, then lets it all through.

    Deepgram's Flux service drops audio that arrives before its websocket is
    open, which loses the start of the first utterance, and with it the words
    said right after the wake word (measured 2026-09-25: a whole first sentence).
    Held audio is released in order the moment the connection is up; at most
    ``max_s`` is kept. Plumbing only.
    """
    from pipecat.frames.frames import InputAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class _Hold(FrameProcessor):
        def __init__(self) -> None:
            super().__init__(name="HoldUntilSttConnected")
            self._held: list[Any] | None = []
            self._since = time.monotonic()

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if (self._held is not None and isinstance(frame, InputAudioRawFrame)
                    and direction == FrameDirection.DOWNSTREAM):
                if not ready():
                    self._held.append(frame)
                    while sum(len(f.audio) for f in self._held) > max_s * 32_000 and len(self._held) > 1:
                        self._held.pop(0)  # 16 kHz 16-bit mono = 32,000 bytes a second
                    return
                held, self._held = self._held, None
                if log is not None:
                    log.event("stt_connected", waited_ms=round((time.monotonic() - self._since) * 1000),
                              held_ms=round(sum(len(f.audio) for f in held) / 32))
                for old in held:
                    await self.push_frame(old, direction)
            await self.push_frame(frame, direction)

    return _Hold()


#: ElevenLabs can-speak check, done once per process: ``(ok, reason)``.
_TTS_CHECK: dict[str, tuple[bool, str]] = {}


def elevenlabs_can_speak(settings: Settings) -> tuple[bool, str]:
    """Whether the ElevenLabs key may synthesise speech (one 1-character request, once per app run).

    The key can be valid yet lack the Text to Speech permission (a scoped key);
    then every reply would be silent, so this is checked before a conversation
    rather than discovered in one. Returns ``(ok, reason)``; never raises.
    """
    if "elevenlabs" in _TTS_CHECK:
        return _TTS_CHECK["elevenlabs"]
    key = get_key(ELEVENLABS)
    if not key:
        result = (False, "ELEVENLABS_API_KEY is not set")
    else:
        import json
        import urllib.error
        import urllib.request

        voice = settings.voice_tts_voice or DEFAULT_VOICES["elevenlabs"]
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
            data=json.dumps({"text": ".", "model_id": settings.voice_tts_model}).encode(), method="POST",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                response.read()
            result = (True, "ok")
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", "replace")
            result = (False, "the key lacks the Text to Speech permission" if "missing_permissions" in body
                      else f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return False, f"cannot reach ElevenLabs ({type(exc).__name__})"  # not cached: may be the network
    _TTS_CHECK["elevenlabs"] = result
    return result


def preflight(settings: Settings, *, need_headphones: bool = True) -> dict[str, Any]:
    """Check what a conversation needs before opening anything. Raises :class:`VoiceUnavailable`.

    Returns facts for the log, including ``tts_provider``: the configured one,
    or ``voice_tts_fallback`` (with ``tts_note`` saying why) when the configured
    one cannot speak.
    """
    facts: dict[str, Any] = {"tts_provider": settings.voice_tts_provider}
    if not get_key(DEEPGRAM):
        raise VoiceUnavailable("Voice needs DEEPGRAM_API_KEY (a Windows user environment variable).")
    if settings.voice_tts_provider == "elevenlabs":
        ok, reason = elevenlabs_can_speak(settings)
        if not ok:
            if settings.voice_tts_fallback:
                facts["tts_provider"] = settings.voice_tts_fallback
                facts["tts_note"] = f"ElevenLabs can't speak ({reason}); using the {settings.voice_tts_fallback} voice"
            else:
                raise VoiceUnavailable(f"Voice can't speak: ElevenLabs {reason}.")
    if not (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or _registry_env("AWS_BEARER_TOKEN_BEDROCK")):
        raise VoiceUnavailable("Voice needs Bedrock access: AWS_BEARER_TOKEN_BEDROCK is not set.")
    if need_headphones and settings.voice_require_headphones:
        from yuki.voice.audio import headphones_check

        ok, heard = headphones_check(settings.voice_headphone_devices)
        facts["output"] = heard
        if not ok:
            where = heard.get("device") or "the speakers"
            raise VoiceUnavailable(
                f"Voice needs headphones for now: sound is going to {where}, and Yuki would hear itself."
            )
    return facts


_LOGGING_SET = False


def _layered_service(base: type, memory: MouthMemory) -> type:
    """``base`` (Pipecat's ``AnthropicLLMService``) sending the mouth's prompt as cached layers.

    Pipecat passes ``system`` as one string. This subclass swaps it for the blocks of
    :func:`~yuki.voice.mouth.mouth_system_blocks`: the composed core (our prompt plus
    Pipecat's async-tool paragraphs, frozen), the profile, the standing rules and the time,
    with breakpoints on profile and rules (Pipecat marks the last two user messages; four
    in all). Built per conversation with that conversation's memory and start time, so the
    bytes stay the same on every call of the conversation. Made on first use so importing
    this module loads no Pipecat (voice off costs nothing).
    """
    started = datetime.now()

    class LayeredAnthropicLLMService(base):  # type: ignore[misc, valid-type]
        def _get_llm_invocation_params(self, context: Any) -> Any:
            params = super()._get_llm_invocation_params(context)
            composed = self._settings.system_instruction or MOUTH_PROMPT
            tail = composed[len(MOUTH_PROMPT):] if composed.startswith(MOUTH_PROMPT) else composed
            params["system"] = mouth_system_blocks(memory, tail=[tail.strip()], now=started)
            return params

    return LayeredAnthropicLLMService


def quiet_pipecat_logging(log_dir: Any) -> None:
    """Pipecat logs every context and many frames at DEBUG through loguru, on the voice loop.

    That work is pure cost (in the tray app stderr goes nowhere) and it carries
    what was said. Once per process: warnings and errors only, into
    ``logs/voice/pipecat-YYYYMMDD.log``.
    """
    global _LOGGING_SET
    if _LOGGING_SET:
        return
    from datetime import datetime
    from pathlib import Path

    from loguru import logger

    logger.remove()
    try:
        path = Path(log_dir) / f"pipecat-{datetime.now():%Y%m%d}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(str(path), level="WARNING", enqueue=False, backtrace=False, diagnose=False)
    except OSError:
        pass
    _LOGGING_SET = True


def _registry_env(name: str) -> str | None:
    from yuki.voice.keys import _from_registry

    return _from_registry(name)


@dataclass
class _Turn:
    n: int
    trigger: str
    opened: float
    language: str | None = None
    user_text: str = ""
    speech_end: float | None = None
    transcript_at: float | None = None
    llm_ttfb: float | None = None
    tts_ttfb: float | None = None
    first_audio: float | None = None
    reply: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    tts_chars: int = 0
    interrupted: bool = False
    error: str | None = None


class TurnObserver:
    """Frame observer: turn timings, usage, state, and the memory turn log. Content goes only to memory."""

    def __init__(self, *, settings: Settings, log: Any, memory: Any, session: str,
                 speech_end: Callable[[], float | None], on_state: Callable[[str, str], None],
                 tts_provider: str, activity: Callable[[], None]) -> None:
        from pipecat.observers.base_observer import BaseObserver

        self._base = BaseObserver
        self.settings = settings
        self.log = log
        self.memory = memory
        self.session = session
        self._speech_end = speech_end
        self._on_state = on_state
        self._tts_provider = tts_provider
        self._activity = activity
        self._seen: dict[int, None] = {}
        self._calls: set[Any] = set()
        self._n = 0
        self.turn: _Turn | None = None
        self.user_speaking = False
        self.bot_speaking = False
        self.generating = False
        self.state = "starting"
        self.fatal: str | None = None
        self.totals = {"turns": 0, "mouth_cost_usd": 0.0, "tts_chars": 0}

    def as_observer(self) -> Any:
        outer = self

        class _Obs(self._base):
            async def on_push_frame(self, data: Any) -> None:
                outer.on_frame(data.frame)

        return _Obs()

    # -- helpers -----------------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        if state != self.state or detail:
            self.state = state
            self._on_state(state, detail)

    def _open(self, trigger: str) -> _Turn:
        if self.turn is not None:
            self.close()
        self._n += 1
        self.turn = _Turn(n=self._n, trigger=trigger, opened=time.monotonic())
        return self.turn

    def _ms(self, a: float | None, b: float | None) -> float | None:
        return round((b - a) * 1000, 1) if a is not None and b is not None else None

    def close(self) -> None:
        """Write the open turn (CSV + JSONL, content-free) and log it to memory (content, encrypted)."""
        turn, self.turn = self.turn, None
        if turn is None:
            return
        usage = {
            "input_tokens": turn.tokens.get("prompt_tokens", 0) - turn.tokens.get("cache_read_input_tokens", 0)
            - turn.tokens.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": turn.tokens.get("cache_read_input_tokens", 0),
            "cache_write_tokens": turn.tokens.get("cache_creation_input_tokens", 0),
            "output_tokens": turn.tokens.get("completion_tokens", 0),
        }
        usage["input_tokens"] = max(0, usage["input_tokens"])
        cost = self.settings.estimate_cost(self.settings.voice_mouth_model, usage)
        reply = "".join(turn.spoken).strip() or "".join(turn.reply).strip()
        if not turn.tts_chars and turn.first_audio is not None:
            turn.tts_chars = len(reply)  # the service reported no usage: what was sent to it
        tts_rate = self.settings.voice_usd_per_1k_tts_chars.get(self._tts_provider)
        tts_cost = round(turn.tts_chars / 1000 * tts_rate, 6) if tts_rate is not None else None
        row = {
            "session": self.session, "turn": turn.n, "trigger": turn.trigger, "language": turn.language,
            "user_chars": len(turn.user_text), "reply_chars": len(reply),
            "eos_to_transcript_ms": self._ms(turn.speech_end, turn.transcript_at),
            "mouth_ttft_ms": round(turn.llm_ttfb * 1000, 1) if turn.llm_ttfb is not None else None,
            "tts_ttfb_ms": round(turn.tts_ttfb * 1000, 1) if turn.tts_ttfb is not None else None,
            "eos_to_first_audio_ms": self._ms(turn.speech_end, turn.first_audio),
            "mouth_input_tokens": usage["input_tokens"], "mouth_cache_read_tokens": usage["cache_read_tokens"],
            "mouth_cache_write_tokens": usage["cache_write_tokens"], "mouth_output_tokens": usage["output_tokens"],
            "mouth_cost_usd": round(cost, 6) if cost is not None else None,
            "tts_chars": turn.tts_chars, "tts_cost_usd": tts_cost,
            "tool_calls": ";".join(turn.tools), "interrupted": turn.interrupted, "error": turn.error,
        }
        self.totals["turns"] += 1
        self.totals["mouth_cost_usd"] += cost or 0.0
        self.totals["tts_chars"] += turn.tts_chars
        if self.log is not None:
            self.log.turn(row)
        if self.memory is not None and (turn.user_text or reply):
            try:
                self.memory.log_turn(
                    request_id=f"voice-{self.session}-{turn.n}", at=time.time(),
                    user_text=turn.user_text, reply_text=reply,
                    actions=[f"{name} (by voice)" for name in turn.tools], outcome="voice",
                )
            except Exception as exc:  # noqa: BLE001 - memory never breaks voice
                if self.log is not None:
                    self.log.event("memory_turn", error=f"{type(exc).__name__}: {exc}")

    # -- frames ------------------------------------------------------------------

    def on_frame(self, frame: Any) -> None:  # noqa: C901 - one switch over frame types
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame, BotStoppedSpeakingFrame, ErrorFrame, FunctionCallInProgressFrame,
            InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, MetricsFrame,
            TranscriptionFrame, TTSTextFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
        )
        from pipecat.metrics.metrics import LLMUsageMetricsData, TTFBMetricsData, TTSUsageMetricsData

        fid = id(frame) if getattr(frame, "id", None) is None else frame.id
        if fid in self._seen:
            return
        self._seen[fid] = None
        if len(self._seen) > 4000:
            for key in list(self._seen)[:2000]:
                del self._seen[key]
        now = time.monotonic()

        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            if self.bot_speaking and self.turn is not None:
                self.turn.interrupted = True
            self.set_state("hearing")
            self._activity()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self._activity()
        elif isinstance(frame, TranscriptionFrame):
            turn = self._open("user")
            turn.user_text = frame.text or ""
            turn.transcript_at = now
            turn.speech_end = self._speech_end()
            lang = getattr(frame, "language", None)
            turn.language = str(getattr(lang, "value", lang)) if lang else None
            self.set_state("thinking")
            self._activity()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.generating = True
            if self.turn is None or self.turn.first_audio is not None:
                self._open("result")
            self._activity()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.generating = False
            self._activity()
        elif isinstance(frame, LLMTextFrame):
            if self.turn is not None:
                self.turn.reply.append(frame.text or "")
        elif isinstance(frame, TTSTextFrame):
            if self.turn is not None:
                self.turn.spoken.append((frame.text or "") + " ")
        elif isinstance(frame, FunctionCallInProgressFrame):
            call = getattr(frame, "tool_call_id", None)
            if self.turn is not None and call not in self._calls:
                self._calls.add(call)
                self.turn.tools.append(frame.function_name)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if self.turn is not None and self.turn.first_audio is None:
                self.turn.first_audio = now
            self.set_state("speaking")
            self._activity()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self.set_state("listening")
            self._activity()
        elif isinstance(frame, InterruptionFrame):
            if self.turn is not None and self.bot_speaking:
                self.turn.interrupted = True
        elif isinstance(frame, MetricsFrame):
            for item in frame.data or []:
                if self.turn is None:
                    break
                if isinstance(item, TTFBMetricsData):
                    name = (item.processor or "").lower()
                    if "llm" in name and self.turn.llm_ttfb is None:
                        self.turn.llm_ttfb = float(item.value)
                    elif "tts" in name and self.turn.tts_ttfb is None:
                        self.turn.tts_ttfb = float(item.value)
                elif isinstance(item, LLMUsageMetricsData):
                    value = item.value
                    for key in ("prompt_tokens", "completion_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens"):
                        self.turn.tokens[key] = self.turn.tokens.get(key, 0) + int(getattr(value, key, 0) or 0)
                elif isinstance(item, TTSUsageMetricsData):
                    self.turn.tts_chars += int(item.value or 0)
        elif isinstance(frame, ErrorFrame):
            text = str(getattr(frame, "error", "") or "error")
            if self.turn is not None:
                self.turn.error = text[:200]
            if self.log is not None:
                self.log.event("error", fatal=bool(getattr(frame, "fatal", False)), error=text[:500])
            if getattr(frame, "fatal", False):
                self.fatal = text
            self.set_state("error", text[:120])
            self._activity()


class Conversation:
    """One conversation's pipeline, run on the current asyncio loop.

    Args:
        settings: Voice settings.
        bridge: :class:`~yuki.voice.bridge.VoiceBridge` (its handlers are the mouth's tools).
        transport: Input and output processors (:class:`~yuki.voice.transport.PairTransport`).
        memory: :class:`~yuki.agent.memory.MemoryAccess` for the turn log, or ``None``.
        mouth_memory: Rules and memory block, fetched before the loop started.
        log: The voice log.
        speech_end: Returns the monotonic time of the user's last voiced audio.
        on_state: ``(state, detail)`` callback (called on the voice thread).
        session: Id of this conversation, for the logs.
    """

    def __init__(self, *, settings: Settings, bridge: Any, transport: Any, memory: Any, mouth_memory: MouthMemory,
                 log: Any, speech_end: Callable[[], float | None], on_state: Callable[[str, str], None],
                 session: str, extra_observers: list[Any] | None = None, tts_provider: str | None = None) -> None:
        self.settings = settings
        self.bridge = bridge
        self.transport = transport
        self.memory = memory
        self.mouth_memory = mouth_memory
        self.log = log
        self.session = session
        self.tts_provider = tts_provider or settings.voice_tts_provider
        self._stop = asyncio.Event()
        self._activity = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.reason = "running"
        self.observer = TurnObserver(
            settings=settings, log=log, memory=memory, session=session, speech_end=speech_end,
            on_state=on_state, tts_provider=self.tts_provider, activity=self._activity.set,
        )
        self.worker: Any = None
        self.started_at = time.monotonic()
        self._extra_observers = list(extra_observers or [])

    def request_stop(self, reason: str = "stopped") -> None:
        """Ask the conversation to end (thread-safe)."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._set_stop, reason)

    def _set_stop(self, reason: str) -> None:
        if self.reason == "running":
            self.reason = reason
        self._stop.set()
        self._activity.set()

    def interrupt(self) -> None:
        """Cut Yuki's current speech (the cancel hotkey). Thread-safe."""
        loop, worker = self._loop, self.worker
        if loop is None or worker is None:
            return
        from pipecat.frames.frames import InterruptionWorkerFrame

        asyncio.run_coroutine_threadsafe(worker.queue_frame(InterruptionWorkerFrame()), loop)

    # -- building ----------------------------------------------------------------

    def _services(self) -> tuple[Any, Any, Any]:
        import pipecat.services.llm_service as llm_service_module
        from anthropic import AsyncAnthropicBedrock
        from pipecat.services.anthropic.llm import AnthropicLLMService
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        from pipecat.transcriptions.language import Language

        s = self.settings
        # Pipecat's standing async-tool paragraph uses an em dash; a model copies the
        # punctuation it is shown, and the user asked for none. Same words, a comma.
        llm_service_module.ASYNC_TOOL_INSTRUCTIONS = llm_service_module.ASYNC_TOOL_INSTRUCTIONS.replace(" \u2014 ", ", ")
        hints = [Language(code) for code in s.voice_stt_languages] if s.voice_stt_model.endswith("multi") else None
        stt = DeepgramFluxSTTService(
            api_key=get_key(DEEPGRAM) or "",
            enable_eager_end_of_turn=s.voice_eager_eot_threshold is not None,
            settings=DeepgramFluxSTTService.Settings(
                model=s.voice_stt_model, language_hints=hints, eot_threshold=s.voice_eot_threshold,
                eager_eot_threshold=s.voice_eager_eot_threshold, eot_timeout_ms=s.voice_eot_timeout_ms,
                keyterm=["Yuki", *s.user_names],
            ),
        )
        self._client = AsyncAnthropicBedrock(aws_region=s.aws_region)
        llm = _layered_service(AnthropicLLMService, self.mouth_memory)(
            api_key="bedrock",  # unused: the Bedrock client carries the auth
            client=self._client,
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                model=s.voice_mouth_model, max_tokens=s.voice_mouth_max_tokens, enable_prompt_caching=True,
                thinking=AnthropicLLMService.ThinkingConfig(type="disabled"),
                # The core layer: Pipecat composes its async-tool paragraphs onto it; the
                # service subclass sends the result as the first of the prompt's layers.
                system_instruction=MOUTH_PROMPT,
            ),
        )
        llm.register_function("tell_yuki", self.bridge.tell_yuki, cancel_on_interruption=False,
                              cancellable_by_llm=True, timeout_secs=None)
        llm.register_function("yuki_status", self.bridge.yuki_status)
        provider = self.tts_provider
        voice = (s.voice_tts_voice if provider == s.voice_tts_provider else "") or DEFAULT_VOICES.get(provider, "")
        if provider == "deepgram":
            from pipecat.services.deepgram.tts import DeepgramTTSService

            tts = DeepgramTTSService(api_key=get_key(DEEPGRAM) or "",
                                     settings=DeepgramTTSService.Settings(voice=voice))
        else:
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

            tts = ElevenLabsTTSService(api_key=get_key(ELEVENLABS) or "",
                                       settings=ElevenLabsTTSService.Settings(voice=voice, model=s.voice_tts_model))
        return stt, llm, tts

    def _build(self) -> Any:
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMAssistantAggregatorParams, LLMContextAggregatorPair,
        )

        stt, llm, tts = self._services()
        tools = ToolsSchema(standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["properties"],
                           required=t["required"]) for t in (TELL_YUKI, YUKI_STATUS)
        ])
        context = LLMContext(tools=tools)
        user_agg, assistant_agg = LLMContextAggregatorPair(
            context, assistant_params=LLMAssistantAggregatorParams(enable_auto_context_summarization=True),
        )
        hold = hold_until_connected(lambda: bool(getattr(stt, "_transport_is_active", lambda: True)()), self.log)
        pipeline = Pipeline([self.transport.input(), hold, stt, user_agg, llm, tts, self.transport.output(),
                             assistant_agg])
        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True,
                                  audio_in_sample_rate=16_000, audio_out_sample_rate=24_000),
            idle_timeout_secs=None,  # the default would cancel an always-open session after 300 s
            enable_rtvi=False,
            observers=[self.observer.as_observer(), *self._extra_observers],
        )
        return self.worker

    # -- running -----------------------------------------------------------------

    async def run(self) -> str:
        """Run until idle, stopped or failed. Returns why it ended."""
        from pipecat.workers.runner import WorkerRunner

        quiet_pipecat_logging(self.settings.voice_log_dir)

        self._loop = asyncio.get_running_loop()
        self.bridge.attach(self._loop)
        self.bridge.announce = self._announce
        runner = WorkerRunner(handle_sigint=False, check_dangling_tasks=False)
        worker = self._build()
        await runner.add_workers(worker)
        self.observer.set_state("listening")
        warm = asyncio.create_task(self._warm())
        watch = asyncio.create_task(self._watch(runner))
        try:
            await runner.run()
        finally:
            watch.cancel()
            warm.cancel()
            self.observer.close()
            self.bridge.closing = True
            self.bridge.attach(None)
            self.bridge.announce = None
        if self.observer.fatal and self.reason == "running":
            self.reason = "error"
        return self.reason

    async def _warm(self) -> None:
        """Open the Bedrock connection while Deepgram connects (a one-token request).

        Each conversation has a fresh client (it belongs to this event loop), so
        without this the first reply pays the TLS handshake. Logged as ``warm``.
        """
        started = time.monotonic()
        try:
            await self._client.messages.create(
                model=self.settings.voice_mouth_model, max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            self.log.event("warm", ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            self.log.event("warm", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _announce(self, payload: dict[str, Any]) -> None:
        """A result with no open tool call (a correction that came too late): say it now."""
        import json

        from pipecat.frames.frames import LLMMessagesAppendFrame

        if self.worker is not None:
            message = {"role": "developer", "content": json.dumps({"late_result": payload}, ensure_ascii=False)}
            await self.worker.queue_frame(LLMMessagesAppendFrame(messages=[message], run_llm=True))

    async def _watch(self, runner: Any) -> None:
        """End the conversation when it has been quiet long enough, or on stop / fatal error."""
        s = self.settings
        last = time.monotonic()
        while True:
            obs = self.observer
            busy = obs.user_speaking or obs.bot_speaking or obs.generating
            owed = self.bridge.open_voice_jobs()
            limit = s.voice_hold_max_s if owed else s.voice_idle_s
            if owed and not busy:
                obs.set_state("working")
            remaining = limit - (time.monotonic() - last)
            if self._stop.is_set() or obs.fatal:
                break
            if not busy and remaining <= 0:
                self._set_stop("held_too_long" if owed else "idle")
                break
            self._activity.clear()
            try:
                await asyncio.wait_for(self._activity.wait(), timeout=max(0.05, remaining) if not busy else None)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            last = time.monotonic()
            if self._stop.is_set():
                break
        self.bridge.closing = True
        await runner.cancel()
